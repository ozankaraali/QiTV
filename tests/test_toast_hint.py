import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.overlays.toast import Toast


def wait_until(predicate, timeout_ms=2000):
    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(10)
        waited += 10
    return predicate()


class ToastHintTests(unittest.TestCase):
    """A hint stays up until it is cleared; messages show over it and then give it back."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.toast = Toast(self.host)
        self.host.show()

    def test_a_hint_stays_until_it_is_cleared(self):
        self.toast.set_hint("Loading Home…")
        self.assertTrue(self.toast.isVisible())
        self.assertEqual(self.toast.text(), "Loading Home…")
        QTest.qWait(100)
        self.assertTrue(self.toast.isVisible())
        self.toast.set_hint(None)
        self.assertFalse(self.toast.isVisible())

    def test_a_message_shows_over_the_hint_and_then_gives_it_back(self):
        self.toast.set_hint("Add a provider")
        self.toast.show_message("Volume 50%", "info", 60)
        self.assertEqual(self.toast.text(), "Volume 50%")
        self.assertTrue(wait_until(lambda: self.toast.text() == "Add a provider"))
        self.assertTrue(self.toast.isVisible())
        QTest.qWait(150)
        self.assertTrue(self.toast.isVisible())

    def test_clearing_the_hint_during_a_message_keeps_the_message_until_it_ends(self):
        self.toast.set_hint("Loading Home…")
        self.toast.show_message("Home: refused", "error", 80)
        self.toast.set_hint(None)
        self.assertEqual(self.toast.text(), "Home: refused")
        self.assertTrue(self.toast.isVisible())
        self.assertTrue(wait_until(lambda: not self.toast.isVisible()))

    def test_a_new_hint_cuts_a_message_short_but_the_same_hint_does_not(self):
        self.toast.set_hint("Loading Home…")
        self.toast.show_message("Home: refused", "error", 5000)
        self.toast.set_hint("Loading Home…")  # e.g. set again: the message stays
        self.assertEqual(self.toast.text(), "Home: refused")
        self.toast.set_hint(None)
        self.toast.set_hint("Loading Backup…")  # a new setup: the old error is history
        self.assertEqual(self.toast.text(), "Loading Backup…")
        QTest.qWait(100)
        self.assertEqual(self.toast.text(), "Loading Backup…")

    def test_a_new_hint_replaces_the_old_one(self):
        self.toast.set_hint("Loading Home…")
        self.toast.set_hint("Loading Backup…")
        self.assertEqual(self.toast.text(), "Loading Backup…")
        self.assertTrue(self.toast.isVisible())

    def test_without_a_hint_a_message_hides_when_it_ends(self):
        self.toast.show_message("Muted", "info", 40)
        self.assertTrue(wait_until(lambda: not self.toast.isVisible()))


if __name__ == "__main__":
    unittest.main()
