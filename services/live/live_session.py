"""Play one live channel through the time-shift pipeline, or directly when it cannot.

`LiveSession` lives on the GUI thread. `start()` opens a `LiveEngine` session (also before
the helper's first hello: the engine queues it) and waits for its `sessionReady`; then VLC
plays the loopback stream from the oldest buffered segment with the pipeline profile, so a
zap starts at once. Stable (the cushion for jittery providers) waits until the buffer holds
its target and starts where Go live would; if that takes longer than `ready_timeout_ms`
from the start, it plays from the oldest segment then.

Falling back (spec §9) never overlaps two upstream connections: the session is closed and
the provider URL plays directly (`PlayKind.LIVE`) once the engine confirms the close, or
after `_CLOSE_CAP_MS`. Only `available(False)` plays directly at once: no helper, so no
upstream and no buffer, is left (also for a session whose input had ended). Unsupported
sources, failures before ready and a ready that does not come within `ready_timeout_ms`
fall back with "Time-shift isn't available for this channel"; after `available(False)`
every start plays directly for the rest of the run. A source that sent bytes by then without
anything playable (no keyframe yet: an on-demand restream starting up, say) gets until
`ready_max_ms` from the start, with "Still loading…" at `ready_timeout_ms`; one that sent
nothing falls back at `ready_timeout_ms`.

Position (spec §6.3): the start of the opened segment plus VLC's `get_time()` deltas; a
delta further than 1 s from wall clock × rate (counted only while VLC plays) is replaced by
wall clock × rate, so a timestamp jump in the stream does not move the position. Seeking
reopens the stream at a segment (about 2 s granularity) and keeps the pause intent: a
reopened stream is paused again as soon as it plays (port map §8.19). A seek to within the
target of the live edge goes live instead, still keeping the pause; only `go_live()` (the
Go live button) unpauses. A pause, from any mode (a catch-up included), sets 1x and makes
the session time-shifted (StayLive SHIFTED), whatever reopens it later, and stay-live
leaves it alone: no rate changes, no reopen at live. After the pause it plays on from
there at 1x, without catching up, until Go live; only a speed the user chooses while
paused applies on resume (CATCHING_UP). A pause, resume or Go live pressed before the
stream plays (a zap waiting for the old close, before ready, while recovering) is
remembered: the stream that opens next follows it.

Every `tick_ms` the lag behind the live edge feeds `StayLive`, whose target is the latency
setting's or the source's burst (`burst_s`), whichever is larger: segments that arrive whole
would make a smaller target underrun at every segment. The burst is an HLS source's target
duration, or what the helper measured on a raw TS source that arrives in bursts (some
providers' restreams send ~10 s at once every ~10 s); 0.0 for a steady source, and for a
raw TS one until its first clear burst was followed by the next (from the second burst on;
`services.live.worker.BurstMeter`). With bursts the lag is a sawtooth as tall as a burst,
so the lag used (and reported) is its mean, in closed form: the current
lag plus the time since the edge last moved, less half a burst (see `_lag`); the step a
landing burst adds is eased in over a few bursts, since it holds the jitter of when a
burst lands (see `_smooth`). Held against the instantaneous peak, the rate would change at
every burst and settle only once the lag before each burst is under 0.75 s. A source that
sends nothing for a burst plus 2 s (a steady one: 2 s) has stalled: the timeline says so
(`stalled`; the transport reads "Reconnecting…"), the time since the edge moved counts up
to there only, and StayLive takes it as buffering, as it does a VLC whose clock stood still
for 0.5 s while it reports Playing (libVLC 3 does when starved): no rate change, no reopen,
and the easing starts afresh once the edge moves again. Stay-live slows down only to build a
bursty source's room: at 0.95x while the mean lag is under a burst, which is under half a
burst left when a burst lands (the player would meet the edge just before each one), until
it is a burst (`StayLive`; not while paused, time-shifted or at a chosen speed). On some
channels VLC cannot play at any rate but 1x: its position crawls. While stay-live's rate is
not 1x, VLC's progress is checked against rate x wall time (`RateHealth`), counted only while
VLC plays (from a second after it opened) with more than `_AHEAD_S` of media ahead, so a
starved VLC is not taken for a slow one. Under 70% of it over 3 s, the rate goes back to 1x at
once and stay-live changes it no more on that channel (`StayLive.lock_rate`).

"Auto" latency (2.0.2, `AutoTarget`) learns the target per channel instead of keeping the
setting's: it starts at Normal's and rises 1.5 s with each starvation of VLC at the live
edge (BUFFERING, or its clock standing still while Playing, within `_AHEAD_S` of the edge)
and each stall, while stay-live is LIVE; trouble again within `_CALM_S` is the same one. Each
5 minutes of playing at the edge without trouble (not paused, not time-shifted) lower it
0.5 s. The target used is the larger of it and the burst, and a raised one is built at 0.95x
(`StayLive.build`) unless the rate is locked; otherwise the lag grows only as VLC waits. What a
channel taught (Auto's target, a locked rate) is kept per channel (`start(channel=...)`, in the
`LiveTuning` given): its next tune starts there, a locked rate under any latency. Go live
picks among every segment start, the open one included, by the lag it would give once VLC
plays it (VLC's open time, play() to Playing, is a moving average seeded at 0.5 s; for a
bursty source the mean; see `_live_seq`): never below the target less a quarter second,
since stay-live wins buffer back only that slowly, and only for a bursty source; the nearest
the target within stay-live's band; else the nearest above it ("behind live" for a moment
while stay-live catches up).

Interruptions while playing from the pipeline:
- VLC ended or failed: reopen at the position, or 5 s past the oldest buffered point with
  "Rewound past the buffer" (once until VLC plays again) when the ring evicted it (after a
  long pause, say). Resuming from a pause checks for that eviction first. Nothing is reopened
  once the session ended: playback runs to the end of the buffer. Interruptions that do not reach playing again within
  `ready_timeout_ms` fall back.
- `sessionFailed("disk")`: the helper went on in RAM without the history: a notice, then go
  live on the RAM ring's first state (`sessionUpdated`), not on the old ring's spans. A
  seek, Go live or resume meanwhile waits for that state too. Fall back when it does not
  come within `ready_timeout_ms` or `burst_s` + 2 s, whichever is longer.
- `sessionFailed("internal")` means a helper death: wait for the engine's reopen of the
  session, then go live (a viewer who was time-shifted or paused hears "Lost the time-shift
  buffer; playing live" at once; a pause is kept); after `_RECOVERY_MS` without it, fall
  back. (Before ready, the
  ready timeout bounds the same wait.)
- Any other kind is final (the source gave up, the helper's session "crashed", or a kind
  this code does not know): at the live edge, fall back with "Lost the time-shift buffer;
  playing live". Time-shifted behind the target, keep playing the buffer ("Lost the
  connection; playing what's buffered", timeline `ended`), and fall back when it runs out
  or on Go live. Before ready, fall back with "Time-shift isn't available…".

A source that the provider refused for its connection limit (`STREAM_LIMIT`, spec §6.9), or
with a plain 403 that may mean the same (`STREAM_REFUSED`), is not played directly, which
would be refused too: once the buffer (if time-shifted) has played, the session closes,
`modeChanged("")` follows if it played, and `failed(<that message>)` reports it.

Engine signals for any session but the current one are ignored, and so are failures after
`sessionEnded` (only `sessionClosed` follows those). `released()` follows `stop()` once no
session this object opened is still open (or after `_CLOSE_CAP_MS`) and, after direct play,
once libVLC let go of the provider's stream (`inputEnded`, or after `_CLOSE_CAP_MS`). A
`start()` during that wait opens its session only after `released()`. After
`engine_shut_down()` nothing the engine held counts as open, and starts play directly until
`use_engine()` hands over a restarted engine.
`failed(message)` reports a playback error in direct mode, or the provider's refusal for its
connection limit; pipeline playback errors are recovered here and never reported.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from enum import Enum
import logging
import math
import time
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.live.refusals import REFUSALS, STREAM_LIMIT
from services.live.stay_live import (
    AUTO,
    LATENCIES,
    TARGETS,
    AutoTarget,
    Mode,
    RateHealth,
    StayLive,
)
from services.live.timeline import Span
from services.live.tuning import LiveTuning, Tuning
from services.playback import PlaybackState, Session
from services.vlc_profile import PlayKind

if TYPE_CHECKING:
    from services.live.engine import LiveEngine

_LOG = logging.getLogger(__name__)

# How far behind the provider's live point an HLS source starts, per latency (Phase 2A).
LIVE_DELAY = {"low": 3.0, "normal": 6.0, "stable": 12.0, AUTO: 6.0}
UNAVAILABLE = "Time-shift isn't available for this channel"
LOST = "Lost the time-shift buffer; playing live"
BUFFERED = "Lost the connection; playing what's buffered"
DISK = "Disk time-shift isn't available; using memory"
REWOUND = "Rewound past the buffer"
LOADING = "Still loading…"
_CLOSE_CAP_MS = 2000  # a close not confirmed by then counts as done
_RECOVERY_MS = 10_000  # a helper restart must bring the session back by then
_JUMP_S = 1.0  # get_time deltas further than this from wall clock x rate are not trusted
_EXPIRED_S = 0.5  # a position this far before the oldest buffered point is gone
_REWIND_MARGIN_S = 5.0  # an evicted position reopens this far past the oldest point
_EASE_BURSTS = 4  # how many bursts the step of a landing burst is eased in over
_BELOW_TARGET_S = 0.25  # Go live never lands a mean lag further below the target
_NUDGE_S = 0.75  # StayLive nudges the rate beyond the target plus this
_STALL_S = 2.0  # an edge this much later than a burst (steady: at all) is a stalled source
_FROZEN_S = 0.5  # a VLC clock standing still this long while Playing is starved
_OPEN_SEED_S = 0.5  # VLC's open time (play() to Playing) before any was measured
_OPEN_MAX_S = 2.0  # a longer open (a stalled source, say) counts as this long
_OPEN_WEIGHT = 0.3  # of each new measurement in the open time's moving average
_AHEAD_S = 1.0  # rate health counts only time with more media than this ahead of VLC
_RATE_GRACE_S = 1.0  # and only from this long after an opened stream plays (VLC's clock starts)
_CALM_S = 1.0  # Auto: trouble again within this is the same starvation (VLC refills, starves)
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
    REFUSED = "refused"  # the provider refused for its connection limit: nothing plays


# Phases with a stream still to open: a pause, resume or Go live is remembered for it.
_BEFORE_PLAY = (_Phase.STARTING, _Phase.WAITING, _Phase.RECOVERING)


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
    modeChanged = Signal(str)  # "pipeline" or "direct"; "" once a refusal stopped it
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
        ready_max_ms: int = 15_000,
        clock: Callable[[], float] = time.monotonic,
        tuning: Optional[LiveTuning] = None,
    ) -> None:
        """`tuning`: what channels taught, shared by the players (None: nothing is kept)."""
        super().__init__()
        self._engine = engine
        self._playback = playback
        self._clock = clock
        self._tuning = tuning
        self._channel = ""  # the current channel's key in `tuning` ("": none)
        self._memory = Tuning()  # what it taught so far
        self._ready_timeout_ms = ready_timeout_ms
        self._ready_timeout_s = ready_timeout_ms / 1000.0
        self._ready_more_ms = max(0, ready_max_ms - ready_timeout_ms)
        self._ready_timer = QTimer(self, singleShot=True)
        self._ready_timer.timeout.connect(self._on_ready_timeout)
        self._loading = False  # bytes flowed but nothing was playable: the wait was extended
        self._tick_timer = QTimer(self, interval=tick_ms)
        self._tick_timer.timeout.connect(self._tick)
        self._recovery_timer = QTimer(self, singleShot=True)
        self._recovery_timer.timeout.connect(self._on_recovery_timeout)
        self._close_timer = QTimer(self, singleShot=True)
        self._close_timer.timeout.connect(self._on_close_cap)
        self._input_timer = QTimer(self, singleShot=True)
        self._input_timer.timeout.connect(self._on_input_cap)
        self._input_open = False  # stopped direct play: libVLC may still hold the stream
        self._limited: Optional[str] = None  # the provider's refusal (one of REFUSALS)
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
        self._clock_moved = 0.0  # when VLC's clock last moved (or was not expected to)
        self._open_s = _OPEN_SEED_S  # VLC's open time, averaged over this object's opens
        self._opened_at: Optional[float] = None  # a pipeline play() not yet Playing
        self._ready_seen = False  # the session is ready: Stable may still wait for its cushion
        self._rewound = False  # "Rewound past the buffer" was said; VLC has not played since
        # Bursty sources: (estimate, correction, when, position) at the last tick.
        self._smoothed: Optional[tuple[float, float, float, float]] = None
        # Rate health: VLC's progress at stay-live's rates other than 1x (see _check_rate).
        self._health = RateHealth()
        self._rate_mark: Optional[tuple[float, float]] = None  # (wall, position) last tick
        self._rate_from: Optional[float] = None  # counted from here (after an open plays)
        # Auto latency: the channel's target, the last tick it heard of, the last trouble.
        self._auto: Optional[AutoTarget] = None
        self._auto_mark: Optional[float] = None
        self._troubled_at: Optional[float] = None
        self._wire_engine(engine, connect=True)
        playback.stateChanged.connect(self._on_state)
        playback.ended.connect(self._on_interrupted)
        playback.errorOccurred.connect(self._on_error)
        playback.inputEnded.connect(self._on_input_ended)

    def _wire_engine(self, engine: LiveEngine, *, connect: bool) -> None:
        for signal, slot in (
            (engine.available, self._on_available),
            (engine.sessionReady, self._on_ready),
            (engine.sessionUpdated, self._on_updated),
            (engine.sessionFailed, self._on_failed),
            (engine.sessionEnded, self._on_ended),
            (engine.sessionClosed, self._on_closed),
        ):
            if connect:
                signal.connect(slot)
            else:
                signal.disconnect(slot)

    # ---- public ------------------------------------------------------------------------
    @property
    def mode(self) -> str:
        """The current channel's mode once it plays, "pipeline" or "direct"; else ""."""
        return self._mode

    def is_pipeline(self) -> bool:
        return self._mode == "pipeline"

    def engine_shut_down(self) -> None:
        """The engine was shut down, which no signal tells: count it gone for this run.

        Nothing it held is open any more, so a stop's wait for its close ends now (without
        the close-cap warning); later starts play directly until `use_engine()`.
        """
        self._on_available(False)

    def use_engine(self, engine: LiveEngine) -> None:
        """Drive `engine` (a restart after a shutdown) from the next start; before its start()."""
        if engine is not self._engine:
            self._wire_engine(self._engine, connect=False)
            self._engine = engine
            self._wire_engine(engine, connect=True)
        self._dead = False

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
        ram_bytes: Optional[int] = None,
        channel: str = "",
    ) -> None:
        """`ram_bytes`: the memory a disk buffer that fails goes on in (the helper's default
        when None). `channel`: the channel's key in the tuning store ("": nothing kept)."""
        self.stop()
        self._url, self._user_agent = url, user_agent
        self._channel = channel
        memory = self._tuning.get(channel) if self._tuning is not None else None
        self._memory = memory or Tuning()
        self._latency = latency if latency in LATENCIES else "normal"
        self._auto = AutoTarget(self._memory.target) if self._latency == AUTO else None
        self._auto_mark = self._troubled_at = None
        self._open_args = {
            "headers": dict(headers),
            "verify_ssl": verify_ssl,
            "disk": disk,
            "max_bytes": max_bytes,
        }
        if ram_bytes is not None:
            self._open_args["ram_bytes"] = ram_bytes
        self._stay = StayLive(self._latency)
        if self._memory.rate_locked:
            self._stay.lock_rate()  # VLC could not play it at another rate before
        self._health = RateHealth()
        self._snapshot_cache = {}
        self._ended = self._source_lost = self._paused = False
        self._limited = None
        self._deferred = None
        self._after_update = self._retry = self._failing_since = None
        self._edge_seen = self._smoothed = None
        self._rewound = False
        self._base = self._elapsed = 0.0
        self._last_ms, self._last_wall = 0, self._clock()
        self._set_rate(1.0)  # every channel starts at normal speed
        if self._unclosed or self._input_open:
            self._phase = _Phase.STARTING  # after the earlier upstream closed (released)
            return
        self._begin()

    def stop(self) -> None:
        """Stop playback and close the session; `released()` follows (see the docstring)."""
        if self._phase is _Phase.IDLE:
            return
        direct = self._phase is _Phase.DIRECT
        self._stop_timers()
        self._phase = _Phase.IDLE
        self._mode = ""
        sid, self._sid = self._sid, None
        self._playback.stop()
        if sid is not None:
            self._close(sid)
        if direct and self._playback.holds_input():
            # libVLC ends its input on a worker: the provider connection may outlive stop().
            self._input_open = True
            self._input_timer.start(_CLOSE_CAP_MS)
        self._release_owed = True
        if not self._unclosed:
            self._all_closed()

    def toggle_pause(self) -> None:
        if self._phase is _Phase.DIRECT:
            self._playback.toggle_pause()
            return
        playing = self._phase is _Phase.PIPELINE
        if not playing and self._phase not in _BEFORE_PLAY:
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
        if self._phase in (_Phase.STARTING, _Phase.WAITING):
            self._stay.user_go_live()  # nothing played yet, so nothing was time-shifted
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
            if unpause and self._phase in _BEFORE_PLAY:
                self._paused = False  # remembered: the stream that opens next plays, live
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
        if name not in LATENCIES:
            return
        self._latency = name
        self._stay.set_latency(name)
        if name != AUTO:
            self._auto = None
        elif self._auto is None:
            self._auto = AutoTarget(self._memory.target)  # where the channel left it

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
        self._ready_seen = self._loading = False
        self._ready_timer.start(self._ready_timeout_ms)

    def _close(self, sid: str) -> None:
        self._engine.close_session(sid)
        if not self._dead:  # without a helper no upstream is left to wait for
            self._unclosed.add(sid)
            self._close_timer.start(_CLOSE_CAP_MS)

    def _all_closed(self) -> None:
        """No session this object opened is open: run what waited for that."""
        if self._input_open:
            return  # libVLC still ends the provider stream it played directly
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
        if self._limited:
            self._refuse()
            return
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

    def _refuse(self) -> None:
        """The provider refused for its connection limit: close it all, play nothing."""
        self._stop_timers()
        sid, self._sid = self._sid, None
        self._phase = _Phase.REFUSED
        played, self._mode = self._mode, ""
        self._paused = self._source_lost = False
        self._deferred = None
        self._after_update = self._retry = self._failing_since = None
        self._playback.stop()
        if sid is not None:
            self._close(sid)
        if played:
            self.modeChanged.emit(self._mode)
        self.failed.emit(self._limited or STREAM_LIMIT)

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
            self._ready_seen = True
            self._play_first()
        elif self._phase is _Phase.RECOVERING:
            self._recovery_timer.stop()
            self._phase = _Phase.PIPELINE
            self._tick_timer.start()
            self._go_live(unpause=False)

    def _on_updated(self, sid: str, snapshot: dict) -> None:
        if sid != self._sid:
            return
        self._since_edge_moved(snapshot)  # when a burst lands, to the update
        if self._phase is _Phase.WAITING and self._ready_seen:
            self._play_first()  # Stable's cushion may be buffered now
            return
        if self._deferred is not None and self._phase is _Phase.PIPELINE:
            self._deferred = None  # the spans now describe the RAM ring
            seek, self._after_update = self._after_update, None
            if seek is None:
                self._go_live(unpause=False)
            else:
                self.seek_to(seek)

    def _on_ready_timeout(self) -> None:
        if self._phase is not _Phase.WAITING:
            return
        if self._ready_seen and self._oldest_seq() is not None:
            self._play_first(cushion=False)  # Stable's cushion did not fill in time
        elif not self._ready_seen and not self._loading and self._receiving():
            # Bytes flow but nothing is playable yet (an on-demand restream starting up,
            # say): wait up to `ready_max_ms` in all before playing directly.
            self._loading = True
            self._ready_timer.start(self._ready_more_ms)
            self.notice.emit(LOADING)
        else:
            self._fall_back(UNAVAILABLE)

    def _receiving(self) -> bool:
        """The source sent bytes (the helper's state before ready says how many)."""
        return self._ready_more_ms > 0 and _number(self._snapshot().get("received")) > 0

    def _play_first(self, *, cushion: bool = True) -> None:
        """The tune's first play: from the oldest segment, so zapping is quick; for Stable,
        once the buffer holds its target, from the start Go live would pick (`cushion`).
        """
        if cushion and self._latency == "stable":
            seq = self._live_seq(cushion=True)
        else:
            seq = self._oldest_seq()
        if seq is None:
            return  # not playable yet, or no cushion yet: the ready timeout still runs
        self._ready_timer.stop()
        self._phase = _Phase.PIPELINE
        self._mode = "pipeline"
        self._tick_timer.start()
        if not self._open_at(seq):
            self._retry = _Retry.RESUME
        self.modeChanged.emit(self._mode)

    def _on_recovery_timeout(self) -> None:
        if self._phase is _Phase.RECOVERING:
            self._fall_back(LOST)

    def _on_failed(self, sid: str, kind: str, message: str) -> None:
        if sid != self._sid or self._ended:
            return
        if message in REFUSALS:
            self._limited = message  # final like any "http" failure, but nothing plays directly
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
                shifted = self._paused or self._stay.mode is not Mode.LIVE
                self._recover()
                if shifted:  # the time-shifted position went with the helper's buffer
                    self.notice.emit(LOST)
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

    def _on_input_ended(self) -> None:
        if self._input_open:
            self._input_open = False
            self._input_timer.stop()
            if not self._unclosed:
                self._all_closed()

    def _on_input_cap(self) -> None:
        if self._input_open:
            _LOG.warning("VLC did not let go of the channel in time; going on without it.")
            self._on_input_ended()

    # ---- playback ----------------------------------------------------------------------
    def _on_state(self, state: str) -> None:
        if self._phase is not _Phase.PIPELINE:
            return
        if state == _PLAYING and self._opened_at is not None:
            took = min(self._clock() - self._opened_at, _OPEN_MAX_S)
            self._open_s += _OPEN_WEIGHT * (took - self._open_s)
            self._opened_at = None
            self._rate_from = self._clock() + _RATE_GRACE_S
        if state == _PLAYING:
            self._rewound = False
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
        self._check_rate(snapshot, position)
        stalled = self._stalled(snapshot)
        lag = max(0.0, self._smooth(snapshot, position, self._lag(snapshot, position), stalled))
        # An ended source has no live edge left to stay near, and a paused player stays
        # where the pause leaves it (StayLive is SHIFTED then, or CATCHING_UP at a speed
        # the user chose while paused, which applies on resume). A stalled source and a
        # starved VLC (libVLC 3 still says Playing then) count as buffering: no rate
        # change and no reopen while nothing arrives.
        if not self._ended and not self._paused:
            self._back_off(snapshot, position, stalled)
            self._stay.target = self._target(snapshot)
            self._stay.burst = _number(snapshot.get("burst_s"))
            auto = self._auto
            self._stay.build = auto.target if auto is not None and auto.adjusted else 0.0
            buffering = (
                stalled or self._playback.state() is PlaybackState.BUFFERING or self._clock_frozen()
            )
            decision = self._stay.update(lag, buffering)
            self._set_rate(decision.rate)
            if decision.reopen_live:
                self._go_live(unpause=False)
                if self._phase is not _Phase.PIPELINE:
                    return
        else:
            self._auto_mark = None  # paused (or ended): no quiet time for Auto
        self._emit_timeline()

    def _back_off(self, snapshot: Mapping[str, Any], position: float, stalled: bool) -> None:
        """Auto latency: raise the channel's target on trouble, lower it while quiet.

        Counted only while stay-live is LIVE (the caller skips paused and ended sessions).
        """
        now = self._clock()
        mark, self._auto_mark = self._auto_mark, now
        auto = self._auto
        if auto is None or self._stay.mode is not Mode.LIVE:
            return
        if stalled or self._starved(snapshot, position):
            fresh = self._troubled_at is None or now - self._troubled_at > _CALM_S
            self._troubled_at = now
            if fresh and auto.trouble(_number(snapshot.get("burst_s"))):
                self._learn()
        elif mark is not None and auto.quiet(now - mark):
            self._learn()

    def _learn(self) -> None:
        """Keep what the channel taught (Auto's target, a locked rate) for its next tune."""
        auto = self._auto
        target = self._memory.target
        if auto is not None:
            target = auto.target if auto.adjusted else None
            _LOG.debug("Auto latency: %.1f s for this channel", auto.target)
        self._memory = Tuning(target, self._stay.rate_locked)
        if self._tuning is not None:
            self._tuning.remember(self._channel, self._memory)

    def _starved(self, snapshot: Mapping[str, Any], position: float) -> bool:
        """VLC met the live edge and waits there (libVLC 3 says Playing, its clock stands still)."""
        waiting = self._playback.state() is PlaybackState.BUFFERING or self._clock_frozen()
        return waiting and self._live_edge(snapshot) - position <= _AHEAD_S

    def _check_rate(self, snapshot: Mapping[str, Any], position: float) -> None:
        """Rate health: since the last tick, did VLC play at stay-live's rate?

        Counted only at a rate other than 1x that stay-live set (LIVE, not paused), while VLC
        plays, from `_RATE_GRACE_S` after an opened stream plays, with more than `_AHEAD_S` of
        media ahead. A crawl locks the rate at 1x at once, for the rest of the channel.
        """
        now = self._clock()
        mark, self._rate_mark = self._rate_mark, (now, position)
        if mark is None or self._stay.rate_locked or self._rate == 1.0:
            return
        counted = (
            self._stay.mode is Mode.LIVE
            and not self._paused
            and not self._ended
            and self._rate_from is not None
            and mark[0] >= self._rate_from
            and self._playback.state() is PlaybackState.PLAYING
            and self._live_edge(snapshot) - position > _AHEAD_S
        )
        if counted and self._health.add(now - mark[0], position - mark[1], self._rate):
            self._stay.lock_rate()
            self._set_rate(1.0)
            _LOG.info(
                "VLC played this channel at %.0f%% of the speed asked: staying at 1x on it",
                100 * self._health.share,
            )
            self._learn()

    def _lag(self, snapshot: Mapping[str, Any], position: float) -> float:
        """Seconds behind the live edge; for a bursty source, the mean over a burst.

        Between bursts the edge stands still while the position moves on, so the lag is a
        sawtooth falling from its peak when a burst lands. Its mean over a burst is the
        current lag plus the time since the edge last moved, less half a burst: known from
        the first sample, with no window to fill after a (re)open (see `_burst_shift`).
        """
        return self._live_edge(snapshot) - position + self._burst_shift(snapshot)

    def _burst_shift(self, snapshot: Mapping[str, Any]) -> float:
        """What a bursty source adds to the current lag for its mean: since the edge moved,
        less half a burst; 0 for a steady source. A burst later than `_STALL_S` is a stall,
        not the sawtooth: the time since the edge moved counts up to there only.
        """
        burst = _number(snapshot.get("burst_s"))
        if burst <= 0:
            return 0.0
        return min(self._since_edge_moved(snapshot), burst + _STALL_S) - burst / 2

    def _stalled(self, snapshot: Mapping[str, Any]) -> bool:
        """The source sent nothing for a burst plus `_STALL_S` (a steady one: `_STALL_S`)."""
        if self._ended:
            return False  # its input is over: nothing more is expected
        limit = _number(snapshot.get("burst_s")) + _STALL_S
        return self._since_edge_moved(snapshot) > limit

    def _clock_frozen(self) -> bool:
        """VLC reports Playing but its clock stood still for `_FROZEN_S` (it is starved)."""
        playing = self._playback.state() is PlaybackState.PLAYING
        return playing and self._clock() - self._clock_moved >= _FROZEN_S

    def _smooth(
        self, snapshot: Mapping[str, Any], position: float, lag: float, stalled: bool = False
    ) -> float:
        """For a bursty source, `lag` with the steps a burst brings eased over a few bursts.

        Between bursts the mean lag changes by exactly the wall time less the play time
        (pausing, catching up), and that passes unchanged. A landing burst adds a step: the
        interval since the last one less the media it brought, which is how late or early it
        lands or is seen (a state message or a tick). Such steps are noise around the
        long-term mean, so they are taken in over `_EASE_BURSTS` bursts: otherwise arrival
        jitter would nudge the rate again across StayLive's 1.0/1.05 band (0.2-0.75 s, spec
        values) after every catch-up. A stall is no such noise: while it lasts nothing is
        eased, and easing starts afresh once the edge moves again; so it does when a steady
        source turns bursty (again).
        """
        burst = _number(snapshot.get("burst_s"))
        if burst <= 0 or stalled:
            # A steady source (a raw TS one may turn steady and later bursty again) eases
            # nothing; a stall is no such noise either. Either way the easing starts afresh.
            self._smoothed = None
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
        """The timeline's state. Under Auto latency `auto` is True and `adjusted` says the
        target is not the one Auto starts with (it learned another, or the burst is larger)."""
        snapshot = self._snapshot()
        edge = self._live_edge(snapshot)
        position = min(self._base + self._elapsed, edge)
        target = self._target(snapshot)
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
                "target": target,
                "stalled": self._stalled(snapshot),
                "auto": self._auto is not None,
                "adjusted": self._auto is not None and target != self._auto.start,
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
        """The latency's target (Auto: the channel's), or the burst when that is larger."""
        latency = self._auto.target if self._auto is not None else TARGETS[self._latency]
        return max(latency, _number(snapshot.get("burst_s")))

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

    def _live_seq(self, *, cushion: bool = False) -> Optional[int]:
        """The start Go live picks; with `cushion`, None while none is far enough behind."""
        assert self._sid is not None
        spans = self._engine.spans(self._sid)
        if not spans:
            return None if cushion else self._oldest_seq()
        snapshot = self._snapshot()
        edge, target = self._live_edge(snapshot), self._target(snapshot)
        # Every start, the open segment's too (where the closed ones end; on a bursty source
        # the newest GOP stays open until the next burst), by the lag it would give once VLC
        # plays it (the edge moves on while VLC opens): for a bursty source the mean over a
        # burst (`_lag`), for a steady source the lag itself.
        starts = [(span.start, span.seq) for span in spans]
        open_seq = snapshot.get("open")
        if isinstance(open_seq, int):
            starts.append((_number(snapshot.get("end")), open_seq))
        shift = self._burst_shift(snapshot) + self._open_s
        means = sorted((edge - start + shift, seq) for start, seq in starts)
        # Stay-live slows down only to build a bursty source's room (0.95x), so a landing
        # below the target would keep its smaller buffer for good, or for a long while: none
        # below a quarter second short of it. Then the nearest the target (ties: the newer
        # start) within stay-live's band, else the nearest above.
        safe = [mean for mean in means if mean[0] >= target - _BELOW_TARGET_S]
        if not safe:
            return None if cushion else means[-1][1]  # a short buffer: all of it
        band = [mean for mean in safe if mean[0] <= target + _NUDGE_S]
        if band:
            return min(band, key=lambda mean: (abs(mean[0] - target), mean[0]))[1]
        return safe[0][1]

    def _advance(self) -> float:
        """Add the play time since the last sample to the position (spec §6.3)."""
        now, ms = self._clock(), self._playback.time_ms()
        playing = self._playback.state() is PlaybackState.PLAYING
        if ms != self._last_ms or not playing:
            self._clock_moved = now
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
        self._clock_moved = self._opened_at = self._last_wall
        self._smoothed = None  # the old average says nothing about the new position
        self._rate_mark = self._rate_from = None  # rate health counts once this one plays
        self._health.reset()
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
        """Reopen where playback was; when the ring evicted that, `_REWIND_MARGIN_S` past its
        oldest point (the oldest segment itself may be evicted before VLC asks for it), with
        "Rewound past the buffer" once until VLC plays again.
        """
        assert self._sid is not None
        position = self._advance()
        start = self._buffer_start()
        expired = position < start - _EXPIRED_S
        at = start + _REWIND_MARGIN_S if expired else max(position, start)
        span = _locate(self._engine.spans(self._sid), at)
        seq = span.seq if span is not None else self._oldest_seq()
        if seq is None or not self._open_at(seq):
            self._retry = _Retry.RESUME
            return
        if expired and not self._rewound:
            self._rewound = True
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
