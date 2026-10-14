"""Make the project's .env and scripts/ available to every test, and keep tests off the real registry and notes."""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import settings  # noqa: F401,E402  (importing it loads .env)
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
    for module in (podnote, podqueue):                      # never the real cache: the queue cleans it
        monkeypatch.setattr(module, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(podshows, "ROOT", root)
    monkeypatch.setattr(podnote, "ROOT", root)
    monkeypatch.setattr(podnote, "SCRIPT_TAG", os.environ.get("PODNOTE_SCRIPT_TAG") or "script-made")   # the folder's marker
