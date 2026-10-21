# QiTV — notes for coding agents

A PySide6 IPTV player on libVLC (VLC 3.0.20 ships inside the app). What each release changed is in
`docs/release-notes-*.md`; the history is in `git log`.

## Commands
- Run: `uv run python main.py`
- Whole suite (parallel, ~2 min): `uv run python scripts/run_tests.py`; one module:
  `QT_QPA_PLATFORM=offscreen PYTHONPATH=.:tests uv run python -m unittest tests.test_<name>`
- Playback smoke (muted, offscreen, local fixtures):
  `QT_QPA_PLATFORM=offscreen QITV_SMOKE_MUTE=1 uv run python scripts/smoke_tv.py --out <dir> --pipeline --pip --fixtures tests/fixtures`
- black, isort and flake8 as configured; keep mypy green on the modules you change.

## Rules the code doesn't show
- No native builds: VLC's official build is bundled, and the time-shift helper (`services/live/`)
  is pure Python running as its own process.
- The GUI thread never blocks on network or disk. Worker threads never create Qt objects or start
  timers; they emit plain data and the GUI thread builds the UI.
- `services/` has no widgets; the UI lives in `ui/`. Use `logging.getLogger(__name__)`, never print.
- Every action in `ui/keymap.py`'s `SHORTCUTS` has an on-screen control (an audit test walks the
  table); the README's key tables are generated from it (`scripts/readme_keys.py`).
- Never open more streams than a provider allows (`services/connection_budget.py`); a closed
  connection can stay counted at the provider for minutes.
- With a single provider everything behaves as in 2.0; mixed-provider features appear with two or more.
- Keep older 2.x builds able to read the config, favorites and `live_tuning.json` (versioned
  formats; new fields are optional).
- Never commit credentials. Never name real providers, channels, brands or other IPTV projects in
  code, tests, docs or commit messages (iptv-org, the bundled free playlist, may be named). Credit
  what QiTV actually uses in `THIRD-PARTY-NOTICES.md`.
- Anything that plays real streams on a developer's machine runs muted (`QITV_SMOKE_MUTE=1`) and briefly.
- Releases publish only from `main`: a push to `main` runs the release job.
- The phone page (`services/remote/web/`) is also served by the private relay at remote.qitv.app,
  which keeps every released page: a release that changes the page has the relay synced and
  deployed first (the owner's relay repository has the steps and its own AGENTS.md).
