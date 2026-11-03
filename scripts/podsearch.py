#!/usr/bin/env python3
"""podsearch — queue a show's episodes whose titles contain a phrase.

    podsearch.py "<show>" "<phrase>" [--period "this year"] [--all] [--dry-run] [--language L]

The show: its Apple Podcasts link, its YouTube channel, @handle or playlist link, or its name or key
in the show registry. An Apple show is searched in its RSS feed (every episode); a YouTube show in
its podcast playlist, Videos and Streams tabs (anything under 5 minutes passed over); a show on both,
in both. Titles are matched against the phrase ignoring case,
accents and punctuation, in any script ("Night Sky" finds "NIGHT-SKY"). YouTube's listing has no
dates, so each matching video is looked up (the newest RESOLVE_LIMIT at most) for its date and to
recognise one already matched on Apple. Each match goes to the podcast queue as an ordinary episode
link (Apple: the US catalogue first), which skips episodes already in the library or the queue.
Up to 10 matches are queued at once; with more, they are listed and nothing is queued unless --all
is given. --language: the language the episodes are in, when the queue asks.

Periods (by release date): "this year", "last year", "this month", "last month", "this week",
"last week", "last 3 months", "last 2 weeks", "last 10 days", "2026", "2026-09", "since 2026-09-01".
"""
from __future__ import annotations

import argparse
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
import languages  # noqa: E402
import podshows  # noqa: E402
import sources  # noqa: E402
from sources import youtube  # noqa: E402

AT_ONCE = 10
RESOLVE_LIMIT = 40                     # matching videos looked up for their dates, newest first
LISTING_LIMIT = 5000                   # videos read from a playlist or channel (a long channel: ~15 s)


def words(text: str) -> str:
    """Lower-case letters and digits of any script separated by single spaces; accents and
    punctuation dropped."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", plain.casefold()))


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


def matched(count: int) -> str:
    return f"{count} episode matches" if count == 1 else f"{count} episodes match"


def target(text: str) -> dict:
    """What to search for the show the user means: {key, title, apple: (show id, storefront) or None,
    youtube: {channel, playlist} or None}. LookupError when it cannot be told; ValueError for a link
    that is not a show's."""
    text = text.strip()
    registry = podshows.load()
    if re.search(r"podcasts\.apple\.com", text):
        show, _, country = podfetch.parse_apple_url(text)
        entry = registry.get(show) or {}
        return {"key": show, "title": entry.get("title", ""), "language": entry.get("language", ""), "apple": (show, country or "us"),
                "youtube": podshows.youtube_link(entry, show) or None}
    if re.search(r"youtube\.com|youtu\.be", text):
        ref = sources.parse(text)
        if ref.episode:
            raise ValueError("That is a video link: give the show's channel, @handle or playlist link.")
        try:
            key = podshows.find(registry, text)
        except LookupError:
            found = youtube.channel(ref)                # a show not in the registry yet
            key = youtube.show_key(found["channel"])
            entry = registry.get(key) or {}
            return {"key": key, "title": entry.get("title") or found["title"], "language": entry.get("language", ""),
                    "apple": (key, entry.get("country") or "us") if key.isdigit() else None,
                    "youtube": {"channel": found["channel"], "playlist": found["playlist"]}}
    else:
        key = podshows.find(registry, text)
    entry = registry[key]
    linked = podshows.youtube_link(entry, key) or None
    return {"key": key, "title": entry.get("title", ""), "language": entry.get("language", ""),
            "apple": (key, entry.get("country") or "us") if key.isdigit() else None,
            "youtube": linked if (linked or {}).get("channel") else None}


def youtube_matches(found: dict, wanted: str, first: date, last: date, spoken: str = "") -> tuple[list[dict], int, list[str]]:
    """Matching videos of the show's playlist (or channel), looked up: ([{released, title, link, peek}],
    videos searched, lines for matches that could not be had or were left unchecked). The listing
    gives titles machine-translated into English; for a show in another language (spoken) it is read
    in that language too, and a video matches on either title."""
    linked = found["youtube"]
    videos = youtube.recent_videos(linked["channel"], linked.get("playlist") or "", count=LISTING_LIMIT)
    titles = {v["id"]: [v["title"]] for v in videos}
    if spoken and spoken != languages.DEFAULT:
        for other in youtube.recent_videos(linked["channel"], linked.get("playlist") or "", count=LISTING_LIMIT, lang=spoken):
            titles.setdefault(other["id"], []).append(other["title"])
    hits = [v for v in videos if any(f" {wanted} " in f" {words(title)} " for title in titles[v["id"]])
            and not v["signed_in"] and not 0 < v["seconds"] < podmonitor.MIN_SECONDS]
    notes = [f"? {v['title']} (the newest {RESOLVE_LIMIT} matches only: narrow the phrase or the period)"
             for v in hits[RESOLVE_LIMIT:]]
    matches = []
    for video in hits[:RESOLVE_LIMIT]:
        try:
            info = youtube.video_info(video["id"])
        except sources.SourceError as error:
            notes.append(f"? {video['title']} ({error})")
            continue
        day = youtube.published_day(info)
        released = date.fromisoformat(day) if day else None
        if released is not None and not first <= released <= last:
            continue
        if float(info.get("duration") or 0) < podmonitor.MIN_SECONDS:
            continue
        matches.append({"released": released, "title": str(info.get("title") or video["title"]), "source": "YouTube",
                        "link": youtube.watch_url(video["id"]),
                        "peek": youtube.peek_from_info(info, found["key"], video["title"])})
    return matches, len(videos), notes


def search(show_text: str, phrase: str, when: str = "", queue_all: bool = False, dry_run: bool = False,
           language: str | None = None) -> str:
    wanted = words(phrase)
    if not wanted:
        return "Give a word or phrase to look for in the episode titles."
    first, last = period(when)
    try:
        found = target(show_text)
    except ValueError as error:
        return f"Not a show link: {error}"
    matches, searched, notes = [], [], []
    if found["apple"]:
        show, country = found["apple"]
        episodes = podmonitor.feed_episodes(show, country)
        for item in episodes:
            released = item.published.astimezone(timezone.utc).date() if item.published else None
            if f" {wanted} " in f" {words(item.title)} " and (released is None or first <= released <= last):
                matches.append({"released": released, "title": item.title, "source": "Apple", "item": item,
                                "peek": sources.apple.peek_from_feed(item, found["key"], podmonitor.FEED_LANGUAGE.get(show, ""))})
        searched.append(f"{len(episodes)} episodes on Apple")
    if found["youtube"]:
        spoken = (languages.code(language) if language else None) or found["language"] or \
            languages.guess(phrase, least=3)            # a phrase in a non-Latin script says its language
        videos, count, notes = youtube_matches(found, wanted, first, last, spoken or "")
        apple = [m for m in matches if m["source"] == "Apple"]
        for video in videos:                        # one episode on both sources is listed once, from Apple
            twin = sources.twin_among(video["peek"], [(m, m["peek"]) for m in apple], found["title"])
            if twin:
                twin["also"] = " (also on YouTube)"
            else:
                matches.append(video)
        searched.append(f"{count} videos on YouTube")
    matches.sort(key=lambda match: match["released"] or date.min)
    scope = f'"{phrase}"' + (f", {when.strip()}" if when.strip() else "")
    where = " and ".join(searched)
    if not matches:
        return "\n".join([f"No episode titles match {scope} ({where} searched)."] + notes)

    def line(match: dict) -> str:
        label = f" [{match['source']}]" if found["apple"] and found["youtube"] else ""
        return f"{match['released'] or 'undated'} — {match['title']}{label}{match.get('also', '')}"
    if dry_run or (len(matches) > AT_ONCE and not queue_all):
        head = (f"{matched(len(matches))} {scope}" +
                ("" if dry_run else f"; more than {AT_ONCE}, so none queued yet — say \"queue all\" to add them") + ":")
        return "\n".join([head] + [line(m) for m in matches] + notes)
    apple = [m for m in matches if m["source"] == "Apple"]
    links = podmonitor.matched_links(*found["apple"], [m["item"] for m in apple],
                                     podmonitor.apple_episodes(*found["apple"])) if apple else {}
    queued, skipped, missing = [], [], []
    for match in matches:
        if match["source"] == "Apple":
            link = links.get(match["item"].guid)
            if not link:
                missing.append(line(match))
                continue
        else:
            link = match["link"]
        answer = podqueue.add(link, source="search", language=language, peek=match["peek"])
        (queued if answer.startswith("Queued") else skipped).append(f"{line(match)} ({answer.rstrip('.')})")
    report = [f"{matched(len(matches))} {scope}: {len(queued)} queued, {len(skipped)} skipped"
              + (f", {len(missing)} not found on Apple" if missing else "") + "."]
    report += [f"+ {l}" for l in queued] + [f"= {l}" for l in skipped] + [f"? {l}" for l in missing] + notes
    return "\n".join(report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("show")
    parser.add_argument("phrase")
    parser.add_argument("--period", default="")
    parser.add_argument("--all", action="store_true", help=f"queue every match, even more than {AT_ONCE}")
    parser.add_argument("--dry-run", action="store_true", help="list the matches, queue nothing")
    parser.add_argument("--language", default=None, help="the language the episodes are in, if the queue asks")
    args = parser.parse_args()
    try:
        print(search(args.show, args.phrase, args.period, args.all, args.dry_run, args.language))
    except ValueError as error:
        print(str(error))
        return 1
    except LookupError as error:
        print(f"Could not read the show: {error}")
        return 1
    except sources.SourceError as error:
        print(f"Could not read the show on YouTube: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
