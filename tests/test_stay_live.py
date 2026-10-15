import unittest

from services.live.stay_live import TARGETS, Decision, Mode, StayLive

T = TARGETS["normal"]
S, N, P, R = T + 0.2, T + 0.75, T + 3, T + 20


def make(mode, prev):
    sl = StayLive("normal")
    if mode is Mode.SHIFTED:
        sl.user_paused()
    elif mode is Mode.CATCHING_UP:
        sl.user_paused()
        sl.user_speed(1.5)
    sl._rate = prev
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
            (P - eps, False, 1.10, Decision(1.05)),  # above N: 1.05 even from 1.10
            (P, False, 1.0, Decision(1.05)),  # > N, not > P
            (P, False, 1.10, Decision(1.05)),
            (P + eps, False, 1.0, Decision(1.10)),
            (R, False, 1.10, Decision(1.10)),
            (R + eps, False, 1.10, Decision(1.0, True)),
            (R + eps, False, 1.0, Decision(1.0, True)),
            # buffering: keep rate, never reopen
            (R + 100, True, 1.05, Decision(1.05)),
            (0.0, True, 1.10, Decision(1.10)),
            (N + 1, True, 1.0, Decision(1.0)),
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

    def test_seek_back_enters_shifted(self):
        sl = StayLive()
        sl.user_seeked_back()
        self.assertIs(sl.mode, Mode.SHIFTED)

    def test_catching_up_returns_live_within_settle(self):
        sl = make(Mode.CATCHING_UP, 1.0)
        self.assertEqual(sl.update(S + 0.01, False), Decision(1.5))
        self.assertIs(sl.mode, Mode.CATCHING_UP)
        self.assertEqual(sl.update(100, False), Decision(1.5))  # never reopens
        self.assertEqual(sl.update(S, False), Decision(1.0))
        self.assertIs(sl.mode, Mode.LIVE)

    def test_go_live_and_speed_ignored_in_live(self):
        sl = make(Mode.CATCHING_UP, 1.0)
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
