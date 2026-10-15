# Phase 2B Live Pipeline Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every live MPEG-TS channel in the TV shell plays through the pipeline:
- pause, rewind, seek and Go live work;
- stay-live keeps playback near the live edge;
- providers' stream limits are respected;
- the transport shows the time-shift bar and the latency indicator;
- Settings offers disk time-shift with consent;
- source and frozen apps pass a pipeline smoke test on all four platforms.

**Architecture:**
- `services/live/live_session.py` (`LiveSession`) drives one channel through `LiveEngine` (Phase 2A) and `PlaybackSession` (Phase 1A). It owns position estimation, `StayLive` and the fallback to direct play.
- `services/connection_budget.py` serializes streams per provider.
- `TVController` (Phase 1B) swaps its direct live path for `LiveSession`.
- `Transport` gains a time-shift mode.

**Tech Stack:** Python 3.14, PySide6, python-vlc / libVLC 3, the Phase 2A pipeline, `unittest`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-15-tv-on-vlc-design.md`: §5.5 (transport), §6.3 (position), §6.6 (VLC pipeline profile), §6.7 (seek modes), §6.8 (stay-live), §6.9 (connection budget), §8 (settings), §9 (errors), §11 (smoke and CI). Port map: `.superpowers/sdd/phase2-port-map.md` §6 (settings UI) and §8.19 (seek and pause semantics).

## Global Constraints

- Everything in the Phase 2A Global Constraints still applies.
- **VLC profile for pipeline playback** (spec §6.6): `:network-caching=300`, `:live-caching=300`, `:clock-jitter=0`, `:ts-trust-pcr=0`, `:http-reconnect=false`. Audio time-stretch stays on (never `:no-audio-time-stretch`).
- **Position** (spec §6.3) is the start of the opened segment plus elapsed play time from `get_time()` deltas. When the deltas disagree with wall clock × rate by more than 1 s, wall clock × rate is used.
- **Seeking:**
  - seek granularity is the segment (about 2 s), and reopening the stream costs 0.3–0.8 s;
  - ←/→ = −10/+10 s; ⏮ = start of the current programme if it is buffered, otherwise the oldest buffered point; `L` = Go live;
  - speeds are 1×, 1.25×, 1.5× and 2×.
- **Fallback** (spec §9):
  - unsupported, failed before ready, engine unavailable, or not ready within 6 s: play directly with the LIVE profile, plus the toast "Time-shift isn't available for this channel" (once per channel per app run);
  - helper died after ready: reopen at live once the engine is available again.
- **Connection budget** (spec §6.9):
  - the Xtream limit is `max_connections`;
  - STB defaults to 1, M3U to unlimited, and the per-provider setting `max_streams` (0 = automatic) overrides both;
  - the old stream is released before the new one opens;
  - over the limit, the toast "Your provider allows N streams at a time" appears and nothing opens.
- **Settings:** port main's Time-shift tab: a consent checkbox plus a 256–16384 MiB slider (default 2048), with the estimates. Keys `timeshift_enabled` (true only for a literal JSON `true`) and `timeshift_max_mib` (clamped). Without consent, the RAM ring is 128 MiB.
- Commit trailers, staging and lint rules are as in Phase 2A.

## Review Focus

1. **Zapping during the 6 s readiness wait:** the old pipeline session must be closed and its late `sessionReady` ignored, so two streams never play.
2. **A long pause that outlives the ring:** resuming must reopen at the oldest buffered point with the toast "Rewound past the buffer". It must not freeze on a dead socket.
3. **Stay-live with a source that delivers in bursts** (HLS, about 2 s chunks): there must be no oscillation between rates and no reopen loops (hysteresis and the buffering guard).
4. **Helper death mid-play, and a second death:** the first reopens at live; the second falls back to direct play and stays there for the rest of the app run.
5. **Frozen apps:** the helper must spawn from the PyInstaller bundle on all four platforms (hidden imports, `freeze_support`). That is verified by the frozen smoke test, not assumed.

---

### Task 1: Pipeline VLC profile, time-shift config keys and the Settings tab

**Files:**
- Modify: `services/vlc_profile.py`, `config_manager.py`, `options.py`
- Test: `tests/test_vlc_profile.py`, `tests/test_config_tv.py`, `tests/test_options_timeshift.py`

**Interfaces:**
- Produces:
  - `PlayKind.LIVE_PIPELINE` with the options listed under Global Constraints;
  - `ConfigManager.timeshift_enabled: bool` and `ConfigManager.timeshift_max_mib: int` (main's `config_manager.py` 119–120, 298–305, 358–363, 382–383, ported);
  - in the Options dialog: a "Time-shift" tab (main's `options.py` 300–383, ported) and a "Max simultaneous streams" `QSpinBox` (0–10, 0 shown as "Automatic") in the provider editor, saved as provider key `max_streams`.
- Copy changes to the ported Time-shift tab:
  - consent checkbox: "Use disk for a longer time-shift buffer.\nQiTV deletes the oldest data automatically and cleans up when you stop, change channel or quit."
  - explanatory note: "Without disk, each live channel keeps about two minutes in memory."
  - no MPV wording anywhere.
- [ ] **Tests and steps:**
  1. Write the tests:
     - the profile options for `LIVE_PIPELINE`;
     - the config: defaults, consent only for a literal `true`, clamping, and the `timeshift_minutes` key removed on load (port main's tests);
     - the Options dialog (offscreen): with consent unchecked the slider is disabled; checking enables it; saving writes both keys; the provider editor round-trips `max_streams`.
  2. Run RED.
  3. Implement.
  4. Run GREEN and the suite.
  5. Commit (`feat: add time-shift settings and the pipeline VLC profile`).

---

### Task 2: Connection budget (`services/connection_budget.py`)

**Files:**
- Create: `services/connection_budget.py`
- Test: `tests/test_connection_budget.py`

**Interfaces:**
- Produces `class ConnectionBudget(QObject)` with:
  - signal `granted(int ticket)`;
  - `limit_for(provider: dict, xtream_max: int | None) -> int | None` (None means unlimited):
    - `provider["max_streams"]` when > 0;
    - else for Xtream, `xtream_max`;
    - else STB gives 1 and others None;
  - `request(provider_name: str, limit: int | None, *, purpose: str) -> int`, which returns a ticket. It is granted immediately when `in_use < limit`. Otherwise it is queued, and `granted` fires when a slot frees.
  - `release(ticket)`;
  - `cancel(ticket)`;
  - `in_use(provider_name) -> int`;
  - `holders(provider_name) -> list[str]` (purposes, for the toast).
- Only the newest queued request per purpose is kept: a newer request cancels an older queued one with the same purpose, which is exactly what zapping needs.
- [ ] **Tests:**
  - immediate grants under the limit;
  - queueing at the limit, with a grant on release;
  - for the same purpose only the newest queued request is granted;
  - unlimited providers;
  - `limit_for` for each provider type and override;
  - releasing an unknown ticket is a no-op.

  Then run RED, implement, run GREEN, run the suite, and commit (`feat: add a per-provider connection budget`).

---

### Task 3: `LiveSession` (`services/live/live_session.py`)

**Files:**
- Create: `services/live/live_session.py`
- Test: `tests/test_live_session.py`

**Interfaces:**
- Consumes:
  - `LiveEngine`: signals `available`, `sessionReady`, `sessionUpdated`, `sessionFailed`, `sessionEnded`, `sessionClosed`; methods `open_session`, `close_session`, `spans`, `snapshot`, `stream_url`, `is_available`;
  - `PlaybackSession`: `play`, `stop`, `set_paused`, `toggle_pause`, `set_rate`, `time_ms`, `state`, and the signals `stateChanged`, `ended`, `errorOccurred`;
  - `StayLive` and `Timeline`-like spans (`Span`);
  - `PlayKind`.
- Produces `class LiveSession(QObject)`, constructed as `__init__(self, engine, playback, *, tick_ms=250, ready_timeout_ms=6000, clock=time.monotonic)`.
  - **Signals:**
    - `modeChanged(str)`: `"pipeline"` or `"direct"`;
    - `timelineChanged(dict)`: keys `start`, `end`, `position`, `live_edge`, `lag`, `mode` (a StayLive Mode value), `rate`, `paused`, `ended`;
    - `notice(str)`;
    - `failed(str)`;
    - `released()`: the upstream is closed, so the budget can release.
  - **Methods:**
    - `start(url, *, headers, verify_ssl, latency, user_agent, disk: bool, max_bytes: int)`;
    - `stop()`;
    - `toggle_pause()`;
    - `seek_to(t: float)`;
    - `seek_relative(dt: float)`;
    - `go_live()`;
    - `set_speed(rate: float)`;
    - `set_latency(name)`;
    - `buffered_range() -> tuple[float, float]`;
    - `position() -> float`;
    - `mode`;
    - `is_pipeline() -> bool`.
- **Behaviour:**
  - **Start:**
    - if `engine.is_available()`: open a session and wait for `sessionReady` for that sid. On ready, play `stream_url(sid, oldest_seq)` with `LIVE_PIPELINE` and set `_base = span(oldest).start` (0.0 when the first segment is still open).
    - Otherwise, and on `sessionFailed` or a timeout before ready: close the session if one is open, play `url` directly with `PlayKind.LIVE` and `latency`, emit `notice("Time-shift isn't available for this channel")` and `modeChanged("direct")`.
  - **Tick** (`tick_ms`):
    - compute the position as in spec §6.3 (a wall-clock × rate fallback when `get_time()` deltas disagree by more than 1 s);
    - `lag = live_edge − position`;
    - `decision = stay_live.update(lag, playback.state() == BUFFERING)`;
    - apply a rate change through `playback.set_rate`;
    - if `reopen_live`, do `go_live()`;
    - emit `timelineChanged`.
  - **Seeking:**
    - `seek_to(t)`: `seq = locate(t)`, then reopen at `stream_url(sid, seq)` with `_base = span(seq).start` and the pause intent preserved (main's semantics, port map §8.19); `stay_live.user_seeked_back()` when `t < live_edge − target`.
    - `go_live()`: reopen at the newest complete segment whose start is at least `target` behind the live edge (else the newest), at rate 1.0, `stay_live.user_go_live()`.
    - `set_speed(rate)`: `playback.set_rate(rate)` plus `stay_live.user_speed(rate)`.
  - **Interruptions:**
    - playback `ended` or `errorOccurred` in pipeline mode while the session is not ended: reopen at `max(position, oldest)`. If the position was older than the oldest, emit `notice("Rewound past the buffer")`.
    - `sessionFailed(kind="internal")` after ready: wait for `available(True)` and the engine's reopen, then `go_live()`.
    - `available(False)`: switch to direct and keep it for this run.
  - **`stop()`:** stop playback, close the session, and emit `released()` on `sessionClosed`, or immediately in direct mode after `playback.stop()`.
- [ ] **Tests**, with fakes: a `FakeEngine` (QObject with the signals; spans driven by the test) and a `FakePlayback` (records calls, with a controllable `time_ms` and `state`). Cases:
  1. Ready gives pipeline playback of `stream_url(oldest)`.
  2. Unsupported gives direct playback with the notice.
  3. The 6 s timeout (use `ready_timeout_ms=50`) gives direct playback.
  4. A late `sessionReady` after `stop()` is ignored.
  5. `seek_to` maps to the right seq and preserves the pause.
  6. `go_live` picks the newest segment at least `target` behind.
  7. Lag above the target gives a 1.05 rate, and then 1.0 when back within 0.2 s.
  8. A position older than the oldest after a long pause gives a reopen plus "Rewound past the buffer".
  9. Helper death after ready leads to a reopen on available; `available(False)` gives direct mode.
  10. Wall-clock fallback: when `get_time` jumps by 30 s, the position follows wall time.
  11. `released()` is emitted after `sessionClosed`.

  Then run RED, implement, run GREEN, run the suite, and commit (`feat: drive live channels through the pipeline`).

---

### Task 4: Time-shift transport and the latency indicator

**Files:**
- Modify: `ui/overlays/transport.py`
- Test: `tests/test_transport_timeshift.py`

**Interfaces:**
- Produces on `Transport`:
  - `show_timeshift(title, subtitle)`;
  - `update_timeline(state: dict, ticks: list[tuple[float, str]])` (ticks are programme starts in timeline seconds, with labels);
  - `show_live_edge(lag_s: float, catching_up: bool)`;
  - new signals `goLiveRequested()`, `programmeStartRequested()`, `seekToRequested(float)`.
- **Painting** (mockup 04, "Live: time-shifted"):
  - the bar spans the buffered range (start to end) with the played part up to the position and a knob;
  - EPG ticks are 2 px white at 0.55 with labels at font 1.05u above;
  - the red live dot sits at the right end;
  - times row: `"Buffer from HH:MM"` on the left (wall clock = now − (live_edge − start)) and `"−MM:SS behind live"` on the right;
  - the buttons row adds ⏮ (programme start), the speed chip, and a Go live chip (accent fill, dark text) shown only when lag is at least target + 1 s.
  - **At the live edge** (mode LIVE and lag < target + 1 s): no bar. Bottom right shows the indicator pill `"Stay live ● 0.9 s behind source"` (the dot green, or amber with `"catching up 1.05×"` while the rate is above 1) next to the LIVE pill.
- [ ] **Tests:**
  - the formatting of `−MM:SS`;
  - a click at the buffer midpoint emits `seekToRequested(≈ mid)`;
  - the Go live chip is visible only when behind;
  - the indicator shows the catching-up text when the rate is above 1;
  - `grab()` is non-null in both states.

  Then run RED, implement, run GREEN, run the suite, and commit (`feat: show the time-shift bar and live latency`).

---

### Task 5: Wire the pipeline into the controller and window

**Files:**
- Modify: `ui/tv_controller.py`, `ui/tv_window.py`, `main.py`, `qitv-macos.spec`, `qitv-windows.spec`, `qitv-linux.spec`
- Test: `tests/test_tv_controller.py` (extend), `tests/test_tv_window.py` (extend)

**Behaviour:**
- **`TVWindow` construction:**
  - creates `LiveEngine(root_dir=<config_dir>/timeshift)` and calls `start()`;
  - creates `ConnectionBudget` and `LiveSession(engine, session)`;
  - passes them to the controller.
- **Window close:** `live_session.stop()`, then `engine.shutdown()`, then the existing teardown.
- **Controller live tune:**
  1. `budget.request(provider, budget.limit_for(provider, catalog.max_connections()), purpose="main")`.
  2. On `granted`, `live_session.start(url, headers={"User-Agent": ua}, verify_ssl=..., latency=config.live_latency, user_agent=ua, disk=config.timeshift_enabled, max_bytes=(config.timeshift_max_mib if disk else 128) * 1024 * 1024)`.
  3. Before a new tune: `live_session.stop()`, then release the ticket on `released`.
  4. The banner shows immediately, as before.
  5. If the limit is 0 or exceeded with other holders (Phase 4 PiP), toast `f"Your provider allows {limit} streams at a time"`.
- **Keys during live:**
  - `SEEK_BACK` / `SEEK_FWD`: `live_session.seek_relative(∓10)`;
  - `PLAY_PAUSE`: `live_session.toggle_pause()`;
  - `GO_LIVE`: `live_session.go_live()`;
  - the transport's `programmeStartRequested`: seek to the start of the current EPG programme (from `catalog.programmes`) when buffered, else to the oldest buffered point;
  - `speedRequested`: `live_session.set_speed`;
  - `timelineChanged`: update the transport (time-shift or live-edge mode) with ticks from `catalog.programmes(channel, now, 6)` mapped to timeline seconds.
- **Settings:** a changed `live_latency` calls `live_session.set_latency`.
- **PyInstaller specs:** add `services.live.worker`, `m3u8` and `cryptography` to `hiddenimports`. Make sure `main.py` calls `multiprocessing.freeze_support()` first (Phase 1B Task 8 added it; verify).
- [ ] **Tests:**
  - controller (fakes):
    - a tune requests the budget and starts the live session on grant;
    - a second tune stops the first and starts the new one only after `released`;
    - `SEEK_BACK` during live calls `seek_relative(-10)`;
    - `GO_LIVE` calls `go_live`;
    - a budget denial shows the toast;
  - window: with the local fixture provider from Phase 1B, a channel tune results in a pipeline-mode session (the URL starts with `http://127.0.0.1:`).

  Then run RED, implement, run GREEN, run the suite, and commit (`feat: play live channels through the time-shift pipeline`).

---

### Task 6: Pipeline smoke test (source and frozen) and CI

**Files:**
- Create: `tests/fixtures/live.ts`. A real, decodable fixture: 12 s, 320×180, 25 fps, H.264 baseline with GOP 25 plus AAC, about 600 KB. Generate it locally with `/opt/local/bin/ffmpeg -f lavfi -i testsrc2=s=320x180:r=25 -f lavfi -i sine=frequency=440 -t 12 -c:v libx264 -profile:v baseline -g 25 -keyint_min 25 -sc_threshold 0 -b:v 400k -c:a aac -b:a 64k -f mpegts tests/fixtures/live.ts` and commit the output. ffmpeg is a developer tool only, never a runtime or build dependency.
- Modify: `scripts/smoke_tv.py` (add a `--pipeline` phase) and `.github/workflows/main.yml` (smoke on the frozen app too)
- Test: the smoke run itself

**Scenario (`--pipeline`):**
1. A local paced HTTP server serves `live.ts` in a loop at real-time rate (188 × 64-byte chunks paced by PTS or by bitrate) as an endless stream. The M3U fixture points one channel at it.
2. Tune it, then wait for a pipeline-mode session (stream URL on loopback) and PLAYING.
3. Pause for 4 s, resume, and check that the timeline lag is at least 3 s.
4. Press `L` and check that the lag drops below target + 1.5 s within 6 s.
5. Seek −10 s and check that the position moves back by 8–12 s.
6. Set speed 1.5× and check that the rate is applied, then that it returns to 1.0 when back at live.
7. Close and check that no helper process remains and that the `timeshift` dir is empty.

Every check is recorded in `smoke.json`.

**CI:**
- after the PyInstaller step, run the frozen app's smoke mode;
- add a `--smoke <args>` entry to `main.py` that runs `scripts/smoke_tv.py`'s logic inside the frozen app, gated by the environment variable `QITV_SMOKE=1`, so release users never trigger it;
- upload the artifacts with `if: always()`.

- [ ] **Steps:**
  1. Generate and commit the fixture.
  2. Implement the pipeline phase.
  3. Run it locally on macOS (source) and inspect `smoke.json` and the screenshots.
  4. Build the frozen app locally (`uv run pyinstaller qitv-macos.spec`) and run its smoke.
  5. Update CI and validate the YAML.
  6. Commit (`test: smoke the live pipeline in source and frozen apps`).
  7. Push `tv/vlc` and confirm the four platforms are green. If Windows or Linux fails the overlay or pipeline checks, record the failure in the ledger and fix it in a follow-up task. The spec D4 fallback applies only to overlay failures.
