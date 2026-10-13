#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from audio_acquisition import probe_audio, sha256_file
from podcast_transcription import (
    DEFAULT_VAD_MODEL,
    DEFAULT_WINDOW_POLICY,
    TranscriptionRunner,
    atomic_json,
    hint_failure,
)
from podcast_asr import TemporalCoverageError
from podcast_state import PodcastStore, file_sha256, payload_hash

MIGRATIONS = SCRIPTS / "migrations"
POLICY = HERE.parent / "references" / "current" / "state-policy-v1.json"


RUNTIME_READY = (Path(__import__("os").environ.get("PODNOTE_VAD_RUNTIME", "/var/lib/podnote/vad-runtime")) / "READY.json").is_file()


@unittest.skipUnless(RUNTIME_READY and __import__("shutil").which("ffmpeg"),
                     "needs ffmpeg and the speech-detection runtime (PODNOTE_VAD_RUNTIME)")
class TranscriptionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.audio = self.root / "source.wav"
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=2", "-ar", "16000", "-ac", "1",
                "-c:a", "pcm_s16le", "-y", str(self.audio),
            ],
            check=True,
        )
        self.store = PodcastStore(self.root / "state.sqlite3")
        self.job_id = "podcast-transcription-fixture"
        self.store.create_job(self.job_id, "episode-fixture")
        for stage in ("resolving", "acquiring_audio", "transcribing"):
            job = self.store.get_job(self.job_id)
            self.store.transition_job(self.job_id, stage, expected_revision=job["revision"], reason="fixture")
        self.vad_calls = 0
        self.asr_calls = 0

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def fake_vad(self, audio: Path, model: Path, policy: Path, output: Path) -> dict:
        self.vad_calls += 1
        duration_ms = int(round(probe_audio(audio)["duration_seconds"] * 1000))
        body = {
            "schema_version": 1,
            "artifact_type": "vad-speech",
            "source": {
                "path": str(audio.resolve()),
                "sha256": sha256_file(audio),
                "decoded_duration_ms": duration_ms,
                "sample_rate_hz": 16000,
            },
            "model": {"name": "silero-vad", "sha256": file_sha256(model)},
            "policy_sha256": file_sha256(policy),
            "request_identity_sha256": payload_hash({"audio": sha256_file(audio)}),
            "speech_intervals_ms": [{"start_ms": 100, "end_ms": duration_ms - 100}],
        }
        atomic_json(output, body)
        return body

    def fake_asr(self, window_audio: Path, raw_output: Path, model: str, base_url: str | None) -> dict:
        self.asr_calls += 1
        duration = probe_audio(window_audio)["duration_seconds"]
        body = {
            "model": model,
            "language": "en",
            "text": "A complete fixture transcript.",
            "segments": [{"start": 0.0, "end": duration, "text": "A complete fixture transcript."}],
            "finish_reason": "stop",
        }
        atomic_json(raw_output, body)
        return body

    def runner(self) -> TranscriptionRunner:
        return TranscriptionRunner(
            self.store, self.job_id, self.audio, self.root / "job",
            vad_model_path=DEFAULT_VAD_MODEL,
            window_policy_path=DEFAULT_WINDOW_POLICY,
            vad_runner=self.fake_vad,
            request_runner=self.fake_asr,
        )


class TranscriptionIntegrationTests(TranscriptionFixture):
    def test_end_to_end_transcription_is_durable_and_moves_to_validation(self) -> None:
        result = self.runner().run_all()
        self.assertEqual(result["status"], "transcript_assembled")
        self.assertEqual(result["job_stage"], "validating_transcript")
        self.assertEqual(self.vad_calls, 1)
        self.assertEqual(self.asr_calls, 1)
        transcript = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(transcript["chunks"]), 1)
        self.assertEqual(transcript["status"], "complete")
        self.assertEqual(transcript["source"], "omlx_asr")
        self.assertEqual(transcript["chunks"][0]["text"], "A complete fixture transcript.")
        self.assertNotIn("start_ms", transcript["chunks"][0])
        artifact_types = {item["artifact_type"] for item in self.store.list_artifacts(self.job_id)}
        self.assertTrue({
            "vad-speech", "asr-window-plan", "asr-window-input", "asr-window-raw",
            "asr-window-accepted", "ordered-transcript",
        }.issubset(artifact_types))
        self.assertEqual(self.store.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_key_check").fetchall(), [])


    def test_open_asr_attempt_with_durable_raw_output_is_recovered_without_resubmission(self) -> None:
        runner = self.runner()
        runner.prepare()
        packet = self.store.list_packets(self.job_id, "transcribing")[0]
        attempt = self.store.start_attempt(
            self.job_id, stage="transcribing", operation="transcribe_window", operation_class="asr_window",
            payload_sha256=packet["payload_sha256"], packet_id=packet["packet_id"], packet_version=1,
            deterministic=False, generation_started=False, model=runner.asr_model,
            expected_revision=self.store.get_job(self.job_id)["revision"],
        )
        raw_path = runner.asr_root / "raw" / f"{packet['packet_id']}-{attempt['attempt_id']}.json"
        duration = probe_audio(runner.asr_root / "windows" / "window-0001.wav")["duration_seconds"]
        atomic_json(raw_path, {
            "model": runner.asr_model,
            "language": "en",
            "text": "Recovered output.",
            "segments": [{"start": 0.0, "end": duration, "text": "Recovered output."}],
            "finish_reason": "stop",
        })
        result = runner.transcribe_next()
        self.assertEqual(result["status"], "window_accepted")
        self.assertEqual(self.asr_calls, 0)
        final = runner.transcribe_next()
        self.assertEqual(final["status"], "transcript_assembled")
        transcript = json.loads(Path(final["path"]).read_text(encoding="utf-8"))
        self.assertEqual(transcript["chunks"][0]["text"], "Recovered output.")

    def test_incomplete_window_coverage_blocks_and_preserves_raw_evidence(self) -> None:
        def incomplete_asr(window_audio: Path, raw_output: Path, model: str, base_url: str | None) -> dict:
            body = {
                "model": model,
                "language": "en",
                "text": "Truncated.",
                "segments": [{"start": 0.0, "end": 0.25, "text": "Truncated."}],
                "finish_reason": "stop",
            }
            atomic_json(raw_output, body)
            return body

        runner = TranscriptionRunner(
            self.store, self.job_id, self.audio, self.root / "job",
            vad_model_path=DEFAULT_VAD_MODEL,
            window_policy_path=DEFAULT_WINDOW_POLICY,
            vad_runner=self.fake_vad,
            request_runner=incomplete_asr,
        )
        runner.prepare()
        with self.assertRaises(TemporalCoverageError):
            runner.transcribe_next()
        self.assertEqual(self.store.get_job(self.job_id)["stage"], "blocked_needs_human")
        failures = self.store.connection.execute(
            "SELECT * FROM attempts WHERE job_id=? AND kind='failure'", (self.job_id,)
        ).fetchall()
        self.assertEqual(failures[-1]["failure_class"], "coverage_incomplete")
        self.assertEqual(failures[-1]["retryable"], 0)
        self.assertEqual(len(self.store.list_artifacts(self.job_id, "asr-window-raw")), 1)
        self.assertEqual(len(self.store.list_artifacts(self.job_id, "asr-window-accepted")), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class NameHintTests(TranscriptionFixture):
    HINT = "Podcast: Ask the Astronomer. Hosts: Rowan Mae Ellis. Names: Arthur Conan Doyle. Episode: The Baskervilles."

    def hinted_runner(self, answer_with_hint: str) -> TranscriptionRunner:
        self.prompts = []

        def asr(window_audio, raw_output, model, base_url, prompt=None):
            self.prompts.append(prompt)
            duration = probe_audio(window_audio)["duration_seconds"]
            text = answer_with_hint if prompt else "Rowan talks to Arthur Conan Doyle about the moors."
            body = {"model": model, "language": "en", "text": text,
                    "segments": [{"start": 0.0, "end": duration, "text": text}], "finish_reason": "stop"}
            atomic_json(raw_output, body)
            return body

        return TranscriptionRunner(self.store, self.job_id, self.audio, self.root / "job",
                                   vad_model_path=DEFAULT_VAD_MODEL, window_policy_path=DEFAULT_WINDOW_POLICY,
                                   vad_runner=self.fake_vad, request_runner=asr, asr_prompt=self.HINT)

    def accepted(self) -> dict:
        path = next((self.root / "job" / "asr" / "accepted").glob("*.json"))
        return json.loads(path.read_text(encoding="utf-8"))

    def test_hint_is_sent_and_recorded(self) -> None:
        result = self.hinted_runner("Rowan talks to Arthur Conan Doyle about the Baskervilles.").run_all()
        self.assertEqual(result["status"], "transcript_assembled")
        self.assertEqual([self.HINT], self.prompts)
        self.assertEqual("used", self.accepted()["asr_hint"])

    def test_a_window_that_repeats_the_hint_is_transcribed_again_without_it(self) -> None:
        result = self.hinted_runner(self.HINT).run_all()
        self.assertEqual(result["status"], "transcript_assembled")
        self.assertEqual([self.HINT, None], self.prompts)
        accepted = self.accepted()
        self.assertEqual("dropped: the transcript repeats the hint", accepted["asr_hint"])
        self.assertEqual("Rowan talks to Arthur Conan Doyle about the moors.", accepted["text"])

    def test_a_shorter_hint_is_tried_before_none(self) -> None:
        self.prompts = []

        def asr(window_audio, raw_output, model, base_url, prompt=None):
            self.prompts.append(prompt)
            duration = probe_audio(window_audio)["duration_seconds"]
            text = (prompt + " ") * 20 if prompt == self.HINT else "Rowan talks about history."
            body = {"model": model, "language": "en", "text": text,
                    "segments": [{"start": 0.0, "end": duration, "text": text}], "finish_reason": "stop"}
            atomic_json(raw_output, body)
            return body

        runner = TranscriptionRunner(self.store, self.job_id, self.audio, self.root / "job",
                                     vad_model_path=DEFAULT_VAD_MODEL, window_policy_path=DEFAULT_WINDOW_POLICY,
                                     vad_runner=self.fake_vad, request_runner=asr,
                                     asr_prompt=[self.HINT, "Podcast: Ask the Astronomer."])
        self.assertEqual("transcript_assembled", runner.run_all()["status"])
        self.assertEqual([self.HINT, "Podcast: Ask the Astronomer."], self.prompts)   # read back, so the shorter one
        self.assertEqual("used (shorter hint)", self.accepted()["asr_hint"])

    def test_hint_failure(self) -> None:
        window = {"duration_ms": 14 * 60000}
        speech = " ".join(f"word{i}" for i in range(2000))
        self.assertEqual("", hint_failure({"text": speech}, window, self.HINT))
        self.assertEqual("14 words for 14.0 minutes", hint_failure({"text": " ".join(["w"] * 14)}, window, self.HINT))
        self.assertEqual("the transcript repeats the hint", hint_failure({"text": self.HINT * 40}, window, self.HINT))
        # a host reading the description once inside a long window is fine
        self.assertEqual("", hint_failure({"text": speech + " " + self.HINT}, window, self.HINT))
