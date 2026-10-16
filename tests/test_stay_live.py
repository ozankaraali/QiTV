import unittest

from services.live.stay_live import AUTO, TARGETS, AutoTarget, Decision, Mode, RateHealth, StayLive

T = TARGETS["normal"]
S, N, P, R = T + 0.2, T + 0.75, T + 3, T + 20
H = 1.0  # from 1.10, back to 1.05 only at P - H or below


PRIME = {1.0: 0.0, 1.05: N + 0.1, 1.10: P + 1}


def make(mode, prev=1.0):
    """Drive the controller through public calls only."""
    sl = StayLive("normal")
    if prev:
        sl.update(PRIME[prev], False)
    if mode is Mode.SHIFTED:
        sl.user_paused()
    elif mode is Mode.CATCHING_UP:
        sl.user_paused()
        sl.user_speed(1.5)
    return sl


class StayLiveTests(unittest.TestCase):
    def test_live_threshold_table(self):
        eps = 1e-6
        rows = [
            # (lag, buffering, previous rate) -> Decision
            (0.0, False, 1.0, Decision(1.0)),
            (S, False, 1.0, Decision(1.0)),
            (S, False, 1.05, Decision(1.0)),
            (S + eps, False, 1.0, Decision(1.0)),  # hysteresis: keep
            (S + eps, False, 1.05, Decision(1.05)),
            (N, False, 1.0, Decision(1.0)),  # boundary is exclusive
            (N, False, 1.05, Decision(1.05)),
            (N + eps, False, 1.0, Decision(1.05)),
            (P - H, False, 1.10, Decision(1.05)),  # above N: 1.05 even from 1.10
            (P - H + eps, False, 1.10, Decision(1.10)),  # hysteresis: keep 1.10
            (P, False, 1.0, Decision(1.05)),  # > N, not > P
            (P, False, 1.05, Decision(1.05)),
            (P, False, 1.10, Decision(1.10)),
            (P + eps, False, 1.0, Decision(1.10)),
            (R, False, 1.10, Decision(1.10)),
            (R + eps, False, 1.10, Decision(1.0, True)),
            (R + eps, False, 1.0, Decision(1.0, True)),
            # buffering: keep rate, never reopen
            (R + 100, True, 1.05, Decision(1.05)),
            (0.0, True, 1.10, Decision(1.10)),
            (N + 1, True, 1.0, Decision(1.0)),
            (S + eps, False, 1.10, Decision(1.10)),  # hold band keeps 1.10
            (N, False, 1.10, Decision(1.10)),
            (S, False, 1.10, Decision(1.0)),
        ]
        for lag, buffering, prev, expected in rows:
            with self.subTest(lag=lag, buffering=buffering, prev=prev):
                sl = make(Mode.LIVE, prev)
                self.assertEqual(sl.update(lag, buffering), expected)
                self.assertIs(sl.mode, Mode.LIVE)

    def test_rate_never_exceeds_1_10_in_live(self):
        sl = StayLive()
        for lag in (0, 3, 5, 8, 15, 22):
            self.assertLessEqual(sl.update(lag, False).rate, 1.10)

    def test_hysteresis_sequence(self):
        sl = StayLive()
        rates = [sl.update(lag, False).rate for lag in (N + 1, S + 0.3, S, S + 0.3)]
        self.assertEqual(rates, [1.05, 1.05, 1.0, 1.0])

    def test_push_hysteresis_sequence(self):
        sl = StayLive()
        lags = (P + 0.1, P - 0.1, P + 0.1, P - H + 0.1, P - H, P - 0.1, P + 0.1)
        rates = [sl.update(lag, False).rate for lag in lags]
        self.assertEqual(rates, [1.10, 1.10, 1.10, 1.10, 1.05, 1.05, 1.10])

    def test_reopen_resets_rate(self):
        sl = StayLive()
        sl.update(P + 1, False)
        self.assertTrue(sl.update(R + 1, False).reopen_live)
        self.assertEqual(sl.update(S + 0.3, False), Decision(1.0))

    def test_shifted(self):
        sl = StayLive()
        sl.user_paused()
        self.assertIs(sl.mode, Mode.SHIFTED)
        self.assertEqual(sl.update(100, False), Decision(1.0))
        sl.user_speed(1.5)
        self.assertIs(sl.mode, Mode.CATCHING_UP)
        sl.user_speed(0.5)
        self.assertIs(sl.mode, Mode.SHIFTED)
        self.assertEqual(sl.update(100, False), Decision(0.5))

    def test_pause_from_catching_up_is_shifted_at_normal_speed(self):
        sl = make(Mode.CATCHING_UP)
        sl.user_paused()
        self.assertIs(sl.mode, Mode.SHIFTED)
        self.assertEqual(sl.update(100, False), Decision(1.0))

    def test_buffering_in_shifted(self):
        sl = make(Mode.SHIFTED)
        self.assertEqual(sl.update(50, True), Decision(1.0))
        sl.user_speed(0.5)
        self.assertEqual(sl.update(50, True), Decision(0.5))

    def test_reopen_is_edge_triggered(self):
        sl = StayLive()
        flags = [sl.update(R + 1, False) for _ in range(3)]
        self.assertEqual([d.reopen_live for d in flags], [True, False, False])
        self.assertTrue(all(d.rate == 1.0 for d in flags))
        sl.user_go_live()
        self.assertTrue(sl.update(R + 1, False).reopen_live)

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

    def test_catching_up_returns_live_within_settle(self):
        sl = make(Mode.CATCHING_UP)
        self.assertEqual(sl.update(S + 1e-6, False), Decision(1.5))
        self.assertEqual(sl.update(S, True), Decision(1.5))  # buffering blocks the exit
        self.assertIs(sl.mode, Mode.CATCHING_UP)
        self.assertIs(sl.mode, Mode.CATCHING_UP)
        self.assertEqual(sl.update(100, False), Decision(1.5))  # never reopens
        self.assertEqual(sl.update(S, False), Decision(1.0))
        self.assertIs(sl.mode, Mode.LIVE)

    def test_go_live_and_speed_ignored_in_live(self):
        sl = make(Mode.CATCHING_UP)
        sl.user_go_live()
        self.assertIs(sl.mode, Mode.LIVE)
        sl.user_speed(2.0)
        self.assertIs(sl.mode, Mode.LIVE)
        self.assertEqual(sl.update(0, False), Decision(1.0))

    def test_set_latency(self):
        sl = StayLive("low")
        self.assertEqual(sl.target, 1.0)
        sl.set_latency("stable")
        self.assertEqual(sl.target, 6.0)
        self.assertEqual(sl.update(6.0 + 0.76, False), Decision(1.05))
        with self.assertRaises(KeyError):
            sl.set_latency("bogus")


class RoomTests(unittest.TestCase):
    """A bursty source (2.0.1, the owner's ruling): below half a burst of room when a burst
    lands, play at 0.95x until it is built. The lag is the sawtooth's mean (LiveSession's),
    which is the room at a landing plus half a burst: half a burst of room is a mean lag of
    one burst."""

    def live(self, *, burst=10.0, target=10.0):
        stay = StayLive("normal")
        stay.target, stay.burst = target, burst
        return stay

    def rates(self, stay, lags, buffering=False):
        return [stay.update(lag, buffering).rate for lag in lags]

    def test_under_half_a_burst_of_room_slows_until_it_is_built(self):
        stay = self.live()
        lags = (9.4, 9.6, 9.99, 10.0, 9.6, 9.51, 9.49)
        self.assertEqual(self.rates(stay, lags), [0.95, 0.95, 0.95, 1.0, 1.0, 1.0, 0.95])
        self.assertIs(stay.mode, Mode.LIVE)

    def test_never_below_0_95(self):
        stay = self.live()
        self.assertEqual(set(self.rates(stay, (0.0, 2.0, 5.0, 9.0))), {0.95})

    def test_a_steady_source_never_slows(self):
        stay = self.live(burst=0.0, target=T)
        self.assertEqual(set(self.rates(stay, (0.0, 0.5, 1.0, 2.0))), {1.0})

    def test_a_latency_target_above_the_burst(self):
        stay = self.live(burst=4.0, target=TARGETS["stable"])
        self.assertEqual(self.rates(stay, (3.4, 3.9, 4.0, 6.0)), [0.95, 0.95, 1.0, 1.0])

    def test_buffering_keeps_the_rate(self):
        stay = self.live()
        self.assertEqual(self.rates(stay, (0.0,), buffering=True), [1.0])
        self.assertEqual(self.rates(stay, (5.0, 0.0), buffering=True), [1.0, 1.0])
        self.assertEqual(self.rates(stay, (5.0,)), [0.95])
        self.assertEqual(self.rates(stay, (12.0,), buffering=True), [0.95])

    def test_never_while_time_shifted_or_at_a_chosen_speed(self):
        stay = self.live()
        stay.user_paused()
        self.assertEqual(self.rates(stay, (0.0, 5.0)), [1.0, 1.0])
        stay.user_speed(1.5)
        self.assertEqual(self.rates(stay, (12.0,)), [1.5])

    def test_go_live_starts_at_normal_speed(self):
        stay = self.live()
        self.rates(stay, (5.0,))
        stay.user_go_live()  # Go live lands at the target less a quarter second at most
        self.assertEqual(self.rates(stay, (9.75, 9.9)), [1.0, 1.0])

    def test_a_source_that_turns_steady_while_slowing_plays_at_normal_speed(self):
        stay = self.live(burst=6.0, target=6.0)
        self.assertEqual(self.rates(stay, (4.0,)), [0.95])
        stay.target, stay.burst = T, 0.0  # steady after all: the latency's target
        self.assertEqual(self.rates(stay, (T + 0.5,)), [1.0])  # not 0.95 in the hold band

    def test_a_smaller_burst_while_slowing_plays_at_normal_speed(self):
        stay = self.live()
        self.assertEqual(self.rates(stay, (8.0,)), [0.95])
        stay.target, stay.burst = 4.0, 4.0
        self.assertEqual(self.rates(stay, (4.5,)), [1.0])

    def test_a_catch_up_settles_above_the_room_and_never_slows(self):
        stay = self.live()
        lags = (14.0, 12.0, 10.2, 10.1, 10.0)
        self.assertEqual(self.rates(stay, lags), [1.10, 1.05, 1.0, 1.0, 1.0])


class RateLockTests(unittest.TestCase):
    """2.0.2: on some channels VLC cannot play at any rate but 1x (it crawls). Once that is
    seen, stay-live keeps 1x for the channel; only a lag far beyond the target goes live."""

    def test_a_locked_rate_stays_at_normal_speed_whatever_the_lag(self):
        for burst in (0.0, 10.0):
            with self.subTest(burst=burst):
                stay = StayLive("normal")
                stay.burst, stay.target = burst, max(T, burst)
                self.assertEqual(stay.update(stay.target + 5, False).rate, 1.10)
                stay.lock_rate()
                self.assertTrue(stay.rate_locked)
                lags = (0.0, 1.0, stay.target + 1, stay.target + 5, stay.target + 19)
                self.assertEqual({stay.update(lag, False).rate for lag in lags}, {1.0})
                self.assertEqual(stay.update(stay.target + 5, True), Decision(1.0))

    def test_a_locked_rate_still_goes_live_once_when_far_behind(self):
        stay = StayLive("normal")
        stay.lock_rate()
        flags = [stay.update(R + 1, False) for _ in range(3)]
        self.assertEqual(flags, [Decision(1.0, True), Decision(1.0), Decision(1.0)])
        stay.user_go_live()
        self.assertTrue(stay.rate_locked)  # for the channel, not until Go live
        self.assertEqual(stay.update(N + 1, False), Decision(1.0))
        self.assertTrue(stay.update(R + 1, False).reopen_live)

    def test_a_speed_the_user_chooses_still_applies(self):
        stay = StayLive("normal")
        stay.lock_rate()
        stay.user_paused()
        stay.user_speed(1.5)
        self.assertEqual(stay.update(100.0, False), Decision(1.5))


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
    """A raised Auto target is reached without a jump back: at 0.95x while the rate may
    change (`build`), else at 1x as starvations add room (2.0.2)."""

    def live(self, target):
        stay = StayLive(AUTO)
        stay.target = stay.build = target
        return stay

    def rates(self, stay, lags):
        return [stay.update(lag, False).rate for lag in lags]

    def test_under_a_raised_target_it_slows_until_there(self):
        stay = self.live(5.5)
        lags = (1.0, 4.9, 5.4, 5.5, 5.2, 4.99)
        self.assertEqual(self.rates(stay, lags), [0.95, 0.95, 0.95, 1.0, 1.0, 0.95])

    def test_nothing_to_build_plays_at_normal_speed_below_the_target(self):
        stay = self.live(TARGETS["normal"])
        stay.build = 0.0
        self.assertEqual(set(self.rates(stay, (0.0, 1.0, 2.0))), {1.0})

    def test_a_locked_rate_never_slows_to_build(self):
        stay = self.live(5.5)
        stay.lock_rate()
        self.assertEqual(set(self.rates(stay, (0.0, 1.0, 5.0))), {1.0})

    def test_the_larger_of_the_burst_and_the_raised_target_is_built(self):
        stay = self.live(12.0)
        stay.burst = 10.0
        self.assertEqual(self.rates(stay, (11.0, 11.9, 12.0)), [0.95, 0.95, 1.0])


if __name__ == "__main__":
    unittest.main()
