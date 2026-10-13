#!/usr/bin/env python3
"""VAD-aligned ASR window planning and temporal validation."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from audio_acquisition import probe_audio, sha256_file

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "references" / "current" / "asr-window-policy-v1.json"


class ASRWindowError(RuntimeError):
    pass


class VADBoundaryUnavailable(ASRWindowError):
    pass


class TemporalCoverageError(ASRWindowError):
    pass


@dataclass(frozen=True)
class WindowPolicy:
    target_window_ms: int
    search_before_ms: int
    search_after_ms: int
    minimum_silence_ms: int
    boundary_guard_ms: int
    hard_max_window_ms: int
    timestamp_tolerance_ms: int
    minimum_speech_coverage_ratio: float
    maximum_uncovered_speech_run_ms: int
    detector_parameters: dict[str, Any] | None = None
    detector: str = "silero-vad"
    schema_version: int = 1

    @classmethod
    def load(cls, path: str | Path = POLICY_PATH) -> "WindowPolicy":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        policy = cls(**data)
        policy.validate()
        return policy

    def validate(self) -> None:
        if self.schema_version != 1 or self.detector != "silero-vad":
            raise ValueError("Unsupported ASR window policy")
        positive = (
            self.target_window_ms, self.minimum_silence_ms,
            self.hard_max_window_ms, self.maximum_uncovered_speech_run_ms,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("ASR window policy durations must be positive")
        if self.search_before_ms < 0 or self.search_after_ms < 0 or self.boundary_guard_ms < 0:
            raise ValueError("ASR window search and guard values cannot be negative")
        if self.target_window_ms + self.search_after_ms > self.hard_max_window_ms:
            raise ValueError("Search corridor extends beyond hard_max_window_ms")
        if self.minimum_silence_ms < 2 * self.boundary_guard_ms:
            raise ValueError("minimum_silence_ms must contain both boundary guards")
        if not 0.0 < self.minimum_speech_coverage_ratio <= 1.0:
            raise ValueError("minimum_speech_coverage_ratio must be in (0, 1]")
        if self.detector_parameters is not None:
            required = {
                "frame_samples", "threshold", "negative_threshold", "min_speech_duration_ms",
                "min_silence_duration_ms", "speech_pad_ms",
            }
            if set(self.detector_parameters) != required:
                raise ValueError("detector_parameters fields do not match the contract")
            if self.detector_parameters["frame_samples"] != 512:
                raise ValueError("Silero 16 kHz requires frame_samples=512")


def _normalize_intervals(intervals: Iterable[tuple[int, int]], duration_ms: int) -> list[tuple[int, int]]:
    normalized: list[tuple[int, int]] = []
    for start, end in intervals:
        start_i, end_i = int(round(start)), int(round(end))
        if start_i < 0 or end_i > duration_ms or end_i <= start_i:
            raise ValueError(f"Invalid interval [{start_i}, {end_i}] for duration {duration_ms}")
        if normalized and start_i < normalized[-1][0]:
            raise ValueError("Intervals must be ordered")
        if normalized and start_i <= normalized[-1][1]:
            normalized[-1] = (normalized[-1][0], max(normalized[-1][1], end_i))
        else:
            normalized.append((start_i, end_i))
    return normalized


def silence_gaps(duration_ms: int, speech_segments_ms: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    speech = _normalize_intervals(speech_segments_ms, duration_ms)
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in speech:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_ms:
        gaps.append((cursor, duration_ms))
    return gaps


def _window_speech(speech: list[tuple[int, int]], start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    clipped: list[tuple[int, int]] = []
    for speech_start, speech_end in speech:
        start = max(start_ms, speech_start)
        end = min(end_ms, speech_end)
        if end > start:
            clipped.append((start - start_ms, end - start_ms))
    return clipped


def plan_vad_windows(duration_ms: int, speech_segments_ms: Iterable[tuple[int, int]],
                     policy: WindowPolicy | None = None) -> list[dict[str, Any]]:
    """Build an exact non-overlapping partition cut only inside VAD silence."""
    policy = policy or WindowPolicy.load()
    policy.validate()
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    speech = _normalize_intervals(speech_segments_ms, duration_ms)
    gaps = silence_gaps(duration_ms, speech)
    windows: list[dict[str, Any]] = []
    start = 0
    sequence = 1
    while duration_ms - start > policy.hard_max_window_ms:
        target = start + policy.target_window_ms
        corridor_start = target - policy.search_before_ms
        corridor_end = min(target + policy.search_after_ms, start + policy.hard_max_window_ms)
        candidates: list[tuple[int, int, int, int]] = []
        fallback_candidates: list[tuple[int, int, int, int]] = []
        for gap_start, gap_end in gaps:
            if gap_end - gap_start < policy.minimum_silence_ms:
                continue
            allowed_start = gap_start + policy.boundary_guard_ms
            allowed_end = gap_end - policy.boundary_guard_ms
            fallback_low = max(allowed_start, start + 1)
            fallback_high = min(allowed_end, start + policy.hard_max_window_ms)
            if fallback_low <= fallback_high:
                fallback_cut = min(max(target, fallback_low), fallback_high)
                fallback_candidates.append((abs(fallback_cut - target), fallback_cut, gap_start, gap_end))
            low = max(allowed_start, corridor_start, start + 1)
            high = min(allowed_end, corridor_end, start + policy.hard_max_window_ms)
            if low > high:
                continue
            cut = min(max(target, low), high)
            candidates.append((abs(cut - target), cut, gap_start, gap_end))
        if not candidates:
            # The search corridor is a quality preference, not a coverage
            # invariant. Long uninterrupted programme sections can have a
            # valid guarded silence before or after it. Use the nearest valid
            # cut within hard_max_window_ms rather than rejecting the episode.
            candidates = fallback_candidates
        quality = "accepted"
        if not candidates:
            # Last resort, never a lost episode: music under the speech, or talk without a pause, leaves no
            # guarded silence. Cut in the longest pause there is (the one nearest the target among equals),
            # else at the target itself. ASR may lose a word at that one cut.
            hard_end = start + policy.hard_max_window_ms
            earliest = start + policy.target_window_ms // 2           # never a sliver of a window
            short = [(gap_start, gap_end) for gap_start, gap_end in gaps
                     if earliest <= (gap_start + gap_end) // 2 <= hard_end and gap_end > gap_start]
            if short:
                gap_start, gap_end = max(short, key=lambda g: (g[1] - g[0], -abs((g[0] + g[1]) // 2 - target)))
                cut = (gap_start + gap_end) // 2
                candidates, quality = [(abs(cut - target), cut, gap_start, gap_end)], "short_silence"
            else:
                candidates, quality = [(0, target, target, target)], "forced"
        _, end, gap_start, gap_end = min(candidates, key=lambda item: (item[0], item[1]))
        relative_speech = _window_speech(speech, start, end)
        windows.append({
            "sequence": sequence,
            "start_ms": start,
            "end_ms": end,
            "duration_ms": end - start,
            "speech_segments_ms": relative_speech,
            "end_boundary": {
                "gap_start_ms": gap_start,
                "gap_end_ms": gap_end,
                "gap_duration_ms": gap_end - gap_start,
                "cut_ms": end,
                "distance_from_target_ms": end - target,
                "left_guard_ms": end - gap_start,
                "right_guard_ms": gap_end - end,
                "quality": quality,
            },
        })
        start = end
        sequence += 1
    relative_speech = _window_speech(speech, start, duration_ms)
    windows.append({
        "sequence": sequence,
        "start_ms": start,
        "end_ms": duration_ms,
        "duration_ms": duration_ms - start,
        "speech_segments_ms": relative_speech,
        "end_boundary": None,
    })
    verify_window_partition(windows, duration_ms, policy)
    return windows


def verify_window_partition(windows: list[dict[str, Any]], duration_ms: int,
                            policy: WindowPolicy | None = None) -> None:
    policy = policy or WindowPolicy.load()
    if not windows or windows[0]["start_ms"] != 0 or windows[-1]["end_ms"] != duration_ms:
        raise ASRWindowError("Windows do not cover the complete source duration")
    cursor = 0
    for sequence, window in enumerate(windows, 1):
        if window["sequence"] != sequence:
            raise ASRWindowError("Window sequence is not contiguous")
        if window["start_ms"] != cursor or window["end_ms"] <= window["start_ms"]:
            raise ASRWindowError("Window partition contains an overlap, gap or empty window")
        if window["duration_ms"] != window["end_ms"] - window["start_ms"]:
            raise ASRWindowError("Window duration metadata is inconsistent")
        if window["duration_ms"] > policy.hard_max_window_ms:
            raise ASRWindowError("Window exceeds hard_max_window_ms")
        boundary = window.get("end_boundary")
        if boundary is not None and boundary["quality"] in ("short_silence", "forced"):
            pass                                          # the planner's last resort: no guarded silence existed
        elif boundary is not None:
            if boundary["quality"] != "accepted":
                raise ASRWindowError("Interior boundary is not accepted")
            if boundary["left_guard_ms"] < policy.boundary_guard_ms or boundary["right_guard_ms"] < policy.boundary_guard_ms:
                raise ASRWindowError("Interior boundary does not satisfy VAD guards")
            if boundary["gap_duration_ms"] < policy.minimum_silence_ms:
                raise ASRWindowError("Interior boundary silence is too short")
        cursor = window["end_ms"]


def materialize_windows(source_audio: str | Path, windows: list[dict[str, Any]],
                        output_dir: str | Path) -> list[dict[str, Any]]:
    """Extract deterministic PCM WAV windows and verify duration with ffprobe."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ASRWindowError("ffmpeg is required to materialize ASR windows")
    source = Path(source_audio)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    source_sha256 = sha256_file(source)
    manifests: list[dict[str, Any]] = []
    for window in windows:
        name = f"window-{window['sequence']:04d}.wav"
        target = output / name
        temporary = output / f".{name}.part.wav"
        duration_seconds = window["duration_ms"] / 1000.0
        if not target.is_file():
            command = [
                ffmpeg, "-nostdin", "-y", "-i", str(source),
                "-ss", f"{window['start_ms'] / 1000.0:.3f}", "-t", f"{duration_seconds:.3f}",
                "-map_metadata", "-1", "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(temporary),
            ]
            completed = subprocess.run(command, capture_output=True, timeout=max(60, int(duration_seconds * 2)))
            if completed.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
                temporary.unlink(missing_ok=True)
                detail = completed.stderr.decode("utf-8", errors="replace")[-1000:]
                raise ASRWindowError(f"ffmpeg window extraction failed: {detail}")
            os.replace(temporary, target)
        probe = probe_audio(target)
        actual_ms = int(round(probe["duration_seconds"] * 1000))
        if abs(actual_ms - window["duration_ms"]) > 50:
            raise ASRWindowError(
                f"ffprobe duration mismatch for {name}: expected={window['duration_ms']} actual={actual_ms}"
            )
        manifests.append({
            **window,
            "source_sha256": source_sha256,
            "path": str(target),
            "window_sha256": sha256_file(target),
            "ffprobe_duration_ms": actual_ms,
        })
    return manifests


def _intersection_ms(left: tuple[int, int], right: tuple[int, int]) -> int:
    return max(0, min(left[1], right[1]) - max(left[0], right[0]))


def _maximum_uncovered_run(speech: list[tuple[int, int]], asr: list[tuple[int, int]]) -> int:
    maximum = 0
    for speech_start, speech_end in speech:
        cursor = speech_start
        for asr_start, asr_end in asr:
            if asr_end <= cursor or asr_start >= speech_end:
                continue
            maximum = max(maximum, max(0, min(asr_start, speech_end) - cursor))
            cursor = max(cursor, min(asr_end, speech_end))
            if cursor >= speech_end:
                break
        maximum = max(maximum, speech_end - cursor)
    return maximum


def validate_asr_window(raw_response: dict[str, Any], window: dict[str, Any], *,
                        expected_model: str, policy: WindowPolicy | None = None) -> dict[str, Any]:
    """Validate ASR timestamps against VAD-positive speech, never raw duration."""
    policy = policy or WindowPolicy.load()
    segments_raw = raw_response.get("segments") or []
    asr_segments: list[tuple[int, int]] = []
    texts: list[str] = []
    for segment in segments_raw:
        try:
            start_ms = int(round(float(segment["start"]) * 1000))
            end_ms = int(round(float(segment["end"]) * 1000))
        except (KeyError, TypeError, ValueError) as error:
            raise TemporalCoverageError("ASR segment has invalid timestamps") from error
        if start_ms < 0 or end_ms <= start_ms or end_ms > window["duration_ms"] + policy.timestamp_tolerance_ms:
            raise TemporalCoverageError("ASR segment lies outside the window")
        asr_segments.append((max(0, start_ms), min(window["duration_ms"], end_ms)))
        if str(segment.get("text") or "").strip():
            texts.append(str(segment["text"]).strip())
    text = str(raw_response.get("text") or "").strip() or " ".join(texts).strip()
    if not text:
        raise TemporalCoverageError("ASR window contains no text")
    # oMLX 0.6.4 does not echo the requested model in verbose_json ASR
    # responses. The caller establishes model identity by holding the shared
    # model-arbiter lock, switching to exactly expected_model, reading that
    # state back, and sending expected_model in the authenticated request.
    # If a provider does echo a model, it remains useful contradictory
    # evidence and must match. Absence alone is not a coverage failure.
    provider_model = raw_response.get("model")
    if provider_model is not None and provider_model != expected_model:
        raise TemporalCoverageError(
            f"ASR model mismatch: expected {expected_model}, received {provider_model}"
        )
    model = expected_model
    speech = _normalize_intervals(window.get("speech_segments_ms") or [], window["duration_ms"])
    if not speech:
        raise TemporalCoverageError("VAD window contains no speech to validate")
    asr = _normalize_intervals(sorted(asr_segments), window["duration_ms"])
    speech_total = sum(end - start for start, end in speech)
    covered = sum(_intersection_ms(speech_interval, asr_interval) for speech_interval in speech for asr_interval in asr)
    covered = min(covered, speech_total)
    ratio = covered / speech_total
    maximum_uncovered = _maximum_uncovered_run(speech, asr)
    if ratio < policy.minimum_speech_coverage_ratio:
        raise TemporalCoverageError(
            f"coverage_incomplete: speech_coverage_ratio={ratio:.6f} minimum={policy.minimum_speech_coverage_ratio:.6f}"
        )
    if maximum_uncovered > policy.maximum_uncovered_speech_run_ms:
        raise TemporalCoverageError(
            f"coverage_incomplete: maximum_uncovered_speech_run_ms={maximum_uncovered} "
            f"maximum={policy.maximum_uncovered_speech_run_ms}"
        )
    return {
        "accepted": True,
        "model": model,
        "provider_model_evidence": provider_model,
        "text_chars": len(text),
        "segment_count": len(asr),
        "speech_coverage_ratio": ratio,
        "maximum_uncovered_speech_run_ms": maximum_uncovered,
        "provider_duration_evidence": raw_response.get("duration"),
    }
