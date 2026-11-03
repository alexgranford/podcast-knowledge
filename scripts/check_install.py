#!/usr/bin/env python3
"""Verify that the cron runtime uses the tested monitor from this checkout.

Run after deploying a monitor change; merging a PR does not update a local skill.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

MONITOR_FILES = ("scripts/podmonitor.py", "tests/test_podmonitor.py", "scripts/podfetch.py", "scripts/podqueue.py",
                 "scripts/podshows.py", "scripts/sources/__init__.py", "scripts/sources/apple.py",
                 "scripts/sources/youtube.py")    # the monitor and what it calls to find and queue episodes


def verify(source: Path, installed: Path, wrapper: Path | None = None) -> list[str]:
    errors = []
    for name in MONITOR_FILES:
        expected, actual = source / name, installed / name
        if not expected.is_file() or not actual.is_file():
            errors.append(f"missing {name}: source={expected.is_file()} installed={actual.is_file()}")
            continue
        if expected.read_bytes() != actual.read_bytes():
            errors.append(f"mismatch {name}: source={hashlib.sha256(expected.read_bytes()).hexdigest()[:12]} "
                          f"installed={hashlib.sha256(actual.read_bytes()).hexdigest()[:12]}")
    if wrapper is not None:
        import subprocess
        target = str(installed / "scripts" / "podmonitor.py")
        found = subprocess.run([sys.executable, str(wrapper), "--check"], capture_output=True, text=True,
                               timeout=60) if wrapper.is_file() else None
        if found is None or found.returncode or not found.stdout.startswith(target):
            errors.append(f"cron wrapper does not run the installed monitor: {wrapper}")
        elif (source / "hermes" / wrapper.name).is_file() and \
                (source / "hermes" / wrapper.name).read_bytes() != wrapper.read_bytes():
            errors.append(f"cron wrapper differs from this checkout's hermes/{wrapper.name}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", type=Path, required=True)
    parser.add_argument("--wrapper", type=Path)
    args = parser.parse_args()
    errors = verify(Path(__file__).resolve().parents[1], args.installed, args.wrapper)
    if errors:
        print("Deployment mismatch:\n" + "\n".join(errors))
        return 1
    print(f"Monitor deployment matches checkout: {args.installed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
