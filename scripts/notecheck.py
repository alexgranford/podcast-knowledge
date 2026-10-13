"""notecheck — the skill's basic check on a note before it is written.

Used when the notes folder has no metadata standard of its own (PODNOTE_ROOT/_meta/tools/
metadata_contract.py); a folder that has one is checked by it instead, through the same two functions:

    parse_note(text)                                    -> (properties, body, quoting styles)
    validate_note(text, models, tags, profile=None)     -> [{"code": ..., "field": ...}, ...]

`models` and `tags` are the folder's model catalogue and tag list (empty: any); `profile` names the fields
an episode note must carry. An empty list means the note may be written.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import yaml

DATE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\Z")       # UTC, as the skill writes it
TAG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")

# The fields every episode note carries (a folder's own podcast profile replaces this)
EPISODE_PROFILE = {"fields": {
    "show": {"type": "text", "required": True},
    "episode": {"type": "text", "required": True},
    "source_url": {"type": "url", "required": True},
    "apple_episode_id": {"type": "identifier", "pattern": "[0-9]+"},
    "hosts": {"type": "text_list"},
    "guests": {"type": "text_list"},
    "publisher": {"type": "text"},
    "duration": {"type": "integer", "minimum": 1},
}}


class Loader(yaml.SafeLoader):
    """YAML that refuses a property given twice."""


def _mapping(loader, node, deep=False):
    keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
    if len(keys) != len(set(map(str, keys))):
        raise yaml.constructor.ConstructorError(None, None, "duplicate property", node.start_mark)
    return {k: loader.construct_object(v, deep=deep) for k, (_, v) in zip(keys, node.value)}


Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_note(text: str):
    """(properties, body, quoting style of each property); no front matter gives ({}, text, {}).
    ValueError if the front matter is not a readable mapping."""
    text = text.replace("\r\n", "\n")
    found = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)", text, re.S)
    if not found:
        if text.startswith("---\n"):
            raise ValueError("unclosed front matter")
        return {}, text, {}
    try:
        node = yaml.compose(found.group(1), Loader=Loader)
        data = yaml.load(found.group(1), Loader=Loader) or {}
    except yaml.YAMLError:
        raise ValueError("unreadable front matter") from None
    if not isinstance(data, dict):
        raise ValueError("front matter is not a list of properties")
    styles = {k.value: getattr(v, "style", None) for k, v in node.value} if isinstance(node, yaml.MappingNode) else {}
    return data, text[found.end():], styles


def _instant(value):
    if not isinstance(value, str) or not DATE.fullmatch(value):
        raise ValueError
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _fits(value, spec) -> bool:
    kind = spec.get("type")
    if kind == "text":
        return isinstance(value, str) and bool(value.strip())
    if kind == "text_list":
        return (isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)
                and len(value) == len(set(value)))
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool) and value >= spec.get("minimum", value)
    if kind == "url":
        return isinstance(value, str) and urlparse(value).scheme in ("http", "https") and bool(urlparse(value).netloc)
    if kind == "identifier":
        return isinstance(value, str) and bool(re.fullmatch(spec.get("pattern", r"\S+"), value))
    return True                                         # a type this check does not know is not held against the note


def validate_note(text, models, tags, *, now=None, profile=None, **_):
    """What is wrong with the note's properties; [] when nothing is."""
    issue = lambda code, field=None: {"code": code, **({"field": field} if field else {})}
    try:
        data, body, _styles = parse_note(text)
    except ValueError:
        return [issue("invalid_frontmatter")]
    errors, now = [], now or datetime.now(timezone.utc)
    when = {}
    for key in ("created", "updated", "published"):
        if key not in data:
            if key != "published":
                errors.append(issue("required", key))
            continue
        try:
            when[key] = _instant(data[key])
        except ValueError:
            errors.append(issue("invalid_utc_datetime", key))
            continue
        if when[key] > now + timedelta(seconds=60):
            errors.append(issue("datetime_in_future", key))
    if "created" in when and "updated" in when and when["updated"] < when["created"]:
        errors.append(issue("updated_before_created", "updated"))
    if "model" in data and not (isinstance(data["model"], str) and data["model"].strip()):
        errors.append(issue("invalid_model", "model"))
    elif models and "model" in data and data["model"] not in models:
        errors.append(issue("unverified_model", "model"))
    if "tags" in data:
        if not isinstance(data["tags"], list):
            errors.append(issue("tags_must_be_list", "tags"))
        else:
            if len(data["tags"]) != len(set(map(str, data["tags"]))):
                errors.append(issue("duplicate_tag", "tags"))
            for tag in data["tags"]:
                if not isinstance(tag, str) or not TAG.fullmatch(tag):
                    errors.append(issue("invalid_tag_spelling", "tags"))
                elif tags and tag not in tags:
                    errors.append(issue("unregistered_tag", "tags"))
    for key, spec in (profile or {}).get("fields", {}).items():
        if key not in data:
            if spec.get("required"):
                errors.append(issue("required", key))
        elif not _fits(data[key], spec):
            errors.append(issue("invalid_field", key))
    if profile and "model" not in data:                 # an episode note names the model that wrote it
        errors.append(issue("required", "model"))
    if re.search(r"\{\{[^{}\n]+\}\}", text):
        errors.append(issue("unrendered_template_placeholder"))
    return errors
