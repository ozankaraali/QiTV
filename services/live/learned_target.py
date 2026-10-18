"""The learned live target (2.1, stay-live stage 4): how far behind the live edge one channel can
play without VLC running dry, learned from how its data arrives and from the trouble it had.

`ArrivalModel` watches the live edge tick by tick (`add(now, edge)`). The edge grows while a
batch downloads (an HLS segment streams in, a raw TS burst comes over a second or more), so an
arrival is a batch: the ticks on which the edge moved, in a row. A batch ends once the edge
stood still for `BATCH_GAP_S`, or stood still a tick after bringing much of what a batch
usually brings (the next one came early); one that comes no faster than it plays (a steady
source) ends after `BATCH_SPLIT_S` and the next tick starts another, so a steady source
arrives about once a second; none lasts beyond `BATCH_MAX_S`. Between two batches' starts VLC
plays at 1x what it holds, so that gap is the media it must have in hand. The need is the
larger of two: the 95th
percentile of the gaps (the usual longest wait) plus half their interquartile range
(lateness: nothing for a source that keeps its rhythm, more the more its gaps wander, as the
window may not have shown the longest yet) plus a fixed margin; and the 95th percentile of how
far the edge fell below its own mean just before a batch (deep where a big batch follows
several small ones) plus the same margin; a source that runs off real time (one only a
synthetic test makes) would read its drift as dips, so then they are not used. A gap far
beyond the rhythm (a stall: trouble) is left out with the dip before it, and the mean starts
afresh after it, or when the edge jumps well above where the mean's window has seen it (a
backlog at once: a step). Until the tune has seen enough batches the need is never lower than
what the channel taught before (or than the burst the source reports, plus the margin, if it
taught nothing yet).

`StarvationPrior` is the belief about how often VLC runs dry: Gamma-like (starvations counted
over hours of playing, but weighed by the margin in force and tempered as below: not a strict
Gamma update), on the rate it would run dry at with no margin. Each `RISK_STEP_S` of margin
is taken to halve that risk (a lateness tail that thins exponentially), so the margin is the
buffer that brings the believed rate down to the accepted one: `RISK_STEP_S` x
log2(rate / `ACCEPTED_PER_H`). A starvation with m seconds of margin in force counts as the
2^(m / `RISK_STEP_S`) it stands for with none (the margin should have made it that much
rarer), so each one adds about as much as the first; a near-miss (VLC's
headroom under `NEAR_S` when a batch lands) counts a part of one, the more the closer to dry,
and near-misses within `NEAR_CALM_S` of playing are one dip, counted at its closest. Nothing
counted takes the margin beyond the room the cap leaves. Playing counts hours (the more, the
less one starvation says) and tempers the belief: the log of the believed rate over the
accepted one shrinks with a half-life of playing time, so the margin itself halves every
`HALF_LIFE_S` of quiet; under `MARGIN_MIN_S` it is none. More trouble only raises the margin,
quiet only lowers it.

`learned_target()` adds the two: max(floor, need) + margin, at most today's cap (2.0.2's
`AutoTarget`) with the need in the place of the burst: max(Stable's 6 s + 4 s, need + 2 s).
The margin adds to the floor, not to a need under it: trouble at the floor raises the target
at once. `LearnedTarget` holds one channel's model and prior; `Learned` is what it remembers
(`services.live.tuning`) when it says something about the next tune, and a 2.0.2 `extra`
reads as a prior that needed that much.

The constants: `MARGIN_S` (1 s) is what VLC needs beyond the media itself (its 0.3 s of
caching on the pipeline, about 0.5 s to open, the 0.25 s tick the edge is sampled at).
`BATCH_GAP_S` (0.75 s: three ticks; the helper's own burst meter waits 1 s for bytes, which on
the tick grid would merge a steady source's 1-s arrivals); `BATCH_SPLIT_S` (1 s) and
`BATCH_FAST` (1.5: a batch bringing no more than 1.5 s of media per second is no download);
`BATCH_PART` (half the median of what the last `TYPICAL_GAPS` batches brought: a download
streams on without a still tick, a steady source's next arrival comes after one);
`BATCH_MAX_S` (10 s: a 12-s segment downloading at under 2x). `QUANTILE` (95%) of the gaps of
the last `GAP_WINDOW_S` (10 minutes, at most `MAX_GAPS`) are the usual waits; `LATE_SHARE`
(half) of their interquartile range is the lateness. `MIN_GAPS` (3) batches give a first
estimate; until `WARM_GAPS` (20) the earlier need holds as a floor; it is estimated at most
once per `ESTIMATE_EVERY_S` (1 s). A gap longer than `OUTLIER_FACTOR` (3) times the gaps'
median and than it plus `STALL_S` (2 s, stay-live's stall bound) is trouble, whether or not
the caller saw a stall. The dips are measured from the mean over `DIP_WINDOW_GAPS` (24) usual
gaps (the median of the last `TYPICAL_GAPS`), `DIP_MIN_S` to `DIP_MAX_S` (20-180 s: longer
than `LagFilter`'s, so a pattern of several batches is held whole a few times; a window
holding part of one more measures from a mean that part pulls off), once it held half of that
and `MIN_GAPS` batches; an edge more than `STEP_S` (3 s, as `LagFilter`) above the highest
that window saw is a step. They are not used while the media of the gaps counted comes more
than `DRIFT_SHARE` (5%) faster or slower than real time. `NEED_MAX_S` (60 s, 2.0.2's bound on
a remembered extra) bounds the need, and a need within `INFO_NEED_S` (0.25 s) of the floor is
not worth remembering. The prior believes `ACCEPTED_PER_H` (one dry run in two hours
of playing) with the weight of `PRIOR_H` (an hour), so a first starvation adds log2(3) = 1.6 s
(2.0.2 added 1.5 s), about 1.1 s once long playing counts; `HALF_LIFE_S` (30 minutes of
playing) halves the margin, so one starvation's is gone within the hour and the cap's within
two; a near-miss counts up to `NEAR_WEIGHT` (half) a starvation, and `NEAR_CALM_S` (5 s) keeps
a source that arrives every tick from counting one dip many times; `MARGIN_MIN_S` (0.5 s, the
step the lag is shown in) is the least margin that counts; `MARGIN_MAX_S` (7.5 s) is the most
today's cap ever allowed over Normal's 2.5 s.

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
STEP_S = 3.0  # an edge this far above where the window saw it is a step
NEED_MAX_S = 60.0  # 2.0.2's bound on a remembered extra
_TICK_S = 0.25  # what a first sample stands for
_MAX_DT = 1.0  # a longer gap between two samples counts as this long

# ArrivalModel
MARGIN_S = 1.0
BATCH_GAP_S = 0.75
BATCH_SPLIT_S = 1.0
BATCH_FAST = 1.5
BATCH_PART = 0.5
BATCH_MAX_S = 10.0
QUANTILE = 0.95
LATE_SHARE = 0.5
GAP_WINDOW_S = 600.0
MAX_GAPS = 1200
MIN_GAPS = 3
WARM_GAPS = 20
ESTIMATE_EVERY_S = 1.0
TYPICAL_GAPS = 25
OUTLIER_FACTOR = 3.0
DIP_WINDOW_GAPS = 24.0
DIP_MIN_S = 20.0
DIP_MAX_S = 180.0
DRIFT_SHARE = 0.05
INFO_NEED_S = 0.25
_EDGE_EPS = 1e-3  # an edge that moved less than this did not move

# StarvationPrior
RISK_STEP_S = 1.0
ACCEPTED_PER_H = 0.5
PRIOR_H = 1.0
PRIOR_EVENTS = ACCEPTED_PER_H * PRIOR_H  # the prior's pseudo-starvations
HALF_LIFE_S = 1800.0
NEAR_S = 0.5
NEAR_WEIGHT = 0.5
NEAR_CALM_S = 5.0
MARGIN_MIN_S = 0.5
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


def _trouble(gap: float, usual: float) -> bool:
    """A gap that is no rhythm: over `OUTLIER_FACTOR` usual gaps and the usual plus `STALL_S`."""
    return gap > usual + STALL_S and gap > OUTLIER_FACTOR * usual


def learned_target(need: float, margin: float, floor: float = FLOOR_S) -> float:
    """max(floor, need) + margin, at most max(`CAP_MIN_S`, need + `CAP_ABOVE_S`) (today's cap
    with the need for the burst), never under max(floor, need)."""
    base = max(floor, need)
    return max(base, min(base + max(0.0, margin), _cap(need)))


def _cap(need: float) -> float:
    return max(CAP_MIN_S, need + CAP_ABOVE_S)


@dataclass(frozen=True)
class Learned:
    """What one channel taught, for its next tune (small numbers; `services.live.tuning`)."""

    need: Optional[float] = None  # the arrival need (s), once a tune saw `WARM_GAPS` batches
    events: float = PRIOR_EVENTS  # the prior's starvations counted (with its pseudo ones) ...
    hours: float = PRIOR_H  # ... over these hours of playing

    def informative(self, floor: float = FLOOR_S) -> bool:
        """It would change the next tune's target: a need above the floor, or a margin."""
        if self.need is not None and _finite(self.need) and self.need > floor + INFO_NEED_S:
            return True
        return StarvationPrior(self.events, self.hours).margin() > 0.0


@dataclass(frozen=True)
class _Gap:
    end: float  # when the batch began (wall time)
    gap: float  # seconds since the batch before it began
    media: float  # seconds of media that came meanwhile
    dip: Optional[float]  # how far the edge was below its mean just before it (None: unknown)


class ArrivalModel:
    """How one channel's data arrives, and the buffer VLC needs for it (see the module)."""

    def __init__(self, need: Optional[float] = None) -> None:
        """`need`: what the channel needed before (`Learned.need`), or None."""
        ok = need is not None and _finite(need) and 0.0 <= need <= NEED_MAX_S
        self.remembered: Optional[float] = float(need) if ok and need is not None else None
        self.seen = 0  # gaps measured this tune
        self.stalls = 0  # gaps the caller said the source stalled in
        self._gaps: deque[_Gap] = deque(maxlen=MAX_GAPS)
        self._recent: deque[float] = deque(maxlen=TYPICAL_GAPS)
        self._usual: Optional[float] = None  # the median of `_recent`
        self._samples: deque[tuple[float, float, float]] = deque()  # (wall, level, weight)
        self._peaks: deque[tuple[float, float]] = deque()  # (wall, level), falling: the max
        self._sum = self._weight = 0.0
        self._anchor: Optional[float] = None  # the level the sum counts from (small numbers)
        self._window_from: Optional[float] = None  # when the mean started afresh
        self._window_batches = 0  # batches since
        self._edge: Optional[float] = None
        self._now: Optional[float] = None  # the last sample's time
        self._moved: Optional[float] = None  # the last tick the edge moved on
        self._paused = False  # the edge stood still on the last tick
        self._batch: Optional[tuple[float, float, float]] = None  # (start, from, edge before)
        self._recent_media: deque[float] = deque(maxlen=TYPICAL_GAPS)
        self._usual_media: Optional[float] = None  # the median of `_recent_media`
        self._stalled = False  # the caller said the source stalled since the last batch
        self._fresh: Optional[float] = None
        self._dirty = False
        self._estimated: Optional[float] = None  # when `_fresh` was estimated

    # ---- what the caller tells ---------------------------------------------------------------
    def add(self, now: float, edge: float) -> bool:
        """This tick's wall time and live edge (media seconds); True on a batch's first tick."""
        if not (_finite(now) and _finite(edge)):
            return False
        previous, last = self._edge, self._now
        if previous is None or last is None or now < last or edge < previous - _EDGE_EPS:
            self.reset()  # the first edge, or another timeline: no batch, no gap
            self._edge, self._now = edge, now
            self._sample(now, edge - now)
            return False
        self._expire(now)
        began = False
        if edge > previous + _EDGE_EPS:
            began = self._begins(now, last, previous)
            if began:
                self._arrive(now, previous)
                self._batch = (now, last, previous)
            self._moved = now
            self._paused = False
        else:
            self._paused = True
        self._edge, self._now = edge, now
        self._sample(now, edge - now)
        return began

    def stall(self) -> None:
        """The source stalled (the caller's bound passed): noted once for the gap in progress.
        Whether that gap was trouble the model judges by itself (see `_trouble`)."""
        self._stalled = True

    def reset(self) -> None:
        """A new stream or a reopen: forget the edge (the next one is no batch); keep what was
        learned."""
        self._edge = self._now = self._moved = None
        self._batch = None
        self._paused = self._stalled = False
        self._restart_window()

    # ---- what it learned ---------------------------------------------------------------------
    def need(self, burst: float = 0.0) -> float:
        """The buffer (s of mean lag) VLC needs on this channel; 0.0 when nothing is known of a
        steady source. `burst`: what the source reports (HLS target duration, 2.0.1's measured
        burst), standing in until batches are measured."""
        fresh = self.fresh()
        burst = float(burst) if _finite(burst) and burst > 0 else 0.0
        if self.remembered is not None:
            fallback = self.remembered
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
        """The need from this tune's batches alone; None before `MIN_GAPS` of rhythm.
        Estimated again at most once per `ESTIMATE_EVERY_S` of the samples' clock."""
        due = (
            self._fresh is None
            or self._estimated is None
            or self._now is None
            or self._now - self._estimated >= ESTIMATE_EVERY_S - 1e-9
        )
        if self._dirty and due:
            self._fresh, self._dirty, self._estimated = self._estimate(), False, self._now
        return self._fresh

    def summary(self) -> Optional[float]:
        """The need to remember: this tune's once it saw `WARM_GAPS` batches, else the one it
        was given (None: nothing learned)."""
        fresh = self.fresh()
        if fresh is not None and self.seen >= WARM_GAPS:
            return fresh
        return self.remembered

    # ---- inside --------------------------------------------------------------------------------
    def _begins(self, now: float, last: float, previous: float) -> bool:
        """The edge moved on this tick: does a new batch begin?"""
        batch, moved = self._batch, self._moved
        if batch is None or moved is None or now - moved >= BATCH_GAP_S - 1e-9:
            return True  # it stood still long enough
        start, since, before = batch
        if now - start >= BATCH_MAX_S - 1e-9:
            return True
        brought, usual = previous - before, self._usual_media
        if self._paused and usual is not None and brought >= BATCH_PART * usual:
            return True  # a pause after much of what a batch brings: the next one began
        # A batch no faster than it plays is a steady source's flow: about a second each.
        slow = brought <= BATCH_FAST * (last - since) + 1e-9
        return now - start >= BATCH_SPLIT_S - 1e-9 and slow

    def _arrive(self, now: float, previous: float) -> None:
        """A batch begins at `now`; the edge stood at `previous` just before it."""
        usual = self._usual
        self._window_batches += 1
        if self._batch is not None:
            gap = now - self._batch[0]
            dip = None
            window_from = self._window_from
            held = window_from is not None and now - window_from >= self._window() / 2
            if held and self._window_batches > MIN_GAPS and self._weight > 0:
                dip = max(0.0, self._mean() - (previous - now))
            media = previous - self._batch[2]
            self._gaps.append(_Gap(now, gap, media, dip))
            self._recent.append(gap)
            self._usual = statistics.median(self._recent)
            self._recent_media.append(media)
            self._usual_media = statistics.median(self._recent_media)
            self.seen += 1
            self._dirty = True
            if self._stalled:
                self.stalls += 1
            if usual is not None and _trouble(gap, usual):
                self._restart_window()  # it may have come back anywhere: the mean afresh
        self._stalled = False

    def _expire(self, now: float) -> None:
        gaps = self._gaps
        while gaps and gaps[0].end < now - GAP_WINDOW_S:
            gaps.popleft()
            self._dirty = True

    def _window(self) -> float:
        usual = self._usual or 0.0
        return min(DIP_MAX_S, max(DIP_MIN_S, DIP_WINDOW_GAPS * usual))

    def _restart_window(self) -> None:
        self._samples.clear()
        self._peaks.clear()
        self._sum = self._weight = 0.0
        self._anchor = None
        self._window_from = None
        self._window_batches = 0

    def _mean(self) -> float:
        anchor = self._anchor
        assert anchor is not None and self._weight > 0
        return anchor + self._sum / self._weight

    def _sample(self, now: float, level: float) -> None:
        peaks = self._peaks
        if peaks and level > peaks[0][1] + STEP_S:
            self._restart_window()  # well above anything the window saw: a step
        samples = self._samples
        dt = min(max(now - samples[-1][0], 0.0), _MAX_DT) if samples else _TICK_S
        if self._window_from is None:
            self._window_from = now
        if self._anchor is None:
            self._anchor = level
        samples.append((now, level, dt))
        self._sum += (level - self._anchor) * dt
        self._weight += dt
        while peaks and peaks[-1][1] <= level:
            peaks.pop()
        peaks.append((now, level))
        start = now - self._window()
        while len(samples) > 1 and samples[0][0] < start:
            _, old, weight = samples.popleft()
            self._sum -= (old - self._anchor) * weight
            self._weight -= weight
        while len(peaks) > 1 and peaks[0][0] < start:
            peaks.popleft()

    def _estimate(self) -> Optional[float]:
        if len(self._gaps) < MIN_GAPS:
            return None
        ordered = sorted(self._gaps, key=lambda g: g.gap)
        usual = _quantile([g.gap for g in ordered], 0.5)  # the median
        rhythm = [g for g in ordered if not _trouble(g.gap, usual)]
        if len(rhythm) < MIN_GAPS:
            return None
        gaps = [g.gap for g in rhythm]  # still in order
        late = _quantile(gaps, 0.75) - _quantile(gaps, 0.25)
        need = _quantile(gaps, QUANTILE) + LATE_SHARE * late + MARGIN_S
        dips = sorted(g.dip for g in rhythm if g.dip is not None)
        pace = sum(g.media for g in rhythm) / sum(gaps)  # media per second of real time
        if len(dips) >= MIN_GAPS and abs(pace - 1.0) <= DRIFT_SHARE:
            need = max(need, _quantile(dips, QUANTILE) + MARGIN_S)
        return min(need, NEED_MAX_S)


class StarvationPrior:
    """How often VLC runs dry on one channel, believed (see the module).

    The state is the hours of playing it holds and `risk`, the log2 of the believed rate with
    no margin over the accepted one; `events` (what `Learned` keeps) is the starvations that
    stands for: hours x `ACCEPTED_PER_H` x 2^risk.
    """

    def __init__(self, events: Optional[float] = None, hours: Optional[float] = None) -> None:
        """A restored belief (`Learned.events`, `Learned.hours`), or the prior's."""
        self.hours = PRIOR_H
        self.risk = 0.0
        self._calm = math.inf  # seconds played since the current dip's first near-miss
        self._dip = 0.0  # the share of a starvation that dip counted ...
        self._dip_weight = 1.0  # ... each share standing for this many with no margin
        if (
            events is not None
            and hours is not None
            and _finite(events)
            and _finite(hours)
            and 0.0 < events <= EVENTS_MAX
            and 0.0 < hours <= HOURS_MAX
        ):
            self.hours = max(float(hours), PRIOR_H)
            ratio = float(events) / (self.hours * ACCEPTED_PER_H)
            self.risk = min(max(0.0, math.log2(ratio)), MARGIN_MAX_S / RISK_STEP_S)

    @classmethod
    def from_extra(cls, extra: Optional[float]) -> StarvationPrior:
        """2.0.2's remembered `extra` (what Auto added after trouble) as the belief that needed
        that margin; anything else (none, not positive, beyond `NEED_MAX_S`) as the prior."""
        if extra is None or not _finite(extra) or not 0.0 < extra <= NEED_MAX_S:
            return cls()
        margin = min(float(extra), MARGIN_MAX_S)
        return cls(PRIOR_H * ACCEPTED_PER_H * 2.0 ** (margin / RISK_STEP_S), PRIOR_H)

    @property
    def events(self) -> float:
        return self.hours * self.rate()

    def rate(self) -> float:
        """Believed starvations per hour of playing with no margin."""
        return float(ACCEPTED_PER_H * 2.0**self.risk)

    def margin(self) -> float:
        """The seconds that bring the believed rate down to `ACCEPTED_PER_H` (none under
        `MARGIN_MIN_S`, at most `MARGIN_MAX_S`)."""
        margin = min(MARGIN_MAX_S, RISK_STEP_S * self.risk)
        return margin if margin >= MARGIN_MIN_S - 1e-9 else 0.0

    def starved(self, margin: Optional[float] = None, room: float = MARGIN_MAX_S) -> None:
        """VLC ran dry (a starvation that ran out a cushion, or a stall that did) with `margin`
        seconds in force (None: `margin()`) and `room` the most the cap lets it have."""
        self._count(self._weight(margin, room), room)

    def near_miss(
        self, headroom: float, margin: Optional[float] = None, room: float = MARGIN_MAX_S
    ) -> None:
        """VLC had only `headroom` seconds left when a batch landed: under `NEAR_S`, part of a
        starvation, up to `NEAR_WEIGHT` the closer it came. Within `NEAR_CALM_S` of playing of
        the dip's first, one dip (weighed by the margin in force when it began): only a closer
        one adds, what it is closer by."""
        if not _finite(headroom) or headroom >= NEAR_S:
            return
        share = NEAR_WEIGHT * min(1.0, 1.0 - headroom / NEAR_S)
        if self._calm < NEAR_CALM_S:
            share, self._dip = share - self._dip, max(self._dip, share)
            if share <= 0:
                return
        else:
            self._calm, self._dip = 0.0, share
            self._dip_weight = self._weight(margin, room)
        self._count(share * self._dip_weight, room)

    def played(self, seconds: float) -> None:
        """`seconds` more of playing at the live edge: the hours count, and the belief is
        tempered (its risk halves every `HALF_LIFE_S`)."""
        if not _finite(seconds) or seconds <= 0:
            return
        self._calm += seconds
        fade = math.log(2.0) / HALF_LIFE_S
        keep = math.exp(-fade * seconds)
        counted = (1.0 - keep) / fade  # the seconds played, each faded since
        self.hours = min(HOURS_MAX, PRIOR_H + (self.hours - PRIOR_H) * keep + counted / 3600.0)
        self.risk *= keep

    def _weight(self, margin: Optional[float], room: float) -> float:
        """What one starvation with `margin` in force (None: `margin()`) stands for with none."""
        if margin is None or not _finite(margin):
            margin = self.margin()
        most = min(max(0.0, room), MARGIN_MAX_S)
        return float(2.0 ** (min(max(0.0, margin), most) / RISK_STEP_S))

    def _count(self, starvations: float, room: float) -> None:
        """Count `starvations` (as with no margin), never beyond what `room` answers."""
        most = min(max(0.0, room), MARGIN_MAX_S) / RISK_STEP_S
        risk = math.log2(2.0**self.risk + starvations / (self.hours * ACCEPTED_PER_H))
        self.risk = max(self.risk, min(risk, most))


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
        batches are measured)."""
        return learned_target(self.need(burst), self.prior.margin(), self.floor)

    def margin(self, burst: float = 0.0) -> float:
        """The margin in force: what the target holds above max(floor, need), after the cap."""
        return self.target(burst) - max(self.floor, self.need(burst))

    def room(self, burst: float = 0.0) -> float:
        """The most margin the cap allows over max(floor, need)."""
        need = self.need(burst)
        return _cap(need) - max(self.floor, need)

    def adjusted(self, burst: float = 0.0) -> bool:
        """The target is above the floor (the indicator's "· adjusted")."""
        return self.target(burst) > self.floor + 1e-9

    def starved(self, burst: float = 0.0) -> None:
        """VLC ran dry with the target for `burst` in force."""
        self.prior.starved(self.margin(burst), self.room(burst))

    def near_miss(self, headroom: float, burst: float = 0.0) -> None:
        """VLC had only `headroom` seconds left when a batch landed."""
        self.prior.near_miss(headroom, self.margin(burst), self.room(burst))

    def played(self, seconds: float) -> None:
        """`seconds` more of playing at the live edge."""
        self.prior.played(seconds)

    def learned(self) -> Optional[Learned]:
        """What to remember (rounded); None unless it would change the next tune's target."""
        need = self.arrivals.summary()
        learned = Learned(
            None if need is None else round(need, 2),
            round(self.prior.events, 4),
            round(self.prior.hours, 4),
        )
        return learned if learned.informative(self.floor) else None
