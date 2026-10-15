# Phase 4 PiP and Swap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Watch a second channel in a small picture-in-picture surface, and swap the two with one key, without restarting either stream. Each keeps its own time-shift ring, so after an ad you swap back and resume where you left, or go live. This uses the spec's terms PiP, swap and "coming back after an ad".

**Architecture:**
- `services/sessions.py` (`SessionManager`) owns two **slots**, `main` and `pip`. Each slot has its own `PlaybackSession` (Phase 1A), `LiveSession` (Phase 2B) and connection-budget ticket (Phase 2B).
- Roles describe which slot is **large**; swapping changes roles and geometry only.
- `ui/pip.py` (`PipFrame`) places the second native `VideoSurface` over the main surface: 25% width, snapped to a corner.
- A PiP label overlay in the `OverlayHost` shows the small surface's channel and time-shift state.
- The controller routes zapping, the banner and the transport to whichever slot is large.

**Tech Stack:** Python 3.14, PySide6, python-vlc / libVLC 3, `unittest`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-15-tv-on-vlc-design.md`:
- §7 (Sessions; PiP and swap);
- §6.9 (connection budget: purpose `"pip"`);
- §5.3 (keys `P`, `S`);
- §12 (Phase 4 exit: "Smoke: two sessions, swap without restart").

## Global Constraints

- Everything in the Phase 1B and Phase 2B Global Constraints still applies (overlays, units, theme, threading, logging, tests, lint, trailers).
- **PiP geometry:**
  - width 25% of the player surface, height width × 9/16;
  - margin 2u from the nearest edges (u = 1% of the player width);
  - one of four corners, default bottom-right;
  - the corner is remembered in config key `pip_corner` (`"br"`, `"bl"`, `"tr"`, `"tl"`).
- **Swap** exchanges geometry and stacking only. The `PlaybackSession`s are never stopped or re-pointed (`set_window` is called once per surface). **Audio follows the large surface:** the small slot is muted and the large one keeps the user's mute state.
- **Budget:** PiP requests a ticket with `purpose="pip"`. When the provider's limit cannot hold a second stream, nothing opens and the toast reads `f"Your provider allows {limit} stream{'s' if limit != 1 else ''} at a time"`. A queued PiP request is cancelled when the user closes PiP or zaps the PiP target away.
- **Keys:**
  - `P` opens PiP with the previous channel, or closes PiP if it is open;
  - `S` swaps (only while PiP is open; otherwise toast `"Open picture-in-picture first (P)"`);
  - the drawer context menu gains "Open in picture-in-picture" for live channels;
  - `Alt+P` stays the v1.13.8 mini player.
- **Zapping, digits, the banner, the transport, seek and Go live** always act on the large slot. The small slot keeps recording into its ring and keeps playing muted.
- Live channels only. Opening PiP during VOD shows the toast `"Picture-in-picture is for live TV"`.

## Review Focus

1. **Provider limit 1** (most STB): `P` must never open a second upstream. The toast appears and the main stream is untouched (Task 1).
2. **Swapping while either slot is still connecting, buffering or in direct fallback mode:**
   - no restart;
   - the correct audio;
   - the transport shows the newly large slot's real state ("−3:20" when shifted) (Tasks 1 and 3).
3. **Closing PiP or zapping the large slot onto the channel the PiP is showing:** the duplicate must not open a second stream for the same channel. Swap instead (Task 3).
4. **Window move, resize, fullscreen and minimize with PiP open:**
   - the PiP surface keeps its corner;
   - the PiP label overlay stays glued over it;
   - after leaving fullscreen nothing is left floating (Tasks 2 and 4).
5. **App close with two live sessions:**
   - both sessions stop;
   - both tickets are released;
   - the helper has no sessions left;
   - no time-shift dirs remain (Task 4 smoke).

---

### Task 1: SessionManager (`services/sessions.py`)

**Files:**
- Create: `services/sessions.py`
- Test: `tests/test_sessions.py`

**Interfaces:**
- Consumes:
  - `PlaybackSession` (`play`, `stop`, `toggle_mute`, `volume`, `set_volume`, plus a new `set_muted(bool)` if absent: add it in `services/playback.py` as a thin `audio_set_mute` wrapper, with a test);
  - `LiveSession` (`start`, `stop`, `released`, `timelineChanged`, `modeChanged`);
  - `ConnectionBudget` (`request`, `release`, `cancel`, `granted`, `limit_for`, `holders`).
- Produces:
  ```python
  @dataclass
  class Slot:
      name: str                      # "main" | "pip"
      playback: PlaybackSession
      live: LiveSession
      channel: dict | None = None
      group_key: str | None = None
      ticket: int | None = None

  class SessionManager(QObject):
      largeChanged = Signal(str)             # name of the slot that is now large
      pipStateChanged = Signal(bool)         # PiP open/closed
      denied = Signal(int)                   # limit (budget refused PiP)
      slotTimeline = Signal(str, dict)       # slot name, LiveSession timeline dict
      def __init__(self, main: Slot, pip: Slot, budget, *, parent=None)
      def large(self) -> Slot; def small(self) -> Slot | None   # small is None when PiP is closed
      def pip_open(self) -> bool
      def open_pip(self, channel: dict, *, provider: dict, xtream_max: int | None,
                   start_kwargs: dict) -> None
      def close_pip(self) -> None
      def swap(self) -> bool                 # False when PiP is closed
      def slot_showing(self, channel: dict) -> Slot | None   # ZapContext.same match
  ```

**Behaviour:**
- **Initial state:** `main` is large and PiP is closed.
- **`open_pip`:**
  - when PiP is already open, it re-tunes the PiP slot: stop its live session, release its ticket on `released`, then request again;
  - otherwise it requests `budget.request(provider_name, budget.limit_for(provider, xtream_max), purpose="pip")`;
  - when that would exceed the limit *with the main slot holding the only slot*, it emits `denied(limit)` and cancels the queued request. A PiP must never wait in the queue for the main stream to end;
  - on `granted` for its ticket: `pip.live.start(**start_kwargs)` and `pip.playback.set_muted(True)`, then emit `pipStateChanged(True)`.
- **`close_pip`:**
  - when the PiP slot is large, swap back first, so the main slot becomes large;
  - then stop the PiP slot's live session, release its ticket on `released`, and emit `pipStateChanged(False)`.
- **`swap`:**
  - exchange the large and small roles;
  - mute the new small slot and unmute the new large one (`set_muted(False)` restores the user's mute state, tracked in the manager);
  - emit `largeChanged(name)`.

  No `play`/`stop`/`start` calls happen during a swap.
- **Timelines:** re-emit each slot's `timelineChanged` as `slotTimeline(name, dict)`.

- [ ] **Step 1: Write failing tests** with fakes: a `FakePlayback` (records play/stop/set_muted), a `FakeLive` (records start/stop and emits `released` on stop) and the real `ConnectionBudget`.
  1. Opening PiP with a limit of 2 starts the PiP live session muted and emits `pipStateChanged(True)`.
  2. With a limit of 1 (main holding it), `denied(1)` is emitted, nothing starts, and no ticket stays queued.
  3. `swap()` makes `pip` large, mutes `main`, unmutes `pip`, and calls no play/stop/start (assert on the call logs).
  4. `swap()` twice restores the roles.
  5. `close_pip()` while `pip` is large swaps back first, then stops the PiP slot and releases its ticket after `released`.
  6. Re-opening PiP with another channel stops the old PiP live session and starts the new one only after `released`.
  7. `slot_showing(channel)` finds the PiP slot's channel.
  8. The user's mute is respected: muted before the swap means both are muted after it.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type, commit (`feat: add the two-slot session manager`).

---

### Task 2: PiP frame and label overlay (`ui/pip.py`, `ui/overlays/pip_label.py`)

**Files:**
- Create: `ui/pip.py`, `ui/overlays/pip_label.py`
- Test: `tests/test_pip_frame.py`

**Interfaces:**
- `PipFrame(QObject)`, constructed as `PipFrame(container: QWidget, main_surface: VideoSurface, pip_surface: VideoSurface, config)`:
  ```python
  swapRequested = Signal()                # click on the small surface
  cornerChanged = Signal(str)
  def show_pip(self) -> None              # makes the PiP surface visible at the saved corner
  def hide_pip(self) -> None
  def set_large(self, name: str) -> None  # "main" | "pip": that surface fills the container; the other goes to the corner and is raised
  def small_rect(self) -> QRect           # container coordinates; empty when hidden
  def corner(self) -> str
  ```
- `PipLabel(Overlay)`:
  - `set_info(name: str, state: str)`, where `state` is `"LIVE"`, `"−MM:SS"` or `"Connecting…"`;
  - `follow(rect: QRect)` (host coordinates);
  - painting: a pill at the top-left inside the small rect (1u inset): the channel name at font 1.0u, a red LIVE dot or the `−MM:SS` text in accent, and a close `×` at the top-right that emits `closeRequested()`;
  - `mask_region()` covers the pill and the × only, so clicks elsewhere on the small video reach the surface.

**Behaviour:**
- Both surfaces are native child widgets of `container` (the player page), so no reparenting happens and VLC's drawables never change.
- The large surface gets `setGeometry(container.rect())`. The small one gets its corner rect (Global Constraints) and `raise_()`.
- On container resize, the layout is recomputed.
- **Mouse on the small surface** (an event filter on it; `VideoSurface` already disables VLC's own mouse handling):
  - a press and release without movement emits `swapRequested`;
  - a drag of at least 8 px moves the surface with the cursor; on release it snaps to the nearest corner, saves `config.pip_corner` and emits `cornerChanged`.
- **On macOS**, native child views can stack below a sibling after a geometry change. After every layout, call `raise_()` on the small surface and verify with a test that `container.childAt(small_rect().center())` is the small surface.

- [ ] **Step 1: Write failing tests** (offscreen, with plain `QWidget`s standing in for `VideoSurface`, which exposes the same geometry API):
  1. `show_pip` places the small surface at bottom-right, 25% wide, 16:9, 2u margins.
  2. `set_large("pip")` swaps the geometries and raises the new small one.
  3. A drag to the top-left quadrant snaps to `tl` and saves `config.pip_corner`.
  4. A click without movement emits `swapRequested`.
  5. Resizing the container keeps the corner.
  6. `PipLabel` emits `closeRequested` when × is clicked, and its mask excludes the area outside the pill.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type. Render `scratchpad/pip/pip.png` with two coloured stand-in surfaces and the label, and view it. Commit (`feat: add the PiP frame and label`).

---

### Task 3: Controller and window integration

**Files:**
- Modify: `ui/tv_window.py`, `ui/tv_controller.py`, `ui/keymap.py`, `ui/overlays/drawer.py` (context menu entry), `config_manager.py` (`pip_corner` key)
- Test: `tests/test_tv_controller.py` (extend), `tests/test_tv_window.py` (extend)

**Behaviour:**
- **TVWindow:**
  - builds a second `VideoSurface`, `PlaybackSession`, `LiveSession`, the `SessionManager`, a `PipFrame` and a `PipLabel`;
  - calls `set_window` once per surface after the window is shown;
  - the `OverlayHost` stays glued to the container. The `PipLabel` follows `PipFrame.small_rect()` mapped into host coordinates.
- **Keymap:** `P` gives `PIP` and `S` gives `SWAP` in the player context. `Alt+P` stays `MINI_PLAYER`.
- **Controller:**
  - The existing single-session paths (`tune`, seek, pause, Go live, transport updates, banner, `zap.tuned`) act on `sessions.large()`.
  - `PIP`:
    - if PiP is open, `close_pip()`;
    - else take the target from the zap controller's previous channel; with none, toast `"No previous channel yet"`;
    - request its link (one in flight, as in Phase 1B), then `sessions.open_pip(channel, provider=…, xtream_max=catalog.max_connections(), start_kwargs=…)`, with the same `start_kwargs` as a main tune.
  - `denied(limit)` shows the budget toast.
  - `SWAP` (or `PipFrame.swapRequested`):
    - `sessions.swap()`, then `PipFrame.set_large(name)`;
    - the banner shows the newly large channel;
    - `ZapContext` switches to the newly large slot's `group_key`/channels when they differ (each slot remembers its context);
    - the transport immediately renders the large slot's last timeline (`slotTimeline`): time-shifted shows `−MM:SS`, live shows the LIVE state.
  - **Zapping the large slot onto the PiP's channel** (Review Focus 3): do not open a second stream; perform a swap instead.
  - The drawer context menu "Open in picture-in-picture" (live items) opens PiP with that channel.
  - The `PipLabel` shows the small slot's channel name and its state from `slotTimeline` (`"−MM:SS"` when the lag is above target + 1 s, else `"LIVE"`; `"Connecting…"` before the first timeline). Its `closeRequested` calls `close_pip()`.
  - **VOD:** `PIP` during VOD shows the toast from Global Constraints. Opening the Library closes PiP first (Phase 3 `stop_for_library` stops both slots).
  - **`shutdown()`:** `close_pip()`, then the main stop, then the existing teardown.

- [ ] **Step 1: Write failing tests** (controller fakes as in Phase 1B/2B, plus a `FakeSessions` recording calls, or the real `SessionManager` over fakes):
  1. `P` with a previous channel requests its link, then `open_pip` with that channel.
  2. `P` again closes PiP.
  3. `S` swaps: the banner shows the PiP channel, `zap.context` switches to its group, and the transport renders the PiP slot's last timeline.
  4. A ↓ zap after a swap tunes the large (former PiP) slot. The small slot receives no calls.
  5. Zapping the large slot onto the PiP's channel triggers a swap, not a tune (no `request_link`).
  6. `denied(1)` toasts `"Your provider allows 1 stream at a time"`.
  7. `P` during VOD toasts and opens nothing.
  8. Window: with two local fixture channels (Phase 1B fixture provider), opening PiP shows two PLAYING sessions, and swapping keeps both PLAYING with unchanged play counts. Assert that each fake/real session's play count does not increase on swap.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type. Run the app manually with the local two-channel fixture: open PiP, swap, drag it to each corner, close it, then describe it in the report. Commit (`feat: add picture-in-picture and swap`).

---

### Task 4: Smoke test and docs

**Files:**
- Modify: `scripts/smoke_tv.py` (a `--pip` phase), `.github/workflows/main.yml` (pass `--pip` in source and frozen smoke), `README.md` (keys), `ui/keymap.py` (`SHORTCUTS` gains P and S)
- Test: the smoke run itself

**Scenario (`--pip`):** with the paced live fixture server from Phase 2B serving two channels (the same file under two paths):
1. Tune channel A, then channel B. Press `P` and wait until both slots are PLAYING; channel A is now the PiP.
2. Check:
   - `PipFrame.small_rect()` is 25% of the width, in the bottom-right corner;
   - the `PipLabel` overlay's geometry matches it in host coordinates.
3. Press `S`:
   - both sessions stay PLAYING with **unchanged play counts** (no restart);
   - the large slot is now A, and A's audio is unmuted while B's is muted.
4. Pause the large slot for 4 s, press `S` twice, and check that the large slot is still shifted (lag ≥ 3 s).
5. Move and resize the window, then repeat the geometry checks from step 2.
6. Press `P` (close):
   - one session remains;
   - the PiP ticket is released;
   - the helper reports a single session.
7. Close the window. No helper process remains and the time-shift dirs are empty.

Every check is recorded in `smoke.json`, with screenshots `pip-01-open.png` and `pip-02-swapped.png`.

- [ ] **Steps:**
  1. Implement the phase.
  2. Run it locally (source) and inspect the JSON and screenshots.
  3. Build the frozen app and run its smoke with `--pip`.
  4. Update CI and validate the YAML.
  5. Update the README key table and the `SHORTCUTS` entries.
  6. Commit (`test: smoke picture-in-picture and swap`).
  7. Push `tv/vlc` and confirm all four platforms are green.
