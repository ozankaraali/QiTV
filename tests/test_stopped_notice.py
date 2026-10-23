"""2.6: the pill after Stop: nothing plays, and Play again is a click away."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.overlays.stopped_notice import StoppedNotice

app = QApplication.instance() or QApplication([])


class StoppedNoticeTests(unittest.TestCase):
    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.close)
        self.notice = StoppedNotice(self.host)

    def test_it_says_stopped_and_offers_to_play_again(self):
        self.notice.show_notice("Stopped")
        self.assertTrue(self.notice.isVisible())
        self.assertEqual(self.notice.button_text(), "Play again")

    def test_a_click_plays_again(self):
        spy = QSignalSpy(self.notice.keepWatching)
        self.notice.show_notice("Stopped")
        center = self.notice.button_rect().center()
        QTest.mouseClick(
            self.notice,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPoint(round(center.x()), round(center.y())),
        )
        self.assertEqual(spy.count(), 1)


if __name__ == "__main__":
    unittest.main()
