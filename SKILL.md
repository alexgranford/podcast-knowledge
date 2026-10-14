---
name: podcast-knowledge
description: Use when the user gives an Apple Podcasts episode URL and wants a knowledge note (Markdown, for example in an Obsidian vault).
version: 5.21.0
required_environment_variables:
  - name: OMLX_BASE_URL
    prompt: Address of the model server (oMLX or any OpenAI-compatible server), e.g. http://<host>:8000
    help: "The server that runs the speech and text models; see README, Models."
  - name: OMLX_API_KEY
    prompt: API key of the model server
    help: "oMLX requires a key; for a server without authentication any non-empty value works."
  - name: PODNOTE_BRAVE_API_KEY
    prompt: Brave Search API key, for checking claims and naming cited books on the web (optional)
    help: "https://brave.com/search/api/ - the skill's own name: Hermes never passes its own BRAVE_SEARCH_API_KEY to skill scripts. The same key value works."
    optional: true
---

# Podcast Knowledge

Episode links go into a queue; a Hermes cron job (`podcast-queue`, script mode, no model) runs
podnote on one episode at a time and messages the user on Telegram when each note is ready or has
failed. The agent's only job is to put the link in the queue. See README.md for how it works.

`<python>` below is the Python that has the skill's requirements installed: in the Hermes Docker image
`/opt/hermes/.venv/bin/python3` (a bare `python3` there lacks them, so a command can fail halfway,
for example after `podshows.py monitor` has saved the registry but before it rewrites `Shows.md`).
`<skill-dir>` is this skill's folder.

## When the user sends an Apple Podcasts episode link

```bash
<python> <skill-dir>/scripts/podqueue.py add "<apple-episode-url>" --source telegram
```

Reply with the single line it prints (for example "Queued (position 2 of 3)."), and nothing else.
Several links in one message: run `add` once per link and reply with one line per link. A show link
(no `?i=`) is refused by `add`; tell the user it needs an episode link.

Do not run podnote.py yourself for a link, and do not wait for the note: the queue sends its own
Telegram message when the note is written.

## Queue a show's episodes by title

"Queue the episodes of <show link> with <words> in the title [this year / last 3 months / 2026-09 / …]":

```bash
<python> <skill-dir>/scripts/podsearch.py "<apple-show-url>" "<words>" --period "<period, if given>"
```

Reply with what it prints. Up to 10 matches are queued at once; with more it lists them and queues
nothing: show the list and ask; on "queue all", run it again with `--all`. "Which episodes would that
be?" → the same command with `--dry-run`. Periods: this/last year, month or week; "last N days, weeks,
months or years"; a year ("2026"), a month ("2026-09"), or "since 2026-09-01".

## Shows: following them and correcting their hosts

Every show the user has had processed is in the show registry; `Shows.md` in the podcasts notes folder lists it.
Hosts, publishers, name fixes and advertisers maintain themselves; the user only corrects. Run, reply
with the single line it prints:

| The user says | Run |
| --- | --- |
| "monitor / follow <show or show link>" | `<python> <skill-dir>/scripts/podshows.py monitor <show>` |
| "stop monitoring / unfollow <show>" | `<python> <skill-dir>/scripts/podshows.py unmonitor <show>` |
| "the hosts of <show> are A and B" | `<python> <skill-dir>/scripts/podshows.py hosts <show> = A, B` |
| "C co-hosts <show>" / "C is a regular guest host of <show>" | `<python> <skill-dir>/scripts/podshows.py cohost <show> = C` |
| "C is not a host of <show>" | `<python> <skill-dir>/scripts/podshows.py not-host <show> = C` |
| "which shows do I follow?" | `<python> <skill-dir>/scripts/podshows.py list` |

`<show>` may be a fragment of its name or its Apple Podcasts link; to follow a show never processed
before, give its show link (the one without `?i=`). Only episodes released after that are queued.
Never edit `shows.json` or `Shows.md` by hand.

## Other requests

- "What's in the queue?" → `<python> <skill-dir>/scripts/podqueue.py list`, reply with its output.
- "Process it now" / "re-make the note" for an episode already transcribed, only on explicit
  request: `<python> <skill-dir>/scripts/podnote.py --job-dir <cache>/jobs/podcast-<id> --force`
  (no download, no ASR), then the validation pass on the note it prints after `NOTE:`:
  `<python> <skill-dir>/scripts/podverify.py "<note path>"`. Run each once, wait silently, reply once
  with the note's filename stem and status. Never re-run after any outcome without the user asking.

## Agent rules

1. Never edit a produced note. Never add retries: the queue already retries episodes that could not
   be processed (at 1, 3, 6 and 12 hours) and reports the final verdict itself.
2. A recurring misheard name that is wrong in every show, or a sponsor phrase that slips through →
   one line in `names.txt`. Hosts belong to the show registry (above), not to `names.txt`.
3. If a manual run you started is interrupted and its process survives, stop only that process (its
   own PID). Never use `pkill`, and never delete lock files: the system releases them.

Do not reintroduce inside podnote: claim extraction, fact-checking, semantic validators, or model
repair calls. Queueing, show monitoring and the validation pass live outside podnote's single run.
The one check inside it is the note check, run before every write: the notes folder's own metadata
validator if it has one (`_meta/tools/metadata_contract.py`), else the skill's basic check
(`scripts/notecheck.py`); a note it rejects is not written.
