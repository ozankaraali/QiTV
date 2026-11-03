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


class SettingsBoxTests(unittest.TestCase):
    """Settings → Providers → Hidden groups lists them and shows one again, no PIN."""

    @classmethod
    def setUpClass(cls):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_a_group_is_listed_and_shown_again(self):
        from PySide6.QtWidgets import QDialog, QFormLayout, QWidget

        from ui.hidden_groups_settings import add_hidden_groups_box

        window = QWidget()
        self.addCleanup(window.deleteLater)
        window.hidden_groups = HiddenGroups(kept())
        window.hidden_groups.hide("live", "P", "12", "Foreign news")
        changed = []
        window.controller = SimpleNamespace(lists_changed=lambda: changed.append(True))
        dialog = QDialog(window)
        tab = QWidget(dialog)
        layout = QFormLayout(tab)
        box = add_hidden_groups_box(dialog, layout, tab)
        widgets = dialog.hidden_groups_widgets
        self.assertEqual(widgets["list"].item(0).text(), "Foreign news (Live TV, P)")
        widgets["show_again"].click()
        self.assertEqual(changed, [True])
        self.assertEqual(widgets["list"].count(), 0)
        self.assertTrue(box.isHidden())  # nothing hidden: no box
