#!/usr/bin/env python3
"""Resolve Apple Podcasts links to RSS enclosure and transcript URLs.

The RSS feed is the audio contract. Apple supplies catalogue metadata and the
show's feed URL; the resolver never scrapes HTML for audio URLs.

Stdlib only: urllib + ElementTree.
"""

from __future__ import annotations

import difflib
import hashlib
import html
import json
import os
import re
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

USER_AGENT = "podfetch/2.0 (+personal transcription workflow)"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
APPLE_HOST = "podcasts.apple.com"
MAX_HTTP_BYTES = 32 * 1024 * 1024
APPLE_EPISODE_LIMIT = 200


class AmbiguousEpisodeError(LookupError):
    """Raised when title/date matching cannot identify one safe winner."""


@dataclass
class Transcript:
    url: str
    type: str = ""
    language: str = ""


@dataclass
class Episode:
    guid: str = ""
    title: str = ""
    published: datetime | None = None
    enclosure_url: str = ""
    enclosure_type: str = ""
    enclosure_length: int = 0
    duration: str = ""
    episode_number: str = ""
    season: str = ""
    link: str = ""
    description: str = ""
    transcripts: list[Transcript] = field(default_factory=list)
    match_method: str = ""
    author: str = ""                                    # itunes:author of the episode, if any
    people: list[dict] = field(default_factory=list)    # podcast:person tags: {"name", "role"}

    @property
    def best_transcript(self) -> Transcript | None:
        """Prefer structured/machine-readable transcript formats."""
        order = [
            "application/json",
            "text/vtt",
            "application/x-subrip",
            "text/srt",
            "text/html",
            "text/plain",
        ]
        for wanted in order:
            for transcript in self.transcripts:
                if transcript.type.lower().startswith(wanted):
                    return transcript
        return self.transcripts[0] if self.transcripts else None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["published"] = self.published.isoformat() if self.published else None
        return data


@dataclass
class Show:
    title: str = ""
    feed_url: str = ""
    episodes: list[Episode] = field(default_factory=list)
    author: str = ""                                    # itunes:author (Apple shows it as the show's artist)
    owner: str = ""                                     # itunes:owner/itunes:name
    people: list[dict] = field(default_factory=list)    # channel podcast:person tags: {"name", "role"}


@dataclass
class AppleEpisodeHint:
    track_id: str
    title: str = ""
    published: datetime | None = None
    episode_guid: str = ""
    episode_url: str = ""
    duration_seconds: float | None = None


# ---------------------------------------------------------------- fetching


def _validate_http_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Unsupported URL: {url}")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in URLs are not allowed")


def http_get(
    url: str,
    etag: str = "",
    modified: str = "",
    timeout: int = 30,
    max_bytes: int = MAX_HTTP_BYTES,
):
    """GET with conditional-request and response-size support.

    Returns ``(status, body, headers)``. HTTP 304 is returned normally.
    """
    _validate_http_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if etag:
        request.add_header("If-None-Match", etag)
    if modified:
        request.add_header("If-Modified-Since", modified)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise ValueError(f"Response exceeds {max_bytes} bytes: {url}")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError(f"Response exceeds {max_bytes} bytes: {url}")
            return response.status, body, dict(response.headers)
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return 304, b"", dict(error.headers)
        raise


def probe_audio_url(url: str, timeout: int = 20) -> dict:
    """Follow redirects and inspect a small media prefix without downloading it."""
    _validate_http_url(url)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Range": "bytes=0-63"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        prefix = response.read(64)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        media_signature = (
            prefix.startswith((b"ID3", b"RIFF", b"OggS", b"fLaC"))
            or (len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0)
            or (len(prefix) >= 12 and prefix[4:8] == b"ftyp")
        )
        return {
            "status": response.status,
            "final_url": response.geturl(),
            "content_type": content_type,
            "content_range": response.headers.get("Content-Range"),
            "audio_verified": content_type.startswith("audio/") or media_signature,
        }


def storefronts(country: str) -> list[str]:
    """Apple catalogues to ask, in order: the US one first (other storefronts lag it by hours or
    days), then the link's own."""
    return list(dict.fromkeys(["us", (country or "us").lower()]))


def resolve_feed_url(collection_id: str | int, country: str = "us") -> str:
    """Resolve one Apple show ID to its public RSS feed URL."""
    results = []
    for store in storefronts(country):
        query = urllib.parse.urlencode(
            {"id": str(collection_id), "entity": "podcast", "country": store}
        )
        status, body, _ = http_get(f"{ITUNES_LOOKUP}?{query}")
        if status != 200:
            raise LookupError(f"Apple lookup returned HTTP {status}")
        results = [
            row
            for row in json.loads(body).get("results", [])
            if str(row.get("collectionId", "")) == str(collection_id)
        ]
        if results:
            break
    if not results:
        raise LookupError(f"No Apple result for collection id {collection_id} ({country})")
    feed_url = results[0].get("feedUrl")
    if not feed_url:
        raise LookupError(f"Show {collection_id} has no public RSS feed")
    _validate_http_url(feed_url)
    return feed_url


def _atomic_bytes(path, body: bytes) -> None:
    """Write a disposable resolver-cache entry atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def resolve_feed_url_cached(
    collection_id: str | int,
    country: str,
    cache_dir,
    max_age_days: int = 7,
) -> str:
    """Resolve a show feed with a bounded, non-authoritative JSON cache."""
    from pathlib import Path

    path = Path(cache_dir) / "show-feeds.json"
    entries = {}
    if path.exists():
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            entries = {}
    key = f"{country}:{collection_id}"
    cached = entries.get(key, {})
    try:
        resolved_at = datetime.fromisoformat(str(cached.get("resolved_at", "")).replace("Z", "+00:00"))
    except ValueError:
        resolved_at = None
    if (
        cached.get("feed_url")
        and resolved_at is not None
        and datetime.now(timezone.utc) - resolved_at <= timedelta(days=max_age_days)
    ):
        _validate_http_url(cached["feed_url"])
        return cached["feed_url"]
    feed_url = resolve_feed_url(collection_id, country)
    entries[key] = {
        "feed_url": feed_url,
        "resolved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    _atomic_bytes(path, (json.dumps(entries, indent=2, sort_keys=True) + "\n").encode())
    return feed_url


def fetch_feed_cached(feed_url: str, cache_dir, *, force_refresh: bool = False) -> bytes:
    """Fetch RSS with ETag/Last-Modified and reuse bytes only after HTTP 304."""
    from pathlib import Path

    root = Path(cache_dir) / "feeds"
    key = hashlib.sha256(feed_url.encode()).hexdigest()
    body_path = root / f"{key}.xml"
    meta_path = root / f"{key}.json"
    metadata = {}
    if not force_refresh and body_path.exists() and meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            metadata = {}
    status, body, headers = http_get(
        feed_url,
        etag=str(metadata.get("etag") or ""),
        modified=str(metadata.get("last_modified") or ""),
    )
    if status == 304:
        if not body_path.exists():
            raise ValueError("Feed returned 304 but no cached body exists")
        return body_path.read_bytes()
    if status != 200:
        raise LookupError(f"Feed returned HTTP {status}")
    _atomic_bytes(body_path, body)
    cache_metadata = {
        "feed_url": feed_url,
        "etag": headers.get("ETag") or headers.get("Etag") or "",
        "last_modified": headers.get("Last-Modified") or "",
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    _atomic_bytes(meta_path, (json.dumps(cache_metadata, indent=2) + "\n").encode())
    return body


def enclosure_needs_refresh(
    resolved_at: datetime,
    response_status: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Refresh signed/wrapped enclosures after one day or verified expiry."""
    if response_status in {403, 404}:
        return True
    if resolved_at.tzinfo is None:
        resolved_at = resolved_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current - resolved_at >= timedelta(days=1)


def refresh_enclosure_if_needed(
    apple_url: str,
    resolved_at: datetime,
    response_status: int | None = None,
    cache_dir=None,
    now: datetime | None = None,
) -> Episode | None:
    """Re-read Apple/RSS metadata when an enclosure is stale or expired."""
    if not enclosure_needs_refresh(resolved_at, response_status, now):
        return None
    _, episode = resolve_apple_url(
        apple_url,
        feed_cache={},
        show_cache={},
        cache_dir=cache_dir,
        force_feed_refresh=True,
    )
    if episode is None:
        raise LookupError("Cannot refresh an enclosure from a show-level Apple URL")
    return episode


def episode_hint_from_apple(
    collection_id: str | int,
    episode_id: str | int,
    country: str = "us",
    limit: int = APPLE_EPISODE_LIMIT,
) -> AppleEpisodeHint | None:
    """The episode as the US catalogue lists it, else as the link's storefront does."""
    for store in storefronts(country):
        found = _episode_hint(collection_id, episode_id, store, limit)
        if found is not None:
            return found
    return None


def _episode_hint(
    collection_id: str | int,
    episode_id: str | int,
    country: str,
    limit: int,
) -> AppleEpisodeHint | None:
    """List a show's Apple episodes and select exact ``trackId``.

    Apple episode IDs are not RSS GUIDs. When Apple exposes ``episodeGuid``, it
    is carried separately and can be matched exactly against the feed.
    """
    query = urllib.parse.urlencode(
        {
            "id": str(collection_id),
            "entity": "podcastEpisode",
            "country": country,
            "limit": str(limit),
        }
    )
    _, body, _ = http_get(f"{ITUNES_LOOKUP}?{query}")
    for row in json.loads(body).get("results", []):
        if str(row.get("trackId", "")) != str(episode_id):
            continue
        published = None
        if row.get("releaseDate"):
            try:
                published = datetime.fromisoformat(
                    row["releaseDate"].replace("Z", "+00:00")
                )
            except ValueError:
                pass
        duration = row.get("trackTimeMillis")
        return AppleEpisodeHint(
            track_id=str(episode_id),
            title=row.get("trackName", ""),
            published=published,
            episode_guid=str(row.get("episodeGuid") or ""),
            episode_url=str(row.get("episodeUrl") or ""),
            duration_seconds=(float(duration) / 1000.0 if duration else None),
        )
    return None


# ---------------------------------------------------------------- parsing


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element) -> str:
    return (element.text or "").strip() if element is not None else ""


def _plain_text(value: str, maximum: int = 8_000) -> str:
    """Reduce bounded RSS HTML to compact publisher context."""
    text = html.unescape(value or "")
    text = re.sub(r"(?is)<(?:script|style).*?>.*?</(?:script|style)>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:maximum]


def _first_child_by_local(parent, local_name: str):
    for child in parent:
        if _local(child.tag) == local_name:
            return child
    return None


def parse_feed(xml_bytes: bytes, feed_url: str = "") -> Show:
    """Parse a podcast RSS feed using namespace-local element names."""
    if len(xml_bytes) > MAX_HTTP_BYTES:
        raise ValueError("Feed exceeds parser size limit")
    root = ET.fromstring(xml_bytes)
    channel = root if _local(root.tag) == "channel" else _first_child_by_local(root, "channel")
    if channel is None:
        raise ValueError("Not an RSS feed (no <channel>)")

    show = Show(feed_url=feed_url)
    for child in channel:
        name = _local(child.tag)
        if name == "title" and not show.title:
            show.title = _text(child)
        elif name == "author" and not show.author:
            show.author = _text(child)
        elif name == "owner" and not show.owner:
            owner_name = _first_child_by_local(child, "name")
            show.owner = _text(owner_name) if owner_name is not None else ""
        elif name == "person" and _text(child):
            show.people.append(_person(child))
        elif name == "item":
            show.episodes.append(_parse_item(child))
    return show


def _person(element) -> dict:
    """A podcast:person tag (Podcasting 2.0); the spec's default role is host."""
    return {"name": _text(element), "role": (element.get("role") or "host").strip().lower()}


def _parse_item(item) -> Episode:
    episode = Episode()
    for element in item:
        name = _local(element.tag)
        if name == "title" and not episode.title:
            episode.title = _text(element)
        elif name == "guid":
            episode.guid = _text(element)
        elif name == "pubDate":
            try:
                episode.published = parsedate_to_datetime(_text(element))
                if episode.published.tzinfo is None:
                    episode.published = episode.published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass
        elif name == "enclosure":
            episode.enclosure_url = element.get("url", "")
            episode.enclosure_type = element.get("type", "")
            try:
                episode.enclosure_length = int(element.get("length") or 0)
            except ValueError:
                episode.enclosure_length = 0
        elif name == "duration" and not episode.duration:
            episode.duration = _text(element)
        elif name == "episode" and not episode.episode_number:
            episode.episode_number = _text(element)
        elif name == "season" and not episode.season:
            episode.season = _text(element)
        elif name == "link" and not episode.link:
            episode.link = _text(element)
        elif name in {"description", "summary", "encoded"} and not episode.description:
            episode.description = _plain_text(_text(element))
        elif name == "author" and not episode.author:
            episode.author = _text(element)
        elif name == "person" and _text(element):
            episode.people.append(_person(element))
        elif name == "transcript":
            url = element.get("url", "")
            if url:
                episode.transcripts.append(
                    Transcript(
                        url=url,
                        type=element.get("type", ""),
                        language=element.get("language", ""),
                    )
                )
    if not episode.guid:
        episode.guid = episode.enclosure_url
    return episode


# ---------------------------------------------------------------- matching


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^\w\s]", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def _duration_seconds(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    parts = value.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return None


def match_episode(
    episodes: list[Episode],
    guid: str = "",
    enclosure_url: str = "",
    title: str = "",
    published: datetime | None = None,
    duration_seconds: float | None = None,
    min_ratio: float = 0.72,
    date_tolerance_days: int = 3,
    ambiguity_margin: float = 0.06,
) -> Episode | None:
    """Match exact GUID first, then title/date/duration with ambiguity rejection."""
    if guid:
        for episode in episodes:
            if episode.guid.strip() == guid.strip():
                episode.match_method = "apple_episode_guid"
                return episode

    if enclosure_url:
        for episode in episodes:
            if episode.enclosure_url.strip() == enclosure_url.strip():
                episode.match_method = "apple_episode_url"
                return episode

    if not title:
        return None

    wanted = _norm(title)
    scored: list[tuple[float, Episode]] = []
    for episode in episodes:
        score = difflib.SequenceMatcher(None, wanted, _norm(episode.title)).ratio()
        if published and episode.published:
            delta_days = abs((episode.published - published).total_seconds()) / 86400.0
            if delta_days <= 1.5:
                score += 0.20
            elif delta_days <= date_tolerance_days:
                score += 0.12
            elif delta_days > 30:
                score -= 0.30
        rss_duration = _duration_seconds(episode.duration)
        if duration_seconds and rss_duration:
            difference = abs(rss_duration - duration_seconds)
            if difference <= max(60.0, duration_seconds * 0.05):
                score += 0.10
            elif difference > max(300.0, duration_seconds * 0.20):
                score -= 0.15
        scored.append((score, episode))

    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored or scored[0][0] < min_ratio:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < ambiguity_margin:
        raise AmbiguousEpisodeError(
            f"Ambiguous episode match: {scored[0][1].title!r} ({scored[0][0]:.3f}) "
            f"vs {scored[1][1].title!r} ({scored[1][0]:.3f})"
        )
    winner = scored[0][1]
    winner.match_method = "title_date_duration"
    return winner


# --------------------------------------------------------------- Apple URL


APPLE_RE = re.compile(r"/id(\d+)(?:/|$)")


def parse_apple_url(url: str) -> tuple[str, str, str]:
    """Return ``(collection_id, episode_id, storefront)``."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != APPLE_HOST:
        raise ValueError(f"Not an Apple Podcasts URL: {url}")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in Apple URLs are not allowed")
    match = APPLE_RE.search(parsed.path)
    if not match:
        raise ValueError(f"Apple Podcasts URL has no show ID: {url}")
    collection_id = match.group(1)
    episode_id = urllib.parse.parse_qs(parsed.query).get("i", [""])[0]
    if episode_id and not episode_id.isdigit():
        raise ValueError(f"Invalid Apple episode ID: {episode_id}")
    parts = [part for part in parsed.path.split("/") if part]
    storefront = parts[0].lower() if parts and re.fullmatch(r"[A-Za-z]{2}", parts[0]) else "us"
    return collection_id, episode_id, storefront


def resolve_apple_url(
    url: str,
    feed_cache: dict[str, str] | None = None,
    show_cache: dict[str, Show] | None = None,
    cache_dir=None,
    force_feed_refresh: bool = False,
) -> tuple[Show, Episode | None]:
    """Resolve an Apple show/episode URL using reusable in-process caches."""
    collection_id, episode_id, storefront = parse_apple_url(url)
    feeds = feed_cache if feed_cache is not None else {}
    shows = show_cache if show_cache is not None else {}

    feed_url = feeds.get(collection_id)
    if not feed_url:
        feed_url = (
            resolve_feed_url_cached(collection_id, storefront, cache_dir)
            if cache_dir is not None
            else resolve_feed_url(collection_id, storefront)
        )
        feeds[collection_id] = feed_url

    show = None if force_feed_refresh else shows.get(feed_url)
    if show is None:
        if cache_dir is not None:
            body = fetch_feed_cached(feed_url, cache_dir)
        else:
            _, body, _ = http_get(feed_url)
        show = parse_feed(body, feed_url)
        shows[feed_url] = show

    if not episode_id:
        return show, None

    hint = episode_hint_from_apple(collection_id, episode_id, storefront)
    if hint is None:
        raise LookupError(
            f"Apple episode {episode_id} was not returned for show {collection_id}; "
            "the episode may be newly indexed or older than Apple's lookup window"
        )
    episode = match_episode(
        show.episodes,
        guid=hint.episode_guid,
        enclosure_url=hint.episode_url,
        title=hint.title,
        published=hint.published,
        duration_seconds=hint.duration_seconds,
    )
    if episode is None and cache_dir is not None:
        # Some podcast hosts return a stale 304 immediately after publishing.
        # Retry once without conditional headers and replace the disposable cache.
        body = fetch_feed_cached(feed_url, cache_dir, force_refresh=True)
        show = parse_feed(body, feed_url)
        shows[feed_url] = show
        episode = match_episode(
            show.episodes,
            guid=hint.episode_guid,
            enclosure_url=hint.episode_url,
            title=hint.title,
            published=hint.published,
            duration_seconds=hint.duration_seconds,
        )
    if episode is None:
        raise LookupError(
            f"Apple episode {episode_id} ({hint.title!r}) did not match the RSS feed"
        )
    return show, episode


# --------------------------------------------------------------------- CLI


def result_dict(url: str, show: Show, episode: Episode | None) -> dict:
    collection_id, episode_id, storefront = parse_apple_url(url)
    base = {
        "input": url,
        "collection_id": collection_id,
        "apple_episode_id": episode_id or None,
        "storefront": storefront,
        "show": show.title,
        "feed": show.feed_url,
    }
    if episode is None:
        return {
            **base,
            "episode_count": len(show.episodes),
            "action": "show_resolved",
        }
    transcript = episode.best_transcript
    return {
        **base,
        "title": episode.title,
        "guid": episode.guid,
        "published": episode.published.isoformat() if episode.published else None,
        "audio_url": episode.enclosure_url,
        "audio_type": episode.enclosure_type,
        "duration": episode.duration,
        "transcript_url": transcript.url if transcript else None,
        "transcript_type": transcript.type if transcript else None,
        "match_method": episode.match_method,
        "action": "use_transcript" if transcript else "transcribe_audio",
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: podfetch.py <apple-podcasts-url> [...]")
        return 2
    feed_cache: dict[str, str] = {}
    show_cache: dict[str, Show] = {}
    failures = 0
    for url in argv[1:]:
        try:
            show, episode = resolve_apple_url(url, feed_cache, show_cache)
            output = result_dict(url, show, episode)
        except Exception as error:
            failures += 1
            output = {
                "input": url,
                "error": str(error),
                "error_type": type(error).__name__,
            }
        print(json.dumps(output, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
