"""Episode sources: the services an episode link can come from, behind one small interface.

A connector module (apple.py, youtube.py) has:

    NAME, ID_FIELD, JOB_PREFIX, HOSTS   "apple", "apple_episode_id", "podcast", {"podcasts.apple.com"}
    LABEL, ICON                         the header link in a note: "🎧 [Apple Podcasts](…)"
    parse(url) -> Ref                   the episode (or show) a link names; ValueError for anything else
    resolve(ref, cache_dir)             -> (record, credits): the episode's record, written once into its
                                        job, and the facts refreshed on every run. SourceError when the
                                        episode cannot be had now (retry=True when a later try may work:
                                        the queue retries those)
    fetch_audio(ref, metadata, metadata_path, audio, log)   the audio into `audio`, through the skill's
                                        own guarded downloader
    peek(ref) -> dict | None            {show, title, published, seconds, source} without downloading anything (None when
                                        it cannot be had now), so a link can be checked for a twin

The job folder, the note's id field and the source's identity in the ASR ledger all come from here,
so the rest of the pipeline never looks inside a link. One episode published on two sources (Apple
and YouTube) is one episode: match(), twin_among() and twin_note() recognise it by show and title within
two days, or, titled differently, by publish time within minutes and length.
"""
from __future__ import annotations

import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

TWIN_WINDOW = timedelta(hours=48)          # two sources' copies of one episode go out within this
CLOSE_WINDOW = timedelta(minutes=10)       # ...and within this when they are titled differently


class SourceError(Exception):
    """The episode cannot be resolved or fetched; the message says why, in plain words. retry: a later
    try may work (YouTube's bot check, a premiere not started, a server error, Apple not indexed yet);
    otherwise it needs someone to act (a removed video, yt-dlp missing) or never will."""

    def __init__(self, message: str, retry: bool = False):
        super().__init__(message)
        self.retry = retry


@dataclass(frozen=True)
class Ref:
    source: str          # the connector's NAME
    episode: str         # the source's episode id; "" for a show link
    show: str = ""       # the source's show id, when the link names it (Apple)
    country: str = ""
    url: str = ""        # the link recorded as the note's source_url

    @property
    def connector(self):
        return CONNECTORS[self.source]

    @property
    def job_id(self) -> str:
        return f"{self.connector.JOB_PREFIX}-{self.episode}"

    @property
    def id_field(self) -> str:
        return self.connector.ID_FIELD


def utc_text(moment: datetime | None) -> str:
    """An instant as the notes' UTC form YYYY-MM-DDTHH:MM:SS (naive means UTC); "" for none."""
    if not moment:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def save_record(path: Path, record: dict) -> None:
    """A job's episode record, written atomically (the form podnote reads)."""
    import json
    from files import atomic_write
    atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2))


def parse(url: str) -> Ref:
    """The episode or show a link names, by the connector for its host."""
    url = url.strip()
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for connector in CONNECTORS.values():
        if host in connector.HOSTS:
            return connector.parse(url)
    raise ValueError(f"not an Apple Podcasts or YouTube link: {url}")


def episode_ref(url: str) -> Ref:
    """The episode a link names; ValueError for a show link or a link no connector takes. Its url is the
    clean link the connector builds from the ids alone, so share and tracking parameters ("?si=", "?is=",
    "utm_…") never reach a source, the queue or a log."""
    ref = parse(url)
    if not ref.episode:
        raise ValueError(ref.connector.SHOW_LINK_REFUSAL)
    return ref


def id_fields() -> list[str]:
    return [connector.ID_FIELD for connector in CONNECTORS.values()]


def note_ref(text: str) -> Ref | None:
    """The episode a note (or its first lines) is about, from its source id property."""
    for connector in CONNECTORS.values():
        found = re.search(rf"(?m)^{connector.ID_FIELD}: ['\"]?([\w-]+)", text)
        if found:
            return Ref(connector.NAME, found.group(1))
    return None


def metadata_ref(metadata: dict) -> Ref | None:
    """The episode a job's record is about, from its source id field."""
    for connector in CONNECTORS.values():
        if metadata.get(connector.ID_FIELD):
            return Ref(connector.NAME, str(metadata[connector.ID_FIELD]), url=str(metadata.get("source_url") or ""))
    return None


def job_root(cache: Path, text: str) -> Path | None:
    """The job folder of the episode a note is about (it may no longer exist)."""
    ref = note_ref(text)
    return cache / "jobs" / ref.job_id if ref else None


def job_prefixes() -> list[str]:
    return [f"{connector.JOB_PREFIX}-" for connector in CONNECTORS.values()]


# ---------------------------------------------------------------- one episode on two sources

def norm_title(title: str, show: str = "") -> str:
    """A title without case, accents, punctuation or the show's own name before or after it."""
    def norm(value: str) -> str:
        value = unicodedata.normalize("NFKD", value or "")
        value = "".join(char for char in value if not unicodedata.combining(char)).lower()
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value)).strip()
    text, name = norm(title), norm(show)
    if name and text != name:
        text = re.sub(rf"^{re.escape(name)}\s+|\s+{re.escape(name)}$", "", text).strip()
    return text


def instant(text: str) -> datetime | None:
    """A UTC instant or a bare date from a note or a record; None when there is neither."""
    text = str(text or "").strip().strip("'\"").replace("Z", "")
    for form, size in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:size], form)
        except ValueError:
            continue
    return None


def exact(text: str) -> datetime | None:
    """An instant with its time of day; None for a bare date or nothing."""
    return instant(text) if re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d", str(text or "")) else None


def similar_length(a, b) -> bool:
    """Two lengths in seconds within 10% or 2 minutes of each other (one copy has the ads, one has not)."""
    a, b = float(a or 0), float(b or 0)
    return a > 0 and b > 0 and abs(a - b) <= max(120.0, 0.10 * max(a, b))


def episode(show: str, source: str, title: str, published: str = "", seconds: float = 0,
            description: str = "", language_hint: str = "", **more) -> dict:
    """One episode's facts as every part of the skill passes them (a "peek", a twin candidate, a note's
    record): the registry key of its show, the source it comes from (a connector NAME), its title, its
    release in the notes' UTC form (an instant, or a day alone when that is all the source gives), its
    length in whole seconds, its description and the source's language tag. A new source builds this and
    the queue, the monitor, search and the twin rules work with it unchanged. more: extra keys a caller
    keeps alongside (a note record's `id`)."""
    return {"show": show, "source": source, "title": title, "published": published or "",
            "seconds": round(float(seconds or 0)), "description": description or "",
            "language_hint": language_hint or "", **more}


def match(a: dict, b: dict, show_title: str = "") -> str:
    """How two {show, title, published, seconds, source} records name one episode:

    "title": the same show, the same title once normalised, published within TWIN_WINDOW. Equal titles,
             not similar ones: "Part 1" and "Part 2" of a series are two episodes.
    "time":  the same show from two different sources, published at exact times within CLOSE_WINDOW,
             with similar lengths: a show that titles its YouTube uploads its own way.
    "":      not one episode. Two copies from one source are never matched on time alone: a show may
             release two episodes in one minute. Two records of one source with different ids (`id`, the
             source's episode id, when a record carries it) are two episodes, whatever their titles."""
    if not (a.get("show") and a.get("show") == b.get("show")):
        return ""
    if a.get("source") and a.get("source") == b.get("source") and a.get("id") and b.get("id") and a["id"] != b["id"]:
        return ""
    when_a, when_b = instant(a.get("published", "")), instant(b.get("published", ""))
    if not (when_a and when_b):
        return ""
    first, second = norm_title(a.get("title", ""), show_title), norm_title(b.get("title", ""), show_title)
    if first and first == second and abs(when_a - when_b) <= TWIN_WINDOW:
        return "title"
    timed_a, timed_b = exact(a.get("published", "")), exact(b.get("published", ""))
    if (a.get("source") and b.get("source") and a["source"] != b["source"] and timed_a and timed_b
            and abs(timed_a - timed_b) <= CLOSE_WINDOW and similar_length(a.get("seconds"), b.get("seconds"))):
        return "time"
    return ""


def twin_among(record: dict, candidates: list[tuple], show_title: str = ""):
    """The key of the only candidate (key, record) that is the same episode: by title, else by time and
    length. Two candidates of one kind and neither is taken: the episode is made. A doubt costs a
    second note. A lost episode would cost more."""
    kinds = [(key, match(record, other, show_title)) for key, other in candidates]
    titled = [key for key, kind in kinds if kind == "title"]
    timed = [key for key, kind in kinds if kind == "time"]
    return titled[0] if len(titled) == 1 else None if titled else timed[0] if len(timed) == 1 else None


def note_records(folder: Path, show: str, other_than: str = "") -> list[tuple[Path, dict]]:
    """The notes in a show's folder as (path, record) for match(): title, `published` (else the date the
    file name starts with), `duration`, the source whose id the note carries and that id. other_than: a source id
    property; notes carrying it are that source's own and are passed over."""
    import notecheck
    records = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            front = notecheck.parse_note(path.read_text(encoding="utf-8", errors="replace"))[0] or {}
        except Exception:
            continue
        if other_than and front.get(other_than):
            continue
        source = next((name for name, connector in CONNECTORS.items() if front.get(connector.ID_FIELD)), "")
        records.append((path, episode(show, source, str(front.get("episode") or ""),
                                      str(front.get("published") or "") or path.name[:10], front.get("duration") or 0,
                                      id=str(front.get(CONNECTORS[source].ID_FIELD)) if source else "")))
    return records


def twin_note(folder: Path, record: dict, show_title: str = "", other_than: str = "") -> Path | None:
    """A note in a show's folder for the episode this record names (see match and twin_among)."""
    return twin_among(record, note_records(folder, record.get("show", ""), other_than), show_title)


from sources import apple, youtube  # noqa: E402  (they use Ref and SourceError above)

CONNECTORS = {connector.NAME: connector for connector in (apple, youtube)}
