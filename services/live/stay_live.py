"""Stay-live controller: holds a latency target with as few and as gentle rate changes as it can.

A live source arrives in its own rhythm (HLS segments, raw TS bursts or chunks), so the lag behind
the live edge is a sawtooth. `LagFilter` gives its mean (2.1): per tick the mean over a burst in
closed form, averaged over a window of a dozen bursts, so arrivals that are late, early or not
alike (a public HLS channel measured in 2.1: 6 s segments every 7 s, every sixth one 12 s) do
not move it. The window is over the live edge only: the player's side (its rate, a frozen
clock, a reopen) shows in the position at once. The filter also says how much its mean still
wanders by itself (`spread`) and which burst the source has (`burst`, with hysteresis: a burst
the helper measures for a moment is none).

`StayLive` leaves the rate at 1x while the mean lag stays within a dead band around the target:
at least `_BAND_S`, at least `_BAND_SPREAD` times the filter's spread, and behind the target a
quarter of a burst more (a bursty source's target already holds half a burst of room). Only a
lag outside the band for `_PERSIST_S` (counted while VLC plays: buffering, a reopen or Go live
start the count again, so such a step is taken into the lag once and never chased) starts a
nudge: one gentle rate (1.03x behind, 0.97x ahead), ramped in and out in `_RAMP_STEPS` steps
`_RAMP_EVERY_S` apart (each libVLC rate change retunes its audio: few, small ones) and sized
to end at the target, then 1x again. At most one nudge per `_BUDGET_S`; a lag outside the
band before that only widens the band. Two cases act sooner (after `_URGENT_S`, a count that
VLC's waits pause but do not restart) and beyond the budget: further than `_FAR_S` behind
(1.10x), and less room than `_ROOM_S` when a burst lands (a steady source: at all; 0.95x: VLC
would run dry, before each burst as 2.0.1 measured, or at any hiccup). And VLC running out of
room (`trouble()`: a starvation or a stall that ran out the cushion, when Auto raises the
target) is owed one slow-down to the target at 0.95x once VLC played `_OWED_HOLD_S` short of
it, beyond the budget, as 2.0.2 built a raised target at once; only a lag short of the target
for that long ends a catch-up (a stall far behind it changes nothing, and so does the first
tick after a stall, which the lag filter starts afresh from: a source that sent what it
missed is back near the target on the next). A nudge stops early when the lag gets to the
target by itself (a step), and is resized when the target moves; a catch-up that trouble cut
counts against the budget only if no slow-down followed it. `SettledLag` gives the lag as
shown: in half seconds, changed only after it differed for `_SHOWN_HOLD_S`.

A stream opened at the start Go live picks (2.1: a tune, Go live, a reopen at live;
the caller says so with `landed()`) lands where the source's keyframes allow. Above the
target by up to a keyframe interval (one provider's 4K channel has one every 10 s) is no
drift: for `_START_S` of playing, far behind and the reopen at live count from that landing
(`_floor`, which follows the lag down as a nudge takes it off), so it gets a gentle nudge,
not 1.10x; after that, as ever (a reconnect's backlog later is drift). Short of the target
(nothing further back was buffered, or a raw TS source's bursts were measured after the start
and raised the target) within `_START_S` of playing, the room is built once, at
`_GENTLE_DOWN` for at most `_START_BUILD_S` of playing, outside the budget; what is still
short then is accepted (no 2.0.1 slow-down, no nudge ahead) until the lag is back within the
band, or VLC runs dry
(`trouble()`: the owed slow-down builds all of it then).

Far behind, a jump to live is better than a long catch-up (2.1: 8 s at 1.10x takes 80
s). The caller says where one would land (`jump_lag`: the mean lag once VLC plays the clean
start Go live would pick, a keyframe's or an intra picture's; None without one). Once far
behind for `_URGENT_S`, and no jump (a tune, Go live, a reopen at live: whatever `landed()`
hears) came within `_JUMP_GAP_S`, stay-live reopens at live instead of speeding up when that
landing is within the band or `_JUMP_CLOSER_S` nearer than the lag; if it is not, for up to
`_JUMP_WAIT_S` it waits at the rate it has for the next keyframes to bring one (one provider's
4K channel has one every 10 s). A catch-up already under way (one started within the minute)
ends in such a jump once the minute passed. When no jump can do better than the lag less
`_GRAIN_S`, the lag is the keyframes' grain, not drift: the nudge stays gentle (and the band
is not widened for it). Never while a stream just opened (`hold_reopen`, which the wait does
not count); the reopen beyond the target plus 20 s stays as it was. A locked rate jumps too
(it changes no rate), and a gentle nudge a jump cut counts against the budget. A jump or Go
live that lands short builds nothing (`landed(build=False)`): it was picked at the target in
a full buffer, so its shortfall is the pick's error, tolerated but for too little room and
trouble, until a stall's end (the source's step, steered as ever).

A raw TS source that restarts often (2.8.5: the caller sets `junctions` while one restarted
within two minutes) is steered at its restarts instead, where the helper's trimmer takes what
it is asked beyond the repeat it measures (`services.live.ts.RepeatTrimmer.extra`): one
provider restarts every ~20 s and hides seconds of repeat from that measure, so its edge ran
4-8 s a minute ahead and stay-live jumped every minute or two. Behind the target by more than
`_JUNCTION_MIN_S`, `Decision.junction` asks the next restart to take that much back, at a
cut the stream has anyway (where the run hides a repeat, the repeat goes): no nudge behind
the target starts (one under way ramps out) and no jump is made. The reopen beyond the target
plus 20 s stays; ahead of the target, as ever.

On some channels VLC cannot play at any rate but 1x: its position crawls instead (measured in
2.0.2: 4.5 s of media in 37 s). `RateHealth` notices that from VLC's progress, and the caller
then calls `lock_rate()`: from there on stay-live keeps 1x and only reopens at live when the
lag is beyond the target plus 20 s, as before, or far behind by a jump (2.1).

"Auto" latency (`AUTO`) steers by the channel's learned target (2.1:
`services.live.learned_target`, fed by the caller), as `SteeredTarget` moves it: rises in steps
(at once with trouble), falls slowly. A target raised above the lag is reached without a jump
back: by a nudge below 1x unless the rate is locked, else as VLC waits at the live edge, which
adds to the lag by itself. 2.0.2's Auto (1.5 s per starvation or stall, 0.5 s off per 5
quiet minutes) stays in the tests (`tests/auto_target.py`), the reference the learned target
is measured against.

Pure logic; the caller feeds the measured lag and applies the returned Decision.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import math
from typing import Optional

TARGETS = {"low": 1.0, "normal": 2.5, "stable": 6.0}
AUTO = "auto"  # the latency setting that learns each channel's target (learned_target)
LATENCIES = (AUTO, *TARGETS)

_SETTLE = 0.2  # a chosen speed catching up ends this close to the target
_REOPEN = 20.0
_HEALTH_WINDOW_S = 3.0  # rate health: VLC's progress is judged over this much counted time
_HEALTH_SHARE = 0.7  # under this share of rate x wall time, VLC cannot play at that rate
_HEALTH_PIECES = 2  # a window holds at least this many stretches (each up to a step of VLC's)
_AUTO_DOWN = 0.5  # 2.0.2's Auto lowered a channel's target this much ...
_AUTO_QUIET_S = 300.0  # ... per this long without a starvation or a stall

# Steering (2.1): the rate changes only for a lasting drift, gently and rarely.
_BAND_S = 1.0  # the dead band reaches at least this far either side of the target ...
_BAND_SPREAD = 1.5  # ... at least this many times the spread of the lag estimate ...
_BAND_BURST = 0.25  # ... and behind the target this share of a burst further
_PERSIST_S = 15.0  # outside the band this long (counted while VLC plays) before a nudge
_FAR_S = 8.0  # further behind the target than this: the nudge is _FAST ...
_ROOM_S = 0.5  # ... and with less room than this before each burst it is _SLOW ...
_URGENT_S = 3.0  # ... both after this long outside, whatever the budget
_GENTLE_UP = 1.03  # the nudge behind the target ...
_GENTLE_DOWN = 0.97  # ... and the one that adds room ahead of it
_FAST = 1.10
_SLOW = 0.95
_RAMP_STEPS = 3  # a nudge ramps in (and out) in this many equal steps ...
_RAMP_EVERY_S = 1.0  # ... this far apart
_OWED_MIN_S = 0.5  # a slow-down owed after trouble is made for a deficit beyond this ...
_OWED_HOLD_S = 1.0  # ... once it lasted this long while VLC plays (right after a stall the
# lag filter holds one sample: a source that sent what it missed is back on the next tick)
_BUDGET_S = 300.0  # one gentle nudge at most in this long
_START_S = 60.0  # after a landing, a shortfall within this much playing is the start's ...
_START_BUILD_S = 20.0  # ... built at _GENTLE_DOWN for at most this much playing, then accepted
_WIDEN_S = 0.01  # beyond the budget the band takes in the lag outside it, and this much
_BACK_IN_S = 3.0  # a landing's shortfall back in the band this long is no more its own
# Jumps (2.1): far behind, a reopen at live instead of a long catch-up.
_JUMP_GAP_S = 60.0  # never two jumps (a tune, Go live, a reopen at live) within this long
_JUMP_CLOSER_S = 5.0  # a jump helps when it lands within the band, or at least this much nearer
_JUMP_WAIT_S = 12.0  # far behind, waiting this long for one that helps (keyframes 10 s apart)
_GRAIN_S = 1.0  # no jump does better than the lag less this: the keyframes' grain, no drift
# Junctions (2.8.5): a source that restarts often takes back what stay-live is behind.
_JUNCTION_MIN_S = 1.0  # behind the target by more than this: asked of the next restart
_JUNCTION_REF_S = 300.0  # ... the highest target of this long at least (twice at most)
_TICK_S = 0.25  # the caller's update interval (what a first sample stands for)
_MAX_DT = 1.0  # a longer gap between two updates counts as this long

# LagFilter: the mean lag over the source's own arrival rhythm.
_WINDOW_MIN_S = 10.0  # a steady source: the mean over this long ...
_WINDOW_BURSTS = 12.0  # ... a bursty one over this many bursts ...
_WINDOW_MAX_S = 90.0  # ... at most this long
_STEP_S = 3.0  # an arrival that moves the level more than this (and a burst) is a step
STALL_S = 2.0  # a source silent a burst plus this long (steady: this long) has stalled
_SPREAD_S = 120.0  # the mean's own spread is measured over this long ...
_SPREAD_EVERY_S = 1.0  # ... from one reading per this long
_WARMUP_S = 10.0  # until the filter held this long (or a window), its spread is its range
_BURST_ON_S = 10.0  # a larger burst counts once the source reported it this long (and
# two such bursts' worth) ...
_BURST_OFF_S = 30.0  # ... a smaller one (or none) once it reported that this long
_BURST_STEP_S = 0.5  # burst changes smaller than this leave it as it was

# SteeredTarget: the learned target (Auto, 2.1) as stay-live steers by it.
_STEER_STEP_S = 0.5  # a rise moves it in a step this big at least ...
_STEER_HOLD_S = 10.0  # ... once it held this long (at once with trouble) ...
_STEER_FALL = _AUTO_DOWN / _AUTO_QUIET_S  # ... a fall of what trouble raised at most this
# fast (Auto's quiet decay) ...
_STEER_FALL_ARRIVALS = 0.5 / 60.0  # ... of what the arrivals alone raised, this fast

# SettledLag: the lag as shown.
_SHOWN_STEP_S = 0.5
_SHOWN_HOLD_S = 3.0


def start_target(latency: str) -> float:
    """The target a latency setting starts with (Auto: Normal's); KeyError if unknown."""
    return TARGETS["normal"] if latency == AUTO else TARGETS[latency]


class Mode(str, Enum):
    LIVE = "live"
    SHIFTED = "shifted"
    CATCHING_UP = "catching_up"


@dataclass(frozen=True)
class Decision:
    rate: float
    reopen_live: bool = False
    jump: bool = False  # the reopen is a jump instead of a catch-up (not the 20 s bound)
    # 2.8.5, with `StayLive.junctions`: seconds behind the target the next stream restart is
    # to take back (0.0: none asked).
    junction: float = 0.0


class RateHealth:
    """Whether VLC plays at the rates stay-live sets: its progress against rate x wall time.

    The caller adds only time that says something about the rate: at a rate other than 1x,
    with VLC playing and media ahead of it (a VLC waiting at the live edge is starved, not
    slow), in stretches that end at a step of VLC's clock (2.1). Each window of
    `window_s` counted seconds (default `_HEALTH_WINDOW_S`) and at least `_HEALTH_PIECES`
    stretches (so at least two of VLC's steps, on a channel whose clock steps every 2 s) is
    judged on its own; `share` is the last window's progress against what its rates asked.
    The verdict needs `windows` bad windows in a row (a healthy one starts the count again; a
    new window after an open keeps it).
    """

    def __init__(self, window_s: float = _HEALTH_WINDOW_S, *, windows: int = 1) -> None:
        self.share = 1.0
        self.window = window_s
        self._needed = windows
        self._bad = 0  # bad windows in a row
        self.reset()

    def reset(self) -> None:
        """Start a new window (the stream was opened again, say)."""
        self._wall = self._asked = self._played = 0.0
        self._pieces = 0

    def add(self, wall: float, played: float, rate: float) -> bool:
        """`wall` seconds at `rate` in which VLC played `played` seconds of media.

        True once a window's progress is under `_HEALTH_SHARE` of what was asked: VLC cannot
        play this channel at that rate.
        """
        self._wall += wall
        self._asked += wall * rate
        self._played += played
        self._pieces += 1
        if self._wall < self.window - 1e-9 or self._pieces < _HEALTH_PIECES:
            return False
        self.share = self._played / self._asked if self._asked > 0 else 1.0
        self.reset()
        self._bad = self._bad + 1 if self.share < _HEALTH_SHARE else 0
        if self._bad < self._needed:
            return False
        self._bad = 0
        return True


class SteeredTarget:
    """The learned target (Auto latency, 2.1: `services.live.learned_target`) as
    stay-live steers by it. The learned target moves with every estimate (the arrival need
    once a second, the margin as it fades); steered by as it is, each move would be a new
    target to build or catch up to. So a rise counts in a step of at least `_STEER_STEP_S`
    once it held `_STEER_HOLD_S` (as a larger burst counts), at once when trouble raised it
    (its owed slow-down comes with it: `StayLive.trouble`); a fall is never a step: what
    trouble raised comes down at most at `_STEER_FALL` (Auto's quiet decay, half a second per
    5 minutes), what the arrivals alone raised at `_STEER_FALL_ARRIVALS` (half a second a
    minute: a stall's aftermath lifts the need for a minute or so). What the source's burst
    moves in it (the lag filter's burst, which has its own hysteresis) moves it at once,
    either way (`shift`): a burst the helper measured late counts as soon as the filter
    takes it, and a one-off first "burst" (a backlog) goes as the filter drops it.
    """

    def __init__(self) -> None:
        self.value: Optional[float] = None
        self._troubled: Optional[float] = None  # the level trouble raised it to, falling
        self._rising = 0.0  # seconds the learned target held a step above it

    def shift(self, delta: float) -> None:
        """The source's burst moved the learned target by `delta` seconds: so much at once."""
        if self.value is not None:
            self.value += delta
        if self._troubled is not None:
            self._troubled += delta

    def update(self, learned: float, dt: float, *, trouble: bool = False) -> float:
        """The target to steer by, `dt` seconds after the last update, the learned target now
        `learned`; `trouble`: trouble raised it this moment."""
        value, dt = self.value, max(0.0, dt)
        if self._troubled is not None:
            self._troubled -= _STEER_FALL * dt
            if self._troubled <= learned + 1e-9:
                self._troubled = None  # what trouble raised is all learned now
        if value is None:
            self.value, self._rising = learned, 0.0
        elif trouble and learned > value:
            self.value = self._troubled = learned
            self._rising = 0.0
        elif learned >= value + _STEER_STEP_S - 1e-9:
            self._rising += dt
            if self._rising >= _STEER_HOLD_S - 1e-9:
                self.value, self._rising = learned, 0.0
        else:
            self._rising = 0.0
            if learned < value:
                held = -math.inf if self._troubled is None else self._troubled
                self.value = max(learned, value - _STEER_FALL_ARRIVALS * dt, min(value, held))
        assert self.value is not None
        return self.value


class LagFilter:
    """The mean lag behind the live edge over the source's own arrival rhythm (2.1).

    Each tick `add()` takes the live edge and the time since it last moved. For a bursty
    source (`burst` > 0) the mean over a burst is the lag now plus that time, less half a
    burst (in closed form, as LiveSession's `_lag`; the time counts up to the burst plus
    `STALL_S`). Carried forward at real time (edge + that shift - wall) it is a level: steady
    while bursts land as large as they are apart, stepping by the difference when they do
    not; for a steady source, the edge less the wall clock. Its time-weighted mean over a
    window of `_WINDOW_BURSTS` bursts (`_WINDOW_MIN_S` to `_WINDOW_MAX_S`) plus the wall time
    now is where the live edge "is" on average; less the position, the mean lag (`lag`).

    An arrival that moves the level by more than `_STEP_S` (and more than a burst) is no
    rhythm but a step (a backlog delivered at once, say): the window starts afresh from it,
    so it is taken in at once. `reset()` does the same for the caller (a new session, or a
    source that stalled: it may come back ahead of or behind where it was).

    `spread` is how far the mean still wanders by itself: half the range of its readings over
    `_SPREAD_S` (one each `_SPREAD_EVERY_S`) around their straight-line trend, so a slow real
    drift does not count. Until the filter held `_WARMUP_S` (or a window, if shorter) it is
    half the range of the levels in the window, all the sawtooth could still leave.

    `burst` is the source's burst with hysteresis: the first one reported is taken at once (an
    HLS source's target duration, or 0.0), a larger one once reported for `_BURST_ON_S` and
    two such bursts, a smaller one (or 0.0) once reported for `_BURST_OFF_S`; changes under
    `_BURST_STEP_S` are ignored. A raw TS source whose measured bursts come and go (2.0.1's
    meter, on one provider: 15 times in 5 minutes) then keeps one burst. It sets the closed
    form and the window; a change starts the window afresh.

    Pure logic: no clock of its own.
    """

    def __init__(self) -> None:
        self._samples: deque[tuple[float, float, float]] = deque()  # (wall, level, weight)
        self._readings: deque[tuple[float, float]] = deque()  # (wall, mean level)
        self._sum = self._weight = 0.0
        self._last: Optional[float] = None  # the last sample's time
        self._first: Optional[float] = None  # the first since the window started afresh
        self._level: Optional[float] = None  # the last sample's level
        self._spread = 0.0
        self.burst = 0.0
        self._burst_seen = False
        self._pending: Optional[tuple[float, float]] = None  # (since, reported) unlike `burst`
        self.restarts = 0  # how often its window started afresh (a step, a burst, reset())
        self.steps = 0  # arrivals that moved the level more than `_STEP_S` (restarting or not)

    def reset(self) -> None:
        """Forget the edges (the burst stays: it is the source's)."""
        self._samples.clear()
        self._readings.clear()
        self._sum = self._weight = 0.0
        self._last = self._first = self._level = None
        self._spread = 0.0
        self.restarts += 1

    def settled(self, now: float) -> bool:
        """Its mean has held `_WARMUP_S` and two bursts since it started afresh (or its whole
        window, if shorter): a pick read from it misses only by its own error (2.1: right
        after a restart the mean still moves with the arrivals)."""
        if self._first is None:
            return False
        return now - self._first >= min(self.window, max(_WARMUP_S, 2 * self.burst)) - 1e-9

    @property
    def window(self) -> float:
        """The seconds the mean is taken over now."""
        return min(_WINDOW_MAX_S, max(_WINDOW_MIN_S, _WINDOW_BURSTS * self.burst))

    def add(self, now: float, edge: float, burst: float = 0.0, since: float = 0.0) -> None:
        """The live edge at `now`, the burst the source reports (0.0: steady), and the seconds
        since the edge last moved."""
        self._classify(now, max(0.0, burst))
        burst = self.burst
        shift = min(max(0.0, since), burst + STALL_S) - burst / 2 if burst > 0 else 0.0
        level = edge + shift - now
        moved = 0.0 if self._level is None else abs(level - self._level)
        if moved > _STEP_S:
            self.steps += 1  # (under a burst the rhythm may still hold it: no restart)
        if moved > max(_STEP_S, burst):
            self.reset()  # a step, not the rhythm: taken in at once
        dt = _TICK_S if self._last is None else min(max(now - self._last, 0.0), _MAX_DT)
        self._last, self._level = now, level
        if self._first is None:
            self._first = now
        self._samples.append((now, level, dt))
        self._sum += level * dt
        self._weight += dt
        window = self.window
        samples = self._samples
        while len(samples) > 1 and samples[0][0] < now - window:
            _, old, weight = samples.popleft()
            self._sum -= old * weight
            self._weight -= weight
        readings = self._readings
        if not readings or now - readings[-1][0] >= _SPREAD_EVERY_S - 1e-9:
            readings.append((now, self.level()))
            while readings and readings[0][0] < now - _SPREAD_S:
                readings.popleft()
            self._spread = self._measure(now, window)

    def level(self) -> float:
        """The mean level over the window (0.0 before any edge)."""
        return self._sum / self._weight if self._weight > 0 else 0.0

    def lag(self, now: float, position: float) -> Optional[float]:
        """The mean lag at `now` for the player at `position`; None before any edge."""
        edge = self.edge(now)
        return None if edge is None else edge - position

    def edge(self, now: float) -> Optional[float]:
        """Where the live edge is on average at `now` (what `lag` measures from; a start a
        jump picks lands that far behind it, 2.1); None before any edge."""
        if self._weight <= 0:
            return None
        return self.level() + now

    def spread(self) -> float:
        return self._spread

    def burst_for(self, reported: float) -> float:
        """The burst it holds (with its hysteresis), or, before its first edge, the one the
        source reports: the first report counts at once."""
        return self.burst if self._burst_seen else max(0.0, reported)

    def _classify(self, now: float, reported: float) -> None:
        if not self._burst_seen:
            self._burst_seen, self.burst = True, reported
            return
        if abs(reported - self.burst) < _BURST_STEP_S:
            self._pending = None
            return
        pending = self._pending
        rising = reported > self.burst
        if pending is None or (pending[1] > self.burst) != rising:
            pending = self._pending = (now, reported)
        hold = max(_BURST_ON_S, 2 * reported) if rising else _BURST_OFF_S
        if now - pending[0] >= hold - 1e-9:
            self.burst, self._pending = reported, None
            self.reset()  # the closed form changed with it: the window starts afresh
        else:
            self._pending = (pending[0], reported)

    def _measure(self, now: float, window: float) -> float:
        assert self._first is not None
        if now - self._first < min(_WARMUP_S, window):
            levels = [level for _, level, _ in self._samples]
            return (max(levels) - min(levels)) / 2  # all the sawtooth could still leave
        readings = [(t, y) for t, y in self._readings if t >= self._first]
        n = len(readings)
        if n < 3:
            return 0.0
        t0 = readings[0][0]
        xs = [t - t0 for t, _ in readings]
        ys = [y for _, y in readings]
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 0 else 0.0
        residuals = [y - (my + slope * (x - mx)) for x, y in zip(xs, ys)]
        return (max(residuals) - min(residuals)) / 2


class SettledLag:
    """The lag as shown (2.1): in `_SHOWN_STEP_S` steps, changed only once the lag differed
    from what is shown by a step or more for `_SHOWN_HOLD_S`; `reset()` (a jump) shows the
    next lag at once."""

    def __init__(self) -> None:
        self.value: Optional[float] = None
        self._since: Optional[float] = None

    def reset(self) -> None:
        self.value = self._since = None

    @staticmethod
    def rounded(lag: float) -> float:
        return round(max(0.0, lag) / _SHOWN_STEP_S) * _SHOWN_STEP_S

    def update(self, now: float, lag: float) -> float:
        if self.value is None:
            self.value, self._since = self.rounded(lag), None
        elif abs(lag - self.value) >= _SHOWN_STEP_S - 1e-9:
            if self._since is None:
                self._since = now
            if now - self._since >= _SHOWN_HOLD_S - 1e-9:
                self.value, self._since = self.rounded(lag), None
        else:
            self._since = None
        return self.value


@dataclass
class _Nudge:
    peak: float  # the rate it ramps to
    left: float  # seconds of lag still to take off (behind) or add (ahead)
    target: float  # the target it was sized for
    ending: bool = False  # ramping out: back to 1x, whatever the lag does meanwhile
    cut: bool = False  # ended before its end (trouble): it does not count against the budget
    spent: bool = False  # it took its whole size (ramping out: it counts, whatever follows)
    stepped: Optional[float] = None  # when the rate last stepped
    limit: Optional[float] = None  # a start's build: seconds below 1x it has left (budget-free)

    @property
    def step(self) -> float:
        """One ramp step's size."""
        return abs(self.peak - 1.0) / _RAMP_STEPS


class StayLive:
    def __init__(self, latency: str = "normal") -> None:
        self.mode = Mode.LIVE
        self.target = start_target(latency)
        # Set by the caller with `target`: the source's burst (0.0: steady; `LagFilter.burst`),
        # how far its lag estimate wanders by itself (`LagFilter.spread`), and how much higher
        # the target would be for the burst the source reports now, which the filter does not
        # count yet (`slack`: never speed up towards a target that may be about to rise).
        self.burst = 0.0
        self.spread = 0.0
        self.slack = 0.0
        # Set by the caller: a stream opened a moment ago (Go live, a seek, a reopen), so its
        # lag is not known yet: no reopen at live until it is (never two opens in a row).
        self.hold_reopen = False
        # Set by the caller: where a jump to live would land now, as a mean lag (the clean
        # start Go live would pick), or None without one; asked only far behind.
        self.jump_lag: Optional[Callable[[], Optional[float]]] = None
        self._jump_age = math.inf  # seconds since the last landing (a tune, Go live, a jump)
        self._waited = 0.0  # far behind: seconds waited for a jump that helps
        self._rate = 1.0  # last rate decided in LIVE
        self._user_rate = 1.0
        self._reopened = False
        self.rate_locked = False  # VLC cannot play this channel at another rate: 1x for good
        self.background = False  # a multiview tile without the focus: 1x, jumps only (2.2)
        # Set by the caller (2.8.5): the source restarts often, and the helper takes what a
        # restart is asked (`Decision.junction`): behind the target, that is how the lag goes.
        self.junctions = False
        # What an ask is measured from (`_junction`): the highest target (behind it) in this
        # `_JUNCTION_REF_S` block of the caller's clock and in the one before.
        self._ref_block = -1
        self._ref_now = self._ref_before = 0.0
        self._last: Optional[float] = None  # the last update's time
        self._outside = 0.0  # seconds outside the band, on `_side`, counted while playing
        self._side = 0  # +1 behind the band, -1 ahead of it
        self._nudge: Optional[_Nudge] = None
        self._nudged_at: Optional[float] = None  # the last budgeted nudge's end
        self._wide = 0.0  # the band widened while the budget is spent
        self._urgent = False  # the time outside counts towards an urgent nudge
        self._owed = False  # VLC ran out of room: a slow-down to the target is owed
        self._short_for = 0.0  # seconds the lag was short of the target since then
        self._uncharged: Optional[float] = None  # a cut catch-up's end, while it may count
        self._floor = 0.0  # how far above the target the last landing was (see landed())
        self._landing = 0.0  # seconds of playing left in which that floor holds
        self._start = 0.0  # seconds of playing in which a shortfall is the start's
        self._accepting = False  # the start's build ran: what is still short is accepted
        # A jump or Go live landed (no build): what is short of the band is its pick's, not
        # missing room; only too little room (`_short`) or trouble acts on it.
        self._tolerant = False
        # The start's build and what it accepts, and a jump's tolerance, are for the
        # landing's own shortfall only: not once the target rose above the one it landed at
        # (taken at the first update after it, when the caller has set the target in force),
        # nor once the lag was back in the band after the stream played (`_own_shortfall`).
        self._landed_at: Optional[float] = None
        self._in_band = 0.0  # seconds the lag was back in the band since (`_BACK_IN_S`)

    @property
    def nudge(self) -> float:
        """The rate the current nudge ramps to (1.0: none)."""
        return self._nudge.peak if self._nudge is not None else 1.0

    @property
    def shortfall_accepted(self) -> bool:
        """The start's build ran and the lag is still short of the band: VLC running dry now
        is trouble, even though the lag never reached the target."""
        return self._accepting

    def band(self, side: int) -> float:
        """How far the lag may be from the target on `side` (+1 behind, -1 ahead)."""
        band = max(_BAND_S, _BAND_SPREAD * self.spread, self._wide)
        return band + (_BAND_BURST * self.burst if side > 0 else 0.0)

    def _centre(self, side: float) -> float:
        """The target on `side`: behind it, as high as the burst reported now would set it."""
        return self.target + max(0.0, self.slack) if side > 0 else self.target

    def lock_rate(self) -> None:
        """VLC cannot play this channel at any rate but 1x (`RateHealth`): keep 1x from now.

        Stay-live then changes no rate: no nudge, only the reopen at live when the lag is
        beyond the target plus 20 s, or a jump far behind (`_far_locked`). A speed the user
        chooses still applies.
        """
        self.rate_locked = True
        self._rate = 1.0
        self._nudge = None

    def set_background(self, background: bool) -> None:
        """A multiview tile without the focus (2.2): it is muted and small, so stay-live
        changes no rate (a nudge under way ends at once, at 1x) and only goes live, as after
        `lock_rate()`: a jump far behind, or the reopen beyond the target plus 20 s. With
        the focus back it steers as before. A speed the user chose still applies."""
        self.background = background
        if background:
            self._rate = 1.0
            self._nudge = None

    def set_latency(self, name: str) -> None:
        self.target = start_target(name)

    def absorb(self) -> None:
        """The lag stepped (a reopen, say): count the time outside the band afresh."""
        self._outside, self._side, self._urgent = 0.0, 0, False
        self._waited = 0.0

    def landed(self, excess: float, *, build: bool = True) -> None:
        """A stream opened at the start Go live picks (after `user_go_live()` for a Go live):
        its mean lag lands `excess` seconds above the target (below it when negative).

        Above it, for `_START_S` of playing, far behind (1.10x) and the reopen at live count
        from there, following the lag down: the landing a keyframe interval allowed is taken
        off by a gentle nudge. Short of it within `_START_S` of playing, the room is built
        once at `_GENTLE_DOWN` for at most `_START_BUILD_S`, then the rest is accepted (a
        source that sent nothing further back gets one build, never one after another).
        Without `build` (a jump, Go live: picked at the target with the buffer full, 2.1)
        the landing's own shortfall is the pick's, not missing room: it is
        tolerated, but for too little room (`_short`, 2.0.1) and VLC running dry
        (`trouble()`), until the next landing or a stall's end (`source_stepped()`).

        Either way only the landing's own shortfall (`_own_shortfall`): a target that rises
        above the one it landed at (a higher latency, a burst measured after the start,
        Auto, a learned target) ends the start's build window, what it accepted and a jump's
        tolerance, so the rise is built once as without the landing (gently, budgeted; at
        0.95x where VLC would run dry); and so does the lag back in the band once the stream
        played (a later shortfall is drift).
        """
        self._floor = max(0.0, excess)
        self._landing = _START_S
        self._start = _START_S if build else 0.0
        self._accepting, self._tolerant = False, not build
        self._landed_at = None  # the target in force at the next update
        self._in_band = 0.0
        self._jump_age = self._waited = 0.0  # a jump: the next one a minute on at the earliest

    def source_stepped(self) -> None:
        """The source's edge stepped after a stall: what is short of the target now is the
        source's, not a landing's pick (a Go live during a stall that went on after it left
        the lag 7 s short on a public sparse-PCR channel). It is steered as ever."""
        self._tolerant = False

    def landed_further(self, excess: float) -> None:
        """A backlog step right after a start took the lag `excess` above the target: within
        the landing's window it is part of the landing (raw TS whose backlog came after the
        start was held), not drift. Nothing else of the start changes."""
        if self._landing > 0:
            self._floor = max(self._floor, excess)

    def stop(self) -> None:
        """Stay-live is no longer consulted (the input ended, or the stream turned out a
        clip): 1x, no nudge left and nothing owed, so nothing shows a catch-up."""
        self._rate = 1.0
        self._nudge = None
        self._owed = False
        self.absorb()

    def trouble(self) -> None:
        """VLC ran out of room: a starvation, or a stall, that ran out the cushion (Auto may have
        raised the target for it). One slow-down to the target is owed, at `_SLOW`, once VLC
        played `_OWED_HOLD_S` short of the target, beyond the budget (`_pay`): a stall far
        behind it changes nothing, a catch-up included."""
        self._owed = True
        self._short_for = 0.0

    def user_paused(self) -> None:
        """A pause leaves LIVE, and a catch-up too: it resumes time-shifted at 1x."""
        self.mode = Mode.SHIFTED
        self._user_rate = 1.0
        self._nudge = None
        self._owed = False
        self._uncharged = None
        self._tolerant = False

    def user_seeked_back(self) -> None:
        self._leave_live()

    def user_go_live(self, *, keep_landing: bool = False) -> None:
        """Back to LIVE at 1x. A Go live that opens a stream says where it landed next
        (`landed()`); `keep_landing`: it opened nothing (merged into stay-live's reopen a
        moment before), so that stream's landing stays."""
        self.mode = Mode.LIVE
        self._rate = 1.0
        self._user_rate = 1.0
        self._reopened = False
        self._nudge = None
        self._owed = False  # the stream opens afresh at the target: nothing is owed
        self._uncharged = None
        if not keep_landing:
            self._floor = self._start = self._landing = 0.0  # its own landing follows
            self._accepting = self._tolerant = False
        self.absorb()

    def user_speed(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if self.mode is Mode.LIVE:
            return
        self._user_rate = rate
        self.mode = Mode.CATCHING_UP if rate > 1 else Mode.SHIFTED

    def update(self, lag_s: float, buffering: bool, *, now: float) -> Decision:
        """Decide the rate for the mean lag at `now` (the caller's clock, in seconds).

        ``reopen_live`` is edge-triggered: once True is returned, later updates return
        False (rate 1.0) until the caller reopens and calls ``user_go_live()``.

        In LIVE the rate stays as it is while buffering (the time outside the band counts
        afresh after), and is 1x after ``lock_rate()``, but for the reopen. Otherwise see the
        module docstring. Time-shifted or at a speed the user chose: the user's rate; a
        catch-up settles at the target, then LIVE at 1x.
        """
        dt = 0.0 if self._last is None else min(max(now - self._last, 0.0), _MAX_DT)
        self._last = now
        self._jump_age += dt
        if self.mode is Mode.SHIFTED:
            return Decision(self._user_rate)
        if self.mode is Mode.CATCHING_UP:
            if not buffering and lag_s <= self.target + _SETTLE:
                self.user_go_live()
                return Decision(1.0)
            return Decision(self._user_rate)
        if buffering:
            # What the wait adds is a step, not a drift; but a VLC that runs dry again and
            # again is the urgent case itself: its count only pauses.
            if not self._urgent:
                self.absorb()
            return Decision(self._rate)
        self._floor = min(self._floor, max(0.0, lag_s - self._centre(1)))
        self._start = max(0.0, self._start - dt)
        self._landing = max(0.0, self._landing - dt)
        if self._owed:  # VLC ran dry: the owed slow-down builds a start's shortfall, all of it
            self._start, self._accepting, self._tolerant = 0.0, False, False
            if self._nudge is not None:
                self._nudge.limit = None
        if lag_s > self.target + _REOPEN + self._landed():
            if self.hold_reopen:
                return Decision(self._rate)  # the stream just opened: its lag comes first
            self._rate, self._nudge = 1.0, None
            if self._reopened:
                return Decision(1.0)
            self._reopened = True
            return Decision(1.0, reopen_live=True)
        if self._landed_at is None:
            # The target a landing just now aimed at, once the source sent (a stalled first
            # tick has no burst classified yet: Normal's 2.5 s made an HLS 7 s target a rise).
            self._landed_at = self.target
        self._own_shortfall(lag_s, dt)
        if self._reopened:
            return Decision(1.0)  # asked for: the caller reopens (and calls user_go_live)
        junction = self._junction(lag_s, now)
        if self.rate_locked or self.background:
            # 1x: set by lock_rate() (never changed after) or by set_background() (a tile
            # without the focus); far behind, a jump changes no rate (the channel stay-live
            # cannot nudge needs it most), unless the next restart takes it.
            if not self.junctions and self._far_locked(dt, lag_s):
                return self._jump(now)
            return Decision(self._rate, junction=junction)
        if self._wide and self._budget_free(now):
            self._wide = 0.0
        if self._owed:
            self._pay(lag_s, dt)
        nudge = self._nudge
        if self.junctions and nudge is not None and nudge.peak > 1.0 and not nudge.ending:
            nudge.ending = True  # a catch-up gives way to the next restart
            nudge.cut = nudge.cut or not nudge.spent
        if self._steer(dt, lag_s, now) if self._nudge is not None else self._watch(dt, lag_s, now):
            return self._jump(now)
        return Decision(self._rate, junction=junction)

    def _junction(self, lag: float, now: float) -> float:
        """With `junctions`: what the next stream restart is to take back, the lag behind the
        target (as high as the burst reported now would set it, and the highest of the last
        `_JUNCTION_REF_S` at least) when that is more than `_JUNCTION_MIN_S`; else 0.0. Never
        while a stream just opened (its lag comes first). Seen live: the burst lowered the target
        3.2 s in a lull between restarts, a cut taken from it left 9 s, the provider's next
        silence was 9.4 s, and VLC ran dry; a cut is for good, the lag is not seen."""
        block = int(now // _JUNCTION_REF_S)
        if block != self._ref_block:
            follows = block == self._ref_block + 1
            self._ref_before, self._ref_now = (self._ref_now if follows else 0.0), 0.0
            self._ref_block = block
        self._ref_now = max(self._ref_now, self._centre(1))
        if not self.junctions or self.hold_reopen:
            return 0.0
        behind = lag - max(self._ref_now, self._ref_before)
        return behind if behind > _JUNCTION_MIN_S else 0.0

    def _own_shortfall(self, lag: float, dt: float) -> None:
        """Only a landing's own shortfall is built at the start or tolerated (for jumps,
        then for the tune's build too): once the target rose above the one it landed at (a
        higher latency, a larger burst, Auto, a learned target) the start's build window, what
        its build accepted and a jump's tolerance end, so the rise is built once as without
        the landing; once the lag is in the band after the stream played (right after an
        open it reads where the pick aimed) what was accepted or tolerated ends, so a later
        shortfall is drift. A rise is one beyond the band (a smaller one, the learned target
        creeping, is drift inside it); back in the band takes `_BACK_IN_S` there (a
        quarter second of the estimate's wander gave a second full build)."""
        landed_at = self._landed_at
        if landed_at is not None and self.target - landed_at > self.dead_band() + 1e-9:
            self._start = 0.0
            self._accepting = self._tolerant = False
        elif not self.hold_reopen and lag - self._centre(-1) >= -self.band(-1):
            self._in_band += dt
            if self._in_band >= _BACK_IN_S - 1e-9:
                self._accepting = self._tolerant = False
        else:
            self._in_band = 0.0

    def dead_band(self) -> float:
        """The band as wide as the estimate's wander, not as the budget widened it."""
        return max(_BAND_S, _BAND_SPREAD * self.spread)

    def _jump(self, now: float) -> Decision:
        """Reopen at live instead (`_far_move`): a catch-up it replaces ends here, and a
        gentle nudge it cut counts against the budget (it ran)."""
        nudge = self._nudge
        if nudge is not None and nudge.peak in (_GENTLE_UP, _GENTLE_DOWN) and nudge.limit is None:
            self._nudged_at = now
        self._rate, self._nudge = 1.0, None
        self._reopened = True
        return Decision(1.0, reopen_live=True, jump=True)

    def _far_locked(self, dt: float, lag: float) -> bool:
        """At a locked 1x: far behind for `_URGENT_S`, a jump that helps (`_far_move`)."""
        if lag - self._centre(1) <= _FAR_S + self._landed():
            self.absorb()
            return False
        self._side, self._urgent = 1, True
        self._outside += dt
        return self._outside >= _URGENT_S - 1e-9 and self._far_move(lag, dt) == "jump"

    def _landed(self) -> float:
        """What far behind and the reopen at live count from: the landing above the target,
        while its window lasts (a backlog later is drift)."""
        return self._floor if self._landing > 0 else 0.0

    def _budget_free(self, now: float) -> bool:
        return self._nudged_at is None or now - self._nudged_at >= _BUDGET_S - 1e-9

    def _pay(self, lag: float, dt: float) -> None:
        """The slow-down owed after trouble, once the lag was short of the target for
        `_OWED_HOLD_S`: a catch-up ends first (and does not count against the budget if the
        slow-down follows), any nudge ramps out first. A lag back near the target drops it,
        and a catch-up it cut then counts again: it ran, and nothing followed."""
        nudge = self._nudge
        deficit = self.target - lag
        if deficit <= _OWED_MIN_S:
            self._owed = False  # room enough: far behind, or the source caught up, say
            if nudge is not None and nudge.cut:
                nudge.cut = False  # still ramping out, and no slow-down follows
            if self._uncharged is not None:  # it ramped out already: it counts from its end
                self._nudged_at = max(self._nudged_at or self._uncharged, self._uncharged)
                self._uncharged = None
            return
        self._short_for += dt
        if self._short_for < _OWED_HOLD_S - 1e-9:
            return  # one unsettled tick is no deficit
        if nudge is not None and nudge.peak > 1.0:
            # Catching up with too little room: stop. One that ran its size already counts.
            nudge.cut = nudge.cut or not nudge.spent
            nudge.ending = True
            return  # the slow-down follows its ramp out
        if nudge is not None and nudge.ending:
            return
        self._owed = False
        self._uncharged = None  # the slow-down follows: a catch-up it cut spent nothing
        if nudge is None:
            self._nudge = _Nudge(_SLOW, deficit, self.target)
            self.absorb()
        else:
            nudge.peak = _SLOW  # a gentle one under way: at once to the owed rate

    def _far_move(self, lag: float, dt: float) -> str:
        """Far behind: "jump" (a clean jump lands within the band or `_JUMP_CLOSER_S` nearer,
        and nothing jumped within `_JUMP_GAP_S`), "wait" (one may, but none helps yet: the
        next keyframes may bring one, for `_JUMP_WAIT_S` counted once no stream just opened;
        one that did is waited for, the wait not counting meanwhile), "gentle" (no jump does
        better than the lag less `_GRAIN_S`: that lag is the keyframes' grain) or "fast"
        (1.10x)."""
        landing = self.jump_lag() if self.jump_lag is not None else None
        if landing is not None and self._jump_age >= _JUMP_GAP_S - 1e-9:
            if self.hold_reopen:
                return "wait"
            if self._helps(landing, lag):
                return "jump"
            if self._waited < _JUMP_WAIT_S - 1e-9:
                self._waited += dt
                return "wait"
        if landing is not None and landing >= lag - _GRAIN_S:
            return "gentle"
        return "fast"

    def _helps(self, landing: float, lag: float) -> bool:
        """A jump landing at the mean lag `landing` helps: within the band around the target
        (as wide as the estimate's wander, not as the budget widened it), or at least
        `_JUMP_CLOSER_S` nearer than `lag` without falling short of the band."""
        band = self.dead_band()
        over = landing - self._centre(1)
        if over < -band:
            return False  # short of room: VLC would run dry
        return over <= band + _BAND_BURST * self.burst or landing <= lag - _JUMP_CLOSER_S

    def _short(self, lag: float) -> bool:
        """Under `_ROOM_S` left when a burst lands (a steady source: at all): VLC runs dry."""
        return lag - self.burst / 2 < _ROOM_S

    def _watch(self, dt: float, lag: float, now: float) -> bool:
        """At 1x: count the time outside the band; start a nudge once it lasted. True: far
        behind, a jump to live instead (`_far_move`)."""
        behind, ahead = lag - self._centre(1), lag - self._centre(-1)
        far = behind > _FAR_S + self._landed()  # a landing above the target is no drift
        short = ahead < -_BAND_S and self._short(lag) and not self._accepting
        tolerated = self._accepting or self._tolerant  # a start's rest, a jump's pick
        under = short or (ahead < -self.band(-1) and not tolerated)
        side = 1 if far or behind > self.band(1) else -1 if under else 0
        if side > 0 and self.junctions:
            self.absorb()  # the next restart takes it (`Decision.junction`): no rate, no jump
            return False
        error = behind if side > 0 else ahead
        if not far:
            self._waited = 0.0
        if side == 0:
            self._outside = max(0.0, self._outside - 2 * dt)  # back inside: it fades
            return False
        if side != self._side:
            self._outside, self._side = 0.0, side
        self._outside += dt
        start = side < 0 and self._start > 0  # the start's shortfall: built once, gently
        urgent = far or short or start
        self._urgent = urgent
        if self._outside < (_URGENT_S if urgent else _PERSIST_S) - 1e-9:
            return False
        grain = False
        if far:
            move = self._far_move(lag, dt)
            if move in ("jump", "wait"):
                return move == "jump"
            if move == "gentle":
                # The keyframes' grain: a lasting lag like any other (behind: nothing short).
                far = urgent = self._urgent = False
                grain = True
                if self._outside < _PERSIST_S - 1e-9:
                    return False
        if not urgent and not self._budget_free(now):
            # Spent: tolerate this much rather than a second nudge within the budget. Not the
            # keyframes' grain: that lag is no drift, and a band that wide (both sides) would
            # tolerate a shortfall as large; the band stays as it was.
            if not grain:
                self._wide = abs(error) + _WIDEN_S
            self.absorb()
            return False
        if start:
            self._start = 0.0  # one build per landing
            nudge = _Nudge(_GENTLE_DOWN, abs(error), self._centre(side), limit=_START_BUILD_S)
        else:
            peak = _FAST if far else _SLOW if short else _GENTLE_UP if side > 0 else _GENTLE_DOWN
            nudge = _Nudge(peak, abs(error), self._centre(side))
        self._nudge = nudge
        self.absorb()
        self._steer(0.0, lag, now)
        return False

    def _steer(self, dt: float, lag: float, now: float) -> bool:
        """Ramp the nudge in, hold it until its size is taken, ramp it out. True: far behind,
        a jump to live instead (`_far_move`), which ends the nudge."""
        nudge = self._nudge
        assert nudge is not None
        rate = self._rate
        sign = 1.0 if nudge.peak > 1.0 else -1.0
        nudge.left -= sign * (rate - 1.0) * dt  # what the last update's rate took
        if nudge.limit is not None and rate != 1.0:
            nudge.limit -= dt
        centre = self._centre(sign)
        if centre != nudge.target:  # Auto moved it, or the burst: resized to match
            nudge.left -= sign * (centre - nudge.target)
            nudge.target = centre
        error = lag - centre
        far = not nudge.ending and sign > 0 and error > _FAR_S + self._landed()
        if not far:
            self._waited = 0.0
        # A catch-up under way asks only once a jump could follow (where it would land is
        # worked out from the whole buffer).
        asks = nudge.peak != _FAST or self._jump_age >= _JUMP_GAP_S - 1e-9
        move = self._far_move(lag, dt) if far and asks else ""
        if move == "jump":
            return True  # a catch-up under way too, once nothing jumped for a minute
        if move == "fast" and nudge.peak == _GENTLE_UP:
            nudge.peak = _FAST  # far behind after all (a step)
        elif (
            not nudge.ending
            and nudge.peak == _GENTLE_DOWN
            and nudge.limit is None  # a start's build stays gentle
            and self._short(lag)
        ):
            nudge.peak = _SLOW  # short of room after all (the burst grew)
        reached = sign * error <= 0.0  # at the target already (a step, say)
        # A gentle nudge ends once its size is taken: the estimate's own wander is no reason
        # to go on. An urgent one goes on to the target: it may have been sized on a lag that
        # was still settling, or the lag stepped (a backlog) or the target moved since, and
        # stopping it short would only start the next one. A start's build ends in time to
        # ramp out within its limit.
        urgent = nudge.peak in (_FAST, _SLOW)
        spent = not urgent and nudge.left <= self._ramp_out(rate, nudge.step)
        k_now = round(abs(rate - 1.0) / nudge.step)
        timed = nudge.limit is not None and nudge.limit <= k_now * _RAMP_EVERY_S + 1e-9
        nudge.spent = nudge.spent or (spent and not nudge.ending)
        nudge.ending = nudge.ending or spent or reached or timed
        due = nudge.stepped is None or now - nudge.stepped >= _RAMP_EVERY_S - 1e-9
        goal = 1.0 if nudge.ending else nudge.peak
        if abs(goal - rate) < 1e-9 or not due:
            return False
        # Steps on the nudge's grid, 1x to its peak; off the grid (a gentle nudge turned
        # urgent raised its peak) one step goes straight to the peak.
        k = abs(rate - 1.0) / nudge.step  # steps from 1x (rates are rounded: nearly whole)
        on_grid = abs(k - round(k)) < 0.05
        if nudge.ending:
            k = max(0.0, round(k) - 1.0 if on_grid else float(math.floor(k)))
        else:
            k = min(float(_RAMP_STEPS), round(k) + 1.0) if on_grid else float(_RAMP_STEPS)
        self._rate = round(1.0 + sign * k * nudge.step, 4)
        nudge.stepped = now
        if nudge.ending and self._rate == 1.0:
            if nudge.limit is not None:  # the start's build ran (outside the budget) ...
                self._accepting = True  # ... and the rest is accepted
            elif nudge.peak in (_GENTLE_UP, _GENTLE_DOWN):
                if nudge.cut:
                    self._uncharged = now  # counts unless the owed slow-down follows
                else:
                    self._nudged_at = now  # the budget counts the gentle ones that ran
            self._nudge = None
            self.absorb()
        return False

    @staticmethod
    def _ramp_out(rate: float, step: float) -> float:
        """The lag the ramp back to 1x from `rate` still takes: each step held a ramp step."""
        taken, rate_left = 0.0, abs(rate - 1.0) - step
        while rate_left > 1e-9:
            taken += rate_left * _RAMP_EVERY_S
            rate_left -= step
        return taken

    def _leave_live(self) -> None:
        if self.mode is Mode.LIVE:
            self.mode = Mode.SHIFTED
            self._user_rate = 1.0
            self._nudge = None
            self._owed = False
            self._uncharged = None
