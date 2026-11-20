"""Groups hidden to tidy the lists (services.hidden_groups, 2.9.1)."""

from types import SimpleNamespace
import unittest

from services.hidden_groups import HiddenGroups
from services.provider_rename import Renames


def kept():
    config = SimpleNamespace(hidden_groups=None, saves=0)
    config.save_config = lambda: setattr(config, "saves", config.saves + 1)
    return config


class HiddenGroupsTests(unittest.TestCase):
    def test_a_group_is_hidden_once_kept_and_shown_again(self):
        config = kept()
        tidy = HiddenGroups(config)
        version = tidy.version
        self.assertTrue(tidy.hide("live", "P", "12", "Foreign news"))
        self.assertFalse(tidy.hide("live", "P", "12", "Foreign news"))  # already
        self.assertTrue(tidy.hides("live", "P", "12"))
        self.assertFalse(tidy.hides("movies", "P", "12"))  # another section's
        self.assertFalse(tidy.hides("live", "Q", "12"))  # another provider's
        self.assertNotEqual(tidy.version, version)
        self.assertEqual(config.saves, 1)
        again = HiddenGroups(config)  # the next run
        self.assertTrue(again.hides("live", "P", "12"))
        [entry] = again.entries()
        self.assertEqual(entry["title"], "Foreign news")
        self.assertTrue(again.show_again(entry["key"]))
        self.assertFalse(again.hides("live", "P", "12"))
        self.assertFalse(again.show_again(entry["key"]))

    def test_another_version_or_a_bad_entry_is_left_out(self):
        config = kept()
        config.hidden_groups = {"version": 2, "groups": [{"key": "x"}]}
        self.assertEqual(HiddenGroups(config).entries(), [])
        config.hidden_groups = {"version": 1, "groups": ["x", {"title": "no key"}]}
        self.assertEqual(HiddenGroups(config).entries(), [])

    def test_a_renamed_provider_keeps_its_hidden_groups(self):
        config = kept()
        tidy = HiddenGroups(config)
        tidy.hide("live", "Old", "12", "News")
        self.assertTrue(tidy.rename_providers(Renames({"Old": "New"})))
        self.assertTrue(tidy.hides("live", "New", "12"))
        self.assertFalse(tidy.hides("live", "Old", "12"))


if __name__ == "__main__":
    unittest.main()
