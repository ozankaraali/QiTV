"""Settings → Parental controls (2.8): its buttons follow the PIN and the lock; hiding the groups
that look adult hides them in every provider's loaded lists."""

import os
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QTabWidget, QWidget

from services.parental import Parental
from ui import parental_settings

app = QApplication.instance() or QApplication([])


class Kept:
    parental = None

    def save_config(self):
        pass


class FakeCatalog:
    def sections(self):
        return ["live"]

    def groups(self, section):
        return [
            SimpleNamespace(id="1", title="News", header=False),
            SimpleNamespace(id="66", title="XXX Adult", header=False),
        ]


class Window(QWidget):
    def __init__(self, guard):
        super().__init__()
        self.parental = guard
        self.changes = 0
        self.catalogs = SimpleNamespace(names=lambda: ["P"], catalog=lambda name: FakeCatalog())

    def parental_changed(self):
        self.changes += 1


def parental_settings_refresh(dialog):
    dialog.parental_refresh()


class ParentalTabTests(unittest.TestCase):
    def setUp(self):
        self.guard = Parental(Kept())
        self.window = Window(self.guard)
        self.addCleanup(self.window.deleteLater)
        self.dialog = QWidget(self.window)
        self.dialog.options_tab = QTabWidget(self.dialog)
        parental_settings.build_parental_tab(self.dialog)
        self.w = self.dialog.parental_widgets

    def set_pin(self, pin="2468"):
        with mock.patch.object(parental_settings, "_ask_pin", side_effect=[pin, pin]):
            self.w["set_pin"].click()

    def test_without_a_pin_only_setting_one_is_offered(self):
        self.assertEqual(self.w["set_pin"].text(), "Set PIN…")
        for name in ("remove_pin", "unlock", "lock", "adult", "show_again"):
            self.assertFalse(self.w[name].isEnabled(), name)

    def test_setting_a_pin_asks_it_twice(self):
        with mock.patch.object(parental_settings, "_ask_pin", side_effect=["2468", "2468"]):
            self.w["set_pin"].click()
        self.assertTrue(self.guard.check("2468"))
        self.assertEqual(self.w["set_pin"].text(), "Change PIN…")
        self.assertTrue(self.w["adult"].isEnabled())
        self.assertGreater(self.window.changes, 0)

    def test_a_short_pin_is_refused(self):
        with (
            mock.patch.object(parental_settings, "_ask_pin", side_effect=["12"]),
            mock.patch.object(QMessageBox, "warning"),
        ):
            self.w["set_pin"].click()
        self.assertFalse(self.guard.has_pin())

    def test_groups_that_look_adult_stay_hidden_as_they_come(self):  # review I8
        self.set_pin()
        self.w["adult"].click()  # a lasting rule, not this moment's lists
        self.assertTrue(self.guard.adult_rule())
        self.assertTrue(self.guard.hides_group("live", "P", "66", title="XXX Adult"))
        self.assertFalse(self.w["adult"].isEnabled())  # locked: unticking needs the PIN
        self.unlock()
        self.assertTrue(self.w["adult"].isEnabled())
        self.w["adult"].click()
        self.assertFalse(self.guard.adult_rule())

    def test_showing_again_needs_the_pin(self):  # review C1
        self.set_pin()
        self.guard.hide_group("live", "P", "66", title="XXX Adult")
        self.window.parental_changed()
        parental_settings_refresh(self.dialog)
        self.assertFalse(self.w["show_again"].isEnabled())
        self.assertEqual(self.w["hidden"].count(), 0)  # locked: not even listed
        self.unlock()
        self.assertEqual(self.w["hidden"].count(), 1)
        self.w["hidden"].setCurrentRow(0)
        self.assertTrue(self.w["show_again"].isEnabled())
        self.w["show_again"].click()
        self.assertEqual(self.guard.hidden(), [])

    def unlock(self, pin="2468"):
        with mock.patch.object(parental_settings, "_ask_pin", side_effect=[pin]):
            self.w["unlock"].click()

    def test_unlocking_asks_for_the_pin(self):
        self.set_pin()
        with mock.patch.object(parental_settings, "_ask_pin", side_effect=["2468"]):
            self.w["unlock"].click()
        self.assertFalse(self.guard.locked())
        self.w["lock"].click()
        self.assertTrue(self.guard.locked())


if __name__ == "__main__":
    unittest.main()
