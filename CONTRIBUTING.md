# Contributing

## Set-up

```bash
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 scripts/setup_vad.py      # optional: without it the transcription tests are skipped
python3 -m pytest -q tests
```

## Rules for a change

- **Tests run offline.** Mock every network, model and message boundary. The fixtures in `tests/conftest.py` keep each test off the real state, cache and notes.
- **Fixtures are fictional.** No real names, phone numbers, chat ids, credentials or followed shows.
- **One change per pull request,** with a test that fails without it.
- **Documentation follows the code.** Update the README, `docs/MANUAL.md`, `SKILL.md` and `CHANGELOG.md` (under Unreleased) where the change touches them.

## Reporting

- **Bugs and ideas:** open an issue, with a redacted reproduction.
- **Security problems:** see [SECURITY.md](SECURITY.md).
