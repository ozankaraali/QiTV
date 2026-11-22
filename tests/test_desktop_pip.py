"""Desktop picture in picture (ui/desktop_pip.py): the large picture's surface floats while the
fullscreen window's desktop is not shown, and comes back."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402
from timer_fixtures import elapse, wait_until  # noqa: E402

from services import picture  # noqa: E402
from services.catalog import ALL_GROUP_ID  # noqa: E402
from services.playback import NullSession  # noqa: E402
from tests.test_tv_window import WindowCase  # noqa: E402
from ui import desktop_pip  # noqa: E402
from ui.desktop_pip import DesktopPip, FloatingPlayer, corner_rect  # noqa: E402
from ui.keymap import Action  # noqa: E402
from ui.pip import PipFrame  # noqa: E402


class Config:
    pip_corner = "bottom-right"


class _BoxedPlayer(NullSession):
    """A player that keeps what the window tells it of the box it shows in."""

    def __init__(self, ratio=None):
        super().__init__()
        self.boxes: list = []
        self.ratio = ratio  # the shape its picture shows in (None: not known)

    def set_box_size(self, size):
        self.boxes.append(size)

    def box(self):
        return self.boxes[-1] if self.boxes else None

    def picture_ratio(self):
        return self.ratio


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


class PageLender:
    """What the TV window lends the floating window: the player page (PageStack.lend_player),
    whose large picture holds its picture small meanwhile (PipFrame.set_floating)."""

    def __init__(self, window: QWidget, page: QWidget, frame: PipFrame) -> None:
        self.window, self.page, self.frame = window, page, frame

    def lend(self):
        return self.page if self.frame.set_floating(True) else None

    def take_back(self, page):
        page.setParent(self.window)
        page.setGeometry(self.window.rect())
        page.show()
        self.frame.set_floating(False)


class PipCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.resize(1280, 720)
        self.page = QWidget(self.window)
        self.page.resize(1280, 720)
        self.main, self.small = Picture(self.page), Picture(self.page)
        self.frame = PipFrame(self.page, self.main, self.small, Config())
        self.lender = PageLender(self.window, self.page, self.frame)
        self.shown: bool | None = True  # the window's desktop shows
        self.allowed = True
        self.joined: list[QWidget] = []
        self.changes: list[bool] = []
        self.keys = KeyTarget()
        self.pip = DesktopPip(
            self.window,
            on_active_space=lambda _window: self.shown,
            may_float=lambda: self.allowed,
            lend=self.lender.lend,
            take_back=self.lender.take_back,
            place=lambda: QRect(900, 500, 320, 180),
            join_all_spaces=self.joined.append,
            keys=self.keys,
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

    def drag(self, start, end):
        mouse(self.main, QEvent.Type.MouseButtonPress, start)
        mouse(self.main, QEvent.Type.MouseMove, end)
        mouse(self.main, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.NoButton)


class KeyTarget(QWidget):
    """The TV window's part: the keys it is handed."""

    def __init__(self):
        super().__init__()
        self.keys: list[int] = []

    def keyPressEvent(self, event):
        self.keys.append(event.key())
        event.accept()


class Picture(QWidget):
    """A surface's part: the mouse that reaches it."""

    def __init__(self, parent):
        super().__init__(parent)
        self.events: list[QEvent.Type] = []

    def event(self, event):
        if isinstance(event, QMouseEvent):
            self.events.append(event.type())
        return super().event(event)


class DesktopPipTests(PipCase):
    def test_another_desktop_floats_the_player_page_and_its_own_puts_it_back(self):
        self.away()
        floating = self.pip._floating
        self.assertTrue(self.pip.floating())
        self.assertIs(self.page.parentWidget(), floating)  # the page: its PiP and all
        self.assertIs(self.main.parentWidget(), self.page)
        self.assertEqual(floating.geometry(), QRect(900, 500, 320, 180))
        self.assertEqual(self.page.geometry(), floating.rect())  # it fills the small window
        self.assertEqual(self.main.geometry(), self.page.rect())
        self.assertTrue(self.frame.shows_small("main"))  # filled to the last pixel there
        self.assertEqual(set(self.joined), {floating})  # on every desktop (before and after show)
        self.back()
        self.assertFalse(self.pip.floating())
        self.assertIs(self.page.parentWidget(), self.window)
        self.assertEqual(self.main.geometry(), self.page.rect())
        self.assertFalse(self.frame.shows_small("main"))
        self.assertTrue(floating.isHidden())
        self.assertEqual(self.changes, [True, False])

    def test_nothing_floats_when_it_may_not_or_the_desktop_cannot_be_told(self):
        self.allowed = False
        self.away()
        self.assertFalse(self.pip.floating())
        self.allowed, self.shown = True, None
        self.pip.check()
        self.assertFalse(self.pip.floating())
        self.assertIs(self.page.parentWidget(), self.window)

    def test_the_multiview_lends_nothing(self):
        self.frame.suspend()  # the grid lays the surfaces out
        self.away()
        self.assertFalse(self.pip.floating())

    def test_after_a_swap_the_large_one_holds_its_picture_small(self):
        self.frame.show_pip()
        self.frame.set_large("pip")  # the small surface is the large one now
        self.away()
        self.assertIs(self.page.parentWidget(), self.pip._floating)
        self.assertEqual(self.small.geometry(), self.page.rect())  # the large one fills it
        self.assertTrue(self.frame.shows_small("pip"))
        self.back()
        self.assertFalse(self.frame.shows_small("pip"))
        self.assertEqual(self.small.geometry(), self.page.rect())

    def test_watching_stops_and_puts_the_picture_back(self):
        self.pip.watch(True)
        self.assertTrue(self.pip._timer.isActive())
        self.away()
        self.pip.watch(False)  # fullscreen left
        self.assertFalse(self.pip._timer.isActive())
        self.assertIs(self.page.parentWidget(), self.window)

    def test_clicks_on_the_picture_are_the_windows_and_coming_back_brings_its_desktop(self):
        # 2.13.5: a press, a click, a drag and a double-click on the picture reach it, as in the
        # window (the TV window's: the channel list, a drag that moves the small window, back);
        # never a way back by themselves.
        main = self.main
        self.away()
        floating = self.pip._floating
        mouse(main, QEvent.Type.MouseButtonPress, QPoint(160, 90))
        mouse(main, QEvent.Type.MouseMove, QPoint(200, 120))
        mouse(main, QEvent.Type.MouseButtonRelease, QPoint(200, 120), Qt.MouseButton.NoButton)
        mouse(main, QEvent.Type.MouseButtonDblClick, QPoint(160, 90))
        self.assertEqual(
            main.events,
            [
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseMove,
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick,
            ],
        )
        self.assertTrue(self.pip.floating())
        self.assertEqual(floating.geometry(), QRect(900, 500, 320, 180))  # (not its move)
        with (
            mock.patch.object(self.window, "raise_") as raised,
            mock.patch.object(self.window, "activateWindow") as activated,
        ):
            self.pip.come_back()  # its double-click, as the window has it
        raised.assert_called_once()
        activated.assert_called_once()
        self.assertFalse(self.pip.floating())  # back at once, while macOS brings the desktop
        self.assertIs(self.page.parentWidget(), self.window)
        self.assertTrue(floating.isHidden())

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
        self.assertIs(self.page.parentWidget(), self.window)

    def test_the_multiview_takes_the_surfaces_only_once_the_picture_is_back(self):
        self.away()
        self.frame.suspending.connect(self.pip.dock)  # (TVWindow connects them)
        self.frame.suspend()
        self.assertFalse(self.pip.floating())
        self.assertIs(self.page.parentWidget(), self.window)
        self.assertFalse(self.main.isHidden())
        self.assertFalse(self.frame.floating())

    def test_the_wheel_over_the_floating_picture_does_nothing(self):
        from PySide6.QtGui import QWheelEvent

        self.away()
        steps = []
        self.main.wheelEvent = lambda event: steps.append(event)  # the window's (a zap)
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

    def test_a_swap_while_it_floats_happens_in_the_small_window(self):
        self.frame.show_pip()
        self.away()
        floating = self.pip._floating
        self.frame.set_large("pip")  # the small one is large now, in the small window
        self.assertEqual(self.small.geometry(), floating.rect())
        self.assertTrue(self.frame.shows_small("pip"))
        self.assertTrue(self.frame.shows_small("main"))  # the picture in picture's, in its corner
        self.back()
        self.assertIs(self.page.parentWidget(), self.window)
        self.assertEqual(self.small.geometry(), self.page.rect())

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
        self.assertEqual(self.main.events, [])  # the resize's, not the picture's

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

    def test_a_resize_is_remembered_and_a_click_on_an_edge_does_nothing(self):
        remembered = []
        self.pip._remember = remembered.append
        self.pip.close()  # a new floating window connects to it
        self.away()
        self.drag(QPoint(318, 90), QPoint(400, 90))
        self.assertEqual(remembered, [self.pip._floating.geometry()])
        self.drag(QPoint(2, 90), QPoint(2, 90))  # a click on the left edge: no resize, no click
        self.assertEqual(len(remembered), 1)
        self.assertEqual(self.main.events, [])

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
        self.assertIs(self.page.parentWidget(), self.window)
        self.assertIsNone(self.pip._floating)

    def test_the_floating_window_shows_without_the_keyboard_and_its_keys_are_the_windows(self):
        player = FloatingPlayer()
        flags = player.windowFlags()
        for flag in (
            Qt.WindowType.Tool,  # a panel: on macOS a press leaves QiTV inactive
            Qt.WindowType.FramelessWindowHint,
            Qt.WindowType.WindowStaysOnTopHint,
        ):
            self.assertTrue(flags & flag, flag)
        # Shown, it takes nothing from the app in front; pressed, it takes the keys (2.13.5:
        # the same keys as the window's, however it floated).
        self.assertTrue(player.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertFalse(flags & Qt.WindowType.WindowDoesNotAcceptFocus)
        player.deleteLater()
        self.away()
        QTest.keyClick(self.pip._floating, Qt.Key.Key_Up)
        self.assertEqual(self.keys.keys, [Qt.Key.Key_Up])

    def test_the_corner_is_a_quarter_of_the_screen_wide_in_16_by_9(self):
        rect = corner_rect(QRect(0, 25, 1920, 1055), 1920)
        self.assertEqual((rect.width(), rect.height()), (480, 270))
        self.assertEqual(rect.right() + 1 + desktop_pip.MARGIN, 1920)
        self.assertEqual(rect.bottom() + 1 + desktop_pip.MARGIN, 25 + 1055)


class FloatingShapeTests(PipCase):
    """The floating window has its picture's shape (no bars), and keeps it as it resizes."""

    def setUp(self):
        super().setUp()
        self.screen = QApplication.primaryScreen().availableGeometry()
        self.ratio = 4 / 3
        self.pip._shape = lambda: self.ratio
        self.place = QRect(self.screen.x() + 40, self.screen.y() + 40, 320, 180)  # top left
        self.pip._place = lambda: self.place

    def test_it_floats_in_its_pictures_shape_as_large_as_it_was(self):
        self.away()
        geometry = self.pip._floating.geometry()
        self.assertEqual(geometry, QRect(self.place.topLeft(), QSize(277, 208)))
        self.assertEqual(self.page.geometry(), self.pip._floating.rect())  # the picture fills it
        self.assertEqual(self.main.geometry(), self.page.rect())

    def test_in_the_bottom_right_corner_that_corner_stays(self):
        width, height = 320, 180
        self.place = QRect(
            self.screen.right() + 1 - desktop_pip.MARGIN - width,
            self.screen.bottom() + 1 - desktop_pip.MARGIN - height,
            width,
            height,
        )
        self.away()
        geometry = self.pip._floating.geometry()
        self.assertEqual(geometry.size(), QSize(277, 208))
        self.assertEqual(geometry.bottomRight(), self.place.bottomRight())

    def test_a_new_shape_while_it_floats_reshapes_it_at_the_next_check(self):
        self.ratio = None  # not known yet: it keeps the one it has
        self.away()
        floating = self.pip._floating
        self.assertEqual(floating.geometry(), self.place)
        self.ratio = 4 / 3  # its first picture came
        self.pip.check()
        self.assertEqual(floating.geometry().size(), QSize(277, 208))
        self.ratio = 16 / 9  # a zap to a 16:9 channel
        self.pip.check()
        self.assertEqual(floating.geometry().size(), QSize(320, 180))
        self.assertTrue(self.pip.floating())

    def test_a_corner_drag_resizes_it_in_its_pictures_shape(self):
        self.away()
        floating = self.pip._floating
        start = floating.geometry()  # 277×208
        self.drag(QPoint(275, 206), QPoint(275 + 100, 206 + 10))  # the bottom-right corner
        geometry = floating.geometry()
        self.assertEqual((geometry.width(), geometry.height()), (377, 283))
        self.assertEqual(geometry.topLeft(), start.topLeft())
        self.drag(QPoint(375, 140), QPoint(0, 140))  # the right edge, far to the left
        self.assertEqual(floating.size(), QSize(*picture.box_size(desktop_pip.MIN_WIDTH, 4 / 3)))

    def test_a_shape_that_comes_during_a_press_waits_for_its_release(self):
        self.away()
        floating = self.pip._floating
        mouse(self.main, QEvent.Type.MouseButtonPress, QPoint(275, 100))  # its right edge
        self.ratio = 16 / 9
        self.pip.check()
        self.assertEqual(floating.size(), QSize(277, 208))  # not under the pointer
        mouse(self.main, QEvent.Type.MouseButtonRelease, QPoint(275, 100), Qt.MouseButton.NoButton)
        self.assertEqual(floating.size(), QSize(320, 180))

    def test_it_stays_on_its_screen(self):
        self.place = QRect(self.screen.x(), self.screen.y(), 320, 180)
        self.ratio = 9 / 16  # an upright picture, as tall as the 16:9 one is wide
        self.away()
        geometry = self.pip._floating.geometry()
        self.assertTrue(self.screen.contains(geometry), geometry)
        self.assertAlmostEqual(geometry.width() / geometry.height(), 9 / 16, delta=0.01)

    def test_a_resize_it_did_not_make_ends_in_its_pictures_shape(self):
        # 2.13.5, the owner: "when resizing still the black lines appear". Qt makes a frameless
        # window resizable by macOS itself: its edges resize it without a press QiTV sees.
        self.away()
        floating = self.pip._floating
        floating.setGeometry(QRect(self.place.topLeft(), QSize(546, 330)))
        self.assertTrue(
            wait_until(lambda: picture.same_shape(floating.width() / floating.height(), 4 / 3))
        )
        floating.setGeometry(QRect(self.place.topLeft(), QSize(120, 90)))  # under the smallest
        smallest = QSize(*picture.box_size(desktop_pip.MIN_WIDTH, 4 / 3))
        self.assertTrue(wait_until(lambda: floating.size() == smallest))

    def test_macos_keeps_its_shape_while_it_resizes(self):
        # Its own resize, on macOS: the window's content aspect ratio, its picture's shape (or
        # the one it has while that is not known), told again as the shape changes.
        shapes = []
        self.pip._lock_shape = lambda widget, shape: shapes.append((widget, shape))
        self.away()
        floating = self.pip._floating
        self.assertTrue(shapes)
        self.assertEqual(shapes[-1], (floating, 4 / 3))
        self.ratio = 16 / 9  # a zap to a 16:9 channel
        self.pip.check()
        self.assertEqual(shapes[-1], (floating, 16 / 9))
        self.ratio = None  # Fill, or not known: it keeps the one it has
        self.pip.check()
        self.assertAlmostEqual(shapes[-1][1], floating.width() / floating.height(), delta=0.01)


class OnePictureInPictureTests(WindowCase):
    """2.13.5, the owner: "fullscreen switch pip and regular pip are not the same, one has hud
    the other does not have". On macOS the mini player (Alt+P, the controls' button) and the
    picture a desktop change floats are one window: the player page in it, under the window's
    own controls, with the same hover and keys however it was entered."""

    def setUp(self):
        super().setUp()
        macos = mock.patch("ui.tv_window._macos_desktops", return_value=True)
        macos.start()
        self.addCleanup(macos.stop)
        self.shown = True  # the fullscreen window's desktop shows
        pip = self.window.desktop_pip
        pip._on_active_space = lambda _window: self.shown
        pip._may_float = lambda: True  # (no libVLC here: nothing plays)

    def float_from_fullscreen(self):
        window = self.window
        window.showFullScreen()
        self.app.processEvents()
        self.shown = False
        for _ in range(desktop_pip.AWAY_CHECKS):
            window.desktop_pip.check()
        self.app.processEvents()

    def assert_the_small_window(self, floating):
        """The player page in `floating` under the window's own controls: a move over the
        picture reaches them, its keys are the window's, its button goes back."""
        window = self.window
        self.assertTrue(floating.isVisible())
        self.assertTrue(floating.isAncestorOf(window.surface))
        self.assertTrue(floating.isAncestorOf(window.pip_surface))  # the page, its PiP too
        drawer = window.overlays.drawer
        if not drawer.isVisible():
            window.controller.handle(Action.OPEN_DRAWER)
        self.assertTrue(wait_until(drawer.isVisible))
        self.assertTrue(window.host.isVisible())
        self.assertEqual(window.host.geometry(), floating.geometry())  # the controls over it
        self.assertEqual(window.overlays.transport.icon("mini"), "full_window")
        moves = []
        window.surface.mouseMoved.connect(lambda: moves.append(True))
        mouse(window.surface, QEvent.Type.MouseMove, QPoint(120, 60), Qt.MouseButton.NoButton)
        self.assertEqual(moves, [True])  # the hover: the controls come
        with mock.patch.object(window.controller, "handle") as handle:
            QTest.keyClick(floating, Qt.Key.Key_Up)
        handle.assert_called()

    def test_both_ways_one_window_holds_the_player_under_the_windows_own_controls(self):
        window = self.window
        pip = window.desktop_pip
        self.show()
        self.float_from_fullscreen()  # a desktop change
        floating = pip.floating_window()
        self.assertIsNotNone(floating)
        self.assert_the_small_window(floating)
        self.shown = True
        pip.check()  # back on the fullscreen desktop
        self.app.processEvents()
        self.assertIs(window.player_page.parentWidget(), window.pages)
        self.assertFalse(floating.isVisible())
        window.showNormal()
        self.app.processEvents()
        self.mini()  # Alt+P
        self.app.processEvents()
        self.assertIs(pip.floating_window(), floating)  # the same window
        self.assertTrue(window.mini_player_active())
        self.assertFalse(window.isVisible())  # the window gives way to it, as the mini player
        self.assert_the_small_window(floating)
        QTest.keyClick(floating, Qt.Key.Key_P, Qt.KeyboardModifier.AltModifier)  # back
        self.app.processEvents()
        self.assertFalse(window.mini_player_active())
        self.assertTrue(window.isVisible())
        self.assertFalse(floating.isVisible())
        self.assertIs(window.player_page.parentWidget(), window.pages)
        self.assertEqual(window.overlays.transport.icon("mini"), "mini_player")

    def test_a_drag_on_the_picture_moves_it_and_a_double_click_brings_the_picture_back(self):
        window = self.window
        pip = window.desktop_pip
        self.show()
        self.float_from_fullscreen()
        floating = pip.floating_window()
        start = floating.pos()
        with mock.patch.object(window.controller, "video_clicked") as clicked:
            mouse(window.surface, QEvent.Type.MouseButtonPress, QPoint(160, 90))
            mouse(window.surface, QEvent.Type.MouseMove, QPoint(220, 130))
            mouse(
                window.surface,
                QEvent.Type.MouseButtonRelease,
                QPoint(220, 130),
                Qt.MouseButton.NoButton,
            )
            elapse(QApplication.doubleClickInterval() + 50)
        clicked.assert_not_called()  # a drag, not a click (the channel list stays)
        self.assertEqual(floating.pos(), start + QPoint(60, 40))
        geometry = floating.geometry()
        self.assertEqual(  # it opens there next time
            window.config_manager.desktop_pip_rect,
            [geometry.x(), geometry.y(), geometry.width(), geometry.height()],
        )
        mouse(window.surface, QEvent.Type.MouseButtonDblClick, QPoint(160, 90))
        self.assertFalse(pip.floating())  # back on the fullscreen desktop
        self.assertIs(window.player_page.parentWidget(), window.pages)
        self.assertTrue(window.isFullScreen())

    def test_alt_p_in_fullscreen_floats_once_fullscreen_is_left(self):
        window = self.window
        pip = window.desktop_pip
        self.show()
        window.showFullScreen()
        self.app.processEvents()
        self.mini()
        self.assertIsNone(pip.reason())  # nothing floats for the desktop on the way out
        self.assertTrue(wait_until(window.mini_player_active))
        self.assertFalse(window.isFullScreen())
        self.assertTrue(pip.floating())
        self.assertEqual(pip.reason(), desktop_pip.MINI)
        window.toggle_mini_player()
        self.assertFalse(pip.floating())
        self.assertTrue(window.isVisible())

    def test_quitting_from_the_mini_player_closes_the_window_its_own_way(self):
        # The system's Quit closes only the windows shown, and Qt quits after the last window
        # closes only for one that was shown: the window comes back for its last steps.
        window = self.window
        self.show()
        self.mini()
        self.assertFalse(window.isVisible())
        self.app.closeAllWindows()  # the system's Quit (the Dock, logging out)
        self.assertTrue(window.closing())
        self.assertTrue(window.isVisible())
        self.assertIs(window.player_page.parentWidget(), window.pages)
        self.assertTrue(
            wait_until(lambda: not window.isVisible() and not window.close_retry.isActive())
        )

    def test_quit_in_the_mini_player_shows_the_window_for_its_last_steps(self):
        window = self.window
        self.show()
        self.mini()
        window.quit_chosen()  # ⌘Q, its keys reaching the window
        self.assertTrue(window.closing())
        self.assertTrue(window.isVisible())
        self.assertFalse(window.desktop_pip.floating())
        self.assertTrue(
            wait_until(lambda: not window.isVisible() and not window.close_retry.isActive())
        )

    def test_a_question_is_asked_in_the_window(self):  # as for the mini player in 2.13.1
        window = self.window
        self.show()
        self.wait_for_drawer()
        self.assertTrue(wait_until(lambda: window.catalog.items("live", ALL_GROUP_ID)))
        self.mini()
        self.assertTrue(window.desktop_pip.floating())
        window.record_at(window.catalog.items("live", ALL_GROUP_ID)[0])  # asked: it makes way
        self.assertFalse(window.mini_player_active())
        self.assertFalse(window.desktop_pip.floating())
        self.assertTrue(window.isVisible())
        self.assertTrue(window.record_card.isVisible())

    def test_the_library_leaves_the_mini_player_first(self):
        window = self.window
        self.show()
        self.wait_for_drawer()
        self.mini()
        self.assertTrue(window.desktop_pip.floating())
        floating = window.desktop_pip.floating_window()
        # The menu's shortcut, typed in the small window (the window itself is hidden).
        QTest.keyClick(floating, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(window.screens.library_shown())
        self.assertFalse(window.mini_player_active())
        self.assertFalse(window.desktop_pip.floating())
        self.assertTrue(window.isVisible())
        self.assertIs(window.player_page.parentWidget(), window.pages)

    def test_a_maximized_window_and_keep_on_top_come_back_as_they_were(self):
        window = self.window
        self.show()
        window.showMaximized()
        self.assertTrue(wait_until(window.isMaximized))
        self.mini()
        self.assertTrue(window.desktop_pip.floating())
        self.mini()
        self.assertTrue(wait_until(window.isMaximized))
        window.showNormal()
        self.app.processEvents()
        self.mini()
        window.menus.keep_on_top.trigger()  # chosen in the mini player: applies once it ends
        self.mini()
        self.assertTrue(window.isVisible())
        self.assertTrue(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertFalse(window.windowFlags() & Qt.WindowType.FramelessWindowHint)

    def test_the_first_click_after_the_mini_player_is_not_swallowed(self):
        window = self.window
        self.show()
        drawer = self.wait_for_drawer()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        self.mini()
        mouse(window.surface, QEvent.Type.MouseButtonPress, QPoint(60, 40))
        mouse(window.surface, QEvent.Type.MouseMove, QPoint(70, 60))
        self.mini()  # back (the press still held)
        drawer.close_drawer()
        window.host.clicked.emit()  # a click on the video around an overlay
        self.assertTrue(drawer.isVisible())

    def test_the_small_window_keeps_its_pictures_shape_and_its_picture_fills_it(self):
        main, small = _BoxedPlayer(16 / 9), _BoxedPlayer()
        players = [("main", main), ("pip", small)]
        with mock.patch.object(self.window, "_players", return_value=players):
            window = self.window
            self.show()
            self.mini()
            self.app.processEvents()
            floating = window.desktop_pip.floating_window()
            self.assertIsNotNone(floating)
            # macOS's own edge resize (Qt makes the frameless window resizable): no press of
            # QiTV's, at a size the picture would show black bars in.
            floating.setGeometry(QRect(floating.pos(), QSize(546, 330)))

            def in_shape():
                return picture.same_shape(floating.width() / floating.height(), 16 / 9)

            self.assertTrue(wait_until(in_shape))
            self.app.processEvents()
            box = main.box()
            self.assertIsNotNone(box)  # told its box: the picture fills it to the last pixel
            self.assertIsNotNone(picture.box_aspect(16 / 9, *box))
            window.toggle_mini_player()
            self.app.processEvents()
            self.assertIsNone(main.box())  # the large view again: the viewer's own shape


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

    """The TV window floats its player page and shows a note in its place meanwhile."""

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
            self.assertIs(window.player_page.parentWidget(), pip.floating_window())
            self.assertIs(window.surface.parentWidget(), window.player_page)
            self.assertTrue(window.floating_note.isVisible())
            self.assertEqual(window.floating_note.geometry(), window.pages.rect())
            shown["value"] = True
            pip.check()
            self.assertIs(window.player_page.parentWidget(), window.pages)
            self.assertEqual(window.player_page.geometry(), window.pages.rect())
            self.assertFalse(window.floating_note.isVisible())
            shown["value"] = False
            pip.check()
            pip.check()
            window.showNormal()  # leaving fullscreen puts it back
            self.app.processEvents()
            self.assertIs(window.player_page.parentWidget(), window.pages)
            self.assertFalse(pip._timer.isActive())

    def test_ai_captions_show_over_the_floating_picture_too(self):
        # 2.9: the owner, "no subs in pip": they showed on the fullscreen desktop left.
        shown = {"value": True}
        with mock.patch("ui.tv_window._macos_desktops", return_value=True):
            window = self.window
            pip = window.desktop_pip
            pip._on_active_space = lambda _w: shown["value"]
            pip._may_float = lambda: True
            window.show()
            window.showFullScreen()
            self.app.processEvents()
            shown["value"] = False
            pip.check()
            pip.check()
            # 2.13.5: the window's own captions, in its controls' host, which follows the
            # player page over the floating window (no second set of them).
            floating, host, overlay = pip.floating_window(), window.host, window.captions_overlay
            window.captions._show("Merhaba")
            self.app.processEvents()
            self.assertEqual(overlay.text(), "Merhaba")
            self.assertTrue(overlay.isVisibleTo(host))
            self.assertTrue(host.isVisible())
            self.assertEqual(host.geometry(), floating.geometry())
            shown["value"] = True
            pip.check()  # back: over the fullscreen window again
            self.app.processEvents()
            page = window.player_page
            self.assertEqual(host.geometry(), QRect(page.mapToGlobal(QPoint(0, 0)), page.size()))
            window.captions._show("")
            self.assertEqual(overlay.text(), "")
            window.showNormal()
            self.app.processEvents()

    def test_a_picture_shown_small_is_told_its_box_in_device_pixels(self):
        # 2.13.1: the floating window and the window's own picture in picture are filled by
        # their picture (no bar); back in the large view the viewer's choice returns.
        shown = {"value": True}
        main, small = _BoxedPlayer(), _BoxedPlayer()
        players = [("main", main), ("pip", small)]
        with (
            mock.patch("ui.tv_window._macos_desktops", return_value=True),
            mock.patch.object(self.window, "_players", return_value=players),
        ):
            window = self.window
            pip = window.desktop_pip
            pip._on_active_space = lambda _w: shown["value"]
            pip._may_float = lambda: True
            pip._place = lambda: QRect(100, 100, 500, 284)  # the owner's floating window
            window.surface.devicePixelRatioF = lambda: 2.0  # a Retina screen
            window.show()
            window.showFullScreen()
            self.app.processEvents()
            self.assertIsNone(main.box())
            shown["value"] = False
            pip.check()
            pip.check()
            self.assertEqual(window.surface.size(), QSize(500, 284))
            self.assertEqual(main.box(), (1000, 568))
            self.assertIsNone(small.box())
            pip.floating_window().resize(640, 360)  # an edge dragged
            self.app.processEvents()
            self.assertEqual(main.box(), (1280, 720))
            shown["value"] = True
            pip.check()  # back in the fullscreen window
            self.app.processEvents()
            self.assertIsNone(main.box())
            window.showNormal()
            self.app.processEvents()
            window.pip_frame.show_pip()  # the small picture in the window
            self.app.processEvents()
            rect = window.pip_frame.small_rect()
            self.assertEqual(small.box(), (rect.width(), rect.height()))
            self.assertIsNone(main.box())
            window.pip_frame.hide_pip()
            self.app.processEvents()
            self.assertIsNone(small.box())

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
