"""Play one live channel through the time-shift pipeline, or directly when it cannot.

`LiveSession` lives on the GUI thread. `start()` opens a `LiveEngine` session and waits for
its `sessionReady`; then VLC plays the loopback stream from the oldest buffered segment with
the pipeline profile. Unsupported sources, failures before ready, an unavailable engine and
a ready that does not come within `ready_timeout_ms` play the provider URL directly instead
(`PlayKind.LIVE`), with the notice "Time-shift isn't available for this channel".

Position (spec §6.3): the start of the opened segment plus VLC's `get_time()` deltas; a
delta further than 1 s from wall clock × rate (counted only while VLC plays) is replaced by
wall clock × rate, so a timestamp jump in the stream does not move the position. Seeking
reopens the stream at a segment (about 2 s granularity) and keeps the pause intent: a
reopened stream is paused again as soon as it plays (port map §8.19).

Every `tick_ms` the lag behind the live edge feeds `StayLive`, whose target is the latency
setting's or the HLS burst (`burst_s`), whichever is larger: segments that arrive whole
would make a smaller target underrun at every segment. With bursts the lag is a sawtooth
as tall as a burst, so the lag used (and reported) is its mean over the last `burst_s`
seconds: held against the instantaneous peak, the rate would change at every burst and
settle only once the lag before each burst is under 0.75 s.

Interruptions while playing from the pipeline:
- VLC ended or failed: reopen at the position, or at the oldest buffered point with "Rewound
  past the buffer" when the ring evicted it (after a long pause, say). Resuming from a pause
  checks for that eviction first. Nothing is reopened once the session ended: playback runs
  to the end of the buffer. Reopens that never reach playing for `ready_timeout_ms` play
  directly.
- `sessionFailed("disk")`: the helper went on in RAM without the history: notice, go live.
- `sessionFailed("internal")` from a helper death (the engine is unavailable): wait for
  `available(True)` and the engine's reopen of the session, then go live; a reopen that is
  not ready within `ready_timeout_ms` of `available(True)` plays directly. An "internal"
  failure while the engine is up is the helper's own and no reopen follows: play directly.
- Any other `sessionFailed` (the source gave up), or `available(False)`: play directly with
  "Lost the time-shift buffer; playing live". `available(False)` keeps every later start
  direct for the rest of the run.

Engine signals for any session but the current one are ignored, and so are failures after
`sessionEnded` (only `sessionClosed` follows those). `released()` is emitted when the
upstream is closed: on `sessionClosed` of a stopped session, or right after
`playback.stop()` in direct mode. `failed(message)` reports a playback error in direct
mode; pipeline playback errors are recovered here and never reported.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.live.stay_live import TARGETS, StayLive
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
DISK = "Disk time-shift isn't available; using memory"
REWOUND = "Rewound past the buffer"
_JUMP_S = 1.0  # get_time deltas further than this from wall clock x rate are not trusted
_EXPIRED_S = 0.5  # a position this far before the oldest buffered point is gone
_PLAYING = PlaybackState.PLAYING.value
_PAUSED = PlaybackState.PAUSED.value


class _Phase(Enum):
    IDLE = "idle"
    WAITING = "waiting"  # the pipeline session is opening; nothing plays yet
    PIPELINE = "pipeline"  # VLC plays the loopback stream
    RECOVERING = "recovering"  # the helper died: waiting for the engine's reopen
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
        self._phase = _Phase.IDLE
        self._mode = ""
        self._dead = False  # available(False): direct play for the rest of the run
        self._sid: Optional[str] = None
        self._owed: set[str] = set()  # stopped sessions whose sessionClosed emits released()
        self._url = ""
        self._latency = "normal"
        self._user_agent: Optional[str] = None
        self._stay = StayLive()
        self._snapshot_cache: dict[str, Any] = {}
        self._ended = False
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
        self._lags: deque[tuple[float, float]] = deque()  # (wall, lag) over the last burst
        engine.available.connect(self._on_available)
        engine.sessionReady.connect(self._on_ready)
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
        self._stay = StayLive(self._latency)
        self._snapshot_cache = {}
        self._ended = self._paused = False
        self._retry = self._failing_since = None
        self._lags.clear()
        self._set_rate(1.0)  # every channel starts at normal speed
        if self._dead or not self._engine.is_available():
            self._go_direct(UNAVAILABLE)
            return
        self._sid = self._engine.open_session(
            url,
            headers=headers,
            verify_ssl=verify_ssl,
            disk=disk,
            max_bytes=max_bytes,
            live_delay=LIVE_DELAY[self._latency],
        )
        self._phase = _Phase.WAITING
        self._ready_timer.start()

    def stop(self) -> None:
        """Stop playback and close the session; `released()` follows (see the docstring)."""
        if self._phase is _Phase.IDLE:
            return
        self._ready_timer.stop()
        self._tick_timer.stop()
        self._phase = _Phase.IDLE
        self._mode = ""
        sid, self._sid = self._sid, None
        self._playback.stop()
        if sid is None:  # direct: the fallback already closed any session
            self.released.emit()
            return
        self._owed.add(sid)
        self._engine.close_session(sid)

    def toggle_pause(self) -> None:
        if self._phase is _Phase.DIRECT:
            self._playback.toggle_pause()
            return
        if self._phase is not _Phase.PIPELINE:
            return
        if not self._paused:
            self._paused = True
            self._stay.user_paused()
            self._playback.set_paused(True)
            return
        self._paused = False
        position = self._advance()
        if position < self._buffer_start() - _EXPIRED_S:
            self._reopen_at_position()  # the ring evicted it during the pause
        else:
            self._playback.set_paused(False)

    def seek_to(self, t: float) -> None:
        if self._phase is not _Phase.PIPELINE or self._sid is None:
            return
        span = _locate(self._engine.spans(self._sid), t)
        if span is None:  # nothing buffered yet
            return
        snapshot = self._snapshot()
        if t < self._live_edge(snapshot) - self._target(snapshot):
            self._stay.user_seeked_back()
        self._open_at(span.seq)

    def seek_relative(self, dt: float) -> None:
        self.seek_to(self.position() + dt)

    def go_live(self) -> None:
        """Reopen at the newest segment at least the target behind the live edge, at 1x."""
        try:
            if self._phase is _Phase.PIPELINE:
                self._advance()
                self._set_rate(1.0)
                self._paused = False
                seq = self._live_seq()
                if seq is None or not self._open_at(seq):
                    self._retry = _Retry.LIVE
        finally:
            # Always: StayLive's reopen signal stays disarmed until this call.
            self._stay.user_go_live()

    def set_speed(self, rate: float) -> None:
        if self._phase is not _Phase.PIPELINE or rate <= 0:
            return
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

    # ---- engine ------------------------------------------------------------------------
    def _on_available(self, available: bool) -> None:
        if available:
            if self._phase is _Phase.RECOVERING:
                self._ready_timer.start()  # the engine just reopened the session
            return
        self._dead = True
        if self._phase is _Phase.WAITING:
            self._go_direct(UNAVAILABLE)
        elif self._phase is _Phase.RECOVERING or (
            self._phase is _Phase.PIPELINE and not self._ended
        ):
            self._go_direct(LOST)

    def _on_ready(self, sid: str) -> None:
        if sid != self._sid:
            return
        if self._phase is _Phase.WAITING:
            seq = self._oldest_seq()
            if seq is None:
                return  # not playable yet: the timeout still runs
            self._ready_timer.stop()
            self._phase = _Phase.PIPELINE
            self._tick_timer.start()
            if not self._open_at(seq):
                self._retry = _Retry.RESUME
            self._mode = "pipeline"
            self.modeChanged.emit(self._mode)
        elif self._phase is _Phase.RECOVERING:
            self._ready_timer.stop()
            self._phase = _Phase.PIPELINE
            self._tick_timer.start()
            self.go_live()

    def _on_ready_timeout(self) -> None:
        if self._phase is _Phase.WAITING:
            self._go_direct(UNAVAILABLE)
        elif self._phase is _Phase.RECOVERING:
            self._go_direct(LOST)

    def _on_failed(self, sid: str, kind: str, _message: str) -> None:
        if sid != self._sid or self._ended:
            return
        if kind == "disk":  # not final: the helper went on in RAM
            self.notice.emit(DISK)
            if self._phase is _Phase.PIPELINE:
                self.go_live()  # the history went with the disk ring
            return
        if self._phase is _Phase.WAITING:
            self._go_direct(UNAVAILABLE)
        elif kind != "internal":
            self._go_direct(LOST)  # the source gave up
        elif self._phase is _Phase.PIPELINE:
            if self._engine.is_available():
                self._go_direct(LOST)  # the helper's own failure: no reopen follows
            else:
                self._recover()
        # "internal" while recovering: a second death; available(False) follows.

    def _on_ended(self, sid: str) -> None:
        if sid == self._sid:
            self._ended = True

    def _on_closed(self, sid: str) -> None:
        if sid in self._owed:
            self._owed.discard(sid)
            self.released.emit()

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
        if self._ended:
            self._emit_timeline()  # it ran to the end of the buffer
            return
        if self._gave_up():
            return
        if self._opening:
            self._retry = _Retry.RESUME  # failed inside play(): retry on the next tick
            return
        self._reopen_at_position()

    # ---- ticks -------------------------------------------------------------------------
    def _tick(self) -> None:
        if self._phase is not _Phase.PIPELINE:
            return
        if self._retry is not None:
            if self._gave_up():
                return
            if self._retry is _Retry.LIVE:
                self.go_live()
            else:
                self._reopen_at_position()
            if self._phase is not _Phase.PIPELINE:
                return
            if self._retry is not None:
                self._emit_timeline()
                return
        snapshot = self._snapshot()
        position = self._advance()
        lag = self._mean_lag(self._live_edge(snapshot) - position, snapshot)
        reported: Optional[float] = lag
        if not self._ended:  # an ended source has no live edge left to stay near
            self._stay.target = self._target(snapshot)
            decision = self._stay.update(lag, self._playback.state() is PlaybackState.BUFFERING)
            self._set_rate(decision.rate)
            if decision.reopen_live:
                self.go_live()
                reported = None  # the reopen moved the position
        self._emit_timeline(reported)

    def _mean_lag(self, lag: float, snapshot: Mapping[str, Any]) -> float:
        """`lag`, or its mean over the last burst when the source delivers in bursts."""
        lag = max(0.0, lag)
        burst = _number(snapshot.get("burst_s"))
        if burst <= 0:
            self._lags.clear()
            return lag
        now = self._clock()
        self._lags.append((now, lag))
        while now - self._lags[0][0] >= burst:
            self._lags.popleft()
        return sum(sample for _, sample in self._lags) / len(self._lags)

    def _emit_timeline(self, lag: Optional[float] = None) -> None:
        """Report the timeline; `lag` is this tick's (mean) lag, else the current one."""
        snapshot = self._snapshot()
        edge = self._live_edge(snapshot)
        position = min(self._base + self._elapsed, edge)
        self.timelineChanged.emit(
            {
                "start": self._buffer_start(snapshot),
                "end": _number(snapshot.get("end")),
                "position": position,
                "live_edge": edge,
                "lag": max(0.0, edge - position) if lag is None else lag,
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
        latest = self._live_edge(snapshot) - self._target(snapshot)
        behind = [span for span in spans if span.start <= latest]
        return (behind or spans)[-1].seq

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
        self._lags.clear()  # lags from before the jump say nothing about the new position
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
        """After `ready_timeout_ms` of interruptions without VLC playing, play directly."""
        now = self._clock()
        if self._failing_since is None:
            self._failing_since = now
            return False
        if now - self._failing_since <= self._ready_timeout_s:
            return False
        _LOG.warning("The time-shift stream kept failing; playing the channel directly.")
        self._go_direct(LOST)
        return True

    def _recover(self) -> None:
        self._phase = _Phase.RECOVERING
        self._tick_timer.stop()
        self._retry = self._failing_since = None
        self._snapshot_cache = {}

    def _go_direct(self, message: str) -> None:
        self._ready_timer.stop()
        self._tick_timer.stop()
        sid, self._sid = self._sid, None
        if sid is not None:
            self._engine.close_session(sid)  # no release is owed: the run goes on
        self._phase = _Phase.DIRECT
        self._paused = False
        self._retry = self._failing_since = None
        self._playback.play(
            self._url, kind=PlayKind.LIVE, latency=self._latency, user_agent=self._user_agent
        )
        self._set_rate(1.0)
        self._mode = "direct"
        self.notice.emit(message)
        self.modeChanged.emit(self._mode)
