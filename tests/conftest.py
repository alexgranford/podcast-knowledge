"""Make the project's .env and scripts/ available to every test, and keep tests off the real registry and notes."""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import settings  # noqa: F401,E402  (importing it loads .env)
import podmonitor  # noqa: E402
import podnote  # noqa: E402
import podqueue  # noqa: E402
import podshows  # noqa: E402

# A notes folder's own metadata standard (its _meta folder) to test against, if you have one; else the
# skill's basic check runs, with a small model catalogue and tag list
LIVE_RULES = Path(os.environ.get("PODNOTE_TEST_RULES") or "/nonexistent")
TEST_TAGS = ["science", "astronomy", "history", "ai", "overview", "script-made"]   # the tags the tests use


@pytest.fixture(autouse=True)
def private_registry(tmp_path, monkeypatch):
    """Every test gets an empty show registry, a notes folder and a cache of its own; tests may still patch them."""
    root = tmp_path / "notes"
    rules = root / "_meta"
    if (LIVE_RULES / "tools" / "metadata_contract.py").is_file():
        shutil.copytree(LIVE_RULES, rules, ignore=shutil.ignore_patterns("*.md", "Archive", "health_reports"))
        vocabulary = rules / "tag-vocabulary.json"                 # the copy also knows the tests' tags
        listed = json.loads(vocabulary.read_text()) if vocabulary.is_file() else {}
        listed["canonical_tags"] = list(dict.fromkeys(listed.get("canonical_tags", []) + TEST_TAGS))
        vocabulary.write_text(json.dumps(listed))
    else:
        rules.mkdir(parents=True)
        for name, data in (("model-catalogue.json", {"verified_models": ["gemma-4-26B-A4B-it-qat-4bit"]}),
                           ("tag-vocabulary.json", {"canonical_tags": TEST_TAGS})):
            (rules / name).write_text(json.dumps(data))
    monkeypatch.setattr(podshows, "STATE", tmp_path / "registry-state")
    import store
    for module in (podqueue, podmonitor, podnote, store):   # never the real queue, store, monitor state or names.txt
        monkeypatch.setattr(module, "STATE", tmp_path / "state")
    monkeypatch.setattr(podnote, "NAMES_FILE", tmp_path / "state" / "names.txt")
    for module in (podnote, podqueue):                      # never the real cache: the queue cleans it
        monkeypatch.setattr(module, "CACHE", tmp_path / "cache")
    for module in (podshows, podnote, podqueue):            # never the real library
        monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(podnote, "SCRIPT_TAG", os.environ.get("PODNOTE_SCRIPT_TAG") or "script-made")   # the folder's marker
    import sources                                          # no network: a source lookup tells nothing in tests
    monkeypatch.setattr(sources.apple, "peek", lambda ref: None)
    monkeypatch.setattr(sources.youtube, "peek", lambda ref: None)
    real = sources.youtube.ytdlp_program                    # YouTube on unless a test switches it off ("")
    monkeypatch.setattr(sources.youtube, "ytdlp_program",
                        lambda: real() if os.environ.get("PODNOTE_YTDLP") == "" else "/usr/bin/yt-dlp")
    import notify                                           # never a real message, never the real user list
    import users
    monkeypatch.setattr(users, "STATE", tmp_path / "users-state")
    sent = []
    monkeypatch.setattr(notify, "send", lambda target, text, who="": sent.append((users.normalize(target), text)) or True)
    notify.sent = sent
    for module in (podshows, podnote, podqueue, podmonitor, users, store):   # nothing real is read or written
        for name in ("ROOT", "STATE", "CACHE"):
            if hasattr(module, name):
                assert str(getattr(module, name)).startswith(str(tmp_path)), f"{module.__name__}.{name} is not the test's"
    for name in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_USER_ID", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_USER_NAME"):
        monkeypatch.delenv(name, raising=False)
