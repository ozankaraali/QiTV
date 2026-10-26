"""Parental controls (2.8): a PIN, hidden groups and channels, locked by default, unlocked for a
while with the PIN; what they protect never reaches the history."""

import unittest

from services.parental import Parental, looks_adult


class FakeConfig:
    def __init__(self):
        self.parental = None
        self.saves = 0

    def save_config(self):
        self.saves += 1


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


NEWS = {"id": "1", "name": "News", "tv_genre_id": "10", "_provider": "P"}
ADULT = {"id": "2", "name": "Late", "tv_genre_id": "66", "_provider": "P"}


class ParentalTests(unittest.TestCase):
    def setUp(self):
        self.config, self.clock = FakeConfig(), FakeClock()
        self.guard = Parental(self.config, clock=self.clock)

    def test_without_a_pin_nothing_is_hidden_or_protected(self):
        self.assertFalse(self.guard.has_pin())
        self.assertFalse(self.guard.hide_group("live", "P", "66"))  # a PIN comes first
        self.assertFalse(self.guard.active())
        self.assertFalse(self.guard.protects("live", ADULT))

    def test_a_pin_is_kept_hashed_and_checked(self):
        self.guard.set_pin("1234")
        stored = self.config.parental
        self.assertEqual(stored["version"], 1)
        self.assertNotIn("1234", str(stored))
        self.assertTrue(self.guard.check("1234"))
        self.assertFalse(self.guard.check("4321"))
        self.assertTrue(Parental(self.config, clock=self.clock).check("1234"))  # read back

    def test_a_hidden_group_hides_its_channels_while_locked(self):
        self.guard.set_pin("1234")
        self.assertTrue(self.guard.hide_group("live", "P", "66"))
        self.assertTrue(self.guard.active())
        self.assertTrue(self.guard.hides_group("live", "P", "66"))
        self.assertFalse(self.guard.hides_group("live", "P", "10"))
        self.assertTrue(self.guard.hides_item("live", ADULT))
        self.assertFalse(self.guard.hides_item("live", NEWS))
        self.assertEqual(self.guard.visible("live", [NEWS, ADULT]), [NEWS])

    def test_a_hidden_channel_is_hidden_alone(self):
        self.guard.set_pin("1234")
        self.guard.hide_item("live", NEWS)
        self.assertEqual(self.guard.visible("live", [NEWS, ADULT]), [ADULT])

    def test_unlocked_with_the_pin_everything_shows_for_an_hour(self):
        self.guard.set_pin("1234")
        self.guard.hide_group("live", "P", "66")
        version = self.guard.version
        self.assertFalse(self.guard.unlock("0000"))
        self.assertTrue(self.guard.unlock("1234"))
        self.assertGreater(self.guard.version, version)
        self.assertFalse(self.guard.active())
        self.assertEqual(self.guard.visible("live", [NEWS, ADULT]), [NEWS, ADULT])
        self.assertTrue(self.guard.protects("live", ADULT))  # still kept out of the history
        self.clock.now += 3601
        self.assertTrue(self.guard.active())  # locked again by itself

    def test_lock_now(self):
        self.guard.set_pin("1234")
        self.guard.hide_group("live", "P", "66")
        self.guard.unlock("1234")
        self.guard.lock()
        self.assertTrue(self.guard.active())

    def test_showing_again_and_removing_the_pin(self):
        self.guard.set_pin("1234")
        self.guard.hide_group("live", "P", "66")
        self.assertEqual(len(self.guard.hidden()), 1)
        self.guard.show_again(self.guard.hidden()[0]["key"])
        self.assertEqual(self.guard.hidden(), [])
        self.guard.hide_group("live", "P", "66")
        self.assertFalse(self.guard.clear_pin("0000"))
        self.assertTrue(self.guard.clear_pin("1234"))
        self.assertFalse(self.guard.has_pin())
        self.assertFalse(self.guard.active())

    def test_groups_that_look_adult(self):
        for title in ("XXX", "Adult Movies", "18+ Channels", "TR: +18", "Erotic"):
            self.assertTrue(looks_adult(title), title)
        for title in ("News", "Sports 18", "Kids", "Documentary"):
            self.assertFalse(looks_adult(title), title)


if __name__ == "__main__":
    unittest.main()
