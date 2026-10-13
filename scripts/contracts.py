#!/usr/bin/env python3
"""Current JSON contract loading and validation."""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "references" / "schemas"
SCHEMAS = {
    "ordered_transcript": "ordered-transcript-v1.schema.json",
}


def load_schema(name: str) -> dict:
    try:
        filename = SCHEMAS[name]
    except KeyError as error:
        raise KeyError(f"Unknown contract: {name}") from error
    return json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))


def validate(name: str, value: dict) -> None:
    validator = Draft202012Validator(load_schema(name), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(
            f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}"
            for error in errors
        )
        raise ValueError(f"{name} contract validation failed: {details}")
