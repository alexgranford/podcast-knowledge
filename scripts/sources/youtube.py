"""YouTube: a video link (watch, youtu.be, shorts, live, embed; www., m. and music. hosts), resolved by
yt-dlp (PODNOTE_YTDLP, its own venv) to the video's facts and one audio-only stream.

yt-dlp only resolves. The user's link never reaches it: the video id is taken from the link and a
canonical watch link is built from it. The audio itself is fetched by the skill's guarded downloader
(audio_acquisition: public addresses only, pinned connection, size cap), in 10 MB ranges, because
YouTube throttles one long request to about playback speed. The signed stream link is refused by any
host other than googlevideo.com.

Discovery for show monitoring lists a channel's podcast playlist (when it has one), its Videos tab (no
Shorts, but clips and trailers) and its Streams tab, each video once, through yt-dlp: YouTube's Atom feeds
answered 404 for whole channels on 2026-11-02, a day after working, so they are not used.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from sources import Ref, SourceError, episode, utc_text

NAME, ID_FIELD, JOB_PREFIX, LABEL, ICON = "youtube", "youtube_video_id", "youtube", "YouTube", "▶️"
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
SHOW_LINK_REFUSAL = "this is a channel or playlist link, not a video link"
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
PLAYLIST_ID = re.compile(r"(?:PL|UU|OL)[A-Za-z0-9_-]{10,40}")
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio"
RANGE_BYTES = 10 * 1024 * 1024
RESOLVE_TIMEOUT = 180
MAX_WAIT = 60                          # seconds; the longest wait for a stream to become available
STREAM_HOST = ".googlevideo.com"
SIGNED_IN = {"subscriber_only", "premium_only", "needs_auth"}


def watch_url(video: str) -> str:
    return f"https://www.youtube.com/watch?v={video}"


def parse(url: str) -> Ref:
    """A video link, or a channel / playlist link (Ref.episode ""; Ref.show the channel id, @handle or
    playlist id). Anything else from a YouTube host is refused."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https", "http"} or host not in HOSTS:
        raise ValueError(f"Not a YouTube link: {url}")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in YouTube links are not allowed")
    parts = [part for part in parsed.path.split("/") if part]
    query = urllib.parse.parse_qs(parsed.query)
    if host == "youtu.be":
        video = parts[0] if len(parts) == 1 else ""
    elif parts[:1] == ["watch"] and len(parts) == 1:
        video = query.get("v", [""])[0]
    elif len(parts) == 2 and parts[0] in {"shorts", "live", "embed", "v"}:
        video = parts[1]
    else:
        video = None                                    # not a video path: perhaps a channel or playlist
    if video is not None:
        if not VIDEO_ID.fullmatch(video):
            raise ValueError(f"Not a YouTube video id: {video or '(none)'}")
        return Ref(NAME, video, url=watch_url(video))
    if parts[:1] == ["playlist"] and PLAYLIST_ID.fullmatch(query.get("list", [""])[0]):
        return Ref(NAME, "", show=query["list"][0], url=f"https://www.youtube.com/playlist?list={query['list'][0]}")
    if len(parts) >= 2 and parts[0] == "channel" and CHANNEL_ID.fullmatch(parts[1]):
        return Ref(NAME, "", show=parts[1], url=f"https://www.youtube.com/channel/{parts[1]}")
    if parts and re.fullmatch(r"@[\w.-]{3,30}", parts[0]):
        return Ref(NAME, "", show=parts[0], url=f"https://www.youtube.com/{parts[0]}")
    raise ValueError(f"Not a YouTube video, channel or playlist link: {url}")


# ---------------------------------------------------------------- yt-dlp

# yt-dlp's error lines -> (the reason in plain words, whether a later try may work)
REASONS = [
    (r"confirm you.?re not a bot|HTTP Error 429|Too Many Requests",
     "YouTube's bot check refused the request; try again later", True),
    (r"Premieres? in|live event will begin|will begin in|is_upcoming|has not started",
     "YouTube says this premiere or live stream has not started yet; try again later", True),
    (r"confirm your age|age.restricted|inappropriate for some users",
     "YouTube says this video is age-restricted: it needs a signed-in account, which this skill does not use", False),
    (r"members.only|Join this channel|channel's members",
     "YouTube says this video is for channel members only", False),
    (r"Private video",
     "YouTube says this video is private", False),
    (r"Video unavailable|video is unavailable|has been removed|been terminated|not available in your country|"
     r"blocked it in your country|does not exist",
     "YouTube says this video is unavailable (removed, private or blocked in this country)", False),
    (r"Requested format is not available",
     "YouTube offers no audio-only stream for this video", False),
    (r"Unable to extract|Signature extraction|nsig|n challenge|JS runtime|player response|PO Token",
     "yt-dlp could not read YouTube's page and needs an update; try again later", True),
    (r"Unable to download|Connection|timed out|Temporary failure|Name or service|getaddrinfo|HTTP Error 5\d\d|"
     r"Remote end closed|EOF occurred",
     "YouTube could not be reached; try again later", True),
]


def explain(stderr: str) -> SourceError:
    """yt-dlp's failure as one plain sentence, marked retry when a later try may work."""
    errors = [line for line in stderr.splitlines() if line.startswith("ERROR")] or stderr.strip().splitlines()[-1:]
    text = " ".join(errors)
    for pattern, reason, retry in REASONS:
        if re.search(pattern, text, re.I):
            return SourceError(reason, retry=retry)
    return SourceError("yt-dlp failed: " + (re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[\w-]+:\s*)?", "", errors[0])[:200]
                                            if errors else "no message"))


def ytdlp_program() -> str:
    program = os.environ.get("PODNOTE_YTDLP", "").strip()
    if not program or not Path(program).is_file():
        raise SourceError("YouTube links are switched off here: PODNOTE_YTDLP does not name a yt-dlp program")
    return program


def ytdlp(arguments: list[str], timeout: int = RESOLVE_TIMEOUT) -> dict:
    """One yt-dlp call that prints JSON; SourceError in plain words when it fails."""
    node = shutil.which("node")
    command = [ytdlp_program(), "--ignore-config", "--no-warnings", "--js-runtimes", f"node:{node}" if node else "node",
               "-J", *arguments]
    try:
        done = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise SourceError("YouTube did not answer in time; try again later", retry=True) from error
    if done.returncode:
        raise explain(done.stderr)
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError as error:
        raise SourceError("yt-dlp gave no readable answer; try again later", retry=True) from error


_RESOLVED: dict[str, tuple[float, dict]] = {}         # video id -> (when, facts): resolve, then fetch, in one run


def forget_resolved() -> None:
    """Drop the resolves kept for the hour, so the next one asks YouTube afresh."""
    _RESOLVED.clear()


def video_info(video: str, fresh: bool = False) -> dict:
    """The video's facts and its chosen audio stream; reused within the hour (stream links last ~6 h)."""
    cached = _RESOLVED.get(video)
    if cached and not fresh and time.time() - cached[0] < 3600:
        return cached[1]
    info = ytdlp(["--no-playlist", "--skip-download", "-f", AUDIO_FORMAT, "--", watch_url(video)])
    if info.get("id") != video:
        raise SourceError(f"yt-dlp answered for another video ({info.get('id')!r})")
    if info.get("live_status") in {"is_upcoming", "is_live", "post_live"}:
        raise SourceError("YouTube says this premiere or live stream has not finished; try again later", retry=True)
    if info.get("availability") in SIGNED_IN:
        raise SourceError(f"YouTube says this video needs a signed-in account ({info['availability']})")
    if not info.get("duration"):
        raise SourceError("YouTube gave no duration for this video; try again later", retry=True)
    _RESOLVED[video] = (time.time(), info)
    return info


def published_utc(info: dict) -> str:
    """The upload instant; "" when YouTube gives only a day (never invented)."""
    moment = info.get("release_timestamp") or info.get("timestamp")
    return utc_text(datetime.fromtimestamp(int(moment), timezone.utc)) if moment else ""


def published_day(info: dict) -> str | None:
    if published_utc(info):
        return published_utc(info)[:10]
    day = str(info.get("upload_date") or "")
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}" if re.fullmatch(r"\d{8}", day) else None


CHAPTER_LINE = re.compile(r"(?m)^[ \t]*[\[(]?(?:\d{1,2}:)?\d{1,2}:\d{2}[\])]?[ \t]*(?:[-–—:|][ \t]*)?\S.*$\n?")


def description(info: dict) -> str:
    """The video description without its chapter list, when YouTube gives the chapters as data: the
    timestamped lines are chapter titles, which would otherwise pass for names in the ASR hint."""
    text = str(info.get("description") or "")
    return CHAPTER_LINE.sub("", text).strip() if info.get("chapters") else text.strip()


def show_key(channel: str) -> str:
    """The registry key of the video's show: the show linked to this channel, else yt:<channel>. Every
    video of a linked channel is that show's, whatever list it sits in: there is no telling an episode
    from a special, so none is left out."""
    import podshows
    return podshows.key_for_channel(channel) or f"yt:{channel}"


def resolve(ref: Ref, cache_dir: Path) -> tuple[dict, dict]:
    import podshows
    info = video_info(ref.episode)
    channel = str(info.get("channel_id") or "")
    if not CHANNEL_ID.fullmatch(channel):
        raise SourceError("YouTube gave no channel for this video")
    show = show_key(channel)
    entry = podshows.show_for(show) or {}
    record = {
        "source_url": watch_url(ref.episode), "youtube_video_id": ref.episode,
        "show": entry.get("title") or str(info.get("channel") or info.get("uploader") or ""),
        "episode": str(info.get("title") or ""), "published": published_day(info),
        "duration_seconds": round(float(info["duration"])), "episode_description": description(info),
    }
    chapters = [{"start": round(float(c.get("start_time") or 0)), "title": str(c.get("title") or "")}
                for c in info.get("chapters") or [] if c.get("title")]
    if chapters:
        record["chapters"] = chapters
    if info.get("language"):
        record["language_hint"] = str(info["language"])     # the audio track's language ("en-US")
    credits = {"show_author": str(info.get("channel") or info.get("uploader") or ""), "youtube_channel_id": channel,
               "youtube_show": show, "published_utc": published_utc(info)}
    return record, credits


def stream_url(info: dict) -> str:
    url = str(info.get("url") or "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(STREAM_HOST) or str(info.get("vcodec")) not in {"none", "None"}:
        raise SourceError("yt-dlp gave no audio-only stream from googlevideo.com")
    return url


STREAM_KEYS = ("id", "itag", "clen", "mime")      # what names the stream in a googlevideo link


def same_stream(original: str, location: str) -> bool:
    """Whether a redirect from a stream link stays on the same stream: https, another googlevideo.com
    server (YouTube moves a download between its servers this way), the same /videoplayback path and the
    same video id, format, size and type. Anything else (another host, another video) is not followed."""
    old, new = urllib.parse.urlparse(original), urllib.parse.urlparse(location)
    if new.scheme != "https" or not (new.hostname or "").endswith(STREAM_HOST) or new.path != old.path:
        return False
    before, after = urllib.parse.parse_qs(old.query), urllib.parse.parse_qs(new.query)
    return all(before.get(key) == after.get(key) for key in STREAM_KEYS if key in before)


def fetch_audio(ref: Ref, metadata: dict, metadata_path: Path, audio: Path, log, cache_dir: Path) -> None:
    """The audio-only stream, in ranges. A refused stream link (HTTP 403: signed links expire, and
    are tied to the address that asked) is resolved again once, and so is a redirect to anything but
    the same stream, or a size other than the stream's. A partial download is resumed only for the
    same stream format."""
    import audio_acquisition
    info = video_info(ref.episode)
    part, marker = audio.with_suffix(audio.suffix + ".part"), audio.with_name(audio.name + ".format")

    def download(info: dict) -> None:
        wait = float(info.get("available_at") or 0) - time.time()      # YouTube refuses the stream (403)
        if wait > 0:                                                     # until a few seconds after resolving
            time.sleep(min(wait + 1, MAX_WAIT))
        format_id = str(info.get("format_id") or "")
        if part.exists() and (not marker.is_file() or marker.read_text().strip() != format_id):
            part.unlink()                                  # another stream's bytes: start again
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(format_id + "\n")
        url = stream_url(info)
        size = int(info.get("filesize") or 0) or None
        audio_acquisition.acquire_audio(url, audio, range_bytes=RANGE_BYTES, expected_bytes=size,
                                        same_stream=lambda location: same_stream(url, location))
        marker.unlink(missing_ok=True)
    try:
        download(info)
        return
    except audio_acquisition.AudioError as error:
        if not re.search(r"\bHTTP (403|404|410)\b|not the same stream|not the stream's", str(error)):
            raise
        log(f"the YouTube stream link was refused ({error}); resolving it again")
    try:
        download(video_info(ref.episode, fresh=True))
    except audio_acquisition.AudioTransientError as error:
        raise SourceError(f"YouTube's stream server would not serve the audio ({error}); try again later",
                          retry=True) from error


def peek(ref: Ref) -> dict | None:
    """Show, title and publish time from YouTube, without the audio; None when it cannot be had now."""
    try:
        info = video_info(ref.episode)
    except SourceError:
        return None
    return peek_from_info(info, show_key(str(info.get("channel_id") or "")))


def peek_from_info(info: dict, show: str, title: str = "") -> dict:
    """The episode record for a video, from yt-dlp's facts (title: when YouTube gives none)."""
    return episode(show, NAME, str(info.get("title") or title), published_utc(info) or published_day(info) or "",
                           float(info.get("duration") or 0), description(info), str(info.get("language") or ""))


# ---------------------------------------------------------------- shows: channels, playlists, discovery

def channel(ref: Ref) -> dict:
    """{channel, title, playlist, handle} for a channel, @handle or playlist link. For a channel, its podcast
    playlist (from the channel's Podcasts tab) when it has exactly one, or one titled like the channel."""
    info = ytdlp(["--flat-playlist", "--playlist-items", "1", "--", ref.url])
    handle = str(info.get("uploader_id") or "")
    found = {"channel": str(info.get("channel_id") or ""), "title": str(info.get("channel") or info.get("uploader") or ""),
             "playlist": ref.show if PLAYLIST_ID.fullmatch(ref.show) else "", "handle": handle if handle.startswith("@") else ""}
    if not CHANNEL_ID.fullmatch(found["channel"]):
        raise SourceError("YouTube gave no channel for this link")
    if not found["playlist"]:
        found["playlist"] = podcast_playlist(found["channel"], found["title"])
    return found


def podcast_playlist(channel_id: str, title: str = "") -> str:
    """The channel's podcast playlist: the only one on its Podcasts tab, or the one whose name starts
    with the channel's; "" when it has none or several without a clear one."""
    try:
        info = ytdlp(["--flat-playlist", "--", f"https://www.youtube.com/channel/{channel_id}/podcasts"])
    except SourceError:
        return ""                                      # no Podcasts tab: the channel feed it is
    lists = [(str(e.get("id") or ""), str(e.get("title") or "")) for e in info.get("entries") or []
             if PLAYLIST_ID.fullmatch(str(e.get("id") or ""))]
    if len(lists) == 1:
        return lists[0][0]
    named = [pid for pid, name in lists if title and name.lower().startswith(title.lower())]
    return named[0] if len(named) == 1 else ""


def recent_videos(channel_id: str = "", playlist: str = "", count: int = 15, lang: str = "") -> list[dict]:
    """The newest videos of a channel: its podcast playlist (when given), its Videos tab and its Streams
    tab (many channels publish their main episodes as recorded live streams, which the Videos tab leaves
    out), newest first per list, each video once: {id, title, seconds, signed_in, tab}. A playlist
    alone when no channel is given. Without publish times: the monitor
    resolves each new one. The titles are as YouTube shows them in English, machine-translated from
    another language; lang (a code) asks for them in that language, which gives a video's original
    title when it is in it. count applies to each tab."""
    def listing(url: str, tab: str) -> list[dict]:
        info = ytdlp(["--flat-playlist", "--playlist-items", f"1:{count}",
                      *(["--extractor-args", f"youtube:lang={lang}"] if lang else []), "--", url])
        return [{"id": str(e["id"]), "title": str(e.get("title") or ""), "seconds": float(e.get("duration") or 0),
                 "signed_in": e.get("availability") in SIGNED_IN, "tab": tab}
                for e in info.get("entries") or [] if VIDEO_ID.fullmatch(str(e.get("id") or ""))]
    if playlist and not channel_id:
        return listing(f"https://www.youtube.com/playlist?list={playlist}", "playlist")
    found = listing(f"https://www.youtube.com/playlist?list={playlist}", "playlist") if playlist else []
    found += listing(f"https://www.youtube.com/channel/{channel_id}/videos", "videos")
    try:
        found += listing(f"https://www.youtube.com/channel/{channel_id}/streams", "streams")
    except SourceError:
        pass                                            # no Streams tab (most channels), or not readable now
    unique: dict[str, dict] = {}
    for video in found:
        unique.setdefault(video["id"], video)
    return list(unique.values())
