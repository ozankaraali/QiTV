"""2.6: the pill offering where the viewer left a channel they came back to."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.keymap import key_hint
from ui.overlays.back_notice import BackNotice
from ui.overlays.sleep_notice import SleepNotice

app = QApplication.instance() or QApplication([])


class BackNoticeTests(unittest.TestCase):
    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.close)
        self.notice = BackNotice(self.host)

    def test_it_says_when_and_offers_to_resume(self):
        self.notice.show_notice("Left off at 20:10")
        self.assertTrue(self.notice.isVisible())
        self.assertEqual(self.notice.text(), "Left off at 20:10")
        self.assertEqual(self.notice.button_text(), "Resume")
        self.assertLess(self.notice.geometry().top(), 720 // 3)  # at the top, as the sleep pill

    def test_a_click_resumes(self):
        spy = QSignalSpy(self.notice.keepWatching)
        self.notice.show_notice("Left off at 20:10")
        center = self.notice.button_rect().center()
        QTest.mouseClick(
            self.notice,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(round(center.x()), round(center.y())),
        )
        self.assertEqual(spy.count(), 1)

    def test_its_x_closes_it_and_never_resumes(self):
        # 2.13.3: a small ✕ at its right end, after Resume, as the channel list's find box has.
        resume, close = QSignalSpy(self.notice.keepWatching), QSignalSpy(self.notice.closeRequested)
        self.notice.show_notice("Left off at 20:10")
        x = self.notice.close_rect()
        self.assertFalse(x.isEmpty())
        self.assertGreaterEqual(x.left(), self.notice.button_rect().right())
        self.assertLessEqual(x.right(), self.notice.width())
        self.assertGreaterEqual(min(x.width(), x.height()), 24)  # a target the mouse can hit
        self.assertEqual(self.notice.tooltip_at(x.center()), key_hint("Close", "Esc"))
        button = self.notice.button_rect().center()
        self.assertEqual(self.notice.tooltip_at(button), key_hint("Resume", "Return"))
        QTest.mouseClick(
            self.notice,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            x.center().toPoint(),
        )
        self.assertEqual((resume.count(), close.count()), (0, 1))

    def test_the_sleep_timers_pill_keeps_no_x(self):
        # Its last minute warns that playback stops: closing the warning would hide the
        # countdown while the timer still runs. Only the Left-off pill has the ✕.
        class Closable(SleepNotice):
            closable = True

        plain, closable = SleepNotice(self.host), Closable(self.host)
        for notice in (plain, closable):
            notice.show_notice("Playback stops in 0:42")
        self.assertTrue(plain.close_rect().isEmpty())
        spy = QSignalSpy(plain.closeRequested)
        QTest.mouseClick(
            plain,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(plain.width() - 4, plain.height() // 2),
        )
        self.assertEqual(spy.count(), 0)
        # The ✕'s end takes the place of the plain pill's inner edge after its button.
        edge = plain.width() - plain.button_rect().right()
        grown = closable.width() - plain.width()
        self.assertAlmostEqual(grown, closable.close_rect().width() - edge, delta=2)


if __name__ == "__main__":
    unittest.main()
