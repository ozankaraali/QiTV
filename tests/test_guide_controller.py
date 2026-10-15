from datetime import datetime, timezone
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from services.zapping import ZapContext
from ui.guide_controller import GuideController
from ui.keymap import Action
from ui.overlays.guide import Guide

NOW = datetime(2026, 10, 15, 20, 30, tzinfo=timezone.utc)
CHANNELS = [{"id": i, "name": f"Chan {i}", "number": str(i)} for i in range(1, 131)]


class GuideControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.addCleanup(self.host.close)
        self.guide = Guide(None, lambda channel, start, limit: [], lambda: NOW, parent=self.host)
        self.host.show()
        self.controller = GuideController(self.guide, digits_ms=80)
        self.tunes = []
        self.controller.tuneRequested.connect(
            lambda channel, group, channels: self.tunes.append((channel, group, channels))
        )
        context = ZapContext("news", CHANNELS)
        context.set_current(CHANNELS[4])
        self.assertTrue(self.controller.open(context, CHANNELS[4], lambda: []))

    def current(self):
        return self.guide.current_channel()["name"]

    def test_opens_on_the_current_channel(self):
        self.assertTrue(self.controller.is_open())
        self.assertEqual(self.current(), "Chan 5")

    def test_digits_jump_to_a_channel_number(self):
        for digit in "12":
            self.assertTrue(self.controller.handle(Action.DIGIT, digit))
        self.assertEqual(self.current(), "Chan 12")
        self.controller.handle(Action.DIGIT, "3")
        self.assertEqual(self.current(), "Chan 123")
        for _ in range(30):  # the number ends after a pause (short waits: a slow paint
            QTest.qWait(10)  # must not swallow the timer's turn)
        self.controller.handle(Action.DIGIT, "7")
        self.assertEqual(self.current(), "Chan 7")
        self.controller.handle(Action.NAV_ACTIVATE)
        channel, group, channels = self.tunes[0]
        self.assertEqual((channel["name"], group, len(channels)), ("Chan 7", "news", 130))
        self.assertFalse(self.controller.is_open())

    def test_an_unknown_number_keeps_the_highlight(self):
        self.controller.handle(Action.DIGIT, "9")
        self.controller.handle(Action.DIGIT, "9")
        self.controller.handle(Action.DIGIT, "9")
        self.assertEqual(self.current(), "Chan 99")  # 999 does not exist

    def test_channel_keys_move_by_one(self):
        self.controller.handle(Action.CH_NEXT)
        self.assertEqual(self.current(), "Chan 6")
        self.controller.handle(Action.CH_PREV)
        self.controller.handle(Action.CH_PREV)
        self.assertEqual(self.current(), "Chan 4")

    def test_g_and_escape_close_and_the_drawer_takes_over(self):
        self.assertTrue(self.controller.handle(Action.GUIDE))
        self.assertFalse(self.controller.is_open())
        self.controller.open(ZapContext("news", CHANNELS), None, lambda: [])
        self.assertTrue(self.controller.handle(Action.ESCAPE))
        self.assertFalse(self.controller.is_open())
        self.controller.open(ZapContext("news", CHANNELS), None, lambda: [])
        self.assertFalse(self.controller.handle(Action.OPEN_DRAWER))  # closes, then usual
        self.assertFalse(self.controller.is_open())

    def test_playback_keys_are_not_the_guides(self):
        for action in (Action.PLAY_PAUSE, Action.MUTE, Action.FULLSCREEN, Action.FAVORITE):
            self.assertFalse(self.controller.handle(action))
        self.assertTrue(self.controller.handle(Action.MEDIA_NEXT))  # ignored, as in the drawer
        self.assertTrue(self.controller.is_open())

    def test_without_a_zap_group_it_lists_every_live_channel(self):
        self.controller.close()
        self.assertTrue(self.controller.open(ZapContext(), None, lambda: CHANNELS[:3]))
        self.assertEqual(self.current(), "Chan 1")
        self.controller.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.tunes[0][1], "*")
        self.controller.close()
        self.assertFalse(self.controller.open(ZapContext(), None, lambda: []))


if __name__ == "__main__":
    unittest.main()
