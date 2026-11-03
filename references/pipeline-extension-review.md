# Reviewing podcast-ingestion extensions

Use this workflow when reviewing a proposal to add a source or change acquisition before implementation. Keep the review read-only unless the user authorizes code changes, dependency installation, configuration changes, or a live download.

## Review sequence

1. **Identify the deployed source of truth.** Read the installed skill's `README.md`, relevant scripts, metadata profile/validator and tests. Check whether the path is a Git checkout before citing branch or commit; report an installed copy without Git metadata as such. Treat the proposal's file paths and architecture claims as hypotheses until matched to code.
2. **Check live prerequisites without changing them.** In the active skill Python environment, inspect the downloader and plugin/runtime versions plus `ffmpeg`/`ffprobe`. For YouTube, check yt-dlp, `yt-dlp-ejs`, Deno or Node, and any optional PO-token provider. Do not install or upgrade them during a design review. If the required downloader is absent, mark the live smoke test unavailable; do not substitute a guessed result. Confirm egress only to the extent a permitted lookup supports it; ISP classification or IP geolocation alone does not prove the exact circuit type or physical location.
3. **Trace each integration boundary in both directions.** Verify URL parsing and canonical identity; queue deduplication and retries; source resolution; job/state/cache naming; audio acquisition and measured duration/size; transcription inputs; note naming, source link and metadata; show registry; post-run messages; monitor logic; cleanup. Search downstream consumers for assumptions about the original source's IDs or URL shape instead of treating the fetcher as the only integration point.
4. **Exercise metadata checks without writing notes.** Use the active vault's actual validator and profile. Check a well-formed source-specific example and malformed identifiers. Unknown fields being allowed is not proof that a property is validated or consumed. Omit source-specific IDs that do not apply; never emit an empty legacy ID. Preserve timestamp semantics: do not convert a date-only upload date into an invented UTC instant.
5. **Run the existing offline baseline** with temporary files directed to the configured scratch area, for example:

   ```bash
   TMPDIR=/opt/data/cache/scratch /opt/hermes/.venv/bin/python -m pytest -q <skill-dir>/tests
   ```

   Report this as baseline evidence only; it does not validate the proposed integration or replace new mocked tests.
6. **Return a go/no-go** with (a) live environment facts, (b) exact code/schema mismatches, (c) security and retry/cleanup gaps, (d) baseline test result, and (e) checks blocked by absent prerequisites. Do not implement while the request is for review only.

## Source-extension pitfalls

- **Do not treat an input-host allowlist as complete SSRF protection for a downloader.** Extractors make secondary requests and follow redirects to CDNs; constrain actual network egress/resolved targets as well as canonicalizing the user-supplied URL.
- **Do not assume existing queue retries cover new downloader failures.** Wire typed retryable/permanent/deferred outcomes into the queue's persisted retry policy; a standalone stderr classifier has no effect on queue behavior.
- **Trace the actual audio path through the full lifecycle.** Post-processors can change extensions and paths; verify that the existing cleanup removes the real output and partial files after transcription, and that cache sweeping includes the new job namespace.
- **Keep source identity separate from show identity.** Video/episode IDs, channel/show registry keys and display names have different semantics; inspect note deduplication, registry lookup, generated links and downstream scripts for source-specific assumptions.
- **Do not fabricate timestamps from dates.** Use a precise timestamp when available, otherwise omit an instant-valued `published` field or explicitly extend the schema to represent a date.
- **Separate current readiness from future design.** A Node/Deno or media-tool version check is not a successful extractor probe; report each independently and never install packages just to make a review appear complete.
