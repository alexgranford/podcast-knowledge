#!/usr/bin/env python3
"""Authenticated oMLX configuration and health checks."""

from __future__ import annotations

import ipaddress

import os
import socket
import urllib.parse
from pathlib import Path

import requests


DEFAULT_OMLX_BASE = "http://127.0.0.1:8000"
DEFAULT_OMLX_MODEL = "Qwen3-ASR-1.7B-8bit"


class OMLXError(RuntimeError):
    pass


def setting(name: str) -> str:
    """A setting from the environment (settings.load() fills it from a .env file, if there is one)."""
    import settings

    settings.load()
    return os.environ.get(name, "")


def config(base_url: str | None = None, model: str | None = None) -> dict:
    base = (base_url or setting("OMLX_BASE_URL") or DEFAULT_OMLX_BASE).rstrip("/")
    selected_model = model or setting("OMLX_ASR_MODEL") or DEFAULT_OMLX_MODEL
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise OMLXError("Invalid oMLX base URL")
    if parsed.scheme == "http":
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 80, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise OMLXError(f"oMLX host DNS lookup failed: {parsed.hostname}") from error
        if not addresses or any(
            not (ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_private or ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_loopback)
            for item in addresses
        ):
            raise OMLXError("Plain HTTP is allowed only for private/loopback oMLX addresses")
    key = setting("OMLX_API_KEY")
    if not key:
        raise OMLXError("OMLX_API_KEY is not configured")
    return {"base_url": base, "model": selected_model, "api_key": key}


def headers(settings: dict) -> dict:
    return {"Authorization": f"Bearer {settings['api_key']}"}


def health(settings: dict, timeout: int = 10) -> dict:
    try:
        openapi = requests.get(settings["base_url"] + "/openapi.json", timeout=timeout)
        openapi.raise_for_status()
        models = requests.get(settings["base_url"] + "/v1/models/status", headers=headers(settings), timeout=timeout)
        models.raise_for_status()
    except requests.RequestException as error:
        raise OMLXError(f"oMLX health check failed: {error}") from error
    openapi_data = openapi.json()
    model_data = models.json()
    entries = model_data.get("models", []) if isinstance(model_data, dict) else []
    selected = next((item for item in entries if item.get("id") == settings["model"]), None)
    if not selected or selected.get("model_type") != "audio_stt":
        raise OMLXError(f"Configured oMLX ASR model is unavailable: {settings['model']}")
    loaded_asr = [item.get("id") for item in entries if item.get("model_type") == "audio_stt" and item.get("loaded")]
    if len(loaded_asr) > 1:
        raise OMLXError(f"More than one ASR model is loaded: {loaded_asr}")
    if loaded_asr and loaded_asr != [settings["model"]]:
        raise OMLXError(f"A different ASR model is loaded: {loaded_asr[0]}")
    return {
        "status": "ok",
        "api_version": openapi_data.get("info", {}).get("version"),
        "model": settings["model"],
        "model_loaded": bool(selected.get("loaded")),
        "loaded_asr_models": loaded_asr,
        "model_type": selected.get("model_type"),
        "model_path": selected.get("model_path"),
        "model_actual_size": selected.get("actual_size"),
    }



