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


def calls(rates, start=1.0):
    """The rates set, as libVLC would be asked: each change once."""
    out, previous = [], start
    for rate in rates:
        if rate != previous:
            out.append(rate)
        previous = rate
    return out


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
    """2.1 (the owner: a public HLS channel flapped between 1x and 1.05x every few seconds, and each
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
        # Ramped in and out in three steps a second apart: six rate changes in all (each
        # libVLC rate change retunes its audio: few and small ones).
        self.assertEqual(calls(rates), [1.01, 1.02, 1.03, 1.02, 1.01, 1.0])
        first = rates.index(1.01)
        self.assertEqual(rates[first : first + 9], [1.01] * 4 + [1.02] * 4 + [1.03])
        self.assertEqual(changes(rates), 2)  # one nudge: to 1.03 and back
        self.assertAlmostEqual(lag, T, delta=0.1)  # sized to end at the target
        self.assertEqual(rates[-1], 1.0)
        self.assertEqual(stay.nudge, 1.0)

    def test_ahead_of_the_band_the_nudge_adds_room_at_0_97(self):
        stay, clock = live(6.0), Clock()
        lag, rates = drive(stay, 4.0, 200.0, clock=clock)
        self.assertEqual(calls(rates), [0.99, 0.98, 0.97, 0.98, 0.99, 1.0])
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
        self.assertEqual(calls(rates), [1.0333, 1.0667, 1.1, 1.0667, 1.0333, 1.0])
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

    def test_a_target_that_rises_does_not_stop_a_far_nudge_to_start_it_again(self):
        # Seen on a public HLS channel after a network change (2.1 review round): a backlog
        # landed during a 1.10x catch-up and Auto raised the target; the nudge stopped short
        # of it and the next one started 3 s later.
        stay, clock = live(), Clock()
        lag, rates = drive(stay, T + 10.0, 10.0, clock=clock)  # sized for 10 s
        lag += 15.0  # a backlog lands: further behind than it was sized for
        stay.target = T + 12.0  # and Auto raises the target: more than is left, still far
        lag, more = drive(stay, lag, 200.0, clock=clock)
        self.assertEqual(changes(rates + more), 2)  # one nudge, in and out once
        self.assertAlmostEqual(lag, T + 12.0, delta=0.15)

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
        _, rates = drive(stay, T - 0.5, 3.0, clock=clock)  # a reopen landed at the target
        self.assertEqual(calls(rates, start=1.03), [1.02, 1.01, 1.0])
        self.assertEqual(stay.nudge, 1.0)

    def test_a_nudge_ramps_out_once_whatever_the_lag_does_meanwhile(self):
        # Measured on a public HLS channel (2.1): the estimate hovered around the target as
        # a 0.95x nudge ended, and the ramp went 0.95, 0.96, 0.95, 0.96 ... before 1x.
        stay, clock = live(7.0, burst=7.0), Clock()
        _, rates = drive(stay, 3.0, 6.0, clock=clock)  # short of room: 0.95x
        self.assertEqual((rates[-1], stay.nudge), (0.95, 0.95))
        rates = []
        for k in range(12):  # at the target, give or take a few hundredths
            rates.append(
                stay.update(7.0 + (-0.05 if k % 2 else 0.05), False, now=clock.tick()).rate
            )
        self.assertEqual(calls(rates, start=0.95), [0.9667, 0.9833, 1.0])

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
        self.assertEqual(calls(rates), [0.9833, 0.9667, 0.95, 0.9667, 0.9833, 1.0])
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

    def test_trouble_owes_a_slow_down_at_once_beyond_the_budget(self):
        # 2.0.2 built a raised Auto target at once at 0.95x; 2.1 first made it wait for the
        # budget when the stall left more than half a second of room (review, R14).
        stay, clock = live(T, latency=AUTO), Clock()
        _, rates = drive(stay, T + 2.0, 120.0, clock=clock)  # a nudge spends the budget
        self.assertEqual(changes(rates), 2)
        stay.trouble()  # a stall ran out the cushion ...
        stay.target = T + 1.5  # ... and Auto raised the target
        _, waiting = drive(stay, 1.5, 8.0, clock=clock, buffering=True)  # VLC waits
        self.assertEqual(set(waiting), {1.0})
        lag, rates = drive(stay, 1.5, 120.0, clock=clock)
        # Once VLC played a second short of the target (stage 2: the first tick after a
        # stall holds one sample of the lag filter, and is no measure on its own).
        self.assertEqual(rates[:4], [1.0, 1.0, 1.0, 0.9833])
        self.assertEqual(calls(rates), [0.9833, 0.9667, 0.95, 0.9667, 0.9833, 1.0])
        self.assertAlmostEqual(lag, T + 1.5, delta=0.15)

    def test_trouble_with_room_enough_owes_nothing(self):
        stay, clock = live(T, latency=AUTO), Clock()
        stay.trouble()  # the source came back with what it had missed, say
        _, rates = drive(stay, T - 0.2, 60.0, clock=clock)
        self.assertEqual(set(rates), {1.0})

    def test_trouble_ends_a_catch_up_then_slows_down(self):
        stay, clock = live(), Clock()
        _, rates = drive(stay, T + 3.0, 20.0, clock=clock)
        self.assertEqual(rates[-1], 1.03)
        stay.trouble()
        lag, rates = drive(stay, 0.5, 120.0, clock=clock)  # short of the target now
        self.assertEqual(calls(rates, start=1.03)[:3], [1.02, 1.01, 1.0])
        self.assertEqual(min(rates), 0.95)
        self.assertAlmostEqual(lag, T, delta=0.15)
        # The cut catch-up spent no budget: a drift behind again gets its nudge.
        lag, rates = drive(stay, T + 2.0, 30.0, clock=clock)
        self.assertEqual(max(rates), 1.03)

    def test_trouble_far_behind_leaves_a_catch_up_alone(self):
        # Review round 2: a stall while catching up from far behind ended the catch-up, and
        # it started again 3 s later, at every stall; VLC had all the room it needed.
        for target in (T, T + 1.5):  # Auto may raise the target with the stall
            with self.subTest(target=target):
                stay, clock = live(), Clock()
                lag, rates = drive(stay, T + 12.0, 10.0, clock=clock)
                self.assertEqual(rates[-1], 1.1)
                stay.trouble()
                stay.target = target
                lag, more = drive(stay, lag, 3.0, clock=clock, buffering=True)  # the stall
                lag, more = drive(stay, lag, 150.0, clock=clock)
                self.assertEqual(changes(rates + more), 2)  # one catch-up, in and out once
                self.assertEqual(min(rates + more), 1.0)  # never slowed down
                self.assertAlmostEqual(lag, target, delta=0.15)

    def test_backfilled_stalls_never_restart_a_catch_up_free_of_the_budget(self):
        # Stage 1's last review: a source whose 3 s stalls are backfilled, so the lag is back
        # at once; the first tick after each (the lag filter holds one sample) reads a second
        # short of the target. It cut a gentle catch-up near the target, which then started
        # again free of the budget, about twice a minute without end.
        stay, clock = live(), Clock()
        _, rates = drive(stay, T + 1.5, 20.0, clock=clock)
        self.assertEqual(rates[-1], 1.03)  # a catch-up is under way
        for _ in range(14):  # 300 s in all
            stay.trouble()  # the stall ran out the cushion
            rates += drive(stay, T + 1.3, 3.0, clock=clock, buffering=True)[1]
            rates += drive(stay, T - 1.0, TICK, clock=clock)[1]  # one unsettled tick
            rates += drive(stay, T + 1.3, 20.0 - 3.0 - TICK, clock=clock)[1]
        self.assertLessEqual(changes(rates), 2)

    def test_a_cut_catch_up_counts_against_the_budget_unless_the_slow_down_follows(self):
        stay, clock = live(), Clock()
        _, rates = drive(stay, T + 3.0, 20.0, clock=clock)
        self.assertEqual(rates[-1], 1.03)
        stay.trouble()
        rates += drive(stay, T - 1.0, 1.25, clock=clock)[1]  # short long enough: it is cut
        self.assertLess(rates[-1], 1.03)
        rates += drive(stay, T + 2.0, 60.0, clock=clock)[1]  # the lag was back: no 0.95x
        self.assertEqual(min(rates), 1.0)
        self.assertEqual(changes(rates), 2)  # and no second nudge within the budget

    def test_a_cut_catch_up_counts_when_the_lag_is_back_just_after_its_ramp_out(self):
        # Stage 2 review: the catch-up ramped out to 1x one tick before the lag came back,
        # and so escaped the budget.
        stay, clock = live(), Clock()
        _, rates = drive(stay, T + 3.0, 20.0, clock=clock)
        self.assertEqual(rates[-1], 1.03)
        stay.trouble()
        for _ in range(100):  # short of the target until the cut catch-up is back at 1x
            more = drive(stay, T - 1.0, TICK, clock=clock)[1]
            rates += more
            if more[-1] == 1.0:
                break
        rates += drive(stay, T + 2.0, 60.0, clock=clock)[1]  # then back at once, and behind
        self.assertEqual(min(rates), 1.0)  # no 0.95x followed ...
        self.assertEqual(changes(rates), 2)  # ... so the catch-up counted: no second one

    def test_a_nudge_that_ran_its_whole_size_counts_against_the_budget(self):
        stay, clock = live(), Clock()
        lag, rates = drive(stay, T + 1.5, 16.0, clock=clock)
        for _ in range(400):  # until it took its size and ramps out
            lag, more = drive(stay, lag, TICK, clock=clock)
            rates += more
            if stay._nudge is not None and stay._nudge.ending:
                break
        self.assertTrue(stay._nudge.spent)
        stay.trouble()
        lag, more = drive(stay, T - 1.5, 40.0, clock=clock)  # short: 0.95x after its ramp out
        self.assertEqual(min(more), 0.95)
        _, more = drive(stay, T + 1.5, 60.0, clock=clock)  # behind again within 5 minutes
        self.assertEqual(max(more), 1.0)  # it counted: the band widens instead

    def test_trouble_then_go_live_owes_nothing(self):
        # Review round 2: an owed slow-down outlived a Go live and slowed the stream that
        # opened at the target (a little short of it while VLC opens).
        stay, clock = live(), Clock()
        stay.trouble()  # VLC ran dry ...
        drive(stay, 0.2, 2.0, clock=clock, buffering=True)  # ... and waits
        stay.user_go_live()
        _, rates = drive(stay, T - 0.6, 30.0, clock=clock)
        self.assertEqual(set(rates), {1.0})
        for leave in ("user_paused", "user_seeked_back"):
            with self.subTest(leave=leave):
                stay, clock = live(), Clock()
                stay.trouble()
                getattr(stay, leave)()
                stay.user_go_live()
                _, rates = drive(stay, T - 0.6, 30.0, clock=clock)
                self.assertEqual(set(rates), {1.0})

    def test_a_vlc_that_runs_dry_again_and_again_still_slows_down(self):
        # Short of room, VLC plays a second and waits a second, over and over: the urgent
        # count pauses while it waits rather than starting again (review, R14).
        stay, clock = live(10.0, burst=10.0), Clock()
        rates = []
        for k in range(40):
            rates += drive(stay, 5.2, 1.0, clock=clock, buffering=k % 2 == 1)[1]
        first = next(i for i, rate in enumerate(rates) if rate != 1.0)
        self.assertLessEqual(first * TICK, 2 * 3.25)  # 3 s of playing, in 6 s
        self.assertEqual(min(rates), 0.95)

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
        # A public HLS channel (measured): every 7 s, 6 s of media, every sixth time 12 s.
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

    def test_its_mean_settles_after_ten_seconds_and_two_bursts_since_a_restart(self):
        # 2.1 stage 5 (re-review N2): only a settled mean's picks are learned from.
        for burst, settles in ((0.0, 10.0), (10.0, 20.0)):
            with self.subTest(burst=burst):
                flt = LagFilter()
                for k in range(81):  # 20 s
                    now = 0.25 * k
                    flt.add(now, 100.0 + now, burst, 0.0)
                    self.assertEqual(flt.settled(now), now >= settles - 1e-9)
                restarts = flt.restarts
                flt.add(20.25, 100.0 + 20.25 + 30.0, burst, 0.0)  # a step: afresh
                self.assertEqual(flt.restarts, restarts + 1)
                self.assertFalse(flt.settled(20.25))


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
    minute on a public HLS channel."""

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


class StretchTests(unittest.TestCase):
    def test_a_window_holds_two_stretches_at_least(self):
        # Stretches end at VLC's steps (2.1 stage 2): a step that came late, then one early,
        # are judged together, not the late one alone (52% of 1.10x).
        health = RateHealth()
        self.assertFalse(health.add(3.5, 2.0, 1.10))
        self.assertFalse(health.add(0.5, 2.0, 1.10))
        self.assertGreater(health.share, 0.7)


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


class LandingTests(unittest.TestCase):
    """2.1 stage 3: a stream opened at the start Go live picks (a tune, Go live, a reopen at
    live) lands where the source's keyframes allow. Above the target by up to a keyframe
    interval (the 1-stream raw-TS provider: one every 10 s) it is nudged gently, never
    caught up at 1.10x nor reopened; short of it (nothing further back was buffered) it is
    built at 0.97x for 20 s at most, then accepted until VLC runs dry."""

    def test_a_landing_above_the_target_is_nudged_gently(self):
        stay, clock = live(), Clock()
        stay.landed(9.0)  # the newest keyframe at or above the target
        lag, rates = drive(stay, T + 9.0, 60.0, clock=clock)
        self.assertEqual(max(rates), 1.03)  # not 1.10x after 3 s
        first = next(i for i, rate in enumerate(rates) if rate != 1.0)
        self.assertGreaterEqual(first * TICK, 14.75)  # a lasting drift: after 15 s
        lag, more = drive(stay, lag, 300.0, clock=clock)
        self.assertEqual(changes(rates + more), 2)  # one nudge, in and out
        self.assertAlmostEqual(lag, T, delta=0.2)

    def test_a_landing_above_the_target_never_reopens_at_live(self):
        stay, clock = live(), Clock()
        stay.landed(12.0)
        decisions = [stay.update(T + 21.0, False, now=clock.tick()) for _ in range(240)]
        self.assertFalse(any(decision.reopen_live for decision in decisions))

    def test_far_beyond_the_landing_is_still_caught_up(self):
        stay, clock = live(), Clock()
        stay.landed(9.0)
        drive(stay, T + 9.0, 2.0, clock=clock)
        lag, rates = drive(stay, T + 9.0 + 9.0, 10.0, clock=clock)  # a stall added 9 s
        self.assertEqual(max(rates), 1.10)

    def test_the_landing_allowance_follows_the_lag_down(self):
        stay, clock = live(), Clock()
        stay.landed(9.0)
        lag, rates = drive(stay, T + 9.0, 45.0, clock=clock)  # the nudge took some off
        self.assertLess(lag, T + 8.5)
        _, rates = drive(stay, lag + 8.6, 10.0, clock=clock)  # 8.6 s more: far from there
        self.assertEqual(max(rates), 1.10)

    def test_after_the_first_minute_a_backlog_is_caught_up_as_ever(self):
        # Review R32: the landing's allowance lasts the start window only. A reconnect's
        # backlog 2 minutes on is drift: 1.10x after 3 s, and the reopen at 20 s beyond.
        stay, clock = live(), Clock()
        stay.landed(9.0)
        lag, _ = drive(stay, T + 9.0, 120.0, clock=clock)
        lag, rates = drive(stay, lag + 8.0, 10.0, clock=clock)
        self.assertEqual(max(rates), 1.10)
        stay, clock = live(), Clock()
        stay.landed(9.0)
        drive(stay, T + 9.0, 120.0, clock=clock)
        decision = stay.update(T + 20.5, False, now=clock.tick())
        self.assertTrue(decision.reopen_live)

    def test_a_backlog_step_right_after_the_start_is_part_of_the_landing(self):
        # Review M8: raw TS whose backlog came after the 1.5 s hold: 1.10x for ~100 s.
        stay, clock = live(), Clock()
        stay.landed(1.5)
        drive(stay, T + 1.5, 3.0, clock=clock)
        stay.landed_further(10.5)  # 9 s more landed
        _, rates = drive(stay, T + 10.5, 30.0, clock=clock)
        self.assertEqual(max(rates), 1.03)
        stay, clock = live(), Clock()  # after the window it is drift
        stay.landed(1.5)
        drive(stay, T + 1.5, 70.0, clock=clock)
        stay.landed_further(10.5)
        _, rates = drive(stay, T + 10.5, 10.0, clock=clock)
        self.assertEqual(max(rates), 1.10)

    def test_go_live_forgets_the_landing(self):
        stay, clock = live(), Clock()
        stay.landed(9.0)
        stay.user_go_live()  # a Go live opens afresh (and says where it landed)
        _, rates = drive(stay, T + 12.0, 10.0, clock=clock)
        self.assertEqual(max(rates), 1.10)

    def test_a_go_live_that_opens_nothing_keeps_the_landing(self):
        # Review M4: a Go live merged into stay-live's reopen a moment before opens nothing:
        # the stream that opened then keeps its landing (no 1.10x for its keyframe interval).
        stay, clock = live(), Clock()
        stay.landed(9.0)
        stay.user_go_live(keep_landing=True)
        _, rates = drive(stay, T + 9.0, 10.0, clock=clock)
        self.assertEqual(set(rates), {1.0})

    def test_a_short_landing_builds_at_0_97_for_20_seconds_at_most_then_accepts(self):
        # A public HLS channel started short (7 s target, 3.5 s behind): 2.0.1's rule built
        # the room at 0.95x for 40-110 s, audibly.
        stay, clock = live(7.0, burst=7.0), Clock()
        stay.landed(-3.5)
        lag, rates = drive(stay, 3.5, 180.0, clock=clock)
        self.assertEqual(min(rates), 0.97)
        slow = sum(1 for rate in rates if rate < 1.0) * TICK
        self.assertLessEqual(slow, 20.0)
        self.assertGreater(slow, 15.0)
        self.assertEqual(changes(rates), 2)
        self.assertEqual(rates[-1], 1.0)
        self.assertLess(lag, 7.0 - 2.5)  # the rest of the shortfall is accepted

    def test_a_shortfall_just_beyond_the_band_is_built_back_inside_it(self):
        stay, clock = live(7.0, burst=7.0), Clock()
        stay.landed(-1.3)
        lag, rates = drive(stay, 5.7, 120.0, clock=clock)
        self.assertEqual((min(rates), changes(rates)), (0.97, 2))
        self.assertGreaterEqual(lag, 7.0 - stay.band(-1))  # 20 s at 0.97x: about 0.5 s
        self.assertFalse(stay.shortfall_accepted)  # inside the band: nothing is accepted
        lag, rates = drive(stay, 3.2, 120.0, clock=clock)  # later, less than 0.5 s of room
        self.assertEqual(min(rates), 0.95)  # 2.0.1's protection

    def test_an_accepted_shortfall_is_built_once_vlc_runs_dry(self):
        stay, clock = live(7.0, burst=7.0), Clock()
        stay.landed(-3.5)
        lag, rates = drive(stay, 3.5, 60.0, clock=clock)  # built, then accepted
        stay.trouble()  # VLC ran dry before a segment landed
        lag, rates = drive(stay, lag, 120.0, clock=clock)
        self.assertEqual(min(rates), 0.95)  # the slow-down owed after trouble
        self.assertAlmostEqual(lag, 7.0, delta=0.15)

    def test_after_the_start_a_real_shortfall_keeps_2_0_1s_protection(self):
        stay, clock = live(10.0, burst=10.0), Clock()
        stay.landed(0.5)
        lag, rates = drive(stay, 10.5, 90.0, clock=clock)
        self.assertEqual(set(rates), {1.0})
        lag, rates = drive(stay, 5.2, 200.0, clock=clock)  # less than half a second of room
        self.assertEqual(min(rates), 0.95)
        self.assertAlmostEqual(lag, 10.0, delta=0.15)

    def test_a_target_that_rises_in_the_first_minute_is_built_at_0_97(self):
        # A raw TS source's bursts are measured only once a few have landed: the target
        # rises from Normal's to the burst after the start.
        stay, clock = live(), Clock()
        stay.landed(0.0)
        drive(stay, T, 20.0, clock=clock)
        stay.target, stay.burst = 10.0, 10.0
        _, rates = drive(stay, T, 120.0, clock=clock)
        self.assertEqual(min(rates), 0.97)
        self.assertLessEqual(sum(1 for rate in rates if rate < 1.0) * TICK, 20.0)


def steer(stay, lag, seconds, *, clock, landing=None, actual=None):
    """Play `seconds` at stay-live's rates from `lag` (the source steady), where a jump to live
    would land at `landing(now, lag)` (None: stay-live is not told). A jump lands there (or at
    `actual(now, lag)`: the prediction erred) and is reported to stay-live as the caller does
    (`user_go_live()`, then `landed()` with the predicted landing). Returns the lag, the rates
    and the times of the jumps."""
    rates, jumps = [], []
    if landing is not None:
        stay.jump_lag = lambda: landing(clock.now, lag)
    for _ in range(round(seconds / TICK)):
        now = clock.tick()
        decision = stay.update(lag, False, now=now)
        rates.append(decision.rate)
        if decision.reopen_live:
            jumps.append(now)
            if decision.jump:
                predicted = landing(now, lag)
                lag = predicted if actual is None else actual(now, lag)
                stay.user_go_live()
                stay.landed(predicted - stay.target, build=False)  # a jump builds nothing
            continue
        lag -= (decision.rate - 1.0) * TICK
    return lag, rates, jumps


def keyframes(every, *, since=1000.0):
    """Where a jump lands on a source with a keyframe every `every` seconds: the newest at
    least a quarter second short of the target, as Go live picks (`since`: one was at the
    target less a quarter second then)."""
    return lambda now, lag: T - 0.25 + (now - since) % every


class JumpTests(unittest.TestCase):
    """2.1 stage 5 (safe jumps): far behind, one clean jump to live replaces a long 1.10x
    catch-up (8 s at 1.10x takes 80 s): when the clean start Go live would pick lands within
    the band or 5 s nearer, and nothing jumped (a tune, Go live, a reopen at live) within a
    minute. Never two within a minute; the keyframes' grain is never a reason for 1.10x."""

    def test_far_behind_with_a_clean_landing_at_the_target_jumps_once_and_never_speeds_up(self):
        stay, clock = live(), Clock()
        lag, rates, jumps = steer(stay, T + 10.0, 30.0, clock=clock, landing=lambda *_: T + 0.2)
        self.assertEqual(len(jumps), 1)
        self.assertLessEqual(jumps[0] - 1000.0, 3.25)  # as soon as 1.10x would have started
        self.assertEqual(set(rates), {1.0})
        self.assertAlmostEqual(lag, T + 0.2)

    def test_a_jump_says_it_is_one_and_the_hard_bound_still_reopens(self):
        stay, clock = live(), Clock()
        stay.jump_lag = lambda: T
        decisions = [stay.update(T + 10.0, False, now=clock.tick()) for _ in range(16)]
        self.assertEqual([d.jump for d in decisions].count(True), 1)
        self.assertEqual(sum(d.reopen_live for d in decisions), 1)
        stay.user_go_live()
        stay.landed(0.0)  # a jump a moment ago: the next within a minute is none ...
        decision = stay.update(R + 1, False, now=clock.tick())
        self.assertEqual(decision, Decision(1.0, True))  # ... but beyond 20 s, as ever

    def test_again_within_a_minute_it_catches_up_at_1_10_then_jumps_once_the_minute_passed(self):
        stay, clock = live(), Clock()
        at = lambda *_: T + 0.2  # noqa: E731
        lag, _, jumps = steer(stay, T + 10.0, 20.0, clock=clock, landing=at)
        self.assertEqual(len(jumps), 1)
        lag, rates, more = steer(stay, lag + 14.0, 30.0, clock=clock, landing=at)  # a backlog
        self.assertEqual((max(rates), more), (1.10, []))  # never two jumps within a minute
        lag, rates, more = steer(stay, lag, 40.0, clock=clock, landing=at)
        self.assertEqual(len(more), 1)  # the catch-up's rest: one jump, a minute on
        self.assertGreaterEqual(more[0] - jumps[0], 60.0)
        self.assertEqual(rates[-1], 1.0)

    def test_on_long_keyframe_intervals_it_waits_for_a_keyframe_that_helps(self):
        # One keyframe every 10 s (the 1-stream raw-TS provider's 4K channel): the landing
        # moves with the next keyframe; a jump waits for one that lands within the band or
        # 5 s nearer, rather than 1.10x for a minute and a half.
        for phase in (0.0, 2.5, 5.0, 7.5):
            with self.subTest(phase=phase):
                stay, clock = live(), Clock()
                landing = keyframes(10.0, since=1000.0 - phase)
                lag, rates, jumps = steer(stay, T + 9.0, 30.0, clock=clock, landing=landing)
                self.assertLessEqual(max(rates), 1.03)  # a landing above the band: gently
                self.assertEqual(set(rates[: round((jumps[0] - 1000.0) / TICK)]), {1.0})
                self.assertEqual(len(jumps), 1)
                self.assertLessEqual(jumps[0] - 1000.0, 3.0 + 10.25)
                self.assertLessEqual(lag, T + 4.0)

    def test_with_no_landing_that_helps_it_catches_up_at_1_10_after_waiting(self):
        # Keyframes 30 s apart, say: the landing is 3 s nearer, neither within the band nor
        # 5 s nearer. A keyframe interval's wait, then 1.10x as before.
        stay, clock = live(), Clock()
        lag, rates, jumps = steer(stay, T + 10.0, 120.0, clock=clock, landing=lambda *_: T + 7.0)
        self.assertEqual(jumps, [])
        first = next(i for i, rate in enumerate(rates) if rate != 1.0)
        self.assertGreaterEqual(first * TICK, 3.0 + 12.0 - 0.25)
        self.assertEqual(max(rates), 1.10)

    def test_without_a_clean_start_to_jump_to_it_catches_up_at_once_as_before(self):
        stay, clock = live(), Clock()
        lag, rates, jumps = steer(stay, T + 10.0, 20.0, clock=clock, landing=lambda *_: None)
        self.assertEqual(jumps, [])
        first = next(i for i, rate in enumerate(rates) if rate != 1.0)
        self.assertLessEqual(first * TICK, 3.25)
        self.assertEqual(max(rates), 1.10)

    def test_the_keyframes_grain_is_no_reason_for_1_10(self):
        # Stage 3's open concern: a landing 9.7 s above the target (the newer keyframe just
        # short of the floor) was nudged gently for its minute, then caught up at 1.10x.
        # When no jump does better than the lag less a second, the gentle nudge goes on.
        stay, clock = live(), Clock()
        stay.landed(9.7)
        lag, rates, jumps = steer(
            stay, T + 9.7, 240.0, clock=clock, landing=lambda now, lag: lag - 0.5
        )
        self.assertEqual(jumps, [])
        self.assertEqual(max(rates), 1.03)
        self.assertLess(lag, T + 9.7 - 4.0)

    def test_a_landing_far_above_the_target_jumps_after_its_minute_on_a_keyframe_that_helps(self):
        stay, clock = live(), Clock()
        stay.landed(9.7)
        landing = keyframes(10.0, since=1003.0)
        lag, rates, jumps = steer(stay, T + 9.7, 120.0, clock=clock, landing=landing)
        self.assertEqual(max(rates), 1.03)  # gently while the landing's minute lasts
        self.assertEqual(len(jumps), 1)
        self.assertGreaterEqual(jumps[0] - 1000.0, 60.0)
        self.assertLessEqual(jumps[0] - 1000.0, 60.0 + 3.0 + 10.25)
        self.assertLessEqual(abs(lag - T), stay.band(1))

    def test_no_jump_while_a_stream_just_opened(self):
        stay, clock = live(), Clock()
        stay.hold_reopen = True  # Go live (or a seek) opened a stream a moment ago
        lag, rates, jumps = steer(stay, T + 10.0, 10.0, clock=clock, landing=lambda *_: T)
        self.assertEqual((jumps, set(rates)), ([], {1.0}))
        stay.hold_reopen = False
        lag, rates, jumps = steer(stay, lag, 1.0, clock=clock, landing=lambda *_: T)
        self.assertEqual(len(jumps), 1)

    def test_a_locked_rate_still_jumps_far_behind_and_never_changes_the_rate(self):
        # Review I2: a channel VLC cannot play at another rate sat up to 20 s behind.
        stay, clock = live(), Clock()
        stay.lock_rate()
        lag, rates, jumps = steer(stay, T + 15.0, 20.0, clock=clock, landing=lambda *_: T)
        self.assertEqual((len(jumps), set(rates)), (1, {1.0}))
        self.assertLessEqual(jumps[0] - 1000.0, 3.25)
        stay, clock = live(), Clock()  # no jump that helps: 1x, as before
        stay.lock_rate()
        _, rates, jumps = steer(stay, T + 15.0, 60.0, clock=clock, landing=lambda *_: T + 13.0)
        self.assertEqual((jumps, set(rates)), ([], {1.0}))

    def test_a_jump_or_go_live_that_lands_short_changes_no_rate(self):
        # Review I3: on a sparse-PCR channel each jump read 1.2-1.4 s short once VLC played,
        # and the start's 0.97x build followed (six rate changes). A jump is made with the
        # ring full and picked at the target: its shortfall is no missing room.
        stay, clock = live(12.0, burst=10.0), Clock()
        lag, rates, jumps = steer(
            stay, 24.0, 200.0, clock=clock, landing=lambda *_: 12.0, actual=lambda *_: 10.7
        )
        self.assertEqual((len(jumps), set(rates), lag), (1, {1.0}, 10.7))
        stay, clock = live(), Clock()  # the viewer's Go live
        stay.user_go_live()
        stay.landed(-1.3, build=False)
        _, rates = drive(stay, T - 1.3, 200.0, clock=clock)
        self.assertEqual(set(rates), {1.0})

    def test_a_short_landing_without_a_build_keeps_the_room_rules(self):
        stay, clock = live(7.0, burst=7.0), Clock()
        stay.landed(-3.5, build=False)
        _, rates = drive(stay, 3.6, 20.0, clock=clock)  # under 0.5 s before each burst
        self.assertEqual(min(rates), 0.95)  # 2.0.1's protection
        stay, clock = live(7.0, burst=7.0), Clock()
        stay.landed(-1.3, build=False)
        drive(stay, 5.7, 20.0, clock=clock)
        stay.trouble()  # VLC ran dry
        lag, rates = drive(stay, 5.7, 120.0, clock=clock)
        self.assertEqual(min(rates), 0.95)  # the owed slow-down
        self.assertAlmostEqual(lag, 7.0, delta=0.15)

    def test_only_the_landings_own_shortfall_is_tolerated(self):
        # Re-review N1: the tolerance outlived the landing: a target raised after a Go live
        # (Normal to Stable, a larger burst, Auto) was never built, and a slow drift down was
        # left until VLC had under half a second of room.
        cases = {
            "the target rises": dict(target=6.0),
            "a larger burst": dict(target=5.0, burst=4.0),
            "a slow drift down": dict(drift=-0.005),
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                stay, clock = live(), Clock()
                stay.user_go_live()
                stay.landed(0.0, build=False)  # landed at the target
                lag, _ = drive(stay, T, 30.0, clock=clock)
                stay.target = case.get("target", stay.target)
                stay.burst = case.get("burst", stay.burst)
                lag, rates = drive(stay, lag, 500.0, clock=clock, drift=case.get("drift", 0.0))
                self.assertEqual(min(rates), 0.97)  # built, gently, as without the Go live
                if "drift" not in case:
                    self.assertGreater(lag, stay.target - stay.band(-1) - 0.1)

    def test_a_short_landing_back_in_the_band_is_tolerated_no_more(self):
        stay, clock = live(), Clock()
        stay.user_go_live()
        stay.landed(0.0, build=False)
        lag, rates = drive(stay, T - 1.4, 30.0, clock=clock)  # it landed short
        self.assertEqual(set(rates), {1.0})
        lag, rates = drive(stay, T - 0.5, 10.0, clock=clock)  # back in the band (a step)
        lag, rates = drive(stay, T - 1.4, 60.0, clock=clock)  # short again: a real one
        self.assertEqual(min(rates), 0.97)

    def test_a_landing_is_judged_once_its_stream_played(self):
        # Right after an open the lag reads where the pick aimed, then where VLC's clock puts
        # it: that first reading in the band is no sign the landing was not short.
        stay, clock = live(), Clock()
        stay.user_go_live()
        stay.landed(0.0, build=False)
        stay.hold_reopen = True  # the stream opened a moment ago
        drive(stay, T, 2.0, clock=clock)
        stay.hold_reopen = False
        _, rates = drive(stay, T - 1.3, 120.0, clock=clock)
        self.assertEqual(set(rates), {1.0})

    def test_a_stall_after_a_landing_ends_its_tolerance(self):
        # A Go live pressed while the source stalled (a public sparse-PCR channel): the stall
        # went on after it and left the lag 7 s short. That is the source's, not the pick's.
        stay, clock = live(13.5, burst=12.0), Clock()
        stay.user_go_live()
        stay.landed(0.4, build=False)
        drive(stay, 13.9, 5.0, clock=clock)
        stay.source_stepped()  # the stall ended: the filter starts afresh
        lag, rates = drive(stay, 6.5, 120.0, clock=clock)
        self.assertEqual(min(rates), 0.97)  # built back, gently
        self.assertGreater(lag, 6.5 + 1.0)

    def test_the_keyframes_grain_never_widens_the_band(self):
        # Review: a "gentle" verdict while the budget is spent widened the band to the far
        # lag (8 s and more), on both sides. The band stays as it was.
        stay, clock = live(), Clock()
        drive(stay, T + 2.0, 120.0, clock=clock)  # the budget is spent
        _, rates, jumps = steer(
            stay, T + 9.0, 60.0, clock=clock, landing=lambda now, lag: lag - 0.5
        )
        self.assertEqual((jumps, set(rates)), ([], {1.0}))
        self.assertLess(stay.band(-1), 2.0)

    def test_a_just_opened_stream_does_not_use_up_the_wait(self):
        stay, clock = live(), Clock()
        stay.hold_reopen = True
        lag, rates, _ = steer(stay, T + 10.0, 10.0, clock=clock, landing=lambda *_: T + 7.0)
        stay.hold_reopen = False
        _, rates, jumps = steer(stay, lag, 20.0, clock=clock, landing=lambda *_: T + 7.0)
        first = next(i for i, rate in enumerate(rates) if rate != 1.0)
        self.assertGreaterEqual(first * TICK, 12.0 - 0.25)  # the whole wait, after the hold

    def test_a_gentle_nudge_a_jump_cut_counts_against_the_budget(self):
        stay, clock = live(), Clock()
        stay.landed(9.7)
        lag, rates, jumps = steer(
            stay, T + 9.7, 80.0, clock=clock, landing=keyframes(10.0, since=1003.0)
        )
        self.assertEqual((len(jumps), max(rates)), (1, 1.03))  # it ran, then the jump
        _, rates = drive(stay, T + 2.0, 120.0, clock=clock)  # a drift after it
        self.assertEqual(set(rates), {1.0})  # within the budget's 5 minutes: tolerated

    def test_a_tune_or_go_live_counts_as_a_jump(self):
        for opened in ("tune", "go live"):
            with self.subTest(opened=opened):
                stay, clock = live(), Clock()
                if opened == "go live":
                    stay.user_go_live()
                stay.landed(0.0)
                lag, rates, jumps = steer(stay, T, 20.0, clock=clock, landing=lambda *_: T)
                lag, rates, jumps = steer(stay, T + 10.0, 30.0, clock=clock, landing=lambda *_: T)
                self.assertEqual((jumps, max(rates)), ([], 1.10))


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
