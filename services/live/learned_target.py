"""The learned live target (2.1, stay-live stage 4): how far behind the live edge one channel can
play without VLC running dry, learned from how its data arrives and from the trouble it had.

`ArrivalModel` watches the live edge tick by tick (`add(now, edge)`). An arrival is a tick on
which the edge moved; between two arrivals VLC plays at 1x what it holds, so the gap between
them is the media it must have in hand. Its need is the larger of two: the 95th percentile of
the gaps (the usual longest wait) plus half their interquartile range (lateness: nothing for a
source that keeps its rhythm, more the more its gaps wander, as the window may not have shown
the longest yet) plus a fixed margin; and the 95th percentile of how far the edge fell below
its own mean just before an arrival (deep where a big batch follows several small ones) plus
the same margin. A gap that is no rhythm (a stall: trouble) is left out, so is the dip before
it, and the mean starts afresh after it or a backlog landing at once. Until the tune has seen
enough arrivals the need is never lower than what the channel taught before (or than the
burst the source reports, plus the margin, if it taught nothing yet).

`StarvationPrior` is the belief about how often VLC runs dry: a Gamma posterior (starvations
counted over hours of playing) on the rate it would run dry at with no margin. Each
`RISK_STEP_S` of margin is taken to halve that risk (a lateness tail that thins
exponentially), so the margin is the buffer that brings the believed rate down to the accepted
one: `RISK_STEP_S` x log2(rate / `ACCEPTED_PER_H`), never below 0 nor above `MARGIN_MAX_S`. A
starvation with m seconds of margin in force counts as the 2^(m / `RISK_STEP_S`) it stands for
with none (the margin should have made it that much rarer), so each one adds about as much as
the first; a near-miss (VLC's headroom under `NEAR_S` when an arrival lands) counts a part of
one, the more the closer to dry, and near-misses within `NEAR_CALM_S` of playing are one dip.
Playing counts hours; the counts fade with a half-life of playing time, back to the prior.
More trouble only raises the margin, quiet only lowers it.

`learned_target()` adds the two: max(floor, need) + margin, at most today's cap (2.0.2's
`AutoTarget`) with the need in the place of the burst: max(Stable's 6 s + 4 s, need + 2 s).
The margin adds to the floor, not to a need under it: trouble at the floor raises the target
at once. `LearnedTarget` holds one channel's model and prior; `Learned` is what it remembers
(`services.live.tuning`), and a 2.0.2 `extra` reads as a prior that needed that much.

The constants: `MARGIN_S` (1 s) is what VLC needs beyond the media itself (its 0.3 s of
caching on the pipeline, about 0.5 s to open, the 0.25 s tick the edge is sampled at).
`QUANTILE` (95%) of the gaps of the last `GAP_WINDOW_S` (10 minutes, at most `MAX_GAPS`) are
the usual waits; `LATE_SHARE` (half) of their interquartile range is the lateness. `MIN_GAPS`
(3) arrivals give a first estimate; until `WARM_GAPS` (20) the earlier need holds as a floor.
A gap longer than the usual (the gaps' median) by more than `STALL_S` (2 s, stay-live's
stall bound) is trouble if the caller said the source stalled or it is also over
`OUTLIER_FACTOR` (3) usual gaps. The usual gap of the moment (the median of the last
`TYPICAL_GAPS`) sizes the rest: an arrival bringing more than max(`STEP_S`, `STEP_GAPS` usual
gaps) of media is a backlog (a step), and the dips are measured from the mean over
`DEPTH_WINDOW_GAPS` (12) usual gaps, `DEPTH_MIN_S` to `DEPTH_MAX_S` (10-90 s, as `LagFilter`),
once it held half of that and `MIN_GAPS` arrivals. The prior believes `ACCEPTED_PER_H` (one dry
run in two hours of playing) with the weight of `PRIOR_H` (an hour), so a starvation adds
log2(3) = 1.6 s (2.0.2 added 1.5 s), less once hours of playing count; `HALF_LIFE_S` (30
minutes of playing) fades it, so an hour of quiet takes most of a starvation's margin back;
a near-miss counts up to `NEAR_WEIGHT` (half) a starvation, and `NEAR_CALM_S` (5 s) keeps a
source that arrives every tick from counting one dip many times. `MARGIN_MAX_S` (7.5 s) is
the most today's cap ever allowed over Normal's 2.5 s, and the counts never hold more than it
answers. `NEED_MAX_S` (60 s, 2.0.2's bound on a remembered extra) bounds the need.

Pure logic: no clock of its own, no Qt, no I/O.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import statistics
from typing import Optional, Sequence

# Mirrored from services.live.stay_live (a test keeps them equal; no import, so either module
# may use the other).
FLOOR_S = 2.5  # Normal's target: the learned target's floor in this stage
CAP_MIN_S = 10.0  # today's cap: Stable's 6 s + 4 s ...
CAP_ABOVE_S = 2.0  # ... or the burst (here: the need) + 2 s, whichever is larger
STALL_S = 2.0  # a source silent this much longer than its rhythm has stalled

# ArrivalModel
MARGIN_S = 1.0
QUANTILE = 0.95
LATE_SHARE = 0.5
GAP_WINDOW_S = 600.0
MAX_GAPS = 1200
MIN_GAPS = 3
WARM_GAPS = 20
TYPICAL_GAPS = 25
OUTLIER_FACTOR = 3.0
STEP_S = 3.0
STEP_GAPS = 2.0
DEPTH_WINDOW_GAPS = 12.0
DEPTH_MIN_S = 10.0
DEPTH_MAX_S = 90.0
NEED_MAX_S = 60.0
_EDGE_EPS = 1e-3  # an edge that moved less than this did not move
_TICK_S = 0.25  # what a first sample stands for
_MAX_DT = 1.0  # a longer gap between two samples counts as this long

# StarvationPrior
RISK_STEP_S = 1.0
ACCEPTED_PER_H = 0.5
PRIOR_H = 1.0
PRIOR_EVENTS = ACCEPTED_PER_H * PRIOR_H  # the prior's pseudo-starvations
HALF_LIFE_S = 1800.0
NEAR_S = 0.5
NEAR_WEIGHT = 0.5
NEAR_CALM_S = 5.0
MARGIN_MAX_S = CAP_MIN_S - FLOOR_S
EVENTS_MAX = 10_000.0  # a restored state beyond these makes no sense
HOURS_MAX = 100.0


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _quantile(ordered: Sequence[float], share: float) -> float:
    """The `share` quantile of the sorted `ordered` (linear between ranks)."""
    rank = share * (len(ordered) - 1)
    low = math.floor(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (rank - low) * (ordered[high] - ordered[low])


def _trouble(gap: float, stalled: bool, usual: float) -> bool:
    """A gap that is no rhythm: longer than the usual by more than `STALL_S`, and a stall the
    caller saw or also over `OUTLIER_FACTOR` usual gaps."""
    return gap > usual + STALL_S and (stalled or gap > OUTLIER_FACTOR * usual)


def learned_target(need: float, margin: float, floor: float = FLOOR_S) -> float:
    """max(floor, need) + margin, at most max(`CAP_MIN_S`, need + `CAP_ABOVE_S`) (today's cap
    with the need for the burst), never under max(floor, need)."""
    base = max(floor, need)
    cap = max(CAP_MIN_S, need + CAP_ABOVE_S)
    return max(base, min(base + max(0.0, margin), cap))


@dataclass(frozen=True)
class Learned:
    """What one channel taught, for its next tune (small numbers; `services.live.tuning`)."""

    need: Optional[float] = None  # the arrival need (s), once a tune saw `WARM_GAPS` arrivals
    events: float = PRIOR_EVENTS  # the prior's starvations counted (with its pseudo ones) ...
    hours: float = PRIOR_H  # ... over these hours of playing (discounted by the margin)


@dataclass(frozen=True)
class _Gap:
    end: float  # when the arrival landed (wall time)
    gap: float  # seconds since the arrival before it
    dip: Optional[float]  # how far the edge was below its mean just before it (None: unknown)
    stalled: bool  # the caller said the source stalled meanwhile


class ArrivalModel:
    """How one channel's data arrives, and the buffer VLC needs for it (see the module)."""

    def __init__(self, need: Optional[float] = None) -> None:
        """`need`: what the channel needed before (`Learned.need`), or None."""
        ok = need is not None and _finite(need) and 0.0 <= need <= NEED_MAX_S
        self.prior: Optional[float] = float(need) if ok and need is not None else None
        self.seen = 0  # gaps measured this tune
        self._gaps: deque[_Gap] = deque(maxlen=MAX_GAPS)
        self._recent: deque[float] = deque(maxlen=TYPICAL_GAPS)
        self._samples: deque[tuple[float, float, float]] = deque()  # (wall, level, weight)
        self._sum = self._weight = 0.0
        self._window_from: Optional[float] = None  # when the mean started afresh
        self._window_arrivals = 0  # arrivals since
        self._edge: Optional[float] = None
        self._now: Optional[float] = None  # the last sample's time
        self._landed: Optional[float] = None  # the last arrival's time
        self._stalled = False  # the caller said the source stalled since the last arrival
        self._fresh: Optional[float] = None
        self._dirty = False

    # ---- what the caller tells ---------------------------------------------------------------
    def add(self, now: float, edge: float) -> bool:
        """This tick's wall time and live edge (media seconds); True when an arrival landed."""
        if not (_finite(now) and _finite(edge)):
            return False
        previous, last = self._edge, self._now
        if previous is None or last is None or now < last or edge < previous - _EDGE_EPS:
            self.reset()  # the first edge, or another timeline: no arrival, no gap
            self._edge, self._now = edge, now
            self._sample(now, edge - now)
            return False
        self._expire(now)
        landed = edge > previous + _EDGE_EPS
        if landed:
            self._arrive(now, previous, edge)
        self._edge, self._now = edge, now
        self._sample(now, edge - now)
        return landed

    def stall(self) -> None:
        """The source stalled: the gap in progress is trouble if it ends far beyond the rhythm,
        and the mean the dips are measured from starts afresh (it may come back anywhere)."""
        self._stalled = True
        self._restart_window()

    def reset(self) -> None:
        """A new stream or a reopen: forget the edge (the next one is no arrival); keep what was
        learned."""
        self._edge = self._now = self._landed = None
        self._stalled = False
        self._restart_window()

    # ---- what it learned ---------------------------------------------------------------------
    def need(self, burst: float = 0.0) -> float:
        """The buffer (s of mean lag) VLC needs on this channel; 0.0 when nothing is known of a
        steady source. `burst`: what the source reports (HLS target duration, 2.0.1's measured
        burst), standing in until arrivals are measured."""
        fresh = self.fresh()
        burst = float(burst) if _finite(burst) and burst > 0 else 0.0
        if self.prior is not None:
            fallback = self.prior
        else:
            fallback = burst + MARGIN_S if burst > 0 else 0.0
        if fresh is None:
            need = fallback
        elif self.seen < WARM_GAPS:
            need = max(fresh, fallback)
        else:
            need = fresh
        return min(need, NEED_MAX_S)

    def fresh(self) -> Optional[float]:
        """The need from this tune's arrivals alone; None before `MIN_GAPS` of rhythm."""
        if self._dirty:
            self._fresh, self._dirty = self._estimate(), False
        return self._fresh

    def summary(self) -> Optional[float]:
        """The need to remember: this tune's once it saw `WARM_GAPS` arrivals, else the one it
        was given (None: nothing learned)."""
        fresh = self.fresh()
        if fresh is not None and self.seen >= WARM_GAPS:
            return fresh
        return self.prior

    # ---- inside --------------------------------------------------------------------------------
    def _arrive(self, now: float, previous: float, edge: float) -> None:
        usual = statistics.median(self._recent) if self._recent else None
        self._window_arrivals += 1
        if self._landed is not None:
            gap = now - self._landed
            dip = None
            window_from = self._window_from
            held = window_from is not None and now - window_from >= self._window() / 2
            if held and self._window_arrivals > MIN_GAPS and self._weight > 0:
                dip = max(0.0, self._sum / self._weight - (previous - now))
            self._gaps.append(_Gap(now, gap, dip, self._stalled))
            self._recent.append(gap)
            self.seen += 1
            self._dirty = True
            if usual is not None and _trouble(gap, self._stalled, usual):
                self._restart_window()  # it may have come back anywhere: the mean afresh
        self._landed, self._stalled = now, False
        if edge - previous > max(STEP_S, STEP_GAPS * (usual or 0.0)):
            self._restart_window()  # a backlog at once: a step, taken in afresh

    def _expire(self, now: float) -> None:
        gaps = self._gaps
        while gaps and gaps[0].end < now - GAP_WINDOW_S:
            gaps.popleft()
            self._dirty = True

    def _window(self) -> float:
        usual = statistics.median(self._recent) if self._recent else 0.0
        return min(DEPTH_MAX_S, max(DEPTH_MIN_S, DEPTH_WINDOW_GAPS * usual))

    def _restart_window(self) -> None:
        self._samples.clear()
        self._sum = self._weight = 0.0
        self._window_from = None
        self._window_arrivals = 0

    def _sample(self, now: float, level: float) -> None:
        samples = self._samples
        dt = min(max(now - samples[-1][0], 0.0), _MAX_DT) if samples else _TICK_S
        if self._window_from is None:
            self._window_from = now
        samples.append((now, level, dt))
        self._sum += level * dt
        self._weight += dt
        window = self._window()
        while len(samples) > 1 and samples[0][0] < now - window:
            _, old, weight = samples.popleft()
            self._sum -= old * weight
            self._weight -= weight

    def _estimate(self) -> Optional[float]:
        if len(self._gaps) < MIN_GAPS:
            return None
        ordered = sorted(self._gaps, key=lambda g: g.gap)
        usual = _quantile([g.gap for g in ordered], 0.5)  # the median
        rhythm = [g for g in ordered if not _trouble(g.gap, g.stalled, usual)]
        if len(rhythm) < MIN_GAPS:
            return None
        gaps = [g.gap for g in rhythm]  # still in order
        late = _quantile(gaps, 0.75) - _quantile(gaps, 0.25)
        need = _quantile(gaps, QUANTILE) + LATE_SHARE * late + MARGIN_S
        dips = sorted(g.dip for g in rhythm if g.dip is not None)
        if len(dips) >= MIN_GAPS:
            need = max(need, _quantile(dips, QUANTILE) + MARGIN_S)
        return min(need, NEED_MAX_S)


class StarvationPrior:
    """How often VLC runs dry on one channel, believed (see the module)."""

    def __init__(self, events: Optional[float] = None, hours: Optional[float] = None) -> None:
        """A restored belief (`Learned.events`, `Learned.hours`), or the prior's."""
        self.events, self.hours = PRIOR_EVENTS, PRIOR_H
        self._calm = math.inf  # seconds played since the last near-miss counted
        if (
            events is not None
            and hours is not None
            and _finite(events)
            and _finite(hours)
            and 0.0 <= events <= EVENTS_MAX
            and 0.0 < hours <= HOURS_MAX
        ):
            self.hours = max(float(hours), PRIOR_H)
            self._count(max(float(events), PRIOR_EVENTS) - PRIOR_EVENTS)

    @classmethod
    def from_extra(cls, extra: Optional[float]) -> StarvationPrior:
        """2.0.2's remembered `extra` (what Auto added after trouble) as the belief that needed
        that margin; anything else (none, not positive, beyond `NEED_MAX_S`) as the prior."""
        if extra is None or not _finite(extra) or not 0.0 < extra <= NEED_MAX_S:
            return cls()
        margin = min(float(extra), MARGIN_MAX_S)
        return cls(PRIOR_H * ACCEPTED_PER_H * 2.0 ** (margin / RISK_STEP_S), PRIOR_H)

    def rate(self) -> float:
        """Believed starvations per hour of playing with no margin."""
        return self.events / self.hours

    def margin(self) -> float:
        """The seconds that bring the believed rate down to `ACCEPTED_PER_H` (0 to
        `MARGIN_MAX_S`)."""
        ratio = self.rate() / ACCEPTED_PER_H
        if ratio <= 1.0:
            return 0.0
        return min(MARGIN_MAX_S, RISK_STEP_S * math.log2(ratio))

    def starved(self, margin: Optional[float] = None) -> None:
        """VLC ran dry (a starvation that ran out a cushion, or a stall that did) with `margin`
        seconds of margin in force (None: all of `margin()`)."""
        self._count(self._weight(margin))

    def near_miss(self, headroom: float, margin: Optional[float] = None) -> None:
        """VLC had only `headroom` seconds left when an arrival landed: under `NEAR_S`, part of
        a starvation, up to `NEAR_WEIGHT` the closer it came; one per `NEAR_CALM_S` played."""
        if not _finite(headroom) or headroom >= NEAR_S or self._calm < NEAR_CALM_S:
            return
        self._calm = 0.0
        share = NEAR_WEIGHT * min(1.0, 1.0 - headroom / NEAR_S)
        self._count(share * self._weight(margin))

    def played(self, seconds: float) -> None:
        """`seconds` more of playing at the live edge: the counts fade towards the prior, and
        the hours count."""
        if not _finite(seconds) or seconds <= 0:
            return
        self._calm += seconds
        fade = math.log(2.0) / HALF_LIFE_S
        keep = math.exp(-fade * seconds)
        counted = (1.0 - keep) / fade  # the seconds played, each faded since
        self.events = PRIOR_EVENTS + (self.events - PRIOR_EVENTS) * keep
        self.hours = min(HOURS_MAX, PRIOR_H + (self.hours - PRIOR_H) * keep + counted / 3600.0)

    def _weight(self, margin: Optional[float]) -> float:
        """What one starvation with `margin` in force stands for with none."""
        if margin is None or not _finite(margin):
            margin = self.margin()
        return float(2.0 ** (min(max(0.0, margin), MARGIN_MAX_S) / RISK_STEP_S))

    def _count(self, events: float) -> None:
        """Add `events`, never beyond what `MARGIN_MAX_S` answers (the belief stays bounded)."""
        most = self.hours * ACCEPTED_PER_H * 2.0 ** (MARGIN_MAX_S / RISK_STEP_S)
        self.events = min(self.events + events, max(most, self.events), EVENTS_MAX)


class LearnedTarget:
    """One channel's learned target: its arrivals and its starvation prior together.

    The caller feeds `arrivals` every tick (`add`, `stall`, `reset`), and tells `starved()`,
    `near_miss()` and `played()`; `target(burst)` is the target in force. `learned()` is what
    to remember for the channel's next tune.
    """

    def __init__(
        self,
        learned: Optional[Learned] = None,
        extra: Optional[float] = None,
        *,
        floor: float = FLOOR_S,
    ) -> None:
        """`learned`: what the channel taught (`Tuning.learned`); without it, a 2.0.2 `extra`
        (`Tuning.extra`) is read as the prior."""
        self.floor = floor
        if learned is not None:
            self.arrivals = ArrivalModel(learned.need)
            self.prior = StarvationPrior(learned.events, learned.hours)
        else:
            self.arrivals = ArrivalModel()
            self.prior = StarvationPrior.from_extra(extra)

    def need(self, burst: float = 0.0) -> float:
        return self.arrivals.need(burst)

    def target(self, burst: float = 0.0) -> float:
        """The target for a source reporting `burst` (0.0: steady; it matters only until the
        arrivals are measured)."""
        return learned_target(self.need(burst), self.prior.margin(), self.floor)

    def margin(self, burst: float = 0.0) -> float:
        """The margin in force: what the target holds above max(floor, need), after the cap."""
        return self.target(burst) - max(self.floor, self.need(burst))

    def adjusted(self, burst: float = 0.0) -> bool:
        """The target is above the floor (the indicator's "· adjusted")."""
        return self.target(burst) > self.floor + 1e-9

    def starved(self, burst: float = 0.0) -> None:
        """VLC ran dry with the target for `burst` in force."""
        self.prior.starved(self.margin(burst))

    def near_miss(self, headroom: float, burst: float = 0.0) -> None:
        """VLC had only `headroom` seconds left when an arrival landed."""
        self.prior.near_miss(headroom, self.margin(burst))

    def played(self, seconds: float) -> None:
        """`seconds` more of playing at the live edge."""
        self.prior.played(seconds)

    def learned(self) -> Optional[Learned]:
        """What to remember (rounded); None when nothing was learned."""
        need = self.arrivals.summary()
        events, hours = self.prior.events, self.prior.hours
        if need is None and events == PRIOR_EVENTS and hours == PRIOR_H:
            return None
        return Learned(None if need is None else round(need, 2), round(events, 4), round(hours, 4))
