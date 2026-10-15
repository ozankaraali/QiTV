import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ui.pointer import PointerInput


class PointerButtonTests(unittest.TestCase):
    """The mouse's Back/Forward buttons (spec §5.3: drawer layer up/down)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.pointer = PointerInput()
        self.got = []
        for name in ("backPressed", "forwardPressed", "doubleClicked", "clicked"):
            getattr(self.pointer, name).connect(lambda n=name: self.got.append(n))

    def test_back_and_forward_presses_are_reported_at_once(self):
        self.pointer.press(Qt.MouseButton.BackButton)
        self.pointer.press(Qt.MouseButton.ForwardButton)
        self.assertEqual(self.got, ["backPressed", "forwardPressed"])

    def test_a_quick_second_back_is_another_press_not_a_double_click(self):
        self.pointer.press(Qt.MouseButton.BackButton)
        self.pointer.double_click(Qt.MouseButton.BackButton)
        self.assertEqual(self.got, ["backPressed", "backPressed"])

    def test_a_left_double_click_is_still_one(self):
        self.pointer.press(Qt.MouseButton.LeftButton)
        self.pointer.double_click(Qt.MouseButton.LeftButton)
        self.assertEqual(self.got, ["doubleClicked"])


if __name__ == "__main__":
    unittest.main()
