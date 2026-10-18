import math
import random
import unittest

from services.live.vlc_clock import VlcClock

TICK = 0.25
DT = 0.05


class Vlc:
    """A VLC playing at `rate` whose get_time() moves in steps (2.1 stage 2, measured).

    Its real position (`truth`) runs on while it plays; its reading is a timestamp the stream
    carries: on a sparse-PCR channel one every `every` seconds of media (2 s, the first
    `phase` into the opened segment), so the reading steps by 2000 ms every 2 s at 1x and
    stands still in between. With `quanta` the reading is instead updated at uneven moments
    (a steady channel: every 0.2-0.5 s, so some ticks see no change). `ms` is what
    get_time() returns: since the open, in whole milliseconds. With `speed` under 1 it plays
    slower than the rate asked (a crawl). `missing(k)` drops the k-th stamp (a step twice as
    long now and then); once VLC's real position passes `jump_at` the stamps jump by
    `jump_by` (a discontinuity in the stream's timestamps, not in what VLC plays).
    """

    def __init__(self, *, rate=1.0, every=2.0, phase=1.6, quanta=None, seed=1):
        self.rate, self.every, self.phase = rate, every, phase
        self.speed = 1.0
        self.missing = None
        self.jump_at, self.jump_by = math.inf, 0.0
        self.quanta = quanta
        self.rng = random.Random(seed)
        self.truth = 0.0
        self.reading = 0.0
        self.offset = 0.0  # added to the stamps (a clock that jumps or restarts)
        self.playing = True
        self.next_update = self.rng.uniform(*quanta) if quanta else None

    @property
    def ms(self):
        return max(0, round((self.reading + self.offset) * 1000))

    def run(self, seconds):
        for _ in range(round(seconds / DT)):
            if not self.playing:
                continue
            self.truth += self.rate * self.speed * DT
            if self.truth >= self.jump_at:
                self.offset += self.jump_by
                self.jump_at = math.inf
            if self.quanta is None:
                k = math.floor((self.truth - self.phase) / self.every)
                stamp = self.phase + k * self.every
                gone = self.missing is not None and self.missing(k)
                if stamp > self.reading + 1e-9 and self.truth >= self.phase and not gone:
                    self.reading = stamp
            else:
                self.next_update -= DT
                if self.next_update <= 1e-9:
                    self.reading = self.truth
                    self.next_update += self.rng.uniform(*self.quanta)


class Harness:
    """Ticks a VlcClock every 0.25 s on a Vlc, as LiveSession does."""

    def __init__(self, vlc, *, limit=math.inf):
        self.vlc, self.limit = vlc, limit
        self.breaks = []  # positions of the stream's discontinuities (as the helper flags them)
        self.now = 1000.0
        self.clock = VlcClock()
        self.clock.restart(self.now)
        self.last_ms = 0
        self.at_changes = []  # (position - VLC's reading) at the ticks where it changed
        self.errors = []  # (position - VLC's real position) at every tick

    def run(self, seconds, *, playing=True):
        for _ in range(round(seconds / TICK)):
            self.vlc.playing = playing
            self.vlc.run(TICK)
            self.now += TICK
            ms = self.vlc.ms
            position = self.clock.update(
                self.now, ms, playing, self.vlc.rate, limit=self.limit, at_break=self.at_break
            )
            if ms != self.last_ms and playing:
                self.at_changes.append(position - self.vlc.reading)
            self.last_ms = ms
            self.errors.append(position - self.vlc.truth)
        return self

    def at_break(self, low, high):
        return any(low <= b <= high for b in self.breaks)

    def until_step(self, limit=40):
        """Tick until VLC's reading changes (at most `limit` ticks)."""
        changes = len(self.at_changes)
        for _ in range(limit):
            self.run(TICK)
            if len(self.at_changes) > changes:
                return self
        raise AssertionError("VLC's reading did not change")


def mean(xs):
    return sum(xs) / len(xs)


class SparseStepTests(unittest.TestCase):
    """A sparse-PCR channel (measured): get_time steps by 2000 ms every 2 s, and the old rule
    took each step for a jump and counted a tick's wall time instead (0.09-0.16 of it)."""

    def test_two_second_steps_are_followed_at_every_step(self):
        for rate in (1.0, 1.10, 0.95):
            with self.subTest(rate=rate):
                run = Harness(Vlc(rate=rate)).run(10.0)
                run.at_changes.clear()
                run.errors.clear()
                run.run(60.0)
                self.assertGreater(len(run.at_changes), 25)
                self.assertLess(max(abs(e) for e in run.at_changes), 0.5)
                self.assertLess(abs(mean(run.at_changes)), 0.25)
                # Between steps it runs on with VLC's real position, which the stale
                # reading does not show.
                self.assertLess(max(abs(e) for e in run.errors), 0.5)

    def test_the_position_never_runs_backwards(self):
        run = Harness(Vlc(rate=1.03))
        positions = []
        for _ in range(240):
            run.run(TICK)
            positions.append(run.clock.position)
        self.assertTrue(all(b >= a for a, b in zip(positions, positions[1:])))

    def test_it_learns_the_steps_and_their_interval(self):
        clock = Harness(Vlc()).run(20.0).clock
        self.assertAlmostEqual(clock.step(), 2.0, delta=0.01)
        self.assertAlmostEqual(clock.interval(), 2.0, delta=0.01)


class SteadyQuantaTests(unittest.TestCase):
    def test_uneven_small_quanta_with_repeated_readings_are_followed(self):
        # A steady HLS channel (measured): 0.2-0.5 s quanta, about one tick in ten unchanged.
        run = Harness(Vlc(quanta=(0.2, 0.5), seed=4)).run(10.0)
        run.at_changes.clear()
        run.errors.clear()
        run.run(60.0)
        self.assertLess(max(abs(e) for e in run.at_changes), 0.25)
        self.assertLess(abs(mean(run.errors)), 0.1)
        self.assertLess(max(abs(e) for e in run.errors), 0.5)
        self.assertLessEqual(run.clock.interval(), 0.5)
        self.assertFalse(run.clock.frozen())


class JumpTests(unittest.TestCase):
    def test_a_real_jump_is_taken_within_two_steps(self):
        run = Harness(Vlc()).run(20.0)
        run.vlc.truth += 5.0  # VLC skipped 5 s (dropped what came too late, say)
        run.vlc.reading += 5.0
        run.vlc.run(0.0)
        run.until_step().until_step()
        self.assertLess(abs(run.at_changes[-1]), 0.5)
        run.run(10.0)
        self.assertLess(max(abs(e) for e in run.errors[-20:]), 0.5)

    def test_a_short_skip_ahead_is_taken_at_its_first_step(self):
        # A sparse-PCR channel run short of room (measured): every 30 s VLC dropped what came
        # too late and its clock stepped 1.6 s further than it played. A reading can lag VLC
        # by a tick at most, so a reading further ahead of the clock than that is no lag.
        run = Harness(Vlc()).run(20.0)
        run.vlc.truth += 1.6
        run.vlc.reading += 1.6
        run.until_step()
        self.assertLess(abs(run.at_changes[-1]), 0.3)

    def test_a_single_outlier_reading_barely_moves_it(self):
        reference = Harness(Vlc()).run(20.0)
        run = Harness(Vlc()).run(20.0)
        run.until_step()  # just after a step: the next is 2 s away
        reference.until_step()
        run.vlc.offset = 5.0  # one reading 5 s off ...
        run.run(TICK)
        run.vlc.offset = 0.0  # ... and the next ones as before
        run.run(4.0)
        reference.run(TICK + 4.0)
        self.assertLess(abs(run.clock.position - reference.clock.position), 0.5)

    def test_a_restarted_vlc_clock_is_followed_from_where_the_position_is(self):
        run = Harness(Vlc()).run(30.0)
        before = run.clock.position
        run.vlc.offset = -run.vlc.reading  # VLC's clock starts again from 0
        run.run(10.0)
        self.assertGreater(run.clock.position, before + 9.0)  # on at its rate, not back
        self.assertLess(max(abs(e) for e in run.errors[-8:]), 0.5)

    def test_a_stamp_beyond_the_buffered_media_is_no_skip(self):
        # The live edge is the most VLC can have played: a reading past it is the stream's
        # clock moving (a timestamp jump), not VLC skipping ahead.
        run = Harness(Vlc(), limit=40.0).run(20.0)
        run.vlc.offset = 30.0
        run.run(10.0)
        self.assertLess(max(abs(e) for e in run.errors[-8:]), 0.5)


class CadenceTests(unittest.TestCase):
    """Stage 2 review: the readings' cadence can change within a tune (fine quanta, then 2 s
    stamps: an encoder or an ad switch, or a few fine readings at a tune's start). The step
    learned from the fine ones held every other 2 s reading as a jump and never learned the
    new step: the position sawed up to 3.4 s behind VLC, and VLC's progress counted as the
    rate asked, so rate health could not see a crawl."""

    @staticmethod
    def to_two_second_stamps(vlc):
        vlc.quanta = None
        vlc.every, vlc.phase = 2.0, vlc.truth + 0.1

    def test_a_coarser_cadence_is_learned_within_a_few_readings(self):
        run = Harness(Vlc(quanta=(0.2, 0.4), seed=3)).run(30.0)
        self.to_two_second_stamps(run.vlc)
        run.until_step().until_step().until_step()
        run.errors.clear()
        run.run(60.0)
        self.assertLess(max(abs(e) for e in run.errors), 0.5)
        self.assertAlmostEqual(run.clock.step(), 2.0, delta=0.05)

    def test_a_few_fine_readings_at_the_start_do_not_hold_the_clock_back(self):
        run = Harness(Vlc(every=0.1, phase=0.0)).run(1.0)
        run.vlc.every, run.vlc.phase = 2.0, run.vlc.truth + 0.3
        run.until_step().until_step().until_step()
        run.errors.clear()
        run.run(60.0)
        self.assertLess(max(abs(e) for e in run.errors), 0.5)

    def test_after_a_cadence_change_vlcs_progress_still_shows_a_crawl(self):
        run = Harness(Vlc(rate=1.10, quanta=(0.2, 0.4), seed=3)).run(30.0)
        self.to_two_second_stamps(run.vlc)
        run.vlc.speed = 0.6
        run.until_step().until_step().until_step()
        start = run.clock.at_change
        run.run(30.0)
        end = run.clock.at_change
        self.assertLess((end.progress - start.progress) / (end.asked - start.asked), 0.7)

    def test_before_its_cadence_is_known_the_clock_stays_near_vlcs_reading(self):
        # A fresh tune whose VLC stands still at once: run on to the live edge, the clock
        # would read as starved there (and raise Auto's target).
        run = Harness(Vlc(), limit=30.0)
        run.vlc.speed = 0.0
        run.run(10.0)
        self.assertLessEqual(run.clock.position, 1.0 + 1e-9)


class BreakTests(unittest.TestCase):
    """Stage 2 review: a forward jump of the stream's timestamps (an HLS discontinuity, a
    raw-TS reconnect) moved the position as if VLC had skipped. The helper flags those
    breaks on its spans; a jump at one is the stream's clock: the position keeps counting."""

    def jump(self, by):
        run = Harness(Vlc(), limit=1000.0).run(20.0)
        run.vlc.jump_at = run.vlc.truth + 1.0
        run.vlc.jump_by = by
        run.breaks = [run.vlc.jump_at]
        run.errors.clear()
        run.run(20.0)
        return run

    def test_a_ten_second_stamp_jump_at_a_discontinuity_leaves_the_position(self):
        run = self.jump(10.0)
        self.assertLess(max(abs(e) for e in run.errors), 0.5)

    def test_a_short_stamp_jump_at_a_discontinuity_leaves_it_too(self):
        run = self.jump(2.5)  # under the 3 s a 2 s-step channel holds as a jump
        self.assertLess(max(abs(e) for e in run.errors), 0.5)

    def test_away_from_a_discontinuity_a_jump_is_vlc_skipping(self):
        run = Harness(Vlc(), limit=1000.0).run(20.0)
        run.vlc.truth += 5.0
        run.vlc.reading += 5.0
        run.breaks = [run.vlc.truth + 100.0]
        run.until_step().until_step()
        self.assertLess(abs(run.at_changes[-1]), 0.5)


class StandingStillTests(unittest.TestCase):
    def test_paused_buffering_or_opening_it_stands_still(self):
        run = Harness(Vlc()).run(20.0)
        position = run.clock.position
        run.vlc.reading += 2.0  # VLC's demux reads on while it waits, say
        run.run(5.0, playing=False)
        self.assertEqual(run.clock.position, position)

    def test_a_new_open_starts_again_at_its_segment(self):
        run = Harness(Vlc()).run(20.0)
        run.clock.restart(run.now)
        self.assertEqual(run.clock.position, 0.0)
        run.vlc = Vlc()  # the new stream's clock starts at 0
        run.last_ms = 0
        run.errors.clear()
        run.run(10.0)
        self.assertLess(max(abs(e) for e in run.errors[-20:]), 0.5)

    def test_frozen_only_when_the_steps_stop_well_past_their_interval(self):
        run = Harness(Vlc()).run(20.0)
        frozen = []
        for _ in range(40):
            run.run(TICK)
            frozen.append(run.clock.frozen())
        self.assertFalse(any(frozen))  # never between 2 s steps
        self.assertAlmostEqual(run.clock.hold(), 1.5 * 2.0 + 0.25)
        run.until_step()
        run.vlc.speed = 0.0  # VLC runs dry: its clock stands still while it says Playing
        position = run.clock.position
        held = 0
        while not run.clock.frozen() and held < 40:
            run.run(TICK)
            held += 1
        self.assertAlmostEqual(held * TICK, 3.25, delta=TICK + 1e-9)
        self.assertGreater(run.clock.position, position)  # it ran on until then ...
        stood = run.clock.position
        run.run(5.0)
        self.assertEqual(run.clock.position, stood)  # ... and stands still from there
        # Never further than one and a half steps past VLC's last reading.
        self.assertLessEqual(stood, run.vlc.reading + 1.5 * 2.0 + 1e-6)

    def test_frozen_needs_half_a_second_at_least(self):
        run = Harness(Vlc(quanta=(0.05, 0.06))).run(5.0)  # changes at every tick
        self.assertEqual(run.clock.hold(), max(0.5, 1.5 * run.clock.interval() + 0.25))
        self.assertGreaterEqual(run.clock.hold(), 0.5)


class LimitTests(unittest.TestCase):
    def test_it_never_runs_past_the_buffered_media(self):
        run = Harness(Vlc(), limit=21.0).run(20.0)
        run.vlc.speed = 0.0  # VLC met the live edge and waits there
        run.run(5.0)
        self.assertLessEqual(run.clock.position, 21.0)


class ProgressTests(unittest.TestCase):
    """VLC's own progress, in its steps (rate health and the crawl check judge it)."""

    def test_progress_counts_vlcs_steps_and_asked_the_rate(self):
        run = Harness(Vlc(rate=1.10)).run(30.0)
        clock = run.clock
        self.assertAlmostEqual(clock.at_change.progress, run.vlc.reading, delta=1e-3)
        self.assertAlmostEqual(clock.asked, 30.0 * 1.10, delta=1e-6)
        self.assertAlmostEqual(clock.played, 30.0, delta=1e-6)

    def test_a_crawling_vlc_shows_in_its_progress_not_hidden_by_the_clock(self):
        # Asked for 1.10x, VLC plays 0.25 s of media every two seconds (2.0.2, measured).
        run = Harness(Vlc(rate=1.10, every=0.25, phase=0.0)).run(20.0)
        start = run.clock.at_change
        run.vlc.speed = 0.125
        run.run(20.0)
        end = run.clock.at_change
        share = (end.progress - start.progress) / (end.asked - start.asked)
        self.assertLess(share, 0.3)
        # And the position goes with VLC (within the quantum its long intervals give it),
        # not on at the rate asked (19 s ahead by now).
        self.assertLess(max(run.errors[-40:]), 3.0)


if __name__ == "__main__":
    unittest.main()
