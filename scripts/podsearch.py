#!/usr/bin/env python3
"""podsearch — queue a show's episodes whose titles contain a phrase.

    podsearch.py "<Apple show link>" "<phrase>" [--period "this year"] [--all] [--dry-run]

The show's RSS feed lists every episode; titles are matched against the phrase ignoring case and
punctuation ("Night Sky" finds "NIGHT-SKY"). Each match is turned into an ordinary Apple episode
link (the US catalogue first) and added to the podcast queue, which skips episodes already in the
library or the queue. Up to 10 matches are queued at once; with more, they are listed and nothing is
queued unless --all is given.

Periods (by release date): "this year", "last year", "this month", "last month", "this week",
"last week", "last 3 months", "last 2 weeks", "last 10 days", "2026", "2026-09", "since 2026-09-01".
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402,F401  (importing it loads the project .env)
import podfetch  # noqa: E402
import podmonitor  # noqa: E402
import podqueue  # noqa: E402

AT_ONCE = 10


def words(text: str) -> str:
    """Lower-case letters and digits separated by single spaces; accents and punctuation dropped."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", plain.lower()))


def period(text: str, today: date | None = None) -> tuple[date, date]:
    """(first day, last day) of a period in plain words; ValueError if it is not understood."""
    today = today or date.today()
    spoken = text.strip().lower()
    month_start = today.replace(day=1)
    if spoken in ("", "all", "any", "ever"):
        return date.min, date.max
    if spoken == "this year":
        return today.replace(month=1, day=1), today
    if spoken == "last year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if spoken == "this month":
        return month_start, today
    if spoken == "last month":
        end = month_start - timedelta(days=1)
        return end.replace(day=1), end
    if spoken == "this week":
        return today - timedelta(days=today.weekday()), today
    if spoken == "last week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    found = re.fullmatch(r"(?:last|past) (\d+) (day|week|month|year)s?", spoken)
    if found:
        n, unit = int(found.group(1)), found.group(2)
        days = {"day": 1, "week": 7, "month": 31, "year": 366}[unit] * n
        return today - timedelta(days=days), today
    found = re.fullmatch(r"(?:since|from) (\d{4}-\d{2}-\d{2})", spoken)
    if found:
        return date.fromisoformat(found.group(1)), today
    if re.fullmatch(r"\d{4}", spoken):
        return date(int(spoken), 1, 1), date(int(spoken), 12, 31)
    if re.fullmatch(r"\d{4}-\d{2}", spoken):
        start = date.fromisoformat(spoken + "-01")
        following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start, following - timedelta(days=1)
    raise ValueError(f'period not understood: "{text}" (try "this year", "last 3 months", "2026-09")')


def apple_links(show: str, country: str) -> tuple[dict[str, str], dict[str, str]]:
    """({episodeGuid: link}, {title words: link}) for up to Apple's last 200 episodes of the show."""
    by_guid, by_title = {}, {}
    for store in podfetch.storefronts(country):
        query = urllib.parse.urlencode({"id": show, "entity": "podcastEpisode", "limit": 200, "country": store})
        status, body, _ = podfetch.http_get(f"{podfetch.ITUNES_LOOKUP}?{query}")
        if status != 200:
            raise LookupError(f"Apple lookup returned HTTP {status}")
        for row in json.loads(body).get("results", []):
            track = row.get("trackId")
            if not track or row.get("wrapperType") == "track" and row.get("kind") == "podcast":
                continue
            link = f"https://podcasts.apple.com/{country}/podcast/id{show}?i={track}"
            if row.get("episodeGuid"):
                by_guid.setdefault(row["episodeGuid"], link)
            if row.get("trackName"):
                by_title.setdefault(words(row["trackName"]), link)
    return by_guid, by_title


def search(show_link: str, phrase: str, when: str = "", queue_all: bool = False, dry_run: bool = False) -> str:
    show, episode, country = podfetch.parse_apple_url(show_link)
    if not show:
        return "Not a podcast link: give the show's Apple Podcasts link."
    wanted = words(phrase)
    if not wanted:
        return "Give a word or phrase to look for in the episode titles."
    first, last = period(when)
    episodes = podmonitor.feed_episodes(show, country or "us")
    matches = []
    for item in episodes:
        released = item.published.astimezone(timezone.utc).date() if item.published else None
        if f" {wanted} " in f" {words(item.title)} " and (released is None or first <= released <= last):
            matches.append((released, item))
    matches.sort(key=lambda pair: pair[0] or date.min)
    scope = f'"{phrase}"' + (f", {when.strip()}" if when.strip() else "")
    if not matches:
        return f"No episode titles match {scope} ({len(episodes)} episodes searched)."
    lines = [f"{released or 'undated'} — {item.title}" for released, item in matches]
    if dry_run or (len(matches) > AT_ONCE and not queue_all):
        head = (f"{len(matches)} episodes match {scope}" +
                ("" if dry_run else f"; more than {AT_ONCE}, so none queued yet — say \"queue all\" to add them") + ":")
        return "\n".join([head] + lines)
    by_guid, by_title = apple_links(show, country or "us")
    queued, skipped, missing = [], [], []
    for released, item in matches:
        link = by_guid.get(item.guid) or by_title.get(words(item.title))
        if not link:
            missing.append(f"{released or 'undated'} — {item.title}")
            continue
        answer = podqueue.add(link, source="search")
        (queued if answer.startswith("Queued") else skipped).append(f"{released or 'undated'} — {item.title} ({answer.rstrip('.')})")
    report = [f"{len(matches)} episodes match {scope}: {len(queued)} queued, {len(skipped)} skipped, "
              f"{len(missing)} not found on Apple."]
    report += [f"+ {line}" for line in queued] + [f"= {line}" for line in skipped] + [f"? {line}" for line in missing]
    return "\n".join(report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("show")
    parser.add_argument("phrase")
    parser.add_argument("--period", default="")
    parser.add_argument("--all", action="store_true", help=f"queue every match, even more than {AT_ONCE}")
    parser.add_argument("--dry-run", action="store_true", help="list the matches, queue nothing")
    args = parser.parse_args()
    try:
        print(search(args.show, args.phrase, args.period, args.all, args.dry_run))
    except ValueError as error:
        print(str(error))
        return 1
    except LookupError as error:
        print(f"Could not read the show: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
