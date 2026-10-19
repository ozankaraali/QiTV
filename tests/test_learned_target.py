"""The learned live target (2.1 stay-live stage 4): arrivals, the starvation prior, the target."""

from collections import deque
import math
import random
import unittest
from unittest import mock

import auto_target
from auto_target import AutoTarget

from services.live import learned_target as lt, stay_live, worker
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
SEEDS = range(5)


class Source:
    """A synthetic live source sampled every tick: `pieces` of media landing at given times;
    `edge` is the live edge (media seconds) the helper would report."""

    def __init__(self, model, *, edge=20.0, now=START):
        self.model = model
        self.edge = edge
        self.now = now
        self.began = []  # ticks on which `add()` said a batch began
        self.levels = []  # (seconds after START, edge - wall) each tick

    def run(self, pieces, until, *, stalled=lambda t: False):
        """Tick until `until` (seconds after START); `pieces`: (seconds after START, media).
        `stalled(t)`: the caller calls `stall()` on that tick."""
        pending = deque(sorted(pieces))
        while self.now - START < until - 1e-9:
            self.now = round(self.now + TICK, 6)
            while pending and pending[0][0] <= self.now - START + 1e-9:
                self.edge += pending.popleft()[1]
            if stalled(self.now - START):
                self.model.stall()
            if self.model.add(self.now, self.edge):
                self.began.append(self.now)
            self.levels.append((self.now - START, self.edge - (self.now - START)))
        return self


def dry_runs(levels, target, skip=120.0, window=60.0, caching=0.3):
    """Times VLC would run dry holding a mean lag of `target` (the mean over the last
    `window`, as stay-live steers it): the lag under VLC's own caching, once per dip."""
    recent, total, count, inside = deque(), 0.0, 0, False
    for t, level in levels:
        recent.append((t, level))
        total += level
        while recent[0][0] < t - window:
            total -= recent.popleft()[1]
        if t < skip:
            continue
        lag = target - (total / len(recent) - level)
        if lag < caching and not inside:
            count, inside = count + 1, True
        elif lag >= caching + TICK:
            inside = False
    return count


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


def truth(levels, skip=120.0):
    """The mean lag that never runs dry: the mean level less its deepest dip, plus a second."""
    later = [level for t, level in levels if t > skip]
    return sum(later) / len(later) - min(later) + 1.0


def lumpy(n):
    """Four small batches and a big one, every 7 s (35 s of media per 35 s)."""
    return 19.0 if n % 5 == 4 else 4.0


def hiccuping(seg, took, seed, until):
    """Segments every `seg` s, each downloading for a random time in `took` in 20-ms blocks,
    the download pausing 0.25-0.5 s at a fifth of its quarter-second marks."""
    rng = random.Random(seed)
    pieces, start = [], seg
    while start < until:
        seconds = rng.uniform(*took)
        count, t = int(seconds / 0.02), start
        for k in range(count):
            t += seconds / count
            if k % 12 == 0 and rng.random() < 0.2:
                t += rng.uniform(0.25, 0.5)
            pieces.append((t, seg / count))
        start += seg
    return pieces


def flowing(until, seed=0, sag=None, pauses=0.0, pause=(0.3, 0.6), stalls=()):
    """A source whose edge moves on every tick (20-ms blocks at 1x). `sag`: (every, seconds,
    rate): for `seconds` of each `every` the rate drops to `rate`, then makes it up as long.
    `pauses`: the chance per second of a short pause, its media coming at once after it.
    `stalls`: (at, seconds) the source sends nothing, and what it missed is lost."""
    rng = random.Random(seed)
    pieces, t, next_pause = [], 0.0, 0.0
    stalls = sorted(stalls)
    while t < until:
        if stalls and t >= stalls[0][0]:
            t += stalls.pop(0)[1]
            continue
        rate = 1.0
        if sag is not None:
            every, seconds, slow = sag
            phase = t % every
            if 10.0 <= phase < 10.0 + seconds:
                rate = slow
            elif 10.0 + seconds <= phase < 10.0 + 2 * seconds:
                rate = 2.0 - slow
        if pauses and t >= next_pause:
            next_pause = t + 1.0
            if rng.random() < pauses:
                held = rng.uniform(*pause)
                t += held
                pieces.append((t, held))
        t += 0.02
        pieces.append((t, 0.02 * rate))
    return pieces


def flips(model, pieces, until, every=5.0, start=300.0):
    """How often the need moved by more than half a second between looks `every` seconds."""
    source, needs = Source(model), []
    pieces = sorted(pieces)
    for look in range(int(start), int(until) + 1, int(every)):
        source.run([p for p in pieces if source.now - START < p[0] <= look], look)
        needs.append(model.need())
    return sum(1 for a, b in zip(needs, needs[1:]) if abs(a - b) > 0.5), needs


def lossy_hls(seed, until, seg, took, every=(180.0, 300.0)):
    """Segments every `seg` s downloading for a time in `took`; now and then (`every`) one never
    comes (the playlist slid past it): its media is lost."""
    rng = random.Random(seed)
    pieces, start, lose = [], seg, rng.uniform(*every)
    while start < until:
        if start >= lose:
            start, lose = start + seg, start + seg + rng.uniform(*every)
            continue
        seconds = rng.uniform(*took)
        count = int(seconds / 0.02)
        pieces += [(start + seconds * (k + 1) / count, seg / count) for k in range(count)]
        start += seg
    return pieces


class ClosedLoop:
    """VLC holding the target as its mean lag over the last `window`, wired as part B is told
    (`LearnedTarget` docstring; `segment_floor` None: the module's setting), or 2.0.2's
    `AutoTarget` (`learned` False). `dry`: when it ran dry (from 30 s on); `targets`: the
    target every 30 s after ten minutes."""

    def __init__(self, burst, learned=True, caching=0.3, window=60.0, segment_floor=None):
        self.burst, self.caching, self.window = burst, caching, window
        self.target = LearnedTarget(segment_floor=segment_floor) if learned else None
        self.auto = None if learned else AutoTarget()
        self.recent, self.total = deque(), 0.0
        self.dry, self.targets, self.inside, self.previous = [], [], False, None
        self.moved_at, self.stalled, self.stall_low, self.stall_dry = 0.0, False, 0.0, False

    def run(self, pieces, until):
        pending, edge, now = deque(sorted(pieces)), 20.0, 0.0
        while now < until:
            now, before = round(now + TICK, 6), edge
            while pending and pending[0][0] <= now:
                edge += pending.popleft()[1]
            self.tick(now, edge, edge > before + 1e-3)
        return self.dry, self.targets

    def tick(self, now, edge, moved):
        target, burst = self.target, self.burst
        began = target.arrivals.add(START + now, edge) if target else False
        held = target.target(burst) if target else self.auto.target(burst)
        level = edge - now
        self.recent.append((now, level))
        self.total += level
        while self.recent[0][0] < now - self.window:
            self.total -= self.recent.popleft()[1]
        lag = held - (self.total / len(self.recent) - level)
        if now >= 30.0:
            self.watch(now, lag, moved, began)
        self.moved_at = now if moved else self.moved_at
        self.previous = lag
        if now >= 600.0 and abs(now % 30.0) < 1e-9:
            self.targets.append(held)

    def watch(self, now, lag, moved, began):
        target, burst = self.target, self.burst
        if not moved and not self.stalled and now - self.moved_at > burst + STALL_S:
            self.stalled, self.stall_low, self.stall_dry = True, lag, False
            target.stalled(burst) if target else self.auto.trouble(burst)
        if self.stalled:
            self.stall_low = min(self.stall_low, lag)
        if lag < self.caching and not self.inside:
            self.inside, self.stall_dry = True, self.stall_dry or self.stalled
            self.dry.append(now)
            target.starved(burst) if target else self.auto.trouble(burst)
        elif lag >= self.caching + TICK:
            self.inside = False
        if target and began and self.previous is not None and not self.inside:
            target.near_miss(self.previous - self.caching, burst)
        if moved and self.stalled:
            self.stalled = False
            if target and not self.stall_dry:
                target.absorbed(self.stall_low - self.caching, burst)
        if not self.inside:
            target.played(TICK) if target else self.auto.quiet(TICK)


def closed_loop(pieces, until, burst, learned=True, segment_floor=None):
    """`ClosedLoop` over `pieces` until `until`: (when VLC ran dry, the target every 30 s)."""
    return ClosedLoop(burst, learned, segment_floor=segment_floor).run(pieces, until)


def starvations_learned(needed, told, hours=6):
    """Trouble every 10 min that a margin under `needed` can't hold out: starvations per hour
    after the first two (`told`: the trouble a margin held out is told as `absorbed()`)."""
    target, late = LearnedTarget(), 0
    for minute in range(hours * 60):
        if minute % 10 == 0:
            if target.margin() < needed - 1e-9:
                target.starved()
                late += minute >= 120
            elif told:
                target.absorbed(target.margin() - needed)  # the trouble ate into the margin
        target.played(60.0)
    return late / (hours - 2)


def starvations_two_zero_two(needed, hours=6):
    """The same with 2.0.2's AutoTarget."""
    auto, late = AutoTarget(), 0
    for minute in range(hours * 60):
        if minute % 10 == 0 and auto.target(0.0) - lt.FLOOR_S < needed - 1e-9:
            auto.trouble(0.0)
            late += minute >= 120
        for _ in range(4):
            auto.quiet(15.0)
    return late / (hours - 2)


def need_after(pieces, until, burst=0.0, **kwargs):
    model = ArrivalModel()
    Source(model).run(pieces, until, **kwargs)
    return model.need(burst)


class ConstantsTests(unittest.TestCase):
    def test_the_mirrored_constants_match_stay_live(self):
        self.assertEqual(lt.FLOOR_S, TARGETS["normal"])
        self.assertEqual(lt.STALL_S, STALL_S)
        self.assertEqual(lt.STEP_S, stay_live._STEP_S)
        self.assertEqual(lt.NEED_MAX_S, auto_target.AUTO_MAX_S)
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

    def test_six_s_every_seven_with_every_sixth_twelve_needs_what_its_deepest_dip_does(self):
        # The edge falls 6 s below its mean before each 12 s batch: about 7 s of mean lag, run
        # dry at no batch, at most 9 s (the gap term, 7 s less half a 6 s batch, is under it).
        for seed in SEEDS:
            for name, pieces in (
                ("one tick", six_and_twelve(600)),
                ("1-12 ticks", spread_over_ticks(six_and_twelve(600), seed)),
                ("0.05-0.2 s downloads", downloaded(six_and_twelve(600), seed)),
                ("1-3 s downloads", downloaded(six_and_twelve(600), seed, (1.0, 3.0))),
            ):
                with self.subTest(seed=seed, batches=name):
                    target = LearnedTarget()
                    source = Source(target.arrivals).run(pieces, 600)
                    # Announced as 6 s segments: the learned need lifts it above its burst.
                    self.assertEqual(dry_runs(source.levels, target.target(6.0)), 0)
                    self.assertGreater(target.target(6.0), 6.0)
                    self.assertLessEqual(target.target(6.0), 9.0)
                    self.assertTrue(target.adjusted(6.0))
                    # Announced as 12 s: never under its burst (as 2.0.2), nothing to add.
                    self.assertEqual(target.target(12.0), 12.0)
                    self.assertFalse(target.adjusted(12.0))
                    # Learned only: the need, whatever the playlist announces.
                    target.segment_floor = False
                    for burst in (6.0, 12.0):
                        self.assertEqual(target.target(burst), target.need())
                        self.assertLessEqual(target.target(burst), 9.0)
                        self.assertTrue(target.adjusted(burst))

    def test_a_sparse_pcr_rhythm_needs_what_keeps_vlc_from_running_dry_and_no_more(self):
        # Two sparse-PCR channels, measured (2.1 stage 4b): 10 s batches 12, 12 and 6 s apart,
        # their playlists' target duration 12 s. A 12 s target kept 4.9 s of headroom before
        # every batch; read as gaps plus lateness the need was 16 s. Whole, over 1-12 ticks or
        # downloading for 0.05-0.2 s: never dry at the need, and no more than 12 s.
        for seed in SEEDS:
            times = [k * 30.0 + d for k in range(21) for d in (12.0, 24.0, 30.0)]
            batches = [(t, 10.0) for t in times if t <= 600]
            for name, pieces in (
                ("one tick", batches),
                ("1-12 ticks", spread_over_ticks(batches, seed)),
                ("0.05-0.2 s downloads", downloaded(batches, seed)),
            ):
                with self.subTest(seed=seed, batches=name):
                    target = LearnedTarget()
                    source = Source(target.arrivals).run(pieces, 600)
                    need = target.need(12.0)
                    self.assertEqual(dry_runs(source.levels, need), 0)
                    self.assertGreater(need, truth(source.levels))
                    self.assertLessEqual(need, 12.0)
                    self.assertEqual(target.target(12.0), 12.0)  # at its burst
                    target.segment_floor = False
                    self.assertEqual(target.target(12.0), need)

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
        self.assertAlmostEqual(regular_need, 7.0 * (1 - lt.MEDIA_SHARE) + lt.MARGIN_S, delta=0.3)
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
                self.assertAlmostEqual(
                    model.need(), 7.0 * (1 - lt.MEDIA_SHARE) + lt.MARGIN_S, delta=0.2
                )

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

    def test_a_slow_download_cut_into_pieces_keeps_its_cycle(self):
        # HLS segments downloading at under 1.5x come in ~1 s pieces. The idle time between
        # two segments is the source's rhythm, not a stall (trouble is judged against the
        # gaps between batches after a still edge), and the troughs before them set the dips.
        # The gap term holds the idle time and a piece, less half a piece (5.5 s and 6.3 s),
        # above the dips' 3.2 s and 4.0 s. With the burst floor the target stays at the burst
        # (2.0.2's footing); learned only, it is the need, which holds the rhythm alone.
        for seg, took, need in ((15.0, (10.2, 10.8), 5.5), (20.0, (13.5, 14.5), 6.3)):
            for seed in range(4):
                with self.subTest(seg=seg, seed=seed):
                    target = LearnedTarget()
                    pieces = downloaded(regular(seg, seg, 600), seed, took)
                    source = Source(target.arrivals).run(pieces, 600)
                    self.assertAlmostEqual(target.need(seg), need, delta=0.5)
                    self.assertGreater(target.need(seg), truth(source.levels))
                    self.assertEqual(dry_runs(source.levels, target.need(seg)), 0)
                    self.assertEqual(target.target(seg), seg)
                    target.segment_floor = False
                    self.assertEqual(target.target(seg), target.need(seg))
                    self.assertTrue(target.adjusted(seg))  # above the floor

    def test_a_download_twice_as_fast_is_one_batch_a_cycle(self):
        # Not cut into pieces: each segment one batch, its gap the cycle, less half of it.
        for seed in range(4):
            target = LearnedTarget()
            pieces = downloaded(regular(15.0, 15.0, 600), seed, (7.3, 7.7))
            source = Source(target.arrivals).run(pieces, 600)
            need = target.need(15.0)
            self.assertAlmostEqual(need, 15.0 * (1 - lt.MEDIA_SHARE) + lt.MARGIN_S, delta=0.5)
            self.assertEqual(dry_runs(source.levels, need), 0)
            self.assertEqual(target.target(15.0), 15.0)

    def test_a_backlog_never_switches_the_dips_off(self):
        # The dips of a lumpy source need 10.4 s; a backlog delivered at once (the first
        # fetch's, or one after a stall) is no drift of the source's.
        rhythm = regular(7.0, lumpy, 1500)
        lost = [(t, media) for t, media in rhythm if not 600 <= t < 630]
        cases = {
            "first fetch": ([(0.75, 30.0)] + rhythm, 200),
            "stall, then the backlog": (lost + [(630.0, 30.0)], 650),
            "stall, nothing back": (lost, 650),
        }
        for name, (pieces, first) in cases.items():
            with self.subTest(case=name):
                model, source, needs = ArrivalModel(), None, []
                source = Source(model)
                for until in range(first, 1501, 50):
                    source.run([p for p in pieces if source.now - START < p[0] <= until], until)
                    needs.append(model.need())
                self.assertGreaterEqual(min(needs), 10.0, needs)
                self.assertLessEqual(max(needs), 10.8, needs)

    def test_a_flowing_source_that_sags_is_held_through_its_sags(self):
        # Throughput dips on a source whose edge moves on every tick: the deepest of each half
        # minute counts, however many shallow pieces come between, and with a pause now and
        # then (troughs) too.
        sags = ((45.0, 3.0, 0.2), (60.0, 6.0, 0.4), (60.0, 4.0, 0.3), (100.0, 5.0, 0.2))
        for sag in sags:
            for pauses in (0.0, 0.02):
                for seed in range(3):
                    with self.subTest(sag=sag, pauses=pauses, seed=seed):
                        model = ArrivalModel()
                        pieces = flowing(780.0, seed, sag=sag, pauses=pauses)
                        source = Source(model).run(pieces, 780.0)
                        target = max(lt.FLOOR_S, model.need())
                        self.assertEqual(dry_runs(source.levels, target), 0)
                        self.assertGreater(target, truth(source.levels) - 0.3)
                        self.assertLess(target, truth(source.levels) + 0.6)

    def test_a_slow_download_with_hiccups_needs_its_deepest_troughs(self):
        for seed in range(3):
            with self.subTest(seed=seed):
                model = ArrivalModel()
                source = Source(model).run(hiccuping(10.0, (6.0, 9.5), seed, 600.0), 600.0)
                self.assertGreaterEqual(model.need(), truth(source.levels))
                self.assertEqual(dry_runs(source.levels, model.need()), 0)

    def test_a_flowing_source_that_only_stands_still_when_it_stalls_has_no_cycle(self):
        # Its stalls (6-10 s every 2-5 min) are not its rhythm: each is trouble, whatever
        # their spacing, and the need stays where the flow puts it.
        for seed in range(2):
            with self.subTest(seed=seed):
                rng, stalls, t = random.Random(seed), [], 0.0
                while t < 1500.0:
                    t += rng.uniform(120.0, 300.0)
                    stalls.append((t, rng.uniform(6.0, 10.0)))
                model = ArrivalModel()
                count, needs = flips(model, flowing(1500.0, seed, stalls=stalls), 1500.0)
                self.assertEqual(count, 0)
                self.assertLessEqual(max(needs), lt.FLOOR_S)

    def test_a_flowing_source_with_short_pauses_takes_a_stall_as_trouble(self):
        # Pauses of about a second now and then make a few close pairs look like cycles;
        # they are not the rhythm while most of its pauses are minutes apart.
        for seed in range(3):
            for stall in (5.0, 10.0):
                with self.subTest(seed=seed, stall=stall):
                    pieces = flowing(
                        1100.0, seed, pauses=0.02, pause=(0.8, 1.2), stalls=[(400.0, stall)]
                    )
                    model = ArrivalModel()
                    count, needs = flips(model, pieces, 1100.0, every=50.0, start=350.0)
                    self.assertEqual(count, 0)
                    self.assertLessEqual(max(needs), lt.FLOOR_S)

    def test_cycles_are_forgotten_after_ten_minutes_of_flow(self):
        model = ArrivalModel()
        source = Source(model).run(downloaded(regular(15.0, 15.0, 300), 1, (10.2, 10.8)), 300)
        self.assertGreater(model._judged_by(), 10.0)  # the segments' cycle
        flow = [(300.0 + t, media) for t, media in flowing(lt.GAP_WINDOW_S + 60.0)]
        source.run(flow, 300.0 + lt.GAP_WINDOW_S + 60.0)
        self.assertLess(model._judged_by(), 2.0)  # the pieces' gap again

    def test_the_pace_takes_a_window_of_its_own_so_sags_do_not_flap_the_need(self):
        for name, pieces in (
            ("sag 0.5x 8 s every 90 s", flowing(900.0, sag=(90.0, 8.0, 0.5))),
            ("sag 0.4x 6 s every 60 s", flowing(900.0, 1, sag=(60.0, 6.0, 0.4), pauses=0.02)),
            ("raw TS bursts", spread_over_ticks(steady(900.0, 4), 4, (1, 2))),
        ):
            with self.subTest(source=name):
                count, _ = flips(ArrivalModel(), pieces, 900.0)
                self.assertLessEqual(count, 1)

    def test_a_download_at_the_border_of_fast_keeps_one_need(self):
        # 15 s segments downloading at 1.43-1.58x, 10 s ones at 1.32-1.72x: the channel's
        # median rate decides whether its downloads are cut (with hysteresis), not each one.
        for seg, took in ((15.0, (9.5, 10.5)), (10.0, (5.8, 7.6))):
            for seed in range(2):
                with self.subTest(seg=seg, seed=seed):
                    pieces = downloaded(regular(seg, seg, 920.0), seed, took)
                    count, _ = flips(ArrivalModel(), pieces, 920.0, start=200.0)
                    self.assertLessEqual(count, 2)

    def test_a_slow_download_is_not_trusted_before_its_idle_times_were_seen(self):
        # 15 s segments downloading at 1.45x come in ~1 s pieces: twenty of them before the
        # second idle time. Until three batches after a still edge were seen, the burst
        # stands in (no need of 2 s, and VLC running dry, half a minute in).
        model = ArrivalModel()
        source = Source(model).run(downloaded(regular(15.0, 15.0, 600.0), 0, (10.2, 10.8)), 40)
        self.assertGreaterEqual(model.seen, lt.WARM_GAPS)
        self.assertFalse(model.warm)
        self.assertEqual(model.need(15.0), 15.0)
        source.run(downloaded(regular(15.0, 15.0, 600.0), 0, (10.2, 10.8)), 600)
        self.assertTrue(model.warm)
        self.assertAlmostEqual(model.need(15.0), 5.5, delta=0.5)
        flow = ArrivalModel()  # a flow never stands still: twenty batches in a row will do
        Source(flow).run(flowing(30.0), 30.0)
        self.assertTrue(flow.warm)

    def test_lost_media_is_no_drift(self):
        # A segment the playlist slid past leaves the edge a segment lower for good: a step
        # down, not a pace off real time, so the dips stay on and the need stays put.
        rng, stalls, t = random.Random(3), [], 0.0
        while t < 3300.0:
            t += rng.uniform(120.0, 300.0)
            stalls.append((t, rng.uniform(6.0, 10.0)))
        cases = (
            ("HLS 15 s at 1.45x", lossy_hls(0, 3300.0, 15.0, (10.2, 10.8)), 3),
            ("HLS 6 s slow", lossy_hls(1, 3300.0, 6.0, (3.0, 5.5)), 3),
            (
                "flowing, 1-s pauses, lost stalls",
                flowing(3300.0, 2, pauses=0.08, pause=(0.8, 1.2), stalls=stalls),
                15,
            ),
        )
        for name, pieces, most in cases:
            with self.subTest(source=name):
                count, _ = flips(ArrivalModel(), pieces, 3300.0, every=30.0)
                self.assertLessEqual(count, most)

    def test_a_fast_channel_that_turns_to_a_flow_leaves_fast(self):
        model = ArrivalModel()
        source = Source(model).run(downloaded(regular(6.0, 6.0, 900.0), 0, (0.3, 1.5)), 900.0)
        model.add(source.now + TICK, source.edge)  # (the fast class needs runs of a second)
        until = 900.0 + 2 * lt.GAP_WINDOW_S + 60.0
        flow = [(900.0 + t, media) for t, media in flowing(until - 900.0)]
        source.run(
            [p for p in flow if p[0] <= 900.0 + lt.GAP_WINDOW_S + 60.0],
            900.0 + lt.GAP_WINDOW_S + 60.0,
        )
        self.assertIsNone(model._fast)  # its runs are gone: judged afresh, the flow cut again
        source.run([p for p in flow if p[0] > 900.0 + lt.GAP_WINDOW_S + 60.0], until)
        self.assertLessEqual(model.need(), lt.FLOOR_S)  # once the long batches left the window

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
        self.assertEqual(model.need(6.0), 6.0)  # a segment of mean lag: 2.0.2's footing
        # An HLS start: the live window's segments are fetched one after another, a second
        # apart, then the rhythm. The quick gaps alone would say 2 s.
        backlog = [(float(n), 6.0) for n in range(1, 6)]
        source = Source(model).run(backlog, 5.5)
        self.assertIsNotNone(model.fresh())
        self.assertLess(model.fresh(), 3.0)
        self.assertGreaterEqual(model.need(6.0), 6.0)
        source.run(regular(6.0, 6.0, 31, start=5.0), 31)
        self.assertGreaterEqual(model.need(6.0), 6.0)
        source.run(regular(6.0, 6.0, 600, start=29.0), 600)
        self.assertAlmostEqual(model.need(6.0), 6.0 * (1 - lt.MEDIA_SHARE) + lt.MARGIN_S, delta=0.3)
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
        for _ in range(4):  # one every ten minutes of playing (under the cap)
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
        tight.played(lt.HOLD_S + 1800.0)
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

    def test_quiet_playing_holds_it_then_lowers_it_steadily_back_to_nothing(self):
        prior = StarvationPrior()
        prior.starved()
        start, margins = prior.margin(), []
        for _ in range(120):  # two hours, a minute at a time
            prior.played(60.0)
            margins.append(prior.margin())
        held = int(lt.HOLD_S / 60)
        self.assertEqual(margins, sorted(margins, reverse=True))
        self.assertEqual(margins[held - 1], start)  # 20 minutes: it holds
        self.assertLess(margins[held + 30], start / 2)  # then half an hour halves it
        self.assertEqual(margins[held + 50], 0.0)  # and it is gone within the hour
        self.assertEqual(margins[-1], 0.0)

    def test_trouble_held_out_or_all_but_needed_holds_the_margin_again(self):
        for name in ("absorbed", "near_miss"):
            with self.subTest(told=name):
                prior = StarvationPrior()
                prior.starved()
                start = prior.margin()
                for _ in range(6):  # an hour and a half, trouble every quarter of an hour
                    prior.played(900.0)
                    prior.absorbed(0.3) if name == "absorbed" else prior.near_miss(0.45)
                peak = prior.margin()
                self.assertGreaterEqual(peak, start)
                prior.played(lt.HOLD_S)
                self.assertEqual(prior.margin(), peak)  # held to the end of the hold
                prior.played(60.0)
                self.assertLess(prior.margin(), peak)
        told = StarvationPrior()
        told.absorbed(0.3)  # no evidence of anything: no margin, nothing to hold
        self.assertEqual((told.margin(), told.hold), (0.0, 0.0))

    def test_only_trouble_the_margin_got_vlc_through_holds_it(self):
        # A stall that left VLC with more than the margin in force says nothing about the
        # margin (stay-live's stall bound trips on every late HLS reload): it fades as before.
        for headroom, holds in ((8.0, False), (2.0, False), (0.4, True)):
            with self.subTest(headroom=headroom):
                prior = StarvationPrior()
                prior.starved()
                start = prior.margin()  # 1.6 s
                for _ in range(180):  # six hours, a stall held out every two minutes
                    prior.played(120.0)
                    prior.absorbed(headroom)
                self.assertEqual(prior.margin() == start, holds)
                if not holds:
                    self.assertEqual(prior.margin(), 0.0)

    def test_trouble_every_ten_minutes_starves_less_than_two_zero_two(self):
        # Trouble every 10 min of playing that a margin under `needed` cannot hold out: the
        # starvations per hour once two hours passed, against 2.0.2's AutoTarget in the same
        # run (1.5 s more per trouble, 0.5 s less per 5 quiet minutes).
        for needed in (4.5, 5.0, 6.0):
            with self.subTest(needed=needed):
                old = starvations_two_zero_two(needed)
                self.assertEqual(old, 4.0)
                self.assertLessEqual(starvations_learned(needed, told=False), 2.5)
                self.assertLess(starvations_learned(needed, told=False), old)
                self.assertEqual(starvations_learned(needed, told=True), 0.0)  # it stops

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
                for _ in range(int(lt.HOLD_S / 60) + 120):  # the hold, then two hours
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

    def test_after_the_hold_an_hour_of_quiet_brings_it_back_to_the_need(self):
        target = LearnedTarget()
        Source(target.arrivals).run(six_and_twelve(600), 600)
        need = target.need()
        target.starved()
        raised = target.target()
        target.played(lt.HOLD_S)
        self.assertEqual(target.target(), raised)
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
        cap = max(lt.CAP_MIN_S, target.need() + lt.CAP_ABOVE_S)
        self.assertEqual(target.target(), cap)
        self.assertAlmostEqual(target.margin(), cap - target.need())
        self.assertAlmostEqual(target.room(), cap - target.need())

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
        self.assertAlmostEqual(again.target(), 7.0 * (1 - lt.MEDIA_SHARE) + lt.MARGIN_S, delta=0.3)
        self.assertAlmostEqual(again.learned().need, again.need(), places=2)

    def test_a_short_tune_of_a_segmented_source_keeps_its_burst_as_the_need(self):
        target = LearnedTarget()
        Source(target.arrivals).run(regular(10.0, 10.0, 60), 60)  # six bursts: not warm
        target.reported(10.0, 0.0)  # what the source reports ...
        self.assertIsNone(target.learned())  # (a moment: maybe a one-off first "burst")
        target.reported(10.0, 20.0)  # ... two bursts on
        self.assertEqual(target.learned().need, 10.0)
        self.assertEqual(LearnedTarget(target.learned()).target(), 10.0)
        target.reported(0.0, 21.0)  # dropped again: none kept
        self.assertIsNone(target.learned())

    def test_a_short_tune_keeps_the_need_it_was_given(self):
        again = LearnedTarget(Learned(need=13.0))
        Source(again.arrivals).run(regular(7.0, 7.0, 35), 35)
        self.assertEqual(again.learned().need, 13.0)
        fresh = LearnedTarget()
        Source(fresh.arrivals).run(regular(7.0, 7.0, 35), 35)
        fresh.starved()
        self.assertIsNone(fresh.learned().need)  # too few batches to be worth keeping

    def test_a_segmented_source_never_goes_under_its_burst(self):
        self.assertEqual(learned_target(2.0, 0.0), 2.5)  # steady: the floor
        self.assertEqual(learned_target(6.0, 0.0, burst=15.0), 15.0)  # a slow download
        self.assertEqual(learned_target(8.0, 0.0, burst=6.0), 8.0)  # uneven batches: above
        self.assertEqual(learned_target(6.0, 20.0), 10.0)  # today's cap ...
        self.assertEqual(learned_target(6.0, 20.0, burst=15.0), 17.0)  # ... burst + 2 s ...
        self.assertEqual(learned_target(16.0, 20.0, burst=15.0), 18.0)  # ... or need + 2 s
        target = LearnedTarget(Learned(need=6.0))  # 15 s segments downloading at 1.45x
        self.assertEqual(target.target(15.0), 15.0)
        self.assertFalse(target.adjusted(15.0))
        self.assertAlmostEqual(target.room(15.0), 2.0)
        target.starved(15.0)  # trouble adds the usual step, on top of the burst
        self.assertAlmostEqual(target.target(15.0), 15.0 + math.log2(3))
        self.assertTrue(target.adjusted(15.0))
        steady = LearnedTarget(Learned(need=6.0))
        steady.starved()  # a steady source keeps its whole learned target
        self.assertAlmostEqual(steady.target(), 6.0 + math.log2(3))

    def test_learned_only_a_segmented_source_has_its_need_and_a_segment_covered_when_dry(self):
        # The burst floor off (stage 4b's A/B): the need alone, the cap leaving room for a lost
        # segment (the burst + 1 s), and VLC running dry there covers a segment at once.
        self.assertEqual(learned_target(6.0, 0.0, burst=15.0, segment_floor=False), 6.0)
        self.assertEqual(learned_target(6.0, 20.0, segment_floor=False), 10.0)  # today's cap
        self.assertEqual(learned_target(6.0, 20.0, burst=15.0, segment_floor=False), 16.0)
        self.assertEqual(learned_target(16.0, 20.0, burst=15.0, segment_floor=False), 18.0)
        target = LearnedTarget(segment_floor=False)  # 15 s segments downloading at 1.45x
        Source(target.arrivals).run(downloaded(regular(15.0, 15.0, 600.0), 0, (10.2, 10.8)), 600)
        self.assertAlmostEqual(target.need(15.0), 6.0, delta=0.5)
        self.assertEqual(target.target(15.0), target.need(15.0))
        self.assertAlmostEqual(target.room(15.0), 16.0 - target.need(15.0))
        target.starved(15.0)
        self.assertAlmostEqual(target.target(15.0), 16.0)
        fast = LearnedTarget(segment_floor=False)  # whole segments: half of one, a second
        Source(fast.arrivals).run(downloaded(regular(15.0, 15.0, 600.0), 0, (0.5, 2.0)), 600)
        self.assertAlmostEqual(fast.need(15.0), 8.5, delta=0.5)
        fast.starved(15.0)
        self.assertAlmostEqual(fast.target(15.0), 16.0)  # a segment covered at once
        low = LearnedTarget(Learned(need=2.0), segment_floor=False)
        self.assertFalse(low.adjusted(15.0))  # at the floor: nothing learned raised it
        self.assertEqual(low.target(15.0), lt.FLOOR_S)

    def test_a_learned_only_segment_cover_is_remembered(self):
        # A learned-only margin may go beyond today's 7.5 s (a lost 30 s segment covered over a
        # need of 6 s): it is remembered and read back whole, the burst floor on or off.
        target = LearnedTarget(Learned(need=6.0), segment_floor=False)
        target.starved(30.0)
        self.assertAlmostEqual(target.target(30.0), 31.0)
        learned = target.learned()
        self.assertGreater(learned.events, 10_000.0)
        again = LearnedTarget(learned, segment_floor=False)
        self.assertAlmostEqual(again.target(30.0), 31.0, delta=0.01)
        floored = LearnedTarget(learned)  # the A/B switched back: the cap bounds it
        self.assertAlmostEqual(floored.target(30.0), 32.0)

    def test_a_stall_is_noted_and_counts_only_once_vlc_runs_dry(self):
        # Ruling R37 counted a segmented source's stall at once, as 2.0.2 did: an ordinary
        # HLS stall the target held through held it up 1.6-2 s for an hour (stage 4b review).
        # Any source's stall is noted; a dry run is trouble (`starved`), one the target held
        # out is `absorbed`.
        for target, burst in (
            (LearnedTarget(Learned(need=6.0)), 15.0),
            (LearnedTarget(Learned(need=6.0)), 0.0),
            (LearnedTarget(Learned(need=6.0), segment_floor=False), 15.0),
        ):
            with self.subTest(burst=burst, segment_floor=target.segment_floor):
                before = target.target(burst)
                target.stalled(burst)
                self.assertEqual(target.target(burst), before)
                self.assertTrue(target.arrivals._stalled)
                target.starved(burst)
                self.assertGreater(target.target(burst), before)

    def test_lost_segments_cost_no_more_than_in_two_zero_two(self):
        # HLS 15 s segments downloading at 1.45x, one lost every 3-5 minutes, VLC holding the
        # target: the first loss runs VLC dry once (with the burst floor its starvation's
        # margin, learned only a segment covered at once), the later ones are held out. (This
        # loop takes a raised target in at once, so R37's stall counted at its bound held
        # even the first out here, and 2.0.2's does; in the session a raise in the middle of a
        # stall brings no media.)
        for seed in range(2):
            pieces = lossy_hls(seed, 3600.0, 15.0, (10.2, 10.8))
            old, _ = closed_loop(pieces, 3600.0, 15.0, learned=False)
            self.assertEqual(old, [])
            for segment_floor, most in ((True, 1), (False, 1)):
                with self.subTest(seed=seed, segment_floor=segment_floor):
                    dry, targets = closed_loop(pieces, 3600.0, 15.0, segment_floor=segment_floor)
                    self.assertLessEqual(len(dry), most)
                    moves = sum(1 for a, b in zip(targets, targets[1:]) if abs(a - b) > 0.5)
                    self.assertLessEqual(moves, 3)
                    self.assertLess(sum(targets) / len(targets), 22.0)  # a segment, not more

    def test_the_burst_floor_is_a_switch_on_by_default(self):
        self.assertTrue(lt.SEGMENT_FLOOR)
        self.assertTrue(LearnedTarget().segment_floor)
        self.assertEqual(learned_target(6.0, 0.0, burst=15.0), 15.0)
        with mock.patch.object(lt, "SEGMENT_FLOOR", False):  # stage 4b's A/B flips it
            self.assertFalse(LearnedTarget().segment_floor)
            self.assertTrue(LearnedTarget(segment_floor=True).segment_floor)
            self.assertEqual(learned_target(6.0, 0.0, burst=15.0), 6.0)
            self.assertEqual(LearnedTarget(Learned(need=6.0)).target(15.0), 6.0)
        steady = LearnedTarget(Learned(need=6.0))  # a steady source: the same either way
        steady.starved()
        only = LearnedTarget(Learned(need=6.0), segment_floor=False)
        only.starved()
        self.assertEqual(steady.target(), only.target())

    def test_a_zap_away_and_back_keeps_the_hold(self):
        target = LearnedTarget()
        target.starved()
        start = target.target()
        target.played(300.0)  # five minutes, then a zap away
        learned = target.learned()
        self.assertAlmostEqual(learned.hold, lt.HOLD_S - 300.0)
        again = LearnedTarget(learned)  # and back
        again.played(lt.HOLD_S - 300.0 - 60.0)
        self.assertAlmostEqual(again.target(), start, delta=0.001)  # still held (rounded)
        again.played(120.0)
        self.assertLess(again.target(), start)  # the hold ended where it would have

    def test_near_misses_while_vlc_fills_at_the_start_do_not_count(self):
        target = LearnedTarget()
        source = Source(target.arrivals)
        source.run(regular(1.0, 1.0, 2.5), 2.5)  # two batches: still filling
        target.near_miss(0.1)
        self.assertIsNone(target.learned())
        source.run(regular(1.0, 1.0, 6.5, start=2.0), 6.5)
        self.assertGreaterEqual(target.arrivals.seen, lt.MIN_GAPS)
        target.near_miss(0.1)
        self.assertGreater(target.prior.margin(), 0.0)

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
