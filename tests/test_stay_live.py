import random
import unittest

from services.live.stay_live import (
    AUTO,
    TARGETS,
    AutoTarget,
    Decision,
    LagFilter,
    Mode,
    RateHealth,
    SettledLag,
    StayLive,
)

T = TARGETS["normal"]
R = T + 20  # beyond this a lag reopens at live
TICK = 0.25


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def tick(self, seconds=TICK):
        self.now += seconds
        return self.now


def drive(stay, lag, seconds, *, clock, drift=0.0, noise=None, buffering=False):
    """Play `seconds` at stay-live's rates: the lag moves by `drift` per second (the source's
    own) less what the rate takes, plus `noise(t)` (the estimate's wander). Returns the lag
    without the noise, and the rates decided per tick."""
    rates = []
    for _ in range(round(seconds / TICK)):
        now = clock.tick()
        seen = lag + (noise(now) if noise is not None else 0.0)
        decision = stay.update(seen, buffering, now=now)
        rates.append(decision.rate)
        if not buffering:
            lag += (drift - (decision.rate - 1.0)) * TICK
    return lag, rates


def changes(rates, start=1.0):
    """The rate changes between 1x and another rate (a ramp's steps count once each way)."""
    count, previous = 0, start
    for rate in rates:
        if (previous == 1.0) != (rate == 1.0):
            count += 1
        previous = rate
    return count


def live(target=T, *, burst=0.0, spread=0.0, latency="normal"):
    stay = StayLive(latency)
    stay.target, stay.burst, stay.spread = target, burst, spread
    return stay


class ModeTests(unittest.TestCase):
    """What stay-live does outside LIVE, and the reopen at live: as in 2.0."""

    def test_shifted(self):
        sl, clock = StayLive(), Clock()
        sl.user_paused()
        self.assertIs(sl.mode, Mode.SHIFTED)
        self.assertEqual(sl.update(100, False, now=clock.tick()), Decision(1.0))
        sl.user_speed(1.5)
        self.assertIs(sl.mode, Mode.CATCHING_UP)
        sl.user_speed(0.5)
        self.assertIs(sl.mode, Mode.SHIFTED)
        self.assertEqual(sl.update(100, False, now=clock.tick()), Decision(0.5))

    def test_pause_from_catching_up_is_shifted_at_normal_speed(self):
        sl, clock = StayLive(), Clock()
        sl.user_paused()
        sl.user_speed(1.5)
        sl.user_paused()
        self.assertIs(sl.mode, Mode.SHIFTED)
        self.assertEqual(sl.update(100, False, now=clock.tick()), Decision(1.0))

    def test_buffering_in_shifted(self):
        sl, clock = StayLive(), Clock()
        sl.user_paused()
        self.assertEqual(sl.update(50, True, now=clock.tick()), Decision(1.0))
        sl.user_speed(0.5)
        self.assertEqual(sl.update(50, True, now=clock.tick()), Decision(0.5))

    def test_catching_up_returns_live_within_settle(self):
        sl, clock = StayLive(), Clock()
        sl.user_paused()
        sl.user_speed(1.5)
        S = T + 0.2
        self.assertEqual(sl.update(S + 1e-6, False, now=clock.tick()), Decision(1.5))
        self.assertEqual(sl.update(S, True, now=clock.tick()), Decision(1.5))  # buffering
        self.assertIs(sl.mode, Mode.CATCHING_UP)
        self.assertEqual(sl.update(100, False, now=clock.tick()), Decision(1.5))  # no reopen
        self.assertEqual(sl.update(S, False, now=clock.tick()), Decision(1.0))
        self.assertIs(sl.mode, Mode.LIVE)

    def test_reopen_is_edge_triggered(self):
        sl, clock = StayLive(), Clock()
        flags = [sl.update(R + 1, False, now=clock.tick()) for _ in range(3)]
        self.assertEqual([d.reopen_live for d in flags], [True, False, False])
        self.assertTrue(all(d.rate == 1.0 for d in flags))
        sl.user_go_live()
        self.assertTrue(sl.update(R + 1, False, now=clock.tick()).reopen_live)

    def test_buffering_never_reopens_and_keeps_the_rate(self):
        sl, clock = live(), Clock()
        self.assertEqual(sl.update(R + 100, True, now=clock.tick()), Decision(1.0))

    def test_user_speed_rejects_non_positive(self):
        sl = StayLive()
        sl.user_paused()
        for rate in (0, -1.0):
            with self.assertRaises(ValueError):
                sl.user_speed(rate)

    def test_seek_back_enters_shifted(self):
        sl = StayLive()
        sl.user_seeked_back()
        self.assertIs(sl.mode, Mode.SHIFTED)

    def test_go_live_and_speed_ignored_in_live(self):
        sl, clock = StayLive(), Clock()
        sl.user_paused()
        sl.user_speed(1.5)
        sl.user_go_live()
        self.assertIs(sl.mode, Mode.LIVE)
        sl.user_speed(2.0)
        self.assertIs(sl.mode, Mode.LIVE)
        self.assertEqual(sl.update(0, False, now=clock.tick()), Decision(1.0))

    def test_set_latency(self):
        sl = StayLive("low")
        self.assertEqual(sl.target, 1.0)
        sl.set_latency("stable")
        self.assertEqual(sl.target, 6.0)
        with self.assertRaises(KeyError):
            sl.set_latency("bogus")


class SteeringTests(unittest.TestCase):
    """2.1 (the owner: the public HLS channel flapped between 1x and 1.05x every few seconds, and each
    change crackled): the rate changes only for a lasting drift outside a dead band, once,
    gently, ramped and sized to end at the target; at most one such nudge per 5 minutes."""

    def test_a_lag_wandering_inside_the_band_never_changes_the_rate(self):
        rng = random.Random(7)
        stay, clock = live(7.0, burst=7.0), Clock()
        _, rates = drive(stay, 7.0, 600.0, clock=clock, noise=lambda t: rng.uniform(-0.95, 0.95))
        self.assertEqual(set(rates), {1.0})

    def test_a_short_excursion_outside_the_band_changes_nothing(self):
        stay, clock = live(), Clock()
        for _ in range(10):  # 14 s out, 10 s back in: never 15 s in a row
            _, out = drive(stay, T + 1.5, 14.0, clock=clock)
            _, back = drive(stay, T, 10.0, clock=clock)
            self.assertEqual(set(out + back), {1.0})

    def test_a_lasting_drift_gets_one_ramped_nudge_that_ends_at_the_target(self):
        stay, clock = live(), Clock()
        lag, rates = drive(stay, T + 2.0, 200.0, clock=clock)
        self.assertEqual(rates[: round(15.0 / TICK) - 1], [1.0] * (round(15.0 / TICK) - 1))
        steps = [rate for rate in rates if rate != 1.0]
        self.assertEqual(steps[:3], [1.01, 1.02, 1.03])  # ramped in ...
        self.assertEqual(steps[-2:], [1.02, 1.01])  # ... and out
        self.assertEqual(max(rates), 1.03)
        self.assertEqual(changes(rates), 2)  # one nudge: to 1.03 and back
        self.assertAlmostEqual(lag, T, delta=0.1)  # sized to end at the target
        self.assertEqual(rates[-1], 1.0)
        self.assertEqual(stay.nudge, 1.0)

    def test_ahead_of_the_band_the_nudge_adds_room_at_0_97(self):
        stay, clock = live(6.0), Clock()
        lag, rates = drive(stay, 4.0, 200.0, clock=clock)
        self.assertEqual(min(rates), 0.97)
        self.assertEqual(changes(rates), 2)
        self.assertAlmostEqual(lag, 6.0, delta=0.1)

    def test_the_nudge_rate_is_reported_while_it_ramps(self):
        stay, clock = live(), Clock()
        drive(stay, T + 2.0, 15.5, clock=clock)
        self.assertEqual(stay.nudge, 1.03)

    def test_far_behind_the_nudge_is_1_10_after_a_few_seconds(self):
        stay, clock = live(), Clock()
        lag, rates = drive(stay, T + 12.0, 200.0, clock=clock)
        first = next(i for i, rate in enumerate(rates) if rate != 1.0)
        self.assertLessEqual(first * TICK, 3.25)
        self.assertEqual(max(rates), 1.10)
        self.assertEqual(changes(rates), 2)
        self.assertAlmostEqual(lag, T, delta=0.15)

    def test_one_nudge_per_five_minutes_then_the_band_widens(self):
        stay, clock = live(), Clock()
        lag, rates = drive(stay, T + 2.0, 120.0, clock=clock)  # one nudge, done
        self.assertEqual(changes(rates), 2)
        ended = clock.now - next(i for i, r in enumerate(reversed(rates)) if r != 1.0) * TICK
        lag, rates = drive(stay, T + 2.0, 150.0, clock=clock)  # drifted again: tolerated
        self.assertEqual(set(rates), {1.0})
        self.assertGreater(stay.band(1), 2.0)  # widened to the lag it tolerates
        lag, rates = drive(stay, lag, (ended + 300.0) - clock.now - TICK, clock=clock)
        self.assertEqual(set(rates), {1.0})  # still within the budget's 5 minutes
        lag, rates = drive(stay, lag, 120.0, clock=clock)  # then the next nudge
        self.assertEqual(changes(rates), 2)
        self.assertAlmostEqual(lag, T, delta=0.1)

    def test_a_far_lag_is_not_held_back_by_the_budget(self):
        stay, clock = live(), Clock()
        drive(stay, T + 2.0, 120.0, clock=clock)  # the budget is spent
        lag, rates = drive(stay, T + 12.0, 150.0, clock=clock)
        self.assertEqual(max(rates), 1.10)
        self.assertAlmostEqual(lag, T, delta=0.15)

    def test_buffering_and_reopens_start_the_count_again(self):
        stay, clock = live(), Clock()
        for step in ("buffering", "absorb", "go live"):
            with self.subTest(step=step):
                _, before = drive(stay, T + 2.0, 12.0, clock=clock)
                if step == "buffering":
                    drive(stay, T + 2.0, TICK, clock=clock, buffering=True)
                elif step == "absorb":
                    stay.absorb()  # the caller reopened the stream
                else:
                    stay.user_go_live()
                _, after = drive(stay, T + 2.0, 12.0, clock=clock)
                self.assertEqual(set(before + after), {1.0})  # a step is not chased
                _, rest = drive(stay, T, 30.0, clock=clock)  # back inside for a while
        _, rates = drive(stay, T + 2.0, 16.0, clock=clock)  # 15 s in a row: the nudge
        self.assertIn(1.01, rates)

    def test_a_nudge_ends_when_a_step_takes_the_lag_to_the_target(self):
        stay, clock = live(), Clock()
        _, rates = drive(stay, T + 3.0, 20.0, clock=clock)
        self.assertEqual(rates[-1], 1.03)
        _, rates = drive(stay, T - 0.5, 2.0, clock=clock)  # a reopen landed at the target
        self.assertEqual(rates[:3], [1.02, 1.01, 1.0])
        self.assertEqual(stay.nudge, 1.0)

    def test_a_nudge_ramps_out_once_whatever_the_lag_does_meanwhile(self):
        # Measured on the public HLS channel (2.1): the estimate hovered around the target as a 0.95x nudge
        # ended, and the ramp went 0.95, 0.96, 0.95, 0.96 ... before 1x.
        stay, clock = live(7.0, burst=7.0), Clock()
        _, rates = drive(stay, 3.0, 6.0, clock=clock)  # short of room: 0.95x
        self.assertEqual((rates[-1], stay.nudge), (0.95, 0.95))
        rates = []
        for k in range(12):  # at the target, give or take a few hundredths
            rates.append(
                stay.update(7.0 + (-0.05 if k % 2 else 0.05), False, now=clock.tick()).rate
            )
        self.assertEqual(rates[:5], [0.96, 0.97, 0.98, 0.99, 1.0])
        self.assertEqual(changes(rates, start=0.95), 1)

    def test_buffering_holds_a_nudge_where_it_was(self):
        stay, clock = live(), Clock()
        _, rates = drive(stay, T + 3.0, 20.0, clock=clock)
        _, held = drive(stay, T + 3.0, 5.0, clock=clock, buffering=True)
        self.assertEqual(set(held), {1.03})
        lag, rates = drive(stay, T + 3.0 - 0.1, 150.0, clock=clock)
        self.assertAlmostEqual(lag, T, delta=0.15)

    def test_the_band_follows_the_estimates_spread_and_behind_a_quarter_burst(self):
        stay = live(7.0, burst=7.0)
        self.assertEqual((stay.band(-1), stay.band(1)), (1.0, 1.0 + 7.0 / 4))
        stay.spread = 1.2
        self.assertAlmostEqual(stay.band(-1), 1.8)
        self.assertAlmostEqual(stay.band(1), 1.8 + 7.0 / 4)
        steady = live()
        self.assertEqual((steady.band(-1), steady.band(1)), (1.0, 1.0))

    def test_a_wide_band_holds_back_a_nudge(self):
        stay, clock = live(spread=2.0), Clock()  # the estimate wanders +-2 s by itself
        _, rates = drive(stay, T + 2.5, 120.0, clock=clock)
        self.assertEqual(set(rates), {1.0})
        _, rates = drive(stay, T + 3.5, 160.0, clock=clock)
        self.assertEqual(changes(rates), 2)

    def test_a_bursty_source_short_of_room_slows_to_0_95_at_once(self):
        # 2.0.1, the owner's channel: 10 s bursts, the player meets the edge before each
        # one. Less than half a second of room left when a burst lands: 0.95x after 3 s,
        # whatever the budget, until the target.
        stay, clock = live(10.0, burst=10.0), Clock()
        _, rates = drive(stay, 15.0, 200.0, clock=clock)  # a nudge spends the budget
        self.assertEqual(changes(rates), 2)
        lag, rates = drive(stay, 5.2, 200.0, clock=clock)
        first = next(i for i, rate in enumerate(rates) if rate != 1.0)
        self.assertLessEqual(first * TICK, 3.25)
        self.assertEqual(min(rates), 0.95)
        self.assertEqual(changes(rates), 2)
        self.assertAlmostEqual(lag, 10.0, delta=0.15)

    def test_with_room_enough_a_bursty_source_waits_for_its_budget(self):
        stay, clock = live(10.0, burst=10.0), Clock()
        _, rates = drive(stay, 15.0, 200.0, clock=clock)  # a nudge spends the budget
        self.assertEqual(changes(rates), 2)
        _, rates = drive(stay, 7.0, 90.0, clock=clock)  # 2 s of room: no hurry
        self.assertEqual(set(rates), {1.0})

    def test_a_steady_source_at_the_live_edge_is_short_of_room(self):
        # After a stall, say: VLC plays right at the edge, and any hiccup runs it dry.
        stay, clock = live(), Clock()
        drive(stay, 3.5, 200.0, clock=clock)  # a nudge spends the budget
        lag, rates = drive(stay, 0.2, 100.0, clock=clock)
        self.assertEqual((min(rates), changes(rates)), (0.95, 2))
        self.assertAlmostEqual(lag, T, delta=0.15)

    def test_low_latency_is_never_short_of_room(self):
        stay, clock = live(TARGETS["low"]), Clock()
        _, rates = drive(stay, 0.2, 60.0, clock=clock)  # within its band: as wanted
        self.assertEqual(set(rates), {1.0})

    def test_the_rate_never_leaves_0_95_to_1_10(self):
        rng = random.Random(3)
        for _ in range(50):
            stay, clock = live(rng.uniform(1.0, 12.0), burst=rng.choice((0.0, 6.0, 10.0))), Clock()
            lag = rng.uniform(0.0, 30.0)
            _, rates = drive(stay, lag, 60.0, clock=clock, drift=rng.uniform(-0.05, 0.05))
            self.assertTrue(all(0.95 <= rate <= 1.10 for rate in rates))


class RateLockTests(unittest.TestCase):
    """2.0.2: on some channels VLC cannot play at any rate but 1x (it crawls). Once that is
    seen, stay-live keeps 1x for the channel; only a lag far beyond the target goes live."""

    def test_a_locked_rate_stays_at_normal_speed_whatever_the_lag(self):
        for burst in (0.0, 10.0):
            with self.subTest(burst=burst):
                stay, clock = live(max(T, burst), burst=burst), Clock()
                _, rates = drive(stay, stay.target + 12, 5.0, clock=clock)
                self.assertNotEqual(rates[-1], 1.0)  # nudging
                stay.lock_rate()
                self.assertTrue(stay.rate_locked)
                self.assertEqual(stay.nudge, 1.0)
                for lag in (0.0, 1.0, stay.target + 1, stay.target + 5, stay.target + 19):
                    _, rates = drive(stay, lag, 30.0, clock=clock)
                    self.assertEqual(set(rates), {1.0})

    def test_a_locked_rate_still_goes_live_once_when_far_behind(self):
        stay, clock = StayLive("normal"), Clock()
        stay.lock_rate()
        flags = [stay.update(R + 1, False, now=clock.tick()) for _ in range(3)]
        self.assertEqual(flags, [Decision(1.0, True), Decision(1.0), Decision(1.0)])
        stay.user_go_live()
        self.assertTrue(stay.rate_locked)  # for the channel, not until Go live
        self.assertEqual(stay.update(T + 5, False, now=clock.tick()), Decision(1.0))
        self.assertTrue(stay.update(R + 1, False, now=clock.tick()).reopen_live)

    def test_a_speed_the_user_chooses_still_applies(self):
        stay, clock = StayLive("normal"), Clock()
        stay.lock_rate()
        stay.user_paused()
        stay.user_speed(1.5)
        self.assertEqual(stay.update(100.0, False, now=clock.tick()), Decision(1.5))


class LagFilterTests(unittest.TestCase):
    """The mean lag over a source's arrival rhythm (2.1)."""

    @staticmethod
    def run_source(arrivals, seconds, *, burst, position=0.0, rate=1.0, start=0.0):
        """`arrivals`: (wall, media) pairs; a player at `rate` from `position`. Returns per tick
        (wall, lag, spread, filter) as LiveSession feeds the filter."""
        flt = LagFilter()
        edge, moved, i = 0.0, start, 0
        arrivals = sorted(arrivals)
        out = []
        t = start
        while t < start + seconds:
            t = round(t + TICK, 6)
            while i < len(arrivals) and arrivals[i][0] <= t:
                edge += arrivals[i][1]
                moved = arrivals[i][0]
                i += 1
            flt.add(t, edge, burst, t - moved)
            position += rate * TICK
            out.append((t, flt.lag(t, position), flt.spread(), flt))
        return out

    def test_regular_bursts_give_a_steady_mean_from_the_first_burst(self):
        arrivals = [(k * 6.0, 6.0 if k else 12.0) for k in range(60)]
        rows = self.run_source(arrivals, 300.0, burst=6.0)
        lags = [lag for _, lag, _, _ in rows[8:]]
        self.assertLess(max(lags) - min(lags), 0.3)
        self.assertLess(max(spread for _, _, spread, _ in rows[8:]), 0.2)

    def test_uneven_arrivals_average_out(self):
        # the public HLS channel (measured): every 7 s, 6 s of media, every sixth time 12 s.
        arrivals = [(k * 7.0, 12.0 if k % 6 == 5 or k == 0 else 6.0) for k in range(45)]
        rows = self.run_source(arrivals, 300.0, burst=7.0)
        late = [lag for t, lag, _, _ in rows if t > 120.0]
        self.assertLess(max(late) - min(late), 0.6)  # the raw lag saws by 6-12 s
        self.assertLess(max(spread for t, _, spread, _ in rows if t > 150.0), 1.0 / 1.5)

    def test_the_players_side_shows_at_once(self):
        flt, t = LagFilter(), 0.0
        for k in range(200):
            t = k * TICK
            flt.add(t, 10.0 + t, 0.0, 0.0)
        before = flt.lag(t, t)
        self.assertAlmostEqual(flt.lag(t, t - 2.0), before + 2.0)  # VLC stood still 2 s

    def test_a_backlog_at_once_is_a_step_taken_in_at_once(self):
        flt, t = LagFilter(), 0.0
        for k in range(200):
            t = k * TICK
            flt.add(t, 10.0 + t, 0.0, 0.0)
        flt.add(t + TICK, 40.0 + t + TICK, 0.0, 0.0)  # 30 s more arrived at once
        self.assertAlmostEqual(flt.lag(t + TICK, t + TICK), 40.0, delta=0.01)

    def test_a_steady_source_is_its_lag(self):
        flt, t = LagFilter(), 0.0
        for k in range(100):
            t = k * TICK
            flt.add(t, 5.0 + t, 0.0, 0.0)
        self.assertAlmostEqual(flt.lag(t, t), 5.0)
        self.assertEqual(flt.window, 10.0)

    def test_nothing_before_the_first_edge(self):
        self.assertIsNone(LagFilter().lag(1.0, 0.0))

    def test_the_window_covers_a_dozen_bursts(self):
        flt = LagFilter()
        flt.add(0.0, 0.0, 7.0, 0.0)
        self.assertEqual(flt.window, 84.0)
        flt = LagFilter()
        flt.add(0.0, 0.0, 12.0, 0.0)
        self.assertEqual(flt.window, 90.0)

    def test_reset_forgets_the_edges(self):
        flt = LagFilter()
        flt.add(0.0, 5.0, 0.0, 0.0)
        flt.reset()
        self.assertIsNone(flt.lag(1.0, 0.0))


class BurstClassTests(unittest.TestCase):
    """The filter's burst, with hysteresis (2.1): the helper's raw TS meter may report a
    burst for a moment and drop it (one provider: 15 times in 5 minutes), which moved the
    target and the lag formula back and forth in 2.0.x."""

    def feed(self, series, seconds, flt=None):
        """`series(t)` is the burst reported at `t`; the classified burst per tick."""
        flt = flt or LagFilter()
        out = []
        for k in range(round(seconds / TICK)):
            t = k * TICK
            flt.add(t, t, series(t), 0.0)
            out.append(flt.burst)
        return flt, out

    def test_the_first_report_counts_at_once(self):
        flt, out = self.feed(lambda t: 6.0, 1.0)
        self.assertEqual(out[0], 6.0)

    def test_a_burst_reported_for_a_moment_is_none(self):
        # Reported for 3 s at a time, every 20 s (the measured raw TS source's blips).
        _, out = self.feed(lambda t: 3.2 if 5.0 <= t % 20.0 < 8.0 else 0.0, 300.0)
        self.assertEqual(set(out), {0.0})

    def test_a_lasting_burst_counts_after_two_bursts_and_ten_seconds(self):
        _, out = self.feed(lambda t: 0.0 if t < 10.0 else 10.0, 60.0)
        on = out.index(10.0) * TICK
        self.assertAlmostEqual(on, 10.0 + 20.0, delta=TICK)
        _, out = self.feed(lambda t: 0.0 if t < 10.0 else 3.0, 60.0)
        self.assertAlmostEqual(out.index(3.0) * TICK, 20.0, delta=TICK)

    def test_it_drops_after_half_a_minute_without(self):
        _, out = self.feed(lambda t: 6.0 if t < 60.0 else 0.0, 120.0)
        self.assertAlmostEqual(out.index(0.0) * TICK, 90.0, delta=TICK)

    def test_gaps_shorter_than_half_a_minute_keep_it(self):
        _, out = self.feed(lambda t: 0.0 if t % 40.0 > 25.0 else 3.2, 300.0)
        self.assertEqual(set(out[round(40.0 / TICK) :]), {3.2})

    def test_small_changes_leave_it_as_it_was(self):
        # An HLS source's target duration (7.0) and the meter's 7.1 from its arrivals.
        _, out = self.feed(lambda t: 7.1 if int(t) % 2 else 7.0, 120.0)
        self.assertEqual(set(out), {7.0})


class SettledLagTests(unittest.TestCase):
    """The lag as shown (2.1): 2.0 printed every tick's estimate with a decimal, 90 changes a
    minute on the public HLS channel."""

    def test_half_seconds(self):
        self.assertEqual(SettledLag().update(0.0, 7.24), 7.0)
        self.assertEqual(SettledLag().update(0.0, 7.26), 7.5)
        self.assertEqual(SettledLag().update(0.0, -3.0), 0.0)

    def test_a_wandering_lag_shows_one_value(self):
        rng = random.Random(5)
        shown, values = SettledLag(), set()
        for k in range(2400):  # 10 minutes of ticks within half a second of 7
            values.add(shown.update(k * TICK, 7.0 + rng.uniform(-0.49, 0.49)))
        self.assertEqual(values, {7.0})

    def test_a_real_change_shows_after_three_seconds(self):
        shown = SettledLag()
        shown.update(0.0, 7.0)
        for k in range(1, 13):  # first seen at 0.25 s: shown 3 s later
            self.assertEqual(shown.update(k * TICK, 8.2), 7.0)
        self.assertEqual(shown.update(13 * TICK, 8.2), 8.0)

    def test_a_brief_change_never_shows(self):
        shown = SettledLag()
        shown.update(0.0, 7.0)
        for k in range(1, 400):
            lag = 8.0 if k % 16 < 8 else 7.0  # 2 s away, 2 s back
            self.assertEqual(shown.update(k * TICK, lag), 7.0)

    def test_reset_shows_the_next_lag_at_once(self):
        shown = SettledLag()
        shown.update(0.0, 7.0)
        shown.reset()
        self.assertEqual(shown.update(TICK, 3.1), 3.0)

    def test_a_slow_drift_changes_the_text_rarely(self):
        shown, values, t = SettledLag(), [], 0.0
        for k in range(1200):  # 5 minutes, 7 s down to 4 s
            t = k * TICK
            values.append(shown.update(t, 7.0 - 3.0 * t / 300.0))
        changed = sum(1 for a, b in zip(values, values[1:]) if a != b)
        self.assertLessEqual(changed, 6)


class RateHealthTests(unittest.TestCase):
    """VLC's progress at a rate other than 1x, against rate x wall time over 3 s."""

    def feed(self, health, share, rate=1.10, seconds=3.0, step=0.25):
        """`seconds` of ticks in which VLC played `share` of what `rate` asks; the verdicts."""
        return [health.add(step, share * rate * step, rate) for _ in range(round(seconds / step))]

    def test_a_crawl_is_seen_once_three_seconds_were_counted(self):
        health = RateHealth()
        verdicts = self.feed(health, 0.1)
        self.assertEqual(verdicts, [False] * 11 + [True])
        self.assertAlmostEqual(health.share, 0.1)

    def test_a_player_that_keeps_up_is_healthy(self):
        for rate in (0.95, 1.05, 1.10):
            with self.subTest(rate=rate):
                health = RateHealth()
                self.assertFalse(any(self.feed(health, 1.0, rate=rate, seconds=30.0)))

    def test_the_threshold_is_seventy_percent(self):
        self.assertFalse(any(self.feed(RateHealth(), 0.71)))
        self.assertTrue(self.feed(RateHealth(), 0.69)[-1])

    def test_each_window_is_judged_afresh(self):
        health = RateHealth()
        self.assertFalse(any(self.feed(health, 1.0)))  # a healthy window
        self.assertEqual(self.feed(health, 0.2)[-1], True)  # not averaged with the one before


class ConsecutiveWindowTests(unittest.TestCase):
    """The HLS crawl verdict needs two bad windows in a row (2.0.2 review, I3)."""

    def window(self, health, share, seconds=8.0):
        return [health.add(0.25, share * 0.25, 1.0) for _ in range(round(seconds / 0.25))]

    def test_one_bad_window_is_not_enough(self):
        health = RateHealth(8.0, windows=2)
        self.assertFalse(any(self.window(health, 0.1)))
        self.assertTrue(self.window(health, 0.1)[-1])  # the second in a row

    def test_a_healthy_window_between_starts_the_count_again(self):
        health = RateHealth(8.0, windows=2)
        for share in (0.1, 1.0, 0.1):
            self.assertFalse(any(self.window(health, share)))
        self.assertTrue(self.window(health, 0.1)[-1])

    def test_a_new_window_after_an_open_keeps_the_count(self):
        health = RateHealth(8.0, windows=2)
        self.window(health, 0.1)
        self.window(health, 0.1, seconds=4.0)
        health.reset()  # the stream was opened again: this window starts afresh
        self.assertTrue(self.window(health, 0.1)[-1])


class AutoTargetTests(unittest.TestCase):
    """2.0.2 "Auto" latency: each channel adds an extra to the target in force (Normal's, or
    the burst when that is larger): it starts at 0, rises 1.5 s with each starvation or stall
    up to max(Stable + 4, burst + 2) in all, and falls 0.5 s after each 5 quiet minutes,
    down to 0. Each rise moves the target in force at once, also on long HLS segments."""

    def test_it_starts_at_the_target_in_force(self):
        auto = AutoTarget()
        self.assertEqual(auto.extra, 0.0)
        self.assertEqual(auto.target(burst=0.0), TARGETS["normal"])
        self.assertEqual(auto.target(burst=10.0), 10.0)
        self.assertFalse(auto.adjusted)

    def test_trouble_raises_it_in_steps_up_to_the_cap(self):
        auto = AutoTarget()
        targets = []
        for _ in range(7):
            auto.trouble(burst=0.0)
            targets.append(auto.target(burst=0.0))
        self.assertEqual(targets, [4.0, 5.5, 7.0, 8.5, 10.0, 10.0, 10.0])  # Stable + 4
        self.assertTrue(auto.adjusted)

    def test_on_long_segments_each_rise_moves_the_target_at_once(self):
        auto = AutoTarget()
        targets = []
        for _ in range(3):
            auto.trouble(burst=10.0)
            targets.append(auto.target(burst=10.0))
        self.assertEqual(targets, [11.5, 12.0, 12.0])  # the burst plus 2 s at most
        six = AutoTarget()
        six.trouble(burst=6.0)
        self.assertEqual(six.target(burst=6.0), 7.5)

    def test_trouble_says_whether_it_changed(self):
        auto = AutoTarget(7.5)
        self.assertFalse(auto.trouble(burst=0.0))  # at the cap
        self.assertTrue(AutoTarget(1.0).trouble(burst=10.0))

    def test_a_learned_extra_never_takes_the_target_past_the_cap(self):
        auto = AutoTarget(7.5)  # learned on a steady source
        self.assertEqual(auto.target(burst=10.0), 12.0)  # now bursty: 10 + 2 at most

    def test_five_quiet_minutes_lower_it_half_a_second_down_to_nothing(self):
        auto = AutoTarget(1.5)
        changes = [auto.quiet(1.0) for _ in range(299)]
        self.assertEqual((auto.extra, any(changes)), (1.5, False))
        self.assertTrue(auto.quiet(1.0))
        self.assertEqual(auto.extra, 1.0)
        for _ in range(300 * 5):
            auto.quiet(1.0)
        self.assertEqual(auto.extra, 0.0)  # 0.5 after 10 min, then 0
        self.assertFalse(auto.adjusted)

    def test_trouble_starts_the_quiet_minutes_afresh(self):
        auto = AutoTarget(4.5)
        auto.quiet(299.0)
        auto.trouble(burst=0.0)
        self.assertFalse(auto.quiet(299.0))
        self.assertEqual(auto.extra, 6.0)
        self.assertTrue(auto.quiet(1.0))
        self.assertEqual(auto.extra, 5.5)

    def test_a_remembered_extra_starts_there(self):
        self.assertEqual(AutoTarget(3.0).target(burst=0.0), 5.5)
        self.assertTrue(AutoTarget(3.0).adjusted)
        for bad in (0.0, float("nan"), float("inf"), -3.0, 100.0):
            with self.subTest(bad=bad):
                self.assertEqual(AutoTarget(bad).extra, 0.0)

    def test_stay_live_starts_auto_at_normal(self):
        self.assertEqual(StayLive(AUTO).target, TARGETS["normal"])
        stay = StayLive("low")
        stay.set_latency(AUTO)
        self.assertEqual(stay.target, TARGETS["normal"])


class BuildTests(unittest.TestCase):
    """A raised Auto target is reached without a jump back (2.0.2): by one nudge below 1x
    where the rate may change, else as VLC waits at the live edge."""

    def test_a_raised_target_is_reached_by_one_gentle_nudge(self):
        stay, clock = live(T, latency=AUTO), Clock()
        drive(stay, T, 30.0, clock=clock)
        stay.target = T + 1.5  # Auto, after a starvation that ran out the cushion
        lag, rates = drive(stay, T, 120.0, clock=clock)
        self.assertEqual(min(rates), 0.97)
        self.assertEqual(changes(rates), 2)
        self.assertAlmostEqual(lag, T + 1.5, delta=0.1)

    def test_a_locked_rate_never_slows_to_build(self):
        stay, clock = live(T, latency=AUTO), Clock()
        stay.lock_rate()
        stay.target = T + 3.0
        _, rates = drive(stay, T, 120.0, clock=clock)
        self.assertEqual(set(rates), {1.0})


if __name__ == "__main__":
    unittest.main()
