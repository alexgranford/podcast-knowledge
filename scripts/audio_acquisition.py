#!/usr/bin/env python3
"""Secure, restartable podcast-audio acquisition and validation."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from network_safety import PinnedHTTPSConnection, public_endpoint

MAX_AUDIO_BYTES = 1024 * 1024 * 1024
MAX_AUDIO_REDIRECTS = 8
CHUNK_BYTES = 1024 * 1024


class AudioError(RuntimeError):
    pass


class AudioSecurityError(AudioError):
    pass


class AudioTransientError(AudioError):
    pass


class AudioCancelled(AudioError):
    pass


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, CHUNK_BYTES)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _safe_local_source(source: str | Path, allowed_roots: tuple[str | Path, ...]) -> Path:
    original = Path(source).expanduser().absolute()
    if original.is_symlink():
        raise AudioSecurityError(f"Local audio source may not be a symbolic link: {original}")
    path = original.resolve()
    if not path.is_file():
        raise AudioSecurityError(f"Local audio source is not a regular file: {path}")
    allowed = [Path(root).expanduser().resolve() for root in allowed_roots]
    if not allowed or not any(path.is_relative_to(root) for root in allowed):
        raise AudioSecurityError("Local audio source is outside the configured allowlist")
    if path.stat().st_size > MAX_AUDIO_BYTES:
        raise AudioSecurityError("Audio exceeds the maximum allowed size")
    return path


def _content_range_start(value: str | None) -> int | None:
    if not value or not value.lower().startswith("bytes ") or "-" not in value:
        return None
    try:
        return int(value.split(" ", 1)[1].split("-", 1)[0])
    except ValueError:
        return None


def download_audio(
    url: str,
    destination: str | Path,
    timeout_seconds: int = 60,
    max_elapsed_seconds: int = 3600,
    progress: Callable[[int, int | None], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Download to a .part file, resume by Range, then atomically publish."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".part")
    if part.exists() and part.stat().st_size > MAX_AUDIO_BYTES:
        raise AudioSecurityError("Partial audio exceeds the maximum allowed size")
    started = time.monotonic()
    current_url = url
    for redirect_count in range(MAX_AUDIO_REDIRECTS + 1):
        if cancelled and cancelled():
            raise AudioCancelled("Audio download cancelled")
        if time.monotonic() - started > max_elapsed_seconds:
            raise AudioTransientError("Audio download exceeded elapsed-time limit")
        parsed, approved_address = public_endpoint(current_url)
        offset = part.stat().st_size if part.exists() else 0
        headers = {
            "User-Agent": "podcast-knowledge/1.0",
            "Accept": "audio/*,application/octet-stream;q=0.8",
            "Accept-Encoding": "identity",
        }
        if offset:
            headers["Range"] = f"bytes={offset}-"
        connection = PinnedHTTPSConnection(parsed.hostname or "", approved_address, parsed.port or 443, timeout_seconds)
        path = urllib.parse.urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise AudioError(f"Audio redirect HTTP {response.status} has no Location")
                if redirect_count >= MAX_AUDIO_REDIRECTS:
                    raise AudioSecurityError(f"Audio redirect limit exceeded ({MAX_AUDIO_REDIRECTS})")
                current_url = urllib.parse.urljoin(current_url, location)
                continue
            if response.status == 416 and offset:
                part.unlink(missing_ok=True)
                return download_audio(
                    current_url,
                    destination,
                    timeout_seconds,
                    max_elapsed_seconds,
                    progress,
                    cancelled,
                )
            if response.status == 429 or 500 <= response.status < 600:
                raise AudioTransientError(f"Audio server returned HTTP {response.status}")
            if not 200 <= response.status < 300:
                raise AudioError(f"Audio server returned HTTP {response.status}")
            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type and not (content_type.startswith("audio/") or content_type == "application/octet-stream"):
                raise AudioError(f"Unexpected audio response type: {content_type}")
            if response.status == 206:
                if _content_range_start(response.getheader("Content-Range")) != offset:
                    raise AudioError("Audio server returned an invalid resume range")
                mode = "ab"
            else:
                mode = "wb"
                offset = 0
            declared = response.getheader("Content-Length")
            try:
                remaining = int(declared) if declared else None
            except ValueError as error:
                raise AudioError("Invalid audio Content-Length header") from error
            total = offset + remaining if remaining is not None else None
            if total is not None and total > MAX_AUDIO_BYTES:
                raise AudioSecurityError("Audio exceeds the maximum allowed size")
            downloaded = offset
            with part.open(mode) as stream:
                while True:
                    if cancelled and cancelled():
                        raise AudioCancelled("Audio download cancelled")
                    if time.monotonic() - started > max_elapsed_seconds:
                        raise AudioTransientError("Audio download exceeded elapsed-time limit")
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_AUDIO_BYTES:
                        raise AudioSecurityError("Audio exceeds the maximum allowed size")
                    stream.write(chunk)
                    if progress:
                        progress(downloaded, total)
                stream.flush()
                os.fsync(stream.fileno())
            if total is not None and downloaded != total:
                raise AudioTransientError(f"Audio download incomplete: expected {total} bytes, received {downloaded}")
            os.replace(part, destination)
            return {
                "path": str(destination),
                "final_url": current_url,
                "bytes": downloaded,
                "content_type": content_type or None,
                "resumed_from": offset,
            }
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise AudioTransientError(f"Audio download failed: {error}") from error
        finally:
            connection.close()
    raise AudioSecurityError(f"Audio redirect limit exceeded ({MAX_AUDIO_REDIRECTS})")


def acquire_audio(
    source: str | Path,
    destination: str | Path,
    allowed_local_roots: tuple[str | Path, ...] = (),
    **download_options,
) -> dict:
    parsed = urllib.parse.urlparse(str(source))
    if parsed.scheme:
        if parsed.scheme != "https":
            raise AudioSecurityError("Remote audio URL must use HTTPS")
        return download_audio(str(source), destination, **download_options)
    local = _safe_local_source(source, allowed_local_roots)
    destination = Path(destination)
    atomic_copy(local, destination)
    return {
        "path": str(destination),
        "final_url": None,
        "bytes": destination.stat().st_size,
        "content_type": None,
        "resumed_from": 0,
    }


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_audio(path: str | Path) -> dict:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,format_name,size,bit_rate:stream=index,codec_type,codec_name,sample_rate,channels",
        "-of", "json", str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if completed.returncode:
        raise AudioError(f"ffprobe rejected audio: {completed.stderr.strip()[:500]}")
    try:
        data = json.loads(completed.stdout)
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AudioError("ffprobe returned no valid media duration") from error
    if duration <= 0:
        raise AudioError("Audio duration must be positive")
    audio_streams = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"]
    if not audio_streams:
        raise AudioError("Media contains no audio stream")
    return {
        "duration_seconds": duration,
        "format_name": data.get("format", {}).get("format_name"),
        "size_bytes": int(data.get("format", {}).get("size") or Path(path).stat().st_size),
        "bit_rate": int(data.get("format", {}).get("bit_rate") or 0) or None,
        "streams": audio_streams,
    }


def duration_warning(expected: int | None, actual: float) -> str | None:
    if expected is None or expected <= 0:
        return None
    tolerance = max(10.0, expected * 0.02)
    difference = abs(actual - expected)
    if difference <= tolerance:
        return None
    return f"audio_duration_mismatch: expected={expected}s actual={actual:.3f}s difference={difference:.3f}s"
