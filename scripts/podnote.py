#!/usr/bin/env python3
"""podnote — Apple Podcasts episode -> one Markdown note. One process, run by hand.

    podnote.py <apple-episode-url>          full run: resolve, download, ASR, note
    podnote.py --job-dir <dir>              note only, from an existing job directory
                                            (needs asr/transcript-v1.json + source/resolved-episode-v1.json)
    podnote.py --job-dir <dir> --model X    same, on a different oMLX model. Model output is cached per job
                                            and per model under podnote/<model>/, so a rerun on the same
                                            model makes no model calls; delete that directory to redo it.
    add --force to overwrite an existing note for the same episode.

Design rules (do not relax):
  * This run has no scheduler, no queue, no state machine, no retries. It runs once and exits
    0 or 1; anything that feeds it episodes lives outside it.
  * The model reads at most ~1,800 words of transcript at a time (the last part up to ~2,250).
  * The note is built by two short calls over those part notes - themes, then the summary -
    each cached on disk. If the themes call fails the part notes go in as they stand, so a
    bad call never costs the episode.
  * No validators that judge meaning. Only: did the model stop naturally, is there text.
  * References are grouped by Python from the parts' tagged works and quotes, never by a
    model. They are raw: titles and quotes come from automatic transcription and need the
    external QA pass before they can be trusted.
  * No scores and no truth judgements: checking claims needs an external source and is a
    separate, future pass.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402,F401  (importing it loads the project .env)
import podshows  # noqa: E402  (the show registry: hosts, fixes, advertisers, folders)
import notecheck  # noqa: E402  (the basic note check, for a notes folder without a standard of its own)

CACHE = Path(os.environ.get("PODNOTE_CACHE", "/var/tmp/podnote"))            # job working directory
STATE = Path(os.environ.get("PODNOTE_STATE", "/var/tmp/podnote-state"))     # lock + ASR database
ROOT = Path(os.environ.get("PODNOTE_ROOT", str(Path.home() / "Notes")))      # notes go under it (an Obsidian vault works)
EPISODES = Path(os.environ.get("PODNOTE_NOTES_DIR", "Podcasts"))   # relative to ROOT
MODEL = os.environ.get("PODNOTE_MODEL", "gemma-4-26B-A4B-it-qat-4bit")


def skill_version() -> str:
    """The skill's version from SKILL.md (its single source), stamped on every note as skill_version."""
    path = HERE.parent / "SKILL.md"
    found = re.search(r"(?m)^version:\s*(\S+)", path.read_text(encoding="utf-8")) if path.is_file() else None
    return found.group(1) if found else "unknown"


SKILL_VERSION = skill_version()
# The tag the notes folder gives notes written by scripts rather than models (empty: none)
SCRIPT_TAG = os.environ.get("PODNOTE_SCRIPT_TAG", "").strip()
# The notes folder's own metadata standard, if it has one (each part optional), relative to ROOT
RULES = {"validator": "_meta/tools/metadata_contract.py", "models": "_meta/model-catalogue.json",
               "tags": "_meta/tag-vocabulary.json", "profile": "_meta/podcast-metadata-profile.json"}
NAMES_FILE = HERE.parent / "names.txt"          # "wrong => Right", "!phrase", "@show => hosts" — see that file

SECTION_WORDS = int(os.environ.get("PODNOTE_SECTION_WORDS", "1800"))   # transcript words per part
SECTION_MAX_TOKENS = int(SECTION_WORDS * 1.0) + 200                       # a cap, not a target: notes grow with the part
THEMES_MAX_TOKENS = 3200
SUMMARY_MAX_TOKENS = 700

from note_prompts import SECTION_PROMPT, THEMES_PROMPT, SUMMARY_PROMPT, TAGS_PROMPT  # noqa: E402  (the model prompts, word for word)


class Stop(Exception):
    """An expected, explainable failure: report it in one line and exit 1, with no traceback."""


def log(message: str) -> None:
    print(f"[podnote {time.strftime('%H:%M:%S')}] {message}", flush=True)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


# ---------------------------------------------------------------- front half (existing, proven code)

def asr_blocked(state_job: str) -> bool:
    """Has the transcription ledger given up on this record (it then refuses to go on)?"""
    from podcast_state import NotFound, PodcastStore
    if not (STATE / "asr-state.sqlite3").is_file():
        return False
    store = PodcastStore(STATE / "asr-state.sqlite3")
    try:
        return store.get_job(state_job)["stage"] == "blocked_needs_human"
    except NotFound:
        return False
    finally:
        store.close()


def asr_known(state_job: str) -> bool:
    """Does the transcription ledger already hold this record?"""
    from podcast_state import NotFound, PodcastStore
    if not (STATE / "asr-state.sqlite3").is_file():
        return False
    store = PodcastStore(STATE / "asr-state.sqlite3")
    try:
        store.get_job(state_job)
        return True
    except NotFound:
        return False
    finally:
        store.close()


def drop_audio(root: Path) -> int:
    """Delete an episode's audio once its transcript is complete: the download and the cut windows
    (also those of earlier runs set aside in previous-*/). Returns the bytes freed. The transcript and
    everything made from it stay; transcribing again downloads the audio again."""
    if not (root / "asr" / "transcript-v1.json").is_file():
        return 0
    freed = 0
    for path in [root / "source" / "episode-audio", *root.glob("asr/windows/*.wav"), *root.glob("previous-*/asr/windows/*.wav")]:
        try:
            freed += path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            pass
    return freed


def fetch_audio(url: str, metadata: dict, metadata_path: Path, audio: Path) -> None:
    """Download the episode's audio. A link that has expired since it was first resolved (HTTP 403, 404
    or 410: podcast hosts sign their links) is resolved again from the feed once and tried again."""
    import podfetch
    from datetime import datetime, timezone
    from audio_acquisition import AudioError, acquire_audio
    try:
        acquire_audio(metadata["audio_url"], audio)
        return
    except AudioError as error:
        if not re.search(r"\bHTTP (403|404|410)\b", str(error)):
            raise
        fresh = podfetch.refresh_enclosure_if_needed(url, datetime.now(timezone.utc), 403, cache_dir=CACHE / "resolver")
        if fresh is None or not fresh.enclosure_url or fresh.enclosure_url == metadata["audio_url"]:
            raise
    log("the audio link had expired; downloading from the feed's current link")
    metadata["audio_url"] = fresh.enclosure_url
    atomic_write(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2))
    acquire_audio(metadata["audio_url"], audio)


def front_half(url: str, retranscribe: bool = False) -> Path:
    """Resolve, download and transcribe with the existing modules. Skips whatever is already on disk,
    unless retranscribe: then the earlier transcript and model output are set aside in previous-<time>/
    and the episode is transcribed again (with today's name hint) under a fresh ASR state record,
    whose id is kept in asr/state-job-id so an interrupted run resumes it."""
    import podfetch
    from podcast_state import NotFound, PodcastStore
    from podcast_transcription import TranscriptionRunner, artifact_descriptor

    try:
        show, episode = podfetch.resolve_apple_url(url, cache_dir=CACHE / "resolver")
    except LookupError as error:
        # Apple indexes a new episode some time after it is published; nothing here can fix that.
        raise Stop(f"{error}. Apple has not indexed this episode yet — try again later.") from error
    except ValueError as error:
        raise Stop(str(error)) from error
    if episode is None:
        raise Stop("URL must be an Apple Podcasts episode link containing ?i=<episode-id>")
    show_id, episode_id, country = podfetch.parse_apple_url(url)
    job_id = f"podcast-{episode_id}"
    root = CACHE / "jobs" / job_id
    metadata_path = root / "source" / "resolved-episode-v1.json"
    if not metadata_path.is_file():
        atomic_write(metadata_path, json.dumps({
            "source_url": url, "apple_episode_id": episode_id, "show": show.title, "episode": episode.title,
            "published": episode.published.date().isoformat() if episode.published else None,
            "duration_seconds": round(podfetch._duration_seconds(episode.duration) or 0),
            "audio_url": episode.enclosure_url, "episode_description": episode.description or "",
        }, ensure_ascii=False, indent=2))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    credits = {"show_author": show.author, "show_owner": show.owner, "show_people": show.people,
               "episode_author": episode.author, "episode_people": episode.people, "feed_url": show.feed_url,
               "apple_show_id": show_id, "country": country or "gb", "published_utc": utc(episode.published)}
    if any(metadata.get(key) != value for key, value in credits.items()):   # who makes the show, from the feed
        metadata.update(credits)
        atomic_write(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2))

    transcript = root / "asr" / "transcript-v1.json"
    state_marker = root / "asr" / "state-job-id"
    if not (root / "asr").exists() and asr_known(job_id):   # its folder was cleaned away: the old record points at nothing
        atomic_write(state_marker, f"{job_id}-asr-{time.strftime('%Y%m%d-%H%M%S')}\n")
    if not transcript.is_file() and asr_blocked(state_marker.read_text(encoding="utf-8").strip()
                                                if state_marker.is_file() else job_id):
        retranscribe = True                        # a blocked transcription record is not retried: start afresh
    if retranscribe and (transcript.is_file() or (root / "asr").exists()):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = root / f"previous-{stamp}"
        backup.mkdir()
        for name in ("asr", "podnote"):
            if (root / name).exists():
                os.replace(root / name, backup / name)
        atomic_write(state_marker, f"{job_id}-asr-{stamp}\n")
        log(f"earlier transcript and model output set aside in {backup.name}; transcribing again")
    elif transcript.is_file():
        log("transcript already on disk; skipping download and ASR")
        return root
    state_job = state_marker.read_text(encoding="utf-8").strip() if state_marker.is_file() else job_id
    identity = f"apple:{episode_id}" + (state_job[len(job_id):].replace("-asr-", "#asr-", 1) if state_job != job_id else "")
    audio = root / "source" / "episode-audio"
    if not audio.is_file():
        log("downloading audio")
        fetch_audio(url, metadata, metadata_path, audio)
    log("transcribing (local ASR)")
    store = PodcastStore(STATE / "asr-state.sqlite3")
    try:
        try:
            store.get_job(state_job)
        except NotFound:
            store.create_job(state_job, identity)
            for stage in ("resolving", "acquiring_audio"):
                store.transition_job(state_job, stage, expected_revision=store.get_job(state_job)["revision"], reason="podnote")
            store.record_decision(state_job, operation="audio_acquired", expected_revision=store.get_job(state_job)["revision"],
                                  artifacts=[artifact_descriptor(state_job, "source-audio-v1", "source-audio", audio)])
            store.transition_job(state_job, "transcribing", expected_revision=store.get_job(state_job)["revision"], reason="podnote")
        TranscriptionRunner(store, state_job, audio, root, model_arbiter_root=CACHE / "model-arbiter",
                            asr_prompt=[asr_hint(metadata), asr_hint(metadata, 600)]).run_all()
        artifacts = store.list_artifacts(state_job, "ordered-transcript") or store.list_artifacts(state_job, "validated-transcript")
        produced = Path(artifacts[-1]["path"])
    finally:
        store.close()
    if produced.resolve() != transcript.resolve():
        atomic_write(transcript, produced.read_text(encoding="utf-8"))
    if freed := drop_audio(root):
        log(f"transcript complete; audio deleted ({freed / 1e6:.0f} MB)")
    return root


# ---------------------------------------------------------------- back half (new, deliberately small)

def sections(transcript: dict) -> list[str]:
    text = " ".join(str(chunk.get("text") or "") for chunk in transcript["chunks"])
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text).strip())
    chunks: list[str] = []
    for sentence in sentences:                    # ASR sometimes emits thousands of words unpunctuated
        words = sentence.split()
        while len(words) > SECTION_WORDS:
            chunks.append(" ".join(words[:SECTION_WORDS]))
            words = words[SECTION_WORDS:]
        if words:
            chunks.append(" ".join(words))
    parts, current, count = [], [], 0
    for sentence in chunks:
        words = len(sentence.split())
        if current and count + words > SECTION_WORDS:
            parts.append(" ".join(current))
            current, count = [], 0
        current.append(sentence)
        count += words
    if current:
        if parts and count < SECTION_WORDS // 4:
            parts[-1] += " " + " ".join(current)
        else:
            parts.append(" ".join(current))
    return parts


def ask(base_url: str, headers: dict, prompt: str, max_tokens: int) -> tuple[str, str]:
    """One model call. Returns (text, finish_reason). Never retries."""
    response = requests.post(base_url + "/v1/chat/completions", headers=headers, timeout=(15, 900), json={
        "model": MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False,
        "max_tokens": max_tokens,
        # Gemma's own sampling defaults. No presence_penalty: it penalises tokens already written, so
        # a name or year repeated in the notes comes out mangled ("Hugging Face" -> "Hugio Face");
        # dropping it halved such corruptions on a 6-episode test. Low temperature (0.3) did worse
        # and repeated bullets; greedy decoding loops on list output. Thinking cost 5x the time
        # for no clear gain, so it stays off.
        "temperature": 1.0, "top_p": 0.95, "top_k": 64,
        "chat_template_kwargs": {"enable_thinking": False},
    })
    response.raise_for_status()
    choice = response.json()["choices"][0]
    text = (choice["message"].get("content") or "").strip()
    text = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", text).strip()
    return text, str(choice.get("finish_reason"))


# promotion only: a bullet about advertising or sponsorship as a subject stays
BUILTIN_DROPS = ["sponsored by", "brought to you by", "discount code", "promo code", "use code", "per rules",
                 "http", ".com", "www."]


def name_rules() -> list[str]:
    """names.txt lines with blank lines, comment lines and trailing '   # comments' removed."""
    if not NAMES_FILE.is_file():
        return []
    lines = (re.sub(r"\s#.*$", "", line).strip() for line in NAMES_FILE.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def hosts_for(show: str, metadata: dict | None = None) -> str:
    """Who the regular hosts are: a names.txt line '@show name fragment => Host One, Host Two' first;
    otherwise the feed: podcast:person tags with role host, then a full name in the show title after
    "with" or "|", then the show's author when it is a person's name (The Tech Morning Brief: "Nadia Whitcombe", where the speech says "NW"). The feed
    owner counts only when there is no author (owners are often producers), and an episode's own
    author field never does (it lists contributors)."""
    for line in name_rules():
        if line.startswith("@") and "=>" in line:
            fragment, hosts = (part.strip() for part in line[1:].split("=>", 1))
            if fragment and fragment.lower() in show.lower():
                return hosts
    metadata = metadata or {}
    people = [p.get("name", "").strip() for p in (metadata.get("episode_people") or []) + (metadata.get("show_people") or [])
              if p.get("role", "host") == "host"]
    if not people:                                    # "Signals with Omar Haddad", "Outliers | Martin Grey"
        named = re.search(r"(?:\bwith|\|)\s+(.+)$", show)
        people = re.split(r"\s*(?:,|&|;|\band\b)\s*", named.group(1)) if named else []
    if not any(podshows.looks_like_person(p, show) for p in people):
        credit = metadata.get("show_author") or metadata.get("show_owner") or ""
        people = re.split(r"\s*(?:,|&|;|\band\b)\s*", credit)
    found = list(dict.fromkeys(p for p in people if podshows.looks_like_person(p, show)))
    return ", ".join(found) if found else "not known - refer to them as 'the host' and never guess a surname"


def apply_names(text: str, fixes: dict | None = None) -> str:
    """names.txt (global spellings and drops) and the show's own name fixes from the registry."""
    rules = name_rules() + [f"{wrong} => {right}" for wrong, right in (fixes or {}).items()]
    drops = [line[1:].strip().lower() for line in rules if line.startswith("!") and line[1:].strip()]
    drops += BUILTIN_DROPS
    text = "\n".join(line for line in text.splitlines()
                     if not (line.lstrip().startswith(("- ", "* ")) and any(d in line.lower() for d in drops)))
    for line in rules:
        if "=>" in line and not line.startswith(("@", "!")):
            wrong, right = (part.strip() for part in line.split("=>", 1))
            if wrong:
                text = re.sub(rf"\b{re.escape(wrong)}\b", right, text, flags=re.IGNORECASE)
    return text + "\n"


# The "**References:**" label line; anything after its colon is the first entry.
REFS_SPLIT = re.compile(r"(?im)^[ \t*_#>-]*references[ \t*_]*(?::[ \t*_]*|$)")
TAG = re.compile(r"^\[(work|idea|quote)\]\s*", re.I)


def split_references(notes: str) -> tuple[str, list[str]]:
    """Separate a part's points from its References bullets."""
    pieces = REFS_SPLIT.split(notes, maxsplit=1)
    if len(pieces) < 2:
        return notes.strip(), []
    entries = [re.sub(r"^[-*\s]+", "", line).strip() for line in pieces[1].splitlines()]
    return pieces[0].strip(), [e for e in entries if e and not e.lower().strip('".').startswith("none")]


# A reference whose note says it came from an advert ("a podcast described in an advertisement")
ADVERT_REFERENCE = re.compile(r"\b(advert(isement)?s?|advertised|sponsor(ed|s|ship)?|ad read|promo(tion|tional)?)\b", re.I)


def references_section(entries: list[str]) -> str:
    """## References from the parts' tagged works, ideas and quotes, in episode order, without asking a
    model. Untagged bullets are dropped, and so is anything the part note says came from an advert.
    The same title or quote cited in two parts appears once; a "quote" of fewer than four words is a
    term, not a line, and is left out."""
    works, ideas, quotes, seen = [], [], [], set()
    for entry in entries:
        tag = TAG.match(entry)
        if not tag:
            continue
        text = entry[tag.end():].strip()
        about = re.split(r"\s[-\u2013\u2014]\s", text, maxsplit=1)
        if len(about) == 2 and ADVERT_REFERENCE.search(about[1]):
            continue                                       # an advert read out in the episode, not a citation
        key = re.sub(r"\W+", "", re.split(r"\s[-\u2013\u2014]\s", text)[0].lower())
        if not key or key in seen:
            continue
        seen.add(key)
        if tag.group(1).lower() == "work":
            works.append(text)
        elif tag.group(1).lower() == "idea":
            ideas.append(text)
        elif len(re.split(r"\s[-\u2013\u2014]\s", text)[0].split()) >= 4:
            quotes.append(text)
    blocks = [f"### {title}\n" + "\n".join(f"- {item}" for item in items)
              for title, items in (("Works", works), ("Ideas", ideas), ("Quotes", quotes)) if items]
    return "## References\n\n" + "\n\n".join(blocks) if blocks else ""


def summary_callout(summary: str) -> str:
    """The summary in an abstract callout: participants line, the quick summary, the paragraph."""
    lines = no_highlights(summary.strip()).splitlines()
    return "\n".join(["> [!abstract] Summary"] + [f"> {line}".rstrip() if line.strip() else ">" for line in lines])


def no_highlights(text: str) -> str:
    """Notes carry no ==highlights==: the model's choice of phrase to highlight is close to random,
    so it is not asked for, and any it adds anyway is unwrapped."""
    return re.sub(r"==([^=\n]+?)==", r"\1", text)


def render_themes(themes: str) -> str:
    """Theme titles become ### headings (outline, links); the essence paragraph under each title
    becomes a quote block; bullets stay as they are."""
    out, in_essence = [], False
    for line in no_highlights(themes.strip()).splitlines():
        title = re.match(r"^\*\*([^*\n]+)\*\*\s*$", line)
        if title:
            out += ["", f"### {title.group(1).strip()}"] if out else [f"### {title.group(1).strip()}"]
            in_essence = True
            continue
        if in_essence and line.strip() and not line.lstrip().startswith(("- ", "* ")):
            out.append(f"> {line.strip()}")
            continue
        if in_essence and line.lstrip().startswith(("- ", "* ")):
            out.append("")
            in_essence = False
        if line.strip() or (out and out[-1] != ""):
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def listening_time(seconds: float) -> str:
    """'1h 17m' or '29 min' for the note's header line; '' when unknown."""
    minutes = round(seconds / 60)
    if minutes <= 0:
        return ""
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes} min"


def stamp(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


PROMO = re.compile(r"sponsor|% off|save \d+%|promo|discount|\bcode\b|patreon|substack|buy me a coffee|merch|"
                   r"subscribe|follow (us|the|on)|check us out|ad choices|privacy information|advertis|sign up|"
                   r"free trial|buy one get|independently supported|proudly independent|head to|try it|"
                   r"thanks? (you )?to .{0,40} for (making|supporting|dedicating)|\bcredit\b|status/\d", re.I)
CALL_TO_ACTION = re.compile(r"(?:Follow|Subscribe|Join|Support|Shop|Try|Visit|Download|Learn more|Go to|Sign up|Get)\b")
# A link ends where a glued-on sentence starts ("…/offerIn this episode"), so no re.I here.
URL = re.compile(r"[Hh][Tt][Tt][Pp][Ss]?://[^\s]*?(?=[A-Z][a-z]|\s|$)|[Ww]{3}\.\S+|\S+@\S+\.\w+|"
                 r"(?i:\b[\w-]+(?:\.[\w-]+)*\.(?:com|co\.uk|fm|org|net|io)\b(?:/\S*)?)")


SPONSOR_HEADER = re.compile(r"brought to you by|sponsored by|\bsponsors?\s*:|thanks to our (?:sponsors?|partners?)|our sponsors? (?:today|this week)|"
                            r"today'?s sponsors?|this (?:episode|show|podcast) is (?:brought to you|sponsored|supported) by", re.I)
BRAND = r"[A-Z0-9][\w&'’.+-]*(?:\s+(?:&\s+)?[A-Z0-9][\w&'’.+-]*){0,3}"
SPONSOR_LINK = re.compile(r"(?i)https?://\S+|www\.\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|co\.uk|ai|io|fm|org|net|tv)\b\S*")
NOT_A_BRAND = re.compile(r"(?i)^(?:ai|roi|the|our|and|you|your|this|today|learn|more|new|jewish|professor|dr|mr|ms|"
                         r"episode|show|podcast|https?|www|newsletter|website|youtube|twitter|x|linkedin|instagram|"
                         r"tiktok|facebook|spotify|apple podcasts|patreon|substack|merch|shop|store|contact|email)$|"
                         r"^(?:professor|dr|sir|mr|ms|mrs)\b|\d{2,}$")


def sponsors_in(raw: str) -> list[str]:
    """Advertisers the show notes name after an explicit sponsor phrase ("Brought to you by:",
    "Sponsored by", "Thanks to our sponsors"): each "Brand – pitch – link" there, or the names in
    "sponsored by X and Y". Only after such a phrase: elsewhere, a capitalised name before a dash is
    as likely a chapter title or a guest. Their reads in the episode are adverts; the part-note prompt
    names them, and they stay out of the ASR hint and the description given to the model."""
    return list(dict.fromkeys(brand for brand, _ in sponsor_items(raw)))


def sponsor_items(raw: str) -> list[tuple[str, str]]:
    """(brand, pitch) for each advertiser in the show notes; the pitch is the brand's own blurb before
    its link ("Research from Northwind and the University of Exampleton shows…"), "" when the notes
    only name the brand."""
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
    found: list[tuple[str, str]] = []
    for header in SPONSOR_HEADER.finditer(text):
        scope = text[header.end():header.end() + 1500]
        following = SPONSOR_HEADER.search(scope)
        scope = re.sub(r"^[\s:–—-]*(?:by\s+)?", "", scope[:following.start()] if following else scope)
        sentence = re.split(r"(?<=[.!?])\s", scope, maxsplit=1)[0]
        if not SPONSOR_LINK.search(sentence) and not re.match(rf"\s*{BRAND}\s*(?:[–—:|]|\s-\s|-\s)", scope):
            found += [(name, "") for name in re.findall(rf"(?:^|,|\band\b|&)\s*({BRAND})", sentence)[:6]]
            continue                                     # "sponsored by X and Y."
        for item in re.finditer(rf"(?:^|(?<=[\s.!?]))({BRAND})\s*(?:[–—:|]|\s-\s|-\s)", scope):
            link = SPONSOR_LINK.search(scope[item.end():item.end() + 250])
            if not link:
                continue                                 # a list item is "Brand – pitch – link"
            words = item.group(1).split()
            while len(words) > 1 and words[0].isupper() and not words[1].isupper():
                words = words[1:]                        # a promo code before the brand: "SHOW Voxa"
            found.append((" ".join(words), scope[item.end():item.end() + link.start()].strip()))
    found = [(n.strip().rstrip(".,;:!"), pitch) for n, pitch in found]
    return [(n, pitch) for n, pitch in found if 2 <= len(n) <= 40 and not NOT_A_BRAND.search(n)]


def drop_native_ads(text: str, items: list[tuple[str, str]]) -> str:
    """Remove bullets that are advertising read as content: a bullet naming an advertiser and
    repeating at least two distinctive words of that advertiser's own pitch ("A Northwind and University
    of Exampleton study found…" against "Research from Northwind and the University of Exampleton
    shows…"). A bullet merely mentioning the brand stays."""
    return "\n".join(line for line in text.splitlines()          # bullets and the themes' essence lines
                     if not (line.lstrip().startswith(("- ", "* ", "> ")) and native_ad(line, items)))


def native_ad(text: str, items: list[tuple[str, str]]) -> bool:
    """A line naming an advertiser and repeating two distinctive words of its own pitch."""
    words = lambda value: {w for w in re.findall(r"[a-z][a-z'-]{3,}", value.lower()) if w not in COMMON_WORDS}
    return any(pitch and re.search(rf"\b{re.escape(brand)}\b", text, re.I)
               and len((words(text) & words(pitch)) - words(brand)) >= 2 for brand, pitch in items)


COMMON_WORDS = {"that", "this", "with", "from", "your", "have", "more", "about", "their", "they", "them", "what",
                "which", "when", "will", "would", "could", "should", "into", "than", "then", "there", "these",
                "those", "been", "were", "also", "just", "like", "learn", "visit", "free", "best", "help", "make"}


def clean_description(raw: str, limit: int = 1500, drop: list[str] | tuple = ()) -> str:
    """The episode description minus links and promotion, chapter list kept. Links are removed on
    their own rather than taking their sentence with them: show notes often glue the guest
    introduction to a sponsor link, and run sentences together without a space."""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = URL.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    pieces = re.split(r"(?<=[.!?):])\s*(?=[A-Z(\"“])|\s+-{1,2}\s+|\s(?=\(?\d{1,2}:\d{2}(?::\d{2})?\)?\s)|"
                      r"\s(?=(?:Follow|Subscribe|Join|Support|Shop|Try|Visit|Download|Learn more|Go to|Sign up)\b)",
                      text)
    pieces = [p.strip(" -|:,(") for p in pieces]
    kept = [p for p in pieces if len(p.split()) >= 3 and not PROMO.search(p) and not CALL_TO_ACTION.match(p)
            and not any(p.startswith(brand) for brand in drop)]
    return " ".join(kept)[:limit]


NOT_NAMES = {"The", "In", "This", "On", "And", "But", "We", "Our", "Join", "Subscribe", "Follow", "Listen", "Get",
             "Support", "Watch", "Today", "For", "With", "What", "Why", "How", "Who", "When", "A", "An", "It", "If",
             "To", "Is", "Are", "Was"}


def names_in(text: str) -> list[str]:
    """Runs of two to four capitalised words ("Arthur Conan Doyle", "Ottoman Empire")."""
    found = []
    for run in re.findall(r"[A-Z][\w'’-]+(?:[ \t]+(?:[A-Z][\w'’-]+|of|de|bin|al|ibn))+", text):
        words = run.split()
        while words and words[0] in NOT_NAMES:
            words = words[1:]
        candidate = " ".join(words)
        if 2 <= len(words) <= 4 and not any(w in NOT_NAMES for w in words) and candidate not in found:
            found.append(candidate)
    return found


def show_key(metadata: dict) -> tuple[str, str]:
    """(Apple show id, storefront) of an episode, from its metadata or its Apple link."""
    found = re.search(r"podcasts\.apple\.com/(\w\w)/podcast/[^?]*?id(\d+)", metadata.get("source_url", ""))
    return (metadata.get("apple_show_id") or (found.group(2) if found else ""),
            metadata.get("country") or (found.group(1) if found else "gb"))


def registry_hosts(metadata: dict) -> tuple[list[str], list[str], dict | None]:
    """(regular hosts, recurring co-hosts, registry entry) for this episode's show and date; the
    registry's hosts, else the names.txt rule and the feed (hosts_for)."""
    entry = podshows.show_for(show_key(metadata)[0])
    regular, cohosts = podshows.hosts_on(entry, metadata.get("published") or "")
    if not regular and not cohosts:
        found = hosts_for(str(metadata.get("show", "")), metadata)
        regular = [] if found.startswith("not known") else [h.strip() for h in found.split(",") if h.strip()]
    return regular, cohosts, entry


GREETING = r"(?:hi|hello|hey|welcome|good (?:morning|afternoon|evening))\b[^.?!]{0,60}?"
LEAD_OUT = r"(?:thanks? (?:you )?for listening|this has been [^.?!]{1,40}|that'?s (?:all|it) for [^.?!]{1,30})[.!]?\s+"
SIGN_OFF = r"(?:,|\.)?\s+(?:and\s+)?(?:this is|welcome|you(?:'re| are) listening|joined|with me|(?:we|i)(?:'ll| will) see you|see you|thanks?|thank you|bye|goodbye)\b"
NAME = r"([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){1,2})"
SELF_INTRO = re.compile(
    rf"(?i:{GREETING})(?:I'm|I am|my name is)\s+{NAME}"                       # "Hi, I'm Iris Fleming"
    rf"|(?i:{LEAD_OUT})(?:I'm|I am)\s+{NAME}"                                  # "Thanks for listening. I'm Iris Fleming"
    rf"|\b(?:I'm|I am|my name is)\s+{NAME}(?={SIGN_OFF}|[.!]?\s*$)"           # "I am Lucas Berg, and we'll see you"
    rf"|(?i:\bwith me,?\s+|\bco-?hosts?,?\s+){NAME}")                         # "with me, Tomas Brandt"


def self_introductions(transcript: dict, show: str) -> list[str]:
    """Who introduces themselves at the start or signs off at the end ("Hi, I'm Iris Fleming", "I am
    Lucas Berg, and we'll see you next week"): evidence of who hosts an episode, kept with the
    episode's metadata for the show registry. Greeting or sign-off wording is required, so banter
    ("I'm Team Newton on this one") is not taken for a name."""
    words = " ".join(str(chunk.get("text") or "") for chunk in transcript.get("chunks", [])).split()
    ends = " ".join(words[:900]) + " … " + " ".join(words[-400:])
    found = [next(group for group in match if group) for match in SELF_INTRO.findall(ends)]
    return list(dict.fromkeys(name for name in found
                              if podshows.looks_like_person(name, show) and name.lower() not in show.lower()))


def asr_hint(metadata: dict, description_limit: int = 1500) -> str:
    """Context for the ASR model, so it spells the people and places of this episode right
    ("Maya", not "Maia"): show, hosts, the names in the title and description, then the cleaned
    description itself. A bare list of names is not enough: in tests Qwen3-ASR once answered a
    names-only hint with the hint and dropped the speech (podcast_transcription.hint_failure)."""
    show, episode = metadata.get("show", ""), metadata.get("episode", "")
    regular, cohosts, entry = registry_hosts(metadata)
    hosts = ", ".join(regular + cohosts)
    sponsors = sponsors_in(metadata.get("episode_description", "")) + podshows.active_advertisers(entry, metadata.get("published") or "")
    description = clean_description(metadata.get("episode_description", ""), limit=description_limit, drop=sponsors)
    names = [n for n in names_in(f"{episode}. {description}") if n not in hosts
             and not any(brand.lower() in n.lower() for brand in sponsors)]
    return (f"Podcast: {show}." + (f" Hosts: {hosts}." if hosts else "") + (f" Names: {', '.join(names)}." if names else "")
            + f" Episode: {episode}. {description}").strip()


def work_dir(root: Path) -> Path:
    """Cached model output for this job, one directory per model, so a --model rerun never reuses
    another model's text under its own synthesis_model label."""
    return root / "podnote" / re.sub(r"[^\w.-]", "_", MODEL)


def fresh_work_dir(root: Path, context: str = "") -> Path:
    """The model-output cache, keyed to what it was made from: the transcript, the prompts and what
    fills them (hosts, advertisers, description — the registry learns these over time). Cached
    part notes are numbered by section, so after a new transcript they would describe the old one, and
    after a prompt change they would keep the old rules. A cache made from other inputs, or without a
    record of its inputs, is set aside (kept beside it as <model>-stale-<time>)."""
    work = work_dir(root)
    inputs = ((root / "asr" / "transcript-v1.json").read_bytes()
              + (SECTION_PROMPT + THEMES_PROMPT + SUMMARY_PROMPT + context).encode()
              + f"parts of {SECTION_WORDS} words, at most {SECTION_MAX_TOKENS} tokens".encode())   # how it is cut
    digest = hashlib.sha256(inputs).hexdigest()
    stamp_file = work / "inputs.sha256"
    stamped = stamp_file.read_text(encoding="utf-8").strip() if stamp_file.is_file() else ""
    if work.is_dir() and any(work.iterdir()) and stamped != digest:
        os.replace(work, work.with_name(f"{work.name}-stale-{time.strftime('%Y%m%d-%H%M%S')}"))
        log("model output cache came from another transcript or prompt; set aside")
    work.mkdir(parents=True, exist_ok=True)
    if not stamp_file.is_file():
        atomic_write(stamp_file, digest + "\n")
    return work


def synthesise(root: Path, base_url: str, headers: dict, origin: dict | None = None) -> tuple[str, list[str]]:
    metadata = json.loads((root / "source" / "resolved-episode-v1.json").read_text(encoding="utf-8"))
    transcript = json.loads((root / "asr" / "transcript-v1.json").read_text(encoding="utf-8"))
    parts = sections(transcript)
    total_words = sum(len(part.split()) for part in parts) or 1
    duration = float(transcript.get("duration_seconds") or metadata.get("duration_seconds") or 0)
    regular, cohosts, entry = registry_hosts(metadata)
    sponsors = list(dict.fromkeys(sponsors_in(str(metadata.get("episode_description") or ""))
                                  + podshows.active_advertisers(entry, metadata.get("published") or "")))
    description = clean_description(str(metadata.get("episode_description") or ""), drop=sponsors)
    introduced = self_introductions(transcript, str(metadata.get("show", "")))
    if metadata.get("self_introductions") != introduced:       # evidence of who hosts, for the registry
        metadata["self_introductions"] = introduced
        atomic_write(root / "source" / "resolved-episode-v1.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    advertisers = (f" This episode's advertisers, named in its show notes, are {', '.join(sponsors)}: a passage "
                   "promoting any of them is an advert, so skip it." if sponsors else "")
    work = fresh_work_dir(root, json.dumps([regular, cohosts, sponsors, description], ensure_ascii=False))
    hosts = (", ".join(regular) + (f" (and, on some episodes, co-host {', '.join(cohosts)})" if cohosts else "")
             if regular or cohosts else "not known - refer to them as 'the host' and never guess a surname")
    problems: list[str] = []
    blocks: list[str] = []
    references: list[str] = []
    seen = 0
    for index, part in enumerate(parts, 1):
        start = stamp(duration * seen / total_words)
        seen += len(part.split())
        saved = work / f"section-{index:02d}.md"
        if saved.is_file():                       # resume after a crash without redoing finished sections
            notes = saved.read_text(encoding="utf-8")
        else:
            log(f"section {index}/{len(parts)}")
            try:
                notes, finish = ask(base_url, headers, SECTION_PROMPT.format(
                    n=index, total=len(parts), show=metadata.get("show", ""), hosts=hosts, episode=metadata.get("episode", ""),
                    description=description, advertisers=advertisers, text=part), SECTION_MAX_TOKENS)
            except requests.exceptions.RequestException as error:
                # The server is down or unreachable: stop now rather than write a note of error
                # messages. Sections already finished stay on disk, so a rerun resumes from here.
                raise Stop(f"model server unreachable at section {index}: {error}") from error
            except Exception as error:            # one bad section must not cost the episode
                notes, finish = "", f"error: {error}"
            if finish != "stop" or not notes:          # a failed part is not cached: a re-run asks again
                problems.append(f"section {index} ({start}): {finish}")
                notes = notes or "_No notes produced for this part._"
            else:
                atomic_write(saved, notes)
        if notes.strip().upper().startswith("SKIP"):
            continue
        points, part_refs = split_references(notes)
        references += part_refs
        blocks.append(f"### Part {index} (from ~{start})\n{points}")
    reading_notes = "\n\n".join(blocks)

    def call(label: str, prompt: str, limit: int, content: bool = True) -> str:
        """One cached model call. A failure returns empty, never a retry; a failed content call (themes,
        summary) makes the note incomplete, a failed metadata call (tags) only leaves the note without it."""
        saved = work / f"{label}.md"
        if saved.is_file():
            return saved.read_text(encoding="utf-8")
        log(label)
        try:
            text, finish = ask(base_url, headers, prompt, limit)
        except requests.exceptions.RequestException as error:
            raise Stop(f"model server unreachable during {label}: {error}") from error
        except Exception as error:
            text, finish = "", f"error: {error}"
        if finish != "stop" or not text:
            if content:
                problems.append(f"{label}: {finish}")
            else:
                log(f"{label} not produced ({finish}); the note goes without it")
            return ""
        atomic_write(saved, text)
        return text

    minutes = round(duration / 60)
    themes = call("themes", THEMES_PROMPT.format(
        show=metadata.get("show", ""), hosts=hosts, episode=metadata.get("episode", ""),
        minutes=minutes, parts=len(blocks), target=max(3, min(8, round(minutes / 12))),
        notes=reading_notes), THEMES_MAX_TOKENS)
    if themes and not themes.lstrip().startswith("## "):
        themes = "## Key themes\n\n" + themes

    themes = drop_native_ads(themes, sponsor_items(str(metadata.get("episode_description") or "")))
    # a regular host away this week must not be listed: keep the hosts the notes name, or all of them
    # when the notes name none (hosts are often never addressed by name)
    host_list = regular + cohosts
    present = [h for h in host_list if re.search(rf"\b{re.escape(h.split()[-1])}\b", reading_notes + themes)]
    summary = call("summary", SUMMARY_PROMPT.format(
        show=metadata.get("show", ""),
        hosts=", ".join(present) if present else (f"not named in the notes (the show's hosts: {', '.join(host_list)})"
                                                    if host_list else "not known"),
        episode=metadata.get("episode", ""), description=description, themes=themes), SUMMARY_MAX_TOKENS)
    first = summary.lstrip().split("\n", 1)[0]
    if summary and first.startswith("**") and not first.startswith("**Participants:**"):
        summary = "**Participants:** " + summary.lstrip()       # the model dropped the label, kept the names

    summary = registry_roles(summary, regular + cohosts)
    # hosts and guests as properties, so the Properties panel and the Podcasts base can use them
    people = re.findall(r"\*\*([^*]+)\*\*\s*\(([^)]*)\)", summary.lstrip().split("\n", 1)[0])
    host_names = [name.strip() for name, role in people if "host" in role.lower()]
    guest_names = [name.strip() for name, role in people if "host" not in role.lower()]

    tags, missing = [], []
    known, aliases = tag_registry()
    if known and summary:
        fingerprint = hashlib.sha256((", ".join(known) + TAGS_PROMPT).encode()).hexdigest()[:8]  # new registry or prompt: new answer
        answer = call(f"tags-{fingerprint}", TAGS_PROMPT.format(vocabulary=", ".join(known), episode=metadata.get("episode", ""),
                                                 summary=summary, themes=themes[:6000]), 600, content=False)
        try:
            parsed = json.loads(re.search(r"\{.*\}", answer, re.S).group(0)) if answer else {}
        except (AttributeError, json.JSONDecodeError):
            parsed = {}
        named = [aliases.get(str(t).lower(), str(t)) for t in parsed.get("tags", [])]
        tags = list(dict.fromkeys(t for t in named if t in known))[:5]
        missing = [str(t) for t in parsed.get("missing", []) if str(t) not in known][:3]

    # provenance kept from the existing note, documented podcast fields, no processing state
    written = utc_now()
    origin = dict(origin or {"new": True})
    if origin.get("new") or not origin.get("created"):  # a new note, or a start never recorded: now
        origin["created"] = written
    publisher = str((entry or {}).get("publisher") or metadata.get("show_author") or "").strip()
    published = metadata.get("published_utc") or origin.get("published") or ""
    if published == origin.get("created"):             # only a different instant is worth the field
        published = ""
    front = "\n".join([line for line in [
        "---", "origin: agent", f"model: {MODEL}",                          # the model that wrote this text
        f'created: "{origin["created"]}"' if origin.get("created") else "", f'updated: "{written}"',
        f"tags: [{', '.join(tags)}]" if tags else "",
        f"show: {json.dumps(metadata.get('show', ''), ensure_ascii=False)}",
        f"episode: {json.dumps(metadata.get('episode', ''), ensure_ascii=False)}",
        f'published: "{published}"' if published else "",
        f"hosts: {json.dumps(host_names, ensure_ascii=False)}" if host_names else "",
        f"guests: {json.dumps(guest_names, ensure_ascii=False)}" if guest_names else "",
        f"publisher: {json.dumps(publisher, ensure_ascii=False)}" if publisher else "",
        f"source_url: {metadata.get('source_url', '')}",
        f"apple_episode_id: '{metadata.get('apple_episode_id', '')}'",
        f"duration: {round(duration)}" if duration else "",               # seconds
        "---", ""] if line != ""] + [""])
    write_record(root, composed_by=MODEL, tags=tags, tag_suggestions=missing)   # completion is stamped after the write
    strip = [part for part in (f"**{metadata['show']}**" if metadata.get("show") else "",
                               metadata.get("published") or "",
                               f"⏱ {listening_time(duration)}" if listening_time(duration) else "",
                               f"🎧 [Apple Podcasts]({metadata['source_url']})" if metadata.get("source_url") else "")
             if part]
    body = [f"# {metadata.get('episode', '') or metadata.get('show', '')}", ""]
    if strip:
        body += [" · ".join(strip), ""]
    if summary:
        body += [summary_callout(summary), ""]
    body += [render_themes(themes)]
    adverts = sponsor_items(str(metadata.get("episode_description") or ""))
    refs = no_highlights(references_section([entry for entry in references if not native_ad(entry, adverts)]))
    if refs:
        body += ["", refs]
    body = name_the_host("\n".join(body) + "\n", host_names)
    return front + apply_names(body, (entry or {}).get("fixes")), problems


THE_HOST = re.compile(r"\b([Tt])he host('s)?\b(?!\s+(?:country|countries|nation|city|team|government|family|"
                      r"club|venue|site|server|body|cell|population|state))")


def registry_roles(summary: str, hosts: list[str]) -> str:
    """The Participants line labels everyone the show registry knows as a host "host", whatever the
    model wrote (it once called a show's own presenter a guest)."""
    if not summary or not hosts:
        return summary
    first, _, rest = summary.lstrip().partition("\n")
    known = {h.lower() for h in hosts}

    def label(match):
        name, role = match.group(1), match.group(2)
        if name.strip().lower() in known and "host" not in role.lower():
            role = "host"
        return f"**{name}** ({role})"
    return re.sub(r"\*\*([^*]+)\*\*\s*\(([^)]*)\)", label, first) + ("\n" + rest if rest else "")


def name_the_host(text: str, hosts: list[str]) -> str:
    """With exactly one host, "the host" in the note's prose is that person (quotes are left alone)."""
    if len(hosts) != 1:
        return text
    name = hosts[0]
    lines = []
    for line in text.split("\n"):
        if line.lstrip("> ").startswith(('"', "“")):
            lines.append(line)
            continue
        lines.append(THE_HOST.sub(lambda m: name + (m.group(2) or ""), line))
    return "\n".join(lines)


def utc(moment) -> str:
    """A datetime (aware, or naive meaning UTC) or an ISO string as the notes' UTC form
    YYYY-MM-DDTHH:MM:SS; "" when there is no instant (a date alone is not one)."""
    from datetime import datetime, timezone
    if isinstance(moment, str):
        text = moment.strip().strip("'\"")
        if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
            return ""
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return ""
    if not moment:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def tag_registry() -> tuple[list[str], dict[str, str]]:
    """The notes folder's tag list and the other spellings it maps to them ("AI" -> "ai"). The script marker
    (PODNOTE_SCRIPT_TAG) is not a subject and is never offered."""
    path = ROOT / RULES["tags"]
    if not path.is_file():
        return [], {}
    registry = json.loads(path.read_text(encoding="utf-8"))
    aliases = registry.get("proposed_aliases") or {}
    return ([t for t in registry.get("canonical_tags", []) if t != SCRIPT_TAG],
            {str(k).lower(): str(v) for k, v in aliases.items()} if isinstance(aliases, dict) else {})


_CHECKER = {}


def note_checker():
    """The notes folder's own metadata module when it has one (loaded once), else the basic check."""
    path = ROOT / RULES["validator"]
    if not path.is_file():
        return notecheck
    if path not in _CHECKER:
        import importlib.util
        spec = importlib.util.spec_from_file_location("metadata_contract", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CHECKER[path] = module
    return _CHECKER[path]


def read_front(text: str) -> dict:
    """A note's properties; {} if there are none or they do not parse."""
    try:
        return notecheck.parse_note(text)[0] or {}
    except Exception:
        return {}


def validate(note: str, episode: bool = True) -> list[str]:
    """The check on a note about to be written; anything it reports blocks the write. The notes folder's
    own standard when it has one (validator, model catalogue, tag list, podcast profile), else the basic
    check (notecheck.py); a missing catalogue or tag list allows any."""
    def read(key, default):
        path = ROOT / RULES[key]
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    found = note_checker().validate_note(note, set(read("models", {}).get("verified_models", [])),
                                         set(read("tags", {}).get("canonical_tags", [])),
                                         profile=read("profile", notecheck.EPISODE_PROFILE) if episode else None)
    return [f"{i['code']}{':' + i['field'] if i.get('field') else ''}" for i in found]


def episode_notes(episode: str) -> list[Path]:
    """Every note in the podcasts folder for this Apple episode id."""
    folder = ROOT / EPISODES
    if not episode or not folder.is_dir():
        return []
    return [p for p in sorted(folder.rglob("*.md"))
            if str(read_front(p.read_text(encoding="utf-8", errors="replace")).get("apple_episode_id", "")) == episode]


def provenance(root: Path) -> dict:
    """When the note was first made, from the episode's existing note(s): `created`, and `published` if
    the feed no longer gives it. An unknown or unreadable start becomes the time of this write (a job
    never fails over a date). `model` is always the model of the latest pass, so it is not carried over."""
    meta = json.loads((root / "source" / "resolved-episode-v1.json").read_text(encoding="utf-8"))
    copies = episode_notes(str(meta.get("apple_episode_id", "")))
    if not copies:
        return {"new": True, "copies": []}
    fronts = [read_front(p.read_text(encoding="utf-8")) for p in copies]
    first = lambda key: next((str(f[key]) for f in fronts if f.get(key)), "")
    return {"created": min((utc(str(f.get("created", ""))) for f in fronts if utc(str(f.get("created", "")))), default=""),
            "published": utc(first("published")), "copies": [str(p) for p in copies]}


def write_record(root: Path, **fields) -> None:
    """The episode's processing record, outside the notes folder: what made the note and when."""
    path = root / "note-record.json"
    record = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    record.update(fields)
    atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2))


def note_path(root: Path) -> Path:
    """<podcasts folder>/<show folder>/<date> - <episode title>.md; the folder name comes from the show registry."""
    metadata = json.loads((root / "source" / "resolved-episode-v1.json").read_text(encoding="utf-8"))
    entry = podshows.show_for(show_key(metadata)[0])
    folder = (entry or {}).get("folder") or podshows.folder_name(str(metadata.get("show", "")))
    title = re.sub(r'[\\/:*?"<>|#^\[\]]', "", f"{metadata.get('published') or 'undated'} - {metadata.get('episode', '')}")
    return ROOT / EPISODES / folder / (re.sub(r"\s+", " ", title).strip()[:150] + ".md")


def register(root: Path, note: Path | None = None) -> None:
    """Enter the show in the registry (before the note: it fixes the folder), and after the note let
    the registry learn from it. A registry problem never costs the note."""
    try:
        metadata = json.loads((root / "source" / "resolved-episode-v1.json").read_text(encoding="utf-8"))
        show_id, country = show_key(metadata)
        if not show_id:
            return
        if note is None:
            with podshows.editing() as registry:
                podshows.ensure_show(registry, show_id, str(metadata.get("show", "")), country, metadata)
            return
        podshows.record_episode(show_id, str(metadata.get("show", "")), country, metadata,
                                sponsors_in(str(metadata.get("episode_description") or "")))
        podshows.render()
    except Exception as error:                            # the note is what matters; report and go on
        log(f"show registry not updated: {type(error).__name__}: {error}")


def main() -> int:
    global MODEL
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?")
    parser.add_argument("--job-dir")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retranscribe", action="store_true",
                        help="with a URL: set the earlier transcript and model output aside and transcribe again")
    parser.add_argument("--model", default=MODEL, help=f"oMLX model id for this run (default {MODEL})")
    args = parser.parse_args()
    MODEL = args.model
    if bool(args.url) == bool(args.job_dir):
        parser.error("give either an Apple Podcasts episode URL or --job-dir")

    try:
        return run(args)
    except Stop as error:
        print(f"STOPPED: {error}")
        return 1
    except RuntimeError as error:          # transcription, state kernel and model-server errors
        print(f"STOPPED: {error}")
        return 1
    except KeyboardInterrupt:
        print("STOPPED: interrupted")
        return 1
    except requests.exceptions.RequestException as error:
        print(f"STOPPED: model server or network unreachable: {error}")
        return 1
    except OSError as error:
        print(f"STOPPED: {error}")
        return 1


def run(args: argparse.Namespace) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "podnote.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("STOPPED: another podnote run is active")
            return 1
        root = Path(args.job_dir) if args.job_dir else front_half(args.url, retranscribe=args.retranscribe)
        register(root)
        target = note_path(root)
        if target.exists() and not args.force:
            print(f"note already exists (use --force to overwrite): {target}")
            return 0

        origin = provenance(root)
        if target.is_file() and str(target) not in origin["copies"]:  # another episode under the same name
            target = target.with_name(f"{target.stem} ({json.loads((root / 'source' / 'resolved-episode-v1.json').read_text(encoding='utf-8'))['apple_episode_id']}).md")
        if os.environ.get("PODNOTE_BASE_URL"):     # any OpenAI-compatible endpoint; no model load/unload
            note, problems = synthesise(root, os.environ["PODNOTE_BASE_URL"].rstrip("/"),
                                        {"Authorization": f"Bearer {os.environ.get('PODNOTE_API_KEY', '')}"}, origin)
        else:                                      # local oMLX: the arbiter loads MODEL and always unloads it
            import omlx_client
            from model_arbiter import local_model_session
            with local_model_session(root.name, "podnote", state_root=CACHE / "model-arbiter",
                                     model=MODEL, operation="podnote") as config:
                note, problems = synthesise(root, config["base_url"], omlx_client.headers(config), origin)

        if problems:                               # any failed part fails the note; the old one stays
            write_record(root, failed=problems)
            print("STOPPED: note incomplete: " + "; ".join(problems))
            return 1
        issues = validate(note)
        if issues:                                 # the check decides; nothing invalid is written
            write_record(root, failed=issues)
            print("STOPPED: note fails the metadata check: " + ", ".join(issues))
            return 1
        unchanged = lambda text: re.sub(r'(?m)^updated: .*$', "", text)
        if not (target.is_file() and unchanged(target.read_text(encoding="utf-8")) == unchanged(note)):
            atomic_write(target, note)             # a re-make that changes nothing leaves the note alone
            if target.read_text(encoding="utf-8") != note:
                print("STOPPED: read-back mismatch after write")
                return 1
        for earlier in (Path(p) for p in origin["copies"] if Path(p) != target and Path(p).is_file()):
            (STATE / "superseded-notes").mkdir(parents=True, exist_ok=True)   # one note per episode
            os.replace(earlier, STATE / "superseded-notes" / f"{earlier.stem} {time.strftime('%Y%m%d-%H%M%S')}.md")
            print(f"SUPERSEDED: {earlier.name}")
        write_record(root, skill_version=SKILL_VERSION, written=utc_now(), note=str(target), failed=[],
                     note_sha256=hashlib.sha256(target.read_text(encoding="utf-8").encode()).hexdigest())
        register(root, target)
        print(f"NOTE: {target}")
        print("STATUS: complete")
        return 0


if __name__ == "__main__":
    sys.exit(main())
