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
   - [ ] Identify all `requests` usages on UI thread and move to QThread workers or async layer
   - [x] Standardize timeouts/retries across network calls (added timeouts; moved update check to QThread)
   - [ ] Consolidate provider/EPG URL building and headers in one place

3) Modularity and structure
   - [ ] Split `channel_list.py` into: widgets/ (delegates, panels), services/ (provider, epg, images), models/ (data structs)
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
   - [ ] Pin more dependency versions in requirements.txt (PySide6, orjson, aiohttp, tzlocal)
   - [ ] Add a `pyproject.toml` for tool config (black/isort/mypy) to keep settings centralized

Recent Changes (for context)
- Fix: Prevent single-click pause when double-click toggles fullscreen (video_player.py)
- Fix: Provider cache pruning now matches hashed provider-name files (provider_manager.py)
- Fix: Image cache accounting bug when file missing on disk (image_manager.py)
- Fix: Country field mapping typo in content info (channel_list.py)
 - Infra: Centralized logging config (main.py); replaced prints with loggers across modules
 - UX: Buffering progress bar visibility consistent for live/VOD (video_player.py)
 - Perf: Update checker moved to QThread and added network timeouts; added timeouts in several requests

Conventions for New Code
- Keep UI and data/services separate. Long-running network calls must run in QThread.
- Avoid coupling VLC/player code to UI state more than necessary; use signals.
- Prefer dataclasses or typed dicts for structured data passed between layers.

How to Contribute
- Update this AGENTS.md when you pick up or complete an item.
- Keep PRs small; focus on one area at a time.
