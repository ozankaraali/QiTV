QiTV — Agent Guide and Working Plan

Overview
- Purpose: Maintain a clear, shared plan and conventions for ongoing refactors and fixes.
- Scope: Applies to the entire repository unless a more specific AGENTS.md is present in a subdirectory.

Principles
- Keep the UI responsive: no blocking network or heavy I/O on the UI thread.
- Fix root causes, not symptoms; keep changes small and focused.
- Prefer composition over monoliths; split large modules by responsibility.
- Use consistent logging over prints and propagate errors meaningfully to the UI when needed.
- Write code that’s testable; isolate logic from PySide UI where possible.

Style & Tooling
- Format: black + isort (configured via pre-commit).
- Lint: flake8 (treats most issues as warnings except syntax/undefined names).
- Types: add gradual type hints; keep mypy green on changed modules.
- Logging: use `logging.getLogger(__name__)` rather than `print`.

Current Work Plan (Living TODO)
1) Input/UI polish and correctness
   - [x] Separate dblclick fullscreen from single-click play/pause (video_player.py)
   - [x] Remove unused `installEventFilter(self)` on `video_frame` or implement `eventFilter` explicitly
   - [ ] Normalize progress bar behavior for live/VOD; avoid toggling visibility repeatedly
   - [ ] Add keyboard shortcuts as QActions (Play/Pause, Mute, Fullscreen, PiP) and bind menu/toolbar if added later

2) Networking and responsiveness
   - [x] Identify and thread key `requests` (M3U load, STB categories, link creation)
   - [x] Standardize timeouts/retries across network calls (added timeouts; moved update check to QThread)
   - [ ] Move remaining UI-thread `requests` to workers (exports OK as is)
   - [ ] Consolidate provider/EPG URL building and headers in one place

3) Modularity and structure
   - [x] Extract delegates to `widgets/delegates.py`
   - [x] Move M3U parsing to `services/m3u.py`
   - [x] Move export helpers to `services/export.py`
   - [ ] Split remaining `channel_list.py` into widgets/ (panels) and services/ (provider, epg)
   - [ ] Move image caching and EPG parsing unit-testable logic out of UI code paths
   - [ ] Consider a small event-bus/signal helper to decouple UI components

4) Logging and error handling
   - [ ] Add module-level loggers; remove stray prints
   - [ ] Plumb important errors to the UI via signals (non-modal first, modal where necessary)

5) Testing and stability
   - [ ] Add tests for provider cache pruning and image cache accounting
   - [ ] Add tests for XMLTV parsing and MultiKeyDict behavior
   - [ ] Add simple smoke tests for content loader pagination/aggregation

6) Packaging & config
   - [x] Completing the Github Actions for UV environment usage.
   - [ ] Pin more dependency versions in requirements.txt (PySide6, orjson, aiohttp, tzlocal)
   - [x] Add a `pyproject.toml` for tool config (black/isort/mypy) to keep settings centralized
   - [x] Drive bundle/app version from `pyproject.toml` in PyInstaller specs

Next Steps (Paused)
- Extract panels from `channel_list.py` into `widgets/`:
  - content info panel, list panel, media controls
- Add `services/provider_api.py` to centralize STB/Xtream calls with timeouts + QThread wrappers
- Move remaining UI-thread `requests` to workers (exports may stay synchronous)
- Introduce lightweight dataclasses for Channel/Program for safer data access
- Add cancelation support to network workers (or switch to aiohttp within QThreads)
- Add unit tests for `services/m3u.py` and `services/export.py`

Recent Changes (for context)
- Feature: Consolidated export functionality into single dropdown menu (fixes #27) (channel_list.py:430-446,1427-1650; README.md:36-46)
  - Replaced "Export Browsed" and "Export All Live" buttons with unified "Export" dropdown menu
  - Export Cached Content: Quickly exports only browsed/cached content
  - Export Complete (Fetch All): For STB series, fetches all seasons/episodes before exporting with progress dialog
  - Export All Live Channels: Exports all available live channels from cache
  - Changed popup mode to InstantPopup for cleaner UX
  - Added synchronous fetch methods for seasons and episodes
- Feature: Added portable mode support via `portable.txt` file (fixes #26) (config_manager.py:79-109; README.md:27-34)
  - When `portable.txt` exists in program directory, config and cache are stored locally instead of system directories
  - Works for both script and PyInstaller executable modes
- Fix: PyInstaller spec files now use SPECPATH instead of __file__ (qitv-*.spec:10)
- Fix: Updated to new UV dependency-groups format (pyproject.toml:49-50)
- Fix: Delayed main window activation to prevent cursor blinking issues (main.py:60-64)
- Fix: Video player no longer steals focus from channel list on playback (video_player.py:250-251)
- Feature: Added optional Serial Number and Device ID fields for STB providers (fixes #31) (options.py:179-187,362-363,408-411,424-431,525-530,537-538; provider_manager.py:115-118,77-82,175,189,197-207,217)
- Feature: Added "Resume Last Watched" button to quickly resume previous content (channel_list.py:412-414,1921-1973; config_manager.py:131-134,205-211)
- Fix: Resume Last Watched now recreates links for STB providers (tokens expire) (channel_list.py:1963-1966)
- Fix: Video player now properly activates on playback start (resolves focus-dependent mouse events) (video_player.py:249-250)
- Fix: CI changelog generation now uses body_path instead of non-existent output (.github/workflows/main.yml:185)
- Fix: App now properly raises and activates on startup (main.py:58-59)
- Fix: Progress bar seek no longer causes window drag (video_player.py:114)
- Fix: Movies/Series content type switching now correctly fetches respective categories (channel_list.py:71-101,1649)
- Fix: Single-click pause/play now works correctly; dragging only marked when mouse moves (video_player.py:340,369)
- Fix: Prevent single-click pause when double-click toggles fullscreen (video_player.py)
- Fix: Provider cache pruning now matches hashed provider-name files (provider_manager.py)
- Fix: Image cache accounting bug when file missing on disk (image_manager.py)
- Fix: Country field mapping typo in content info (channel_list.py)
- Infra: Centralized logging config (main.py); replaced prints with loggers across modules
- UX: Buffering progress bar visibility consistent for live/VOD (video_player.py)
- Perf: Update checker moved to QThread and added network timeouts; added timeouts in several requests
- Arch: Extracted delegates to `widgets/delegates.py`; moved M3U parsing to `services/m3u.py`; moved export helpers to `services/export.py`
- Packaging: Added `__init__.py` to `services/` and `widgets/` to satisfy mypy package resolution
- CI: Switched GitHub Actions to uv; centralized tool configs in `pyproject.toml`

Conventions for New Code
- Keep UI and data/services separate. Long-running network calls must run in QThread.
- Avoid coupling VLC/player code to UI state more than necessary; use signals.
- Prefer dataclasses or typed dicts for structured data passed between layers.

How to Contribute
- Update this AGENTS.md when you pick up or complete an item.
- Keep PRs small; focus on one area at a time.
