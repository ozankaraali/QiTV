"""Desktop picture in picture (ui/desktop_pip.py): the large picture's surface floats while the
fullscreen window's desktop is not shown, and comes back."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from tests.test_tv_window import WindowCase  # noqa: E402
from ui import desktop_pip  # noqa: E402
from ui.desktop_pip import DesktopPip, FloatingPlayer, corner_rect  # noqa: E402
from ui.pip import PipFrame  # noqa: E402


class Config:
    pip_corner = "bottom-right"


def mouse(widget, kind, at, buttons=Qt.MouseButton.LeftButton):
    point = QPointF(at)
    event = QMouseEvent(
        kind,
        point,
        QPointF(widget.mapToGlobal(at)),
        Qt.MouseButton.LeftButton,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


class DesktopPipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.resize(1280, 720)
        self.page = QWidget(self.window)
        self.page.resize(1280, 720)
        self.main, self.small = QWidget(self.page), QWidget(self.page)
        self.frame = PipFrame(self.page, self.main, self.small, Config())
        self.shown: bool | None = True  # the window's desktop shows
        self.allowed = True
        self.joined: list[QWidget] = []
        self.changes: list[bool] = []
        self.pip = DesktopPip(
            self.window,
            on_active_space=lambda _window: self.shown,
            may_float=lambda: self.allowed,
            lend=self.frame.lend_large,
            take_back=self.frame.take_back,
            place=lambda: QRect(900, 500, 320, 180),
            join_all_spaces=self.joined.append,
        )
        self.pip.floatingChanged.connect(self.changes.append)
        self.addCleanup(self.pip.close)
        self.addCleanup(self.window.deleteLater)

    def away(self):
        self.shown = False
        for _ in range(desktop_pip.AWAY_CHECKS):  # (one answer may be the fullscreen animation's)
            self.pip.check()

    def back(self):
        self.shown = True
        self.pip.check()

    def test_another_desktop_floats_the_large_picture_and_its_own_puts_it_back(self):
        self.away()
        floating = self.pip._floating
        self.assertTrue(self.pip.floating())
        self.assertIs(self.main.parentWidget(), floating)
        self.assertEqual(floating.geometry(), QRect(900, 500, 320, 180))
        self.assertEqual(self.main.geometry(), floating.rect())  # it fills the small window
        self.assertEqual(set(self.joined), {floating})  # on every desktop (before and after show)
        self.page.resize(1000, 600)  # the player lays out again: the lent one is left alone
        self.assertIs(self.main.parentWidget(), floating)
        self.assertEqual(self.main.geometry(), floating.rect())
        self.back()
        self.assertFalse(self.pip.floating())
        self.assertIs(self.main.parentWidget(), self.page)
        self.assertEqual(self.main.geometry(), self.page.rect())
        self.assertTrue(floating.isHidden())
        self.assertEqual(self.changes, [True, False])

    def test_nothing_floats_when_it_may_not_or_the_desktop_cannot_be_told(self):
        self.allowed = False
        self.away()
        self.assertFalse(self.pip.floating())
        self.allowed, self.shown = True, None
        self.pip.check()
        self.assertFalse(self.pip.floating())
        self.assertIs(self.main.parentWidget(), self.page)

    def test_the_multiview_lends_nothing(self):
        self.frame.suspend()  # the grid lays the surfaces out
        self.away()
        self.assertFalse(self.pip.floating())

    def test_after_a_swap_the_large_one_floats(self):
        self.frame.show_pip()
        self.frame.set_large("pip")  # the small surface is the large one now
        self.away()
        self.assertIs(self.small.parentWidget(), self.pip._floating)
        self.assertIs(self.main.parentWidget(), self.page)  # the small picture stays
        self.back()
        self.assertIs(self.small.parentWidget(), self.page)
        self.assertEqual(self.small.geometry(), self.page.rect())

    def test_watching_stops_and_puts_the_picture_back(self):
        self.pip.watch(True)
        self.assertTrue(self.pip._timer.isActive())
        self.away()
        self.pip.watch(False)  # fullscreen left
        self.assertFalse(self.pip._timer.isActive())
        self.assertIs(self.main.parentWidget(), self.page)

    def test_a_click_does_nothing_and_a_double_click_brings_the_fullscreen_desktop(self):
        self.away()
        floating = self.pip._floating
        with (
            mock.patch.object(self.window, "raise_") as raised,
            mock.patch.object(self.window, "activateWindow") as activated,
        ):
            # A press to grab it, released where it was: never back to fullscreen.
            mouse(self.main, QEvent.Type.MouseButtonPress, QPoint(160, 90))
            mouse(
                self.main, QEvent.Type.MouseButtonRelease, QPoint(160, 90), Qt.MouseButton.NoButton
            )
            activated.assert_not_called()
            self.assertTrue(self.pip.floating())
            mouse(self.main, QEvent.Type.MouseButtonDblClick, QPoint(160, 90))
        raised.assert_called_once()
        activated.assert_called_once()
        self.assertFalse(self.pip.floating())  # back at once, while macOS brings the desktop
        self.assertIs(self.main.parentWidget(), self.page)
        self.assertTrue(floating.isHidden())

    def test_a_drag_moves_the_floating_picture_and_is_no_click(self):
        self.away()
        floating = self.pip._floating
        start = floating.pos()
        with mock.patch.object(self.window, "activateWindow") as activated:
            mouse(self.main, QEvent.Type.MouseButtonPress, QPoint(10, 10))
            mouse(self.main, QEvent.Type.MouseMove, QPoint(60, 40))
            mouse(
                self.main, QEvent.Type.MouseButtonRelease, QPoint(60, 40), Qt.MouseButton.NoButton
            )
        self.assertEqual(floating.pos(), start + QPoint(50, 30))
        activated.assert_not_called()

    def test_one_away_answer_floats_nothing(self):
        self.shown = False
        self.pip.check()  # the fullscreen animation may answer so once
        self.assertFalse(self.pip.floating())
        self.shown = True
        self.pip.check()
        self.shown = False
        self.pip.check()
        self.assertFalse(self.pip.floating())  # the count starts again

    def test_a_picture_that_may_float_no_more_comes_back_on_the_next_check(self):
        self.away()
        self.allowed = False  # it stopped, or the Library opened
        self.pip.check()
        self.assertFalse(self.pip.floating())
        self.assertIs(self.main.parentWidget(), self.page)

    def test_the_multiview_takes_the_surfaces_only_once_the_picture_is_back(self):
        self.away()
        self.frame.suspending.connect(self.pip.dock)  # (TVWindow connects them)
        self.frame.suspend()
        self.assertFalse(self.pip.floating())
        self.assertIs(self.main.parentWidget(), self.page)
        self.assertFalse(self.main.isHidden())

    def test_the_wheel_over_the_floating_picture_does_nothing(self):
        from PySide6.QtGui import QWheelEvent

        self.away()
        steps = []
        self.main.wheelEvent = lambda event: steps.append(event)  # the shell's (a zap)
        event = QWheelEvent(
            QPointF(10, 10),
            QPointF(self.main.mapToGlobal(QPoint(10, 10))),
            QPoint(),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(self.main, event)
        self.assertEqual(steps, [])

    def test_a_swap_while_it_floats_leaves_it_where_it_is(self):
        self.frame.show_pip()
        self.away()
        floating = self.pip._floating
        self.frame.set_large("pip")  # the small one is large now; the lent one stays out
        self.assertEqual(self.main.geometry(), floating.rect())
        self.back()
        self.assertIs(self.main.parentWidget(), self.page)

    def drag(self, start, end):
        mouse(self.main, QEvent.Type.MouseButtonPress, start)
        mouse(self.main, QEvent.Type.MouseMove, end)
        mouse(self.main, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.NoButton)

    def test_a_corner_drag_resizes_it_in_16_by_9_and_the_other_corner_stays(self):
        places = []
        self.pip._remember = None
        self.away()
        floating = self.pip._floating
        floating.placed.connect(places.append)
        start = floating.geometry()  # 320×180 at (900, 500)
        self.drag(QPoint(318, 178), QPoint(318 + 160, 178 + 10))  # the bottom-right corner
        geometry = floating.geometry()
        self.assertEqual((geometry.width(), geometry.height()), (480, 270))  # the larger change
        self.assertEqual(geometry.topLeft(), start.topLeft())
        self.assertEqual(places, [geometry])
        self.assertEqual(self.main.geometry(), floating.rect())  # the picture fills it

    def test_the_top_left_corner_keeps_the_bottom_right_one(self):
        self.away()
        floating = self.pip._floating
        start = floating.geometry()
        self.drag(QPoint(2, 2), QPoint(2 - 160, 2 - 90))
        geometry = floating.geometry()
        self.assertEqual((geometry.width(), geometry.height()), (480, 270))
        self.assertEqual(geometry.bottomRight(), start.bottomRight())

    def test_an_edge_resizes_too_never_under_the_smallest(self):
        self.away()
        floating = self.pip._floating
        self.drag(QPoint(318, 90), QPoint(0, 90))  # the right edge, far to the left
        geometry = floating.geometry()
        self.assertEqual((geometry.width(), geometry.height()), (desktop_pip.MIN_WIDTH, 135))
        self.drag(QPoint(120, geometry.height() - 2), QPoint(120, geometry.height() + 133))
        self.assertEqual(floating.height(), 270)  # the bottom edge: the width follows
        self.assertEqual(floating.width(), 480)

    def test_the_pointer_says_where_it_resizes(self):
        self.away()
        cursor = Qt.CursorShape
        for at, shape in (
            (QPoint(2, 2), cursor.SizeFDiagCursor),
            (QPoint(318, 2), cursor.SizeBDiagCursor),
            (QPoint(2, 90), cursor.SizeHorCursor),
            (QPoint(160, 178), cursor.SizeVerCursor),
        ):
            mouse(self.main, QEvent.Type.MouseMove, at, Qt.MouseButton.NoButton)
            self.assertEqual(self.main.cursor().shape(), shape, at)
        mouse(self.main, QEvent.Type.MouseMove, QPoint(160, 90), Qt.MouseButton.NoButton)
        self.assertEqual(self.main.cursor().shape(), cursor.ArrowCursor)

    def test_a_move_or_a_resize_is_remembered(self):
        remembered = []
        self.pip._remember = remembered.append
        self.pip.close()  # a new floating window connects to it
        self.away()
        self.drag(QPoint(160, 90), QPoint(200, 120))
        self.assertEqual(remembered, [self.pip._floating.geometry()])

    def test_the_second_away_answer_is_asked_soon_not_a_poll_later(self):
        from timer_fixtures import wait_until

        self.pip.watch(True)
        self.shown = False
        self.pip.check()  # the first answer
        self.assertFalse(self.pip.floating())
        self.assertTrue(wait_until(self.pip.floating, desktop_pip.CONFIRM_MS + 150))

    def test_a_focus_change_asks_at_once_while_watching(self):
        self.shown = False
        self.pip.nudge()  # not watching: nothing is asked
        self.assertEqual(self.pip._away, 0)
        self.pip.watch(True)
        self.pip.nudge()
        self.assertEqual(self.pip._away, 1)

    def test_closing_puts_the_picture_back_and_drops_the_window(self):
        self.away()
        self.pip.close()
        self.assertIs(self.main.parentWidget(), self.page)
        self.assertIsNone(self.pip._floating)

    def test_the_floating_window_never_takes_the_keyboard(self):
        flags = FloatingPlayer().windowFlags()
        for flag in (
            Qt.WindowType.Tool,  # a panel: on macOS a press leaves QiTV inactive
            Qt.WindowType.FramelessWindowHint,
            Qt.WindowType.WindowStaysOnTopHint,
            Qt.WindowType.WindowDoesNotAcceptFocus,
        ):
            self.assertTrue(flags & flag, flag)

    def test_the_corner_is_a_quarter_of_the_screen_wide_in_16_by_9(self):
        rect = corner_rect(QRect(0, 25, 1920, 1055), 1920)
        self.assertEqual((rect.width(), rect.height()), (480, 270))
        self.assertEqual(rect.right() + 1 + desktop_pip.MARGIN, 1920)
        self.assertEqual(rect.bottom() + 1 + desktop_pip.MARGIN, 25 + 1055)


class DesktopPipWindowTests(WindowCase):
    def test_the_floating_picture_opens_where_it_was_left_while_that_is_on_a_screen(self):
        window = self.window
        screen = QApplication.primaryScreen().availableGeometry()
        inside = QRect(screen.x() + 40, screen.y() + 40, 400, 225)
        window._remember_floating_rect(inside)
        self.assertEqual(window._floating_rect(), inside)
        window.config_manager.desktop_pip_rect = [-90000, -90000, 400, 225]  # a screen gone
        rect = window._floating_rect()
        self.assertTrue(screen.contains(rect.center()))

    """The TV window floats its large picture and shows a note in its place meanwhile."""

    def test_fullscreen_watches_the_desktop_and_the_picture_floats_and_returns(self):
        shown = {"value": True}
        with mock.patch("ui.tv_window._macos_desktops", return_value=True):
            window = self.window
            pip = window.desktop_pip
            pip._on_active_space = lambda _w: shown["value"]
            pip._may_float = lambda: True  # (no libVLC here: nothing plays)
            window.show()
            window.showFullScreen()
            self.app.processEvents()
            self.assertTrue(pip._timer.isActive())
            shown["value"] = False
            pip.check()
            pip.check()
            self.assertIsNot(window.surface.parentWidget(), window.player_page)
            self.assertTrue(window.floating_note.isVisible())
            shown["value"] = True
            pip.check()
            self.assertIs(window.surface.parentWidget(), window.player_page)
            self.assertFalse(window.floating_note.isVisible())
            shown["value"] = False
            pip.check()
            pip.check()
            window.showNormal()  # leaving fullscreen puts it back
            self.app.processEvents()
            self.assertIs(window.surface.parentWidget(), window.player_page)
            self.assertFalse(pip._timer.isActive())

    def test_without_a_playing_picture_or_with_the_setting_off_nothing_floats(self):
        window = self.window
        window.show()
        window.showFullScreen()
        self.app.processEvents()
        self.assertFalse(window._may_float_picture())  # nothing plays (no libVLC)
        window.config_manager.desktop_pip = False
        self.assertFalse(window._may_float_picture())


if __name__ == "__main__":
    unittest.main()
