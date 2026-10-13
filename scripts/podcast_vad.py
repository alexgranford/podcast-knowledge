#!/usr/bin/env python3
"""Standalone streaming Silero ONNX VAD for podcast transcription.

This executable intentionally has no podcast-module imports. It runs
inside the isolated podcast VAD virtual environment and atomically emits one
immutable speech-interval artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import subprocess
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

# The local VAD is offline. Suppress ONNX Runtime's host-fingerprint telemetry
# probe before importing its native module.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

import numpy as np
import onnxruntime as ort

SAMPLE_RATE = 16000


class VADError(RuntimeError):
    pass


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def atomic_json(path: str | Path, value: dict) -> None:
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


def probe_audio(path: str | Path) -> dict:
    command = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=index,codec_type",
        "-of", "json", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise VADError(f"ffprobe failed: {result.stderr.strip()[-1000:]}")
    try:
        body = json.loads(result.stdout)
        duration = float(body["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise VADError("ffprobe returned no valid duration") from error
    if duration <= 0 or not any(stream.get("codec_type") == "audio" for stream in body.get("streams", [])):
        raise VADError("Input has no valid audio stream")
    return {"duration_seconds": duration}


def ffmpeg_version() -> str:
    result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode or not result.stdout:
        raise VADError("ffmpeg is unavailable")
    return result.stdout.splitlines()[0]


class SileroONNX:
    """Pure NumPy port of Silero's MIT-licensed ONNX wrapper."""

    def __init__(self, model_path: str | Path, frame_samples: int = 512):
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"], sess_options=options
        )
        self.frame_samples = frame_samples
        self.context_samples = 64
        self.reset()

    def reset(self) -> None:
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, self.context_samples), dtype=np.float32)

    def probability(self, frame: np.ndarray) -> float:
        if frame.dtype != np.float32 or frame.shape != (self.frame_samples,):
            raise ValueError(f"Expected float32 frame ({self.frame_samples},), received {frame.dtype} {frame.shape}")
        input_audio = np.concatenate((self.context, frame.reshape(1, -1)), axis=1)
        output, next_state = self.session.run(
            None,
            {
                "input": input_audio,
                "state": self.state,
                "sr": np.array(SAMPLE_RATE, dtype=np.int64),
            },
        )
        self.state = next_state.astype(np.float32, copy=False)
        self.context = input_audio[:, -self.context_samples:]
        return float(output[0, 0])


def _read_frame(stream: BinaryIO, frame_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = frame_bytes
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def speech_intervals_from_probabilities(
    probabilities: list[float],
    audio_samples: int,
    *,
    frame_samples: int,
    threshold: float,
    negative_threshold: float,
    min_speech_duration_ms: int,
    detector_min_silence_ms: int,
    speech_pad_ms: int,
) -> list[tuple[int, int]]:
    min_speech_samples = SAMPLE_RATE * min_speech_duration_ms / 1000
    min_silence_samples = SAMPLE_RATE * detector_min_silence_ms / 1000
    speech_pad_samples = int(SAMPLE_RATE * speech_pad_ms / 1000)
    triggered = False
    temporary_end = 0
    current_start = 0
    speech: list[list[int]] = []

    for index, probability in enumerate(probabilities):
        current_sample = frame_samples * index
        if probability >= threshold and temporary_end:
            temporary_end = 0
        if probability >= threshold and not triggered:
            triggered = True
            current_start = current_sample
            continue
        if probability < negative_threshold and triggered:
            if not temporary_end:
                temporary_end = current_sample
            if current_sample - temporary_end < min_silence_samples:
                continue
            if temporary_end - current_start > min_speech_samples:
                speech.append([current_start, temporary_end])
            triggered = False
            temporary_end = 0

    if triggered and audio_samples - current_start > min_speech_samples:
        speech.append([current_start, audio_samples])

    for index, interval in enumerate(speech):
        if index == 0:
            interval[0] = max(0, interval[0] - speech_pad_samples)
        if index != len(speech) - 1:
            silence = speech[index + 1][0] - interval[1]
            if silence < 2 * speech_pad_samples:
                interval[1] += silence // 2
                speech[index + 1][0] = max(0, speech[index + 1][0] - silence // 2)
            else:
                interval[1] = min(audio_samples, interval[1] + speech_pad_samples)
                speech[index + 1][0] = max(0, speech[index + 1][0] - speech_pad_samples)
        else:
            interval[1] = min(audio_samples, interval[1] + speech_pad_samples)

    return [
        (int(round(start * 1000 / SAMPLE_RATE)), int(round(end * 1000 / SAMPLE_RATE)))
        for start, end in speech
    ]


def stream_probabilities(audio_path: str | Path, model: SileroONNX) -> tuple[list[float], int, str]:
    frame_bytes = model.frame_samples * 2
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(audio_path),
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
    ]
    probabilities: list[float] = []
    audio_samples = 0
    model.reset()
    with tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr, stdin=subprocess.DEVNULL)
        assert process.stdout is not None
        while True:
            raw = _read_frame(process.stdout, frame_bytes)
            if not raw:
                break
            if len(raw) % 2:
                process.kill()
                process.wait()
                raise VADError("ffmpeg produced an odd PCM byte count")
            samples = len(raw) // 2
            audio_samples += samples
            if len(raw) < frame_bytes:
                raw += b"\x00" * (frame_bytes - len(raw))
            frame = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            probabilities.append(model.probability(frame))
        returncode = process.wait()
        stderr.seek(0)
        diagnostic = stderr.read().decode("utf-8", errors="replace")[-2000:]
    if returncode:
        raise VADError(f"ffmpeg PCM decode failed ({returncode}): {diagnostic}")
    if not probabilities or audio_samples <= 0:
        raise VADError("ffmpeg decoded no audio samples")
    return probabilities, audio_samples, diagnostic


def run_scan(audio: Path, model_path: Path, policy_path: Path, output: Path) -> dict:
    started = time.monotonic()
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    detector = policy.get("detector_parameters") or {}
    frame_samples = int(detector.get("frame_samples", 512))
    threshold = float(detector.get("threshold", 0.5))
    negative_threshold = float(detector.get("negative_threshold", max(threshold - 0.15, 0.01)))
    min_speech_duration_ms = int(detector.get("min_speech_duration_ms", 250))
    detector_min_silence_ms = int(detector.get("min_silence_duration_ms", 100))
    speech_pad_ms = int(detector.get("speech_pad_ms", 30))
    if frame_samples != 512:
        raise VADError("The pinned 16 kHz Silero model requires frame_samples=512")
    if not 0 < negative_threshold < threshold < 1:
        raise VADError("Invalid VAD threshold configuration")

    source_sha256 = sha256_file(audio)
    model_sha256 = sha256_file(model_path)
    policy_sha256 = sha256_file(policy_path)
    probe = probe_audio(audio)
    model = SileroONNX(model_path, frame_samples)
    probabilities, audio_samples, _ = stream_probabilities(audio, model)
    decoded_duration_ms = int(round(audio_samples * 1000 / SAMPLE_RATE))
    probed_duration_ms = int(round(probe["duration_seconds"] * 1000))
    if abs(decoded_duration_ms - probed_duration_ms) > max(1000, int(probed_duration_ms * 0.002)):
        raise VADError(
            f"Decoded/probed duration mismatch: decoded={decoded_duration_ms}ms probed={probed_duration_ms}ms"
        )
    intervals = speech_intervals_from_probabilities(
        probabilities,
        audio_samples,
        frame_samples=frame_samples,
        threshold=threshold,
        negative_threshold=negative_threshold,
        min_speech_duration_ms=min_speech_duration_ms,
        detector_min_silence_ms=detector_min_silence_ms,
        speech_pad_ms=speech_pad_ms,
    )
    speech_ms = sum(end - start for start, end in intervals)
    request_identity = {
        "source_sha256": source_sha256,
        "model_sha256": model_sha256,
        "policy_sha256": policy_sha256,
        "algorithm": "podcast-silero-onnx-stream-v1",
    }
    result = {
        "schema_version": 1,
        "algorithm": request_identity["algorithm"],
        "request_identity_sha256": canonical_hash(request_identity),
        "source": {
            "path": str(audio.resolve()),
            "sha256": source_sha256,
            "ffprobe_duration_ms": probed_duration_ms,
            "decoded_duration_ms": decoded_duration_ms,
        },
        "model": {
            "name": "silero-vad",
            "package_source_version": "6.2.1",
            "path": str(model_path.resolve()),
            "sha256": model_sha256,
            "execution_provider": "CPUExecutionProvider",
        },
        "runtime": {
            "onnxruntime": ort.__version__,
            "numpy": np.__version__,
            "ffmpeg": ffmpeg_version(),
            "sample_rate": SAMPLE_RATE,
            "frame_samples": frame_samples,
            "frame_count": len(probabilities),
            "wall_time_ms": int(round((time.monotonic() - started) * 1000)),
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
        "detector_parameters": {
            "threshold": threshold,
            "negative_threshold": negative_threshold,
            "min_speech_duration_ms": min_speech_duration_ms,
            "min_silence_duration_ms": detector_min_silence_ms,
            "speech_pad_ms": speech_pad_ms,
        },
        "speech_intervals_ms": [{"start_ms": start, "end_ms": end} for start, end in intervals],
        "summary": {
            "speech_interval_count": len(intervals),
            "speech_duration_ms": speech_ms,
            "non_speech_duration_ms": max(0, decoded_duration_ms - speech_ms),
            "minimum_probability": min(probabilities),
            "maximum_probability": max(probabilities),
        },
    }
    atomic_json(output, result)
    return result


def self_test(model_path: Path) -> dict:
    model = SileroONNX(model_path)
    silence = np.zeros(512, dtype=np.float32)
    first: list[float] = []
    for _ in range(100):
        first.append(model.probability(silence))
    model.reset()
    second = [model.probability(silence) for _ in range(100)]
    maximum_delta = max(abs(left - right) for left, right in zip(first, second))
    result = {
        "model_sha256": sha256_file(model_path),
        "onnxruntime": ort.__version__,
        "numpy": np.__version__,
        "provider": model.session.get_providers(),
        "silence_max_probability": max(first),
        "repeat_max_delta": maximum_delta,
        "passed": max(first) < 0.1 and maximum_delta <= 1e-7,
    }
    if not result["passed"]:
        raise VADError(f"VAD self-test failed: {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan")
    scan.add_argument("--audio", required=True)
    scan.add_argument("--model", required=True)
    scan.add_argument("--policy", required=True)
    scan.add_argument("--output", required=True)
    test = subparsers.add_parser("self-test")
    test.add_argument("--model", required=True)
    args = parser.parse_args()
    try:
        if args.command == "self-test":
            print(json.dumps(self_test(Path(args.model)), sort_keys=True))
        else:
            result = run_scan(Path(args.audio), Path(args.model), Path(args.policy), Path(args.output))
            print(json.dumps({
                "output": str(Path(args.output).resolve()),
                "request_identity_sha256": result["request_identity_sha256"],
                "speech_interval_count": result["summary"]["speech_interval_count"],
            }, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)[:2000]}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
