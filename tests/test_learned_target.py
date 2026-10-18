"""The learned live target (2.1 stay-live stage 4): arrivals, the starvation prior, the target."""

import math
import random
import unittest
from unittest import mock

from services.live import learned_target as lt, stay_live, worker
from services.live.learned_target import (
    ArrivalModel,
    Learned,
    LearnedTarget,
    StarvationPrior,
    learned_target,
)
from services.live.stay_live import STALL_S, TARGETS, AutoTarget

TICK = 0.25  # the session's update interval
START = 1_000_000.0  # a wall clock far from zero
SEEDS = range(5)


class Source:
    """A synthetic live source sampled every tick: `pieces` of media landing at given times;
    `edge` is the live edge (media seconds) the helper would report."""

    def __init__(self, model, *, edge=20.0, now=START):
        self.model = model
        self.edge = edge
        self.now = now
        self.began = []  # ticks on which `add()` said a batch began

    def run(self, pieces, until, *, stalled=lambda t: False):
        """Tick until `until` (seconds after START); `pieces`: (seconds after START, media).
        `stalled(t)`: the caller calls `stall()` on that tick."""
        pending = sorted(pieces)
        while self.now - START < until - 1e-9:
            self.now = round(self.now + TICK, 6)
            while pending and pending[0][0] <= self.now - START + 1e-9:
                self.edge += pending.pop(0)[1]
            if stalled(self.now - START):
                self.model.stall()
            if self.model.add(self.now, self.edge):
                self.began.append(self.now)
        return self


def regular(gap, media, until, *, start=0.0):
    """Batches every `gap` seconds from `start`, the n-th bringing media(n) seconds."""
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
    """Batches due every `gap` s, each up to `spread` s early or late, each bringing `gap` s."""
    rng = random.Random(seed)
    return [(n * gap + rng.uniform(-spread, spread), gap) for n in range(1, int(until / gap) + 1)]


def spread_over_ticks(batches, seed, ticks=(1, 12)):
    """Each batch landing over a random number of ticks, from a random phase within a tick."""
    rng = random.Random(seed)
    pieces = []
    for t, media in batches:
        count, t0 = rng.randint(*ticks), t + rng.uniform(0.0, TICK)
        pieces += [(t0 + k * TICK, media / count) for k in range(count)]
    return pieces


def downloaded(batches, seed, took=(0.05, 0.2)):
    """Each batch downloading for a random time, in 20-ms blocks, from a random phase."""
    rng = random.Random(seed)
    pieces = []
    for t, media in batches:
        seconds, t0 = rng.uniform(*took), t + rng.uniform(0.0, TICK)
        count = max(1, int(seconds / 0.02))
        pieces += [(t0 + seconds * (k + 1) / count, media / count) for k in range(count)]
    return pieces


def need_after(pieces, until, burst=0.0, **kwargs):
    model = ArrivalModel()
    Source(model).run(pieces, until, **kwargs)
    return model.need(burst)


class ConstantsTests(unittest.TestCase):
    def test_the_mirrored_constants_match_stay_live(self):
        self.assertEqual(lt.FLOOR_S, TARGETS["normal"])
        self.assertEqual(lt.STALL_S, STALL_S)
        self.assertEqual(lt.STEP_S, stay_live._STEP_S)
        self.assertEqual(lt.NEED_MAX_S, stay_live._AUTO_MAX_S)
        self.assertEqual(lt._TICK_S, stay_live._TICK_S)
        self.assertEqual(lt._MAX_DT, stay_live._MAX_DT)
        for burst in (0.0, 6.0, 7.9, 8.0, 10.0, 20.0):  # today's cap, with the need for it
            self.assertEqual(AutoTarget._cap(burst), lt._cap(burst), burst)
        self.assertEqual(lt.CAP_MIN_S, TARGETS["stable"] + 4.0)
        self.assertEqual(lt.MARGIN_MAX_S, lt.CAP_MIN_S - lt.FLOOR_S)

    def test_a_batch_ends_sooner_than_the_helper_ends_a_burst(self):
        # Three still ticks end a batch; the helper waits 1 s without a byte (on the tick grid
        # that would merge a steady source's 1-s arrivals into one).
        self.assertLess(lt.BATCH_GAP_S, worker._BURST_GAP_S)
        self.assertEqual(lt.BATCH_GAP_S, 3 * TICK)


class ArrivalModelTests(unittest.TestCase):
    def test_steady_one_second_arrivals_need_no_more_than_the_floor(self):
        for seed in SEEDS:
            arrivals = steady(600, seed)
            for ticks in ((1, 1), (1, 2), (1, 12)):
                pieces = spread_over_ticks(arrivals, seed, ticks)
                with self.subTest(seed=seed, ticks=ticks):
                    target = LearnedTarget()
                    Source(target.arrivals).run(pieces, 600)
                    self.assertGreater(target.need(), 1.0)
                    self.assertEqual(target.target(), lt.FLOOR_S)
                    self.assertFalse(target.adjusted())
        self.assertEqual(LearnedTarget().target(), lt.FLOOR_S)  # nothing known: the floor

    def test_an_edge_moving_every_tick_arrives_about_once_a_second(self):
        model = ArrivalModel()
        pieces = [(k * TICK, TICK * (1.1 if k % 3 else 0.8)) for k in range(1, 2401)]
        source = Source(model).run(pieces, 600)
        self.assertAlmostEqual(len(source.began), 600, delta=60)
        self.assertLessEqual(model.need(), lt.FLOOR_S)

    def test_six_s_every_seven_with_every_sixth_twelve_needs_seven_and_a_half_to_nine(self):
        for seed in SEEDS:
            for name, pieces in (
                ("one tick", six_and_twelve(600)),
                ("1-12 ticks", spread_over_ticks(six_and_twelve(600), seed)),
                ("0.05-0.2 s downloads", downloaded(six_and_twelve(600), seed)),
                ("1-3 s downloads", downloaded(six_and_twelve(600), seed, (1.0, 3.0))),
            ):
                for burst in (6.0, 12.0):  # whatever the playlist announces
                    with self.subTest(seed=seed, batches=name, burst=burst):
                        target = LearnedTarget()
                        Source(target.arrivals).run(pieces, 600)
                        self.assertGreaterEqual(target.target(burst), 7.5)
                        self.assertLessEqual(target.target(burst), 9.0)
                        self.assertTrue(target.adjusted(burst))

    def test_ten_s_every_twelve_needs_more_than_the_batch_interval_before_any_starvation(self):
        # TARGETDURATION 10 whose batches land every 12 s: Auto's 2.0.2 target was 10 (the
        # burst) and VLC starved before each batch. Literally 10 s every 12 s, and 12 s batches
        # announced as 10; whole, over 1-12 ticks or downloading for 0.05-0.2 s.
        for seed in SEEDS:
            for media in (10.0, 12.0):
                batches = regular(12.0, media, 600)
                for name, pieces in (
                    ("one tick", batches),
                    ("1-12 ticks", spread_over_ticks(batches, seed)),
                    ("0.05-0.2 s downloads", downloaded(batches, seed)),
                ):
                    with self.subTest(seed=seed, media=media, batches=name):
                        target = LearnedTarget()
                        source = Source(target.arrivals)
                        source.run([p for p in pieces if p[0] <= 60], 60)
                        self.assertEqual(target.prior.margin(), 0.0)  # no starvation told
                        self.assertGreaterEqual(target.target(10.0), 12.5)
                        source.run([p for p in pieces if p[0] > 60], 600)
                        self.assertGreaterEqual(target.target(10.0), 12.5)
                        self.assertLessEqual(target.target(10.0), 14.5)

    def test_a_batch_is_one_arrival_however_many_ticks_it_takes(self):
        model = ArrivalModel()
        pieces = spread_over_ticks(regular(7.0, 7.0, 70), seed=3)
        source = Source(model).run(pieces, 72)
        self.assertEqual(len(source.began), 10)
        for began, (t, _) in zip(source.began, regular(7.0, 7.0, 70)):
            self.assertLessEqual(abs(began - START - t), TICK + 1e-6)

    def test_jittery_arrivals_need_more_than_regular_ones_with_the_same_mean(self):
        regular_need = need_after(regular(7.0, 7.0, 600), 600)
        jittery_need = need_after(jittery(7.0, 600, 2.0), 600)
        wilder_need = need_after(jittery(7.0, 600, 3.0), 600)
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
                self.assertEqual(model.stalls, 1 if told else 0)  # once per stall

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

    def test_late_batches_the_session_calls_stalls_still_count(self):
        # A 12-s rhythm announced as 10 (the session's stall bound: 12 s), about one batch in
        # eight 3-6 s late: the session calls those stalls, yet VLC must hold them out.
        rng = random.Random(5)
        batches, t = [], 0.0
        while t < 600:
            t += 12.0 if rng.random() > 0.12 else rng.choice([15.0, 16.0, 17.0, 18.0])
            batches.append((t, 12.0))
        needs = {}
        for told in (False, True):
            model = ArrivalModel()
            edge_moved = {"at": 0.0, "edge": None}

            def stalled(now, model=model, moved=edge_moved):
                if told and now - moved["at"] > 12.0:
                    return True
                return False

            source = Source(model)
            for when, media in batches:
                source.run([(when, media)], when, stalled=stalled)
                edge_moved["at"] = when
            needs[told] = model.need(10.0)
        self.assertAlmostEqual(needs[True], needs[False], delta=0.05)
        self.assertGreater(needs[False], 16.0)  # the late batches are part of the rhythm

    def test_a_stall_noted_every_cycle_leaves_the_dips_alone(self):
        lumpy = regular(7.0, lambda n: 22.0 if n % 6 == 5 else 4.0, 600)
        quiet = need_after(lumpy, 600)
        told = need_after(lumpy, 600, stalled=lambda t: t % 42 > 30)
        self.assertAlmostEqual(told, quiet, delta=0.05)

    def test_uneven_batches_need_what_the_deepest_dip_needs(self):
        # Small batches and a big one, at a steady 7 s: the buffer sinks deeper than one gap.
        for media, need in (
            (lambda n: 19.0 if n % 5 == 4 else 4.0, 10.3),
            (lambda n: 16.0 if n % 4 == 3 else 4.0, 8.9),
            (lambda n: 22.0 if n % 6 == 5 else 4.0, 12.0),
        ):
            for seed in (None, *SEEDS):
                batches = regular(7.0, media, 600)
                pieces = batches if seed is None else spread_over_ticks(batches, seed, (1, 8))
                with self.subTest(need=need, seed=seed):
                    self.assertAlmostEqual(need_after(pieces, 600), need, delta=0.4)

    def test_a_backlog_at_once_is_a_step_not_a_dip(self):
        # A tune's first fetch: 20 s over 3 or 12 ticks, then a steady source.
        for ticks in (3, 12):
            with self.subTest(ticks=ticks):
                flush = [(TICK * (k + 1), 20.0 / ticks) for k in range(ticks)]
                rhythm = [(t + ticks * TICK, media) for t, media in steady(600, seed=3)]
                self.assertLessEqual(need_after(flush + rhythm, 600), lt.FLOOR_S)

    def test_a_reset_forgets_the_edge_but_keeps_what_was_learned(self):
        model = ArrivalModel()
        source = Source(model).run(six_and_twelve(600), 600)
        learned = model.need()
        model.reset()  # a reopen: the next edge is no batch, and no gap spans the reset
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

    def test_it_estimates_at_most_once_a_second(self):
        # The caller asks every tick (the GUI thread, twice with PiP); a source arriving
        # every half second makes two batches a second: the gaps are sorted once a second.
        estimate, calls = ArrivalModel._estimate, []

        def counted(model):
            calls.append(model._now)
            return estimate(model)

        for step in (1, 2):  # the edge moving on every tick, and on every other one
            with self.subTest(step=step):
                calls.clear()
                target = LearnedTarget()
                now, edge = START, 20.0
                with mock.patch.object(ArrivalModel, "_estimate", counted):
                    for k in range(2400):
                        now = now + TICK
                        if k % step == 0:
                            edge += step * TICK * (1.1 if k % 3 else 0.8)
                        target.arrivals.add(now, edge)
                        target.target()
                        target.adjusted()
                self.assertLessEqual(len(calls), 600 + lt.MIN_GAPS)
                first = lt.MIN_GAPS  # before a first estimate, each batch tries
                later = calls[first:]
                spaced = [b - a for a, b in zip(later, later[1:])]
                self.assertGreaterEqual(min(spaced), lt.ESTIMATE_EVERY_S - 1e-6)

    def test_memory_stays_bounded(self):
        model = ArrivalModel()
        Source(model).run([(k * TICK, TICK) for k in range(1, 14401)], 3600)
        self.assertLessEqual(len(model._gaps), lt.MAX_GAPS)
        self.assertLessEqual(len(model._samples), lt.DIP_MAX_S / TICK + 2)
        self.assertLessEqual(len(model._peaks), lt.DIP_MAX_S / TICK + 2)


class StarvationPriorTests(unittest.TestCase):
    def test_at_first_it_adds_nothing(self):
        prior = StarvationPrior()
        self.assertEqual(prior.margin(), 0.0)
        self.assertAlmostEqual(prior.rate(), lt.ACCEPTED_PER_H)
        self.assertAlmostEqual(prior.events, lt.PRIOR_EVENTS)

    def test_a_starvation_raises_the_margin_at_once_and_a_near_miss_less(self):
        starved, near, nearer, clear = (StarvationPrior() for _ in range(4))
        starved.starved()
        near.near_miss(0.25)
        nearer.near_miss(0.1)
        clear.near_miss(lt.NEAR_S)  # that much headroom is no evidence
        self.assertAlmostEqual(starved.margin(), math.log2(3), places=6)  # about 2.0.2's 1.5 s
        self.assertGreater(starved.margin(), nearer.margin())
        self.assertGreater(nearer.margin(), near.margin())
        self.assertGreater(near.margin(), 0.0)
        self.assertEqual(clear.margin(), 0.0)

    def test_slight_near_misses_count_once_they_add_up(self):
        prior = StarvationPrior()
        prior.near_miss(0.4)
        self.assertEqual(prior.margin(), 0.0)  # under half a second: none yet
        for _ in range(2):  # the next dips, a batch apart
            prior.played(7.0)
            prior.near_miss(0.4)
        self.assertGreater(prior.margin(), 0.0)

    def test_each_starvation_adds_about_as_much_as_the_first(self):
        # One with a margin in force stands for more with none: the margin should have made
        # it rarer. So the second adds as much as the first (2.0.2 added 1.5 s each time).
        prior = StarvationPrior()
        prior.starved()
        prior.starved()
        self.assertAlmostEqual(prior.margin(), 2 * math.log2(3), places=6)
        spaced = StarvationPrior()
        for _ in range(6):  # one every ten minutes of playing
            before = spaced.margin()
            spaced.starved()
            self.assertGreater(spaced.margin() - before, 1.0)
            spaced.played(600.0)

    def test_nothing_counted_goes_beyond_the_room_the_cap_leaves(self):
        prior, margins = StarvationPrior(), []
        for _ in range(200):
            prior.starved()
            prior.near_miss(0.1)
            margins.append(prior.margin())
        self.assertEqual(margins, sorted(margins))
        self.assertEqual(margins[-1], lt.MARGIN_MAX_S)
        tight = StarvationPrior()
        for _ in range(20):
            tight.starved(room=2.0)
        self.assertAlmostEqual(tight.margin(), 2.0)
        tight.played(1800.0)
        self.assertLess(tight.margin(), 1.01)  # it did not pile up beyond the room

    def test_a_dip_counts_once_at_its_closest(self):
        once = StarvationPrior()
        once.near_miss(0.1)
        for order in ((0.4, 0.1), (0.1, 0.4), (0.3, 0.2, 0.1)):
            with self.subTest(order=order):
                dip = StarvationPrior()
                for headroom in order:
                    dip.near_miss(headroom)
                    dip.played(TICK)
                self.assertAlmostEqual(dip.margin(), once.margin(), delta=0.01)
        flood = StarvationPrior()
        for _ in range(int(lt.NEAR_CALM_S / TICK)):  # a source arriving every tick
            flood.near_miss(0.1)
            flood.played(TICK)
        flood.near_miss(0.1)  # a new dip
        self.assertGreater(flood.margin(), once.margin() + 0.2)

    def test_quiet_playing_lowers_it_steadily_back_to_nothing(self):
        prior = StarvationPrior()
        prior.starved()
        start, margins = prior.margin(), []
        for _ in range(120):  # two hours, a minute at a time
            prior.played(60.0)
            margins.append(prior.margin())
        self.assertEqual(margins, sorted(margins, reverse=True))
        self.assertLess(margins[30], start / 2)  # half an hour
        self.assertEqual(margins[59], 0.0)  # an hour
        self.assertEqual(margins[-1], 0.0)

    def test_a_long_run_of_trouble_is_back_within_two_hours_of_quiet(self):
        for need in (0.0, 8.0, 13.0):
            with self.subTest(need=need):
                target = LearnedTarget(Learned(need=need) if need else None)
                peak = 0.0
                for minute in range(120):  # starving every ten minutes for two hours
                    if minute % 10 == 0:
                        target.starved()
                    peak = max(peak, target.prior.margin())  # the belief's own margin
                    target.played(60.0)
                self.assertLessEqual(peak, target.room() + 1e-9)  # never beyond the room
                for _ in range(120):
                    target.played(60.0)
                self.assertEqual(target.target(), max(lt.FLOOR_S, target.need()))

    def test_how_the_playing_time_is_told_does_not_matter(self):
        once, ticks = StarvationPrior(), StarvationPrior()
        for prior in (once, ticks):
            prior.starved()
            prior.starved()
        once.played(1800.0)
        for _ in range(7200):
            ticks.played(0.25)
        self.assertAlmostEqual(once.margin(), ticks.margin(), places=6)
        self.assertAlmostEqual(once.hours, ticks.hours, places=6)

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

    def test_a_restored_state_round_trips(self):
        prior = StarvationPrior()
        prior.starved()
        prior.played(600.0)
        again = StarvationPrior(prior.events, prior.hours)
        self.assertAlmostEqual(again.margin(), prior.margin())
        self.assertAlmostEqual(again.hours, prior.hours)


class LearnedTargetFunctionTests(unittest.TestCase):
    def test_the_floor_then_the_need_then_the_margin_up_to_todays_cap(self):
        self.assertEqual(learned_target(0.0, 0.0), 2.5)
        self.assertEqual(learned_target(2.0, 0.0), 2.5)
        self.assertEqual(learned_target(2.0, 0.6), 3.1)  # trouble at the floor raises it
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

    def test_an_hour_of_quiet_brings_it_back_to_the_need(self):
        target = LearnedTarget()
        Source(target.arrivals).run(six_and_twelve(600), 600)
        need = target.need()
        target.starved()
        raised = target.target()
        for _ in range(1800):
            target.played(1.0)
        self.assertLess(target.target() - need, (raised - need) / 2 + 1e-9)
        for _ in range(1800):
            target.played(1.0)
        self.assertEqual(target.target(), need)

    def test_the_margin_in_force_is_what_the_cap_leaves(self):
        target = LearnedTarget()
        Source(target.arrivals).run(regular(12.0, 12.0, 600), 600)
        for _ in range(20):
            target.starved()
        self.assertEqual(target.target(), target.need() + 2.0)
        self.assertAlmostEqual(target.margin(), 2.0)
        self.assertAlmostEqual(target.room(), 2.0)

    def test_nothing_that_would_change_the_next_tune_is_nothing_to_remember(self):
        self.assertIsNone(LearnedTarget().learned())
        zap = LearnedTarget()
        zap.played(0.25)  # a zap through
        self.assertIsNone(zap.learned())
        quiet = LearnedTarget()  # a steady channel: its need is under the floor
        Source(quiet.arrivals).run(steady(120), 120)
        quiet.played(120.0)
        self.assertIsNone(quiet.learned())
        faded = LearnedTarget()
        faded.near_miss(0.4)  # too slight to make a margin
        self.assertIsNone(faded.learned())

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

    def test_a_restored_need_holds_until_the_tune_has_seen_enough_batches(self):
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
        fresh.starved()
        self.assertIsNone(fresh.learned().need)  # too few batches to be worth keeping

    def test_a_two_zero_two_extra_is_read_as_a_prior(self):
        target = LearnedTarget(None, extra=3.0)
        self.assertAlmostEqual(target.target(), lt.FLOOR_S + 3.0)
        self.assertTrue(target.adjusted())
        learned = target.learned()
        self.assertIsNone(learned.need)
        self.assertAlmostEqual(LearnedTarget(learned).target(), lt.FLOOR_S + 3.0)
        for _ in range(7200):  # it fades as any trouble does
            target.played(1.0)
        self.assertEqual(target.target(), lt.FLOOR_S)

    def test_a_learned_state_wins_over_an_extra(self):
        target = LearnedTarget(Learned(need=8.0), extra=3.0)
        self.assertEqual(target.target(), 8.0)


if __name__ == "__main__":
    unittest.main()
