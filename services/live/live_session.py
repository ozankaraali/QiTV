"""Play one live channel through the time-shift pipeline, or directly when it cannot.

`LiveSession` lives on the GUI thread. `start()` opens a `LiveEngine` session (also before
the helper's first hello: the engine queues it; an HLS source is told the target, Auto's
learned extra included, so it fetches far enough back for a start there) and waits for its
`sessionReady`; then VLC plays the loopback stream with the pipeline profile from the start
Go live would pick (2.1 stage 3; before, the oldest segment: one provider sends a 20 s
backlog at connect, and a sparse-PCR channel's one fetched segment ran VLC dry). The start
waits for the backlog the source sends at connect (`_play_first`): an HLS source says how
much listed media is still to come, so the starts are judged on the edge it will reach and
one is taken once nothing still to come can bring a nearer one (or every listed segment
came); any other is held until its live edge grows at about real time, at most 1.5 s after
ready. With nothing that far back, a steady source
starts at the oldest segment within a second; a bursty or HLS one (and Stable, the cushion
for jittery providers) waits for one, saying "Still loading…" after a second; if that takes
longer than `ready_timeout_ms` from the start, it plays from the oldest segment then.
Stay-live hears where each such start (and each Go live) landed (`StayLive.landed`): a
keyframe interval above the target is nudged gently, a shortfall built briefly.

Falling back (spec §9) never overlaps two upstream connections: the session is closed and
the provider URL plays directly (`PlayKind.LIVE`) once the engine confirms the close, or
after `_CLOSE_CAP_MS`. A direct play after a session was open that fails at once (within
`_DIRECT_QUICK_S` of its play call) is tried again up to three times, `retry_ms` apart (3, 5
and 10 s; a provider allowing one stream may still count the buffer's connection a while),
with "Reconnecting…" each time, and only then reported; a later failure (VLC timed out
opening it: the provider does not answer) and, once it played, any error are reported at
once. Only `available(False)` plays directly at once: no helper, so no upstream and no
buffer, is left (also for a session whose input had ended). Unsupported sources, failures
before ready and a ready that does not come within `ready_timeout_ms` fall back with
"Time-shift isn't available for this channel"; after `available(False)` every start plays
directly for the rest of the run. A source that
sent bytes by then without anything playable (no keyframe yet: an on-demand restream
starting up, say) gets until `ready_max_ms` from the start, with "Still loading…" at
`ready_timeout_ms`; one that sent nothing falls back at `ready_timeout_ms`.

Position (spec §6.3; 2.1 stage 2): the start of the opened segment plus VLC's clock as
`VlcClock` follows its `get_time()` readings: a clock at the rate set while VLC plays,
corrected towards each new reading, with the readings' steps learned (a sparse-PCR channel's
get_time steps by 2 s every 2 s; a steady one's every tick or so). A jump of VLC's clock is
taken once the next reading agrees with it, unless it lands past the live edge or matches a
timestamp jump the helper reports having left out of its timeline (`breaks` in its state: an
HLS discontinuity or a reconnect whose timestamps jumped 3 s or more; the position goes on
at the rate); it never runs backwards. Seeking
reopens the stream at a segment (about 2 s granularity) where playback starts cleanly (2.1
stage 5, `_seek_start`: a keyframe, else an intra picture, at or within 10 s before the point
asked, or within 2 s after it; a start without either only when none is that near; a step
back or on opens before or after where it plays) and keeps the pause intent: a
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
providers' restreams send ~10 s at once every ~10 s); 0.0 for a steady source, and for a raw
TS one until its first clear burst was followed by the next (from the second burst on;
`services.live.worker.BurstMeter`). With bursts the lag is a sawtooth as tall as a burst, so
the lag used (and reported) is its mean (2.1, `LagFilter`): per tick in closed form, the
current lag plus the time since the edge last moved, less half a burst (see `_lag`),
averaged over a dozen bursts of the live edge's own history, so arrivals that are late,
early or unlike (a public HLS channel measured in 2.1: 6 s segments every 7 s, every sixth
one 12 s) do not move it; the position (VLC's side) is never averaged. The burst stay-live
steers by has hysteresis (a burst the helper measures for a moment is none:
`LagFilter.burst`); the timeline's target, the stall bound and Go live follow the helper's
at once. Held against the instantaneous peak, the rate would change at every burst. A source
that sends nothing for a burst plus 2 s (a steady one: 2 s) has stalled: the timeline says
so (`stalled`; the transport reads "Reconnecting…"), the lag is the closed form with the
time since the edge moved counted up to there only, and StayLive takes it as buffering, as
it does a VLC whose clock stood still while it reports Playing (libVLC 3 does when starved)
clearly longer than it steps (1.5 learned intervals plus a quarter second, at least 0.5 s):
no rate change, no reopen, and the filter starts afresh once the edge moves again.
StayLive changes the rate only for a lag outside its dead band for 15 s, by one gentle nudge
(1.03x or 0.97x, ramped in three steps, sized to end at the target; one per 5 minutes),
sooner and beyond that budget only far behind (1.10x), with under half a second of room when
a burst lands (0.95x, 2.0.1's case) and once after trouble (a starvation or a stall that ran
out the cushion: 0.95x back to the target, once VLC played a second short of it); not
while paused, time-shifted or at a chosen speed. Far behind, a jump to live replaces
the 1.10x catch-up (2.1 stage 5) when the clean start Go live would pick (`_jump_lag`) lands
within stay-live's band or 5 s nearer and nothing jumped within a minute (a tune, Go live, a
reopen: see `StayLive`). Its reopen at live (such a jump, or beyond the target plus 20 s)
waits until a stream opened (Go live, a seek, a reopen) has played and `_REOPEN_HOLD_S`
passed, and a Go live right after stay-live's reopen (also one the tick made after it could
not open at once) opens nothing more: never two opens in a row. The timeline's `shown` is the lag as the
indicator shows it (`SettledLag`: half seconds, changed after 3 s), `nudge` the rate a nudge
ramps to. On some channels VLC cannot play at any rate but 1x: its position crawls. While
stay-live's rate is not 1x, VLC's progress is checked against rate x wall time
(`RateHealth`), counted only while VLC plays and at least `_MEASURE_AHEAD_S` of media is
ahead at both ends of a tick, and from `_RATE_GRACE_S` after an opened stream plays or after
VLC was at the live edge (within `_EDGE_S`, BUFFERING, or the source stalled): a starved
VLC, and its restart once data comes again (it refills its caching while its clock stands
still), are not taken for a slow one. The progress is VLC's own clock's, in its steps, never
the position (whose clock runs at the rate by design): a stretch runs from VLC's first step
once counting starts to a later step (a step only late or missing counts at its end), or
holds nothing played once VLC's clock stood still a whole window and two of its holds. Under
70% of it over 3 s (at least two such stretches), the rate goes back to 1x at once and
stay-live changes it no more on that channel (`StayLive.lock_rate`). On some HLS
channels VLC crawls through the pipeline even at 1x (measured, 2.0.2), while its own HLS
plays them smoothly: counted the same way at 1x, under 70% progress in `_CRAWL_WINDOWS`
windows of `_CRAWL_WINDOW_S` in a row makes the channel play directly ("Plays without rewind
for smoother playback", `CRAWLING`), and the channel's next tune plays directly at once (for
a day: see `services.live.tuning`). A raw TS channel never does.

"Auto" latency (2.0.2, `AutoTarget`) learns per channel what to add to the target in force
(Normal's, or the burst when that is larger): nothing at first, then 1.5 s more with each
starvation of VLC at the live edge (BUFFERING, or its clock standing still while Playing,
within `_EDGE_S` of the edge) and each stall, while stay-live is LIVE, so the target moves at
once, on long HLS segments too; trouble again within `_CALM_S` is the same one. Only
trouble that ran out a cushion counts: the lag had reached the current target (less
`_BELOW_TARGET_S`) since the tune, the tune started at a pick chosen for the target (ruling
R33: its lag may read a little short once the source's rhythm is learned), or stay-live
accepted the start's shortfall (nothing further back was buffered); a starvation after a
start that took less on purpose, while it is still being built, says nothing about the
target. Each
5 minutes of playing at the edge without trouble (not paused, not time-shifted) take 0.5 s
off. A raised target is built by a nudge below 1x (as above) unless the rate is locked;
otherwise the lag grows only as VLC waits. What a channel taught (Auto's extra, a locked
rate) is kept per channel (`start(channel=...)`, in the
`LiveTuning` given): its next tune starts there, a locked rate under any latency (for a day:
then the store reads it as none, so a tune tries other rates again). Go live
picks among every segment start, the open one included, by the lag it would give once VLC
plays it (VLC's open time, play() to Playing, is a moving average seeded at 0.5 s; for a
bursty source the mean; while playing, from the lag filter's mean edge, as stay-live then
measures it; see `_live_start`): never below the target less a quarter second,
since stay-live wins buffer back only that slowly, and only for a bursty source; the nearest
the target within stay-live's band; else the nearest above it ("behind live" for a moment
while stay-live catches up). Every such open (a tune, Go live, stay-live's reopen) starts
cleanly where it can (2.1 stage 5): a keyframe, else an intra picture, a start without either
(its picture breaks until the next keyframe) only when nothing else is far enough back, which
a debug log says on a stream with video; a keyframe over an intra picture within that band.

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
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.live.refusals import REFUSALS, STREAM_LIMIT
from services.live.stay_live import (
    AUTO,
    LATENCIES,
    STALL_S,
    TARGETS,
    AutoTarget,
    LagFilter,
    Mode,
    RateHealth,
    SettledLag,
    StayLive,
    start_target,
)
from services.live.timeline import Span
from services.live.tuning import LiveTuning, Tuning
from services.live.vlc_clock import ClockMark, VlcClock
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
CRAWLING = "Plays without rewind for smoother playback"
RECONNECTING = "Reconnecting…"
_DIRECT_RETRIES_MS = (3000, 5000, 10000)  # a fallback's direct play, failing at once ...
_DIRECT_QUICK_S = 5.0  # ... within this of its play call (a timeout is reported at once)
LOADING = "Still loading…"
_CLOSE_CAP_MS = 2000  # a close not confirmed by then counts as done
_RECOVERY_MS = 10_000  # a helper restart must bring the session back by then
_EXPIRED_S = 0.5  # a position this far before the oldest buffered point is gone
_REWIND_MARGIN_S = 5.0  # an evicted position reopens this far past the oldest point
_BELOW_TARGET_S = 0.25  # Go live never lands a mean lag further below the target
_LAND_ABOVE_S = 0.75  # Go live lands at most this far above the target (when a start is there)
_OPEN_SEED_S = 0.5  # VLC's open time (play() to Playing) before any was measured
_OPEN_MAX_S = 2.0  # a longer open (a stalled source, say) counts as this long
_OPEN_WEIGHT = 0.3  # of each new measurement in the open time's moving average
_EDGE_S = 1.0  # VLC this close to the live edge is at it (a starvation, and its restart after)
_MEASURE_AHEAD_S = 3.0  # rate health counts only ticks with this much ahead at both ends
_RATE_GRACE_S = 2.0  # and only from this long after an open plays or VLC was at the edge
_CALM_S = 1.0  # Auto: trouble again within this is the same starvation (VLC refills, starves)
_REOPEN_HOLD_S = 5.0  # no reopen at live within this of an open (and before VLC played it)
# The tune's start (2.1 stage 3, `_play_first`). A source other than HLS is held until the
# backlog it sends at connect landed: its live edge grew by at most _SETTLE_GROWTH_S over the
# last _SETTLE_S, or _SETTLE_MAX_S passed since ready.
_SETTLE_S = 1.0
_SETTLE_GROWTH_S = 2.0
_SETTLE_MAX_S = 1.5
# With nothing far enough back, a steady source starts at the oldest after this long.
_STEADY_WAIT_S = 1.0
# A start held longer than this says "Still loading…" (a bursty or HLS source, Stable).
_LOADING_AFTER_S = 1.0
# Within this long after a tune's start, a backlog step that raises the lag is part of the
# landing (raw TS whose backlog came after the hold).
_RELAND_S = 10.0
_CRAWL_WINDOW_S = 8.0  # an HLS channel crawling at 1x for this long (counted) ...
_CRAWL_WINDOWS = 2  # ... this many times in a row plays directly (one freeze is not enough)
_FROZEN_HOLDS = 2.0  # VLC's clock still for a window and this many holds has played nothing
_ARRIVAL_S = 0.1  # the live edge moving less is no arrival (a closing segment's last frame)
# Seeks (2.1 stage 5): a start without a keyframe gives way to a clean one this near the point
# asked (a forced cut's length) ...
_SEEK_REACH_S = 10.0
# ... and an intra picture's to a keyframe's at most this much further (a common keyframe
# interval).
_KEY_REACH_S = 2.0
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


class _Progress(NamedTuple):
    """A stretch of VLC's progress for rate health: from `start` (its counters; None until
    VLC's clock steps), at 1x or not, while VLC's clock had stepped `changes` times since the
    open."""

    at_1x: bool
    changes: int
    start: Optional[ClockMark]


class _Retry(Enum):
    LIVE = "live"  # a recovery's or the RAM ring's go live (a short landing is built)
    GO_LIVE = "go_live"  # the Go live button's, a seek to the edge (nothing is built)
    REOPEN = "reopen"  # stay-live's own reopen at live (a Go live right after it is merged)
    RESUME = "resume"
    FIRST = "first"  # the tune's first open: its start is picked again


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


# How cleanly playback starts at a segment (`Span.start_quality`), best first; a start whose
# quality is not known counts as a key.
_QUALITY_RANK = {"key": 0, "open": 1, "none": 2}


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
        retry_ms: Sequence[int] = _DIRECT_RETRIES_MS,
    ) -> None:
        """`tuning`: what channels taught, shared by the players (None: nothing is kept).
        `retry_ms`: after a fallback, the waits before the direct play is tried again when it
        fails at once (a provider may count the connection just closed a while longer)."""
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
        self._direct_waits = tuple(retry_ms)
        self._direct_timer = QTimer(self, singleShot=True)
        self._direct_timer.timeout.connect(self._retry_direct)
        self._direct_retries = 0  # tries a fallback's direct play has left (0: report errors)
        self._direct_at = -math.inf  # when the direct play() was last made
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
        # Position: _base (the opened segment's start) + _elapsed, VLC's clock (2.1 stage 2).
        self._base = 0.0
        self._elapsed = 0.0
        self._vlc = VlcClock()
        self._last_ms = 0  # VLC's last reading (0: the opened stream's clock has not moved)
        self._retry: Optional[_Retry] = None  # a reopen that could not be made yet
        self._failing_since: Optional[float] = None  # first interruption since VLC played
        self._opening = False  # inside playback.play(): its errors are retried on a tick
        self._edge_seen: Optional[float] = None  # the live edge last seen
        self._edge_moved = 0.0  # when it last moved (for bursts: when the last one landed)
        self._last_open = -math.inf  # when the last pipeline play() was made
        self._reopened_at = -math.inf  # when stay-live last reopened at live
        self._open_s = _OPEN_SEED_S  # VLC's open time, averaged over this object's opens
        self._opened_at: Optional[float] = None  # a pipeline play() not yet Playing
        self._ready_seen = False  # the session is ready: the start may still be held
        self._ready_at = 0.0  # when it was
        self._edges: list[tuple[float, float]] = []  # (when, live edge) while the start waits
        self._held_said = False  # "Still loading…" was said while the start waited
        self._reland_until = -math.inf  # a backlog step before this is part of the landing
        self._reland_edge = 0.0  # the live edge at the last tick, meanwhile
        self._rewound = False  # "Rewound past the buffer" was said; VLC has not played since
        # The mean lag over the source's arrival rhythm, and the lag as shown (2.1).
        self._filter = LagFilter()
        self._settled = SettledLag()
        self._was_stalled = False  # the source had stalled at the last tick
        # Rate health: VLC's progress at stay-live's rates other than 1x (see _check_rate).
        self._health = RateHealth()
        self._crawl = RateHealth(_CRAWL_WINDOW_S, windows=_CRAWL_WINDOWS)  # HLS at 1x
        self._rate_mark: Optional[tuple[float, float]] = None  # (wall, media ahead) last tick
        self._progress: Optional[_Progress] = None  # VLC's progress counted since
        self._rate_from: Optional[float] = None  # counted from here (after an open plays)
        # Auto latency: the channel's target, the last tick it heard of, the last trouble.
        self._auto: Optional[AutoTarget] = None
        self._auto_mark: Optional[float] = None
        self._troubled_at: Optional[float] = None
        self._cushion = 0.0  # the largest lag at the live edge since the tune (or the target)
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

    @property
    def no_rewind_reason(self) -> str:
        """What a rewind says while the channel plays directly: `CRAWLING` when VLC crawled
        through the time-shift buffer on it (now, or within the day), else `UNAVAILABLE`."""
        return CRAWLING if self._memory.direct else UNAVAILABLE

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
        self._auto = AutoTarget(self._memory.extra) if self._latency == AUTO else None
        self._auto_mark = self._troubled_at = None
        self._cushion = 0.0
        self._open_args = {
            "headers": dict(headers),
            "verify_ssl": verify_ssl,
            "disk": disk,
            "max_bytes": max_bytes,
        }
        if ram_bytes is not None:
            self._open_args["ram_bytes"] = ram_bytes
        self._stay = StayLive(self._latency)
        self._stay.jump_lag = self._jump_lag  # where a jump to live would land (stage 5)
        if self._memory.rate_locked:
            self._stay.lock_rate()  # VLC could not play it at another rate before
        self._health = RateHealth()
        self._crawl = RateHealth(_CRAWL_WINDOW_S, windows=_CRAWL_WINDOWS)
        self._snapshot_cache = {}
        self._ended = self._source_lost = self._paused = False
        self._limited = None
        self._deferred = None
        self._after_update = self._retry = self._failing_since = None
        self._edge_seen = None
        self._reopened_at = -math.inf
        self._filter = LagFilter()  # a new source: its burst too is known afresh
        self._settled.reset()
        self._was_stalled = False
        self._rewound = False
        self._base = self._elapsed = 0.0
        self._last_ms = 0
        self._vlc = VlcClock()  # a new channel: its clock's cadence is learned afresh
        self._vlc.restart(self._clock())
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
        self._seek(t)

    def _seek(self, t: float, *, lo: float = -math.inf, hi: float = math.inf) -> None:
        """Seek to `t`, opening a segment that starts in [`lo`, `hi`) (a step's direction)."""
        if self._phase is not _Phase.PIPELINE or self._sid is None:
            return
        snapshot = self._snapshot()
        if t >= self._live_edge(snapshot) - self._target(snapshot):
            # At the edge: not a segment back, and back to LIVE.
            self._go_live(unpause=False, merge=True)
            return
        if self._deferred is not None:
            self._after_update = t  # the spans still describe the lost disk ring
            return
        span = self._seek_start(t, lo=lo, hi=hi)
        if span is None:  # nothing buffered yet (or nothing that way)
            return
        self._stay.user_seeked_back()
        self._open_at(span.seq)

    def seek_relative(self, dt: float) -> None:
        """A step back or on (←/→): it opens a segment starting before where it plays, or
        after it (review I1: a keyframe after the point asked could take a step back to
        where it played, again and again)."""
        position = self.position()
        if dt < 0:
            self._seek(position + dt, hi=position)
        elif dt > 0:
            self._seek(position + dt, lo=position + 1e-6)

    def go_live(self) -> None:
        """The Go live button: back to the live edge at 1x, playing (not paused)."""
        self._go_live(unpause=True, merge=True)

    def _go_live(
        self,
        *,
        unpause: bool,
        reopen: bool = False,
        merge: bool = False,
        build: Optional[bool] = None,
    ) -> None:
        """Reopen at the live target behind the edge, at 1x; a paused one stays paused.
        `reopen`: stay-live's own, far behind. `merge` (the Go live button, a seek to the
        edge): within `_REOPEN_HOLD_S` of stay-live's reopen nothing more opens (it is at
        live already: one open, not two). A recovery or the RAM ring's reopen always opens.
        Stay-live hears where an open landed (`StayLive.landed`); `build` (default: neither
        `reopen` nor `merge`) whether a shortfall then is missing room, built once: a
        recovery's or the RAM ring's fresh buffer may hold little, while Go live and a jump
        pick at the target in a full one (review I3)."""
        build = not (reopen or merge) if build is None else build
        landing: Optional[float] = None
        merged = False
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
                if (
                    merge
                    and not self._paused
                    and self._stay.mode is Mode.LIVE
                    and self._retry is None
                    and self._clock() - self._reopened_at < _REOPEN_HOLD_S
                ):
                    merged = True
                    return  # stay-live reopened at live a moment ago
                pick = self._live_start()
                if pick is None or not self._open_at(pick[0]):
                    # The tick tries again, as the same kind of open.
                    retry = _Retry.LIVE if build else _Retry.GO_LIVE
                    self._retry = _Retry.REOPEN if reopen else retry
                else:
                    landing = pick[1] - self._target(self._snapshot())
                    if reopen:
                        self._reopened_at = self._clock()
        finally:
            # Always, so StayLive's edge-triggered reopen re-arms; but a pause that was kept
            # stays time-shifted: only the Go live button (it unpaused) returns to LIVE.
            if self._paused:
                self._stay.user_paused()
            else:
                self._stay.user_go_live(keep_landing=merged)  # nothing opened: it stays
                if landing is not None:
                    self._stay.landed(landing, build=build)

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
            self._auto = AutoTarget(self._memory.extra)  # where the channel left it

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
        """Open the pipeline session for the channel `start()` stored; play directly at once
        without the helper, or a channel that crawled through the pipeline (`CRAWLING`)."""
        if self._dead or self._memory.direct:
            self._fall_back(UNAVAILABLE)
            return
        auto = self._auto
        self._sid = self._engine.open_session(
            self._url,
            **self._open_args,
            live_delay=LIVE_DELAY[self._latency],
            # An HLS source starts far enough back for a start at the target (2.1 stage 3).
            target=start_target(self._latency),
            extra=auto.extra if auto is not None else 0.0,
        )
        self._phase = _Phase.WAITING
        self._ready_seen = self._loading = self._held_said = False
        self._edges = []
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
        # A buffer that was open may still count with the provider a while (one stream).
        self._direct_retries = len(self._direct_waits) if sid is not None else 0
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
        self._direct_at = self._clock()
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
        self._edge_seen = None  # the reopened session starts afresh
        self._filter = LagFilter()
        self._settled.reset()
        self._was_stalled = False
        self._recovery_timer.start(_RECOVERY_MS)

    def _stop_timers(self) -> None:
        self._ready_timer.stop()
        self._tick_timer.stop()
        self._recovery_timer.stop()
        self._direct_timer.stop()

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
            self._ready_at = self._clock()
            self._tick_timer.start()  # the held start is looked at again on each tick
            self._play_first()
        elif self._phase is _Phase.RECOVERING:
            self._recovery_timer.stop()
            self._phase = _Phase.PIPELINE
            self._tick_timer.start()
            self._go_live(unpause=False)
            if not self._paused:  # the new session's backlog may land after it: the landing
                self._reland_until = self._clock() + _RELAND_S
                self._reland_edge = self._live_edge(self._snapshot())

    def _on_updated(self, sid: str, snapshot: dict) -> None:
        if sid != self._sid:
            return
        self._since_edge_moved(snapshot)  # when a burst lands, to the update
        if self._phase is _Phase.WAITING:
            self._edges.append((self._clock(), self._live_edge(snapshot)))
            if self._ready_seen:
                self._play_first()  # the backlog may have landed, the cushion be there
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
            self._play_first(waited=True)  # held for a start that did not come in time
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

    def _play_first(self, *, waited: bool = False) -> None:
        """The tune's first play (2.1 stage 3), at the start Go live would pick: the nearest
        the target from above (a keyframe start before any other, `_live_start`), never the
        oldest backlog when a start nearer it exists.

        A finished playlist (ENDLIST), or a session whose input already ended, plays from its
        oldest segment at once: a clip from its beginning. An HLS source says how much listed
        media is still to come (`backlog_s`, its EXTINF scaled by the media measured): the
        starts are judged against the live edge it will reach (`_hls_ready`). Any other
        source is held, on each state and tick, until the backlog it sent at connect landed
        (`_landed`), at most `_SETTLE_MAX_S` after ready. With nothing far enough back, a
        steady source starts at the oldest after `_STEADY_WAIT_S`; a bursty or HLS source (or
        Stable, the cushion for jittery providers) waits for one. A start held longer than
        `_LOADING_AFTER_S` (but a raw TS backlog's) says "Still loading…". `waited`: the ready
        timeout ran out: the best start there is now. Stay-live hears where it landed
        (`StayLive.landed`).
        """
        snapshot = self._snapshot()
        if self._ended or snapshot.get("ended") is True or snapshot.get("finite") is True:
            oldest = self._oldest_seq()
            if oldest is not None:
                self._first_open(oldest, None)  # nothing live to land near
            return
        hls = snapshot.get("hls") is True
        ahead = self._ahead(snapshot)
        pick = self._live_start(cushion=True, ahead=ahead)
        if not waited:
            held = self._clock() - self._ready_at
            if pick is None:
                if self._waits_for_cushion(snapshot):
                    self._say_loading(held)
                    return  # the ready timeout bounds it
                if held < _STEADY_WAIT_S - 1e-9:
                    return
            elif hls and not self._hls_ready(snapshot, pick[1], ahead):
                self._say_loading(held)
                return  # bounded by the ready timeout
            elif not hls and not self._landed(snapshot) and held < _SETTLE_MAX_S - 1e-9:
                return
        aimed = pick is not None  # chosen for the target, not the most there is
        if pick is None:
            pick = self._live_start(ahead=ahead)  # the most buffer there is
        if pick is not None:
            self._first_open(*pick, aimed=aimed)

    def _first_open(self, seq: int, lag: Optional[float], *, aimed: bool = False) -> None:
        """Play the tune from `seq`, its lag once playing `lag` (None: a finished playlist's
        beginning, no landing); a failed open picks again on the tick (`_Retry.FIRST`)."""
        self._ready_timer.stop()
        self._phase = _Phase.PIPELINE
        self._mode = "pipeline"
        self._edges = []
        self._tick_timer.start()
        self._open_first(seq, lag, aimed=aimed)
        self.modeChanged.emit(self._mode)

    def _open_first(self, seq: int, lag: Optional[float], *, aimed: bool = False) -> None:
        """Open the tune at `seq`. `aimed`: the start was chosen for the target, so its
        cushion counts as reached (ruling R33): its lag may read a little short of it once
        the source's rhythm is learned, and VLC running dry then is trouble all the same.
        A start that took less on purpose (the oldest segment, nothing further back) has
        none."""
        if not self._open_at(seq):
            self._retry = _Retry.FIRST
            return
        if lag is None:
            return
        snapshot = self._snapshot()
        target = self._target(snapshot)
        self._stay.landed(lag - target)
        if aimed:
            self._cushion = max(self._cushion, target)
        self._reland_until = self._clock() + _RELAND_S
        self._reland_edge = self._live_edge(snapshot)

    def _open_first_again(self) -> None:
        """The tune's first open failed (no helper served it): pick its start again."""
        snapshot = self._snapshot()
        if self._ended or snapshot.get("ended") is True or snapshot.get("finite") is True:
            oldest = self._oldest_seq()
            if oldest is not None:
                self._open_first(oldest, None)
            return
        ahead = self._ahead(snapshot)
        pick = self._live_start(cushion=True, ahead=ahead)
        aimed = pick is not None
        pick = pick or self._live_start(ahead=ahead)
        if pick is not None:
            self._open_first(*pick, aimed=aimed)

    def _ahead(self, snapshot: Mapping[str, Any]) -> float:
        """The media an HLS tune's backlog still brings (0.0 once every listed segment came,
        and while that is not known)."""
        backlog = snapshot.get("backlog_s")
        if snapshot.get("caught_up") is True or not isinstance(backlog, (int, float)):
            return 0.0
        return max(0.0, float(backlog))

    def _say_loading(self, held: float) -> None:
        if held >= _LOADING_AFTER_S - 1e-9 and not self._held_said:
            self._held_said = True
            self.notice.emit(LOADING)

    def _waits_for_cushion(self, snapshot: Mapping[str, Any]) -> bool:
        """A tune with nothing far enough back waits for it: a bursty or HLS source (each
        arrival brings a burst's worth) and Stable; a steady one has it within a second."""
        bursty = snapshot.get("hls") is True or _number(snapshot.get("burst_s")) > 0
        return bursty or self._latency == "stable"

    def _hls_ready(self, snapshot: Mapping[str, Any], lag: float, ahead: float) -> bool:
        """An HLS tune's start (its lag `lag` on the edge `ahead` seconds further) can be
        taken: every listed segment came (`caught_up`); or, the media still to come known
        (`backlog_s`) and more than a segment of it, the start is already within stay-live's
        band, or the newest start is short of the target there, so nothing still to come can
        bring a nearer one. With a segment or less to come it waits for it: a wrong EXTINF or
        a missing segment cannot move the start then."""
        if snapshot.get("caught_up") is True:
            return True
        if not isinstance(snapshot.get("backlog_s"), (int, float)):
            return False  # not measured yet: all of it, then
        if ahead <= _number(snapshot.get("burst_s")) + 1e-9:
            return False
        target = self._target(snapshot)
        if target - _BELOW_TARGET_S <= lag <= target + _LAND_ABOVE_S:
            return True
        means = self._start_lags(snapshot, ahead)
        return bool(means) and means[0][0] < target - _BELOW_TARGET_S

    def _landed(self, snapshot: Mapping[str, Any]) -> bool:
        """A source other than HLS: the backlog it sent at connect has landed, its live edge
        grew by at most `_SETTLE_GROWTH_S` over the last `_SETTLE_S` (about real time; one
        provider's 20 s came as two keyframe intervals a second apart)."""
        edge, since = self._live_edge(snapshot), self._clock() - _SETTLE_S
        past = self._edges[0][1] if self._edges else edge
        for at, seen in self._edges:
            if at > since:
                break
            past = seen
        return edge - past <= _SETTLE_GROWTH_S + 1e-9

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
        if self._phase is _Phase.DIRECT and state == _PLAYING:
            self._direct_retries = 0  # it played: a later error is reported at once
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
            quick = self._clock() - self._direct_at <= _DIRECT_QUICK_S
            if self._direct_retries > 0 and quick:
                # Right after a fallback the provider may still count the connection the
                # buffer closed (one allowed stream): tried again after a while. A provider
                # that does not answer at all times out, and is reported at once.
                wait = self._direct_waits[len(self._direct_waits) - self._direct_retries]
                self._direct_retries -= 1
                _LOG.info("The channel did not open directly; trying again in %d ms.", wait)
                self.notice.emit(RECONNECTING)
                self._direct_timer.start(wait)
                return
            self._direct_retries = 0
            self.failed.emit(message)
        else:
            self._on_interrupted()

    def _retry_direct(self) -> None:
        if self._phase is _Phase.DIRECT:
            self._direct_at = self._clock()
            self._playback.play(
                self._url, kind=PlayKind.LIVE, latency=self._latency, user_agent=self._user_agent
            )

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
        if self._phase is _Phase.WAITING and self._ready_seen:
            self._play_first()  # a held start: the backlog may have landed by now
            return
        if self._phase is not _Phase.PIPELINE:
            return
        if self._retry is not None:
            if self._retry is _Retry.RESUME:
                self._resume()
            elif self._retry is _Retry.FIRST:
                if not self._gave_up():
                    self._open_first_again()
            elif not self._gave_up():
                retry = self._retry
                self._go_live(
                    unpause=False, reopen=retry is _Retry.REOPEN, build=retry is _Retry.LIVE
                )
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
        if self._phase is not _Phase.PIPELINE:
            return  # it crawled at 1x: it plays directly now
        stalled = self._stalled(snapshot)
        self._observe(snapshot, stalled)
        lag = max(0.0, self._mean_lag(snapshot, position))
        self._settled.update(self._clock(), lag)
        # An ended source, and a finished playlist (a clip, while it still downloads), have
        # no live edge to stay near: no catch-up, no reopen, and 1x if stay-live had set
        # another rate. A paused player stays where the pause leaves it (StayLive is SHIFTED
        # then, or CATCHING_UP at a speed the user chose while paused, which applies on
        # resume). A stalled source and a starved VLC (libVLC 3 still says Playing then)
        # count as buffering: no rate change and no reopen while nothing arrives.
        live = not self._ended and snapshot.get("finite") is not True
        if live and not self._paused:
            self._back_off(snapshot, position, stalled, lag)
            self._stay.target = self._control_target()
            self._stay.slack = self._target(snapshot) - self._stay.target
            self._stay.hold_reopen = self._just_opened()
            self._stay.burst = self._filter.burst
            self._stay.spread = self._filter.spread()
            self._reland(snapshot, lag)
            buffering = (
                stalled or self._playback.state() is PlaybackState.BUFFERING or self._clock_frozen()
            )
            decision = self._stay.update(lag, buffering, now=self._clock())
            self._set_rate(decision.rate)
            if decision.reopen_live:
                _LOG.debug(
                    "Stay-live reopens at live %.1f s behind (target %.1f s)%s.",
                    lag,
                    self._stay.target,
                    ": a jump instead of a catch-up" if decision.jump else "",
                )
                self._go_live(unpause=False, reopen=True)
                if self._phase is not _Phase.PIPELINE:
                    return
        else:
            self._auto_mark = None  # paused (or ended): no quiet time for Auto
            if not live and not self._paused:
                self._no_live_edge(snapshot, position)
        self._emit_timeline()

    def _no_live_edge(self, snapshot: Mapping[str, Any], position: float) -> None:
        """The input ended, or the stream is a finished playlist (a clip): stay-live is not
        consulted. A catch-up it was making stops at 1x (`StayLive.stop`: no nudge left to
        show). A clip still downloading that plays at a speed the user chose comes back to
        1x within the target of its download edge, as a video player does near its
        download, and is live again: it would run dry on a slow one."""
        stay = self._stay
        if stay.mode is Mode.LIVE:
            if self._rate != 1.0 or stay.nudge != 1.0:
                stay.stop()
                self._set_rate(1.0)
            return
        downloading = snapshot.get("finite") is True and not self._ended
        near = self._live_edge(snapshot) - position <= self._target(snapshot)
        if downloading and self._rate > 1.0 and near:
            stay.user_go_live()
            stay.stop()
            self._set_rate(1.0)

    def _reland(self, snapshot: Mapping[str, Any], lag: float) -> None:
        """Within `_RELAND_S` of a tune's start, a backlog step (the live edge grew by more
        than `_SETTLE_GROWTH_S` in a tick) that took the lag further above the target is part
        of the landing (`StayLive.landed_further`): raw TS whose backlog came after the hold."""
        if self._clock() >= self._reland_until:
            return
        edge = self._live_edge(snapshot)
        stepped, self._reland_edge = edge - self._reland_edge > _SETTLE_GROWTH_S, edge
        if stepped:
            self._stay.landed_further(lag - self._stay.target)

    def _back_off(
        self, snapshot: Mapping[str, Any], position: float, stalled: bool, lag: float
    ) -> None:
        """Trouble: a starvation or a stall that ran out the cushion. Auto latency raises the
        channel's target on it and lowers it while quiet; under any latency stay-live owes
        a slow-down back to the target (`StayLive.trouble`: once VLC played a second short of
        it, beyond its budget).

        Counted only while stay-live is LIVE (the caller skips paused and ended sessions),
        and trouble only once the lag had reached the target since the tune (`_cushion`).
        """
        now = self._clock()
        mark, self._auto_mark = self._auto_mark, now
        if self._stay.mode is not Mode.LIVE:
            return
        self._cushion = max(self._cushion, lag)  # under any latency: Auto may follow
        auto = self._auto
        if stalled or self._starved(snapshot, position):
            fresh = self._troubled_at is None or now - self._troubled_at > _CALM_S
            self._troubled_at = now
            # A start whose shortfall stay-live accepted (nothing further back was buffered)
            # never had the cushion: VLC running dry then is trouble all the same.
            ran_out = (
                self._cushion >= self._target(snapshot) - _BELOW_TARGET_S
                or self._stay.shortfall_accepted
            )
            if fresh and ran_out:
                if auto is not None and auto.trouble(_number(snapshot.get("burst_s"))):
                    self._learn()
                self._stay.trouble()
        elif auto is not None and mark is not None and auto.quiet(now - mark):
            self._learn()

    def _learn(self, *, direct: bool = False) -> None:
        """Keep what the channel taught (Auto's target, a locked rate, playing directly) for
        its next tune."""
        auto = self._auto
        extra = self._memory.extra
        if auto is not None:
            extra = auto.extra if auto.adjusted else None
            _LOG.debug("Auto latency: %.1f s more for this channel", auto.extra)
        self._memory = Tuning(extra, self._stay.rate_locked, self._memory.direct or direct)
        if self._tuning is not None:
            self._tuning.remember(self._channel, self._memory)

    def _starved(self, snapshot: Mapping[str, Any], position: float) -> bool:
        """VLC met the live edge and waits there (libVLC 3 says Playing, its clock stands still)."""
        waiting = self._playback.state() is PlaybackState.BUFFERING or self._clock_frozen()
        return waiting and self._live_edge(snapshot) - position <= _EDGE_S

    def _check_rate(self, snapshot: Mapping[str, Any], position: float) -> None:
        """Rate health: did VLC play at the rate set? Judged on VLC's own clock, in its steps.

        Counted only while stay-live is LIVE (not paused), VLC plays, at least
        `_MEASURE_AHEAD_S` is ahead at both ends of each tick, and from `_RATE_GRACE_S` after
        an opened stream plays or VLC was at the live edge (its restart after running dry is
        no crawl). VLC's progress over a counted stretch is what its clock stepped (2.1 stage
        2: a sparse-PCR channel's reading steps by 2 s every 2 s, so a tick says nothing),
        against what the rates asked meanwhile. A stretch starts at VLC's first step once
        counting starts (never a whole step against part of its time) and ends at a later
        step; a step that is late or missing (twice as long) counts at its end, and only a
        clock that stood still a whole window and two holds counts as nothing played.
        `RateHealth` judges windows of at least two stretches, so they close at a step. At a
        rate other than 1x that stay-live set, a crawl locks the rate at 1x at once for the
        channel. At 1x, an HLS channel that still crawls plays directly (`CRAWLING`). The
        caller returns if it fell back.
        """
        now = self._clock()
        ahead = self._live_edge(snapshot) - position
        state = self._playback.state()
        at_edge = ahead <= _EDGE_S or state is PlaybackState.BUFFERING
        if self._rate_from is not None and (at_edge or self._stalled(snapshot)):
            self._rate_from = max(self._rate_from, now + _RATE_GRACE_S)
        mark, self._rate_mark = self._rate_mark, (now, ahead)
        counted = (
            mark is not None
            and self._stay.mode is Mode.LIVE
            and not self._paused
            and not self._ended
            and self._rate_from is not None
            and mark[0] >= self._rate_from
            and state is PlaybackState.PLAYING
            and min(mark[1], ahead) >= _MEASURE_AHEAD_S
        )
        vlc, at_1x, stretch = self._vlc, self._rate == 1.0, self._progress
        if not counted:
            self._progress = None
            return
        if stretch is None or stretch.at_1x != at_1x:
            # Counting starts (again): a stretch starts at VLC's next step, so it never books
            # a whole step against part of its time.
            self._progress = _Progress(at_1x, vlc.changes, None)
            return
        health = self._crawl if at_1x else self._health
        if vlc.changes != stretch.changes:
            end = vlc.at_change  # up to VLC's last step
            if stretch.start is None:
                self._progress = _Progress(at_1x, vlc.changes, end)
                return
        elif stretch.start is not None and min(vlc.still, vlc.played - stretch.start.played) >= max(
            health.window, _FROZEN_HOLDS * vlc.hold()
        ):
            # VLC's clock really stood still a whole window: that much was played of nothing.
            # A step that is only late (or missing: twice as long) counts at its end.
            end = vlc.mark()
        else:
            return
        self._progress = _Progress(at_1x, vlc.changes, end)
        wall = end.played - stretch.start.played
        if wall <= 0:
            return
        played = end.progress - stretch.start.progress
        rate = (end.asked - stretch.start.asked) / wall
        if not at_1x:
            if self._stay.rate_locked:
                return
            if self._health.add(wall, played, rate):
                self._stay.lock_rate()
                self._set_rate(1.0)
                _LOG.info(
                    "VLC played this channel at %.0f%% of the speed asked: staying at 1x on it",
                    100 * self._health.share,
                )
                self._learn()
        elif snapshot.get("hls") is True:
            if self._crawl.add(wall, played, 1.0):
                _LOG.info(
                    "VLC played this HLS channel at %.0f%% speed through the time-shift buffer "
                    "even at 1x: playing it directly",
                    100 * self._crawl.share,
                )
                self._learn(direct=True)
                self._fall_back(CRAWLING)

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
        less half a burst; 0 for a steady source. A burst later than `STALL_S` is a stall,
        not the sawtooth: the time since the edge moved counts up to there only.
        """
        burst = _number(snapshot.get("burst_s"))
        if burst <= 0:
            return 0.0
        return min(self._since_edge_moved(snapshot), burst + STALL_S) - burst / 2

    def _stalled(self, snapshot: Mapping[str, Any]) -> bool:
        """The source sent nothing for a burst plus `STALL_S` (a steady one: `STALL_S`)."""
        if self._ended:
            return False  # its input is over: nothing more is expected
        limit = _number(snapshot.get("burst_s")) + STALL_S
        return self._since_edge_moved(snapshot) > limit

    def _clock_frozen(self) -> bool:
        """VLC reports Playing but its clock stood still clearly longer than it steps (it is
        starved): past `VlcClock.hold()`, 1.5 learned intervals plus a quarter second."""
        playing = self._playback.state() is PlaybackState.PLAYING
        return playing and self._vlc.frozen()

    def _observe(self, snapshot: Mapping[str, Any], stalled: bool) -> None:
        """Give the lag filter this tick's live edge. A stalled source adds nothing (its
        standing edge is no rhythm), and the filter starts afresh once it sends again: it may
        come back ahead or behind where it was, a step taken in at once, not as a drift."""
        if stalled:
            self._was_stalled = True
            return
        if self._was_stalled:
            self._was_stalled = False
            self._filter.reset()
        self._filter.add(
            self._clock(),
            self._live_edge(snapshot),
            _number(snapshot.get("burst_s")),
            self._since_edge_moved(snapshot),
        )

    def _mean_lag(self, snapshot: Mapping[str, Any], position: float) -> float:
        """The mean lag behind the live edge (`LagFilter`); before its first edge, and while
        the source stalls (its edge stands; the filter would carry it on at real time), the
        lag in closed form (`_lag`)."""
        lag = None if self._was_stalled else self._filter.lag(self._clock(), position)
        return self._lag(snapshot, position) if lag is None else lag

    def _since_edge_moved(self, snapshot: Mapping[str, Any]) -> float:
        """Seconds since media last arrived: the live edge moved by `_ARRIVAL_S` or more since
        the last arrival. Less is the frame a segment adds as it closes (the helper reports a
        batch's end at once, and the next batch's first state is its last segment closing:
        taken for the batch, it dropped a bursty source's mean lag by a burst)."""
        edge, now = self._live_edge(snapshot), self._clock()
        seen = self._edge_seen
        if seen is None or edge >= seen + _ARRIVAL_S or edge < seen:
            self._edge_seen, self._edge_moved = edge, now
        return now - self._edge_moved

    def _reported_lag(self, snapshot: Mapping[str, Any], position: float) -> float:
        """The mean lag now: what StayLive acts on."""
        return max(0.0, self._mean_lag(snapshot, position))

    def _emit_timeline(self) -> None:
        """The timeline's state. Under Auto latency `auto` is True and `adjusted` says the
        channel's Auto target moved from where Auto starts (not a burst: that sets the target
        under any latency)."""
        snapshot = self._snapshot()
        edge = self._live_edge(snapshot)
        position = min(self._base + self._elapsed, edge)
        auto = self._auto
        lag = self._reported_lag(snapshot, position)
        shown = self._settled.value
        self.timelineChanged.emit(
            {
                "start": self._buffer_start(snapshot),
                "end": _number(snapshot.get("end")),
                "position": position,
                "live_edge": edge,
                "lag": lag,
                # The lag as the Stay live indicator shows it: settled, in half seconds.
                "shown": shown if shown is not None else SettledLag.rounded(lag),
                "mode": self._stay.mode.value,
                "rate": self._rate,
                "nudge": self._stay.nudge,  # the rate a nudge ramps to (1.0: none)
                "paused": self._paused,
                "ended": self._ended,
                "target": self._target(snapshot),
                "stalled": self._stalled(snapshot),
                "auto": auto is not None,
                "adjusted": auto is not None and auto.adjusted,
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
        """The latency's target, or the burst when that is larger; under Auto, plus what the
        channel added (`AutoTarget`)."""
        burst = _number(snapshot.get("burst_s"))
        if self._auto is not None:
            return self._auto.target(burst)
        return max(TARGETS[self._latency], burst)

    def _just_opened(self) -> bool:
        """A pipeline stream opened a moment ago: VLC has not played it yet (a fresh clock),
        or it is within `_REOPEN_HOLD_S` of the open. Its lag is not known yet, so stay-live
        does not reopen at live: a user's Go live and stay-live's reopen are never two opens
        in a row (a backlog landing just after Go live made them so, on one provider)."""
        if self._opened_at is not None or self._last_ms == 0:
            return True
        return self._clock() - self._last_open < _REOPEN_HOLD_S

    def _control_target(self) -> float:
        """The target stay-live steers to: as `_target`, for the source's burst as the lag
        filter classifies it (a burst the helper measures for a moment and drops is none)."""
        burst = self._filter.burst
        if self._auto is not None:
            return self._auto.target(burst)
        return max(TARGETS[self._latency], burst)

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

    def _qualities(self, snapshot: Mapping[str, Any]) -> dict[int, int]:
        """Each start's `_QUALITY_RANK`: the closed spans' `start_quality`, the open segment's
        as the helper knows it so far (`open_quality`)."""
        assert self._sid is not None
        ranks = {
            span.seq: _QUALITY_RANK.get(str(getattr(span, "start_quality", "key")), 0)
            for span in self._engine.spans(self._sid)
        }
        open_seq = snapshot.get("open")
        if isinstance(open_seq, int):
            ranks[open_seq] = _QUALITY_RANK.get(str(snapshot.get("open_quality") or "key"), 0)
        return ranks

    def _start_lags(
        self, snapshot: Mapping[str, Any], ahead: float = 0.0
    ) -> list[tuple[float, int]]:
        """(lag, seq) of every start, the newest first: the lag each gives once VLC plays it,
        `ahead` seconds of media still to come.

        Every start, the open segment's too (where the closed ones end; on a bursty source
        the newest GOP stays open until the next burst), by the lag it would give once VLC
        plays it (the edge moves on while VLC opens): for a bursty source the mean over a
        burst (`_lag`), for a steady source the lag itself. While playing, from where the lag
        filter has the live edge on average (2.1 stage 5, review I3): the mean stay-live then
        measures the landing with. Taken from the arrival now, a batch earlier or later than
        its rhythm (a sparse-PCR channel's 10 s batches, some 6 s apart, under a 12 s target
        duration) moved landings 1.7-2.3 s off either way."""
        assert self._sid is not None
        starts = [(span.start, span.seq) for span in self._engine.spans(self._sid)]
        open_seq = snapshot.get("open")
        if isinstance(open_seq, int):
            starts.append((_number(snapshot.get("end")), open_seq))
        mean = None
        if ahead <= 0 and not self._was_stalled and self._phase is _Phase.PIPELINE:
            mean = self._filter.edge(self._clock())  # None before its first edge
        if mean is None:
            mean = self._live_edge(snapshot) + ahead + self._burst_shift(snapshot)
        edge = mean + self._open_s
        return sorted((edge - start, seq) for start, seq in starts)

    def _live_start(
        self, *, cushion: bool = False, ahead: float = 0.0
    ) -> Optional[tuple[int, float]]:
        """The start Go live picks (a tune, Go live, stay-live's reopen at live) and the lag
        it gives once VLC plays it, on a live edge `ahead` seconds further (a tune's HLS
        backlog still to come); with `cushion`, None while none is far enough behind. A start
        without a keyframe or an intra picture (5a's `start_quality` "none": its picture
        breaks until the next keyframe) only when nothing else is far enough behind (or, in a
        short buffer, there at all); and a keyframe start over an intra picture's within
        stay-live's band (the target, a quarter second short to `_LAND_ABOVE_S` beyond),
        where it costs no steering, never further back (one IDR in a backlog of intra
        pictures started a tune 18 s above its target; an HEVC BLA, "open" too, starts as
        cleanly as an IDR)."""
        assert self._sid is not None
        snapshot = self._snapshot()
        target = self._target(snapshot)
        means = self._start_lags(snapshot, ahead)
        if not means:
            oldest = None if cushion else self._oldest_seq()
            if oldest is None:
                return None
            edge = self._live_edge(snapshot) + ahead
            return (
                oldest,
                edge - self._start_of(oldest) + self._burst_shift(snapshot) + self._open_s,
            )
        ranks = self._qualities(snapshot)
        # Stay-live slows down only to build room, gently and briefly, so a landing below the
        # target would keep its smaller buffer for a long while: none below a quarter second
        # short of it. Then the nearest the target (ties: the newer start) within stay-live's
        # band, else the nearest above.
        safe = [mean for mean in means if mean[0] >= target - _BELOW_TARGET_S]
        if not safe:
            if cushion:
                return None
            clean = [mean for mean in means if ranks.get(mean[1], 0) < _QUALITY_RANK["none"]]
            lag, seq = (clean or means)[-1]  # a short buffer: all of it there is
            return seq, lag
        clean = [mean for mean in safe if ranks.get(mean[1], 0) < _QUALITY_RANK["none"]]
        safe = clean or safe
        band = [mean for mean in safe if mean[0] <= target + _LAND_ABOVE_S]
        if band:
            keys = [mean for mean in band if ranks.get(mean[1], 0) == _QUALITY_RANK["key"]]
            band = keys or band
        if band:
            lag, seq = min(band, key=lambda mean: (abs(mean[0] - target), mean[0]))
        else:
            lag, seq = safe[0]
        return seq, lag

    def _jump_lag(self) -> Optional[float]:
        """Where a jump to live (stay-live's reopen, 2.1 stage 5) would land now: the mean lag
        once VLC plays the start Go live would pick, when it starts cleanly (a keyframe or an
        intra picture); None otherwise, and while none is far enough behind."""
        if self._sid is None or self._phase is not _Phase.PIPELINE:
            return None
        pick = self._live_start(cushion=True)
        if pick is None:
            return None
        seq, lag = pick
        snapshot = self._snapshot()
        audio_only = snapshot.get("video") is False  # every start is "none", and clean
        if not audio_only and self._qualities(snapshot).get(seq, 0) >= _QUALITY_RANK["none"]:
            return None  # only starts that break the picture are far enough behind
        return lag

    def _seek_start(
        self, t: float, *, lo: float = -math.inf, hi: float = math.inf
    ) -> Optional[Span]:
        """Where a seek to `t` opens (2.1 stage 5), among segments starting in [`lo`, `hi`):
        the clean start (a keyframe, or an intra picture) latest at or before `t` within
        `_SEEK_REACH_S`, a keyframe's if one is at most `_KEY_REACH_S` further back; without
        one, a clean start after `t` within `_KEY_REACH_S` (a keyframe's first); without
        either, the segment holding `t` (or, out of bounds, the first in them after `t`).
        None while nothing is closed (or nothing is in bounds)."""
        assert self._sid is not None
        spans = self._engine.spans(self._sid)
        if not spans:
            return None
        t = min(max(t, spans[0].start), spans[-1].end)
        inside = [span for span in spans if lo <= span.start < hi]
        ranks = self._qualities(self._snapshot())
        key, none = _QUALITY_RANK["key"], _QUALITY_RANK["none"]
        clean = [span for span in inside if ranks.get(span.seq, 0) < none]
        before = [span for span in clean if t - _SEEK_REACH_S <= span.start <= t]
        if before:
            latest = max(span.start for span in before)
            keys = [
                span
                for span in before
                if ranks.get(span.seq, 0) == key and span.start >= latest - _KEY_REACH_S
            ]
            return max(keys or before, key=lambda span: span.start)
        after = [span for span in clean if t < span.start <= t + _KEY_REACH_S]
        if after:
            keys = [span for span in after if ranks.get(span.seq, 0) == key]
            return min(keys or after, key=lambda span: span.start)
        holding = _locate(spans, t)
        if holding is not None and lo <= holding.start < hi:
            return holding
        later = [span for span in inside if span.start > t]
        if later:
            return min(later, key=lambda span: span.start)
        return max(inside, key=lambda span: span.start) if inside else None

    def _say_unclean(self, seq: int) -> None:
        """Opening at `seq`, a start without a keyframe or an intra picture: the pickers take
        one only when nothing else is in reach. Said for debugging (an audio-only stream has
        no other start, so nothing is said there)."""
        snapshot = self._snapshot()
        if snapshot.get("video") is False:
            return
        if self._qualities(snapshot).get(seq, 0) < _QUALITY_RANK["none"]:
            return
        _LOG.debug(
            "Segment %d starts without a keyframe, and none is in reach: its picture may "
            "break until the next one.",
            seq,
        )

    def _advance(self) -> float:
        """VLC's position (spec §6.3; 2.1 stage 2): the opened segment's start plus VLC's
        clock as `VlcClock` follows its readings."""
        ms = self._playback.time_ms()
        playing = self._playback.state() is PlaybackState.PLAYING
        edge = self._live_edge(self._snapshot_cache)  # the most VLC can have played
        limit = edge - self._base if edge > self._base else math.inf
        # The timestamp jumps the helper left out of its timeline while its ring holds them,
        # as positions since the open (those before it negative, never reached).
        breaks = [
            (_number(where) - self._base, _number(seconds))
            for where, seconds in self._snapshot_cache.get("breaks") or ()
        ]
        self._elapsed = self._vlc.update(
            self._clock(), ms, playing, self._rate, limit=limit, breaks=breaks
        )
        self._last_ms = ms
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
        self._say_unclean(seq)
        self._base, self._elapsed = self._start_of(seq), 0.0
        self._last_ms = 0
        self._opened_at = self._last_open = self._clock()
        self._vlc.restart(self._last_open)  # VLC's clock starts at the segment's start
        self._stay.absorb()  # the position starts afresh: a step, not a drift
        self._settled.reset()  # and the lag shows where it landed at once
        self._rate_mark = self._rate_from = None  # rate health counts once this one plays
        self._progress = None
        self._health.reset()
        self._crawl.reset()
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
        # Where playback starts cleanly, as a seek there; past the ring, never before the
        # segment the margin reaches (an earlier keyframe would give the margin back).
        holding = _locate(self._engine.spans(self._sid), at) if expired else None
        lo = holding.start if holding is not None else -math.inf
        span = self._seek_start(at, lo=lo)
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
