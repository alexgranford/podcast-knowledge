#!/usr/bin/env python3
"""Deterministic unit and fixture tests for VAD-windowed ASR."""
from __future__ import annotations

import json
import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from podcast_asr import (  # noqa: E402
    ASRWindowError,
    TemporalCoverageError,
    VADBoundaryUnavailable,
    WindowPolicy,
    materialize_windows,
    plan_vad_windows,
    silence_gaps,
    validate_asr_window,
    verify_window_partition,
)


def fixture_policy(**changes) -> WindowPolicy:
    values = dict(
        target_window_ms=1000,
        search_before_ms=200,
        search_after_ms=300,
        minimum_silence_ms=400,
        boundary_guard_ms=100,
        hard_max_window_ms=1400,
        timestamp_tolerance_ms=50,
        minimum_speech_coverage_ratio=0.90,
        maximum_uncovered_speech_run_ms=200,
    )
    values.update(changes)
    return WindowPolicy(**values)


class PolicyTests(unittest.TestCase):
    def test_policy_matches_schema(self) -> None:
        schema = json.loads((ROOT / "references/schemas/asr-window-policy-v1.schema.json").read_text())
        policy = json.loads((ROOT / "references/current/asr-window-policy-v1.json").read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(policy)
        WindowPolicy.load()



class WindowPlannerTests(unittest.TestCase):

    def test_windows_cut_at_guarded_silence_and_exactly_partition_source(self) -> None:
        policy = fixture_policy()
        speech = [(0, 1050), (1500, 2300), (2700, 3200)]
        windows = plan_vad_windows(3200, speech, policy)
        self.assertEqual([(0, 1150), (1150, 2400), (2400, 3200)], [
            (window["start_ms"], window["end_ms"]) for window in windows
        ])
        self.assertTrue(all(window["end_boundary"] is None or window["end_boundary"]["quality"] == "accepted" for window in windows))
        verify_window_partition(windows, 3200, policy)


    def test_valid_silence_outside_preferred_corridor_is_used(self) -> None:
        policy = fixture_policy()
        speech = [(0, 300), (700, 1600), (2000, 3000)]
        windows = plan_vad_windows(3000, speech, policy)
        self.assertEqual([(0, 600), (600, 1700), (1700, 3000)], [
            (window["start_ms"], window["end_ms"]) for window in windows
        ])
        verify_window_partition(windows, 3000, policy)

    def test_no_qualifying_silence_is_cut_at_the_target_rather_than_lost(self) -> None:
        windows = plan_vad_windows(3000, [(0, 3000)], fixture_policy())
        self.assertEqual(3000, windows[-1]["end_ms"])
        self.assertTrue(all(w["end_boundary"] is None or w["end_boundary"]["quality"] == "forced" for w in windows))




class MaterializationTests(unittest.TestCase):
    def test_pcm_windows_are_extracted_and_ffprobe_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.wav"
            rate = 16000
            with wave.open(str(source), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(rate)
                samples = bytearray()
                for index in range(rate * 2):
                    value = int(4000 * math.sin(2 * math.pi * 440 * index / rate))
                    samples.extend(struct.pack("<h", value))
                output.writeframes(bytes(samples))
            windows = [
                {"sequence": 1, "start_ms": 0, "end_ms": 1000, "duration_ms": 1000,
                 "speech_segments_ms": [(0, 1000)], "end_boundary": None},
                {"sequence": 2, "start_ms": 1000, "end_ms": 2000, "duration_ms": 1000,
                 "speech_segments_ms": [(0, 1000)], "end_boundary": None},
            ]
            manifests = materialize_windows(source, windows, root / "windows")
            self.assertEqual(2, len(manifests))
            self.assertTrue(all(Path(item["path"]).is_file() for item in manifests))
            self.assertTrue(all(len(item["window_sha256"]) == 64 for item in manifests))
            self.assertEqual([1000, 1000], [item["ffprobe_duration_ms"] for item in manifests])


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = fixture_policy(
            target_window_ms=10000, search_before_ms=0, search_after_ms=0,
            hard_max_window_ms=10000, minimum_silence_ms=400,
            minimum_speech_coverage_ratio=0.98, maximum_uncovered_speech_run_ms=300,
        )
        self.window = {
            "sequence": 1, "start_ms": 0, "end_ms": 10000, "duration_ms": 10000,
            "speech_segments_ms": [(0, 9500)], "end_boundary": None,
        }

    def test_vad_speech_coverage_passes_and_raw_duration_is_only_evidence(self) -> None:
        raw = {
            "model": "Qwen3-ASR-1.7B-8bit", "duration": 2.0, "text": "complete",
            "segments": [{"start": 0.0, "end": 9.5, "text": "complete"}],
        }
        result = validate_asr_window(raw, self.window, expected_model="Qwen3-ASR-1.7B-8bit", policy=self.policy)
        self.assertTrue(result["accepted"])
        self.assertEqual(2.0, result["provider_duration_evidence"])
        self.assertEqual(1.0, result["speech_coverage_ratio"])






if __name__ == "__main__":
    unittest.main(verbosity=2)


class LastResortCutTests(unittest.TestCase):

    def test_talk_without_a_usable_pause_is_cut_anyway_and_the_plan_holds(self):
        from podcast_asr import plan_vad_windows, verify_window_partition, WindowPolicy
        policy = WindowPolicy.load()
        minute = 60 * 1000
        duration = 40 * minute
        speech = [(0, 27 * minute - 200), (27 * minute, duration)]          # one 200 ms pause in 40 minutes
        windows = plan_vad_windows(duration, speech, policy)
        verify_window_partition(windows, duration, policy)
        self.assertEqual(["forced", "short_silence"], [w["end_boundary"]["quality"] for w in windows if w["end_boundary"]])
        self.assertEqual(15 * minute, windows[0]["end_ms"])                 # no pause before 18 min: the target
        self.assertEqual(27 * minute - 100, windows[1]["end_ms"])           # then inside the short pause
        self.assertEqual(duration, windows[-1]["end_ms"])
