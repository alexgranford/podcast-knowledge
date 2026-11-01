#!/usr/bin/env python3
"""Verify that the cron runtime uses the tested monitor from this checkout.

Run after deploying a monitor change; merging a PR does not update a local skill.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

MONITOR_FILES = ("scripts/podmonitor.py", "tests/test_podmonitor.py", "scripts/podfetch.py")


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
        if not wrapper.is_file() or str(installed / "scripts/podmonitor.py") not in wrapper.read_text():
            errors.append(f"cron wrapper does not point to the installed monitor: {wrapper}")
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
