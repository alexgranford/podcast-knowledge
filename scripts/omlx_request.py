#!/usr/bin/env python3
"""One blocking oMLX request, isolated so the wrapper can cancel it."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import requests

import omlx_client


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--error-output", required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--prompt", help="context for the ASR model (names, topic); Qwen3-ASR biases spellings with it")
    args = parser.parse_args()
    settings = omlx_client.config(args.base_url, args.model)
    audio = Path(args.audio)
    try:
        with audio.open("rb") as stream:
            response = requests.post(
                settings["base_url"] + "/v1/audio/transcriptions",
                headers=omlx_client.headers(settings),
                files={"file": (audio.name, stream, "application/octet-stream")},
                data={"model": settings["model"], "response_format": "verbose_json",
                      **({"prompt": args.prompt} if args.prompt else {})},
                timeout=(15, 21600),
            )
        try:
            body = response.json()
        except ValueError:
            body = {"raw_text": response.text[:2000]}
        if response.status_code != 200:
            atomic_json(Path(args.error_output), {"http_status": response.status_code, "response": body})
            return 2
        if not isinstance(body, dict):
            atomic_json(Path(args.error_output), {"http_status": 200, "response": body, "error": "non-object response"})
            return 3
        atomic_json(Path(args.output), body)
        return 0
    except requests.RequestException as error:
        atomic_json(Path(args.error_output), {"http_status": None, "error": type(error).__name__, "message": str(error)})
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
