#!/usr/bin/env python3
"""podmonitor — watch the user's podcast subscriptions and queue new episodes.

    podmonitor.py            one check of every subscribed show (the Hermes cron job's call)

Monitored shows are the ones flagged in the show registry (podshows.py; "monitor <show>" through
Hermes), listed in the Shows.md overview.

For each show: RSS feed -> episodes not seen before -> Apple catalogue. RSS GUIDs
are state keys, not guaranteed Apple identities. Unmatched episodes remain pending
after the three-day notification so they can recover when indexed later.
The first time a show is seen, everything already published is marked as seen: only episodes
released from then on are processed. Trailers and teasers under 5 minutes are skipped.

Nothing is printed on a normal check; queued episodes are announced by the queue when their note
is ready. One notification is printed after three days unresolved, without giving up.
State: PODNOTE_STATE/monitor-state.json.
"""
from __future__ import annotations

import json
import os
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

STATE = Path(os.environ.get("PODNOTE_STATE", "/var/tmp/podnote-state"))
MIN_SECONDS = 5 * 60
NOTIFY_AFTER = timedelta(days=3)


def subscriptions() -> list[tuple[str, str]]:
    """(show id, storefront) for every monitored show in the registry."""
    return podshows.monitored()


def feed_episodes(show: str, country: str) -> list[podfetch.Episode]:
    feed = podfetch.resolve_feed_url(show, country)
    status, body, _ = podfetch.http_get(feed)
    if status != 200:
        raise LookupError(f"feed returned HTTP {status}")
    return podfetch.parse_feed(body, feed).episodes


def apple_episodes(show: str, country: str) -> list[podfetch.AppleEpisodeHint]:
    """Query both storefronts once and deduplicate Apple track IDs."""
    hints: dict[str, podfetch.AppleEpisodeHint] = {}
    for store in podfetch.storefronts(country):
        query = urllib.parse.urlencode({"id": show, "entity": "podcastEpisode",
                                        "limit": podfetch.APPLE_EPISODE_LIMIT, "country": store})
        status, body, _ = podfetch.http_get(f"{podfetch.ITUNES_LOOKUP}?{query}")
        if status != 200:
            raise LookupError(f"Apple lookup returned HTTP {status} ({store})")
        for row in json.loads(body).get("results", []):
            track = row.get("trackId")
            if not track:
                continue
            published = None
            if row.get("releaseDate"):
                try:
                    published = datetime.fromisoformat(row["releaseDate"].replace("Z", "+00:00"))
                except ValueError:
                    pass
            duration = row.get("trackTimeMillis")
            hints.setdefault(str(track), podfetch.AppleEpisodeHint(
                track_id=str(track), title=row.get("trackName") or "",
                published=published, episode_guid=str(row.get("episodeGuid") or ""),
                episode_url=str(row.get("episodeUrl") or ""),
                duration_seconds=float(duration) / 1000 if duration else None,
            ))
    return list(hints.values())


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
            rss_duration = podfetch._duration_seconds(match.duration)
            if not (hint.published and match.published and hint.duration_seconds and rss_duration):
                continue
            delta = abs((match.published - hint.published).total_seconds())
            difference = abs(rss_duration - hint.duration_seconds)
            if delta > 3 * 86400 or difference > max(60, hint.duration_seconds * 0.05):
                continue
        candidates.setdefault(match.guid, set()).add(hint.track_id)
    return {guid: f"https://podcasts.apple.com/{country}/podcast/id{show}?i={next(iter(tracks))}"
            for guid, tracks in candidates.items() if len(tracks) == 1}


def check() -> str:
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "monitor-state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    messages = []
    for show, country in subscriptions():
        entry = state.setdefault(show, {"seen": [], "pending": {}})
        try:
            episodes = feed_episodes(show, country)
        except Exception as error:                      # a feed down today is checked again next time
            entry["last_error"] = f"{podqueue.iso(podqueue.now())} {error}"[:300]
            continue
        entry.pop("last_error", None)
        if "initialised" not in entry:                  # first sight: the back catalogue is not queued,
            since = podshows.monitored_since(show)        # but episodes released since the follow command are
            entry["seen"] = [e.guid for e in episodes if e.guid and not (
                since and e.published and podqueue.iso(e.published.astimezone(podqueue.timezone.utc)) >= since)]
            entry["initialised"] = podqueue.iso(podqueue.now())
            if len(entry["seen"]) == len([e for e in episodes if e.guid]):
                continue
        seen = set(entry["seen"])
        new = [e for e in episodes if e.guid and e.guid not in seen]
        if not new:
            continue
        try:
            hints = apple_episodes(show, country)
        except Exception as error:
            entry["last_error"] = f"{podqueue.iso(podqueue.now())} {error}"[:300]
            continue                       # an Apple error does not age pending into a skip
        links = matched_links(show, country, new, hints)
        notified = entry.setdefault("unresolved_notified", [])
        for episode in reversed(new):                   # oldest first, so the queue keeps release order
            seconds = podfetch._duration_seconds(episode.duration) or 0
            if 0 < seconds < MIN_SECONDS:
                entry["seen"].append(episode.guid)
                entry["pending"].pop(episode.guid, None)
                continue
            if episode.guid in links:
                outcome = podqueue.add(links[episode.guid], source="monitor")
                if outcome.startswith(("Queued (", "Already queued (", "Already in the library;")):
                    entry["seen"].append(episode.guid)
                    entry["pending"].pop(episode.guid, None)
                    if episode.guid in notified:
                        notified.remove(episode.guid)
                else:
                    entry["last_error"] = f"{podqueue.iso(podqueue.now())} {outcome}"[:300]
                continue
            first = entry["pending"].setdefault(episode.guid, podqueue.iso(podqueue.now()))
            if (episode.guid not in notified and
                    podqueue.now() - podqueue.parse_iso(first) >= NOTIFY_AFTER):
                notified.append(episode.guid)
                messages.append(f"Podcast still unresolved on Apple after 3 days (will retry): {episode.title}")
        current = {e.guid for e in episodes if e.guid}      # never forget an episode still in the feed
        entry["seen"] = [g for g in entry["seen"] if g in current] + [g for g in entry["seen"] if g not in current][-500:]
        entry["unresolved_notified"] = [g for g in notified if g in current or g in entry["pending"]]
    podqueue.atomic_write(path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    return "\n".join(messages)


def main() -> int:
    message = check()
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
