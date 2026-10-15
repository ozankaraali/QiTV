import unittest

from services.live.stay_live import TARGETS, Decision, Mode, StayLive

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


if __name__ == "__main__":
    unittest.main()
