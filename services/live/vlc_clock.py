"""VLC's play position from its get_time() readings (2.1 stage 2: an honest clock).

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
(VLC played more than the clock counted: it skipped what came too late, say). It learns the steps' typical size and interval online (the median
of the last `_LEARN_N`; the cadence stays the same channel's across opens), so a 2 s-step and
a quarter-second-step channel both work without a setting:

- VLC's clock may stand still while it reports Playing for its own step interval and more:
  only past `hold()` (1.5 intervals plus a quarter second, at least `FROZEN_S`) is it starved
  (`frozen()`), and the software clock stands still from there too (before any interval was
  learned it runs on: VLC may be playing). It never runs more than `_AHEAD_STEPS` learned
  steps past VLC's last reading either (VLC cannot be further on without its clock
  stepping), nor past `limit`, the media there is.
- A reading that disagrees by much more than a step (`_JUMP_STEPS` steps, at least
  `_JUMP_MIN_S`: ahead, or behind with the reading gone backwards) is held until the next one:
  if that one agrees with it, VLC really skipped ahead (the clock takes it at once) or
  restarted its clock (the clock goes on from where it is, reading VLC's clock from there);
  if not, it was an outlier and is dropped. A skip past `limit` (the buffered media: VLC
  cannot have played beyond it) is the stream's timestamps jumping, not VLC: the clock goes
  on as for a restart. Only readings taken as they came teach the steps' size.
- `position` never runs backwards: when VLC is behind the clock, the clock waits for it.

It also keeps VLC's own progress for rate health (counters since the open: `played` wall
seconds while VLC plays, `asked` media seconds the rates asked for meanwhile, and `progress`
VLC's clock's advance in its steps; `at_change` holds them at the last step), so a check of
VLC's speed compares its steps, never the software clock, which runs at the rate by design.
A held reading counts once it is settled; a skip or a restart counts as the rate asked (it
says nothing about VLC's speed).

Pure logic: the caller feeds the time, the reading, the state and the rate.
"""

from __future__ import annotations

from collections import deque
import math
from statistics import median
from typing import NamedTuple, Optional

FROZEN_S = 0.5  # VLC's clock standing still this long while Playing is starved, at least
_GAIN = 0.5  # of the clock's error that each new reading corrects
_LEARN_N = 16  # the steps' size and interval are the median of this many
_HOLD_INTERVALS = 1.5  # VLC's clock is starved after standing still this many intervals ...
_HOLD_PAD_S = 0.25  # ... and this long
_AHEAD_STEPS = 1.5  # the clock runs at most this many steps past VLC's last reading
_JUMP_STEPS = 1.5  # a reading further than this many steps from the clock is a jump ...
_JUMP_MIN_S = 1.0  # ... and further than this
_LIMIT_SLACK_S = 1.0  # a reading this far past the buffered media is the stream's clock


class ClockMark(NamedTuple):
    """VLC's counters at one moment (see the module docstring)."""

    played: float
    asked: float
    progress: float


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
        self._estimate = 0.0  # the filter's position (the output waits for it if behind)
        self._wall = now
        self._ms = 0  # the last reading seen
        self._good_ms = 0  # the last one taken (a held one is not, yet)
        self._good_asked = 0.0  # `asked` then
        self._offset = 0.0  # reading + offset = position (moved when VLC's clock restarts)
        self._still = 0.0  # seconds VLC played since its reading last changed
        self._held: Optional[float] = None  # the error of a reading held for the next one

    # ---- learned -----------------------------------------------------------------------
    def step(self) -> Optional[float]:
        """The typical step of VLC's reading (s); None before any."""
        return median(self._steps) if self._steps else None

    def interval(self) -> Optional[float]:
        """The typical time between two steps (s of playing); None before any."""
        return median(self._intervals) if self._intervals else None

    def hold(self) -> float:
        """How long VLC's clock may stand still while it plays before it counts as starved."""
        interval = self.interval()
        if interval is None:
            return FROZEN_S
        return max(FROZEN_S, _HOLD_INTERVALS * interval + _HOLD_PAD_S)

    def frozen(self) -> bool:
        """VLC's clock stood still past `hold()` while it played (starved)."""
        return self._still >= self.hold() - 1e-9

    def mark(self) -> ClockMark:
        """The counters now."""
        return ClockMark(self.played, self.asked, self.progress)

    # ---- update ------------------------------------------------------------------------
    def update(
        self, now: float, ms: int, playing: bool, rate: float = 1.0, *, limit: float = math.inf
    ) -> float:
        """VLC's reading `ms` at `now`, whether it plays, at `rate`; `limit`: the most media
        it can have played since the open (the live edge). Returns the position."""
        dt = max(0.0, now - self._wall)
        self._wall = now
        if playing:
            hold = self.hold() if self._intervals else math.inf  # unknown: it may be playing
            before = self._still
            self._still += dt
            self.played += dt
            self.asked += rate * dt
            running = max(0.0, min(self._still, hold) - min(before, hold))
            cap = min(self._cap(), limit)  # nor past the media there is
            self._estimate = max(self._estimate, min(self._estimate + rate * running, cap))
            if ms != self._ms:
                self._read(ms, limit, stale=rate * dt)
        self.position = max(self.position, self._estimate)
        return self.position

    def _cap(self) -> float:
        """The furthest the software clock may run: a few steps past VLC's last reading."""
        step = self.step()
        if step is None:
            return math.inf
        return self._good_ms / 1000.0 + self._offset + _AHEAD_STEPS * step

    def _read(self, ms: int, limit: float, *, stale: float) -> None:
        """A new reading: follow it, hold it, or take a jump; learn the cadence from it.
        `stale`: how far behind VLC a reading that changed since the last update can be."""
        if self.changes or self._held is not None:  # not the first: the open is no step
            if self._still > 0:
                self._intervals.append(self._still)
        self._still = 0.0
        self._ms = ms
        reading = ms / 1000.0 + self._offset
        error = reading - self._estimate
        jump = max(_JUMP_MIN_S, _JUMP_STEPS * (self.step() or 0.0))
        backwards = ms < self._good_ms
        if error > jump or (error < -jump and backwards):
            held, self._held = self._held, error
            if held is None or held * error <= 0:
                return  # held until the next reading says whether it was an outlier
            self._held = None
            if error > 0 and reading <= limit + _LIMIT_SLACK_S:
                self._estimate = reading  # VLC really skipped ahead
            else:
                self._offset -= error  # VLC's clock restarted (or the stream's jumped)
            self._settle(ms, step=False)
            return
        if self._held is None and self.changes and ms > self._good_ms:
            self._steps.append((ms - self._good_ms) / 1000.0)  # a step like the others
        self._held = None
        # A reading is at most `stale` behind VLC: beyond that, ahead of the clock means VLC
        # played more than the clock counted (it skipped what came too late, say): at once.
        beyond = max(0.0, error - stale)
        self._estimate += beyond + _GAIN * (error - beyond)
        self._settle(ms, step=True)

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
