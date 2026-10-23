"""2.6: the REC sign of recordings going on while something else is watched."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.overlays.rec_sign import RecSign


class RecSignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.now = [163.0]
        self.sign = RecSign(self.host, clock=lambda: self.now[0])
        self.back, self.stopped = [], []
        self.sign.goBack.connect(self.back.append)
        self.sign.stop.connect(self.stopped.append)

    def tearDown(self):
        self.host.deleteLater()

    def test_it_shows_for_a_recording_and_counts_its_time(self):
        self.assertTrue(self.sign.isHidden())
        self.sign.set_recordings([("main", "One", 100.0)])
        self.assertTrue(self.sign.isVisibleTo(self.host))
        self.assertEqual(self.sign.text(), "REC One 01:03")
        self.now[0] = 100.0 + 3723
        self.assertEqual(self.sign.text(), "REC One 1:02:03")

    def test_several_are_counted(self):
        self.sign.set_recordings([("main", "One", 100.0), ("b1", "Two", 120.0)])
        self.assertEqual(self.sign.text(), "REC 2 channels")

    def test_none_hides_it(self):
        self.sign.set_recordings([("main", "One", 100.0)])
        self.sign.set_recordings([])
        self.assertTrue(self.sign.isHidden())

    def test_its_menu_goes_back_or_stops(self):
        self.sign.set_recordings([("main", "One", 100.0), ("b1", "Two", 120.0)])
        menu = self.sign.menu()
        texts = [a.text() for a in menu.actions() if not a.isSeparator()]
        self.assertEqual(
            texts, ["Go back to One", "Stop recording", "Go back to Two", "Stop recording"]
        )
        actions = [a for a in menu.actions() if not a.isSeparator()]
        actions[0].trigger()
        actions[3].trigger()
        self.assertEqual((self.back, self.stopped), (["main"], ["b1"]))

    def test_a_click_opens_the_menu(self):
        self.sign.set_recordings([("main", "One", 100.0)])
        opened = []
        self.sign.open_menu = lambda: opened.append(True)
        QTest.mouseClick(self.sign, Qt.MouseButton.LeftButton, pos=self.sign.rect().center())
        self.assertEqual(opened, [True])


if __name__ == "__main__":
    unittest.main()
