# Changelog

Each release, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [6.1.0] - 2026-11-08

### Changed

- **Speech recognition is told the episode's names only.** Each window's context is now the hosts', guests' and organisations' names, separated by spaces. The labels, titles and description it carried before are gone.
  - Guests and organisations come from the show notes only when the title names them too or the notes do twice. Chapter headings, sponsor copy, links and a phrase named once are left out.
  - With no reliable name, a window is sent without context.
- **One retry, without context.** A window whose answer reads the names out first, is too short or mostly repeats them is transcribed once more without context. The second, shorter context is gone.
- **Read-out check.** It looks for the context said word for word at the start of the answer. Speech naming the same people in its own words is kept.

### Why

- With the earlier context, about a quarter of windows were read back and needed up to three requests.
- On a 10-window comparison, names only fixed a few spellings, made one worse, and left most text as it was without context. Existing transcripts are not redone.

## [6.0.1] - 2026-11-04

### Fixed

- **Empty listings.** A feed or YouTube listing served empty at a show's first read no longer makes the next read queue the whole back catalogue. An empty listing now counts as unreadable.
- **Settings file.** `.env.example` reads as documented. Inline comments are no longer part of the values.
- **User list.** A `users.json` that cannot be read is never written over. Readers send nothing until it is fixed.
- **Unknown chat callers.** A chat session that does not say who is asking gets no access.
- **Admin-only jobs.** Only the admin, cron or a shell runs the queue worker and the show monitor, or reads the weekly digest.
- **Streams tab seen late.** When a channel's Streams tab is read for the first time after the watch began, streams released since then are queued.
- **Same title, two episodes.** Two episodes of one source with the same title are both made. Two equal-title candidates leave a twin match in doubt, so the episode is made.
- **The admin's message.** The admin's copy of a finished note goes through the outbox and is retried like everyone else's. A set-up with no delivery address for the admin still gets it as cron output.
- **Smaller fixes:**
  - a GUID shared by two feed entries is settled by the audio URL;
  - `done.txt` is written only after the finishing transaction commits;
  - a follow notice is sent before the show table is redrawn;
  - a failed yt-dlp rollback is reported as failed;
  - setting up a new store is done by one process at a time.

### Changed

- **README.** The privacy, language and crash-safety claims say what holds. The installation uses a venv and creates the state folder.
- **CI** tests Python 3.11 and 3.12.
- **LICENSE** holds the MIT text only. The bundled model's licence stays in `assets/silero-vad/`.

## [6.0.0] - 2026-11-03

YouTube as a second source, every episode in its own language, several people on one installation, and crash-safe bookkeeping. See [What's new in 6.0](README.md#whats-new-in-60).

[Unreleased]: https://github.com/alexgranford/podcast-knowledge/compare/v6.1.0...HEAD
[6.1.0]: https://github.com/alexgranford/podcast-knowledge/compare/v6.0.1...v6.1.0
[6.0.1]: https://github.com/alexgranford/podcast-knowledge/compare/v6.0.0...v6.0.1
[6.0.0]: https://github.com/alexgranford/podcast-knowledge/releases/tag/v6.0.0
