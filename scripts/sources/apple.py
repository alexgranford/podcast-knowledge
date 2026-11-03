"""Apple Podcasts: an episode link (podcasts.apple.com/<storefront>/podcast/<name>/id<show>?i=<episode>),
resolved through Apple's catalogue to the show's RSS feed, whose enclosure is the audio (podfetch.py)."""
from __future__ import annotations

import re
from pathlib import Path

import podfetch
from sources import Ref, SourceError, episode, save_record, utc_text

NAME, ID_FIELD, JOB_PREFIX, LABEL, ICON = "apple", "apple_episode_id", "podcast", "Apple Podcasts", "🎧"
HOSTS = {podfetch.APPLE_HOST}
SHOW_LINK_REFUSAL = "this is a show link, not an episode link (it has no ?i=<episode-id>)"


def episode_link(show: str, episode: str = "", country: str = "us") -> str:
    """The clean link of an Apple show or episode: the storefront and the ids only."""
    return f"https://podcasts.apple.com/{country}/podcast/id{show}" + (f"?i={episode}" if episode else "")


def parse(url: str) -> Ref:
    """The episode (or show) an Apple Podcasts link names, as a clean link: the storefront, the show id
    and the episode id only; the title slug and any tracking parameters are dropped."""
    show, episode, country = podfetch.parse_apple_url(url)
    return Ref(NAME, episode, show, country, episode_link(show, episode, country))


def lookup_error(error: LookupError) -> SourceError:
    """What a failed catalogue or feed lookup means. Apple lists a new episode some time after it is
    published, and a server error passes: both are tried again later. A feed that is gone, a show with no
    public feed or an episode that cannot be told apart from another is not, and says so as it is."""
    text = str(error)
    if isinstance(error, podfetch.AppleLookupError):      # Apple's service, not the episode: it passes
        return SourceError(f"{text}; try again later", retry=True)
    if re.search(r"was not returned for show|did not match the RSS feed", text):
        return SourceError(f"{text}. Apple has not indexed this episode yet; try again later.", retry=True)
    if re.search(r"HTTP (429|5\d\d)\b", text):
        return SourceError(f"{text}; try again later", retry=True)
    return SourceError(text)


def resolve(ref: Ref, cache_dir: Path) -> tuple[dict, dict]:
    """The episode from Apple's catalogue and the show's feed. A new episode is indexed by Apple some
    time after it is published; nothing here can fix that, so the message asks for a later try."""
    try:
        show, episode = podfetch.resolve_apple_url(ref.url, cache_dir=cache_dir)
    except LookupError as error:
        raise lookup_error(error) from error
    if episode is None:
        raise SourceError("URL must be an Apple Podcasts episode link containing ?i=<episode-id>")
    record = {
        "source_url": ref.url, "apple_episode_id": ref.episode, "show": show.title, "episode": episode.title,
        "published": episode.published.date().isoformat() if episode.published else None,
        "duration_seconds": round(podfetch.parse_duration(episode.duration) or 0),
        "audio_url": episode.enclosure_url, "episode_description": episode.description or "",
    }
    if show.language:
        record["language_hint"] = show.language              # the feed's <language>
    credits = {"show_author": show.author, "show_owner": show.owner, "show_people": show.people,
               "episode_author": episode.author, "episode_people": episode.people, "feed_url": show.feed_url,
               "apple_show_id": ref.show, "country": ref.country or "gb", "published_utc": utc_text(episode.published)}
    return record, credits


def fetch_audio(ref: Ref, metadata: dict, metadata_path: Path, audio: Path, log, cache_dir: Path) -> None:
    """Download the episode's audio. A link that has expired since it was first resolved (HTTP 403, 404
    or 410: podcast hosts sign their links) is resolved again from the feed once and tried again. Audio
    missing for an episode released less than FRESH_HOURS ago is tried again later: a host can list an
    episode minutes before its audio is served (a 404 ten minutes after release, 2026-10-13)."""
    import audio_acquisition
    try:
        download(ref, metadata, metadata_path, audio, log, cache_dir)
    except audio_acquisition.AudioTransientError:
        raise
    except audio_acquisition.AudioError as error:
        if missing(error) and released_within(metadata, FRESH_HOURS):
            raise SourceError(f"{error}: the episode came out less than {FRESH_HOURS} hours ago and its audio "
                              "may not be served yet; try again later", retry=True) from error
        raise


FRESH_HOURS = 24


def missing(error: Exception) -> bool:
    return bool(re.search(r"\bHTTP (403|404|410)\b", str(error)))


def released_within(metadata: dict, hours: int) -> bool:
    from datetime import datetime, timedelta, timezone
    try:
        released = datetime.fromisoformat(str(metadata.get("published_utc") or ""))
    except ValueError:
        return False
    released = released.replace(tzinfo=released.tzinfo or timezone.utc)
    return datetime.now(timezone.utc) - released < timedelta(hours=hours)


def download(ref: Ref, metadata: dict, metadata_path: Path, audio: Path, log, cache_dir: Path) -> None:
    from datetime import datetime, timezone
    import audio_acquisition
    try:
        audio_acquisition.acquire_audio(metadata["audio_url"], audio)
        return
    except audio_acquisition.AudioError as error:
        if not missing(error):
            raise
        fresh = podfetch.refresh_enclosure_if_needed(ref.url, datetime.now(timezone.utc), 403, cache_dir=cache_dir)
        if fresh is None or not fresh.enclosure_url or fresh.enclosure_url == metadata["audio_url"]:
            raise
    log("the audio link had expired; downloading from the feed's current link")
    metadata["audio_url"] = fresh.enclosure_url
    save_record(metadata_path, metadata)
    audio_acquisition.acquire_audio(metadata["audio_url"], audio)


def peek(ref: Ref) -> dict | None:
    """Show, title and publish time from Apple's catalogue alone (no feed)."""
    try:
        hint = podfetch.episode_hint_from_apple(ref.show, ref.episode, ref.country or "us")
    except Exception:
        return None
    if hint is None:
        return None
    return episode(ref.show, NAME, hint.title, utc_text(hint.published), hint.duration_seconds or 0,
                           hint.description)


def peek_from_feed(item, show: str, language_hint: str = "") -> dict:
    """The episode record for an item of the show's own feed (podfetch's feed episodes)."""
    return episode(show, NAME, item.title, utc_text(item.published) if item.published else "",
                           podfetch.parse_duration(item.duration) or 0, item.description, language_hint)


