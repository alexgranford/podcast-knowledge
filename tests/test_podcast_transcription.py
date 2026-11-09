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
    WindowRequestError,
    atomic_json,
    hint_failure,
    loop_failure,
    merge_pieces,
    split_point,
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


class NameHintTests(TranscriptionFixture):
    HINT = "Rowan Mae Ellis Arthur Conan Doyle"            # the shape podnote.asr_names gives: names only
    SPEECH = "I'm Rowan Mae Ellis, and today Arthur Conan Doyle joins me to talk about the moors."

    def hinted_runner(self, answer_with_hint: str, asr_prompt=None, language_name: str = "") -> TranscriptionRunner:
        self.prompts, self.calls = [], []

        def asr(window_audio, raw_output, model, base_url, prompt=None, language=None):
            self.prompts.append(prompt)
            self.calls.append((Path(window_audio).read_bytes(), language))
            duration = probe_audio(window_audio)["duration_seconds"]
            text = answer_with_hint if prompt else "Rowan talks to Arthur Conan Doyle about the moors."
            body = {"model": model, "language": "en", "text": text,
                    "segments": [{"start": 0.0, "end": duration, "text": text}], "finish_reason": "stop"}
            atomic_json(raw_output, body)
            return body

        return TranscriptionRunner(self.store, self.job_id, self.audio, self.root / "job",
                                   vad_model_path=DEFAULT_VAD_MODEL, window_policy_path=DEFAULT_WINDOW_POLICY,
                                   vad_runner=self.fake_vad, request_runner=asr,
                                   asr_prompt=[self.HINT] if asr_prompt is None else asr_prompt,
                                   language="en" if language_name else "", language_name=language_name)

    def accepted(self) -> dict:
        path = next((self.root / "job" / "asr" / "accepted").glob("*.json"))
        return json.loads(path.read_text(encoding="utf-8"))

    def test_an_accepted_answer_takes_one_request(self) -> None:
        result = self.hinted_runner(self.SPEECH).run_all()
        self.assertEqual(result["status"], "transcript_assembled")
        self.assertEqual([self.HINT], self.prompts)
        self.assertEqual("used", self.accepted()["asr_hint"])

    def test_names_read_out_first_are_asked_for_again_without_them_once(self) -> None:
        result = self.hinted_runner(self.HINT + " " + self.SPEECH, language_name="English").run_all()
        self.assertEqual(result["status"], "transcript_assembled")
        self.assertEqual([self.HINT, None], self.prompts)                  # no second, shorter context
        accepted = self.accepted()
        self.assertEqual("dropped: the transcript reads the hint out", accepted["asr_hint"])
        self.assertEqual("Rowan talks to Arthur Conan Doyle about the moors.", accepted["text"])
        self.assertEqual(1, len({audio for audio, _ in self.calls}))      # the same window audio
        self.assertEqual(["English", "English"], [language for _, language in self.calls])

    def test_no_names_means_one_request_without_context(self) -> None:
        self.hinted_runner(self.SPEECH, asr_prompt=[""]).run_all()
        self.assertEqual([None], self.prompts)
        self.assertEqual("none", self.accepted()["asr_hint"])

    def test_an_earlier_callers_list_of_contexts_still_works(self) -> None:
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
                                     asr_prompt=[self.HINT, "Rowan Mae Ellis"])
        self.assertEqual("transcript_assembled", runner.run_all()["status"])
        self.assertEqual([self.HINT, "Rowan Mae Ellis"], self.prompts)
        self.assertEqual("used (shorter hint)", self.accepted()["asr_hint"])   # a status old records hold


class LanguageTests(TranscriptionFixture):

    def test_the_episode_language_is_sent_with_every_window_and_recorded(self) -> None:
        sent = []

        def asr(window_audio, raw_output, model, base_url, prompt=None, language=None):
            sent.append(language)
            duration = probe_audio(window_audio)["duration_seconds"]
            body = {"model": model, "language": "greek", "text": "Πλήρης μεταγραφή του αποσπάσματος.",
                    "segments": [{"start": 0.0, "end": duration, "text": "Πλήρης μεταγραφή του αποσπάσματος."}], "finish_reason": "stop"}
            atomic_json(raw_output, body)
            return body

        runner = TranscriptionRunner(self.store, self.job_id, self.audio, self.root / "job",
                                     vad_model_path=DEFAULT_VAD_MODEL, window_policy_path=DEFAULT_WINDOW_POLICY,
                                     vad_runner=self.fake_vad, request_runner=asr, language="el", language_name="Greek")
        self.assertEqual("transcript_assembled", runner.run_all()["status"])
        self.assertEqual(["Greek"], sent)
        transcript = json.loads((self.root / "job" / "asr" / "transcript-v1.json").read_text(encoding="utf-8"))
        self.assertEqual("el", transcript["language"])          # the stated language, not the model's spelling of it


class LoopTests(TranscriptionFixture):
    """A window the speech model answers with a repetition loop is cut in two at a pause, each half asked for
    alone, and the halves made one answer; a window that loops in every piece fails, never kept."""
    LOOP = "no " * 600

    def runner_with(self, answers, asr_prompt=None) -> TranscriptionRunner:
        self.calls = []

        def asr(window_audio, raw_output, model, base_url, prompt=None, language=None):
            duration = probe_audio(window_audio)["duration_seconds"]
            self.calls.append((prompt, round(duration, 1)))
            text = answers(prompt, duration)
            body = {"model": model, "language": "en", "text": text,
                    "segments": [{"start": 0.0, "end": duration, "text": text}], "finish_reason": "stop"}
            atomic_json(raw_output, body)
            return body

        return TranscriptionRunner(self.store, self.job_id, self.audio, self.root / "job",
                                   vad_model_path=DEFAULT_VAD_MODEL, window_policy_path=DEFAULT_WINDOW_POLICY,
                                   vad_runner=self.fake_vad, request_runner=asr, asr_prompt=asr_prompt or [])

    def accepted(self) -> dict:
        return json.loads(next((self.root / "job" / "asr" / "accepted").glob("*.json")).read_text(encoding="utf-8"))

    def test_a_looping_window_is_asked_for_again_in_two_halves(self) -> None:
        pieces = iter(["First half of the talk.", "Second half of the talk."])
        runner = self.runner_with(lambda prompt, duration: self.LOOP if duration > 1.5 else next(pieces))
        self.assertEqual("transcript_assembled", runner.run_all()["status"])
        self.assertEqual([None, None, None], [prompt for prompt, _ in self.calls])
        whole, first, second = (seconds for _, seconds in self.calls)
        self.assertAlmostEqual(whole, first + second, delta=0.1)           # the halves are the window
        accepted = self.accepted()
        self.assertEqual("First half of the talk. Second half of the talk.", accepted["text"])
        self.assertEqual("none; looped: cut into 2 pieces", accepted["asr_hint"])
        self.assertTrue(accepted["coverage"]["accepted"])

    def test_names_that_loop_fall_back_without_them_and_then_a_loop_is_cut(self) -> None:
        pieces = iter(["One.", "Two."])
        runner = self.runner_with(lambda prompt, duration: self.LOOP if duration > 1.5 else next(pieces), asr_prompt=["Rowan Mae Ellis"])
        runner.run_all()
        self.assertEqual(["Rowan Mae Ellis", None, None, None], [prompt for prompt, _ in self.calls])
        self.assertEqual("dropped: the transcript loops (100% of it is repeated); looped: cut into 2 pieces", self.accepted()["asr_hint"])

    def test_a_loop_everywhere_fails_after_the_cuts(self) -> None:
        from unittest import mock
        import podcast_transcription
        runner = self.runner_with(lambda prompt, duration: self.LOOP)
        with mock.patch.object(podcast_transcription, "LOOP_CUTS", 2), mock.patch.object(podcast_transcription, "LOOP_MIN_MS", 0):
            with self.assertRaises(WindowRequestError) as caught:
                runner.run_all()
        self.assertEqual("asr_window_looped", caught.exception.failure_code)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(3, len(self.calls))                               # the window, a half, a quarter
        self.assertFalse(list((self.root / "job" / "asr" / "accepted").glob("*.json")))
        self.assertFalse(list((self.root / "job" / "asr" / "raw").glob("*.part-*")))   # no cut audio left behind

    def test_a_stretch_too_short_to_cut_again_fails_at_once(self) -> None:
        from unittest import mock
        import podcast_transcription
        runner = self.runner_with(lambda prompt, duration: self.LOOP)
        with mock.patch.object(podcast_transcription, "LOOP_MIN_MS", 5000):    # the fixture's pieces are 1 s
            with self.assertRaises(WindowRequestError):
                runner.run_all()
        self.assertEqual(2, len(self.calls))                               # the window, one half: too short to cut

    def test_a_half_that_loops_is_cut_again_until_it_reads_clean(self) -> None:
        from unittest import mock
        import podcast_transcription
        pieces = iter(["One.", "Two.", "Three.", "Four."])
        # loops above 0.6 s of audio: the window (2 s) and both halves (1 s) loop; the quarters (0.5 s) read clean
        runner = self.runner_with(lambda prompt, duration: self.LOOP if duration > 0.6 else next(pieces))
        with mock.patch.object(podcast_transcription, "LOOP_MIN_MS", 0):
            self.assertEqual("transcript_assembled", runner.run_all()["status"])
        self.assertEqual("One. Two. Three. Four.", self.accepted()["text"])
        self.assertEqual("none; looped: cut into 4 pieces", self.accepted()["asr_hint"])
        self.assertEqual(7, len(self.calls))                               # the window, two halves, four quarters

    def test_a_clean_answer_is_never_cut(self) -> None:
        self.runner_with(lambda prompt, duration: "A complete fixture transcript.").run_all()
        self.assertEqual(1, len(self.calls))
        self.assertEqual("none", self.accepted()["asr_hint"])


class PureTests(unittest.TestCase):
    """Checks that need neither ffmpeg nor the speech-detection runtime, so they run everywhere."""

    def test_hint_failure(self) -> None:
        window = {"duration_ms": 14 * 60000}
        names = NameHintTests.HINT
        speech = " ".join(f"word{i}" for i in range(2000))
        self.assertEqual("", hint_failure({"text": speech}, window, names))
        self.assertEqual("14 words for 14.0 minutes", hint_failure({"text": " ".join(["w"] * 14)}, window, names))
        self.assertEqual("14 words for 14.0 minutes", hint_failure({"text": "Arthur Doyle, Rowan Ellis. " * 3 + "w w"}, window, names))
        # the names read out word for word before the speech, perhaps after a filler word
        self.assertEqual("the transcript reads the hint out", hint_failure({"text": names + " " + speech}, window, names))
        self.assertEqual("the transcript reads the hint out", hint_failure({"text": "Um. " + names + ". " + speech}, window, names))
        self.assertEqual("the transcript reads the hint out", hint_failure({"text": (names + " ") * 400}, window, names))
        # speech naming the same people is kept: introductions, another order, the names later on
        for spoken in ("I'm Rowan Mae Ellis, and with me today is Arthur Conan Doyle. ",
                       "Arthur Conan Doyle and Rowan Mae Ellis here. ", "Welcome back. " * 5 + names + ". "):
            self.assertEqual("", hint_failure({"text": spoken + speech}, window, names), spoken)
        self.assertEqual("", hint_failure({"text": "Rowan Mae here. " + speech}, window, "Rowan Mae"))   # one name: proves nothing
        bare = "Rowan Mae Ellis Arthur Conan Doyle The Baskervilles Episode " * 100   # a long context looped
        self.assertEqual("the transcript repeats the hint", hint_failure({"text": "So. " * 4 + bare}, window, "Names " + bare))

    def test_loop_failure_tells_a_loop_from_speech(self) -> None:
        speech = " ".join(f"word{i}" for i in range(2000))
        self.assertEqual("", loop_failure({"text": speech}))
        self.assertEqual("", loop_failure({"text": ""}))
        self.assertEqual("", loop_failure({"text": "no no no"}))                       # too short to measure
        self.assertEqual("the transcript loops (100% of it is repeated)", loop_failure({"text": "no " * 600}))
        paragraph = "She told me the whole story again and then she left the room. "
        self.assertTrue(loop_failure({"text": speech + " " + paragraph * 60}).startswith("the transcript loops"))   # a loop from the middle on
        self.assertTrue(loop_failure({"segments": [{"text": "no, " * 300}]}).startswith("the transcript loops"))    # read from the segments
        # spoken repeats stay far below the bar: a stutter, a chorus sung four times, a catchphrase now and then
        self.assertEqual("", loop_failure({"text": speech + " no, no, no, no, no, no, no, no, no, no, no, no. " + speech}))
        self.assertEqual("", loop_failure({"text": speech + (" And we sing the chorus again tonight." * 4) + " " + speech}))
        self.assertEqual("", loop_failure({"text": (speech + " That is the thing about the market. ") * 3}))

    def test_a_looping_window_is_cut_at_a_pause_near_its_middle(self) -> None:
        window = {"speech_segments_ms": [[0, 4000], [4100, 9000], [9500, 10000], [10600, 20000]]}
        self.assertEqual(10300, split_point(window, 0, 20000))       # the pause nearest the middle (not 4,050, outside the middle half; not 9,250, farther)
        self.assertEqual(10000, split_point({"speech_segments_ms": [[0, 20000]]}, 0, 20000))   # no pause: the middle
        self.assertEqual(10300, split_point(window, 9000, 12000))           # a stretch of the window: pauses inside it only
        self.assertEqual(7500, split_point({"speech_segments_ms": []}, 5000, 10000))

    def test_pieces_become_one_answer_with_the_segments_where_they_were(self) -> None:
        first = {"text": "One.", "language": "en", "segments": [{"start": 0.0, "end": 4.0, "text": "One."}]}
        second = {"text": "Two.", "language": "en", "segments": [{"start": 0.0, "end": 6.0, "text": "Two."}]}
        merged = merge_pieces([(0, first), (4000, second)], 10000)
        self.assertEqual("One. Two.", merged["text"])
        self.assertEqual([(0.0, 4.0), (4.0, 10.0)], [(s["start"], s["end"]) for s in merged["segments"]])
        self.assertEqual("en", merged["language"])
        self.assertEqual("Two.", merge_pieces([(0, {"segments": [{"start": 0, "end": 1, "text": "Two."}]})], 5000)["text"])

    def test_the_request_names_the_language(self) -> None:
        from unittest import mock
        import podcast_transcription
        with mock.patch.object(podcast_transcription.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr="x")) as run:
            with tempfile.TemporaryDirectory() as folder, self.assertRaises(Exception):
                audio = Path(folder) / "window.wav"
                audio.write_bytes(b"")
                podcast_transcription.default_request_runner(audio, Path(folder) / "raw.json", "m", None, language="Greek")
        command = run.call_args.args[0]
        self.assertEqual("Greek", command[command.index("--language") + 1])


def test_the_readiness_record_must_match_every_file_it_pins(tmp_path):
    import json
    import podcast_transcription as pt
    (tmp_path / "requirements.lock").write_text("onnxruntime==1\n")
    ready = {"status": "ready", "model_sha256": pt.file_sha256(pt.DEFAULT_VAD_MODEL),
             "cli_sha256": pt.file_sha256(pt.DEFAULT_VAD_CLI), "policy_sha256": pt.file_sha256(pt.DEFAULT_WINDOW_POLICY),
             "requirements_lock_sha256": pt.file_sha256(tmp_path / "requirements.lock")}
    (tmp_path / "READY.json").write_text(json.dumps(ready))
    assert pt.runtime_problem(tmp_path) == ""
    (tmp_path / "READY.json").write_text(json.dumps(ready | {"cli_sha256": "0" * 64}))    # the VAD script changed
    assert pt.runtime_problem(tmp_path) == "VAD runtime readiness/hash validation failed"
    (tmp_path / "READY.json").unlink()
    assert pt.runtime_problem(tmp_path).startswith("VAD runtime readiness record is absent")
