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
        self.assertEqual(self.joined, [floating])  # on every desktop
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

    def test_a_click_on_the_floating_picture_asks_for_the_fullscreen_desktop(self):
        self.away()
        floating = self.pip._floating
        with (
            mock.patch.object(self.window, "raise_") as raised,
            mock.patch.object(self.window, "activateWindow") as activated,
        ):
            mouse(self.main, QEvent.Type.MouseButtonPress, QPoint(10, 10))
            mouse(
                self.main, QEvent.Type.MouseButtonRelease, QPoint(10, 10), Qt.MouseButton.NoButton
            )
        raised.assert_called_once()
        activated.assert_called_once()
        self.assertTrue(self.pip.floating())  # back once the desktop shows (the next check)
        self.back()
        self.assertIs(self.main.parentWidget(), self.page)
        del floating

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

    def test_closing_puts_the_picture_back_and_drops_the_window(self):
        self.away()
        self.pip.close()
        self.assertIs(self.main.parentWidget(), self.page)
        self.assertIsNone(self.pip._floating)

    def test_the_floating_window_never_takes_the_keyboard(self):
        flags = FloatingPlayer().windowFlags()
        for flag in (
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
    """The TV window floats its large picture and shows a note in its place meanwhile."""

    def test_fullscreen_watches_the_desktop_and_the_picture_floats_and_returns(self):
        shown = {"value": True}
        with mock.patch("ui.macos_window.on_active_space", lambda _w: shown["value"]):
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
            self.assertIsNot(window.surface.parentWidget(), window.player_page)
            self.assertTrue(window.floating_note.isVisible())
            shown["value"] = True
            pip.check()
            self.assertIs(window.surface.parentWidget(), window.player_page)
            self.assertFalse(window.floating_note.isVisible())
            shown["value"] = False
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
