"""2.6: the pill offering where the viewer left a channel they came back to."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.overlays.back_notice import BackNotice

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


if __name__ == "__main__":
    unittest.main()
