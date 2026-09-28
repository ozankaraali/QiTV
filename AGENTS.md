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
- In progress: 1.14.1 bundled standalone MPV replaces the Qt/libVLC player. Keep Internal isolated from user configuration; external VLC/MPV use installed applications. Release requires direct and time-shift source/frozen smoke checks on all four packaging targets, with matching MPV and private PyAV/FFmpeg sources.
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
   - [ ] Split remaining `channel_list.py` into widgets/ (panels) and services/ (provider, epg)
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
- Extract panels from `channel_list.py` into `widgets/`:
  - content info panel, list panel, media controls
- Add `services/provider_api.py` to centralize STB/Xtream calls with timeouts + QThread wrappers
- Move remaining UI-thread `requests` to workers (exports may stay synchronous)
- Introduce lightweight dataclasses for Channel/Program for safer data access
- Add cancelation support to network workers (or switch to aiohttp within QThreads)
- Add unit tests for `services/m3u.py` and `services/export.py`

Recent Changes (for context)
- Release preparation: Version 1.14.1. The owner requested a branch push and full review against main before the first complete MPV cutover; native/frozen platform verification and matching source/license coverage are merge gates.
- Windows playback shutdown fixed: The native WASAPI hotplug patch retains COM's MTA independently of the client thread that first enumerates audio devices. [Run 36388835306](https://github.com/ozankaraali/QiTV/actions/runs/36388835306) passed Windows application regressions and source/frozen direct and time-shift playback after the previously reproduced shutdown failure. Linux also passed; packaged playback captures were inspected.
- Mac playback lifetime fixed locally: Ordinary stop/replay with window-size changes failed after two cycles before the patch and completed 100 cycles with exit 0 afterward. Cocoa now invalidates its borrowed video-output object before teardown returns and protects queued geometry reads with the existing events lock; window closing stays asynchronous. The integrated application passes all 93 regressions, six-module types and syntax/undefined-name lint. Temporary crash collectors and workflow hooks are removed. Source/frozen playback and the final four-platform gate still determine release readiness.
- Windows pipe EOF correction: CPython's Windows `PipeConnection.poll()` can raise `BrokenPipeError` from `PeekNamedPipe` before `recv()` translates closure into EOF. The supervisor now handles closure at either boundary, retaining its existing unexpected-exit reporting when the child supplied no error. A real spawned unsupported-source regression reproduced the duplicate rejection/startup error before and passed after. All 93 local tests, changed-service types, formatting/lint and native time-shift passed; native video/audio captures were inspected.
- Full-cutover review: Guarded late STB/resume/autoplay requests during application close; Windows updates now initiate player shutdown before waiting and launch synchronously at the completed native-teardown barrier. Redirect responses are closed before Requests can buffer their bodies, preserving redirect TLS/cookie/auth behavior. Repeated buffered seeks retain user pause intent, unsolicited native file replacements detach the old recorder and cannot be killed by stale stops, and audio-only playback retains a native control window.
- Private time-shift packaging: PyPI's PyAV wheel links GPL x264/x265 despite an LGPL-reported FFmpeg configuration, and its ARM wheel requires macOS 14. Releases instead source-build pinned PyAV 18.1.0 with LGPL-only FFmpeg 8.1.2, preserving macOS 13 and publishing separate corresponding sources/notices. Cache reuse validates recipe, wheel, native-library closure, minimum OS and source/license integrity; specs reject a registry wheel replacing the private install. Intel preparation and isolated codec/linkage checks passed. Source commands retain the private wheel with `--no-sync`.
- Decoder-only remux correction: PyAV defaults video stream templates to an encoder context. Explicit opaque context copying makes packet remux independent of encoder availability; a real H.264/AAC regression failed before and passed after the change, retaining all fixture frames. Native Intel playback with the private LGPL build passed two clock resets, repeated seeks during the actual temporary pause, paused rewind, speed/Go live, native audio-file replacement, control-window rendering and one-source/clean-child-session shutdown. Both video/time-shift and audio-only captures were inspected. Local review verification now passes all 92 regressions; focused eight-module types and the subsequent two-service check also passed.
- Local frozen verification at `42e6444`: Rebuilt the current private PyAV runtime and Intel app; strict/deep ad-hoc signature verification passed. From an external working directory with both native input trees hidden, five consecutive direct stop/replay/shutdown scenarios each passed 14 checks, and spawned time-shift passed 12 checks. Every native exit was code 0/NormalExit with all child processes reaped. Inspected rebuilt video, time-shift and audio-only control captures. The throwaway driver was removed and both native trees restored.
- DNS-independent loopback startup: CI at `333794b` identified both macOS architectures blocked in `HTTPServer.server_bind()` → `socket.getfqdn()`. Internal playlist and HLS servers now bind with TCPServer and numeric metadata, preserving their single/threaded behavior without reverse DNS. Two real decode regressions failed before and passed after the fix. Native Intel time-shift passed all 12 checks with reverse DNS disabled in both parent and recording child. Temporary startup instrumentation was removed; no production or test timeout was relaxed.
- Windows native verification: The MSVC-first/MSYS2-second PATH fixed FFmpeg's sed/awk quoting failures; real compilation, wheel repair and isolated runtime validation now succeed. The strict PE dependency check recognizes Microsoft's system [Avicap32.dll](https://learn.microsoft.com/en-us/windows/win32/api/vfw/nf-vfw-capgetdriverdescriptiona), rather than requiring it inside the wheel.
- Cross-platform time-shift corrections: Windows drive-letter paths no longer receive the network-only FFmpeg protocol whitelist. Ownership checks accept exact LF/CRLF markers, allowing Windows abandoned-session recovery without changing QLockFile safety; real native-lock smoke reproduced the CRLF rejection before and passed after. Socket cleanup regressions now recognize Windows reset/abort semantics rather than requiring Unix EOF. All 92 local regressions, focused service types, formatting, syntax/undefined-name lint and workflow lint passed; integrated native Intel time-shift passed all 12 checks with a captured clean process exit.
- Feature (`verify/bundled-mpv`): Explicit Settings → Time-shift disk consent enables a temporary PyAV-remuxed, keyframe-segmented live buffer, default 2 GiB. Cache size is the only retention limit; older segments are replaced when storage fills. MPV/uosc's own timeline, speed control, Time-shift menu and Go live action handle rewind/catch-up; no separate Qt playback-control panel. Unknown M3U entries require explicit native “Buffer this stream”; known movies/episodes stay direct. Ingestion continues during pause/rewind through one capture pipeline; HLS uses its normal playlist/media requests, not a second provider player. Old segments, partial writes and active-reader leases are bounded; stop/channel change/exit and opt-out clean up asynchronously through ThreadCleanup. Source failure or incompatible selectable tracks are explicit errors.
- Main integration: Incorporated main through v1.13.8 into the MPV candidate. Retained the shared worker-lifetime fixes and missing-window configuration repair, together with the MPV-specific shutdown barrier, migration regressions and version 1.14.1. The retired embedded VLC window and python-mpv fallback are not restored.
- Initial verification: Intel macOS native playback of synthetic non-seekable MPEG-TS passed pause-with-recording, native timeline mouse seek (5.00s requested, 5.04s observed), native forward seek (16.226s to 26.226s), 2× catch-up returning to 1× near live, native rewind/Live controls, opt-out to direct playback, short-window eviction and clean shutdown. Existing native H.264/AAC VOD/resume/window/isolation smoke passed. All 39 regressions and focused five-module types/syntax/undefined-name lint passed. This initial proof used synthetic streams; subsequent real-channel verification is below. Time-shift remains unverified on Windows, Linux, ARM macOS and rebuilt frozen applications.
- Native time-shift hardening: Real MPV traces exposed two integration boundaries: live HLS requires waiting for a genuinely seekable cached GOP before a sub-segment seek, and recreating a paused Cocoa video output could block both the playback and IPC threads. Deferred cache seeks and retaining the native window during recording/reader replacements address these paths. The previously failing activation stress passed 12 consecutive direct-to-buffered transitions; expired paused positions, explicit unclassified-M3U activation and channel replacement cleanup passed. H.264/AAC remux preserved all 72 fixture video frames and decoded 282 audio frames; a source-ended buffer remained seekable to 5.95s of its six-second history.
- IPTV time-shift compatibility: HTTP(S) capture now uses verified requests, avoiding PyAV's macOS SecureTransport CA-file/protocol-whitelist failure. HLS selects the highest-bandwidth compatible video variant and preserves associated language tracks; a token-protected loopback bridge applies TLS verification, redirects and cookies to nested playlists, media, encryption keys and init segments, without allowing remote local-file reads. The new pinned `m3u8` dependency parses playlist structure. Network operations allow 60 seconds instead of the former 5-second open/2-second read limits. A spawned recorder process keeps cancellation bounded even when native I/O stalls; an owner-death watchdog closes its sockets if the GUI process disappears. Credentials travel through process bootstrap IPC, not argv or error messages.
- IPTV timestamp repair: A real channel supplied distinct complete AAC frames with equal DTS, including a preceding zero-duration packet. Remuxing now normalizes timestamps to MPEG-TS's 90-kHz clock and advances colliding DTS by one tick per stream, preserving frames without accumulating a frame-duration audio offset. Regressions cover decoded video/audio counts, separate language tracks, high-resolution input clocks and return to unchanged subsequent timestamps.
- Compatibility verification: All 58 tests, focused four-module mypy and syntax/undefined-name lint passed on Intel macOS. Synthetic native HLS playback passed a seven-second startup delay, highest-quality selection without fetching the lower variant, pause-with-recording, rewind, 2× catch-up and automatic return to 1×; cancelling a stalled source took 0.54 seconds. A previously failing real HTTPS HLS channel played at 2560×1440 with TLS verification enabled, rewound from 12s to 2s through native controls, returned near live and shut down with no remaining recorder child/session. Tests also exercise AES-128 decryption, fragmented MP4/byte ranges, nested TLS rejection/explicit opt-out, live reloads, owner death and preservation of an active sibling recorder. This does not establish compatibility with every provider or other packaging targets.
- Live clock recovery: Backward source DTS jumps now start a shared recording-clock epoch instead of aborting. Audio tracks can cross an epoch before or after video; late old-epoch packets keep their original mapping, and video refines the shared offset to preserve presentation/decode ordering. B-frame PTS reordering and minor AAC timestamp collisions remain distinct from clock resets. Five focused clock/AAC regressions passed, including repeated resets, both audio arrival orders, retained frame/sample counts and bounded history. Native Intel H.264/AAC playback crossed real MPEG-TS resets at 16s and 32s, rewound from 35s to 25.04s, returned near live and continued recording, with one upstream request and clean child/session shutdown. Native video captures were inspected; no source re-encoding is used.
- Time-shift cache settings: Replaced the competing minutes/MiB inputs with one 256 MiB–16 GiB disk-cache slider, default 2 GiB. Live estimates use explicit example bitrates (1080p at 8 Mb/s: about 36 minutes; 4K at 25 Mb/s: about 11 minutes at the default size); they are not resolution guarantees or encoding settings. Configuration migration removes the obsolete minutes limit while preserving consent and exact capacity, including non-round saved values. Recorder/process APIs and eviction now use storage only, with the existing allocation, active-reader and free-space safeguards. Native themed settings smoke passed mouse dragging, keyboard endpoints/steps, dynamic estimates, Save/reopen, Cancel and disabling; the rendered settings were inspected. Storage regressions retain four-hour-old history while it fits and evict only under byte pressure.
- Clock/cache verification: All 61 regressions, focused five-module mypy and syntax/undefined-name lint passed. Native playback and settings checks were run on Intel macOS; other platforms and rebuilt frozen bundles remain unverified.
- Packaging gate: The workflow now runs the full regressions plus real spawned time-shift/native-control scenarios in source and isolated frozen apps. Both MPV and private PyAV/FFmpeg source archives and notices accompany each target. The original 1.14.1 candidate at `332e013` passed direct source/frozen playback on all four platforms ([run 36358416623](https://github.com/ozankaraali/QiTV/actions/runs/36358416623)); that run predates the full-review fixes and does not satisfy the expanded time-shift release gate. No merge or release has been performed.
- Documentation: Refreshed README for the current 1.14 development UI: provider setup, sidebar navigation, Favorites/History/Resume, Search All, player modes, autoplay, EPG, exports, and portable mode. Removed implementation/CI detail and the outdated screenshot; collapsed source build instructions, identified the development branch, and removed the incompatible Linux `venv` installer command from the `uv` setup. Verified Markdown tables, command blocks, local links, and the expandable source section in a rendered browser preview.
- README screenshots: Captured the actual themed channel browser and bundled MPV time-shift controls using an isolated local M3U playlist, fictional channel names, generated logos and original synthetic video. No real provider accounts or broadcast footage were used. Verified category navigation, channel activation, buffering/rewind and clean player/recorder shutdown; inspected both final captures and embedded them with a short demo-content caption.
- Stability fix (also backported to main v1.13.7): Added shared `services/thread_cleanup.py` ownership for catalog, image, provider, link, verification, and update workers. Qt's native `finished` signal precedes deferred object destruction; retain Python/Qt wrappers until `wait(0)` succeeds, then deliver GUI completion and delete the thread. Closing the app waits asynchronously for these jobs, and downloaded updates are applied only after worker cleanup.
- Verification: On Intel macOS/Python 3.14.0/PySide6 6.11.2, the formerly crashing local M3U scenario completed 3,000 worker lifecycles on each branch. Subprocess regressions cover delayed native destruction, GUI responsiveness, and destruction of the consumer during cleanup. All 28 branch tests and 13 main tests passed, along with controlled-response updater success/error/cancellation smoke checks, focused seven-module type checks, and syntax/undefined-name lint. Broader mypy still reports 22 errors in unchanged export/dialog modules. This fix has not yet been verified in rebuilt frozen bundles or on other operating systems.
- Experiment v1.14.0 (`verify/bundled-mpv`, not main): Internal playback uses a private standalone MPV process with bundled uosc; external VLC and user-configured MPV remain separate modes. Removed the embedded Qt/libVLC player and its packaging paths.
- Verification: [CI run 35478147441](https://github.com/ozankaraali/QiTV/actions/runs/35478147441) at `64a8fb1` passed headless, native-window, and isolated frozen playback on Windows, Linux, Intel macOS, and ARM macOS. All frozen captures show video and uosc controls; configuration isolation, Unicode subtitle paths, resume, window controls, EOF handling, and process shutdown passed. Matching native source archives were produced. Earlier native Intel TLS and owner-lifecycle checks also passed.
- Measurements against v1.13.6 (release-format downloads, decimal MB): Linux 171.37 to 145.99 (14.8% smaller), Windows 121.82 to 79.13 (35.0%), Intel macOS 85.48 to 72.78 (14.9%), ARM macOS 75.17 to 67.81 (9.8%). The old universal-labelled Mac archive was ARM64-only. Native preparation took 7–22 minutes per target in this run; download sizes are not installed-disk, memory, or playback-quality measurements.
- Native caching: [Cold run 35504505896](https://github.com/ozankaraali/QiTV/actions/runs/35504505896) and [warm run 35505987002](https://github.com/ozankaraali/QiTV/actions/runs/35505987002) at `62968c2` passed all four platforms. Exact-key caches preserve the runtime, helper, licenses, and matching source archive; native inputs, toolchain/SDK, build flags, target, and runner image control invalidation. Restored files, permissions, and source checksums are validated before reuse; caches are saved only after successful packaging and playback checks. Cache boundary regressions cover application/native input separation, corruption, missing sources/licenses, and executable permissions.
- Measured full CI job times, cold to warm: Linux 17m54s to 2m43s, Windows 23m55s to 3m20s, ARM macOS 12m19s to 2m15s, Intel macOS 30m46s to 5m25s. All four warm jobs reused their caches without compilation, restored them in 2–10 seconds, and completed native verification in 0–2 seconds. Source and frozen playback checks still ran.
- Status: Playback and native runtime/source caching are verified on `verify/bundled-mpv`; the MPV migration remains isolated from main and release jobs were skipped in those runs. Temporary diagnostic workflows and scripts were removed. The thread-lifetime fix is shared with main v1.13.7. Do not merge or publish without the owner's decision; upstream prebuilt binaries remain deferred.
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
