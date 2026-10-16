import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from ui.pointer import PointerInput, SecondClickGuard


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


class _Page(QWidget):
    """A page under the pointer: it records the mouse events that reach it."""

    def __init__(self):
        super().__init__()
        self.events = []
        self.resize(400, 300)
        self.show()

    def mousePressEvent(self, event):
        self.events.append("press")

    def mouseReleaseEvent(self, event):
        self.events.append("release")

    def mouseDoubleClickEvent(self, event):
        self.events.append("double")


class SecondClickGuardTests(unittest.TestCase):
    """A click that shows another page (‹ Library): a double-click's second click would land
    on whatever that page shows under the pointer (2.0.1 review M1)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.page = _Page()
        self.addCleanup(self.page.deleteLater)
        self.addCleanup(self.page.close)
        self.guard = SecondClickGuard()
        self.addCleanup(self.guard.deleteLater)

    def click(self):
        QTest.mouseClick(self.page.windowHandle(), Qt.MouseButton.LeftButton, pos=QPoint(50, 50))

    def test_the_next_click_soon_after_reaches_nothing_and_only_that_one(self):
        self.guard.arm()
        self.click()
        self.assertEqual(self.page.events, [])
        self.click()
        self.assertEqual(self.page.events, ["press", "release"])

    def test_a_second_click_reported_as_a_double_click_reaches_nothing(self):
        self.guard.arm()
        window, point = self.page.windowHandle(), QPointF(50, 50)
        for kind in (QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseButtonRelease):
            left = Qt.MouseButton.LeftButton
            held = left if kind is QEvent.Type.MouseButtonDblClick else Qt.MouseButton.NoButton
            event = QMouseEvent(kind, point, point, left, held, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(window, event)
        self.assertEqual(self.page.events, [])

    def test_a_click_after_the_double_click_interval_reaches_the_page(self):
        self.guard.arm()
        elapse(QApplication.doubleClickInterval() + 100)
        self.click()
        self.assertEqual(self.page.events, ["press", "release"])


if __name__ == "__main__":
    unittest.main()
