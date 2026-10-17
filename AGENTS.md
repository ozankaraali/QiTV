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

Layout
- The root holds only `main.py` (the entry point; `python main.py` runs the app), the three PyInstaller specs, `pyproject.toml`/`uv.lock`/`requirements.txt`, README/AGENTS/LICENSE/THIRD-PARTY-NOTICES and the folders (`licenses/` holds the license texts the packaged apps ship).
- `services/`: everything without widgets (providers, catalog, EPG, config, images, live pipeline, sessions, the update check, download and launch; images are decoded to QImage only). `ui/`: the TV window, its overlays, the Library, the settings dialog (`ui/settings_dialog.py`) and the update dialogs (`ui/update_dialogs.py`). `scripts/`: the smoke test, the demo content and README screenshots, the README's key tables and the Linux installer. `tests/`: unittest suites (`uv run python -m unittest discover -s tests`).

Style & Tooling
- Format: black + isort (configured via pre-commit).
- Lint: flake8 (treats most issues as warnings except syntax/undefined names).
- Types: add gradual type hints; keep mypy green on changed modules.
- Logging: use `logging.getLogger(__name__)` rather than `print`.

Current Work Plan (Living TODO)
- QiTV 2.0: a TV shell on VLC (branch `tv/vlc`, spec `docs/superpowers/specs/2026-10-15-tv-on-vlc-design.md`, a plan per phase in `docs/superpowers/plans/`). No native builds: VLC's official build ships inside the app; the live pipeline is pure Python. The modules by group:
  - Entry: `main.py` (frozen helper guard, the bundled libVLC via `services/vlc_bundle.py`, `--smoke`).
  - Window: `ui/tv_window.py` (one window: the player page with two native video surfaces, the Library page), `ui/tv_pages.py` (moves between pages), `ui/tv_menus.py`, `ui/pip.py` (PiP frame), `ui/settings_dialog.py`, `ui/update_dialogs.py`.
  - Overlays: `ui/overlay_host.py` (translucent host glued over the video) and `ui/overlays/` (zap banner, fast-zap column, drawer, transport, guide, toast, next card, welcome card, PiP label, mixer); tokens in `ui/theme.py`, painting helpers in `ui/overlays/painting.py`.
  - Behaviour: `ui/tv_controller.py` with `ui/link_lanes.py`, `ui/live_tuner.py`, `ui/live_controls.py`, `ui/pip_controls.py`, `ui/mixer_controls.py`, `ui/guide_controller.py`, `ui/context_actions.py`, `ui/autoplay.py`, `ui/provider_flow.py`, `ui/bottom_band.py`, `ui/export_flow.py`; keys in `ui/keymap.py` (`SHORTCUTS` feeds Help and the README).
  - Library: `ui/library_controller.py` and `ui/library/` (top nav, genre list, poster grid with detail strip, series view, search session).
  - Catalog and data: `services/catalog.py`, `provider_manager.py`, `provider_api.py`, `workers.py`, `content_loader.py`, `m3u.py`, `epg_*.py`, `navigation.py`, `zapping.py`, `library.py`, `search.py`, `favorites.py`, `watch_state.py`, `next_episode.py`, `exporter.py`, `config_manager.py`, `jobs.py`, `thread_cleanup.py`.
  - Playback: `services/playback.py` (one libVLC player), `vlc_profile.py`, `sessions.py` (the two slots), `connection_budget.py` and `budgeted_stream.py` (provider stream limits), `external_players.py`.
  - Time-shift: `services/live/` (helper process: source, TS segmenter, RAM/disk rings, loopback server, timeline, stay-live, engine and live session).
  - Scripts: `scripts/smoke_tv.py` (CI playback smoke), `scripts/make_demo_content.py` (demo content and the README screenshots), `scripts/readme_keys.py` (the README's key tables), `scripts/install-linux.sh`.
  - [x] Phase 0 Foundation: tidy-up landed; fixed STB prehash, image cache eviction, overnight EPG stop, per-provider HTTPS, STB export bases, STB next-season autoplay, M3U line endings; design tokens (`ui/theme.py`); CI runs tests on all platforms and releases only from `main`
  - [x] Phase 1 TV shell (Live TV): single window, native VLC surface + glued overlay host, zap banner/column/drawer, NavModel/ZapContext, transport
  - [x] Phase 2 Live pipeline: TS segmenter, RAM/disk rings, loopback server, timeline, stay-live, connection budget, time-shift UI, CI playback smoke
  - [x] Phase 3 Library + guide; remove old windows (v1.13.8 windows deleted, root modules moved into `services/` and `ui/`); version 2.0.0, README, release notes, licenses
  - [x] Phase 4 PiP + swap (two players, swap, TV-style mixer, a memory ring per stream)
1) Input/UI polish and correctness
   - [x] Separate dblclick fullscreen from single-click play/pause (video_player.py)
   - [x] Remove unused `installEventFilter(self)` on `video_frame` or implement `eventFilter` explicitly
   - [x] Normalize progress bar behavior for live/VOD; avoid toggling visibility repeatedly
   - [x] Add keyboard shortcuts as QActions (Play/Pause, Mute, Fullscreen, PiP) and bind menu/toolbar if added later

2) Networking and responsiveness
   - [x] Identify and thread key `requests` (M3U load, STB categories, link creation)
   - [x] Standardize timeouts/retries across network calls (added timeouts; moved update check to QThread)
   - [x] Move remaining UI-thread `requests` to workers (exports OK as is)
   - [x] Ensure all worker completions marshal back to the UI thread (no cross-thread timers)
   - [x] Consolidate provider/EPG URL building and headers in one place

3) Modularity and structure
   - [x] Extract delegates to `widgets/delegates.py`
   - [x] Move M3U parsing to `services/m3u.py`
   - [x] Move export helpers to `services/export.py`
   - [x] ~~Split remaining `channel_list.py`~~: the v1.13.8 windows were removed in 2.0 (the TV window replaced them)
   - [ ] Move EPG parsing unit-testable logic out of UI code paths
   - [x] Refactor image pipeline: workers only cache bytes; GUI builds QPixmap/QIcon on main thread
   - [ ] Consider a small event-bus/signal helper to decouple UI components
 - [x] Add network security toggles (Prefer HTTPS, SSL verify) and plumb into requests/aiohttp (XTREAM/STB/M3U)
 - [ ] Debug log EPG endpoints for providers (STB `load.php` get_epg_info, Xtream `xmltv.php`) to help diagnose ID mismatches
 - [x] Show all EPG entries in channel program list (no windowing), with local time formatting
 - [x] Investigate VideoPlayer seeking: prevent playback ending on seek; ensure double-clicking the progress slider controls the bar (not window drag)
 - [ ] Investigate optional VLC quality enhancements for low-bitrate streams (audio compressor/equalizer and lightweight upscaling/sharpening), gated by settings and disabled by default

4) Logging and error handling
   - [x] Add module-level loggers; remove stray prints
   - [x] Downgrade transient image fetch errors to info; reduce log noise
   - [ ] Plumb important errors to the UI via signals (non-modal first, modal where necessary)

5) Testing and stability
   - [x] Fix native QThread teardown races without blocking the GUI; retain wrappers through a successful nonblocking join
   - [ ] Add tests for provider cache pruning and image cache accounting
   - [ ] Add tests for XMLTV parsing and MultiKeyDict behavior
   - [ ] Add simple smoke tests for content loader pagination/aggregation

6) Packaging & config
   - [x] Completing the Github Actions for UV environment usage.
   - [ ] Pin more dependency versions in requirements.txt (PySide6, orjson, aiohttp, tzlocal)
   - [x] Add a `pyproject.toml` for tool config (black/isort/mypy) to keep settings centralized
   - [x] Drive bundle/app version from `pyproject.toml` in PyInstaller specs

Next Steps (Paused)
- Add `services/provider_api.py` to centralize STB/Xtream calls with timeouts + QThread wrappers
- Move remaining UI-thread `requests` to workers (exports may stay synchronous)
- Introduce lightweight dataclasses for Channel/Program for safer data access
- Add cancelation support to network workers (or switch to aiohttp within QThreads)
- Add unit tests for `services/m3u.py` and `services/export.py`

Recent Changes (for context)
- 2.1 stay-live stage 1 (`tv/vlc`, spec `docs/superpowers/specs/2026-10-17-stay-live-rework-design.md`): stay-live steers only real drift, gently. `LagFilter` (`services/live/stay_live.py`) averages the closed-form mean lag over a dozen bursts of the live edge's history (10-90 s), so the public HLS channel's uneven HLS batches (6 s, every sixth 12 s, every 7 s) no longer move it; the position is never averaged; an arrival that moves the level > max(3 s, a burst) or a stall's end restarts the window (a step taken in once); its burst has hysteresis (a larger one counts after max(10 s, two bursts), a smaller after 30 s, changes < 0.5 s ignored) for the target stay-live steers by, while the timeline's target, the stall bound and Go live still follow the helper's burst at once. `StayLive` keeps 1x inside a dead band of max(1 s, 1.5 × the filter's spread) (+ a quarter burst behind), and only after 15 s outside it makes one nudge (1.03x/0.97x, ramped 0.01 a tick, sized to end at the target), at most one per 5 minutes (else the band widens); far behind (> 8 s) 1.10x and less than 0.5 s of room at a landing 0.95x act after 3 s beyond the budget. The timeline carries `shown` (`SettledLag`: half seconds, changed after 3 s) and `nudge`; the indicator reads "≈ 7 s behind", amber whenever the rate isn't 1x. Stages 2-5 (honest clock, start policy, learned target, safe jumps) are still to come.
- 2.1 scale (`tv/vlc`): `scripts/bench_mixed.py` builds 4 providers' caches (213,998 live channels, 40,000 films) and times the drawer, the Library and Search on "All providers" (offscreen, muted; `--trace`, `--profile STEP` and `--sample` show where the time goes). A group of 2,000+ channels is a `NodeSeq` (`services/navigation.py`): its nodes are made on demand and the models take the channel dicts as they are; row views lay out in batches; Search reads the content the catalogs hold (`Catalogs.loaded_contents()`) instead of parsing the cache files again; `main.py` sets a 0.5 ms thread switch interval and the loaded catalogs are `gc.freeze()`n out of the collector.
- 2.0.8 (`tv/vlc`): the speed chip is `− 1.25x +` (`SPEEDS` 0.5–2×, stopping at the ends; the number goes back to 1x) and, like Go live and ‹ Library, as wide and tall as its text (`Transport._chip_h`; the font stops at `MIN_FONT_PX` while u shrinks with the window, so "1.25x" was cut off); where − and + don't fit (a 300-px mini player, shifted, with Go live) the number shows alone.
- 2.0.8 (`tv/vlc`): the pointer over the video hides with the player controls, as in VLC and mpv (`PointerHider` in `ui/pointer.py`): once it has rested `POINTER_IDLE_MS` (3 s, the transport's wait) and the transport is gone, while `TVController.pointer_may_hide()` holds (something plays on the player page; no drawer, column, guide, mixer, next card or welcome card open); a move over the video, the PiP or the overlays shows it again. macOS hides it with `[NSCursor setHiddenUntilMouseMoves:YES]` (`ui/macos_window.py`), which also covers libVLC's own view; Windows/Linux set a blank cursor on the surfaces and the overlay host (libVLC's mouse input is off, so Qt's cursor applies).
- 2.0.7 (`tv/vlc`): every provider request QiTV makes itself (Xtream catalog, series info, M3U playlists, EPG downloads, Verify) names the client as `VLC/3.0.20` (`CLIENT_HEADERS` in `services/provider_api.py`, the agent the streams already used): some panels' proxies answer the requests library's own `python-requests/…` with 403, so QiTV was refused where other players signed in. Xtream Verify signs in through `player_api.php` (`xtream_signed_in`: auth 1 and an active account) instead of a HEAD on the bare host, which such a panel answers with 502. `tests/test_provider_user_agent.py` serves a panel that blocks the library agent.
- Verification for 2.0.7: a reported Xtream panel (two hosts) answered 403 to `python-requests` and 200 to VLC/curl/browser agents; before the fix the catalog failed with 403 on both hosts, after it both load 921 categories and 57,359 channels, and Verify passes with the right password and fails with a wrong one (that panel answers an empty 513). Streams were not played (1-connection account).
- 2.0.2 (`tv/vlc`): "Auto" live latency, the new default: per channel it backs off on starvations and stalls (only once a cushion existed) and decays after quiet minutes; rate health stops rate changes on a channel whose VLC crawls at other rates (1x lock, retried after 24 h); an HLS channel that still crawls through the pipeline at 1x plays directly (VLC's own HLS demux) with one notice, remembered 24 h; per-channel memory in `live_tuning.json` (hashed keys). HLS segment joins keep each PID's continuity counter running (`ContinuityJoiner`). Overlays stack in the order they are added (the PiP label above the transport took its clicks) and the transport's shades leave the PiP alone.
- Verification for 2.0.2: Kanal D (iptv-org Turkey, public HLS whose PCR comes every 2 s) measured with VLC's log: through the pipeline it crawled (~0.8x, 'no reference clock'/'too late' by the hundred, jumps every ~23 s) whatever the rate or caching; VLC's own HLS demux plays it at 1x; with the fallback the channel goes direct at ~14 s and at once on the next tune, with 2 late frames in 45 s. The PiP speaker/⇄/× were reproduced dead with real NSEvent clicks while the controls showed (the label under the transport) and work after the fix, in a window and fullscreen.
- 2.0.1 (`tv/vlc`, from the owner's first days with 2.0): raw TS that a provider sends in bursts (~10 s every ~10 s) is measured in the helper (`burst_s` from the first clear burst, confirmed or dropped by the next; an unconfirmed one lasts at most one estimate) so stay-live, the stall check ("Reconnecting…") and Go live treat it like HLS, and stay-live slows to 0.95× until a bursty source has half a burst of room; a channel that sends bytes but isn't playable yet gets up to 15 s before the direct fallback, and a live direct stream that ends is reopened twice. On macOS a click on the video no longer leaves libVLC's view as the first responder (arrow keys beeped). Everything in `SHORTCUTS` has an on-screen control (an audit test walks the table): player controls for volume/mixer, guide, search, previous channel, ★ favorite, the channel's info, "‹ Library" after a movie; the PiP label's speaker mutes it and ⇄ swaps (≥28 px targets); the channel list's top row has ‹ back, ⌕ search and clickable names (the provider's name opens the sources to switch), plus an Open file row; the guide's header has ‹ › ⌕ ⛶ ×; the Library has ⛶. Double-clicks act once on toggles.
- Verification for 2.0.1: the controller measured "TR: 24 TV" (bursty raw TS) on the owner's provider before and after: before, 1.1× catch-ups and "Reconnecting…" every 10 s forever; after, "Reconnecting…" only in the first ~14 s, never at the edge after 40 s, 0.95× until ~15 s of room, then 1.0×. The macOS first-responder fix was confirmed on a real muted window (NSEvent click on the video).
- 2.0.0 release pass (`tv/vlc`): a performance and leak pass (first frame 1.80 → 1.45 s and RSS 312 → 202 MiB at start on a 2,500-channel/20,000-film synthetic catalog; drawer open 118 → 53 ms; guide after a provider switch 439 → 16 ms; the saved guide is a packed `<key>.guide`, old `.pkl` files still read; network libraries load after the first frame; no growing leaks over 600 zaps and 60 PiP cycles). Hardening fixed a locked config.json stalling zaps or Save, old catalog jobs beyond the two-job cap, unmasked `QITV_DEBUG_CURL` cookies, remaining URL logs, slow quit with running catalog jobs, STB links re-made once on a failed play, banner times in the guide's zone, the fast-zap column over the banner, and macOS's stale VLC plugin cache (regenerated after signing in CI; a check fails the job if it is stale). The final review's fixes: the main player's own volume (`--no-volume-save`; libVLC shared the last level between players), a small 1.14 window reused as the TV window, a movie waiting for a closing PiP's stream, PiP refused while an external player holds the stream, external players started without the bundle's VLC environment, EPG Weeks/Months, the update flow with a locked config, the Python runtime in the notices, the PiP lifted above the zap banner, and the Linux/Windows packaged smokes run with the system VLC hidden.
- After the owner tried 2.0: the channel list's top row ends with ↻ (the provider's channels and guide again, also Ctrl/⌘+R) and ⚙ (Settings); an old "always play in VLC/MPV" is switched off once on the first 2.0 start, Settings offers "Always play in VLC/MPV", the player controls have an "Open in external player" button and the Library's right-click has Play in VLC/MPV and Copy stream link (an opened file sent out leaves no history); M3U group titles with ";" (iptv-org) put a channel in each of those groups, also in playlists cached before the split, with one channel still one channel in All, Favorites, search and the guide.
- Verification for the release pass: the full suite at d27a3e9 ran 1975 tests OK (3 skipped); manual checks against the public iptv-org list and a real Xtream account (play, zaps, PiP open/swap/close, time-shift 13 s behind live, never more than 2 streams, nothing left after quit).
- QiTV 2.0.0 (Phase 3 T14, `tv/vlc`): version 2.0.0 in `pyproject.toml` and `uv.lock` (About shows it; `compare_versions` offers 2.0.0 to 1.13.x and 1.14.x). The README was rewritten for 2.0 (installing with VLC inside, adding a provider from the drawer's sources column, Watching TV with the key tables generated from `SHORTCUTS` by `scripts/readme_keys.py`, time-shift, live latency, picture-in-picture, the guide, the Library, favorites, exports, external players, portable mode, the source build and debug switches collapsed, licensing for VLC). Release notes are in `docs/release-notes-2.0.0.md`. `scripts/make_demo_content.py` makes fictional demo content (24 channels in 4 groups with logos and a guide, a local Xtream-shaped library of 36 films and one series with 2 seasons, served from 127.0.0.1) and, with `--screenshots docs/screenshots`, takes the README's 8 screenshots on the offscreen platform with libVLC muted. `THIRD-PARTY-NOTICES.md`, `LICENSE` and `licenses/` (VLC 3.0.20's COPYING and COPYING.LIB; the LGPLv3 and GPLv3 texts for Qt) now ship inside every packaged app (three `datas` lines per spec).
- Verification for 2.0 (this phase): CI run 38778479935 at 467a12c passed the unit tests, the source smoke and the packaged smoke (bundled libVLC, VLC.app hidden on macOS) on Windows, Linux, Intel macOS and ARM macOS; the full suite at d433f58 ran 1880 tests OK (3 skipped). T14's focused tests (`tests/test_readme.py`: the README's key tables equal `SHORTCUTS`, its images exist, its and the notices' licenses match the installed packages' metadata; `tests/test_demo_content.py`: the demo content is complete and nothing reaches beyond 127.0.0.1) pass. The packaged apps with the license files are checked by CI's next packaging run.
- Phase 3 T13 (`tv/vlc`): Removed the v1.13.8 windows (`channel_list.py`, `video_player.py`, `mixins/`, `widgets/`) and the old-only workers (tree widget items, `SetProviderThread`, `XtreamAuthWorker`). Their tests were retargeted to the services that own the behaviour (CatalogService, `services.next_episode`, the link worker, the exporter, ProviderManager) or deleted with a reason (Task 13 report). The root modules moved with `git mv` into `services/` (config_manager, provider_manager, epg_manager, image_loader, image_manager, multikeydict, sleep_manager, update_checker, content_loader, workers) and `options.py` became `ui/settings_dialog.py`. The config file location is unchanged (portable mode looks beside `main.py`). Dropped settings only the old windows read (Keyboard/Remote mode, smooth paused seeking, info panel switches); the drawer plays only channels (Movies/Series play from the Library). Season chips show the provider's season name again.
- Phase 0 (`tv/vlc`): Restarted from v1.13.8 after retiring the MPV line. Landed a behaviour-preserving service-layer tidy-up and fixed seven bugs it surfaced, each with a regression test (STB profile prehash sent as a set; image cache never evicting and deleting wrong files; overnight EPG stop discarded; global instead of per-provider HTTPS; `…/c//play` STB export URLs; STB next-season autoplay; CRLF/CR/BOM M3U names). Added design tokens and cross-platform CI test runs (CI run 38660241530 on `tv/vlc` passed tests and packaging on Windows, Linux, Intel macOS and ARM macOS with the release job skipped); releases now publish only from `main`.
- v1.13.8 (unreleased): Restore missing window geometry during config migration without overwriting saved windows or user preferences. Returning from the MPV experiment left no `window_positions.video_player`, so the VLC-based 1.13.7 release raised `KeyError: 'video_player'` before displaying its windows. A regression covers player initialization and subsequent geometry persistence while preserving existing state.
- Verification: Confirmed the installed Intel 1.13.7 bundle fails with the MPV-shaped config; a rebuilt 1.13.8 bundle starts with it and restores the missing entry. Native source startup displayed the local catalog, produced a window capture, and closed normally; all 14 tests, focused config types, lint, and the lock check passed. Frozen verification observed native windows and config migration; OS screenshot/interaction permissions were unavailable, and the isolated frozen process was stopped through its supervisor. No new cross-platform CI run has been performed for this patch.
- Release v1.13.7: Fixed an intermittent native PySide/QThread cleanup crash, observed immediately after waking from sleep and reproduced with repeated local M3U loads. Shared `services/thread_cleanup.py` retains thread/worker wrappers through a successful nonblocking native join before notifying the GUI or deleting the thread. Applied to catalog, images, provider setup/verification, playback links, and updates; app closure waits asynchronously for pending cleanup. VLC playback and existing packaging remain unchanged; no MPV migration was merged.
- Verification: On Intel macOS/Python 3.14.0/PySide6 6.11.2, 3,000 local M3U worker lifecycles, all 13 regressions, controlled-response update/download success/error/cancellation smoke checks, focused seven-module type checks, syntax/undefined-name lint, and the version/lock consistency check passed. New subprocess regressions cover delayed native teardown, GUI responsiveness, and consumer destruction. Broader mypy still reports 22 errors in unchanged export/dialog modules. Frozen bundles and other operating systems have not yet been reverified for this patch.
- Release v1.13.6: Dependency/security upgrades and the Xtream duplicate-request fix (#51). Verified 11 regressions, 11-module type checks, native macOS single-request playback and VOD resume, asynchronous image loading, and the macOS bundle build.
- Fix #51: Xtream catalog loading no longer probes media URLs, and the embedded player no longer requests network preparsing before playback. Provider metadata determines stream URLs/formats; native playback, redirects, reconnects, and VOD error/seek handling remain intact. `tests/test_xtream_requests.py` covers API-only live/VOD catalog requests, explicit scheme/port precedence, HLS-only providers, and VOD container metadata.
- Dependencies: Updated runtime/tooling pins and all transitive dependencies, aligned pre-commit tool versions, and pinned supported CI action releases by commit SHA. Removed unused m3u-parser/asyncio and obsolete tzlocal stubs. Python is constrained to 3.14 by current Qt/theme support; requirements.txt is generated with hashes from uv.lock.
- Compatibility: Routed video-frame double-clicks through the existing Qt event filter instead of assigning a Qt virtual method. Made PiP geometry restoration explicit and removed obsolete mouse-button enum fallbacks; separated asyncio completion awaitables from cancellation tasks for current type stubs.
- Release v1.13.5: Large content lists populate in cancellable GUI batches with an eight-millisecond row-construction budget. Detached row construction reduces model notifications; numeric sort values are cached and initial sorting runs once. EPG rows populate per batch, and display refresh preserves category, selected item identity, and sort order.
- Fix: Catalog refresh/navigation uses nonblocking workers, rejects superseded results, reuses cached STB/Xtream seasons and STB episodes, and batches list updates. Logo/poster jobs never lock navigation and map results to item identity after sorting.
- Feature: Provider connection changes and Apply/Verify followed by Save trigger automatic content refresh. Provider drafts are isolated until Save; verification runs in an isolated background session.
- Cache: Six-hour per-content freshness with connection identity checks; five-minute visible-catalog checks defer while inside a series. Cache serialization/writes are ordered, asynchronous, atomic, and invalidation-safe. Local M3U parsing and STB category indexing run in workers.
- Verification: Eight focused regressions in `tests/test_catalog_refresh.py` cover cache expiry, same-name provider edits, pending-write invalidation, cached/interrupted Back navigation, sorted/retired logos, numeric/EPG sorting, and refresh selection restoration (`uv run python -m unittest discover -s tests -v`).
- UX: Added QActions for playback controls (Space: Play/Pause, M: Mute, F: Fullscreen, Alt+P: PiP) for future menu/toolbar binding (video_player.py)
- UX: Normalized VOD vs Live progress behavior; avoid repeated visibility toggles and only update values on VOD (video_player.py)
- Refactor: Centralized STB URL building in `services/provider_api.py`; updated STB workers and EPG to use it (channel_list.py, epg_manager.py)
- Fix: Eliminated cross-thread timer warnings by posting worker completions to the GUI thread (channel_list.py: M3U/STB/link creators; update_checker.py). Also avoided unconditional signal disconnects that caused warnings.
- Refactor: Image loading pipeline avoids GUI objects in worker threads; workers cache files, GUI constructs QPixmap/QIcon (image_loader.py, image_manager.py, channel_list.py logos/posters).
- UX: Export button now uses a clean label; dropdown arrow provided by Qt via setMenu (channel_list.py).
- Fix: Robust list population (avoids None-to-Qt conversions) and EPG text handling; safer selectionChanged disconnects for program/content lists.
- Debug: Optional Qt warning capture via `QITV_DEBUG_QT=1` prints timer/thread issues to stderr without crashing (main.py).
- Feature: Resume Last Watched auto-switches provider on user confirmation, then resumes playback (channel_list.py).
- Feature: Modern toolbar UI with quick provider switcher (channel_list.py:394-538)
  - Single-row toolbar with logical sections: Provider | File Ops | Navigation | Content Actions
  - Quick provider dropdown at start - switch providers without opening Settings
  - Compact gear icon (⚙) for Settings button
  - Shortened button labels with tooltips (Update, Resume, Rescan Logos)
  - Export button shows dropdown arrow (▼) and opens menu on click
  - Visual section grouping with consistent 12px spacing between sections
  - Auto-refreshes provider list after Settings dialog closes
- Fix: Removed incorrect @staticmethod decorator from load_stb_categories (channel_list.py:1877)
  - Was causing AttributeError: 'str' object has no attribute 'provider_manager'
  - The decorator caused parameter shift where self received url string instead of instance
- Feature: Enhanced export validation and tooltips (channel_list.py:430-456,1467-1544)
  - Added helpful tooltips to each export menu option
  - Export Complete now shows informative messages for inappropriate content types
  - Validates provider type and content type before attempting fetch operations
- Feature: Consolidated export functionality into single dropdown menu (fixes #27) (channel_list.py:430-456,1467-1650; README.md:36-46)
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
 - Tweak: Removed manual window activation/raise calls to avoid focus stealing (main.py, video_player.py)
- Fix: Progress bar seek no longer causes window drag (video_player.py:114)
- Fix: Movies/Series content type switching now correctly fetches respective categories (channel_list.py:71-101,1649)
- Fix: Single-click pause/play now works correctly; dragging only marked when mouse moves (video_player.py:340,369)
- Fix: Prevent single-click pause when double-click toggles fullscreen (video_player.py)
- Fix: Provider cache pruning now matches hashed provider-name files (provider_manager.py)
- Fix: Image cache accounting bug when file missing on disk (image_manager.py)
- Fix: Country field mapping typo in content info (channel_list.py)
- Infra: Centralized logging config (main.py); replaced prints with loggers across modules
- UX: Buffering progress bar visibility consistent for live/VOD (video_player.py)
 - UX: Mouse Back/Forward buttons map to Back/Forward navigation (VideoPlayer emits backRequested/forwardRequested -> ChannelList.go_back/go_forward)
 - UX: Optional "Keyboard/Remote Mode" setting moves list highlight with Up/Down; auto-plays when item is playable (options.py, video_player.py, channel_list.py)
- Perf: Update checker moved to QThread and added network timeouts; added timeouts in several requests
- Arch: Extracted delegates to `widgets/delegates.py`; moved M3U parsing to `services/m3u.py`; moved export helpers to `services/export.py`
- Packaging: Added `__init__.py` to `services/` and `widgets/` to satisfy mypy package resolution
- CI: Switched GitHub Actions to uv; centralized tool configs in `pyproject.toml`
 - Feature: Added global Network settings: "Prefer HTTPS when available" and "Verify SSL certificates". Applied to Xtream, STB, and M3U fetchers (options.py, config_manager.py, channel_list.py, provider_manager.py, epg_manager.py, services/provider_api.py, content_loader.py, image_loader.py)
 - Behavior: Xtream base resolution no longer auto-enforces HTTPS; respects entered scheme unless Prefer HTTPS is enabled (services/provider_api.py, channel_list.py)
 - Player: Replaced plain `QProgressBar` with seekable progress bar subclass; drag/double-click seeks without window drag. Seeking clamps near end to avoid playback ending.
 - EPG UX: Program list highlights the currently airing entry with a "▶ Now" prefix and a light blue background; times are localized.
 - EPG: Added Settings control for STB EPG server fetch period (hours); loader now uses `epg_stb_period_hours` instead of fixed 5.

Conventions for New Code
- Keep UI and data/services separate. Long-running network calls must run in QThread.
- Avoid coupling VLC/player code to UI state more than necessary; use signals.
- Prefer dataclasses or typed dicts for structured data passed between layers.
 - Never create Qt GUI objects (QPixmap/QIcon) or start timers from worker threads; emit plain data and build UI in the main thread.

How to Contribute
- Update this AGENTS.md when you pick up or complete an item.
- Keep PRs small; focus on one area at a time.
