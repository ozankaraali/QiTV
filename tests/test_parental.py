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


class HandEditedTests(unittest.TestCase):  # 2.8 review M8
    def test_wrong_shapes_in_the_settings_are_left_out(self):
        for hidden in (5, "text", [{"kind": "group"}, {"kind": "item", "key": 7}, "x", None]):
            with self.subTest(hidden=hidden):
                config = FakeConfig()
                config.parental = {"version": 1, "hidden": hidden}
                guard = Parental(config)  # no error
                self.assertFalse(guard.protects("live", NEWS))


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
        self.guard.unlock("1234")
        self.assertTrue(self.guard.show_again(self.guard.hidden()[0]["key"]))
        self.assertEqual(self.guard.hidden(), [])
        self.guard.lock()
        self.guard.hide_group("live", "P", "66")
        self.assertFalse(self.guard.clear_pin("0000"))
        self.assertTrue(self.guard.clear_pin("1234"))
        self.assertFalse(self.guard.has_pin())
        self.assertFalse(self.guard.active())

    def test_showing_again_needs_the_pin(self):  # review C1: a child can't undo it
        self.guard.set_pin("1234")
        self.guard.hide_group("live", "P", "66")
        key = self.guard.hidden()[0]["key"]
        self.assertFalse(self.guard.show_again(key))  # locked
        self.assertTrue(self.guard.hides_item("live", ADULT))
        self.guard.unlock("1234")
        self.assertTrue(self.guard.show_again(key))
        self.assertEqual(self.guard.hidden(), [])

    def test_a_title_in_a_group_named_adult_is_told_with_no_pin_or_rule(self):  # 2.8: TMDB
        titles = {"66": "XXX Late", "10": "News"}
        self.guard.group_title = lambda section, provider, gid: titles.get(gid, "")
        self.assertFalse(self.guard.has_pin())
        self.assertTrue(self.guard.in_adult_group("movies", ADULT))
        self.assertFalse(self.guard.in_adult_group("movies", NEWS))
        later = {"id": "3", "name": "Night", "category_id": "88", "_provider": "P"}
        self.assertFalse(self.guard.in_adult_group("movies", later))  # its group not known yet
        titles["88"] = "18+ Films"
        self.assertTrue(self.guard.in_adult_group("movies", later))  # asked again once known
        self.assertFalse(Parental(self.config).in_adult_group("movies", ADULT))  # no names

    def test_the_adult_rule_hides_groups_as_they_come(self):  # review I8
        titles = {"66": "XXX Late", "10": "News"}
        self.guard.group_title = lambda section, provider, gid: titles.get(gid, "")
        self.assertFalse(self.guard.set_adult_rule(True))  # a PIN comes first
        self.guard.set_pin("1234")
        self.assertTrue(self.guard.set_adult_rule(True))
        self.assertTrue(self.guard.active())
        self.assertTrue(self.guard.hides_group("live", "P", "66", title="XXX Late"))
        self.assertFalse(self.guard.hides_group("live", "P", "10", title="News"))
        self.assertEqual(self.guard.visible("live", [NEWS, ADULT]), [NEWS])
        titles["77"] = "Adults only"  # a group the provider adds later
        later = {"id": "3", "name": "Night", "category_id": "77", "_provider": "P"}
        self.assertTrue(self.guard.hides_item("live", later))
        self.assertTrue(self.guard.protects("live", ADULT))  # kept out of the history too
        self.assertFalse(self.guard.set_adult_rule(False))  # locked: only with the PIN
        again = Parental(self.config, clock=self.clock)
        self.assertTrue(again.adult_rule())
        self.guard.unlock("1234")
        self.assertFalse(self.guard.hides_item("live", ADULT))
        self.assertTrue(self.guard.set_adult_rule(False))
        self.assertFalse(self.guard.protects("live", ADULT))

    def test_an_episode_of_a_hidden_series_genre_is_protected(self):  # review I6
        show = {"series_id": "s1", "name": "Show", "category_id": "5", "_provider": "P"}
        episode = {"id": "e1", "name": "Episode 1", "_provider": "P"}
        self.guard.set_pin("1234")
        self.guard.hide_group("series", "P", "5")
        self.assertFalse(self.guard.protects("series", episode))
        self.assertTrue(self.guard.protects_play("series", episode, {"series": show}))
        self.assertFalse(self.guard.protects_play("series", episode, {"series": NEWS}))
        self.assertFalse(self.guard.protects_play("live", ADULT, None))  # nothing live hidden

    def test_groups_that_look_adult(self):
        for title in ("XXX", "Adult Movies", "18+ Channels", "TR: +18", "Erotic"):
            self.assertTrue(looks_adult(title), title)
        for title in ("News", "Sports 18", "Kids", "Documentary"):
            self.assertFalse(looks_adult(title), title)

    def test_adult_in_an_everyday_phrase_is_not(self):  # a cartoon block, a school, a genre
        for title in (
            "Adult swim lessons",
            "US: ADULT SWIM HD",
            "Adult education",
            "Adult learning",
            "Young adult",
            "Young Adult Films",
        ):
            self.assertFalse(looks_adult(title), title)
        for title in ("Adult", "ADULTS ONLY", "TR: ADULT", "Adult | VIP", "For adults"):
            self.assertTrue(looks_adult(title), title)


if __name__ == "__main__":
    unittest.main()
