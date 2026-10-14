# podcast-knowledge (podnote)

Turn an Apple Podcasts episode into one durable Markdown note: who took part, a short summary with
an overall take, the key themes as a synthesis of the whole conversation, and the works and quotes
it cites. All model work runs on a local model server: no audio, transcript or note goes to a cloud
model. Only the optional validation pass sends short search queries to web services.

```
podnote.py "https://podcasts.apple.com/gb/podcast/<show>/id<id>?i=<episode-id>"
→ NOTE: ~/Notes/Podcasts/Show/2026-09-16 - Episode.md
  STATUS: complete
```

## Quick start

1. A Mac with Apple Silicon (24 GB) runs [oMLX](https://github.com/jundot/omlx) with
   `Qwen3-ASR-1.7B-8bit` and `gemma-4-26B-A4B-it-qat-4bit`, and an API key.
2. On the machine that runs the scripts (Python 3.11+, `ffmpeg`):
   ```bash
   git clone https://github.com/alexgranford/podcast-knowledge.git && cd podcast-knowledge
   python3 -m pip install -r requirements.txt
   cp .env.example .env && chmod 600 .env       # set OMLX_BASE_URL, OMLX_API_KEY, PODNOTE_ROOT
   python3 scripts/setup_vad.py                 # the speech-detection runtime
   python3 scripts/podnote.py "<an Apple Podcasts episode link>"
   ```
3. Under Hermes, follow "Running under Hermes" instead of step 2's `.env`: the settings go in
   Hermes's `.env`, and three cron jobs run the queue, the show monitor and a weekly digest.

The rest of this file explains the design, the setup in detail, operations and the output.

## Why it is shaped like this

A small local model reads 2,000 words carefully and 30,000 words badly. So every model call is kept
small, and Python does everything a model does not need to do. The rules:

- **The model reads at most ~1,800 words of transcript at a time.** The transcript is split at
  sentence boundaries and each part gets its own call, which writes one bullet per distinct point
  and lists the works and quotes the part cites. A final remainder under a quarter of the limit is
  folded into the last part, so the last part can reach ~2,250 words. Larger parts were tested and
  lose detail steadily.
- **The note is built from the part notes, never from the transcript.** Two short calls over them
  write the themes and then the summary. If any call fails, no note is written and the old one stays:
  a partial note is a failure. The finished part notes stay cached, so the next run redoes only what
  failed.
- **References are assembled by Python**, not by a model: the parts' tagged works and quotes,
  de-duplicated, in episode order. They are raw — titles and quotes come from automatic
  transcription — and are cleaned by the separate validation pass.
- **No scores and no truth judgements inside this run.** Checking claims needs external sources;
  that is the separate validation pass (`podverify.py`).
- **No retries, no repair calls, no scheduler, no semantic validators inside this run.** One
  process exits 0 or 1. The only checks are mechanical: did the model stop naturally, is there
  text. Anything that feeds it episodes (a queue, a show monitor) lives outside it.

`scripts/podnote.py` (about 1,100 lines) is the whole note pipeline, over a separate acquisition and
speech-recognition layer.

## Recommended setup

```
  you ── Telegram ──►  Hermes agent  (Docker, on an always-on box: NAS or small server)
                         │  .env: every key and setting
                         │  cron jobs, script mode, no model:
                         │    podcast-queue (5 min) · podcast-monitor (30 min) · podcast-shows-digest (weekly)
                         ▼
                 podqueue.py ──► podnote.py ──► podverify.py
                      │              │               │
                      │              │               ├──► Wikipedia / Wikidata · Open Library · OpenAlex (free)
                      │              │               └──► Brave Search: claims and book names     optional key
                      │              ├──► Apple Podcasts: lookup, RSS feed, audio
                      │              ▼
                      │      oMLX on an Apple Silicon Mac (24 GB), same LAN
                      │        Qwen3-ASR-1.7B-8bit: speech to text
                      │        gemma-4-26B-A4B-it-qat-4bit: everything else
                      ▼
               notes folder (Markdown; an Obsidian vault works well, synced to your devices)
```

| component | role | notes |
| --- | --- | --- |
| [Hermes agent](https://hermes-agent.nousresearch.com) | takes episode links on Telegram, runs the cron jobs, sends one message per finished or failed episode | the skill lives in its skills folder; keys and settings in Hermes's `.env` |
| [oMLX](https://github.com/jundot/omlx) | serves both models over an OpenAI-compatible API | one model loaded at a time (the model arbiter), so the memory budget holds; a key is required, and plain HTTP is accepted only on private addresses |
| notes folder | where notes are written | any local folder, mounted into the container; see "Where notes go" |
| state and cache folders | queue, show registry, logs; audio, transcripts, model output per episode | outside the notes folder; the cache keeps audio and transcripts per episode, so plan for tens of GB |
| Silero VAD runtime | finds speech pauses to cut the audio into windows | its own small venv (`onnxruntime`, `numpy`); the model is bundled in `assets/` |
| web services | evidence for the validation pass | Wikipedia, Wikidata, Open Library and OpenAlex need no key; Brave Search (one optional key) adds web evidence for claims and the real names of cited books |

The heavy work (speech recognition and the text model) runs on the Mac; the box running Hermes only
downloads audio, cuts it and moves text, so it needs no GPU. Without Hermes the scripts run the same
from a shell or any scheduler; only the Telegram messages are Hermes's.

## Models

The preferred inference server is [oMLX](https://github.com/jundot/omlx), on Apple Silicon. Two models
do all the work:

- **`Qwen3-ASR-1.7B-8bit`** turns speech into text, window by window, with a hint of the show's names
  (see "Name hints"). It is small enough (1.7B parameters, 8-bit) to leave the memory to the text model.
- **`gemma-4-26B-A4B-it-qat-4bit`** (Google's quantization-aware-trained 4-bit Gemma 4, a 26B mixture
  of experts with about 4B active parameters) does everything else: part notes, themes, summary, tags
  and the validation pass.

Considerable testing went into choosing these two for a 24 GB Apple Silicon Mac, where the text model
peaks near 19 GB:

- On a six-episode comparison the QAT Gemma made far fewer garbled words than the plain
  `gemma-4-26b-a4b-it-4bit` conversion (about 1–2 against 6) and kept more of each episode in its
  themes, for about 30% more time.
- The 26B mixture of experts was about five times faster than a dense 9B model and attributed
  arguments to speakers more reliably; a 12B Gemma also worked.
- Sampling: Gemma's own defaults (temperature 1.0, top_p 0.95, top_k 64), no presence or frequency
  penalty (it mangled repeated words), thinking off (about five times the time, no clear gain). The
  comment in `podnote.ask()` records what each alternative did.
- Transcript parts of about 1,800 words: larger parts lost detail steadily.

Other OpenAI-compatible servers and models work (`OMLX_BASE_URL`, `PODNOTE_MODEL`, or
`PODNOTE_BASE_URL` for the text model only), but these are the tested ones.

## Who said what

Automatic transcription has no speaker labels, so attribution comes from the text:

- The episode description is cleaned (links, sponsor reads and calls to action removed; guest
  introductions and chapter lists kept, even when show notes glue them to a sponsor link) and given
  to every part call and to the summary.
- The part prompt credits a point to a person only when the text shows it is theirs, treats whoever
  is addressed by name as the listener, and never guesses between several hosts.
- The themes call must keep attributions exactly as the part notes give them.
- The summary lists the hosts who appear in the notes (all of a show's hosts if the notes name
  none, since hosts are often never addressed by name), then the guests from the description.

On a six-episode test with 17 attributions checked against the transcripts: 12 right, 2 wrong,
3 left unattributed. The errors sit in fast exchanges that cannot be resolved from text alone.

## Requirements

- Python 3.11+ and `ffmpeg` (with `ffprobe`) on the machine that runs the scripts
- A model server with an OpenAI-compatible API serving both models (see "Models"): oMLX on an Apple
  Silicon Mac with 24 GB or more is the tested setup
- Network access to Apple Podcasts (lookup, feeds, audio) and, for the validation pass, to Wikipedia,
  Wikidata, Open Library, OpenAlex and (optionally) Brave Search
- Disk for the cache: audio and transcripts are kept per episode (tens of GB over time)

| file | what it installs | where |
| --- | --- | --- |
| `requirements.txt` | `requests`, `jsonschema`, `PyYAML` | the Python that runs the scripts |
| `requirements-vad.txt` | `onnxruntime`, `numpy` | the speech-detection venv; `scripts/setup_vad.py` installs it |
| `requirements-dev.txt` | the above plus `pytest` | only for running the tests |

## Setup

### 1. Get the code and its packages

```bash
git clone https://github.com/alexgranford/podcast-knowledge.git && cd podcast-knowledge
python3 -m pip install -r requirements.txt
ffmpeg -version                                # must work; install ffmpeg first if it does not
```

### 2. Prepare the model server

Run [oMLX](https://github.com/jundot/omlx) (or another OpenAI-compatible server) where the scripts can
reach it, give it an API key, and make both models available under these ids:

- `Qwen3-ASR-1.7B-8bit` (speech recognition; override with `OMLX_ASR_MODEL`)
- `gemma-4-26B-A4B-it-qat-4bit` (everything else; override with `PODNOTE_MODEL`)

The skill loads and unloads them itself, one at a time. Plain `http://` is accepted only for a private
or loopback address; anything else needs `https://`.

### 3. Settings and keys

```bash
cp .env.example .env && chmod 600 .env       # then fill it in (under Hermes: see "Running under Hermes")
cp names.example.txt names.txt               # optional: your own fixes for misheard names
```

Required: `OMLX_BASE_URL` and `OMLX_API_KEY`. Worth setting: `PODNOTE_ROOT` (where notes go),
`PODNOTE_CACHE`, `PODNOTE_STATE`, `PODNOTE_VAD_RUNTIME` (all folders the scripts can write to),
`PODNOTE_CONTACT` (asked for by Wikipedia and Open Library) and, optionally, `PODNOTE_BRAVE_API_KEY`.

### 4. The speech-detection runtime

```bash
python3 scripts/setup_vad.py                 # creates PODNOTE_VAD_RUNTIME/venv and its readiness record
```

It installs `requirements-vad.txt` into its own venv, runs a self-test on the bundled Silero model and
writes `READY.json` with checksums of the model, `scripts/podcast_vad.py`, the window policy and the
installed versions. Transcription checks that record before every job, so after an update that changes
one of those files it stops with "VAD runtime readiness/hash validation failed": run the script again.

### 5. Check and run

```bash
python3 -m pip install -r requirements-dev.txt && python3 -m pytest -q tests     # no model server needed
python3 scripts/podnote.py "https://podcasts.apple.com/us/podcast/<show>/id<id>?i=<episode-id>"
python3 scripts/podverify.py "<the note path it printed after NOTE:>"
```

The tests use invented data and need neither the model server nor your settings; a few that need the
speech runtime or `ffmpeg` are skipped without them.

Configuration is environment variables; the model server's address and key are required:

| variable | default | meaning |
| --- | --- | --- |
| `OMLX_BASE_URL` | `http://127.0.0.1:8000` | model server |
| `OMLX_API_KEY` | — | required; any non-empty value if your server has no auth |
| `OMLX_ASR_MODEL` | `Qwen3-ASR-1.7B-8bit` | speech recognition model id |
| `PODNOTE_MODEL` | `gemma-4-26B-A4B-it-qat-4bit` | synthesis model id |
| `PODNOTE_ROOT` | `~/Notes` | the folder notes go under: any local folder (an Obsidian vault works well) |
| `PODNOTE_NOTES_DIR` | `Podcasts` | podcasts folder inside it: one folder per show, plus `Podcasts.base` and `Shows.md` |
| `PODNOTE_CACHE` | `/var/tmp/podnote` | job working directory (audio, transcript, part notes) |
| `PODNOTE_STATE` | `/var/tmp/podnote-state` | queue, show registry, monitor state, logs, locks and the ASR database |
| `PODNOTE_VAD_RUNTIME` | `/var/lib/podnote/vad-runtime` | VAD venv and readiness record |
| `PODNOTE_KEEP_DAYS` | `30` | days an episode's transcript and working files are kept after their last use (audio goes as soon as the transcript is complete) |
| `PODNOTE_SECTION_WORDS` | `1800` | transcript words per part; for experiments |
| `PODNOTE_BASE_URL`, `PODNOTE_API_KEY` | — | optional: write notes with any OpenAI-compatible service instead of local oMLX (speech recognition still needs oMLX) |
| `PODNOTE_BRAVE_API_KEY` | — | optional: web search (Brave Search API) for claim evidence and the names of cited books |
| `PODNOTE_CONTACT` | — | your e-mail or URL, sent in the User-Agent to Wikipedia, Open Library and the others, as they ask of automated clients |
| `PODNOTE_SCRIPT_TAG` | — | the tag your notes folder gives notes written by scripts (used on `Shows.md`) |
| `PODNOTE_TEST_RULES` | — | tests only: your folder's `_meta` folder, to test against its own metadata standard |

Every key and setting is read from the environment, from one place:

- **Under Hermes:** Hermes's own `.env` (`~/.hermes/.env`; in Docker `/opt/data/.env`). Hermes loads
  it and hands the settings to the cron jobs. The skill folder then needs no `.env`. The keys and
  settings the skill needs are declared in `SKILL.md` (`required_environment_variables`), so Hermes
  reports any that are missing and can ask for them.
- **Elsewhere:** `.env` in the skill folder, copied from `.env.example` (git-ignored, keep it
  `chmod 600`). `scripts/settings.py` loads it for every script and the tests, and never overrides a
  variable already set, so the shell still wins.

## Running under Hermes

1. Put the skill in Hermes's skills folder, for example `~/.hermes/skills/media/podcast-knowledge/`
   (in Docker `/opt/data/skills/…`), and install `requirements.txt` into the Python Hermes runs tools
   with (in the Hermes Docker image, `/opt/hermes/.venv`; add the packages and `ffmpeg` to the image
   so a rebuild keeps them).
2. Put the settings in Hermes's `.env`, not in the skill folder: `OMLX_BASE_URL`, `OMLX_API_KEY`,
   `PODNOTE_ROOT`, `PODNOTE_CACHE`, `PODNOTE_STATE`, `PODNOTE_VAD_RUNTIME`, `PODNOTE_CONTACT`, and
   `PODNOTE_BRAVE_API_KEY` if you use Brave. Hermes rereads `.env` for every cron run; no restart needed.
3. Build the speech-detection runtime once, with the same Python: `scripts/setup_vad.py`.
4. Copy the three wrappers from `hermes/` to `~/.hermes/scripts/` (in Docker `/opt/data/scripts/`) and
   create the cron jobs. Each runs a script with no model and sends its output to Telegram; empty
   output sends nothing:

   ```bash
   hermes cron create "*/5 * * * *" --name podcast-queue --script podcast-queue.py --no-agent --deliver telegram
   hermes cron create "7,37 * * * *" --name podcast-monitor --script podcast-monitor.py --no-agent --deliver telegram
   hermes cron create "15 7 * * 1" --name podcast-shows-digest --script podcast-shows-digest.py --no-agent --deliver telegram
   ```

5. Send an Apple Podcasts episode link to Hermes on Telegram. The agent queues it (see `SKILL.md`), and
   within a few minutes the queue job starts; a message follows when the note is written or has failed.

The wrappers find the skill under `~/.hermes/skills/` (or `PODNOTE_SKILL_DIR`) and run it on the Python
that has its requirements (`PODNOTE_PYTHON`, else the Docker image's `/opt/hermes/.venv`), because
Hermes may start cron scripts on a managed environment of its own that lacks them. The notes folder
must be mounted into the container if Hermes runs in Docker; an Obsidian vault kept in sync on your
devices works well.

## Updating

```bash
git pull
python3 -m pip install -r requirements.txt
python3 scripts/setup_vad.py                  # only needed if a job reports the readiness check failing
python3 -m pytest -q tests
```

Settings, `names.txt`, the queue, the show registry and the notes are untouched by an update.

## Usage

```bash
scripts/podnote.py "<apple-podcasts-episode-url>"       # full run
scripts/podnote.py --job-dir <dir> --force              # re-make the note from a saved transcript
scripts/podnote.py --job-dir <dir> --force --model X    # same, on a different model
scripts/podnote.py "<url>" --force --retranscribe       # transcribe again (earlier transcript set aside)
```

**Name hints for the ASR model.** Every window is sent with a hint: show, hosts (show registry), the names
in the episode title and description, and the cleaned description. Qwen3-ASR uses it as context, so
"Siobhan", "Niamh" and "Maya" come out right instead of "Shivon", "Neeve" and "Maia". A window whose
answer repeats the hint or is implausibly short for its length is transcribed again: with a shorter
hint (description cut to 600 characters), then without one. Qwen3-ASR sometimes reads a hint back
instead of the speech (seen in about 1 window in 7); which hint length does it differs from window to
window, so no length is safe and the check is what matters. Each accepted window records `asr_hint`
(`used`, `used (shorter hint)`, `dropped: <why>` or `none`). `--retranscribe` moves `asr/` and `podnote/` into
`previous-<time>/` and runs ASR under a fresh state record, so an earlier transcript is never lost.

Every model output (part notes, themes, summary) is cached per job and per model under
`<job>/podnote/<model>/`. A `--job-dir` rerun on the same model makes no model calls; delete a file
there to redo that one call, or the directory to redo them all. `--model X` writes its own cache,
so comparing models never mixes their output. The cache records what it was made from (`inputs.sha256`: the transcript and the
prompts); after a new transcript or a prompt change it is set aside automatically, not reused.

`STATUS: complete` means the note was written (or was already up to date). `STOPPED: <reason>` means
no note was written: an unindexed episode, an unreachable model server, a model call that did not
finish (`note incomplete: …`), a note that fails the check. Nothing partial is ever written, and the
old note stays. There is no traceback and no retry; finished part notes stay on disk, so rerunning
resumes from where it stopped. Exit code is 0 only for `STATUS: complete`.

## Queue, shows and validation

podnote stays a single run. Three small programs around it, all run by Hermes cron jobs in script
mode (no model, no cost), decide what it runs and check what it wrote:

| program | Hermes cron job | what it does |
| --- | --- | --- |
| `scripts/podqueue.py` | `podcast-queue`, every 5 min | runs podnote on the next queued link, then the validation pass; one episode per tick; Telegram message per finished or failed episode, silence otherwise |
| `scripts/podmonitor.py` | `podcast-monitor`, at :07 and :37 | checks the monitored shows for new episodes and queues them |
| `scripts/podshows.py digest` | `podcast-shows-digest`, Mondays 07:15 | weekly summary of show registry changes (silent when none) |
| `scripts/podverify.py` | (called by the queue) | validates names, works, quotes and key claims against external sources |

Each cron job calls a small wrapper from `hermes/`, copied to `~/.hermes/scripts/` (Hermes only runs
cron scripts from there); see "Running under Hermes".

**Queue.** `podqueue.py add <apple-episode-url>` appends a link (the Hermes agent does this when a
link arrives on Telegram); `podqueue.py list` shows it. Files in `PODNOTE_STATE`: `queue.txt`
(bare links, editable by hand), `queue-state.json` (retry bookkeeping), `done.txt`, `failed.txt`.
An episode that could not be processed — not indexed by Apple yet, model server unreachable, ASR
busy — is retried 1, 3, 6 and 12 hours after its first failure, then reported as failed. Another
podnote run holding the lock is not a failure; the link just waits. An incomplete note is a failure
(the old note stays), reported at once. A verification pass that fails (a source or the model was
unavailable) is retried on its own, without remaking the note, on the same schedule
(`verify-retry.json`); this is silent on Telegram and logged in `done.txt` or `failed.txt`. A run that
dies mid-way (a timeout or a crash) has the models it left loaded on the server unloaded.

**Cache.** Each episode's working folder (`PODNOTE_CACHE/jobs/podcast-<id>/`) keeps the transcript,
the part notes and the processing records. The audio (the download and the cut windows) is deleted as
soon as the transcript is complete; the folder itself goes once it has not been used (transcribed,
written or verified) for `PODNOTE_KEEP_DAYS` days, 30 by default. The queue does this at most once a
day (`cache-cleanup.log`); episodes in the queue are left alone, and the notes are never touched.
Re-making a note from a kept transcript needs no download; after the folder has gone, the episode is
downloaded and transcribed again.

**Ideas.** Part notes tag named theories, principles, laws, effects, paradoxes, fallacies, biases,
thought experiments, models, ideologies and doctrines (`[idea]`). The validation pass keeps an idea when
Wikipedia's top hit is that very idea (same title; its description or Wikidata type says theory,
principle, ideology…), takes Wikipedia's spelling of the name and links the article; an entry filed
as a work that Wikipedia describes as an idea ("Broken window theory") moves to Ideas instead of
being removed. Without evidence an idea stays as it is.

**Show registry (`podshows.py`, `PODNOTE_STATE/shows.json`).** Every show ever processed, keyed by its
Apple show id, with facts about the show only: folder name, publisher, feed, Wikidata item, monitor
flag, hosts, show-scoped name fixes and advertisers. Episodes are not stored; they are the notes in the
show's folder. Hosts maintain themselves from evidence, with no approvals:

- strong sources add a host at once: the user (locked, never changed automatically), the feed's
  `podcast:person` host tags, Wikidata's presenter (looked up by Apple id), a full name in the show's
  title ("Signals with Omar Haddad") and the feed author when it is a person;
- episodes add the rest: whoever the notes list as host, or who introduces themselves at the start or
  signs off at the end ("Hi, I'm…", "with me, Tomas Brandt", "Thanks for listening. I'm Iris
  Fleming"), in 3 episodes;
- a role follows the share of recent episodes (last 20, or 30 days if longer): about half or more is a
  regular host, 10–50% a recurring co-host (a weekly guest presenter on a daily show);
- a regular host missing from 8 recent episodes for 60 days, or a co-host missing for three of their
  usual gaps (60 days at least), becomes former with an end date: nobody is deleted, and older
  episodes keep their hosts;
- a name corrected by the validation pass in 2 episodes of a show becomes that show's name fix;
  advertisers named in show notes are kept per show for 90 days and named in the part-note prompt.

Every change is logged (`shows-changes.log`) and summarised in a weekly Telegram digest, sent only when
something changed. The user corrects through Hermes ("hosts of X are A, B", "C is not a host of X",
"monitor X"). `Shows.md` in the podcasts notes folder is a generated, read-only overview.

**Monitoring.** Shows flagged `monitor` in the registry. For each: Apple lookup → RSS feed → episodes
not seen before; an episode's RSS GUID is matched to Apple's `episodeGuid` to get an ordinary episode
link for the queue. Not on Apple yet → tried again next check, for up to 3 days. A newly monitored show
has its back catalogue marked as seen; episodes under 5 minutes (trailers) are skipped. State:
`PODNOTE_STATE/monitor-state.json`.

**Validation pass (`podverify.py`).** One pass after every note; sources decide, the model only
reads. One model session: an inventory call (work types, clean titles, up to six checkable claims
with a search query each), a names-and-works verdict call and a claims verdict call, each shown only
gathered evidence. Sources: the episode description, Wikipedia search and Wikidata "instance of"
(what a thing is), books named first by a general web search (Brave Search:
the model reads the results and gives the title and author as they spell them, so a misheard or only
described book gets its real name), then Open Library (canonical record and a link to the work page on
the latest English print edition; an Amazon UK book search when Open Library has no record), Wikipedia full-text
search, OpenAlex (research papers and reports) and Brave Search (`PODNOTE_BRAVE_API_KEY`) for claims, and the
transcript for quotes. Python guards every edit: a name only to a spelling variant, a title only to
a variant of the same title, a work removed only when Wikidata/Wikipedia say the same title is not a
work, or when the inventory reads it as not a work and no source says it is one; an author only
when the entry itself names them and they are not part of the title. A claim gets a verdict
(supported / disputed) only with cited evidence; a claim without one is left out of the note. A dispute must also
survive a second, narrow question — the model copies the contradicting sentence, which must really
be in the evidence — and evidence that names only other years than the episode's does not count
against a claim that names none. Everything the model saw and decided is kept in the job folder as
`verify-record.json`.

Wikimedia rate-limits by User-Agent, one budget for Wikipedia, Wikidata and REST, reset at each
clock minute: 10 requests/min without contact details, far more with them (the 2026 tiers say 200),
so `AGENT` carries a contact address. A pass makes at most ~65 Wikimedia calls. Should a 429 come
anyway, lookups wait as asked, and a host that keeps refusing is skipped for the rest of the pass
(the log then says "Wikipedia did not answer"), so a throttled source never stalls the queue.
Nothing is removed without evidence. Open Library allows 3 requests a second to clients that
identify themselves, so its calls are spaced 0.35 s apart. Google Books is not used: since late
September 2026 its field searches (`intitle:`, `inauthor:`, `isbn:`) return nothing, and its
volumes do not tell print ISBNs from e-book ones.

The note gets References grouped by type (Books as a table with Open Library links, Films & TV,
Papers & reports, Laws, Other works, Ideas linked to Wikipedia, Quotes as callouts), a **Claims checked** table with source
footnotes and a collapsed verification log. What the pass did is recorded in the job folder
(`verify-record.json`), not in the note's properties. A note already verified (same fingerprint) is
skipped; regenerate it with podnote to verify again. A claim's verdict reads "Supported by sources" or
"Disputed": it says what the gathered evidence shows, not that the claim is true.

Web search is one service, Brave Search (its own index; at most one request a second), used for
claim evidence and for naming books: about 4 claim queries and 2–3 book queries per episode. One
optional key, `PODNOTE_BRAVE_API_KEY`. Without it the validation pass still checks names, books and
quotes against Wikipedia, Wikidata, Open Library and OpenAlex; claims get no web evidence and a misheard
or only described book keeps the title as heard. The key has the skill's own name on purpose: Hermes
never hands its own keys (`BRAVE_SEARCH_API_KEY` and the other credentials its tools use) to skill
scripts, so a skill that calls the same service needs a key of its own. The same key value works; a
separate key keeps the skill's usage apart.

## Where notes go and how they are checked

Notes are Markdown files with YAML front matter (properties), written to `PODNOTE_ROOT`, in
`PODNOTE_NOTES_DIR/<show>/`. Any local folder works. An Obsidian vault is a good choice: the notes, their
properties and the generated `Podcasts.base` (a table of every episode) and `Shows.md` open there as
they are. Elsewhere `Podcasts.base` is just an unused file.

Every note is checked just before it is written. A note that fails is not written, and the reasons go to
the job's `note-record.json`.

- By default the skill's basic check (`scripts/notecheck.py`) runs. It checks that the properties are
  readable, that `created` and `updated` are quoted UTC times, that `model` is named, that tags are
  well spelt, and that the episode fields are present and of the right kind.
- A notes folder with a metadata standard of its own is checked by that instead. Every part is optional,
  relative to `PODNOTE_ROOT`:
  - `_meta/tools/metadata_contract.py`: a validator with the same two functions as `notecheck.py`,
    `parse_note(text)` and `validate_note(text, models, tags, profile=...)`, returning a list of
    `{"code", "field"}` issues;
  - `_meta/model-catalogue.json`: `{"verified_models": [...]}`; only these models may write notes;
  - `_meta/tag-vocabulary.json`: `{"canonical_tags": [...], "proposed_aliases": {"AI": "ai"}}`; notes
    get up to five subject tags from it (without it, no subject tags);
  - `_meta/podcast-metadata-profile.json`: the episode fields to require, replacing the built-in list.

The properties the skill writes:

```yaml
origin: agent
model: gemma-4-26B-A4B-it-qat-4bit        # the model of the latest pass
created: "2026-08-24T05:12:09"             # quoted UTC; set once, kept when the note is re-made
updated: "2026-10-08T17:02:41"             # every change, including the validation pass
tags: [science, history]                   # up to five subjects, only from the folder's tag list
show / episode / published (UTC, only when it differs from created) / hosts / guests / publisher
source_url / apple_episode_id / duration   # the podcast profile's fields; duration in seconds
```

- Re-making a note keeps `created` (and `published`, if the feed no longer gives it) from the
  episode's existing note(s). A note whose creation time was never recorded gets the time of the re-make
  (a job never fails over an unknown date).
- Whole or nothing: if any part of the note fails (a section, the themes, the summary), the note is not
  written and the old one stays; the queue reports a failure. A re-make that changes nothing is not
  written either. Completion (skill version, note path, fingerprint) is recorded only after the write.
- The verification pass fails openly (exit 1, `STOPPED:`) when it has no model, a model call fails, the
  check rejects its result, or the note changed while it ran; it is then retried, never marked verified.
- Unknown values are left out, never written empty.
- One note per episode: every other copy of the episode (older titles, restored files) goes to
  `<state>/superseded-notes/`; a different episode with the same file name gets its Apple id appended.
- `Shows.md` is a script's note: `origin: agent`, `created`, `updated`, the tag `PODNOTE_SCRIPT_TAG` plus any
  tags already on it, no `model`; a render that changes nothing leaves the file alone.
- Tags: a short model call picks up to five subjects from the folder's tag list; subjects it lacks are kept as
  suggestions in the job record, never written to the note.
- Processing state stays outside the notes folder, in the episode's job folder: `note-record.json` (skill
  version, the model that composed this version, status, problems, tags, suggestions) and
  `verify-record.json` (verification time and counts, and the fingerprint of the note it verified).

## names.txt

Global rules, applied to every note. Text after ` #` is a comment:

```
Ada Lovelase => Ada Lovelace      # fix a misheard name, in every show
!SomeSponsor                     # drop any bullet mentioning this
```

Bullets mentioning sponsors, adverts, promo or discount codes and URLs are dropped whether or not
`names.txt` exists; a `!` rule is only needed for something that slips through. Hosts and fixes that
belong to one show live in the show registry. An `@Show Name => First Host, Second Host` line still
works where the registry knows no hosts, but the registry is the place for them.

`names.txt` is gitignored — it is yours, not part of the project.

## What it produces

```markdown
---
origin: agent                  # see "Where notes go" above
model: gemma-4-26B-A4B-it-qat-4bit
created: "2026-10-07T21:14:03"
updated: "2026-10-07T21:20:40"
tags: [science, history]
show: "..."
episode: "..."
published: "2026-10-07T04:00:00"
hosts: ["Host Name"]
guests: ["Guest Name"]
publisher: "..."
source_url: https://podcasts.apple.com/...
apple_episode_id: '1000000000000'
duration: 4637
---
# Episode title

**Show** · 2026-10-07 · ⏱ 1h 17m · 🎧 [Apple Podcasts](...)

> [!abstract] Summary
> **Participants:** **Host Name** (host), **Guest Name** (guest, role)
>
> One short sentence on what is discussed (the quick summary; it is also the Telegram preview)
>
> One paragraph on what the episode is about and where it lands: its arguments and conclusions.

## Key themes
### Theme title
> The essence of the theme: where the speakers agree or clash, what is left open.

- **Lead-in** — one point per bullet, with who made it; `named concepts` in code style

## Claims checked
| Claim | Who | Verdict | Sources |      ✅ supported by sources · ⚠️ disputed (claims without a verdict are left out; no section when none has one)

## References
### Books              table: title, author, cited for, Open Library link
### Films & TV / Papers & reports / Laws / Other works
### Ideas              theories, principles, ideologies: linked to their Wikipedia article
### Quotes             quote callouts with the speaker

> [!info]- Verification log      collapsed
[^1]: source footnotes for the claims
```

Themes are a synthesis across the whole episode, grouped by subject, about one per 12 minutes
(3 to 8). References is omitted when the episode cites nothing.

## Queueing a show's episodes by title

`podsearch.py "<show link>" "<words>" [--period "this year"] [--all] [--dry-run]` reads the show's RSS
feed, matches episode titles against the words (case and punctuation ignored, whole words), filters by
release date ("this year", "last 3 months", "2026-09", "since 2026-09-01", …) and queues the matches
as ordinary Apple episode links. Up to 10 at once; more are listed and queued only with `--all`.

## Apple, hosts, quotes and folders

- Apple lookups ask the **US catalogue** first and the link's own storefront second: other storefronts lag
  the US one by hours or days, which made new episodes look "not indexed".
- The Participants line labels anyone the show registry knows as a host "host"; with exactly one host,
  "the host" in the prose becomes that person's name. A self-introduction is no evidence of hosting.
- Quotes keep only the words said: a close quote is replaced by the transcript's own words (repetitions
  included); anything else is removed.
- Show folders: the title before its first separator (":", " | ", or a spaced dash), at most 60 characters.

## Limits worth knowing

- No speaker diarization, so attribution in fast exchanges is a best guess (see "Who said what").
- Names, titles and quotes come from ASR. The validation pass corrects what external sources
  confirm; items without clear evidence stay as transcribed.
- The finished note keeps about 80% of the subjects the part notes record; minor tangents drop out.
- Occasional inverted claims (X opposing something they proposed) do occur with small models. The
  note carries no timestamps; the part notes in the job directory show roughly where each point
  came from. Treat the note as a reading aid, not a record.
- Long episodes simply produce more parts. There is no overflow path and none is needed.
- Part notes are a working artefact kept in the job directory, not part of the note.
- The ASR model stays loaded between transcription windows; a finished synthesis session unloads
  every model. If a run crashes between the two, the queue unloads what it left
  (`model_arbiter.reconcile()`); a crashed run started by hand needs that call by hand.

## Network and disk

Per episode, roughly:

| what | volume |
| --- | --- |
| Apple and the feed | 2–4 lookups and the RSS feed (cached per show); the audio once (50–150 MB, deleted after transcription) |
| model server | one ASR call per window (about 15 minutes of audio each), then one call per transcript part (about 1,800 words), themes, summary and tags; then the validation pass: three calls, plus one per cited book and one per disputed claim |
| Brave Search | about 4 queries for claims and 2–3 for books, at most one a second (optional) |
| Wikipedia, Wikidata | at most about 65 calls per validation pass, one rate budget, with a contact address in the User-Agent |
| Open Library, OpenAlex | a few calls per cited book or paper; Open Library spaced 0.35 s apart |
| disk | while transcribing, the audio and its windows (a few hundred MB); afterwards about 0.5–1 MB of text per episode, kept `PODNOTE_KEEP_DAYS` days |

Network addresses from feeds and redirects must be public: anything on the home network (loopback,
private, link-local) is refused, so a feed cannot be used to reach other machines on it.

## Troubleshooting

| symptom | likely cause | what to do |
| --- | --- | --- |
| `STOPPED: … not indexed` | Apple has not listed the episode yet | nothing: the queue retries at 1, 3, 6 and 12 hours |
| `STOPPED: model server unreachable …` | oMLX down or asleep, wrong `OMLX_BASE_URL` | start oMLX; the queue retries |
| a model stays loaded after a crash | a run died between the ASR and text sessions | the queue unloads it; by hand: `model_arbiter.reconcile()` |
| `VAD runtime readiness/hash validation failed` | an update changed the VAD script, model or policy | `python3 scripts/setup_vad.py` |
| `Audio server returned HTTP 403` after a retry | the host's signed audio link expired | handled once automatically; if it persists the episode was withdrawn |
| `STOPPED: note incomplete: …` | a model call was cut off or failed | run again: finished parts are cached; the old note stays meanwhile |
| `STOPPED: note fails the metadata check: …` | the notes folder's standard rejects a property | see the listed codes; `notecheck.py` or your `_meta` validator |
| a note was not verified | a source or the model was unavailable | retried automatically; by hand: `scripts/podverify.py "<note>"` |
| an episode sits in the queue | a retry is pending, or another run holds the lock | `scripts/podqueue.py list` shows when it is due |
| the cache grows | normal while transcribing | audio goes after each transcript; folders after `PODNOTE_KEEP_DAYS` |

## Layout

```
scripts/podnote.py              the whole note pipeline (one episode, one run)
scripts/podqueue.py             queue of episode links in front of podnote (Hermes cron)
scripts/podmonitor.py           monitored shows -> new episodes -> queue (Hermes cron)
scripts/podshows.py             show registry: hosts, publishers, fixes, advertisers; Shows.md overview
scripts/podverify.py            external validation of names, works, quotes and claims
scripts/note_prompts.py         the prompts podnote sends (part notes, themes, summary, tags)
scripts/verify_prompts.py       the prompts the validation pass sends
scripts/podsearch.py            queue a show's episodes whose titles match a phrase
scripts/notecheck.py            the basic note check (when the notes folder has no standard of its own)
scripts/podfetch.py             Apple Podcasts URL → RSS → audio URL
scripts/audio_acquisition.py    bounded download with SSRF guards
scripts/podcast_vad.py          Silero VAD CLI, runs in its own venv
scripts/podcast_asr.py          windowing policy and ASR merge
scripts/podcast_transcription.py  ASR orchestration, resumable per window
scripts/podcast_state.py        SQLite kernel for ASR progress (+ migrations/)
scripts/model_arbiter.py        exclusive model load; the text session unloads on every exit path
scripts/omlx_client.py          model server config, auth and ASR health check
scripts/omlx_text.py            model status and load/unload
scripts/omlx_request.py         one ASR window -> /v1/audio/transcriptions (run as a subprocess)
scripts/contracts.py            JSON schema validation for the ordered transcript
scripts/settings.py             loads .env into the environment
scripts/setup_vad.py            builds the speech-detection runtime and its readiness record
hermes/                         the three Hermes cron wrappers (copy to ~/.hermes/scripts/)
references/                     window and state policies, JSON schemas
assets/silero-vad/              the bundled Silero VAD model (MIT)
requirements*.txt               packages: scripts, speech-detection venv, tests
tests/                          python3 -m pytest -q tests (invented data; no model server needed)
.github/workflows/tests.yml     runs the tests on every push
```

## License

MIT, Copyright (c) 2026 Alex Granford (https://github.com/alexgranford); see `LICENSE`. The bundled
Silero VAD model is MIT-licensed by the Silero Team; see `assets/silero-vad/6.2.1/LICENSE`.
