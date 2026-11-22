"""Desktop picture in picture (ui/desktop_pip.py): the large picture's surface floats while the
fullscreen window's desktop is not shown, and comes back."""

import contextlib
import logging
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
from services.playback import NullSession  # noqa: E402
from tests.test_tv_window import WindowCase  # noqa: E402
from ui import desktop_pip, float_controls  # noqa: E402
from ui.desktop_pip import DesktopPip, FloatingPlayer, corner_rect  # noqa: E402
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


class PipCase(unittest.TestCase):
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

    def drag(self, start, end):
        mouse(self.main, QEvent.Type.MouseButtonPress, start)
        mouse(self.main, QEvent.Type.MouseMove, end)
        mouse(self.main, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.NoButton)


class DesktopPipTests(PipCase):
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

    def test_its_hover_is_told_and_asked_for_only_while_qitv_is_inactive(self):
        # 2.13.5: its controls come on a hover; Qt says nothing of the pointer while QiTV is
        # not the active app (as nearly always while it floats), so its place is asked then.
        hovers: list = []
        state = {"active": True, "at": QPoint(-5000, -5000)}
        self.pip._hover = hovers.append
        self.pip._app_active = lambda: state["active"]
        self.pip._cursor_pos = lambda: state["at"]
        poll = self.pip.hover_poll
        self.away()
        floating = self.pip._floating
        self.assertFalse(poll.active())  # QiTV active: Qt's own moves arrive
        mouse(self.main, QEvent.Type.MouseMove, QPoint(40, 30), Qt.MouseButton.NoButton)
        self.assertEqual(hovers, [self.main.mapToGlobal(QPoint(40, 30))])
        hovers.clear()
        state["active"] = False
        self.pip.activity_changed()
        self.assertTrue(poll.active())
        state["at"] = floating.geometry().center()
        poll.poll()
        self.assertEqual(hovers, [floating.geometry().center()])
        state["at"] = QPoint(-5000, -5000)
        poll.poll()
        self.assertEqual(hovers[-1], None)  # off it
        self.back()
        self.assertFalse(poll.active())  # back in the window: nothing is asked
        state["at"] = QPoint(0, 0)
        self.away()
        self.assertTrue(poll.active())

    def test_coming_back_floats_nothing_while_macos_switches_desktops(self):
        now = {"value": 100.0}
        self.pip._clock = lambda: now["value"]
        self.pip.watch(True)
        self.away()
        with (
            mock.patch.object(self.window, "raise_") as raised,
            mock.patch.object(self.window, "activateWindow") as activated,
        ):
            self.pip.come_back("a button of its controls")  # as a double-click does
        raised.assert_called_once()
        activated.assert_called_once()
        self.assertFalse(self.pip.floating())
        self.assertIs(self.main.parentWidget(), self.page)
        self.away()  # its desktop not shown yet: macOS still switches
        self.assertFalse(self.pip.floating())
        now["value"] += desktop_pip.BACK_HOLD_S  # it never switched: the picture floats again
        self.away()
        self.assertTrue(self.pip.floating())
        self.pip.come_back()
        self.back()  # the desktop showed: from then on as ever
        self.away()
        self.assertTrue(self.pip.floating())

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

    def test_each_way_back_says_why_in_the_log(self):  # 2.13.5: the next report says
        self.away()
        with self.assertLogs("ui.desktop_pip", "INFO") as logs:
            self.back()
        self.assertIn("back in the window: its desktop shows again", logs.output[-1])
        self.away()
        with self.assertLogs("ui.desktop_pip", "INFO") as logs:
            self.pip.dock("the Library")
        self.assertIn("back in the window: the Library", logs.output[-1])

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
        self.assertEqual(self.main.geometry(), self.pip._floating.rect())  # the picture fills it

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
        mouse(self.main, QEvent.Type.MouseButtonPress, QPoint(100, 100))
        self.ratio = 16 / 9
        self.pip.check()
        self.assertEqual(floating.size(), QSize(277, 208))  # not under the pointer
        mouse(self.main, QEvent.Type.MouseButtonRelease, QPoint(100, 100), Qt.MouseButton.NoButton)
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
        self.assertEqual(self.main.geometry(), floating.rect())  # the picture fills it
        floating.setGeometry(QRect(self.place.topLeft(), QSize(120, 90)))  # under the smallest
        smallest = QSize(*picture.box_size(desktop_pip.MIN_WIDTH, 4 / 3))
        self.assertTrue(wait_until(lambda: floating.size() == smallest))

    def test_macos_keeps_its_shape_between_the_smallest_window_and_the_screen(self):
        # Its own resize, on macOS: the window's content aspect ratio (its picture's shape, or
        # the one it has while that is not known), never under the smallest window nor over
        # the screen, told again as the shape changes.
        shapes = []
        self.pip._lock_shape = lambda widget, shape, smallest, largest: shapes.append(
            (widget, shape, smallest, largest)
        )
        self.away()
        floating = self.pip._floating
        self.assertTrue(shapes)
        self.assertEqual(shapes[-1][:2], (floating, 4 / 3))
        self.assertEqual(shapes[-1][2], QSize(*picture.box_size(desktop_pip.MIN_WIDTH, 4 / 3)))
        self.assertEqual(shapes[-1][3], self.screen.size())
        self.ratio = 16 / 9  # a zap to a 16:9 channel
        self.pip.check()
        self.assertEqual(shapes[-1][:2], (floating, 16 / 9))
        self.ratio = None  # Fill, or not known: it keeps the one it has
        self.pip.check()
        self.assertAlmostEqual(shapes[-1][1], floating.width() / floating.height(), delta=0.01)

    def test_a_resize_macos_made_is_remembered_once_it_ends_in_shape(self):
        remembered = []
        self.pip._remember = remembered.append
        self.away()
        floating = self.pip._floating
        self.assertEqual(remembered, [])  # its own place, not the user's
        floating.setGeometry(QRect(self.place.topLeft(), QSize(546, 330)))
        self.assertTrue(wait_until(lambda: bool(remembered)))
        self.assertEqual(remembered, [floating.geometry()])
        self.assertTrue(picture.same_shape(remembered[0].width() / remembered[0].height(), 4 / 3))
        self.ratio = 16 / 9  # its own reshape (a zap) is not the user's
        self.pip.check()
        elapse(desktop_pip.SNAP_MS + 100)
        self.assertEqual(len(remembered), 1)

    def test_a_new_shape_waits_while_macos_resizes_it(self):
        self.away()
        floating = self.pip._floating
        floating.setGeometry(QRect(self.place.topLeft(), QSize(400, 300)))  # an edge dragged
        self.ratio = 16 / 9
        self.pip.check()  # the check comes during the drag: no jump under the pointer
        self.assertEqual(floating.size(), QSize(400, 300))
        self.assertTrue(
            wait_until(lambda: picture.same_shape(floating.width() / floating.height(), 16 / 9))
        )

    def test_an_edge_press_gives_the_keys_back_to_the_window(self):
        self.away()
        with mock.patch("ui.small_window.reclaim_first_responder") as reclaimed:
            self.drag(QPoint(275, 100), QPoint(290, 100))
        reclaimed.assert_called_with(self.pip._floating)


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
            host = window._floating_captions
            self.assertIsNotNone(host)
            self.assertIs(host.parentWidget(), pip.floating_window())
            # The mouse goes through to the floating picture (dragged, double-clicked there).
            self.assertTrue(host.windowFlags() & Qt.WindowType.WindowTransparentForInput)
            [overlay] = host._overlays
            window.captions._show("Merhaba")
            self.app.processEvents()
            self.assertEqual(overlay.text(), "Merhaba")
            self.assertTrue(overlay.isVisibleTo(host))
            self.assertTrue(host.isVisible())
            self.assertEqual(window.captions_overlay.text(), "Merhaba")  # the window's too
            shown["value"] = True
            pip.check()  # back: the floating window and its captions hide
            self.app.processEvents()
            self.assertFalse(host.isVisible())
            shown["value"] = False
            pip.check()
            pip.check()
            self.assertIs(window._floating_captions, host)  # made once
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

    def float_it(self, shown: dict) -> FloatingPlayer:
        """Fullscreen, then another desktop: the picture floats (in a 640×360 window)."""
        window = self.window
        pip = window.desktop_pip
        pip._on_active_space = lambda _w: shown["value"]
        pip._may_float = lambda: True  # (no libVLC here: nothing plays)
        pip._place = lambda: QRect(100, 100, 640, 360)
        window.show()
        window.showFullScreen()
        self.app.processEvents()
        shown["value"] = False
        pip.check()
        pip.check()
        floating = pip.floating_window()
        self.assertIsNotNone(floating)
        self.assertTrue(pip.floating())
        self.app.processEvents()
        return floating

    def test_its_own_controls_come_on_a_hover_and_never_touch_a_window(self):
        # 2.13.5, the owner: "THIS IS ALSO PIP AND THOSE SHOULD BE EQUAL": the mini player's
        # controls over the floating picture. And it stays as steady as in 2.13.4 (the page's
        # move made it dock again every second or two): no hover, show, fade or click of them
        # focuses, activates, raises or shows the TV window or the floating window, nor moves
        # the page.
        shown = {"value": True}
        window = self.window
        transport = window.overlays.transport
        transport._idle.setInterval(150)  # (they fade after the window's own wait)
        with (
            mock.patch("ui.tv_window._macos_desktops", return_value=True),
            mock.patch("ui.overlay_host.attach_child_window", return_value=True),  # as on macOS
        ):
            floating = self.float_it(shown)
            pip = window.desktop_pip
            controls = window.float_controls
            self.assertIsNotNone(controls)
            mirror = controls.mirror
            self.assertIs(controls.host.parentWidget(), floating)
            self.assertTrue(mirror.isHidden())  # until a hover
            self.assertEqual(mirror.icon("mini"), "full_window")  # the mini player's look
            self.assertEqual(mirror.texts(), transport.texts())  # what the window's say
            transport.set_favorite(True)
            transport.show_live("1 · News", "At nine", None)  # and each change after
            self.assertEqual((mirror.texts(), mirror._title), (transport.texts(), "1 · News"))
            self.assertTrue(mirror._favorite)
            mirror.dismiss()
            at = {"value": QPoint(-5000, -5000)}
            pip._app_active = lambda: False  # another app in front, as after a desktop change
            pip._cursor_pos = lambda: at["value"]
            pip.activity_changed()
            touched = mock.Mock()
            moves: list = []
            window.pointer.realMoved.connect(lambda: moves.append(True))
            played: list = []
            transport.playPauseRequested.connect(lambda: played.append(True))
            with contextlib.ExitStack() as stack:
                for label, target in (("window", window), ("floating", floating)):
                    for name in ("activateWindow", "raise_", "show", "setFocus", "showNormal"):
                        stack.enter_context(
                            mock.patch.object(target, name, getattr(touched, f"{label}_{name}"))
                        )
                stack.enter_context(mock.patch.object(pip, "_activate", touched.activate_app))
                stack.enter_context(
                    mock.patch(
                        "ui.tv_window.macos_window.let_presses_activate",
                        touched.let_presses_activate,
                    )
                )
                poked = stack.enter_context(
                    mock.patch.object(transport, "poke", wraps=transport.poke)
                )
                at["value"] = floating.geometry().center()  # the pointer moves over it
                self.assertTrue(wait_until(lambda: not mirror.isHidden()))
                self.assertTrue(controls.host.isVisible())
                self.assertEqual(controls.host.geometry(), floating.geometry())
                poked.assert_not_called()  # the window's own controls stay as they were
                self.assertEqual(moves, [])  # nor is it a move over the window's picture
                self.assertTrue(wait_until(mirror.isHidden))  # it fades, as over the window
                self.assertFalse(controls.host.isVisible())
                at["value"] = floating.geometry().center() + QPoint(30, 10)
                self.assertTrue(wait_until(lambda: not mirror.isHidden()))
                QTest.mouseClick(
                    mirror,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                    mirror.button_rect("play").center().toPoint(),
                )
                self.app.processEvents()
                self.assertEqual(played, [True])  # play/pause acts on the player
                self.assertTrue(pip.floating())
            self.assertEqual(touched.mock_calls, [])
            self.assertIs(window.surface.parentWidget(), floating)
            self.assertIs(window.player_page.parentWidget(), window.pages)  # the page stays
            self.assertIs(window.host.followed(), window)
            shown["value"] = True
            pip.check()  # back: its controls go with it
            self.assertTrue(mirror.isHidden())
            self.assertFalse(controls.host.isVisible())
            window.showNormal()
            self.app.processEvents()

    def test_a_click_on_the_floating_picture_opens_nothing_and_does_not_come_back(self):
        # 2.13.5, the owner: a click on the small picture opened the guide in the window and
        # brought the fullscreen desktop back. As in 2.13.4 a click on it does nothing, its
        # controls up or not, beside a control too: only the controls act.
        from tests.test_float_controls import off_a_control

        shown = {"value": True}
        window = self.window
        with mock.patch("ui.tv_window._macos_desktops", return_value=True):
            floating = self.float_it(shown)
            pip = window.desktop_pip
            controls = window.float_controls
            mirror = controls.mirror
            asked: list = []
            for request in float_controls.requests():
                getattr(window.overlays.transport, request).connect(
                    lambda *_a, r=request: asked.append(r)
                )
            drawer, guide = window.overlays.drawer, window.overlays.guide
            wait_until(drawer.isVisible, 2000)  # (the first show opens it: no channel yet)
            drawer.close_drawer()
            self.app.processEvents()
            self.assertFalse(drawer.isVisible())
            with (
                mock.patch.object(window, "raise_") as raised,
                mock.patch.object(window, "activateWindow") as activated,
                mock.patch.object(pip, "_activate") as activated_app,
                mock.patch.object(window.controller, "video_clicked") as clicked,
                mock.patch.object(window.controller, "video_double_clicked") as doubled,
            ):
                middle = window.surface.rect().center()  # the picture, the controls hidden
                mouse(window.surface, QEvent.Type.MouseButtonPress, middle)
                mouse(
                    window.surface,
                    QEvent.Type.MouseButtonRelease,
                    middle,
                    Qt.MouseButton.NoButton,
                )
                elapse(QApplication.doubleClickInterval() + 50)
                controls.hover_at(floating.geometry().center())  # the controls up
                self.app.processEvents()
                self.assertFalse(mirror.isHidden())
                _name, beside = off_a_control(mirror)  # within a button's reach, off it
                for at in (QPoint(mirror.width() // 2, 30), beside):
                    mouse(mirror, QEvent.Type.MouseButtonPress, at)
                    mouse(mirror, QEvent.Type.MouseButtonRelease, at, Qt.MouseButton.NoButton)
                    elapse(QApplication.doubleClickInterval() + 50)
                self.app.processEvents()
            self.assertEqual(asked, [])
            clicked.assert_not_called()
            doubled.assert_not_called()
            raised.assert_not_called()
            activated.assert_not_called()
            activated_app.assert_not_called()
            self.assertFalse(drawer.isVisible())  # no channel list
            self.assertFalse(guide.isVisible())
            self.assertTrue(pip.floating())  # nor back to the fullscreen desktop
            self.assertIs(window.surface.parentWidget(), floating)
            mouse(window.surface, QEvent.Type.MouseButtonDblClick, middle)  # this comes back
            self.assertFalse(pip.floating())
            window.showNormal()
            self.app.processEvents()

    def test_what_opens_in_the_window_brings_the_fullscreen_desktop_back_first(self):
        shown = {"value": True}
        window = self.window
        with mock.patch("ui.tv_window._macos_desktops", return_value=True):
            floating = self.float_it(shown)
            mirror = window.float_controls.mirror
            transport = window.overlays.transport
            pip = window.desktop_pip
            sound: list = []
            transport.audioRequested.connect(lambda: sound.append(pip.floating()))
            with (
                mock.patch.object(window, "raise_") as raised,
                mock.patch.object(window, "activateWindow") as activated,
            ):
                mirror.audioRequested.emit()  # ♫: the Sound panel lives in the window
                self.app.processEvents()
                # macOS still switches desktops: the window's checks float nothing meanwhile.
                for _ in range(desktop_pip.AWAY_CHECKS + 1):
                    pip.check()
                self.assertFalse(pip.floating())
            raised.assert_called_once()
            activated.assert_called_once()
            self.assertEqual(sound, [False])  # asked there, the picture back in the window
            self.assertTrue(floating.isHidden())
            self.assertTrue(window.isFullScreen())
            shown["value"] = True  # the fullscreen desktop shows
            pip.check()
            shown["value"] = False  # and is left again
            pip.check()
            pip.check()
            self.assertTrue(pip.floating())
            toggled: list = []
            transport.fullscreenRequested.connect(lambda: toggled.append(True))
            with (
                mock.patch.object(window, "raise_") as raised,
                mock.patch.object(window, "activateWindow") as activated,
            ):
                mirror.fullscreenRequested.emit()  # its way back: the fullscreen desktop
                self.app.processEvents()
            activated.assert_called_once()
            self.assertEqual(toggled, [])  # never out of fullscreen
            self.assertTrue(window.fullscreen_heading())
            window.showNormal()
            self.app.processEvents()

    def test_the_tv_window_taking_the_focus_while_it_floats_is_logged_with_who_asked(self):
        shown = {"value": True}
        window = self.window
        with mock.patch("ui.tv_window._macos_desktops", return_value=True):
            self.float_it(shown)
            with self.assertLogs("ui.tv_window", "INFO") as logs:
                window.activateWindow()
            lines = [line for line in logs.output if "while the picture floats" in line]
            asked = [line for line in lines if "is asked to activate" in line]
            self.assertEqual(len(asked), 1, lines)
            self.assertIn("test_the_tv_window_taking_the_focus_while_it_floats", asked[0])
            shown["value"] = True
            window.desktop_pip.check()  # back in the window: nothing to say
            with self.assertLogs("ui.tv_window", "INFO") as logs:
                logging.getLogger("ui.tv_window").info("(a line)")
                window.activateWindow()
            self.assertFalse([line for line in logs.output if "while the picture floats" in line])
            window.showNormal()
            self.app.processEvents()

    def test_a_desktop_float_holds_the_picture_alone_as_in_2_13_4(self):
        # 2.13.5, the owner: "keep it as before": the large surface alone, no keys (the mini
        # player is the one that holds the page); its controls are its own (the test above).
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
            floating = pip.floating_window()
            self.assertIs(window.surface.parentWidget(), floating)
            self.assertIs(window.player_page.parentWidget(), window.pages)  # the page stays
            self.assertIs(window.host._window, window)  # the controls stay with the window
            flags = floating.windowFlags()
            self.assertTrue(flags & Qt.WindowType.WindowDoesNotAcceptFocus)
            self.assertFalse(window.mini_player.shown())
            shown["value"] = True
            pip.check()
            window.showNormal()
            self.app.processEvents()

    def test_after_an_odd_resize_the_floating_picture_keeps_its_shape_and_fills_it(self):
        shown = {"value": True}
        main, small = _BoxedPlayer(16 / 9), _BoxedPlayer()
        players = [("main", main), ("pip", small)]
        with (
            mock.patch("ui.tv_window._macos_desktops", return_value=True),
            mock.patch.object(self.window, "_players", return_value=players),
        ):
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
            floating = pip.floating_window()
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
            shown["value"] = True
            pip.check()
            window.showNormal()
            self.app.processEvents()

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
