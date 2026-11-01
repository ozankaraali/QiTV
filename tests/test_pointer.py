import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QWidget
from timer_fixtures import elapse

from ui.pointer import PointerHider, PointerInput, SecondClickGuard


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


def _wheel(angle_y: int = 0, pixel_y: int = 0, phase=Qt.ScrollPhase.NoScrollPhase) -> QWheelEvent:
    return QWheelEvent(
        QPointF(10, 10),
        QPointF(10, 10),
        QPoint(0, pixel_y),
        QPoint(0, angle_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        phase,
        False,
    )


class WheelStepTests(unittest.TestCase):
    """Scrolling over the picture zaps (or sets the volume) a step at a time: a wheel's notch is
    a step; a trackpad's swipe must go a deliberate distance, and its glide is ignored."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.pointer = PointerInput()
        self.steps = []
        self.pointer.wheelSteps.connect(lambda steps, modifiers: self.steps.append(steps))

    def swipe(self, *pixels: int, momentum: tuple[int, ...] = ()) -> None:
        """A trackpad swipe: its start, its moves (`pixels`), its end, then its glide."""
        P = Qt.ScrollPhase
        self.pointer.wheel_event(_wheel(phase=P.ScrollBegin))
        for px in pixels:
            self.pointer.wheel_event(_wheel(2 * px, px, P.ScrollUpdate))
        self.pointer.wheel_event(_wheel(phase=P.ScrollEnd))
        for px in momentum:
            self.pointer.wheel_event(_wheel(2 * px, px, P.ScrollMomentum))

    def test_a_wheel_notch_is_one_step(self):
        self.pointer.wheel_event(_wheel(-120))  # down: the next channel
        self.pointer.wheel_event(_wheel(120))
        self.assertEqual(self.steps, [1, -1])

    def test_a_wheel_notch_with_pixels_but_no_phase_is_still_one_step(self):
        self.pointer.wheel_event(_wheel(-120, -20))  # macOS gives a mouse's notch pixels too
        self.assertEqual(self.steps, [1])

    def test_a_brush_on_the_trackpad_is_no_step(self):
        self.swipe(-10, -15, -20)  # 45 px: 90 eighths of a degree, under a notch either way
        self.swipe(-30, -40)  # 70 px: a notch's worth of angle (140) before
        self.assertEqual(self.steps, [])

    def test_a_deliberate_swipe_is_a_step_and_its_glide_is_none(self):
        self.swipe(-40, -50, -40, momentum=(-60, -60, -40, -30, -20, -10))
        self.assertEqual(self.steps, [1])

    def test_a_long_swipe_steps_per_distance(self):
        self.swipe(*([-30] * 8))  # 240 px: a step at 120, another at 240
        self.assertEqual(self.steps, [1, 1])

    def test_what_a_swipe_left_over_does_not_add_to_the_next(self):
        self.swipe(-100)
        self.swipe(-100)
        self.assertEqual(self.steps, [])

    def test_up_is_the_previous_channel(self):
        self.swipe(60, 70)
        self.assertEqual(self.steps, [-1])


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
    on whatever that page shows under the pointer (2.0.1)."""

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

    def test_a_swallowed_click_gives_the_keys_back_to_its_window(self):
        # macOS: the second click of a double-click on ⤢ lands on the video, where
        # AppKit makes libVLC's view the first responder; the window's own filter, which gives
        # the keys back (2.0.1), never sees a click the guard swallowed: every key beeped.
        surface = QWidget(self.page)
        surface.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)  # as a video surface
        surface.setGeometry(0, 0, 200, 200)
        surface.show()
        self.assertIsNotNone(surface.windowHandle())
        self.guard.arm()
        with mock.patch("ui.pointer.reclaim_first_responder") as reclaim:
            QTest.mouseClick(surface.windowHandle(), Qt.MouseButton.LeftButton, pos=QPoint(50, 50))
        self.assertEqual(reclaim.call_args_list, [mock.call(self.page.windowHandle())] * 2)
        with mock.patch("ui.pointer.reclaim_first_responder") as reclaim:
            QTest.mouseClick(surface.windowHandle(), Qt.MouseButton.LeftButton, pos=QPoint(50, 50))
        reclaim.assert_not_called()  # a click that goes through is the window's to handle

    def test_a_click_after_disarming_reaches_the_page(self):
        # The page changed again (a key played a title): the guard was for the page before.
        self.guard.arm()
        self.guard.disarm()
        self.click()
        self.assertEqual(self.page.events, ["press", "release"])

    def test_a_click_after_the_double_click_interval_reaches_the_page(self):
        self.guard.arm()
        elapse(QApplication.doubleClickInterval() + 100)
        self.click()
        self.assertEqual(self.page.events, ["press", "release"])


class PointerHiderTests(unittest.TestCase):
    """The pointer over the video goes with the player controls, and a move brings it back."""

    IDLE_MS = 40

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.video, self.host = QWidget(), QWidget()
        self.overlay = QWidget(self.host)  # the transport, say: it takes its own moves
        self.overlay.setMouseTracking(True)  # (a move reaches a widget without it no more)
        self.controls = QWidget()  # the transport
        self.allowed = True
        self.at = QPoint(100, 100)  # where the pointer is (global)
        self.hider = PointerHider(
            (self.video, self.host),
            self.controls,
            lambda: self.allowed,
            idle_ms=self.IDLE_MS,
            cursor_pos=lambda: QPoint(self.at),
        )
        self.real = []
        self.hider.realMoved.connect(lambda: self.real.append(True))

    def tearDown(self):
        for widget in (self.video, self.host, self.controls):
            widget.deleteLater()

    def move(self, dx=20, dy=0):
        """The mouse moves over the video: the surface's mouseMoved."""
        self.at += QPoint(dx, dy)
        self.hider.moved()

    def blank(self):
        return [
            w.testAttribute(Qt.WidgetAttribute.WA_SetCursor)
            and w.cursor().shape() == Qt.CursorShape.BlankCursor
            for w in (self.video, self.host)
        ]

    def rest(self):
        elapse(self.IDLE_MS * 2)

    def test_it_hides_once_the_pointer_rests(self):
        self.move()
        self.assertFalse(self.hider.hidden())
        self.rest()
        self.assertTrue(self.hider.hidden())
        self.assertEqual(self.blank(), [True, True])

    def test_a_move_brings_it_back_and_starts_the_wait_again(self):
        self.move()
        self.rest()
        self.move()
        self.assertFalse(self.hider.hidden())
        self.assertEqual(self.blank(), [False, False])
        self.rest()
        self.assertTrue(self.hider.hidden())

    def test_it_stays_while_the_controls_show_and_goes_with_them(self):
        self.controls.show()
        self.move()
        self.rest()
        self.assertFalse(self.hider.hidden())
        self.controls.hide()
        self.assertTrue(self.hider.hidden())

    def test_controls_closed_right_after_a_move_leave_it_until_the_wait_ends(self):
        self.controls.show()
        self.move()
        self.controls.hide()  # Esc, say
        self.assertFalse(self.hider.hidden())
        self.rest()
        self.assertTrue(self.hider.hidden())

    def test_it_stays_while_hiding_is_not_allowed(self):
        self.allowed = False  # a list or menu is open, or nothing plays
        self.move()
        self.rest()
        self.controls.show()
        self.controls.hide()
        self.assertFalse(self.hider.hidden())
        self.assertEqual(self.blank(), [False, False])

    # ---- 2.8.12: "sometimes the cursor forgets to vanish" -------------------------------------

    def test_once_hiding_is_allowed_again_it_hides_without_a_move(self):
        # The drawer (or a channel still opening) when the wait ended: nothing tried again.
        self.allowed = False
        self.move()
        self.rest()
        self.assertFalse(self.hider.hidden())
        self.allowed = True  # the drawer closed with a key: the mouse never moved
        self.rest()
        self.assertTrue(self.hider.hidden())

    def test_a_popup_menu_keeps_it_and_it_hides_once_the_menu_closes(self):
        menu = QMenu()
        self.addCleanup(menu.deleteLater)
        menu.addAction("One")
        self.move()
        menu.popup(QPoint(0, 0))
        self.rest()
        self.assertFalse(self.hider.hidden())
        menu.close()
        self.rest()
        self.assertTrue(self.hider.hidden())

    def test_jitter_neither_shows_it_nor_keeps_it_or_the_controls(self):
        self.move()
        for _ in range(6):  # a mouse that trembles by a pixel, again and again
            elapse(self.IDLE_MS // 3)
            self.move(1, 0) if self.at.x() % 2 else self.move(-1, 0)
        self.assertTrue(self.hider.hidden())  # the wait ran from the real move
        self.assertEqual(len(self.real), 1)  # the controls came once, for the real move
        self.move(1, 1)
        self.assertTrue(self.hider.hidden())
        self.move(10, 0)
        self.assertFalse(self.hider.hidden())
        self.assertEqual(len(self.real), 2)

    def test_on_macos_jitter_that_showed_it_hides_it_again(self):
        with mock.patch("ui.pointer.hide_pointer_until_moved", return_value=True) as hide:
            self.move()
            self.rest()
            self.assertEqual(hide.call_count, 1)
            self.move(1, 0)  # AppKit showed it on that move
            self.assertEqual(hide.call_count, 2)
            self.assertTrue(self.hider.hidden())

    def test_a_move_over_an_overlay_counts(self):
        # The controls take their own moves: on macOS they showed the pointer while it was
        # still counted hidden, and it never hid again.
        with mock.patch("ui.pointer.hide_pointer_until_moved", return_value=True) as hide:
            self.move()
            self.rest()
            self.assertTrue(self.hider.hidden())
            self.at += QPoint(30, 0)
            event = QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(5, 5),
                QPointF(self.at),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(self.overlay, event)
            self.assertFalse(self.hider.hidden())
            self.assertEqual(self.real, [True])  # the overlay answers its own moves
            self.rest()
            self.assertTrue(self.hider.hidden())
            self.assertEqual(hide.call_count, 2)

    def test_an_overlay_added_later_is_watched_too(self):
        self.move()
        late = QWidget(self.host)
        late.setMouseTracking(True)
        self.rest()
        self.at += QPoint(30, 0)
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(5, 5),
            QPointF(self.at),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(late, event)
        self.assertFalse(self.hider.hidden())

    def test_the_window_coming_back_hides_it_again(self):
        # Activated again, or out of fullscreen: the system may show the pointer meanwhile.
        with mock.patch("ui.pointer.hide_pointer_until_moved", return_value=True) as hide:
            self.move()
            self.rest()
            self.assertEqual(hide.call_count, 1)
            QApplication.sendEvent(self.video, QEvent(QEvent.Type.WindowActivate))
            self.rest()
            self.assertEqual(hide.call_count, 2)
            self.assertTrue(self.hider.hidden())


if __name__ == "__main__":
    unittest.main()
