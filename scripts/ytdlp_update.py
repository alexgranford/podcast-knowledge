#!/usr/bin/env python3
"""ytdlp_update — keep yt-dlp current: YouTube changes break older versions every few weeks.

    ytdlp_update.py            upgrade yt-dlp in its venv, test it, and roll back if the test fails

The venv is the one PODNOTE_YTDLP's program lives in (<venv>/bin/yt-dlp). The test resolves one fixed
public video and downloads its audio (19 seconds, a few hundred KB) the way an episode is downloaded.
If a new version fails it, the version that was installed before is put back and tested again. Silent
when all is well; one line otherwise, which the weekly Hermes cron job sends to chat. A passing test
says YouTube works today, no more: a later YouTube change can still break it before the next run.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402,F401  (importing it loads the project .env)
import sources  # noqa: E402

TEST_VIDEO = "jNQXAC9IVRw"             # "Me at the zoo", YouTube's first video: public since 2005, 19 seconds


def pip(program: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(program.parent / "python3"), "-m", "pip", "--disable-pip-version-check", *arguments],
                          capture_output=True, text=True, errors="replace", timeout=900)


def version(program: Path) -> str:
    return subprocess.run([str(program), "--version"], capture_output=True, text=True, timeout=60).stdout.strip()


def smoke_test() -> str:
    """"" when a resolve and an audio download of the test video work, else why not."""
    youtube = sources.youtube
    youtube.forget_resolved()
    try:
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            youtube.fetch_audio(sources.Ref(youtube.NAME, TEST_VIDEO, url=youtube.watch_url(TEST_VIDEO)), {},
                                work / "record.json", work / "audio", lambda *_: None, work)
            if (work / "audio").stat().st_size < 10_000:
                return "the test download was too small to be its audio"
    except (sources.SourceError, OSError, RuntimeError) as error:
        return str(error)
    return ""


def main() -> int:
    program = Path(os.environ.get("PODNOTE_YTDLP", "").strip() or "/nonexistent")
    if not program.is_file():
        print("yt-dlp update: PODNOTE_YTDLP does not name a yt-dlp program, so YouTube links are off.")
        return 1
    before = version(program)
    done = pip(program, "install", "--quiet", "--upgrade", "yt-dlp[default]")
    if done.returncode:
        print("yt-dlp update failed: " + ((done.stderr or done.stdout).strip().splitlines() or ["pip gave no message"])[-1][:200])
        return 1
    after = version(program)
    problem = smoke_test()
    if not problem:
        return 0
    if after == before or not before:
        print(f"yt-dlp {after} fails the YouTube test ({problem}); no newer release yet. YouTube episodes "
              "will fail until one fixes it.")
        return 1
    back = pip(program, "install", "--quiet", f"yt-dlp[default]=={before}")
    restored = version(program)
    if back.returncode or restored != before:
        print(f"yt-dlp {after} failed the YouTube test ({problem}); putting {before} back failed too (now "
              f"{restored or 'unknown'}). YouTube episodes will fail until yt-dlp is fixed by hand.")
        return 1
    again = smoke_test()
    print(f"yt-dlp {after} failed the YouTube test ({problem}); put {before} back" +
          (", which passes it." if not again else f", which fails it too ({again})."))
    return 1


if __name__ == "__main__":
    sys.exit(main())
