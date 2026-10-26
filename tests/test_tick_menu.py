import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui.tick_menu import TickMenu


class TickMenuTests(unittest.TestCase):  # 2.8.1: the Library's provider ticks
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.menu = TickMenu()
        self.addCleanup(self.menu.deleteLater)
        self.tick = self.menu.addAction("Demo TV")
        self.tick.setCheckable(True)
        self.done = []
        self.menu.addAction("Refresh").triggered.connect(lambda: self.done.append("refresh"))
        self.menu.popup(self.menu.pos())
        self.assertTrue(QTest.qWaitForWindowExposed(self.menu))

    def click(self, action):
        QTest.mouseClick(
            self.menu, Qt.MouseButton.LeftButton, pos=self.menu.actionGeometry(action).center()
        )

    def test_a_click_ticks_and_the_menu_stays_open(self):
        self.click(self.tick)
        self.assertTrue(self.tick.isChecked())
        self.assertTrue(self.menu.isVisible())
        self.click(self.tick)
        self.assertFalse(self.tick.isChecked())
        self.assertTrue(self.menu.isVisible())

    def test_return_and_space_tick_the_highlighted_entry(self):
        self.menu.setActiveAction(self.tick)
        QTest.keyClick(self.menu, Qt.Key.Key_Return)
        self.assertTrue(self.tick.isChecked())
        QTest.keyClick(self.menu, Qt.Key.Key_Space)
        self.assertFalse(self.tick.isChecked())
        self.assertTrue(self.menu.isVisible())

    def test_another_entry_acts_and_closes_the_menu(self):
        self.click(self.menu.actions()[1])
        self.assertEqual(self.done, ["refresh"])
        self.assertFalse(self.menu.isVisible())

    def test_a_release_without_a_press_in_the_menu_ticks_nothing(self):
        # The click that opened the menu ends over it: that release is not a choice.
        QTest.mouseRelease(
            self.menu, Qt.MouseButton.LeftButton, pos=self.menu.actionGeometry(self.tick).center()
        )
        self.assertFalse(self.tick.isChecked())


if __name__ == "__main__":
    unittest.main()
