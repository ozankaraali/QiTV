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


if __name__ == "__main__":
    unittest.main()
