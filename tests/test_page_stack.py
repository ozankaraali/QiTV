import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QWidget

from ui.page_stack import CORNERS, PageStack

LEFT = Qt.MouseButton.LeftButton
NONE = Qt.MouseButton.NoButton


class FakeConfig:
    def __init__(self, corner=None):
        if corner is not None:
            self.corner_player_corner = corner
        self.saves = 0

    def save_config(self):
        self.saves += 1


def send(widget, kind, global_point, button=LEFT, buttons=LEFT):
    """A mouse event at a global point, delivered to `widget` as the platform would."""
    local = QPointF(widget.mapFromGlobal(global_point))
    event = QMouseEvent(kind, local, QPointF(global_point), button, buttons, Qt.NoModifier)
    QApplication.sendEvent(widget, event)
    return event


class StackCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_stack(self, width=1280, height=720, corner=None):
        self.config = FakeConfig(corner)
        self.player, self.library = QWidget(), QWidget()
        self.stack = PageStack(self.player, self.library, self.config)
        self.stack.resize(width, height)
        self.stack.show()
        self.addCleanup(self.stack.close)
        return self.stack

    def drag(self, stack, start, end, steps=4):
        """The left button down at `start` (stack coordinates) on the player page, moved to
        `end` and released there."""
        origin = stack.mapToGlobal(start)
        target = stack.mapToGlobal(end)
        send(self.player, QEvent.Type.MouseButtonPress, origin)
        for step in range(1, steps + 1):
            point = origin + (target - origin) * step / steps
            send(self.player, QEvent.Type.MouseMove, point, NONE, LEFT)
        send(self.player, QEvent.Type.MouseButtonRelease, target, LEFT, NONE)


class PageStackTests(StackCase):
    def test_the_player_fills_the_stack_and_the_library_hides_at_first(self):
        stack = self.make_stack()
        self.assertEqual(stack.mode(), PageStack.PLAYER)
        self.assertEqual(self.player.geometry(), stack.rect())
        self.assertTrue(self.player.isVisible())
        self.assertFalse(self.library.isVisible())
        self.assertIs(stack.currentWidget(), self.player)

    def test_the_library_alone_hides_the_player(self):
        stack = self.make_stack()
        stack.show_library(corner=False)
        self.assertEqual(stack.mode(), PageStack.LIBRARY)
        self.assertEqual(self.library.geometry(), stack.rect())
        self.assertFalse(self.player.isVisible())
        self.assertIs(self.player.parentWidget(), stack)
        self.assertIs(stack.currentWidget(), self.library)
        stack.show_player()
        self.assertEqual(self.player.geometry(), stack.rect())
        self.assertFalse(self.library.isVisible())

    def test_corner_mode_keeps_both_pages_and_resizes_the_player(self):
        stack = self.make_stack(width=1280, height=720)
        stack.show_library(corner=True)
        rect = stack.corner_rect()
        self.assertEqual(rect.width(), round(1280 / 3))
        self.assertAlmostEqual(rect.height() / rect.width(), 9 / 16, places=2)
        self.assertEqual(self.player.geometry(), rect)
        self.assertTrue(self.player.isVisible() and self.library.isVisible())
        self.assertIs(self.player.parentWidget(), stack)
        self.assertIs(self.library.parentWidget(), stack)
        self.assertEqual(self.library.geometry(), stack.rect())
        self.assertIs(stack.currentWidget(), self.library)

    def test_the_corner_is_the_saved_one_a_margin_in(self):
        stack = self.make_stack(corner="top-left")
        stack.show_library(corner=True)
        margin = round(1280 * 1.6 / 100)
        self.assertEqual(stack.corner_rect().topLeft(), QPoint(margin, margin))
        stack.set_corner("bottom-right")
        rect = stack.corner_rect()
        self.assertEqual(rect.right() + 1 + margin, 1280)
        self.assertEqual(rect.bottom() + 1 + margin, 720)

    def test_a_bad_saved_corner_is_the_default(self):
        stack = self.make_stack(corner="middle")
        self.assertEqual(stack.corner(), "bottom-right")

    def test_the_corner_follows_the_stacks_size(self):
        stack = self.make_stack()
        stack.show_library(corner=True)
        stack.resize(900, 600)
        self.assertEqual(stack.corner_rect().width(), round(900 / 3))
        self.assertEqual(self.player.geometry(), stack.corner_rect())

    def test_a_mode_change_is_reported_once(self):
        stack = self.make_stack()
        spy = QSignalSpy(stack.modeChanged)
        stack.show_library(corner=True)
        stack.show_library(corner=True)
        stack.show_library(corner=False)
        stack.show_player()
        self.assertEqual(
            [spy.at(i)[0] for i in range(spy.count())], ["corner", "library", "player"]
        )

    def test_dragging_snaps_to_the_nearest_corner_and_is_saved(self):
        stack = self.make_stack(width=1280, height=720)
        stack.show_library(corner=True)
        self.drag(stack, stack.corner_rect().center(), QPoint(200, 150))
        self.assertEqual(self.config.corner_player_corner, "top-left")
        self.assertEqual(stack.corner(), "top-left")
        self.assertEqual(self.player.geometry(), stack.corner_rect())
        self.assertEqual(self.config.saves, 1)

    def test_a_drag_that_stays_in_its_corner_saves_nothing_and_does_not_click(self):
        stack = self.make_stack()
        stack.show_library(corner=True)
        clicks = QSignalSpy(stack.cornerClicked)
        rect = stack.corner_rect()
        self.drag(stack, rect.center(), rect.center() - QPoint(40, 30))
        self.assertEqual(stack.corner(), "bottom-right")
        self.assertEqual(self.config.saves, 0)
        self.assertEqual(self.player.geometry(), rect)
        self.assertEqual(clicks.count(), 0)

    def test_a_click_without_a_drag_is_a_click_on_the_corner_player(self):
        stack = self.make_stack()
        stack.show_library(corner=True)
        clicks = QSignalSpy(stack.cornerClicked)
        point = stack.mapToGlobal(stack.corner_rect().center())
        send(self.player, QEvent.Type.MouseButtonPress, point)
        send(self.player, QEvent.Type.MouseButtonRelease, point, LEFT, NONE)
        self.assertEqual(clicks.count(), 1)
        self.assertEqual(stack.mode(), PageStack.CORNER)  # the window decides what a click does

    def test_a_click_on_the_corner_player_gives_the_keys_back_to_the_window(self):
        # Final review M1: macOS beeped at the next key typed in the Library after a click.
        stack = self.make_stack()
        stack.show_library(corner=True)
        point = stack.mapToGlobal(stack.corner_rect().center())
        with mock.patch("ui.page_stack.reclaim_first_responder") as reclaim:
            send(self.player, QEvent.Type.MouseButtonPress, point)
            send(self.player, QEvent.Type.MouseMove, point, NONE, LEFT)
            send(self.player, QEvent.Type.MouseButtonRelease, point, LEFT, NONE)
        self.assertEqual(reclaim.call_args_list, [mock.call(stack.window())] * 2)

    def test_the_mouse_on_the_player_is_the_stacks_only_in_the_corner(self):
        seen = []

        class View(QWidget):  # a video surface
            def mousePressEvent(self, event):
                seen.append(event.type())

        self.config = FakeConfig()
        self.player, self.library = QWidget(), QWidget()
        view = View(self.player)
        stack = PageStack(self.player, self.library, self.config)
        stack.resize(1280, 720)
        stack.show()
        self.addCleanup(stack.close)
        view.setGeometry(self.player.rect())
        point = view.mapToGlobal(view.rect().center())
        send(view, QEvent.Type.MouseButtonPress, point)
        self.assertEqual(seen, [QEvent.Type.MouseButtonPress])  # on the player page: its own
        stack.show_library(corner=True)
        view.setGeometry(self.player.rect())
        send(view, QEvent.Type.MouseButtonPress, view.mapToGlobal(view.rect().center()))
        self.assertEqual(len(seen), 1)  # in the corner: the stack took it

    def test_views_added_to_the_player_later_are_the_stacks_in_the_corner_too(self):
        # Sweep B2: only the views there when the stack was made were the stack's.
        seen = []

        class View(QWidget):
            def mousePressEvent(self, event):
                seen.append(self)

        stack = self.make_stack()
        view = View(self.player)  # after the stack
        inner = View(view)  # and inside that one
        stack.show_library(corner=True)
        for widget in (view, inner):
            widget.setGeometry(self.player.rect())
            send(widget, QEvent.Type.MouseButtonPress, widget.mapToGlobal(QPoint(5, 5)))
        self.assertEqual(seen, [])
        stack.show_player()
        send(inner, QEvent.Type.MouseButtonPress, inner.mapToGlobal(QPoint(5, 5)))
        self.assertEqual(seen, [inner])  # on the player page: its own

    def test_the_corners_are_the_four_names(self):
        stack = self.make_stack()
        for name in CORNERS:
            stack.set_corner(name)
            self.assertEqual(stack.corner(), name)
        with self.assertRaises(ValueError):
            stack.set_corner("middle")

    def test_corner_rects_stay_inside_the_stack(self):
        stack = self.make_stack()
        for name in CORNERS:
            stack.set_corner(name)
            self.assertTrue(QRect(stack.rect()).contains(stack.corner_rect()), name)


if __name__ == "__main__":
    unittest.main()
