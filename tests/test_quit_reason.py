"""Why QiTV's window closes, in its log (2.8): QiTV's own Quit, a quit request from outside
(the Dock, another app, logging out) and the window's button are told apart."""

import unittest

from PySide6.QtCore import QEvent

from ui import quit_reason


class _Event:
    def __init__(self, kind, spontaneous):
        self._kind, self._spontaneous = kind, spontaneous

    def type(self):
        return self._kind

    def spontaneous(self):
        return self._spontaneous


class DescribeTests(unittest.TestCase):
    def test_each_way_of_closing_has_its_own_words(self):
        self.assertEqual(
            quit_reason.describe(False, quit_reason.MENU), "QiTV's Quit, chosen in QiTV"
        )
        self.assertIn("the Dock, another app or logging out", quit_reason.describe(False, "system"))
        self.assertEqual(
            quit_reason.describe(True, None), "the window's close button or the system"
        )
        self.assertEqual(quit_reason.describe(False, None), "QiTV closed its window")

    def test_a_request_from_outside_wins_over_the_window_button(self):
        self.assertIn("asked QiTV to quit", quit_reason.describe(True, quit_reason.SYSTEM))

    def test_the_quit_action_counts_as_chosen_only_while_qitv_is_the_active_app(self):
        self.assertEqual(quit_reason.asked_by_menu(True), quit_reason.MENU)
        self.assertEqual(quit_reason.asked_by_menu(False), quit_reason.SYSTEM)

    def test_only_a_spontaneous_quit_event_is_the_systems(self):
        self.assertTrue(quit_reason.is_system_quit(_Event(QEvent.Type.Quit, True)))
        self.assertFalse(quit_reason.is_system_quit(_Event(QEvent.Type.Quit, False)))
        self.assertFalse(quit_reason.is_system_quit(_Event(QEvent.Type.Close, True)))


if __name__ == "__main__":
    unittest.main()
