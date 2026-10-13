#!/usr/bin/env python3
"""Durable single-host arbiter for podcast ASR and local text models."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import omlx_text

DEFAULT_STATE = Path(os.environ.get("PODNOTE_CACHE", "/var/tmp/podnote")) / "state"
ACTIVE_ASR_STATES = {"queued", "submitting", "running", "result_ready"}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise


def asr_is_active(state_root: str | Path = DEFAULT_STATE) -> bool:
    jobs = Path(state_root) / "asr-jobs"
    for path in jobs.glob("*.json") if jobs.is_dir() else []:
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("status") in ACTIVE_ASR_STATES:
                return True
        except (OSError, ValueError):
            return True
    return False


def lease_path(state_root: str | Path = DEFAULT_STATE) -> Path:
    return Path(state_root) / "omlx-model-lease.json"


def current_lease(state_root: str | Path = DEFAULT_STATE) -> dict | None:
    path = lease_path(state_root)
    if not path.is_file(): return None
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError): return {"invalid": True}


def _switch_exact(config: dict, requested: str) -> list[str]:
    loaded = omlx_text.loaded_models(config)
    for model in loaded:
        if model != requested:
            omlx_text.transition(config, model, False)
    if requested not in omlx_text.loaded_models(config):
        omlx_text.transition(config, requested, True)
    loaded = omlx_text.loaded_models(config)
    if loaded != [requested]:
        raise RuntimeError(f"Exclusive oMLX model read-back failed: {loaded}")
    return loaded


def _unload_all(config: dict) -> list[str]:
    for model in omlx_text.loaded_models(config):
        omlx_text.transition(config, model, False)
    loaded = omlx_text.loaded_models(config)
    if loaded:
        raise RuntimeError(f"oMLX model cleanup read-back failed: {loaded}")
    return loaded


def reconcile(state_root: str | Path = DEFAULT_STATE, base_url: str | None = None) -> dict:
    root = Path(state_root); root.mkdir(parents=True, exist_ok=True)
    lock = root / "omlx-model.lock"
    with lock.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        config = omlx_text.settings(base_url)
        lease = current_lease(root)
        loaded = omlx_text.loaded_models(config)
        # A live local session holds this same file lock. If reconcile acquires
        # it while a lease or loaded model remains, the prior owner exited
        # before cleanup completed. Return the host to an unloaded state.
        needs_cleanup = bool(lease) or bool(loaded)
        if needs_cleanup:
            _unload_all(config)
            lease_path(root).unlink(missing_ok=True)
        return {"loaded_models": omlx_text.loaded_models(config), "lease": current_lease(root),
                "cleaned": needs_cleanup}


@contextmanager
def asr_model_session(
    job_id: str,
    owner: str,
    model: str,
    state_root: str | Path = DEFAULT_STATE,
    base_url: str | None = None,
    lease_seconds: int = 21600,
    operation: str = "asr_window",
):
    """Hold the shared ASR slot and model lock for one ASR request."""
    root = Path(state_root); root.mkdir(parents=True, exist_ok=True)
    slot = root / "locks" / "asr-slot.lock"; slot.parent.mkdir(parents=True, exist_ok=True)
    model_lock = root / "omlx-model.lock"
    with slot.open("a+") as slot_stream:
        try:
            fcntl.flock(slot_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Shared ASR slot is already active") from error
        with model_lock.open("a+") as model_stream:
            fcntl.flock(model_stream, fcntl.LOCK_EX)
            config = omlx_text.settings(base_url, model)
            lease = {
                "schema_version": 1,
                "owner": owner,
                "job_id": job_id,
                "operation": operation,
                "requested_model": model,
                "created_at": now(),
                "expires_epoch": time.time() + lease_seconds,
                "session_model": model,
            }
            atomic_json(lease_path(root), lease)
            try:
                _switch_exact(config, model)
                yield config
            finally:
                # Keep ASR resident only between sequential ASR windows. A
                # subsequent text-model session replaces it and exits unloaded.
                loaded = omlx_text.loaded_models(config)
                if loaded != [model]:
                    _switch_exact(config, model)
                lease_path(root).unlink(missing_ok=True)


@contextmanager
def local_model_session(
    job_id: str,
    owner: str,
    state_root: str | Path = DEFAULT_STATE,
    base_url: str | None = None,
    lease_seconds: int = 1800,
    model: str = omlx_text.TEXT_MODEL,
    operation: str = "local_preparation",
):
    root = Path(state_root); root.mkdir(parents=True, exist_ok=True)
    lock = root / "omlx-model.lock"
    with lock.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if asr_is_active(root):
            raise RuntimeError("ASR has priority; local preparation deferred")
        config = omlx_text.settings(base_url, model)
        lease = {"schema_version": 1, "owner": owner, "job_id": job_id, "operation": operation,
                 "requested_model": model, "created_at": now(), "expires_epoch": time.time() + lease_seconds,
                 "final_model_state": "unloaded"}
        atomic_json(lease_path(root), lease)
        try:
            _switch_exact(config, model)
            yield config
        finally:
            try:
                _unload_all(config)
                lease_path(root).unlink(missing_ok=True)
            except Exception as error:
                lease["cleanup_error"] = f"{type(error).__name__}: {error}"[:1000]
                lease["expires_epoch"] = 0
                atomic_json(lease_path(root), lease)
                raise
