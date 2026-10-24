#!/usr/bin/env python3
"""podmonitor — watch the user's podcast subscriptions and queue new episodes.

    podmonitor.py            one check of every subscribed show (the Hermes cron job's call)

Monitored shows are the ones flagged in the show registry (podshows.py; "monitor <show>" through
Hermes), listed in the Shows.md overview.

For each show: Apple lookup -> RSS feed -> episodes not seen before. A new episode's RSS GUID is
matched to Apple's episodeGuid to get an ordinary Apple episode link, which goes into the podqueue.
An episode Apple has not indexed yet is simply tried again on the next check (for up to 3 days).
The first time a show is seen, everything already published is marked as seen: only episodes
released from then on are processed. Trailers and teasers under 5 minutes are skipped.

Nothing is printed on a normal check; queued episodes are announced by the queue when their note
is ready. A message is printed only when an episode could not be matched to Apple for 3 days.
State: PODNOTE_STATE/monitor-state.json.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402,F401  (importing it loads the project .env)
import podfetch  # noqa: E402
import podqueue  # noqa: E402
import podshows  # noqa: E402

STATE = Path(os.environ.get("PODNOTE_STATE", "/var/tmp/podnote-state"))
MIN_SECONDS = 5 * 60
GIVE_UP = timedelta(days=3)


def subscriptions() -> list[tuple[str, str]]:
    """(show id, storefront) for every monitored show in the registry."""
    return podshows.monitored()


def feed_episodes(show: str, country: str) -> list[podfetch.Episode]:
    feed = podfetch.resolve_feed_url(show, country)
    status, body, _ = podfetch.http_get(feed)
    if status != 200:
        raise LookupError(f"feed returned HTTP {status}")
    return podfetch.parse_feed(body, feed).episodes


def apple_links(show: str, country: str) -> dict[str, str]:
    """episodeGuid -> Apple episode link, for the show's most recent episodes on Apple."""
    links = {}
    for store in podfetch.storefronts(country):              # the US catalogue first: others lag it
        # Use the same window as direct-link resolution. Apple can return a stale
        # 50-episode response while its 200-episode response already has a new GUID.
        query = urllib.parse.urlencode({"id": show, "entity": "podcastEpisode",
                                        "limit": podfetch.APPLE_EPISODE_LIMIT, "country": store})
        status, body, _ = podfetch.http_get(f"{podfetch.ITUNES_LOOKUP}?{query}")
        if status != 200:
            raise LookupError(f"Apple lookup returned HTTP {status}")
        for row in json.loads(body).get("results", []):
            guid, track = row.get("episodeGuid"), row.get("trackId")
            if guid and track and guid not in links:
                links[guid] = f"https://podcasts.apple.com/{country}/podcast/id{show}?i={track}"
    return links


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
        links = {}
        try:
            links = apple_links(show, country)
        except Exception as error:
            entry["last_error"] = f"{podqueue.iso(podqueue.now())} {error}"[:300]
        for episode in reversed(new):                   # oldest first, so the queue keeps release order
            seconds = podfetch._duration_seconds(episode.duration) or 0
            if 0 < seconds < MIN_SECONDS:
                entry["seen"].append(episode.guid)
                continue
            if episode.guid in links:
                podqueue.add(links[episode.guid], source="monitor")
                entry["seen"].append(episode.guid)
                entry["pending"].pop(episode.guid, None)
                continue
            first = entry["pending"].setdefault(episode.guid, podqueue.iso(podqueue.now()))
            if podqueue.now() - podqueue.parse_iso(first) >= GIVE_UP:
                entry["seen"].append(episode.guid)
                entry["pending"].pop(episode.guid, None)
                messages.append(f"Podcast not on Apple after 3 days, skipped: {episode.title}")
        current = {e.guid for e in episodes if e.guid}      # never forget an episode still in the feed
        entry["seen"] = [g for g in entry["seen"] if g in current] + [g for g in entry["seen"] if g not in current][-500:]
    podqueue.atomic_write(path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    return "\n".join(messages)


def main() -> int:
    message = check()
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
