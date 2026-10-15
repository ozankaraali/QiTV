"""Play one live channel through the time-shift pipeline, or directly when it cannot.

`LiveSession` lives on the GUI thread. `start()` opens a `LiveEngine` session (also before
the helper's first hello: the engine queues it) and waits for its `sessionReady`; then VLC
plays the loopback stream from the oldest buffered segment with the pipeline profile.

Falling back (spec §9) never overlaps two upstream connections: the session is closed and
the provider URL plays directly (`PlayKind.LIVE`) once the engine confirms the close, or
after `_CLOSE_CAP_MS`. Only `available(False)` plays directly at once: no helper, so no
upstream and no buffer, is left (also for a session whose input had ended). Unsupported
sources, failures before ready and a ready that does not come within `ready_timeout_ms`
fall back with "Time-shift isn't available for this channel"; after `available(False)`
every start plays directly for the rest of the run.

Position (spec §6.3): the start of the opened segment plus VLC's `get_time()` deltas; a
delta further than 1 s from wall clock × rate (counted only while VLC plays) is replaced by
wall clock × rate, so a timestamp jump in the stream does not move the position. Seeking
reopens the stream at a segment (about 2 s granularity) and keeps the pause intent: a
reopened stream is paused again as soon as it plays (port map §8.19). A seek to within the
target of the live edge goes live instead, still keeping the pause; only `go_live()` (the
Go live button) unpauses. A pause, from any mode (a catch-up included), sets 1x and makes
the session time-shifted (StayLive SHIFTED), whatever reopens it later, and stay-live
leaves it alone: no rate changes, no reopen at live. After the pause it plays on from
there at 1x, without catching up, until Go live. A pause or resume pressed before ready or
while recovering is remembered: the stream that opens next follows it.

Every `tick_ms` the lag behind the live edge feeds `StayLive`, whose target is the latency
setting's or the HLS burst (`burst_s`), whichever is larger: segments that arrive whole
would make a smaller target underrun at every segment. With bursts the lag is a sawtooth
as tall as a burst, so the lag used (and reported) is its mean, in closed form: the current
lag plus the time since the edge last moved, less half a burst (see `_lag`); the step a
landing burst adds is eased in over a few bursts, since it holds the jitter of when a
burst lands (see `_smooth`). Held against the instantaneous peak, the rate would change at
every burst and settle only once the lag before each burst is under 0.75 s. Go live on a
bursty source picks among every segment start, the open one included, by the mean lag it
would give (see `_live_seq`): never below the target less a quarter second, since
stay-live cannot slow down to win buffer back; the nearest the target within stay-live's
band; else the nearest above it ("behind live" for a moment while stay-live catches up).

Interruptions while playing from the pipeline:
- VLC ended or failed: reopen at the position, or at the oldest buffered point with "Rewound
  past the buffer" when the ring evicted it (after a long pause, say). Resuming from a pause
  checks for that eviction first. Nothing is reopened once the session ended: playback runs
  to the end of the buffer. Interruptions that do not reach playing again within
  `ready_timeout_ms` fall back.
- `sessionFailed("disk")`: the helper went on in RAM without the history: a notice, then go
  live on the RAM ring's first state (`sessionUpdated`), not on the old ring's spans. A
  seek, Go live or resume meanwhile waits for that state too. Fall back when it does not
  come within `ready_timeout_ms` or `burst_s` + 2 s, whichever is longer.
- `sessionFailed("internal")` means a helper death: wait for the engine's reopen of the
  session, then go live; after `_RECOVERY_MS` without it, fall back. (Before ready, the
  ready timeout bounds the same wait.)
- Any other kind is final (the source gave up, the helper's session "crashed", or a kind
  this code does not know): at the live edge, fall back with "Lost the time-shift buffer;
  playing live". Time-shifted behind the target, keep playing the buffer ("Lost the
  connection; playing what's buffered", timeline `ended`), and fall back when it runs out
  or on Go live. Before ready, fall back with "Time-shift isn't available…".

Engine signals for any session but the current one are ignored, and so are failures after
`sessionEnded` (only `sessionClosed` follows those). `released()` follows `stop()` once no
session this object opened is still open (or after `_CLOSE_CAP_MS`): at once in direct
mode. A `start()` during that wait opens its session only after `released()`.
`failed(message)` reports a playback error in direct mode; pipeline playback errors are
recovered here and never reported.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from enum import Enum
import logging
import math
import time
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.live.stay_live import TARGETS, Mode, StayLive
from services.live.timeline import Span
from services.playback import PlaybackState, Session
from services.vlc_profile import PlayKind

if TYPE_CHECKING:
    from services.live.engine import LiveEngine

_LOG = logging.getLogger(__name__)

# How far behind the provider's live point an HLS source starts, per latency (Phase 2A).
LIVE_DELAY = {"low": 3.0, "normal": 6.0, "stable": 12.0}
UNAVAILABLE = "Time-shift isn't available for this channel"
LOST = "Lost the time-shift buffer; playing live"
BUFFERED = "Lost the connection; playing what's buffered"
DISK = "Disk time-shift isn't available; using memory"
REWOUND = "Rewound past the buffer"
_CLOSE_CAP_MS = 2000  # a close not confirmed by then counts as done
_RECOVERY_MS = 10_000  # a helper restart must bring the session back by then
_JUMP_S = 1.0  # get_time deltas further than this from wall clock x rate are not trusted
_EXPIRED_S = 0.5  # a position this far before the oldest buffered point is gone
_EASE_BURSTS = 4  # how many bursts the step of a landing burst is eased in over
_BELOW_TARGET_S = 0.25  # Go live never lands a mean lag further below the target
_NUDGE_S = 0.75  # StayLive nudges the rate beyond the target plus this
_PLAYING = PlaybackState.PLAYING.value
_PAUSED = PlaybackState.PAUSED.value


class _Phase(Enum):
    IDLE = "idle"
    STARTING = "starting"  # start() waits for an earlier session to close
    WAITING = "waiting"  # the pipeline session is opening; nothing plays yet
    PIPELINE = "pipeline"  # VLC plays the loopback stream
    RECOVERING = "recovering"  # the helper died: waiting for the engine's reopen
    FALLING_BACK = "falling_back"  # direct play waits for the session to close
    DIRECT = "direct"  # VLC plays the provider URL


class _Retry(Enum):
    LIVE = "live"
    RESUME = "resume"


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _locate(spans: Sequence[Span], t: float) -> Optional[Span]:
    """The closed span holding `t`, clamped to the closed spans; None when there is none."""
    if not spans:
        return None
    for span in spans:
        if t < span.end:
            return span
    return spans[-1]


class LiveSession(QObject):
    modeChanged = Signal(str)  # "pipeline" or "direct"
    timelineChanged = Signal(dict)
    notice = Signal(str)
    failed = Signal(str)
    released = Signal()

    def __init__(
        self,
        engine: LiveEngine,
        playback: Session,
        *,
        tick_ms: int = 250,
        ready_timeout_ms: int = 6000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._playback = playback
        self._clock = clock
        self._ready_timeout_s = ready_timeout_ms / 1000.0
        self._ready_timer = QTimer(self, singleShot=True, interval=ready_timeout_ms)
        self._ready_timer.timeout.connect(self._on_ready_timeout)
        self._tick_timer = QTimer(self, interval=tick_ms)
        self._tick_timer.timeout.connect(self._tick)
        self._recovery_timer = QTimer(self, singleShot=True)
        self._recovery_timer.timeout.connect(self._on_recovery_timeout)
        self._close_timer = QTimer(self, singleShot=True)
        self._close_timer.timeout.connect(self._on_close_cap)
        self._phase = _Phase.IDLE
        self._mode = ""
        self._dead = False  # available(False): direct play for the rest of the run
        self._sid: Optional[str] = None
        self._unclosed: set[str] = set()  # closed here, not yet confirmed by the engine
        self._release_owed = False  # stop() was called: released() once nothing is open
        self._fallback_notice = ""
        self._url = ""
        self._latency = "normal"
        self._user_agent: Optional[str] = None
        self._open_args: dict[str, Any] = {}
        self._stay = StayLive()
        self._snapshot_cache: dict[str, Any] = {}
        self._ended = False  # the session's input is over: no reopen, no stay-live
        self._source_lost = False  # ended by a source failure: fall back when it runs out
        self._deferred: Optional[float] = None  # since a disk failure: act on the next state
        self._after_update: Optional[float] = None  # then seek here; None: go live
        self._paused = False
        self._rate = 1.0
        # Position: _base (the opened segment's start) + _elapsed, sampled from VLC.
        self._base = 0.0
        self._elapsed = 0.0
        self._last_ms = 0
        self._last_wall = 0.0
        self._retry: Optional[_Retry] = None  # a reopen that could not be made yet
        self._failing_since: Optional[float] = None  # first interruption since VLC played
        self._opening = False  # inside playback.play(): its errors are retried on a tick
        self._edge_seen: Optional[float] = None  # the live edge last seen
        self._edge_moved = 0.0  # when it last moved (for bursts: when the last one landed)
        # Bursty sources: (estimate, correction, when, position) at the last tick.
        self._smoothed: Optional[tuple[float, float, float, float]] = None
        engine.available.connect(self._on_available)
        engine.sessionReady.connect(self._on_ready)
        engine.sessionUpdated.connect(self._on_updated)
        engine.sessionFailed.connect(self._on_failed)
        engine.sessionEnded.connect(self._on_ended)
        engine.sessionClosed.connect(self._on_closed)
        playback.stateChanged.connect(self._on_state)
        playback.ended.connect(self._on_interrupted)
        playback.errorOccurred.connect(self._on_error)

    # ---- public ------------------------------------------------------------------------
    @property
    def mode(self) -> str:
        """The current channel's mode once it plays, "pipeline" or "direct"; else ""."""
        return self._mode

    def is_pipeline(self) -> bool:
        return self._mode == "pipeline"

    def start(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        verify_ssl: bool,
        latency: str,
        user_agent: Optional[str],
        disk: bool,
        max_bytes: int,
    ) -> None:
        self.stop()
        self._url, self._user_agent = url, user_agent
        self._latency = latency if latency in TARGETS else "normal"
        self._open_args = {
            "headers": dict(headers),
            "verify_ssl": verify_ssl,
            "disk": disk,
            "max_bytes": max_bytes,
        }
        self._stay = StayLive(self._latency)
        self._snapshot_cache = {}
        self._ended = self._source_lost = self._paused = False
        self._deferred = None
        self._after_update = self._retry = self._failing_since = None
        self._edge_seen = self._smoothed = None
        self._base = self._elapsed = 0.0
        self._last_ms, self._last_wall = 0, self._clock()
        self._set_rate(1.0)  # every channel starts at normal speed
        if self._unclosed:
            self._phase = _Phase.STARTING  # after the earlier upstream closed (released)
            return
        self._begin()

    def stop(self) -> None:
        """Stop playback and close the session; `released()` follows (see the docstring)."""
        if self._phase is _Phase.IDLE:
            return
        self._stop_timers()
        self._phase = _Phase.IDLE
        self._mode = ""
        sid, self._sid = self._sid, None
        self._playback.stop()
        if sid is not None:
            self._close(sid)
        self._release_owed = True
        if not self._unclosed:
            self._all_closed()

    def toggle_pause(self) -> None:
        if self._phase is _Phase.DIRECT:
            self._playback.toggle_pause()
            return
        playing = self._phase is _Phase.PIPELINE
        if not playing and self._phase not in (_Phase.WAITING, _Phase.RECOVERING):
            return
        if not self._paused:
            # From any mode, a catch-up included: paused at 1x, time-shifted. Before ready
            # or while recovering it is remembered: the stream that opens next is paused.
            self._paused = True
            self._set_rate(1.0)
            self._stay.user_paused()
            if playing:
                self._playback.set_paused(True)
            return
        self._paused = False
        if not playing or self._deferred is not None:
            return  # the stream that opens next plays
        position = self._advance()
        if position < self._buffer_start() - _EXPIRED_S:
            self._reopen_at_position()  # the ring evicted it during the pause
        else:
            self._playback.set_paused(False)

    def seek_to(self, t: float) -> None:
        if self._phase is not _Phase.PIPELINE or self._sid is None:
            return
        snapshot = self._snapshot()
        if t >= self._live_edge(snapshot) - self._target(snapshot):
            self._go_live(unpause=False)  # at the edge: not a segment back, and back to LIVE
            return
        if self._deferred is not None:
            self._after_update = t  # the spans still describe the lost disk ring
            return
        span = _locate(self._engine.spans(self._sid), t)
        if span is None:  # nothing buffered yet
            return
        self._stay.user_seeked_back()
        self._open_at(span.seq)

    def seek_relative(self, dt: float) -> None:
        self.seek_to(self.position() + dt)

    def go_live(self) -> None:
        """The Go live button: back to the live edge at 1x, playing (not paused)."""
        self._go_live(unpause=True)

    def _go_live(self, *, unpause: bool) -> None:
        """Reopen at the live target behind the edge, at 1x; a paused one stays paused."""
        try:
            if self._phase is _Phase.PIPELINE:
                if unpause:
                    self._paused = False
                if self._deferred is not None:  # stale spans: the RAM ring's next state does
                    self._after_update = None
                    return
                if self._source_lost:
                    self._fall_back(LOST)  # nothing newer is buffered: play the channel
                    return
                self._advance()
                self._set_rate(1.0)
                seq = self._live_seq()
                if seq is None or not self._open_at(seq):
                    self._retry = _Retry.LIVE
        finally:
            # Always, so StayLive's edge-triggered reopen re-arms; but a pause that was kept
            # stays time-shifted: only the Go live button (it unpaused) returns to LIVE.
            if self._paused:
                self._stay.user_paused()
            else:
                self._stay.user_go_live()

    def set_speed(self, rate: float) -> None:
        if self._phase is not _Phase.PIPELINE or rate <= 0 or self._stay.mode is Mode.LIVE:
            return  # at the edge, stay-live owns the rate
        self._advance()  # the time so far counts at the old rate
        self._set_rate(rate)
        self._stay.user_speed(rate)

    def set_latency(self, name: str) -> None:
        if name not in TARGETS:
            return
        self._latency = name
        self._stay.set_latency(name)

    def buffered_range(self) -> tuple[float, float]:
        """The buffered timeline seconds, from the oldest point to the live edge."""
        if self._phase is not _Phase.PIPELINE:
            return (0.0, 0.0)
        snapshot = self._snapshot()
        return (self._buffer_start(snapshot), self._live_edge(snapshot))

    def position(self) -> float:
        if self._phase is _Phase.PIPELINE:
            return self._advance()
        return self._base + self._elapsed

    # ---- sessions ----------------------------------------------------------------------
    def _begin(self) -> None:
        """Open the pipeline session for the channel `start()` stored."""
        if self._dead:
            self._fall_back(UNAVAILABLE)
            return
        self._sid = self._engine.open_session(
            self._url, **self._open_args, live_delay=LIVE_DELAY[self._latency]
        )
        self._phase = _Phase.WAITING
        self._ready_timer.start()

    def _close(self, sid: str) -> None:
        self._engine.close_session(sid)
        if not self._dead:  # without a helper no upstream is left to wait for
            self._unclosed.add(sid)
            self._close_timer.start(_CLOSE_CAP_MS)

    def _all_closed(self) -> None:
        """No session this object opened is open: run what waited for that."""
        if self._release_owed:
            self._release_owed = False
            self.released.emit()
        if self._unclosed:
            return  # a released() handler started and stopped another channel
        if self._phase is _Phase.FALLING_BACK:
            self._play_direct()
        elif self._phase is _Phase.STARTING:
            self._begin()

    def _fall_back(self, message: str) -> None:
        """Close the session, then play directly once its upstream is closed (spec §9)."""
        self._stop_timers()
        sid, self._sid = self._sid, None
        self._phase = _Phase.FALLING_BACK
        self._fallback_notice = message
        self._paused = self._source_lost = False
        self._deferred = None
        self._after_update = self._retry = self._failing_since = None
        if sid is not None:
            self._close(sid)
        if not self._unclosed:
            self._play_direct()

    def _play_direct(self) -> None:
        self._phase = _Phase.DIRECT
        self._mode = "direct"
        self._playback.play(
            self._url, kind=PlayKind.LIVE, latency=self._latency, user_agent=self._user_agent
        )
        self._set_rate(1.0)
        self.notice.emit(self._fallback_notice)
        self.modeChanged.emit(self._mode)

    def _recover(self) -> None:
        self._phase = _Phase.RECOVERING
        self._tick_timer.stop()
        self._after_update = self._retry = self._failing_since = None
        self._deferred = None
        self._snapshot_cache = {}
        self._edge_seen = self._smoothed = None  # the reopened session starts afresh
        self._recovery_timer.start(_RECOVERY_MS)

    def _stop_timers(self) -> None:
        self._ready_timer.stop()
        self._tick_timer.stop()
        self._recovery_timer.stop()

    # ---- engine ------------------------------------------------------------------------
    def _on_available(self, available: bool) -> None:
        if available:
            return  # a recovering session goes on with its own sessionReady
        self._dead = True
        waited = bool(self._unclosed)
        self._unclosed.clear()  # no helper: no upstream is left
        self._close_timer.stop()
        phase = self._phase
        if phase is _Phase.WAITING:
            self._fall_back(UNAVAILABLE)
        elif phase in (_Phase.RECOVERING, _Phase.PIPELINE):
            self._fall_back(LOST)  # an ended buffer went with the helper too
        elif waited:
            self._all_closed()

    def _on_ready(self, sid: str) -> None:
        if sid != self._sid:
            return
        if self._phase is _Phase.WAITING:
            seq = self._oldest_seq()
            if seq is None:
                return  # not playable yet: the timeout still runs
            self._ready_timer.stop()
            self._phase = _Phase.PIPELINE
            self._mode = "pipeline"
            self._tick_timer.start()
            if not self._open_at(seq):
                self._retry = _Retry.RESUME
            self.modeChanged.emit(self._mode)
        elif self._phase is _Phase.RECOVERING:
            self._recovery_timer.stop()
            self._phase = _Phase.PIPELINE
            self._tick_timer.start()
            self._go_live(unpause=False)

    def _on_updated(self, sid: str, snapshot: dict) -> None:
        if sid != self._sid:
            return
        self._since_edge_moved(snapshot)  # when a burst lands, to the update
        if self._deferred is not None and self._phase is _Phase.PIPELINE:
            self._deferred = None  # the spans now describe the RAM ring
            seek, self._after_update = self._after_update, None
            if seek is None:
                self._go_live(unpause=False)
            else:
                self.seek_to(seek)

    def _on_ready_timeout(self) -> None:
        if self._phase is _Phase.WAITING:
            self._fall_back(UNAVAILABLE)

    def _on_recovery_timeout(self) -> None:
        if self._phase is _Phase.RECOVERING:
            self._fall_back(LOST)

    def _on_failed(self, sid: str, kind: str, _message: str) -> None:
        if sid != self._sid or self._ended:
            return
        if kind == "disk":  # not final: the helper went on in RAM
            self.notice.emit(DISK)
            if self._phase is _Phase.PIPELINE:
                # The history went with the disk ring; the spans still describe it.
                self._deferred = self._clock()
                self._after_update = None
            return
        if kind == "internal":
            # A helper death: the engine reopens the session on available(True), or
            # reports available(False). Waiting is bounded by the ready timeout before
            # ready, and by the recovery cap after (also when the restarted helper dies).
            if self._phase is _Phase.PIPELINE:
                self._recover()
            return
        # Every other kind is final: the session's input is over.
        if self._phase is _Phase.WAITING:
            self._fall_back(UNAVAILABLE)
        elif (
            self._phase is _Phase.PIPELINE
            and self._deferred is None  # else the history went with the disk ring
            and self._behind_live()
        ):
            self._ended = self._source_lost = True  # the buffer still plays
            self.notice.emit(BUFFERED)
            self._emit_timeline()
        else:  # at the live edge, or recovering: nothing buffered is worth keeping
            self._fall_back(LOST)

    def _on_ended(self, sid: str) -> None:
        if sid == self._sid:
            self._ended = True

    def _on_closed(self, sid: str) -> None:
        if sid not in self._unclosed:
            return
        self._unclosed.discard(sid)
        if not self._unclosed:
            self._close_timer.stop()
            self._all_closed()

    def _on_close_cap(self) -> None:
        if not self._unclosed:
            return
        _LOG.warning("The live buffer did not confirm a close in time; going on without it.")
        self._unclosed.clear()
        self._all_closed()

    # ---- playback ----------------------------------------------------------------------
    def _on_state(self, state: str) -> None:
        if self._phase is not _Phase.PIPELINE:
            return
        if state in (_PLAYING, _PAUSED):
            self._failing_since = None
        if state == _PLAYING and self._paused:
            self._playback.set_paused(True)  # a reopened stream keeps the pause

    def _on_error(self, message: str) -> None:
        if self._phase is _Phase.DIRECT:
            self.failed.emit(message)
        else:
            self._on_interrupted()

    def _on_interrupted(self) -> None:
        if self._phase is not _Phase.PIPELINE:
            return
        if self._opening:
            self._retry = _Retry.RESUME  # failed inside play(): handled on the next tick
            return
        self._resume()
        if self._phase is _Phase.PIPELINE and self._ended:
            self._emit_timeline()  # it ran to the end of the buffer

    def _resume(self) -> None:
        """Get playing again after an interruption, or fall back once nothing more can."""
        self._retry = None
        if self._source_lost:
            self._fall_back(LOST)  # the buffer ran out: play the channel live
        elif self._ended or self._deferred is not None:
            return  # the end of the buffer, or the RAM ring's next state reopens (bounded)
        elif not self._gave_up():
            self._reopen_at_position()

    # ---- ticks -------------------------------------------------------------------------
    def _tick(self) -> None:
        if self._phase is not _Phase.PIPELINE:
            return
        if self._retry is not None:
            if self._retry is _Retry.RESUME:
                self._resume()
            elif not self._gave_up():
                self._go_live(unpause=False)
            if self._phase is not _Phase.PIPELINE:
                return  # it fell back
            if self._retry is not None:
                self._emit_timeline()
                return
        if self._deferred is not None:  # waiting for the RAM ring's first state
            # An HLS source may send it only with its next burst. Counted from the disk
            # failure: neither a pause nor an interruption moves the bound.
            burst = _number(self._snapshot().get("burst_s"))
            if self._clock() - self._deferred > max(self._ready_timeout_s, burst + 2.0):
                _LOG.warning(
                    "The live buffer sent nothing after its disk failed; playing directly."
                )
                self._fall_back(LOST)
            else:
                self._emit_timeline()
            return
        snapshot = self._snapshot()
        position = self._advance()
        lag = max(0.0, self._smooth(snapshot, position, self._lag(snapshot, position)))
        # An ended source has no live edge left to stay near, and a paused player stays
        # where the pause leaves it (StayLive is SHIFTED then).
        if not self._ended and not self._paused:
            self._stay.target = self._target(snapshot)
            decision = self._stay.update(lag, self._playback.state() is PlaybackState.BUFFERING)
            self._set_rate(decision.rate)
            if decision.reopen_live:
                self._go_live(unpause=False)
                if self._phase is not _Phase.PIPELINE:
                    return
        self._emit_timeline()

    def _lag(self, snapshot: Mapping[str, Any], position: float) -> float:
        """Seconds behind the live edge; for a bursty source, the mean over a burst.

        Between bursts the edge stands still while the position moves on, so the lag is a
        sawtooth falling from its peak when a burst lands. Its mean over a burst is the
        current lag plus the time since the edge last moved, less half a burst: known from
        the first sample, with no window to fill after a (re)open.
        """
        lag = self._live_edge(snapshot) - position
        burst = _number(snapshot.get("burst_s"))
        if burst > 0:
            lag += self._since_edge_moved(snapshot) - burst / 2
        return lag

    def _smooth(self, snapshot: Mapping[str, Any], position: float, lag: float) -> float:
        """For a bursty source, `lag` with the steps a burst brings eased over a few bursts.

        Between bursts the mean lag changes by exactly the wall time less the play time
        (pausing, catching up), and that passes unchanged. A landing burst adds a step: the
        interval since the last one less the media it brought, which is how late or early it
        lands or is seen (a state message or a tick). Such steps are noise around the
        long-term mean, so they are taken in over `_EASE_BURSTS` bursts: otherwise arrival
        jitter would nudge the rate again across StayLive's 1.0/1.05 band (0.2-0.75 s, spec
        values) after every catch-up.
        """
        burst = _number(snapshot.get("burst_s"))
        if burst <= 0:
            return lag
        now, correction = self._clock(), 0.0
        if self._smoothed is not None:
            last, correction, when, last_position = self._smoothed
            elapsed = now - when
            step = (lag - last) - (elapsed - (position - last_position))
            correction = (correction - step) * math.exp(-elapsed / (burst * _EASE_BURSTS))
        self._smoothed = (lag, correction, now, position)
        return lag + correction

    def _since_edge_moved(self, snapshot: Mapping[str, Any]) -> float:
        edge, now = self._live_edge(snapshot), self._clock()
        if edge != self._edge_seen:
            self._edge_seen, self._edge_moved = edge, now
        return now - self._edge_moved

    def _reported_lag(self, snapshot: Mapping[str, Any], position: float) -> float:
        """The lag StayLive saw at the last tick; the estimate before any tick."""
        if self._smoothed is not None and _number(snapshot.get("burst_s")) > 0:
            last, correction, _, _ = self._smoothed
            return max(0.0, last + correction)
        return max(0.0, self._lag(snapshot, position))

    def _emit_timeline(self) -> None:
        snapshot = self._snapshot()
        edge = self._live_edge(snapshot)
        position = min(self._base + self._elapsed, edge)
        self.timelineChanged.emit(
            {
                "start": self._buffer_start(snapshot),
                "end": _number(snapshot.get("end")),
                "position": position,
                "live_edge": edge,
                "lag": self._reported_lag(snapshot, position),
                "mode": self._stay.mode.value,
                "rate": self._rate,
                "paused": self._paused,
                "ended": self._ended,
                "target": self._target(snapshot),
            }
        )

    # ---- helpers -----------------------------------------------------------------------
    def _snapshot(self) -> dict[str, Any]:
        """The session's latest state; the last known one once the engine dropped it."""
        if self._sid is not None:
            snapshot = self._engine.snapshot(self._sid)
            if snapshot:
                self._snapshot_cache = snapshot
        return self._snapshot_cache

    def _target(self, snapshot: Mapping[str, Any]) -> float:
        return max(TARGETS[self._latency], _number(snapshot.get("burst_s")))

    @staticmethod
    def _live_edge(snapshot: Mapping[str, Any]) -> float:
        return _number(snapshot.get("live_edge", snapshot.get("end")))

    def _buffer_start(self, snapshot: Optional[Mapping[str, Any]] = None) -> float:
        return _number((self._snapshot() if snapshot is None else snapshot).get("start"))

    def _behind_live(self) -> bool:
        """Time-shifted by the user and further back than the target."""
        if self._stay.mode is Mode.LIVE:
            return False
        snapshot = self._snapshot()
        return self._reported_lag(snapshot, self._advance()) > self._target(snapshot)

    def _oldest_seq(self) -> Optional[int]:
        """The oldest buffered segment, which may be the open one."""
        assert self._sid is not None
        oldest = self._snapshot().get("oldest")
        if isinstance(oldest, int):
            return oldest
        spans = self._engine.spans(self._sid)
        return spans[0].seq if spans else None

    def _start_of(self, seq: int) -> float:
        assert self._sid is not None
        for span in self._engine.spans(self._sid):
            if span.seq == seq:
                return span.start
        # The open segment starts where the closed ones end (0.0 for the very first).
        return _number(self._snapshot().get("end"))

    def _live_seq(self) -> Optional[int]:
        assert self._sid is not None
        spans = self._engine.spans(self._sid)
        if not spans:
            return self._oldest_seq()
        snapshot = self._snapshot()
        edge, target = self._live_edge(snapshot), self._target(snapshot)
        burst = _number(snapshot.get("burst_s"))
        if burst <= 0:
            latest = edge - target
            return ([span for span in spans if span.start <= latest] or spans)[-1].seq
        # Every start, the open segment's too (where the closed ones end; the newest GOP of
        # a burst stays open until the next one), by the mean lag it would give (`_lag`).
        starts = [(span.start, span.seq) for span in spans]
        open_seq = snapshot.get("open")
        if isinstance(open_seq, int):
            starts.append((_number(snapshot.get("end")), open_seq))
        shift = self._since_edge_moved(snapshot) - burst / 2
        means = sorted((edge - start + shift, seq) for start, seq in starts)
        # Stay-live never slows down, so a landing below the target would keep its smaller
        # buffer for good: none below a quarter second short of it. Then the nearest the
        # target (ties: the newer start) within stay-live's band, else the nearest above.
        safe = [mean for mean in means if mean[0] >= target - _BELOW_TARGET_S]
        if not safe:
            return means[-1][1]  # a short buffer: all of it
        band = [mean for mean in safe if mean[0] <= target + _NUDGE_S]
        if band:
            return min(band, key=lambda mean: (abs(mean[0] - target), mean[0]))[1]
        return safe[0][1]

    def _advance(self) -> float:
        """Add the play time since the last sample to the position (spec §6.3)."""
        now, ms = self._clock(), self._playback.time_ms()
        playing = self._playback.state() is PlaybackState.PLAYING
        wall = (now - self._last_wall) * self._rate if playing else 0.0
        step = (ms - self._last_ms) / 1000.0
        if abs(step - wall) > _JUMP_S:
            step = wall
        self._elapsed += max(0.0, step)
        self._last_wall, self._last_ms = now, ms
        return self._base + self._elapsed

    def _set_rate(self, rate: float) -> None:
        if rate != self._rate:
            self._rate = rate
            self._playback.set_rate(rate)

    def _open_at(self, seq: int) -> bool:
        """Play the loopback stream from segment `seq`; False while no helper serves it."""
        assert self._sid is not None
        url = self._engine.stream_url(self._sid, seq)
        if not url:
            return False
        self._base, self._elapsed = self._start_of(seq), 0.0
        self._last_ms, self._last_wall = 0, self._clock()
        self._smoothed = None  # the old average says nothing about the new position
        self._retry = None
        self._opening = True
        try:
            self._playback.play(
                url,
                kind=PlayKind.LIVE_PIPELINE,
                latency=self._latency,
                user_agent=self._user_agent,
            )
            if self._rate != 1.0:
                self._playback.set_rate(self._rate)  # for the new media too
        finally:
            self._opening = False
        return True

    def _reopen_at_position(self) -> None:
        """Reopen where playback was, or at the oldest buffered point when that is gone."""
        assert self._sid is not None
        position = self._advance()
        start = self._buffer_start()
        span = _locate(self._engine.spans(self._sid), max(position, start))
        seq = span.seq if span is not None else self._oldest_seq()
        if seq is None or not self._open_at(seq):
            self._retry = _Retry.RESUME
            return
        if position < start - _EXPIRED_S:
            self.notice.emit(REWOUND)

    def _gave_up(self) -> bool:
        """After `ready_timeout_ms` of interruptions without VLC playing, fall back."""
        now = self._clock()
        if self._failing_since is None:
            self._failing_since = now
            return False
        if now - self._failing_since <= self._ready_timeout_s:
            return False
        _LOG.warning("The time-shift stream kept failing; playing the channel directly.")
        self._fall_back(LOST)
        return True
