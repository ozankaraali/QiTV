"""VLC's play position from its get_time() readings (2.1: an honest clock).

libVLC reports the time of a stream it plays in quanta: the stream's own timestamps as its
demuxer meets them. On a steady channel they come every 0.2-0.5 s (measured, about one tick in
ten unchanged); on a sparse-PCR channel the reading stands still and then steps by 2000 ms
every 2 s. Between two steps the reading is stale by up to a step, while VLC plays on.

`VlcClock` is a software clock that runs at the rate set while VLC plays (not while it is
paused, opening or buffering), corrected towards VLC's readings by a complementary filter: at
each change of the reading (a new step is the freshest truth) the clock moves `_GAIN` of the
way to it, so it follows a VLC that plays slower or faster than asked, and its own steady run
carries it between steps. A reading that changed since the last update lags VLC by that
update's time at most, so one further ahead of the clock than that is taken at once beyond it
(VLC played more than the clock counted: it skipped what came too late, say).

It learns the readings' cadence online: their typical step and the time between two (the
median of the last `_LEARN_N`; the median of the last `_RECENT_N` instead when those agree and
differ from it by half or more, so a cadence that changes, fine quanta and then 2 s stamps,
shows within a few readings, while a stamp or two missing now and then does not; it stays the
same channel's across opens), so a 2 s-step and a quarter-second-step channel both work
without a setting:

- The software clock runs at most `_AHEAD_STEPS` steps past where it was at VLC's last
  reading (VLC cannot be further on without its clock stepping; before any step is known, a
  second at the rate, `_AHEAD_FIRST_S`), and never past `limit`, the media there is. So it
  goes with a VLC that crawls in small steps, and an early freeze does not run it on to the
  live edge. VLC's clock may stand still while it reports Playing for a step's time at the
  rate and more: only past `hold()` (1.5 of them plus a quarter second, at least `FROZEN_S`)
  is it starved (`frozen()`).
- A reading further from the clock than `_JUMP_STEPS` quanta (the larger of its step and the
  rate times its interval, at least `_JUMP_MIN_S`: ahead, or behind with the reading gone
  backwards) is held until the next one. If that one agrees with it, the two teach the
  cadence a step, and either VLC really skipped ahead (the clock takes it at once) or the
  clock goes on from where it is, reading VLC's clock from there: VLC's clock restarted, or
  the stream's timestamps jumped. Those are a jump past `limit` (the media VLC can have
  played), and one the helper reported (`breaks`): the segmenter leaves a forward timestamp
  jump of `JUMP_S` or more out of the timeline (an HLS discontinuity, a reconnect after an
  outage) and reports where and how much; a VLC jump as large, where it was, is that one. A
  smaller step stays media on the timeline, and VLC's clock with it: it is followed. If the
  next reading does not agree, the held one was an outlier and is dropped.
- `position` never runs backwards: when VLC is behind the clock, the clock waits for it.

It also keeps VLC's own progress for rate health (counters since the open: `played` wall
seconds while VLC plays, `asked` media seconds the rates asked for meanwhile, and `progress`
VLC's clock's advance in its steps; `at_change` holds them at the last step; `still` is how
long its reading stood still), so a check of VLC's speed compares its steps, never the
software clock, which runs at the rate by design. A held reading counts once it is settled; a
skip, a restart or the stream's jump counts as the rate asked (it says nothing about VLC's
speed).

Pure logic: the caller feeds the time, the reading, the state and the rate.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
import math
from statistics import median
from typing import NamedTuple, Optional

from services.live.ts import JUMP_S

FROZEN_S = 0.5  # VLC's clock standing still this long while Playing is starved, at least
_GAIN = 0.5  # of the clock's error that each new reading corrects
_LEARN_N = 16  # the cadence is the median of this many readings ...
_RECENT_N = 3  # ... or of this many last ones, when they agree (within this) ...
_AGREE = 1.25
_CHANGED = 0.5  # ... and differ from it by more than this share: the cadence changed
_HOLD_INTERVALS = 1.5  # VLC's clock is starved after standing still this many intervals ...
_HOLD_PAD_S = 0.25  # ... and this long
_COARSE_STEP_S = 2.0  # the coarsest cadence met: a sparse-PCR channel's 2 s stamps
_AHEAD_STEPS = 1.5  # the clock runs at most this many steps past VLC's last reading ...
_AHEAD_FIRST_S = 1.0  # ... and a second at the rate before any step is known
_JUMP_STEPS = 1.5  # a reading further than this many quanta from the clock is a jump ...
_JUMP_MIN_S = 1.0  # ... and further than this
_LIMIT_SLACK_S = 1.0  # a reading this far past the buffered media is the stream's clock
_BREAK_PAD_S = 1.0  # a reported break this near (and a quantum) is where the jump was ...
_BREAK_AMOUNT_S = 0.5  # ... and it left out as much, within this (or half a quantum)

# The helper's breaks: (position since the open, seconds the timeline left out there; those
# before the open have negative positions).
Breaks = Sequence[tuple[float, float]]


class ClockMark(NamedTuple):
    """VLC's counters at one moment (see the module docstring)."""

    played: float
    asked: float
    progress: float


def _typical(values: Sequence[float]) -> Optional[float]:
    """The median of the values; the median of the last few instead when those agree with
    each other (within `_AGREE`) and differ from it by more than `_CHANGED`: the cadence
    changed (a stamp or two missing now and then is no change)."""
    if not values:
        return None
    typical = median(values)
    recent = list(values)[-_RECENT_N:]
    if len(recent) < _RECENT_N or min(recent) <= 0:
        return typical
    agree = max(recent) <= _AGREE * min(recent)
    latest = median(recent)
    if agree and abs(latest - typical) > _CHANGED * typical:
        return latest
    return typical


class VlcClock:
    def __init__(self) -> None:
        self._steps: deque[float] = deque(maxlen=_LEARN_N)
        self._intervals: deque[float] = deque(maxlen=_LEARN_N)
        self.restart(0.0)

    def restart(self, now: float) -> None:
        """A new open: the position and VLC's clock start at 0 (the opened segment's start).
        The cadence learned so far stays: it is the channel's."""
        self.position = 0.0
        self.played = self.asked = self.progress = 0.0
        self.at_change = ClockMark(0.0, 0.0, 0.0)
        self.changes = 0  # settled readings since the open
        self.still = 0.0  # seconds VLC played since its reading last changed
        self._estimate = 0.0  # the filter's position (the output waits for it if behind)
        self._wall = now
        self._ms = 0  # the last reading seen
        self._good_ms = 0  # the last one taken (a held one is not, yet)
        self._good_asked = 0.0  # `asked` then
        self._anchor = 0.0  # the clock right after VLC's last reading (the cap counts from it)
        self._rate = 1.0  # the rate at the last update
        self._offset = 0.0  # reading + offset = position (moved when VLC's clock restarts)
        self._held: Optional[tuple[float, int, float]] = None  # (error, reading, position)

    # ---- learned -----------------------------------------------------------------------
    def step(self) -> Optional[float]:
        """The typical step of VLC's reading (s); None before any."""
        return _typical(self._steps)

    def interval(self) -> Optional[float]:
        """The typical time between two steps (s of playing); None before any."""
        return _typical(self._intervals)

    def quantum(self, rate: float = 1.0) -> Optional[float]:
        """How far VLC's reading typically moves at once at `rate`: its step, or the rate
        times its interval when that is larger; None before any cadence is known."""
        step, interval = self.step(), self.interval()
        if step is None and interval is None:
            return None
        return max(step or 0.0, rate * (interval or 0.0))

    def hold(self) -> float:
        """How long VLC's clock may stand still while it plays before it counts as starved:
        1.5 times a step's time at the rate (a crawl's small steps make it short; before any
        step is known, its interval) plus a quarter second, at least `FROZEN_S`."""
        step = self.step()
        interval = step / self._rate if step is not None and self._rate > 0 else self.interval()
        if interval is None:
            return FROZEN_S
        return max(FROZEN_S, _HOLD_INTERVALS * interval + _HOLD_PAD_S)

    def changed_hold(self, rate: float = 1.0) -> float:
        """How long VLC's clock may stand still at `rate` because its cadence just turned
        coarse, before the steps it learns say so: two of the coarsest steps met
        (`_COARSE_STEP_S`: one stamp missing) and two holds' pad. Rate health waits that long
        before a still clock counts as nothing played (measured: fine quanta, then 2 s
        stamps with one of the first three missing, at 1.10x or 0.95x, locked a healthy
        channel at 1x for a day)."""
        return 2 * _COARSE_STEP_S / max(rate, 1e-6) + 2 * _HOLD_PAD_S

    def frozen(self) -> bool:
        """VLC's clock stood still past `hold()` while it played (starved)."""
        return self.still >= self.hold() - 1e-9

    def mark(self) -> ClockMark:
        """The counters now."""
        return ClockMark(self.played, self.asked, self.progress)

    # ---- update ------------------------------------------------------------------------
    def update(
        self,
        now: float,
        ms: int,
        playing: bool,
        rate: float = 1.0,
        *,
        limit: float = math.inf,
        breaks: Breaks = (),
    ) -> float:
        """VLC's reading `ms` at `now`, whether it plays, at `rate`; `limit`: the most media
        it can have played since the open (the live edge); `breaks`: the timestamp jumps the
        helper left out of its timeline (position since the open, seconds). Returns the
        position."""
        dt = max(0.0, now - self._wall)
        self._wall, self._rate = now, rate
        if playing:
            self.still += dt
            self.played += dt
            self.asked += rate * dt
            cap = min(self._cap(rate), limit)  # nor past the media there is
            self._estimate = max(self._estimate, min(self._estimate + rate * dt, cap))
            if ms != self._ms:
                self._read(ms, rate, rate * dt, limit, breaks)
        self.position = max(self.position, self._estimate)
        return self.position

    def _cap(self, rate: float) -> float:
        """The furthest the software clock may run: a little past VLC's last reading."""
        step = self.step()
        return self._anchor + (_AHEAD_FIRST_S * rate if step is None else _AHEAD_STEPS * step)

    def _read(self, ms: int, rate: float, stale: float, limit: float, breaks: Breaks) -> None:
        """A new reading: follow it, hold it, or take a jump; learn the cadence from it.
        `stale`: how far behind VLC a reading that changed since the last update can be."""
        if self.changes or self._held is not None:  # not the first: the open is no step
            if self.still > 0:
                self._intervals.append(self.still)
        self.still = 0.0
        self._ms = ms
        self._follow(ms, rate, stale, limit, breaks)
        self._anchor = self._estimate

    def _follow(self, ms: int, rate: float, stale: float, limit: float, breaks: Breaks) -> None:
        reading = ms / 1000.0 + self._offset
        error = reading - self._estimate
        quantum = self.quantum(rate) or 0.0
        jump = max(_JUMP_MIN_S, _JUMP_STEPS * quantum)
        backwards = ms < self._good_ms
        if error > jump or (error < -jump and backwards):
            held, self._held = self._held, (error, ms, self._estimate)
            if held is None or held[0] * error <= 0:
                return  # held until the next reading says whether it was an outlier
            self._held = None
            if ms > held[1]:
                self._learn_held(held[1], ms)  # two alike: steps of the cadence
            skipped = error > 0 and reading <= limit + _LIMIT_SLACK_S
            if skipped and not self._matches(breaks, held[2], held[0], quantum):
                self._estimate = reading  # VLC really skipped ahead
            else:
                self._offset -= error  # VLC's clock restarted, or the stream's jumped
            self._settle(ms, step=False)
            return
        held, self._held = self._held, None
        if held is None and self.changes and ms > self._good_ms:
            self._steps.append((ms - self._good_ms) / 1000.0)  # a step like the others
        elif held is not None and held[0] > 0 and error > 0 and ms > held[1]:
            self._learn_held(held[1], ms)  # it agrees with the one held ahead
        # A reading is at most `stale` behind VLC: beyond that, ahead of the clock means VLC
        # played more than the clock counted (it skipped what came too late, say): at once.
        # At a break the helper flagged, the stream's timestamps jumped instead.
        beyond = max(0.0, error - stale)
        if self._matches(breaks, self._estimate, beyond, quantum):
            self._offset -= beyond
            self._estimate += _GAIN * (error - beyond)
            self._settle(ms, step=False)
            return
        self._estimate += beyond + _GAIN * (error - beyond)
        self._settle(ms, step=True)

    def _learn_held(self, held_ms: int, ms: int) -> None:
        """A held reading and the next agree: both steps are the cadence's (a cadence that
        grew shows in two readings)."""
        if self.changes and held_ms > self._good_ms:
            self._steps.append((held_ms - self._good_ms) / 1000.0)
        self._steps.append((ms - held_ms) / 1000.0)

    @staticmethod
    def _matches(breaks: Breaks, position: float, jump: float, quantum: float) -> bool:
        """A jump ahead of VLC's clock by `jump` at `position` is one the helper reported: as
        large as the segmenter leaves out (`JUMP_S`), where a break was reported (within a
        quantum and `_BREAK_PAD_S`) and as large (within half a quantum or `_BREAK_AMOUNT_S`:
        a real skip or a kept step near a reported break is VLC's, not the break). A smaller
        jump the timeline kept as media, and so does VLC: it is followed."""
        alike = max(_BREAK_AMOUNT_S, quantum / 2)
        if jump < JUMP_S - alike:
            return False
        near = quantum + _BREAK_PAD_S
        return any(
            abs(where - position) <= near and abs(seconds - jump) <= alike
            for where, seconds in breaks
        )

    def _settle(self, ms: int, *, step: bool) -> None:
        """A reading taken: VLC's progress moves by its step from the last one taken (`step`),
        or by the rate asked since (a jump says nothing about VLC's speed)."""
        if step:
            self.progress += (ms - self._good_ms) / 1000.0
        else:
            self.progress += self.asked - self._good_asked
        self._good_ms, self._good_asked = ms, self.asked
        self.changes += 1
        self.at_change = self.mark()
