import unittest

from services.remote.protocol import steps_for
from ui.remote_repeat import KeyRepeater


class RepeaterCase(unittest.TestCase):
    def setUp(self):
        self.fired: list[str] = []
        self.r = KeyRepeater(self.fired.append)

    def run_until(self, end_ms: int, step_ms: int = 5, start_ms: int = 0) -> None:
        for now in range(start_ms, end_ms + 1, step_ms):
            self.r.advance(now)


class RepeatTests(RepeaterCase):
    def test_tap_fires_once(self):
        self.r.down("a", "down", 0)
        self.r.up("a", "down", 1, 50)
        self.run_until(1000)
        self.assertEqual(self.fired, ["down"])

    def test_hold_matches_formula(self):
        self.r.down("a", "down", 0)
        for now in range(0, 1201, 5):
            if now % 200 == 0:
                self.r.held("a", now)
            self.r.advance(now)
        self.assertEqual(len(self.fired), steps_for(1200))

    def test_up_catches_up_missing_steps(self):
        self.r.down("a", "down", 0)
        self.run_until(420)
        self.assertEqual(len(self.fired), 2)
        self.r.up("a", "down", 5, 420)
        self.assertEqual(self.fired, ["down"] * 5)

    def test_overshoot_steps_back_on_lists(self):
        self.r.down("a", "down", 0)
        for now in range(0, 1201, 5):
            if now % 200 == 0:
                self.r.held("a", now)
            self.r.advance(now)
        self.assertEqual(len(self.fired), 22)
        self.r.up("a", "down", 20, 1200)
        self.assertEqual(self.fired, ["down"] * 22 + ["up"] * 2)

    def test_no_step_back_for_volume(self):
        self.r.down("a", "volup", 0)
        for now in range(0, 761, 5):
            if now % 200 == 0:
                self.r.held("a", now)
            self.r.advance(now)
        self.assertEqual(len(self.fired), 11)
        self.r.up("a", "volup", 8, 760)
        self.assertEqual(self.fired, ["volup"] * 11)

    def test_buttons_that_do_not_repeat(self):
        self.r.down("a", "ok", 0)
        self.run_until(2000)
        self.r.up("a", "ok", 30, 2000)
        self.assertEqual(self.fired, ["ok"])

    def test_silence_releases_the_hold(self):
        self.r.down("a", "down", 0)
        self.run_until(200)
        self.r.held("a", 200)
        self.run_until(3000, start_ms=200)
        self.assertLessEqual(len(self.fired), steps_for(800))
        self.assertIsNone(self.r.next_due())
        self.r.up("a", "down", 70, 3000)  # a late release corrects nothing
        self.assertLessEqual(len(self.fired), steps_for(800))

    def test_one_hold_at_a_time(self):
        self.r.down("a", "down", 0)
        self.r.held("a", 0)
        self.run_until(500)
        downs = self.fired.count("down")
        self.r.down("b", "right", 500)
        for now in range(500, 1200, 5):
            if now % 200 == 0:
                self.r.held("b", now)
            self.r.advance(now)
        self.assertEqual(self.fired.count("down"), downs)
        self.assertGreater(self.fired.count("right"), 1)

    def test_drop_ends_hold_of_that_session_only(self):
        self.r.down("a", "down", 0)
        self.r.drop("b")
        for now in range(0, 600, 5):
            if now % 200 == 0:
                self.r.held("a", now)
            self.r.advance(now)
        before = len(self.fired)
        self.assertGreater(before, 1)
        self.r.drop("a")
        self.run_until(1500, start_ms=600)
        self.assertEqual(len(self.fired), before)

    def test_next_due(self):
        self.assertIsNone(self.r.next_due())
        self.r.down("a", "down", 100)
        self.assertEqual(self.r.next_due(), 500)
        self.r.advance(500)
        self.assertEqual(self.r.next_due(), 540)

    def test_held_from_another_phone_does_not_keep_alive(self):
        self.r.down("a", "down", 0)
        for now in range(0, 2000, 5):
            if now % 200 == 0:
                self.r.held("b", now)
            self.r.advance(now)
        self.assertIsNone(self.r.next_due())


if __name__ == "__main__":
    unittest.main()
