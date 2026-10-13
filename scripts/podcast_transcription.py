#!/usr/bin/env python3
"""Restart-safe VAD and windowed-ASR orchestration."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from audio_acquisition import probe_audio, sha256_file
from contracts import validate
from model_arbiter import DEFAULT_STATE as DEFAULT_MODEL_ARBITER_STATE, asr_model_session
from podcast_asr import (
    TemporalCoverageError,
    WindowPolicy,
    materialize_windows,
    plan_vad_windows,
    validate_asr_window,
)
from podcast_state import PodcastStore, StateKernelError, file_sha256, payload_hash, worker_session

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = Path(os.environ.get("PODNOTE_VAD_RUNTIME", "/var/lib/podnote/vad-runtime"))
DEFAULT_VAD_CLI = ROOT / "scripts" / "podcast_vad.py"
DEFAULT_VAD_MODEL = ROOT / "assets" / "silero-vad" / "6.2.1" / "model.onnx"
DEFAULT_WINDOW_POLICY = ROOT / "references" / "current" / "asr-window-policy-v1.json"
DEFAULT_ASR_MODEL = "Qwen3-ASR-1.7B-8bit"
RequestRunner = Callable[[Path, Path, str, str | None], dict[str, Any]]
VADRunner = Callable[[Path, Path, Path, Path], dict[str, Any]]


class TranscriptionError(RuntimeError):
    pass


class WindowRequestError(TranscriptionError):
    def __init__(self, failure_class: str, failure_code: str, message: str, retryable: bool):
        super().__init__(message)
        self.failure_class = failure_class
        self.failure_code = failure_code
        self.retryable = retryable


def atomic_json(path: str | Path, value: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def artifact_descriptor(job_id: str, artifact_id: str, artifact_type: str,
                        path: str | Path) -> dict[str, Any]:
    artifact_path = Path(path)
    return {
        "artifact_id": f"{job_id}:{artifact_id}",
        "artifact_type": artifact_type,
        "path": str(artifact_path),
        "sha256": file_sha256(artifact_path),
        "byte_size": artifact_path.stat().st_size,
    }


def default_vad_runner(audio: Path, model: Path, policy: Path, output: Path,
                       runtime: Path = DEFAULT_RUNTIME) -> dict[str, Any]:
    interpreter = runtime / "venv" / "bin" / "python"
    result = subprocess.run(
        [
            str(interpreter), str(DEFAULT_VAD_CLI), "scan", "--audio", str(audio),
            "--model", str(model), "--policy", str(policy), "--output", str(output),
        ],
        capture_output=True, text=True, timeout=3600, check=False,
    )
    if result.returncode or not output.is_file():
        raise WindowRequestError(
            "unknown_internal", "vad_process_failed",
            (result.stdout + "\n" + result.stderr).strip()[-2000:], True,
        )
    return json.loads(output.read_text(encoding="utf-8"))


def default_request_runner(window_audio: Path, raw_output: Path, model: str,
                           base_url: str | None, prompt: str | None = None) -> dict[str, Any]:
    error_output = raw_output.with_suffix(".error.json")
    script = ROOT / "scripts" / "omlx_request.py"
    command = [
        sys.executable, str(script), "--audio", str(window_audio), "--output", str(raw_output),
        "--error-output", str(error_output), "--model", model,
    ]
    if base_url:
        command.extend(["--base-url", base_url])
    if prompt:
        command.extend(["--prompt", prompt])
    result = subprocess.run(command, capture_output=True, text=True, timeout=22000, check=False)
    if result.returncode or not raw_output.is_file():
        detail: Any = None
        if error_output.is_file():
            try:
                detail = json.loads(error_output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                detail = error_output.read_text(encoding="utf-8", errors="replace")[-2000:]
        status = detail.get("http_status") if isinstance(detail, dict) else None
        retryable = status is None or status == 429 or (isinstance(status, int) and status >= 500)
        raise WindowRequestError(
            "transport_timeout" if status is None else "model_crashed" if retryable else "response_malformed",
            "omlx_window_request_failed", str(detail or result.stderr or result.stdout)[-2000:], retryable,
        )
    return json.loads(raw_output.read_text(encoding="utf-8"))


def hint_failure(raw: dict[str, Any], window: dict[str, Any], hint: str) -> str:
    """Why a window transcribed with a name hint cannot be kept ("" when it can). Qwen3-ASR can
    answer a hint with the hint itself and drop the speech (a 14-minute window came back as 14
    words); the coverage check does not see it, because the single segment still spans the window."""
    text = str(raw.get("text") or "") or " ".join(str(s.get("text") or "") for s in raw.get("segments") or [])
    words = text.split()
    minutes = window["duration_ms"] / 60000
    if minutes >= 1 and len(words) < 40 * minutes:
        return f"{len(words)} words for {minutes:.1f} minutes"
    grams = lambda value: {" ".join(g) for g in zip(*[re.findall(r"\w+", value.lower())[i:] for i in range(6)])}
    spoken, hinted = grams(text), grams(hint)
    if spoken and len(spoken & hinted) / len(spoken) > 0.3:
        return "the transcript repeats the hint"
    return ""


class TranscriptionRunner:
    def __init__(self, store: PodcastStore, job_id: str, audio_path: str | Path,
                 job_root: str | Path, *, window_policy_path: str | Path = DEFAULT_WINDOW_POLICY,
                 vad_model_path: str | Path = DEFAULT_VAD_MODEL, runtime_root: str | Path = DEFAULT_RUNTIME,
                 asr_model: str = DEFAULT_ASR_MODEL, omlx_base_url: str | None = None,
                 model_arbiter_root: str | Path = DEFAULT_MODEL_ARBITER_STATE,
                 vad_runner: VADRunner | None = None, request_runner: RequestRunner | None = None,
                 asr_prompt: str | list[str] = ""):
        self.store = store
        self.job_id = job_id
        self.audio_path = Path(audio_path)
        self.job_root = Path(job_root)
        self.asr_root = self.job_root / "asr"
        self.window_policy_path = Path(window_policy_path)
        self.vad_model_path = Path(vad_model_path)
        self.runtime_root = Path(runtime_root)
        self.asr_model = asr_model
        self.omlx_base_url = omlx_base_url
        self.model_arbiter_root = Path(model_arbiter_root)
        self.vad_runner = vad_runner or (
            lambda audio, model, policy, output: default_vad_runner(audio, model, policy, output, self.runtime_root)
        )
        self.use_model_arbiter = request_runner is None
        self.request_runner = request_runner or default_request_runner
        # name hints for every window (show, hosts, names, description), tried in order: the full one,
        # then shorter ones, then none
        prompts = [asr_prompt] if isinstance(asr_prompt, str) else list(asr_prompt)
        self.asr_prompts = [p.strip() for p in prompts if p and p.strip()]

    def _registered(self, artifact_id: str) -> dict[str, Any] | None:
        full_id = f"{self.job_id}:{artifact_id}"
        for artifact in self.store.list_artifacts(self.job_id):
            if artifact["artifact_id"] == full_id:
                path = Path(artifact["path"])
                if not path.is_file() or file_sha256(path) != artifact["sha256"] or path.stat().st_size != artifact["byte_size"]:
                    raise TranscriptionError(f"Registered artifact failed read-back: {full_id}")
                return artifact
        return None

    def _packet_id(self, sequence: int) -> str:
        # packets.packet_id is globally keyed in schema v1; namespace it by the
        # full job hash without exposing user-controlled text in filenames.
        return f"asr-{payload_hash(self.job_id)}-window-{sequence:04d}"

    def validate_runtime(self) -> dict[str, Any]:
        ready_path = self.runtime_root / "READY.json"
        if not ready_path.is_file():
            raise TranscriptionError(f"VAD runtime readiness record is absent: {ready_path}")
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        checks = {
            "model_sha256": file_sha256(self.vad_model_path),
            "cli_sha256": file_sha256(DEFAULT_VAD_CLI),
            "policy_sha256": file_sha256(self.window_policy_path),
            "requirements_lock_sha256": file_sha256(self.runtime_root / "requirements.lock"),
        }
        if ready.get("status") != "ready" or any(ready.get(key) != value for key, value in checks.items()):
            raise TranscriptionError("VAD runtime readiness/hash validation failed")
        if not (self.runtime_root / "venv" / "bin" / "python").is_file():
            raise TranscriptionError("VAD runtime interpreter is absent")
        return ready

    def _validate_vad_artifact(self, path: Path) -> dict[str, Any]:
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("schema_version") != 1:
            raise TranscriptionError("Unsupported VAD artifact schema")
        if body.get("source", {}).get("sha256") != sha256_file(self.audio_path):
            raise TranscriptionError("VAD artifact source hash mismatch")
        if body.get("model", {}).get("sha256") != file_sha256(self.vad_model_path):
            raise TranscriptionError("VAD artifact model hash mismatch")
        duration_ms = int(body["source"]["decoded_duration_ms"])
        intervals: list[tuple[int, int]] = []
        cursor = -1
        for item in body.get("speech_intervals_ms", []):
            start, end = int(item["start_ms"]), int(item["end_ms"])
            if start < 0 or end <= start or end > duration_ms or start < cursor:
                raise TranscriptionError("VAD artifact contains invalid or unordered speech intervals")
            intervals.append((start, end))
            cursor = end
        return body

    @staticmethod
    def _validate_materialized_window(path: Path, window: dict[str, Any]) -> dict[str, Any]:
        probe = probe_audio(path)
        expected = int(window["duration_ms"])
        actual = int(round(float(probe["duration_seconds"]) * 1000))
        tolerance = max(50, int(expected * 0.001))
        if abs(actual - expected) > tolerance:
            raise TranscriptionError(
                f"Recovered window duration mismatch: expected {expected} ms, received {actual} ms"
            )
        return probe

    def _finalize_open_attempt(self, operation: str, artifact_path: Path,
                               descriptor: Callable[[], dict[str, Any]], validator: Callable[[Path], Any]) -> bool:
        open_attempts = [
            attempt for attempt in self.store.list_open_attempts(self.job_id)
            if attempt["operation"] == operation
        ]
        if not open_attempts:
            return False
        if len(open_attempts) != 1:
            raise TranscriptionError(f"Multiple open attempts for {operation}")
        attempt = open_attempts[0]
        if artifact_path.is_file():
            validator(artifact_path)
            self.store.succeed_attempt(
                attempt["attempt_id"], expected_revision=self.store.get_job(self.job_id)["revision"],
                artifacts=[descriptor()], finish_reason="recovered_persisted_output",
            )
            return True
        self.store.fail_attempt(
            attempt["attempt_id"], failure_class="unknown_internal", failure_code="interrupted_without_output",
            diagnostic=f"Open {operation} attempt had no durable output", retryable=True,
            expected_revision=self.store.get_job(self.job_id)["revision"],
        )
        return False

    def prepare(self) -> dict[str, Any]:
        job = self.store.get_job(self.job_id)
        if job["stage"] != "transcribing":
            raise TranscriptionError(f"Transcription preparation requires transcribing, received {job['stage']}")
        if not self.audio_path.is_file():
            raise TranscriptionError(f"Source audio is absent: {self.audio_path}")
        self.validate_runtime()
        self.asr_root.mkdir(parents=True, exist_ok=True)
        source_sha256 = sha256_file(self.audio_path)
        source_duration_ms = int(round(probe_audio(self.audio_path)["duration_seconds"] * 1000))
        policy = WindowPolicy.load(self.window_policy_path)

        vad_path = self.asr_root / "vad-speech-v1.json"
        vad_descriptor = lambda: artifact_descriptor(self.job_id, "vad-speech-v1", "vad-speech", vad_path)
        if self._registered("vad-speech-v1"):
            vad = self._validate_vad_artifact(vad_path)
        else:
            if vad_path.is_file() and not any(
                attempt["operation"] == "vad_scan" for attempt in self.store.list_open_attempts(self.job_id)
            ):
                vad_path.unlink()
            if self._finalize_open_attempt("vad_scan", vad_path, vad_descriptor, self._validate_vad_artifact):
                vad = self._validate_vad_artifact(vad_path)
            else:
                request_identity = payload_hash({
                    "source_sha256": source_sha256,
                    "policy_sha256": file_sha256(self.window_policy_path),
                    "model_sha256": file_sha256(self.vad_model_path),
                })
                attempt = self.store.start_attempt(
                    self.job_id, stage="transcribing", operation="vad_scan", operation_class="vad_scan",
                    payload_sha256=request_identity, deterministic=False, generation_started=False,
                    model="silero-vad-6.2.1-onnx", expected_revision=self.store.get_job(self.job_id)["revision"],
                )
                started = time.monotonic()
                try:
                    self.vad_runner(self.audio_path, self.vad_model_path, self.window_policy_path, vad_path)
                    vad = self._validate_vad_artifact(vad_path)
                except WindowRequestError as error:
                    self.store.fail_attempt(
                        attempt["attempt_id"], failure_class=error.failure_class,
                        failure_code=error.failure_code, diagnostic=str(error), retryable=error.retryable,
                        expected_revision=self.store.get_job(self.job_id)["revision"],
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                    raise
                self.store.succeed_attempt(
                    attempt["attempt_id"], expected_revision=self.store.get_job(self.job_id)["revision"],
                    elapsed_ms=int((time.monotonic() - started) * 1000), artifacts=[vad_descriptor()],
                )

        vad_duration_ms = int(vad["source"]["decoded_duration_ms"])
        if abs(vad_duration_ms - source_duration_ms) > max(1000, int(source_duration_ms * 0.002)):
            raise TranscriptionError("VAD/source duration mismatch")
        speech = [(int(item["start_ms"]), int(item["end_ms"])) for item in vad["speech_intervals_ms"]]
        windows = plan_vad_windows(vad_duration_ms, speech, policy)
        plan_path = self.asr_root / "window-plan-v1.json"
        plan_body = {
            "schema_version": 1,
            "job_id": self.job_id,
            "source_sha256": source_sha256,
            "source_duration_ms": vad_duration_ms,
            "policy_sha256": file_sha256(self.window_policy_path),
            "vad_artifact_sha256": file_sha256(vad_path),
            "asr_model": self.asr_model,
            "windows": windows,
        }
        if self._registered("window-plan-v1"):
            persisted = json.loads(plan_path.read_text(encoding="utf-8"))
            if payload_hash(persisted) != payload_hash(plan_body):
                raise TranscriptionError("Registered window plan differs from deterministic reconstruction")
        else:
            atomic_json(plan_path, plan_body)
            self.store.record_decision(
                self.job_id, operation="vad_window_plan", expected_revision=self.store.get_job(self.job_id)["revision"],
                payload={"window_count": len(windows)},
                artifacts=[artifact_descriptor(self.job_id, "window-plan-v1", "asr-window-plan", plan_path)],
            )

        packets = []
        for window in windows:
            packet_id = self._packet_id(int(window["sequence"]))
            packet_payload = {
                "source_sha256": source_sha256,
                "policy_sha256": plan_body["policy_sha256"],
                "asr_model": self.asr_model,
                "window": window,
            }
            packets.append({
                "packet_id": packet_id,
                "packet_version": 1,
                "stage": "transcribing",
                "source_range": {
                    "start_ms": window["start_ms"], "end_ms": window["end_ms"],
                    "duration_ms": window["duration_ms"], "sequence": window["sequence"],
                },
                "overlap_units": 0,
                "payload_sha256": payload_hash(packet_payload),
            })
        self.store.record_packets(
            self.job_id, packets, expected_revision=self.store.get_job(self.job_id)["revision"]
        )

        window_dir = self.asr_root / "windows"
        manifests = []
        for window in windows:
            sequence = int(window["sequence"])
            packet_id = self._packet_id(sequence)
            artifact_id = f"asr-window-input-{sequence:04d}"
            target = window_dir / f"window-{sequence:04d}.wav"
            if self._registered(artifact_id):
                manifests.append({**window, "path": str(target), "window_sha256": file_sha256(target)})
                continue
            operation = f"materialize_{packet_id}"
            descriptor = lambda: artifact_descriptor(
                self.job_id, artifact_id, "asr-window-input", target
            )
            if target.is_file() and not any(
                attempt["operation"] == operation for attempt in self.store.list_open_attempts(self.job_id)
            ):
                target.unlink()
            if self._finalize_open_attempt(
                operation, target, descriptor,
                lambda path, expected=window: self._validate_materialized_window(path, expected),
            ):
                manifests.append({**window, "path": str(target), "window_sha256": file_sha256(target)})
                continue
            attempt = self.store.start_attempt(
                self.job_id, stage="transcribing", operation=operation, operation_class="audio_window",
                payload_sha256=self.store.get_packet(self.job_id, packet_id)["payload_sha256"],
                packet_id=packet_id, packet_version=1, deterministic=False, generation_started=False,
                expected_revision=self.store.get_job(self.job_id)["revision"],
            )
            started = time.monotonic()
            try:
                manifest = materialize_windows(self.audio_path, [window], window_dir)[0]
            except Exception as error:
                self.store.fail_attempt(
                    attempt["attempt_id"], failure_class="unknown_internal", failure_code="window_materialization_failed",
                    diagnostic=f"{type(error).__name__}: {error}", retryable=True,
                    expected_revision=self.store.get_job(self.job_id)["revision"],
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            self.store.succeed_attempt(
                attempt["attempt_id"], expected_revision=self.store.get_job(self.job_id)["revision"],
                elapsed_ms=int((time.monotonic() - started) * 1000), artifacts=[descriptor()],
            )
            manifests.append(manifest)
        return {"vad": vad, "plan": plan_body, "window_manifests": manifests}

    def _window_for_packet(self, packet_id: str) -> dict[str, Any]:
        plan = json.loads((self.asr_root / "window-plan-v1.json").read_text(encoding="utf-8"))
        sequence = int(packet_id.rsplit("-", 1)[1])
        return next(window for window in plan["windows"] if int(window["sequence"]) == sequence)

    def _request_with_hint(self, window_audio: Path, raw_path: Path, window: dict[str, Any],
                           base_url: str | None) -> tuple[dict[str, Any], str]:
        """One window with the name hints in turn: a hinted answer that fails hint_failure() is retried
        with the next, shorter hint, and finally without one (Qwen3-ASR sometimes reads a hint back
        instead of the speech, and which hint length does it differs from window to window).
        Returns (raw response, hint status)."""
        problems = []
        for index, prompt in enumerate(self.asr_prompts):
            raw = self.request_runner(window_audio, raw_path, self.asr_model, base_url, prompt=prompt)
            problem = hint_failure(raw, window, prompt)
            if not problem:
                return raw, "used" if index == 0 else "used (shorter hint)"
            problems.append(problem)
            raw_path.unlink(missing_ok=True)
        raw = self.request_runner(window_audio, raw_path, self.asr_model, base_url)
        return raw, f"dropped: {'; '.join(problems)}" if problems else "none"

    def transcribe_next(self) -> dict[str, Any]:
        job = self.store.get_job(self.job_id)
        if job["stage"] != "transcribing":
            raise TranscriptionError(f"Window transcription requires transcribing, received {job['stage']}")
        packets = self.store.list_packets(self.job_id, "transcribing")
        if not packets:
            raise TranscriptionError("No ASR window packets are prepared")
        accepted_ids = {artifact["artifact_id"] for artifact in self.store.list_artifacts(self.job_id, "asr-window-accepted")}
        packet = next((item for item in packets if f"{self.job_id}:{item['packet_id']}-accepted" not in accepted_ids), None)
        if packet is None:
            return self.assemble()
        packet_id = packet["packet_id"]
        window = self._window_for_packet(packet_id)
        window_audio = self.asr_root / "windows" / f"window-{window['sequence']:04d}.wav"
        if not window_audio.is_file():
            raise TranscriptionError(f"Prepared window audio is absent: {window_audio}")
        open_attempts = [
            item for item in self.store.list_open_attempts(self.job_id)
            if item["operation"] == "transcribe_window" and item["packet_id"] == packet_id
        ]
        if len(open_attempts) > 1:
            raise TranscriptionError(f"Multiple open ASR attempts for {packet_id}")
        if open_attempts:
            attempt = open_attempts[0]
            raw_path = self.asr_root / "raw" / f"{packet_id}-{attempt['attempt_id']}.json"
            if not raw_path.is_file():
                self.store.fail_attempt(
                    attempt["attempt_id"], failure_class="unknown_internal", failure_code="interrupted_without_output",
                    diagnostic=f"Open ASR attempt for {packet_id} had no durable output", retryable=True,
                    expected_revision=self.store.get_job(self.job_id)["revision"],
                )
                open_attempts = []
        if not open_attempts:
            # The model slot is taken first: a busy slot is waiting, not a failed attempt, so it never
            # counts towards the ledger's attempt ceiling (and never blocks the episode).
            slot = asr_model_session(
                self.job_id, owner="podcast-transcription", model=self.asr_model,
                state_root=self.model_arbiter_root, base_url=self.omlx_base_url,
                operation=f"transcribe:{packet_id}",
            ) if self.use_model_arbiter else contextlib.nullcontext({"base_url": self.omlx_base_url})
            with slot as settings:
                attempt = self.store.start_attempt(
                    self.job_id, stage="transcribing", operation="transcribe_window", operation_class="asr_window",
                    payload_sha256=packet["payload_sha256"], packet_id=packet_id, packet_version=packet["packet_version"],
                    deterministic=False, generation_started=False, model=self.asr_model,
                    expected_revision=self.store.get_job(self.job_id)["revision"],
                )
                raw_path = self.asr_root / "raw" / f"{packet_id}-{attempt['attempt_id']}.json"
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                started = time.monotonic()
                try:
                    raw, hint_status = self._request_with_hint(window_audio, raw_path, window, settings["base_url"])
                    if not raw_path.is_file():
                        atomic_json(raw_path, raw)
                except Exception as caught:
                    error = caught if isinstance(caught, WindowRequestError) else WindowRequestError(
                        "model_crashed", "model_arbitration_or_request_failed",
                        f"{type(caught).__name__}: {caught}", True,
                    )
                    self.store.fail_attempt(
                        attempt["attempt_id"], failure_class=error.failure_class,
                        failure_code=error.failure_code, diagnostic=str(error), retryable=error.retryable,
                        expected_revision=self.store.get_job(self.job_id)["revision"],
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                    raise
        else:
            started = time.monotonic()
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            hint_status = "recovered output"

        raw_descriptor = artifact_descriptor(
            self.job_id, f"{packet_id}-raw-{attempt['attempt_id']}", "asr-window-raw", raw_path
        )
        try:
            coverage = validate_asr_window(raw, window, expected_model=self.asr_model)
        except TemporalCoverageError as error:
            self.store.fail_attempt(
                attempt["attempt_id"], failure_class="coverage_incomplete", failure_code="window_coverage_incomplete",
                diagnostic=str(error), retryable=False,
                expected_revision=self.store.get_job(self.job_id)["revision"],
                elapsed_ms=int((time.monotonic() - started) * 1000), artifacts=[raw_descriptor],
            )
            raise
        text = str(raw.get("text") or "").strip() or " ".join(
            str(segment.get("text") or "").strip() for segment in raw.get("segments") or []
        ).strip()
        accepted_path = self.asr_root / "accepted" / f"{packet_id}.json"
        accepted_body = {
            "schema_version": 1,
            "packet_id": packet_id,
            "packet_version": packet["packet_version"],
            "window": window,
            "model": self.asr_model,
            "raw_artifact_path": str(raw_path),
            "raw_artifact_sha256": file_sha256(raw_path),
            "text": text,
            "language": raw.get("language"),
            "coverage": coverage,
            "asr_hint": hint_status,
        }
        atomic_json(accepted_path, accepted_body)
        accepted_descriptor = artifact_descriptor(
            self.job_id, f"{packet_id}-accepted", "asr-window-accepted", accepted_path
        )
        self.store.succeed_attempt(
            attempt["attempt_id"], expected_revision=self.store.get_job(self.job_id)["revision"],
            elapsed_ms=int((time.monotonic() - started) * 1000), finish_reason=str(raw.get("finish_reason") or "accepted"),
            artifacts=[raw_descriptor, accepted_descriptor],
        )
        return {
            "status": "window_accepted", "packet_id": packet_id,
            "accepted": len(self.store.list_artifacts(self.job_id, "asr-window-accepted")),
            "total": len(packets),
        }

    def assemble(self) -> dict[str, Any]:
        packets = self.store.list_packets(self.job_id, "transcribing")
        accepted = self.store.list_artifacts(self.job_id, "asr-window-accepted")
        if len(accepted) != len(packets):
            return {"status": "windows_pending", "accepted": len(accepted), "total": len(packets)}
        transcript_path = self.asr_root / "transcript-v1.json"
        if not self._registered("transcript-v1"):
            bodies = [json.loads(Path(item["path"]).read_text(encoding="utf-8")) for item in accepted]
            bodies.sort(key=lambda body: body["window"]["sequence"])
            languages = [body.get("language") for body in bodies if body.get("language")]
            source = json.loads((self.asr_root / "window-plan-v1.json").read_text(encoding="utf-8"))
            transcript = {
                "schema_version": 1,
                "job_id": self.job_id,
                "status": "complete",
                "source": "omlx_asr",
                "model": self.asr_model,
                "language": languages[0] if languages and all(item == languages[0] for item in languages) else "en",
                "duration_seconds": int(round(source["source_duration_ms"] / 1000)),
                "audio_sha256": source["source_sha256"],
                "chunks": [
                    {
                        "id": body["window"]["sequence"],
                        "speaker": None,
                        "text": body["text"],
                        "confidence": None,
                    }
                    for body in bodies
                ],
            }
            # Per-window timing/coverage remains in immutable accepted-window
            # artifacts. The stage-boundary transcript uses the canonical
            # ordered-transcript contract consumed by post-ASR processing.
            validate("ordered_transcript", transcript)
            atomic_json(transcript_path, transcript)
            self.store.record_decision(
                self.job_id, operation="assemble_windowed_transcript",
                expected_revision=self.store.get_job(self.job_id)["revision"],
                payload={"window_count": len(bodies)},
                artifacts=[artifact_descriptor(self.job_id, "transcript-v1", "ordered-transcript", transcript_path)],
            )
        job = self.store.get_job(self.job_id)
        if job["stage"] == "transcribing":
            self.store.transition_job(
                self.job_id, "validating_transcript", expected_revision=job["revision"],
                reason="all ASR windows accepted and transcript assembled",
            )
        return {
            "status": "transcript_assembled",
            "path": str(transcript_path),
            "sha256": file_sha256(transcript_path),
            "window_count": len(packets),
            "job_stage": self.store.get_job(self.job_id)["stage"],
        }

    def run_all(self) -> dict[str, Any]:
        self.prepare()
        while self.store.get_job(self.job_id)["stage"] == "transcribing":
            result = self.transcribe_next()
            if result.get("status") == "transcript_assembled":
                return result
        raise TranscriptionError(f"Transcription stopped in {self.store.get_job(self.job_id)['stage']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one isolated podcast transcription job")
    parser.add_argument("action", choices=("prepare", "tick", "run-all"))
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--job-root", required=True)
    parser.add_argument("--window-policy", default=str(DEFAULT_WINDOW_POLICY))
    parser.add_argument("--vad-model", default=str(DEFAULT_VAD_MODEL))
    parser.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME))
    parser.add_argument("--asr-model", default=DEFAULT_ASR_MODEL)
    parser.add_argument("--omlx-base-url")
    parser.add_argument("--model-arbiter-root", default=str(DEFAULT_MODEL_ARBITER_STATE))
    arguments = parser.parse_args(argv)
    with worker_session(arguments.state_root) as store:
        runner = TranscriptionRunner(
            store, arguments.job_id, arguments.audio, arguments.job_root,
            window_policy_path=arguments.window_policy,
            vad_model_path=arguments.vad_model,
            runtime_root=arguments.runtime_root,
            asr_model=arguments.asr_model,
            omlx_base_url=arguments.omlx_base_url,
            model_arbiter_root=arguments.model_arbiter_root,
        )
        if arguments.action == "prepare":
            prepared = runner.prepare()
            result = {
                "status": "prepared",
                "job_id": arguments.job_id,
                "window_count": len(prepared["plan"]["windows"]),
                "job_stage": store.get_job(arguments.job_id)["stage"],
            }
        elif arguments.action == "tick":
            result = runner.transcribe_next()
        else:
            result = runner.run_all()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
