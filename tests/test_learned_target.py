"""The learned live target (2.1 stay-live stage 4): arrivals, the starvation prior, the target."""

import math
import random
import unittest

from services.live import learned_target as lt
from services.live.learned_target import (
    ArrivalModel,
    Learned,
    LearnedTarget,
    StarvationPrior,
    learned_target,
)
from services.live.stay_live import STALL_S, TARGETS

TICK = 0.25  # the session's update interval
START = 1_000_000.0  # a wall clock far from zero


class Source:
    """A synthetic live source sampled every tick: arrivals at given times, each bringing some
    seconds of media; `edge` is the live edge (media seconds) the helper would report."""

    def __init__(self, model, *, edge=20.0, now=START):
        self.model = model
        self.edge = edge
        self.now = now
        self.landed = []  # ticks on which `add()` said an arrival landed

    def run(self, arrivals, until, *, stalled=lambda t: False):
        """Tick until `until` (seconds after START); `arrivals`: sorted (seconds after START,
        media). `stalled(t)`: the caller calls `stall()` on that tick."""
        pending = list(arrivals)
        while self.now - START < until - 1e-9:
            self.now = round(self.now + TICK, 6)
            while pending and pending[0][0] <= self.now - START + 1e-9:
                self.edge += pending.pop(0)[1]
            if stalled(self.now - START):
                self.model.stall()
            if self.model.add(self.now, self.edge):
                self.landed.append(self.now)
        return self


def regular(gap, media, until, *, start=0.0):
    """Arrivals every `gap` seconds from `start`, the n-th bringing media(n) seconds."""
    times, t, n = [], start + gap, 0
    while t <= until + 1e-9:
        times.append((t, media(n) if callable(media) else media))
        t, n = t + gap, n + 1
    return times


def six_and_twelve(until):
    """A public HLS channel as measured in 2.1: a batch every 7 s, 6 s of media, every sixth 12 s."""
    return regular(7.0, lambda n: 12.0 if n % 6 == 5 else 6.0, until)


def steady(until, seed=1):
    """~1 s arrivals of ~1 s of media each (a raw TS source that keeps its rhythm)."""
    rng = random.Random(seed)
    times, t = [], 0.0
    while t < until:
        gap = rng.uniform(0.85, 1.15)
        t += gap
        times.append((t, gap))
    return times


def jittery(gap, until, spread, seed=2):
    """Arrivals due every `gap` s, each up to `spread` s early or late, each bringing `gap` s."""
    rng = random.Random(seed)
    return [(n * gap + rng.uniform(-spread, spread), gap) for n in range(1, int(until / gap) + 1)]


class ConstantsTests(unittest.TestCase):
    def test_the_mirrored_constants_match_stay_live(self):
        self.assertEqual(lt.FLOOR_S, TARGETS["normal"])
        self.assertEqual(lt.CAP_MIN_S, TARGETS["stable"] + 4.0)
        self.assertEqual(lt.STALL_S, STALL_S)
        self.assertEqual(lt.MARGIN_MAX_S, lt.CAP_MIN_S - lt.FLOOR_S)


class ArrivalModelTests(unittest.TestCase):
    def need_after(self, arrivals, until, burst=0.0, **kwargs):
        model = ArrivalModel()
        Source(model).run(arrivals, until, **kwargs)
        return model, model.need(burst)

    def test_steady_one_second_arrivals_need_no_more_than_the_floor(self):
        model, need = self.need_after(steady(600), 600)
        self.assertGreater(need, 1.0)
        self.assertLessEqual(need, lt.FLOOR_S)
        self.assertEqual(LearnedTarget().target(), lt.FLOOR_S)  # nothing known: the floor
        target = LearnedTarget()
        Source(target.arrivals).run(steady(600), 600)
        self.assertEqual(target.target(), lt.FLOOR_S)
        self.assertFalse(target.adjusted())

    def test_six_s_every_seven_with_every_sixth_twelve_needs_seven_and_a_half_to_nine(self):
        for burst in (6.0, 7.0, 12.0):  # whatever the playlist announces
            with self.subTest(burst=burst):
                target = LearnedTarget()
                Source(target.arrivals).run(six_and_twelve(600), 600)
                self.assertGreaterEqual(target.target(burst), 7.5)
                self.assertLessEqual(target.target(burst), 9.0)
                self.assertTrue(target.adjusted(burst))

    def test_ten_s_every_twelve_needs_more_than_the_batch_interval_before_any_starvation(self):
        # TARGETDURATION 10 whose batches land every 12 s: Auto's 2.0.2 target was 10 (the
        # burst) and VLC starved before each batch. Literally 10 s every 12 s, and 12 s batches
        # announced as 10.
        for media in (10.0, 12.0):
            with self.subTest(media=media):
                target = LearnedTarget()
                source = Source(target.arrivals)
                source.run(regular(12.0, media, 60), 60)
                self.assertEqual(target.prior.margin(), 0.0)  # no starvation told
                self.assertGreaterEqual(target.target(10.0), 12.5)
                source.run(regular(12.0, media, 600, start=60), 600)
                self.assertGreaterEqual(target.target(10.0), 12.5)
                self.assertLessEqual(target.target(10.0), 16.0)

    def test_jittery_arrivals_need_more_than_regular_ones_with_the_same_mean(self):
        _, regular_need = self.need_after(regular(7.0, 7.0, 600), 600)
        _, jittery_need = self.need_after(jittery(7.0, 600, 2.0), 600)
        _, wilder_need = self.need_after(jittery(7.0, 600, 3.0), 600)
        self.assertAlmostEqual(regular_need, 7.0 + lt.MARGIN_S, delta=0.3)
        self.assertGreater(jittery_need, regular_need + 1.0)
        self.assertGreater(wilder_need, jittery_need)

    def test_a_forty_second_stall_does_not_move_the_need(self):
        for told in (True, False):  # whether or not the caller says it stalled
            with self.subTest(told=told):
                model = ArrivalModel()
                source = Source(model).run(regular(7.0, 7.0, 600), 600)
                before = model.need()
                # Nothing for 40 s more than a gap, then the missed media at once, then the rhythm.
                after_stall = [(647.0, 47.0)] + regular(7.0, 7.0, 720, start=647.0)
                source.run(after_stall, 720, stalled=lambda t: told and 600 + 7 + STALL_S < t < 647)
                self.assertAlmostEqual(model.need(), before, delta=0.05)

    def test_stalls_however_many_are_no_rhythm(self):
        # One every two and a half minutes: enough to sit in the 95th percentile if counted.
        for told in (True, False):
            with self.subTest(told=told):
                model = ArrivalModel()
                source = Source(model).run(regular(7.0, 7.0, 120), 120)
                start = 120.0
                for _ in range(6):
                    back = start + 7.0 + 40.0
                    rhythm = [(back, 47.0)] + regular(7.0, 7.0, back + 105.0, start=back)
                    source.run(
                        rhythm,
                        back + 105.0,
                        stalled=lambda t, s=start: told and s + 7 + STALL_S < t < s + 47,
                    )
                    start = back + 105.0
                self.assertAlmostEqual(model.need(), 7.0 + lt.MARGIN_S, delta=0.05)

    def test_an_arrival_is_reported_on_the_tick_it_lands(self):
        model = ArrivalModel()
        source = Source(model).run(regular(7.0, 7.0, 30), 30)
        self.assertEqual([t - START for t in source.landed], [7.0, 14.0, 21.0, 28.0])

    def test_a_reset_forgets_the_edge_but_keeps_what_was_learned(self):
        model = ArrivalModel()
        source = Source(model).run(six_and_twelve(600), 600)
        learned = model.need()
        model.reset()  # a reopen: the next edge is no arrival, and no gap spans the reset
        source.now += 30.0
        source.run(six_and_twelve(720)[-5:], 720)
        self.assertAlmostEqual(model.need(), learned, delta=0.3)

    def test_an_edge_that_goes_back_starts_afresh_without_a_bogus_gap(self):
        model = ArrivalModel()
        source = Source(model).run(regular(7.0, 7.0, 300), 300)
        before = model.need()
        source.edge -= 500.0  # another timeline
        source.run(regular(7.0, 7.0, 600, start=300), 600)
        self.assertAlmostEqual(model.need(), before, delta=0.05)

    def test_until_measured_the_burst_stands_in_and_warm_up_never_lowers_it(self):
        model = ArrivalModel()
        self.assertEqual(model.need(), 0.0)  # steady and unknown: the floor decides
        self.assertEqual(model.need(6.0), 6.0 + lt.MARGIN_S)
        # An HLS start: the live window's segments are fetched one after another, a second
        # apart, then the rhythm. The quick gaps alone would say 2 s.
        backlog = [(float(n), 6.0) for n in range(1, 6)]
        source = Source(model).run(backlog, 5.5)
        self.assertIsNotNone(model.fresh())
        self.assertLess(model.fresh(), 3.0)
        self.assertGreaterEqual(model.need(6.0), 6.0 + lt.MARGIN_S)
        source.run(regular(6.0, 6.0, 31, start=5.0), 31)
        self.assertGreaterEqual(model.need(6.0), 6.0 + lt.MARGIN_S)
        source.run(regular(6.0, 6.0, 600, start=29.0), 600)
        self.assertAlmostEqual(model.need(6.0), 6.0 + lt.MARGIN_S, delta=0.3)
        self.assertAlmostEqual(model.need(0.0), model.need(10.0))  # measured: the burst is moot

    def test_uneven_batches_need_what_the_deepest_dip_needs(self):
        # Five small batches and a big one, at a steady 7 s: the buffer sinks below one gap.
        lumpy = regular(7.0, lambda n: 22.0 if n % 6 == 5 else 4.0, 600)
        model, need = self.need_after(lumpy, 600)
        self.assertGreater(need, 7.0 + lt.MARGIN_S + 1.0)

    def test_memory_stays_bounded(self):
        model = ArrivalModel()
        Source(model).run(regular(0.25, 0.25, 3600), 3600)
        self.assertLessEqual(len(model._gaps), lt.MAX_GAPS)
        self.assertLessEqual(len(model._samples), lt.DEPTH_MAX_S / TICK + 2)


class StarvationPriorTests(unittest.TestCase):
    def test_at_first_it_adds_nothing(self):
        prior = StarvationPrior()
        self.assertEqual(prior.margin(), 0.0)
        self.assertAlmostEqual(prior.rate(), lt.ACCEPTED_PER_H)

    def test_a_starvation_raises_the_margin_at_once_and_a_near_miss_less(self):
        starved, near, nearer, clear = (StarvationPrior() for _ in range(4))
        starved.starved()
        near.near_miss(0.4)
        nearer.near_miss(0.1)
        clear.near_miss(lt.NEAR_S)  # that much headroom is no evidence
        self.assertAlmostEqual(starved.margin(), math.log2(3), places=6)  # about 2.0.2's 1.5 s
        self.assertGreater(starved.margin(), nearer.margin())
        self.assertGreater(nearer.margin(), near.margin())
        self.assertGreater(near.margin(), 0.0)
        self.assertEqual(clear.margin(), 0.0)

    def test_each_starvation_adds_about_as_much_as_the_first(self):
        # One with a margin in force stands for more with none: the margin should have made
        # it rarer. So the second adds as much as the first (2.0.2 added 1.5 s each time).
        prior = StarvationPrior()
        prior.starved()
        prior.starved()
        self.assertAlmostEqual(prior.margin(), 2 * math.log2(3), places=6)
        spaced, margins = StarvationPrior(), []
        for _ in range(4):  # one every ten minutes of playing
            spaced.starved()
            margins.append(spaced.margin())
            spaced.played(600.0)
        steps = [b - a for a, b in zip(margins, margins[1:])]
        for step in steps:
            self.assertGreater(step, 0.7)
        self.assertGreater(margins[-1], 4.0)

    def test_more_trouble_means_more_margin_up_to_the_bound(self):
        prior, margins = StarvationPrior(), []
        for _ in range(200):
            prior.starved()
            prior.near_miss(0.1)
            margins.append(prior.margin())
        self.assertEqual(margins, sorted(margins))
        self.assertEqual(margins[-1], lt.MARGIN_MAX_S)
        self.assertLess(prior.events, 200.0)  # never more than the bound answers
        for _ in range(240):  # so four hours of quiet still bring it down
            prior.played(60.0)
        self.assertLess(prior.margin(), 1.0)

    def test_a_dip_counts_once_however_often_it_is_told(self):
        once, flood = StarvationPrior(), StarvationPrior()
        once.near_miss(0.2)
        for _ in range(int(lt.NEAR_CALM_S / TICK) - 1):  # a source arriving every tick
            flood.near_miss(0.2)
            flood.played(TICK)
        self.assertAlmostEqual(flood.margin(), once.margin(), delta=0.01)
        flood.played(TICK)
        flood.near_miss(0.2)  # a new dip
        self.assertGreater(flood.margin(), once.margin() + 0.2)

    def test_quiet_playing_lowers_it_steadily_back_to_nothing(self):
        prior = StarvationPrior()
        prior.starved()
        start, margins = prior.margin(), []
        for _ in range(120):  # two hours, a minute at a time
            prior.played(60.0)
            margins.append(prior.margin())
        self.assertEqual(margins, sorted(margins, reverse=True))
        self.assertLess(margins[29], start / 2)  # half an hour
        self.assertEqual(margins[59], 0.0)  # an hour
        self.assertEqual(margins[-1], 0.0)

    def test_how_the_playing_time_is_told_does_not_matter(self):
        once, ticks = StarvationPrior(), StarvationPrior()
        for prior in (once, ticks):
            prior.starved()
        once.played(1800.0)
        for _ in range(7200):
            ticks.played(0.25)
        self.assertAlmostEqual(once.margin(), ticks.margin(), places=6)

    def test_a_two_zero_two_extra_reads_as_the_margin_it_needed(self):
        self.assertAlmostEqual(StarvationPrior.from_extra(3.0).margin(), 3.0)
        self.assertAlmostEqual(StarvationPrior.from_extra(1.5).margin(), 1.5)
        self.assertEqual(StarvationPrior.from_extra(50.0).margin(), lt.MARGIN_MAX_S)
        for bad in (None, 0.0, -1.0, math.nan, math.inf, 61.0):
            self.assertEqual(StarvationPrior.from_extra(bad).margin(), 0.0, bad)

    def test_a_restored_state_that_makes_no_sense_starts_afresh(self):
        for events, hours in ((math.nan, 1.0), (1.0, -1.0), (math.inf, 1.0), (1.0, 0.0)):
            prior = StarvationPrior(events, hours)
            self.assertEqual((prior.events, prior.hours), (lt.PRIOR_EVENTS, lt.PRIOR_H))


class LearnedTargetFunctionTests(unittest.TestCase):
    def test_the_floor_then_the_need_then_the_margin_up_to_todays_cap(self):
        self.assertEqual(learned_target(0.0, 0.0), 2.5)
        self.assertEqual(learned_target(2.0, 0.0), 2.5)
        self.assertEqual(learned_target(2.0, 0.3), 2.8)  # trouble at the floor raises it
        self.assertEqual(learned_target(8.0, 0.0), 8.0)
        self.assertEqual(learned_target(8.0, 1.5), 9.5)
        self.assertEqual(learned_target(2.0, 9.0), 10.0)  # Stable's + 4 s
        self.assertEqual(learned_target(13.0, 5.0), 15.0)  # or the need + 2 s
        self.assertEqual(learned_target(13.0, -1.0), 13.0)

    def test_the_target_never_drops_below_the_need(self):
        target = LearnedTarget()
        Source(target.arrivals).run(regular(12.0, 12.0, 600), 600)
        need = target.need()
        target.starved()
        self.assertGreater(target.target(), need)
        for _ in range(240):  # four hours of quiet, a minute at a time
            target.played(60.0)
            self.assertGreaterEqual(target.target(), need)
        self.assertAlmostEqual(target.target(), need)


class LearnedTargetTests(unittest.TestCase):
    def test_a_starvation_raises_the_target_at_once_a_near_miss_less(self):
        targets = [LearnedTarget() for _ in range(3)]
        for target in targets:
            Source(target.arrivals).run(six_and_twelve(600), 600)
        base = targets[0].target()
        targets[1].starved()
        targets[2].near_miss(0.25)
        self.assertGreater(targets[1].target(), targets[2].target())
        self.assertGreater(targets[2].target(), base)

    def test_an_hour_of_quiet_brings_it_back_toward_the_need(self):
        target = LearnedTarget()
        Source(target.arrivals).run(six_and_twelve(600), 600)
        need = target.need()
        target.starved()
        raised = target.target()
        for _ in range(3600):
            target.played(1.0)
        self.assertLess(target.target() - need, (raised - need) / 2)
        self.assertGreaterEqual(target.target(), need)

    def test_the_margin_in_force_is_what_the_cap_leaves(self):
        target = LearnedTarget()
        Source(target.arrivals).run(regular(12.0, 12.0, 600), 600)
        for _ in range(20):
            target.starved()
        self.assertEqual(target.target(), target.need() + 2.0)
        self.assertAlmostEqual(target.margin(), 2.0)

    def test_nothing_learned_is_nothing_to_remember(self):
        self.assertIsNone(LearnedTarget().learned())
        self.assertIsNone(LearnedTarget(None, None).learned())

    def test_what_it_learned_restores_the_same_target_at_once(self):
        target = LearnedTarget()
        Source(target.arrivals).run(regular(12.0, 12.0, 600), 600)
        target.near_miss(0.2)
        target.played(120.0)
        learned = target.learned()
        self.assertIsInstance(learned, Learned)
        again = LearnedTarget(learned)
        self.assertAlmostEqual(again.target(), target.target(), delta=0.01)
        self.assertAlmostEqual(again.target(10.0), target.target(10.0), delta=0.01)

    def test_a_restored_need_holds_until_the_tune_has_seen_enough_arrivals(self):
        again = LearnedTarget(Learned(need=13.0))
        self.assertEqual(again.target(), 13.0)
        source = Source(again.arrivals).run(regular(7.0, 7.0, 35), 35)  # five quick ones
        self.assertEqual(again.target(), 13.0)
        source.run(regular(7.0, 7.0, 600, start=35), 600)
        self.assertAlmostEqual(again.target(), 7.0 + lt.MARGIN_S, delta=0.3)
        self.assertAlmostEqual(again.learned().need, again.need(), places=2)

    def test_a_short_tune_keeps_the_need_it_was_given(self):
        again = LearnedTarget(Learned(need=13.0))
        Source(again.arrivals).run(regular(7.0, 7.0, 35), 35)
        self.assertEqual(again.learned().need, 13.0)
        fresh = LearnedTarget()
        Source(fresh.arrivals).run(regular(7.0, 7.0, 35), 35)
        fresh.played(35.0)
        self.assertIsNone(fresh.learned().need)  # too few arrivals to be worth keeping

    def test_a_two_zero_two_extra_is_read_as_a_prior(self):
        target = LearnedTarget(None, extra=3.0)
        self.assertAlmostEqual(target.target(), lt.FLOOR_S + 3.0)
        self.assertTrue(target.adjusted())
        learned = target.learned()
        self.assertIsNone(learned.need)
        self.assertAlmostEqual(LearnedTarget(learned).target(), lt.FLOOR_S + 3.0)
        for _ in range(7200):  # it fades as any trouble does
            target.played(1.0)
        self.assertLess(target.target(), lt.FLOOR_S + 1.0)

    def test_a_learned_state_wins_over_an_extra(self):
        target = LearnedTarget(Learned(need=8.0), extra=3.0)
        self.assertEqual(target.target(), 8.0)


if __name__ == "__main__":
    unittest.main()
