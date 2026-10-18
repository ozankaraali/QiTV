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

A stream opened at the start Go live picks (2.1 stage 3: a tune, Go live, a reopen at live;
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

On some channels VLC cannot play at any rate but 1x: its position crawls instead (measured in
2.0.2: 4.5 s of media in 37 s). `RateHealth` notices that from VLC's progress, and the caller
then calls `lock_rate()`: from there on stay-live keeps 1x and only reopens at live when the
lag is beyond the target plus 20 s, as before.

"Auto" latency (`AUTO`, `AutoTarget`) learns what each channel adds to the target in force: it
starts at nothing, rises with each starvation or stall and falls again while the channel plays
without one. A target raised above the lag is reached without a jump back: by a nudge below 1x
unless the rate is locked, else as VLC waits at the live edge, which adds to the lag by itself.

Pure logic; the caller feeds the measured lag and applies the returned Decision.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import math
from typing import Optional

TARGETS = {"low": 1.0, "normal": 2.5, "stable": 6.0}
AUTO = "auto"  # the latency setting that learns each channel's target (AutoTarget)
LATENCIES = (AUTO, *TARGETS)

_SETTLE = 0.2  # a chosen speed catching up ends this close to the target
_REOPEN = 20.0
_HEALTH_WINDOW_S = 3.0  # rate health: VLC's progress is judged over this much counted time
_HEALTH_SHARE = 0.7  # under this share of rate x wall time, VLC cannot play at that rate
_HEALTH_PIECES = 2  # a window holds at least this many stretches (each up to a step of VLC's)
_AUTO_UP = 1.5  # Auto: a starvation or a stall raises the channel's target this much
_AUTO_DOWN = 0.5  # and each _AUTO_QUIET_S without one lowers it this much
_AUTO_QUIET_S = 300.0
_AUTO_MAX_S = 60.0  # a remembered extra beyond this is not one Auto set: start afresh

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


class RateHealth:
    """Whether VLC plays at the rates stay-live sets: its progress against rate x wall time.

    The caller adds only time that says something about the rate: at a rate other than 1x,
    with VLC playing and media ahead of it (a VLC waiting at the live edge is starved, not
    slow), in stretches that end at a step of VLC's clock (2.1 stage 2). Each window of
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


class AutoTarget:
    """One channel's Auto latency (2.0.2): what it adds to the target in force.

    The target in force is Normal's, or the source's burst when that is larger, as under any
    latency; Auto adds `extra` on top: 0 at first, or where the channel's last tune left it.
    Each starvation of VLC at the live edge, and each stall of the source, adds `_AUTO_UP`,
    so the target in force moves at once (on long HLS segments too), up to max(Stable's +
    4 s, burst + 2 s) in all; every `_AUTO_QUIET_S` of playing without one takes
    `_AUTO_DOWN` off, down to 0. The caller says what counts.
    """

    def __init__(self, extra: Optional[float] = None) -> None:
        self.extra = 0.0
        if extra is not None and math.isfinite(extra) and 0.0 < extra <= _AUTO_MAX_S:
            self.extra = extra
        self._quiet = 0.0

    @staticmethod
    def _base(burst: float) -> float:
        return max(TARGETS["normal"], burst)

    @staticmethod
    def _cap(burst: float) -> float:
        return max(TARGETS["stable"] + 4.0, burst + 2.0)

    def target(self, burst: float) -> float:
        """The target in force for a source with this burst (0.0: steady), with the extra."""
        base = self._base(burst)
        return max(base, min(base + self.extra, self._cap(burst)))

    @property
    def adjusted(self) -> bool:
        """The channel needed more than the target in force."""
        return self.extra > 0.0

    def trouble(self, burst: float) -> bool:
        """A starvation or a stall: 1.5 s more, up to the cap; True when the extra changed."""
        self._quiet = 0.0
        room = self._cap(burst) - self._base(burst)  # the most Auto may add on this source
        raised = max(self.extra, min(self.extra + _AUTO_UP, room))  # never lowered here
        changed, self.extra = raised != self.extra, raised
        return changed

    def quiet(self, seconds: float) -> bool:
        """`seconds` more of playing without trouble; True when the extra was lowered."""
        self._quiet += seconds
        if self._quiet < _AUTO_QUIET_S - 1e-9:
            return False
        self._quiet = 0.0
        lowered = max(0.0, self.extra - _AUTO_DOWN)
        changed, self.extra = lowered != self.extra, lowered
        return changed


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

    def reset(self) -> None:
        """Forget the edges (the burst stays: it is the source's)."""
        self._samples.clear()
        self._readings.clear()
        self._sum = self._weight = 0.0
        self._last = self._first = self._level = None
        self._spread = 0.0

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
        if self._level is not None and abs(level - self._level) > max(_STEP_S, burst):
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
        if self._weight <= 0:
            return None
        return self.level() + now - position

    def spread(self) -> float:
        return self._spread

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
        self._rate = 1.0  # last rate decided in LIVE
        self._user_rate = 1.0
        self._reopened = False
        self.rate_locked = False  # VLC cannot play this channel at another rate: 1x for good
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
        beyond the target plus 20 s. A speed the user chooses still applies.
        """
        self.rate_locked = True
        self._rate = 1.0
        self._nudge = None

    def set_latency(self, name: str) -> None:
        self.target = start_target(name)

    def absorb(self) -> None:
        """The lag stepped (a reopen, say): count the time outside the band afresh."""
        self._outside, self._side, self._urgent = 0.0, 0, False

    def landed(self, excess: float) -> None:
        """A stream opened at the start Go live picks (after `user_go_live()` for a Go live):
        its mean lag lands `excess` seconds above the target (below it when negative).

        Above it, for `_START_S` of playing, far behind (1.10x) and the reopen at live count
        from there, following the lag down: the landing a keyframe interval allowed is taken
        off by a gentle nudge. Short of it, or should the target rise within `_START_S` of
        playing, the room is built once at `_GENTLE_DOWN` for at most `_START_BUILD_S`, then
        the rest is accepted.
        """
        self._floor = max(0.0, excess)
        self._start = self._landing = _START_S
        self._accepting = False

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
            self._accepting = False
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
            self._start, self._accepting = 0.0, False
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
        if self.rate_locked:
            return Decision(self._rate)  # 1x: set by lock_rate(), never changed after
        if self._wide and self._budget_free(now):
            self._wide = 0.0
        if self._owed:
            self._pay(lag_s, dt)
        if self._nudge is not None:
            self._steer(dt, lag_s, now)
        else:
            self._watch(dt, lag_s, now)
        return Decision(self._rate)

    def _landed(self) -> float:
        """What far behind and the reopen at live count from: the landing above the target,
        while its window lasts (review R32: a backlog later is drift)."""
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

    def _short(self, lag: float) -> bool:
        """Under `_ROOM_S` left when a burst lands (a steady source: at all): VLC runs dry."""
        return lag - self.burst / 2 < _ROOM_S

    def _watch(self, dt: float, lag: float, now: float) -> None:
        """At 1x: count the time outside the band; start a nudge once it lasted."""
        behind, ahead = lag - self._centre(1), lag - self._centre(-1)
        if self._accepting and ahead >= -self.band(-1):
            self._accepting = False  # back at the target: a later shortfall is a real one
        far = behind > _FAR_S + self._landed()  # a landing above the target is no drift
        short = ahead < -_BAND_S and self._short(lag) and not self._accepting
        under = short or (ahead < -self.band(-1) and not self._accepting)
        side = 1 if far or behind > self.band(1) else -1 if under else 0
        error = behind if side > 0 else ahead
        if side == 0:
            self._outside = max(0.0, self._outside - 2 * dt)  # back inside: it fades
            return
        if side != self._side:
            self._outside, self._side = 0.0, side
        self._outside += dt
        start = side < 0 and self._start > 0  # the start's shortfall: built once, gently
        urgent = far or short or start
        self._urgent = urgent
        if self._outside < (_URGENT_S if urgent else _PERSIST_S) - 1e-9:
            return
        if not urgent and not self._budget_free(now):
            # Spent: tolerate this much rather than a second nudge within the budget.
            self._wide = abs(error) + _WIDEN_S
            self.absorb()
            return
        if start:
            self._start = 0.0  # one build per landing
            nudge = _Nudge(_GENTLE_DOWN, abs(error), self._centre(side), limit=_START_BUILD_S)
        else:
            peak = _FAST if far else _SLOW if short else _GENTLE_UP if side > 0 else _GENTLE_DOWN
            nudge = _Nudge(peak, abs(error), self._centre(side))
        self._nudge = nudge
        self.absorb()
        self._steer(0.0, lag, now)

    def _steer(self, dt: float, lag: float, now: float) -> None:
        """Ramp the nudge in, hold it until its size is taken, ramp it out."""
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
        if not nudge.ending and nudge.peak == _GENTLE_UP and error > _FAR_S + self._landed():
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
            return
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
