#!/usr/bin/env python3
"""Authenticated oMLX model status and exclusive load/unload, used by the model arbiter."""

from __future__ import annotations

import os
import urllib.parse

import requests

import omlx_client

TEXT_MODEL = os.environ.get("PODNOTE_MODEL", "gemma-4-26B-A4B-it-qat-4bit")
ASR_MODEL = "Qwen3-ASR-1.7B-8bit"


def settings(base_url: str | None = None, model: str = TEXT_MODEL) -> dict:
    return omlx_client.config(base_url, model)


def model_status(config: dict, timeout: int = 15) -> list[dict]:
    response = requests.get(config["base_url"] + "/v1/models/status", headers=omlx_client.headers(config), timeout=timeout)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("models"), list):
        raise omlx_client.OMLXError("Invalid oMLX model-status response")
    return body["models"]


def loaded_models(config: dict) -> list[str]:
    return [str(item["id"]) for item in model_status(config) if item.get("loaded")]


def transition(config: dict, model: str, load: bool, timeout: int = 1200) -> dict:
    action = "load" if load else "unload"
    url = config["base_url"] + "/v1/models/" + urllib.parse.quote(model, safe="") + f"/{action}"
    response = requests.post(url, headers=omlx_client.headers(config), timeout=timeout)
    if response.status_code not in {200, 204}:
        raise omlx_client.OMLXError(f"oMLX model {action} failed ({response.status_code}): {response.text[:500]}")
    status = model_status(config)
    selected = next((item for item in status if item.get("id") == model), None)
    if selected is None or bool(selected.get("loaded")) is not load:
        raise omlx_client.OMLXError(f"oMLX model {action} read-back failed for {model}")
    return selected
