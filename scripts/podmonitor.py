#!/usr/bin/env python3
"""podmonitor — watch the user's podcast subscriptions and queue new episodes.

    podmonitor.py            one check of every subscribed show (the Hermes cron job's call)

Monitored shows are the ones someone follows in the show registry (podshows.py follow; "follow <show>"
through Hermes), listed in the Shows.md overview.

For each show: RSS feed -> episodes not seen before -> Apple catalogue. RSS GUIDs
are state keys, not guaranteed Apple identities. Unmatched episodes remain pending
after the three-day notification so they can recover when indexed later.
The first time a show is seen, everything already published is marked as seen: only episodes
released from then on are processed. Trailers and teasers under 5 minutes are skipped.

A show linked to a YouTube channel (podshows.py link) is also watched there: its podcast playlist (when
it has one), its Videos and its Streams tabs, anything under 5 minutes skipped. Each source of a show
has a priority (Apple first unless the admin sets otherwise): a new episode from a lower-priority source
waits SOURCE_WAIT_HOURS for the higher one's copy, and is queued only if that does not turn up.
Each new video is resolved once for its publish time; one still to premiere waits. The queue is
given the show, title and publish time of every episode it is offered, so an episode that arrives on
both sources is processed once; a waiting copy is kept in hand (`held`) until the other is made, and
queued if that one fails.

An episode that looks like it is in another language than English, from a show whose language the
user has not confirmed, is not queued: it waits, and one message asks for the show's language
("<show> is in <language>", podshows.py language); the next check after the answer queues it.

Both sources follow one set of rules (Watch); they differ in coverage: the feed lists every episode
(COMPLETE), YouTube's tabs only the newest (BOUNDED), so a waiting video is looked up by id.

Nothing is printed on a normal check; queued episodes are announced by the queue when their note
is ready. One notification is printed after three days unresolved, without giving up; one when a source
has not been read for STALE_HOURS; one for a fault in the monitor on a show (the pass goes on).
State: PODNOTE_STATE/monitor-state.json (an Apple show's entry under its id, a YouTube playlist's or
channel's under youtube:<id>).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402,F401  (importing it loads the project .env)
import podfetch  # noqa: E402
import podqueue  # noqa: E402
import podshows  # noqa: E402
import sources  # noqa: E402

STATE = Path(os.environ.get("PODNOTE_STATE", "/var/tmp/podnote-state"))
MIN_SECONDS = 5 * 60                   # anything shorter is a trailer or a clip, on every source
SOURCE_WAIT_HOURS = float(os.environ.get("PODNOTE_SOURCE_WAIT_HOURS", "12"))  # a lower-priority copy waits this long
NOTIFY_AFTER = timedelta(days=3)
STALE_HOURS = 48                       # a source unreadable this long is reported (once)
PENDING_DAYS = 7                       # a video YouTube still refuses after this long is reported (once), not dropped


FEED_LANGUAGE: dict[str, str] = {}      # show -> its feed's <language>, from the last feed_episodes


def feed_episodes(show: str, country: str) -> list[podfetch.Episode]:
    feed = podfetch.resolve_feed_url(show, country)
    status, body, _ = podfetch.http_get(feed)
    if status != 200:
        raise LookupError(f"feed returned HTTP {status}")
    parsed = podfetch.parse_feed(body, feed)
    FEED_LANGUAGE[show] = parsed.language
    return parsed.episodes


def apple_episodes(show: str, country: str) -> list[podfetch.AppleEpisodeHint]:
    """Every episode Apple lists for the show, recent and full windows, both storefronts; raises
    podfetch.AppleLookupError when a lookup fails."""
    return podfetch.apple_episode_hints(show, country)


def matched_links(show: str, country: str, episodes: list[podfetch.Episode],
                  hints: list[podfetch.AppleEpisodeHint]) -> dict[str, str]:
    """Use the resolver's matching, but never queue on an uncorroborated title.

    Multiple Apple tracks pointing at the same RSS item are ambiguous, not two
    independent episodes. Leave them pending rather than selecting arbitrarily.
    """
    candidates: dict[str, set[str]] = {}
    for hint in hints:
        try:
            match = podfetch.match_episode(
                episodes, guid=hint.episode_guid, enclosure_url=hint.episode_url,
                title=hint.title, published=hint.published,
                duration_seconds=hint.duration_seconds,
            )
        except podfetch.AmbiguousEpisodeError:
            continue
        if match is None:
            continue
        if match.match_method == "title_date_duration":
            rss_duration = podfetch.parse_duration(match.duration)
            if not (hint.published and match.published and hint.duration_seconds and rss_duration):
                continue
            delta = abs((match.published - hint.published).total_seconds())
            difference = abs(rss_duration - hint.duration_seconds)
            if delta > 3 * 86400 or difference > max(60, hint.duration_seconds * 0.05):
                continue
        candidates.setdefault(match.guid, set()).add(hint.track_id)
    return {guid: sources.apple.episode_link(show, next(iter(tracks)), country)
            for guid, tracks in candidates.items() if len(tracks) == 1}


def made_elsewhere(peek: dict) -> bool:
    """Whether this episode's note is already in the library, made from another source's copy (or from
    this one): then a copy pending, held or new on this source has nothing left to wait for. Only a
    note counts; a copy merely queued may still fail."""
    return podqueue.twin_status(peek) == "library"


def check() -> str:
    """One pass over every followed show. A pass already running (the cron job, or one started by hand)
    is left to finish: this one does nothing, so neither overwrites the other's monitor state. The state is
    saved after each show, so a pass cut short keeps what it learnt; an error on one show is recorded on it
    (and reported once) and the pass goes on to the next."""
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "monitor-run.lock").open("a+") as runner:
        try:
            fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ""
        path = STATE / "monitor-state.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        messages = []
        for show, country in podshows.monitored():
            watches = []
            try:
                if not show.startswith("yt:"):          # a show only on YouTube has no Apple feed
                    watches.append(check_apple(show, country, state, messages))
                watches.append(check_youtube(show, state, messages))
            except Exception as error:                  # a fault on one show never stops the others
                problem = f"monitor error: {type(error).__name__}: {error}"[:300]
                entry = state.setdefault(show, {"seen": [], "pending": {}})
                Watch(entry, COMPLETE).failed(problem)
                if entry.get("error_reported") != problem:
                    entry["error_reported"] = problem
                    messages.append(f"⚠️ {(podshows.show_for(show) or {}).get('folder') or show}: {problem}")
            else:
                state.get(show, {}).pop("error_reported", None)
            for watch in filter(None, watches):
                watch.finish(messages, (podshows.show_for(show) or {}).get("folder") or show)
            podqueue.atomic_write(path, json.dumps(state, indent=2, sort_keys=True) + "\n")
        return "\n".join(messages)


COMPLETE, BOUNDED = "complete", "bounded"     # a source that lists every episode (a feed), or only the newest


class Watch:
    """One source's watch on one show (an entry of monitor-state.json), with the rules every source
    shares: the back catalogue at the first read after a follow, episodes settled (`seen`), waiting to be
    resolvable (`pending`) or held for a higher-priority source (`held`), the queue's answer, the language
    question, and health (`last_attempt`, `last_success`, `failing_since`). coverage: a COMPLETE source's
    listing is the whole catalogue, so what it no longer lists is gone; a BOUNDED one (YouTube's newest
    videos) proves nothing by leaving something out, so waiting episodes are kept and looked up by id."""

    def __init__(self, entry: dict, coverage: str):
        self.entry, self.coverage, self.broken = entry, coverage, False
        entry.setdefault("seen", [])
        entry.setdefault("pending", {})
        entry["last_attempt"] = podqueue.iso(podqueue.now())

    @property
    def seen(self) -> set:
        return set(self.entry["seen"])

    @property
    def held(self) -> dict:
        return self.entry.setdefault("held", {})

    def failed(self, error) -> None:
        """The source could not be read this time: checked again next time; nothing is aged or dropped."""
        self.broken = True
        self.entry["last_error"] = f"{podqueue.iso(podqueue.now())} {error}"[:300]
        self.entry.setdefault("failing_since", podqueue.iso(podqueue.now()))

    def read(self) -> None:
        self.entry.pop("last_error", None)

    def finish(self, messages: list[str], folder: str) -> None:
        """After the pass: healthy again, or (once) a source that has not been read for STALE_HOURS."""
        if not self.broken:
            self.entry["last_success"] = podqueue.iso(podqueue.now())
            self.entry.pop("failing_since", None)
            self.entry.pop("stale_reported", None)
            return
        since = podqueue.parse_iso(self.entry["failing_since"])
        if podqueue.now() - since >= timedelta(hours=STALE_HOURS) and not self.entry.get("stale_reported"):
            self.entry["stale_reported"] = podqueue.iso(podqueue.now())
            messages.append(f"⚠️ {folder}: not read since {self.entry['failing_since'][:16].replace('T', ' ')} UTC "
                            f"({self.entry.get('last_error', '')[21:160]}); checked every pass, nothing is dropped")

    def needs_first_read(self, since: str) -> bool:
        return "initialised" not in self.entry or bool(since and self.entry["initialised"] < since)

    def first_read(self, keys: list[str], fresh: set) -> bool:
        """The first read after a follow: everything listed is back catalogue, except what came out after
        the follow (fresh). True when something is left to process now."""
        self.entry["seen"] = [key for key in keys if key not in fresh]
        self.entry["initialised"] = podqueue.iso(podqueue.now())
        return bool(fresh)

    def done(self, key: str) -> None:
        self.entry["seen"].append(key)
        self.entry["pending"].pop(key, None)
        self.held.pop(key, None)
        for told in (self.entry.get("unresolved_notified", []), self.entry.get("pending_notified", [])):
            if key in told:
                told.remove(key)

    def pending_since(self, key: str):
        return podqueue.parse_iso(self.entry["pending"].setdefault(key, podqueue.iso(podqueue.now())))

    def hold(self, key: str, peek: dict | None = None, link: str = "") -> dict:
        """Keep an episode in hand, with its facts and its link, until something settles it: a wait for a
        higher-priority source (begun the first time; an older entry may hold just that time), or an offer
        the queue did not take yet. Leaving the listings does not settle it."""
        old = self.held.get(key)
        kept = old if isinstance(old, dict) else {}
        since = old if isinstance(old, str) else kept.get("since") or podqueue.iso(podqueue.now())
        self.held[key] = {"since": since, "peek": peek or kept.get("peek") or {}}
        if link or kept.get("link"):
            self.held[key]["link"] = link or kept["link"]
        return self.held[key]

    def wait_over(self, key: str, peek: dict, waits: bool) -> bool:
        """Whether a copy may be queued now: not made from another source already (then it is settled),
        and, when another source comes first, that one's copy is not on its way and the wait is over. A copy
        waits while the higher one is queued: if that fails for good, this one is made after all."""
        if made_elsewhere(peek):
            self.done(key)
            return False
        if not waits:
            return True
        if podqueue.twin_status(peek) == "queue":
            return False
        return podqueue.now() - podqueue.parse_iso(self.hold(key, peek)["since"]) >= timedelta(hours=SOURCE_WAIT_HOURS)

    def offer(self, link: str, key: str, peek: dict, show: str, messages: list[str]) -> None:
        """Into the queue; settled when queued or already there, else kept (a language question is asked once)."""
        outcome = podqueue.add(link, source="monitor", peek=peek)
        if outcome.startswith(("Queued (", "Already queued (", "Already in the library;")):
            self.done(key)
        else:                                           # kept in hand, so leaving the listings loses nothing
            self.hold(key, peek, link)
            self.entry["last_error"] = f"{podqueue.iso(podqueue.now())} {outcome}"[:300]
            ask_language(show, key, peek.get("title", ""), outcome, self.entry, messages)

    def tidy(self, listed: set) -> None:
        """Keep the state small: what is still listed stays seen, plus the last 500 of the rest. Held episodes
        stay on every source (a feed can roll an episode off before it is settled); a complete source's
        questions go once it lists the episode no longer and nothing holds it."""
        seen = self.entry["seen"]
        self.entry["seen"] = [k for k in seen if k in listed] + [k for k in seen if k not in listed][-500:]
        if self.coverage == COMPLETE:
            self.entry["language_asked"] = [k for k in self.entry.get("language_asked", [])
                                            if k in listed or k in self.held]
            self.entry["unresolved_notified"] = [k for k in self.entry.get("unresolved_notified", [])
                                                 if k in listed or k in self.entry["pending"]]


def check_apple(show: str, country: str, state: dict, messages: list[str]) -> Watch:
    """New episodes of the show's feed into the queue, by their Apple links (the feed is the whole
    catalogue: COMPLETE). An episode Apple does not list yet stays pending, with one notice after three days."""
    watch = Watch(state.setdefault(show, {"seen": [], "pending": {}}), COMPLETE)
    try:
        episodes = feed_episodes(show, country)
        if not episodes:                            # an empty feed proves nothing: read as nothing seen,
            raise LookupError("the feed lists no episodes")   # the next full read would queue it all
    except Exception as error:                      # a feed down today is checked again next time
        watch.failed(error)
        return watch
    watch.read()
    since = podshows.monitored_since(show)
    keys = [e.guid for e in episodes if e.guid]
    if watch.needs_first_read(since):               # the back catalogue is not queued; what came out since the follow is
        fresh = {e.guid for e in episodes if e.guid and since and e.published
                 and podqueue.iso(e.published.astimezone(podqueue.timezone.utc)) >= since}
        if not watch.first_read(keys, fresh):
            return watch
    seen = watch.seen
    new = [e for e in episodes if e.guid and e.guid not in seen]
    waits = podshows.outranked(show, podshows.show_for(show) or {}, "apple")   # another source comes first here
    for guid, waiting in list(watch.held.items()):      # held, and rolled off the feed: settled by its link
        if guid not in keys and isinstance(waiting, dict) and waiting.get("link") and waiting.get("peek"):
            if watch.wait_over(guid, waiting["peek"], waits):
                watch.offer(waiting["link"], guid, waiting["peek"], show, messages)
    if not new:
        watch.tidy(set(keys))
        return watch
    try:
        hints = apple_episodes(show, country)
    except Exception as error:
        watch.failed(error)                          # an Apple error does not age pending into a skip
        return watch
    links = matched_links(show, country, new, hints)
    notified = watch.entry.setdefault("unresolved_notified", [])
    for episode in reversed(new):                   # oldest first, so the queue keeps release order
        seconds = podfetch.parse_duration(episode.duration) or 0
        if 0 < seconds < MIN_SECONDS:
            watch.done(episode.guid)
            continue
        peek = sources.apple.peek_from_feed(episode, show, FEED_LANGUAGE.get(show, ""))
        if episode.guid in links:                   # its Apple id: another Apple episode of the same title is not it
            peek["id"] = sources.parse(links[episode.guid]).episode
        if made_elsewhere(peek):                    # pending, held or new: nothing left to wait for
            watch.done(episode.guid)
            continue
        if episode.guid in links:
            if waits:
                watch.hold(episode.guid, peek, links[episode.guid])
                if not watch.wait_over(episode.guid, peek, waits):
                    continue                           # the higher copy may still come or fail: keep this one in hand
            watch.offer(links[episode.guid], episode.guid, peek, show, messages)
            continue
        first = watch.pending_since(episode.guid)
        if episode.guid not in notified and podqueue.now() - first >= NOTIFY_AFTER:
            notified.append(episode.guid)
            messages.append(f"Podcast still unresolved on Apple after 3 days (will retry): {episode.title}")
    watch.tidy(set(keys))                           # never forget an episode still in the feed
    return watch


def ask_language(show: str, key: str, title: str, outcome: str, entry: dict, messages: list[str]) -> None:
    """Once per episode: ask the admin (the monitor's own messages) for the show's language when the queue
    held an episode for it. Only the admin sets a show's language, so a member who brought the show in is
    not asked."""
    found = re.search(r"episode looks (\w+)", outcome)
    asked = entry.setdefault("language_asked", [])
    if not found or key in asked:
        return
    asked.append(key)
    entry_show = podshows.show_for(show) or {}
    folder = entry_show.get("folder") or show
    question = (f"{folder}: a new episode looks {found.group(1)}: \"{title}\". It waits until you tell me the "
                f"show's language (\"{folder} is in {found.group(1)}\", or \"… is in English\").")
    messages.append(question)


def check_youtube(show: str, state: dict, messages: list[str] | None = None) -> Watch | None:
    """New videos of the show's YouTube channel into the queue, oldest first: its podcast playlist, its
    Videos and its Streams tabs (the newest 15 of each: BOUNDED). What a tab holds when first seen is
    back catalogue, except videos released after the follow. When another source comes first (Apple,
    unless the admin set otherwise), a new video waits SOURCE_WAIT_HOURS for that copy, and is queued only
    if none was made (so an episode only on YouTube is made as well). A video still to premiere or stream,
    or one YouTube refuses for now, stays pending; anything under 5 minutes, a members-only or a private
    video is passed over. A waiting video pushed off the listings is still looked up by its id."""
    youtube = sources.youtube
    messages = messages if messages is not None else []
    registered = podshows.show_for(show) or {}
    linked = podshows.youtube_link(registered, show)
    if not linked.get("channel"):
        return None
    watch = Watch(state.setdefault(f"youtube:{linked.get('playlist') or linked['channel']}", {"seen": [], "pending": {}}),
                  BOUNDED)
    entry = watch.entry
    try:
        videos = youtube.recent_videos(linked["channel"], linked.get("playlist") or "")
        if not videos:                              # an empty listing proves nothing (see check_apple)
            raise LookupError("YouTube lists no videos")
    except Exception as error:                      # YouTube or yt-dlp down today: checked again next time
        watch.failed(error)
        return watch
    watch.read()
    tabs = {video.get("tab", "videos") for video in videos}
    since = podshows.monitored_since(show)
    if watch.needs_first_read(since):               # first sight, or followed again
        fresh = released_since(videos, since) if since else set()
        if fresh is None:                               # YouTube will not say yet: the first read waits a pass
            return watch
        entry["tabs"] = sorted(tabs)
        if not watch.first_read([video["id"] for video in videos], fresh):
            return watch
    known = set(entry.get("tabs", []))
    if tabs - known:                                # a tab read for the first time (a channel's Streams, or one
        first = [v for v in videos if v.get("tab") in tabs - known and v["id"] not in entry["seen"]]   # unreadable
        fresh = released_since(first, entry["initialised"]) if first else set()   # before): its back catalogue,
        if fresh is None:                           # except what came out since the watch began. YouTube will
            videos = [v for v in videos if v not in first]          # not say yet: the new tab waits a pass
        else:
            entry["seen"] += [v["id"] for v in first if v["id"] not in fresh]
            entry["tabs"] = sorted(known | tabs)
    waits = podshows.outranked(show, registered, "youtube")         # another source comes first: wait for it
    seen, held = watch.seen, watch.held
    listed = {video["id"] for video in videos}
    revisit = [{"id": video, "title": "", "seconds": 0, "signed_in": False}      # waiting, but past the listings
               for video in dict.fromkeys([*held, *entry["pending"]]) if video not in listed and video not in seen]
    for video in reversed([v for v in videos if v["id"] not in seen] + revisit):
        key = video["id"]
        if video["signed_in"] or 0 < video["seconds"] < MIN_SECONDS:
            watch.done(key)
            continue
        waiting = held.get(key)
        if isinstance(waiting, dict) and waiting.get("peek"):   # held since an earlier check: its facts are known
            if watch.wait_over(key, waiting["peek"], waits):
                watch.offer(youtube.watch_url(key), key, waiting["peek"], show, messages)
            continue
        try:
            info = youtube.video_info(key)
        except sources.SourceError as error:
            first = watch.pending_since(key)
            if not error.retry:
                watch.done(key)                     # private, members-only, removed: never
                continue
            entry["last_error"] = f"{podqueue.iso(podqueue.now())} {key}: {error}"[:300]
            told = entry.setdefault("pending_notified", [])
            if podqueue.now() - first >= timedelta(days=PENDING_DAYS) and key not in told:
                told.append(key)                    # kept and tried again: a week is only worth a word
                messages.append(f"YouTube video still unavailable after {PENDING_DAYS} days (will keep trying): "
                                f"{video['title'] or youtube.watch_url(key)}")
            continue
        if float(info.get("duration") or 0) < MIN_SECONDS:
            watch.done(key)
            continue
        peek = {**youtube.peek_from_info(info, show, video["title"]), "id": key}
        if waits:
            watch.hold(key, peek)
            if not watch.wait_over(key, peek, waits):
                continue                               # give the higher source its chance; checked again next time
        watch.offer(youtube.watch_url(key), key, peek, show, messages)
    watch.tidy(listed)
    return watch


def released_since(videos: list[dict], since: str) -> set[str] | None:
    """The listed videos released after `since` (when the show was followed). Each tab is read newest first
    until its first video proven older, by its exact time or, when YouTube gives only a day, by a day
    before the follow's; every video before that one is new. None when YouTube will not describe one of
    them yet: the first read is tried again next pass, so nothing new is marked seen unproven."""
    fresh = set()
    for tab in dict.fromkeys(video.get("tab", "videos") for video in videos):
        for video in [v for v in videos if v.get("tab", "videos") == tab]:
            try:
                info = sources.youtube.video_info(video["id"])
            except sources.SourceError as error:
                if error.retry:
                    return None
                continue                                # private or members-only: never ours to make
            published = sources.youtube.published_utc(info)
            day = published[:10] if published else (sources.youtube.published_day(info) or "")
            if (published and published < since[:19]) or (not published and day and day < since[:10]):
                break
            fresh.add(video["id"])
    return fresh


def main() -> int:
    import users
    refused = users.admin_only("run the show monitor")    # the cron job's, or the admin's by hand
    if refused:
        print(refused)
        return 1
    message = check()
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
