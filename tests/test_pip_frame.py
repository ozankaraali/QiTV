import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, qInstallMessageHandler
from PySide6.QtGui import QFontMetrics, QMouseEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget
from shiboken6 import delete, isValid
from timer_fixtures import elapse, wait_until

from services import i18n
from services.i18n import translate
from ui.overlay_host import OverlayHost
from ui.overlays.painting import MIN_FONT_PX
from ui.overlays.pip_label import PipLabel
from ui.pip import LIFT_MS, PipFrame, corner_rect
from ui.video_surface import VideoSurface

# The offscreen platform has no window stacking or window masks; it says so on every raise_()
# and setMask() of a native window. Those are artefacts of the test platform, not failures.
_OFFSCREEN_NOISE = (
    "This plugin does not support raise()",
    "This plugin does not support setting window masks",
)
_previous_handler = None


def _quiet_offscreen(mode, context, message):
    if message in _OFFSCREEN_NOISE:
        return
    if _previous_handler is not None:
        _previous_handler(mode, context, message)
    else:  # Qt's default handler, as it prints
        category = context.category if context.category not in (None, "default") else ""
        sys.stderr.write(f"{category}: {message}\n" if category else message + "\n")


def setUpModule():
    global _previous_handler
    _previous_handler = qInstallMessageHandler(_quiet_offscreen)


def tearDownModule():
    qInstallMessageHandler(_previous_handler)


LEFT = Qt.MouseButton.LeftButton
NONE = Qt.MouseButton.NoButton
# 1280 x 720: 25% wide is 320, 16:9 is 180 high, 2u is 26 px (u = 12.8 px)
BOTTOM_RIGHT = QRect(934, 514, 320, 180)
TOP_LEFT = QRect(26, 26, 320, 180)


class FakeConfig:
    def __init__(self, corner=None):
        if corner is not None:
            self.pip_corner = corner
        self.saves = 0

    def save_config(self):
        self.saves += 1


def send(widget, kind, global_point, button=LEFT, buttons=LEFT):
    """A mouse event at a global point, delivered to `widget` as the platform would."""
    local = QPointF(widget.mapFromGlobal(global_point))
    event = QMouseEvent(kind, local, QPointF(global_point), button, buttons, Qt.NoModifier)
    QApplication.sendEvent(widget, event)
    return event


def centre(widget):
    return widget.mapToGlobal(widget.rect().center())


def click(widget, at=None):
    point = centre(widget) if at is None else at
    send(widget, QEvent.Type.MouseButtonPress, point)
    send(widget, QEvent.Type.MouseButtonRelease, point, LEFT, NONE)


def after_double_click_interval():
    QTest.qWait(QApplication.doubleClickInterval() + 50)


class FrameCase(unittest.TestCase):
    surface_type: type = QWidget  # VideoSurface exposes the same geometry API

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, corner=None, size=(1280, 720)):
        self.container = QWidget()
        self.container.resize(*size)
        self.main = self.surface_type(self.container)
        self.pip = self.surface_type(self.container)
        for surface in (self.main, self.pip):
            surface.setMouseTracking(True)  # as VideoSurface: moves come without a button too
        self.config = FakeConfig(corner)
        self.frame = PipFrame(self.container, self.main, self.pip, self.config)
        self.container.show()
        self.addCleanup(self.container.close)
        return self.frame

    def on_top_at_small_centre(self):
        return self.container.childAt(self.frame.small_rect().center())


class PipFrameTests(FrameCase):
    def test_hidden_until_shown(self):
        frame = self.make()
        self.assertTrue(frame.small_rect().isEmpty())
        self.assertTrue(self.pip.isHidden())
        self.assertEqual(self.main.geometry(), self.container.rect())

    def test_show_pip_places_the_small_surface_bottom_right(self):
        frame = self.make()
        frame.show_pip()
        self.assertEqual(frame.corner(), "br")
        self.assertEqual(frame.small_rect(), BOTTOM_RIGHT)
        self.assertEqual(self.pip.geometry(), BOTTOM_RIGHT)
        self.assertFalse(self.pip.isHidden())
        self.assertEqual(self.main.geometry(), self.container.rect())
        self.assertIs(self.on_top_at_small_centre(), self.pip)

    def test_show_pip_uses_the_saved_corner_and_ignores_an_unknown_one(self):
        frame = self.make(corner="tl")
        frame.show_pip()
        self.assertEqual(frame.small_rect(), TOP_LEFT)
        frame = self.make(corner="middle")
        frame.show_pip()
        self.assertEqual(frame.small_rect(), BOTTOM_RIGHT)

    def test_set_large_swaps_the_geometries_and_raises_the_new_small_one(self):
        frame = self.make()
        frame.show_pip()
        frame.set_large("pip")
        self.assertEqual(self.pip.geometry(), self.container.rect())
        self.assertEqual(self.main.geometry(), BOTTOM_RIGHT)
        self.assertEqual(frame.small_rect(), BOTTOM_RIGHT)
        self.assertIs(self.on_top_at_small_centre(), self.main)
        frame.set_large("main")
        self.assertEqual(self.main.geometry(), self.container.rect())
        self.assertEqual(self.pip.geometry(), BOTTOM_RIGHT)
        self.assertIs(self.on_top_at_small_centre(), self.pip)
        with self.assertRaises(ValueError):
            frame.set_large("other")

    def test_a_third_surface_by_name_is_hidden_until_it_has_a_role(self):
        frame = self.make()
        tile = self.surface_type(self.container)
        frame.add_surface("t2", tile)
        frame.show_pip()
        self.assertTrue(tile.isHidden())
        frame.set_large("t2")  # not the PiP's: main is hidden, pip stays small
        self.assertEqual(tile.geometry(), self.container.rect())
        self.assertTrue(self.main.isHidden())
        self.assertEqual(self.pip.geometry(), BOTTOM_RIGHT)
        frame.set_roles("main", "t2")
        self.assertEqual(self.main.geometry(), self.container.rect())
        self.assertEqual(tile.geometry(), BOTTOM_RIGHT)
        self.assertTrue(self.pip.isHidden())
        frame.set_large("t2")  # the PiP's: a swap
        self.assertEqual(self.main.geometry(), BOTTOM_RIGHT)
        with self.assertRaises(ValueError):
            frame.add_surface("t2", tile)
        with self.assertRaises(ValueError):
            frame.set_roles("main", "main")

    def test_hide_pip_leaves_the_large_surface_large(self):
        frame = self.make()
        frame.show_pip()
        frame.hide_pip()
        self.assertTrue(frame.small_rect().isEmpty())
        self.assertTrue(self.pip.isHidden())
        self.assertTrue(self.main.isVisible())
        self.assertEqual(self.main.geometry(), self.container.rect())

    def test_after_a_swap_closing_keeps_the_large_picture_on_screen(self):
        # The picture left after closing a swapped PiP went black (both hidden).
        frame = self.make()
        frame.show_pip()
        frame.set_large("pip")
        frame.hide_pip()
        self.assertTrue(self.pip.isVisible())
        self.assertEqual(self.pip.geometry(), self.container.rect())
        self.assertTrue(self.main.isHidden())
        frame.set_large("pip")  # what the controller asks for: the same, still on screen
        self.assertTrue(self.pip.isVisible())
        frame.show_pip()  # opened again: the other surface is small this time
        self.assertTrue(self.pip.isVisible() and self.main.isVisible())
        self.assertEqual(frame.small_rect(), self.main.geometry())
        frame.set_large("main")
        frame.hide_pip()
        self.assertTrue(self.main.isVisible())
        self.assertTrue(self.pip.isHidden())

    def test_a_lift_raises_a_bottom_corner_pip_and_settles_back(self):
        # Over the transport band, a bottom-corner PiP rises, animated.
        frame = self.make()
        frame.show_pip()
        rects = []
        frame.smallRectChanged.connect(rects.append)
        frame.set_lift(100)
        self.assertEqual(frame.small_rect(), BOTTOM_RIGHT)  # it moves over ~150 ms
        raised = BOTTOM_RIGHT.translated(0, -100)
        self.assertTrue(wait_until(lambda: frame.small_rect() == raised, 2000))
        self.assertEqual(rects[-1], raised)
        self.assertGreater(len(rects), 2)  # the label and the host's hole follow every step
        self.assertIs(self.on_top_at_small_centre(), self.pip)
        frame.set_lift(0)
        self.assertTrue(wait_until(lambda: frame.small_rect() == BOTTOM_RIGHT, 2000))

    def test_a_lift_taken_back_at_once_does_not_move_it(self):
        frame = self.make()
        frame.show_pip()
        frame.set_lift(100)  # the transport shows and hides again at once (a swap's banner)
        frame.set_lift(0)
        elapse(LIFT_MS + 100)
        self.assertEqual(frame.small_rect(), BOTTOM_RIGHT)

    def test_a_top_corner_pip_never_moves_for_a_lift(self):
        frame = self.make(corner="tl")
        frame.show_pip()
        frame.set_lift(100)
        elapse(LIFT_MS + 100)
        self.assertEqual(frame.small_rect(), TOP_LEFT)

    def test_a_lift_keeps_through_a_swap_a_resize_and_a_reopen(self):
        frame = self.make()
        frame.set_lift(100)  # while hidden: it applies once shown
        frame.show_pip()
        self.assertEqual(frame.small_rect(), BOTTOM_RIGHT.translated(0, -100))
        frame.set_large("pip")
        self.assertEqual(self.main.geometry(), BOTTOM_RIGHT.translated(0, -100))
        self.container.resize(1280, 720)
        self.assertEqual(frame.small_rect(), BOTTOM_RIGHT.translated(0, -100))

    def test_a_lift_waits_while_the_pip_is_dragged(self):
        frame = self.make()
        frame.show_pip()
        start = centre(self.pip)
        send(self.pip, QEvent.Type.MouseButtonPress, start)
        send(self.pip, QEvent.Type.MouseMove, start + QPoint(-20, 0))
        frame.set_lift(100)
        elapse(LIFT_MS + 100)
        self.assertEqual(frame.small_rect().top(), BOTTOM_RIGHT.top())  # under the cursor
        send(self.pip, QEvent.Type.MouseButtonRelease, start + QPoint(-20, 0), LEFT, NONE)
        self.assertEqual(frame.small_rect(), BOTTOM_RIGHT.translated(0, -100))  # snapped, raised

    def test_drag_to_the_top_left_quadrant_snaps_to_tl_and_saves_it(self):
        frame = self.make()
        frame.show_pip()
        corners = QSignalSpy(frame.cornerChanged)
        swaps = QSignalSpy(frame.swapRequested)
        start = centre(self.pip)
        send(self.pip, QEvent.Type.MouseButtonPress, start)
        send(self.pip, QEvent.Type.MouseMove, start + QPoint(3, 3), NONE)
        self.assertEqual(self.pip.geometry(), BOTTOM_RIGHT)  # under 8 px: not a drag yet
        target = self.container.mapToGlobal(QPoint(300, 200))
        send(self.pip, QEvent.Type.MouseMove, target, NONE)
        self.assertEqual(self.pip.geometry().center(), self.container.mapFromGlobal(target))
        send(self.pip, QEvent.Type.MouseButtonRelease, target, LEFT, NONE)
        self.assertEqual(frame.corner(), "tl")
        self.assertEqual(frame.small_rect(), TOP_LEFT)
        self.assertEqual(self.config.pip_corner, "tl")
        self.assertEqual(self.config.saves, 1)
        self.assertEqual([corners.at(i) for i in range(corners.count())], [["tl"]])
        self.assertEqual(swaps.count(), 0)
        self.assertIs(self.on_top_at_small_centre(), self.pip)

    def test_a_drag_stays_inside_the_container(self):
        frame = self.make()
        frame.show_pip()
        start = centre(self.pip)
        send(self.pip, QEvent.Type.MouseButtonPress, start)
        send(self.pip, QEvent.Type.MouseMove, start + QPoint(900, 900), NONE)
        self.assertTrue(self.container.rect().contains(self.pip.geometry()))
        send(self.pip, QEvent.Type.MouseButtonRelease, start + QPoint(900, 900), LEFT, NONE)
        self.assertEqual(frame.corner(), "br")
        self.assertEqual(self.config.saves, 0)  # back in the same corner: nothing to save

    def test_a_drag_whose_release_was_lost_snaps_without_swapping(self):
        frame = self.make()
        frame.show_pip()
        swaps = QSignalSpy(frame.swapRequested)
        start = centre(self.pip)
        send(self.pip, QEvent.Type.MouseButtonPress, start)
        send(self.pip, QEvent.Type.MouseMove, start - QPoint(800, 0), NONE)
        send(self.pip, QEvent.Type.MouseMove, start - QPoint(800, 0), NONE, NONE)  # no button
        self.assertEqual(frame.corner(), "bl")
        self.assertEqual(frame.small_rect(), QRect(26, 514, 320, 180))
        self.assertEqual(swaps.count(), 0)

    def test_the_small_rect_is_reported_while_dragging(self):
        frame = self.make()
        rects = QSignalSpy(frame.smallRectChanged)
        frame.show_pip()
        self.assertEqual(rects.at(rects.count() - 1), [BOTTOM_RIGHT])
        start = centre(self.pip)
        send(self.pip, QEvent.Type.MouseButtonPress, start)
        send(self.pip, QEvent.Type.MouseMove, start - QPoint(40, 30), NONE)
        self.assertEqual(rects.at(rects.count() - 1), [BOTTOM_RIGHT.translated(-40, -30)])
        send(self.pip, QEvent.Type.MouseButtonRelease, start - QPoint(40, 30), LEFT, NONE)
        self.assertEqual(rects.at(rects.count() - 1), [BOTTOM_RIGHT])
        frame.hide_pip()
        self.assertEqual(rects.at(rects.count() - 1), [QRect()])

    def test_a_click_without_movement_requests_a_swap(self):
        frame = self.make()
        frame.show_pip()
        swaps = QSignalSpy(frame.swapRequested)
        start = centre(self.pip)
        send(self.pip, QEvent.Type.MouseButtonPress, start)
        send(self.pip, QEvent.Type.MouseMove, start + QPoint(2, 1), NONE)
        send(self.pip, QEvent.Type.MouseButtonRelease, start + QPoint(2, 1), LEFT, NONE)
        self.assertEqual(swaps.count(), 1)
        self.assertEqual(frame.small_rect(), BOTTOM_RIGHT)
        self.assertEqual(self.config.saves, 0)

    def test_clicks_on_the_large_surface_are_not_taken(self):
        frame = self.make()
        frame.show_pip()
        swaps = QSignalSpy(frame.swapRequested)
        point = self.main.mapToGlobal(QPoint(100, 100))
        send(self.main, QEvent.Type.MouseButtonPress, point)
        send(self.main, QEvent.Type.MouseButtonRelease, point, LEFT, NONE)
        self.assertEqual(swaps.count(), 0)

    def test_a_double_click_on_the_small_surface_swaps_once(self):
        frame = self.make()
        frame.show_pip()
        swaps = QSignalSpy(frame.swapRequested)
        frame.swapRequested.connect(lambda: frame.set_large("pip"))
        point = centre(self.pip)
        click(self.pip, point)
        self.assertEqual(swaps.count(), 1)
        # The second press of the double-click lands on whichever surface is small now.
        self.assertIs(self.on_top_at_small_centre(), self.main)
        send(self.main, QEvent.Type.MouseButtonDblClick, point)
        send(self.main, QEvent.Type.MouseButtonRelease, point, LEFT, NONE)
        self.assertEqual(swaps.count(), 1)
        after_double_click_interval()
        click(self.main, point)  # a later click swaps again
        self.assertEqual(swaps.count(), 2)

    def test_a_quick_second_press_on_the_other_surface_does_not_swap_back(self):
        """Qt need not make a double-click of presses on two native windows."""
        frame = self.make()
        frame.show_pip()
        swaps = QSignalSpy(frame.swapRequested)
        frame.swapRequested.connect(
            lambda: frame.set_large("main" if swaps.count() % 2 == 0 else "pip")
        )
        point = centre(self.pip)
        click(self.pip, point)
        self.assertEqual(swaps.count(), 1)
        click(self.main, point + QPoint(2, 1))  # a plain press, within the double-click distance
        self.assertEqual(swaps.count(), 1)
        click(self.main, point + QPoint(40, 20))  # well away from it: a click of its own
        self.assertEqual(swaps.count(), 2)
        after_double_click_interval()
        click(self.pip, point)  # and once the interval is over, the same spot swaps again
        self.assertEqual(swaps.count(), 3)

    def test_resizing_the_container_keeps_the_corner(self):
        frame = self.make(corner="tl")
        frame.show_pip()
        self.container.resize(1000, 600)
        # 25% of 1000 is 250, 16:9 is 141 high (140.6), 2u is 20 px
        self.assertEqual(frame.small_rect(), QRect(20, 20, 250, 141))
        self.assertEqual(self.main.geometry(), QRect(0, 0, 1000, 600))
        self.assertIs(self.on_top_at_small_centre(), self.pip)
        frame.set_large("pip")
        self.container.resize(1280, 720)
        self.assertEqual(frame.small_rect(), TOP_LEFT)
        self.assertEqual(self.pip.geometry(), QRect(0, 0, 1280, 720))
        self.assertIs(self.on_top_at_small_centre(), self.main)


class NativeSurfaceTests(FrameCase):
    """With real VideoSurfaces: the small surface's clicks are the frame's, not the shell's."""

    surface_type = VideoSurface

    def test_a_click_on_the_small_surface_is_not_a_video_click(self):
        frame = self.make()
        frame.show_pip()
        swaps = QSignalSpy(frame.swapRequested)
        shell_clicks = QSignalSpy(self.pip.clicked)
        large_clicks = QSignalSpy(self.main.clicked)
        click(self.pip)
        click(self.main, self.main.mapToGlobal(QPoint(100, 100)))
        elapse(QApplication.doubleClickInterval() + 100)
        self.assertEqual(swaps.count(), 1)
        self.assertEqual(shell_clicks.count(), 0)
        self.assertEqual(large_clicks.count(), 1)

    def test_close_stops_watching_the_surfaces_and_the_page(self):
        frame = self.make()
        frame.show_pip()
        swaps = QSignalSpy(frame.swapRequested)
        shell_clicks = QSignalSpy(self.pip.clicked)
        frame.close()
        click(self.pip)
        elapse(QApplication.doubleClickInterval() + 100)
        self.assertEqual(swaps.count(), 0)
        self.assertEqual(shell_clicks.count(), 1)  # no longer taken by the frame
        self.container.resize(1000, 600)
        self.assertEqual(self.main.geometry(), QRect(0, 0, 1280, 720))  # no more layouts
        frame.close()  # again: nothing to do

    def test_a_deleted_frame_stops_watching(self):
        frame = self.make()
        frame.show_pip()
        shell_clicks = QSignalSpy(self.pip.clicked)
        delete(frame)  # Qt drops a deleted object from every event filter list
        click(self.pip)
        self.container.resize(1000, 600)
        elapse(QApplication.doubleClickInterval() + 100)
        self.assertEqual(shell_clicks.count(), 1)

    def test_deleting_the_page_with_a_cleared_frame_wrapper_raises_nothing(self):
        """The garbage collector may clear the frame's attributes before Qt deletes the page,
        whose teardown still sends events (hide, deactivate) through the frame's filters."""
        container = QWidget()
        main, pip = VideoSurface(container), VideoSurface(container)
        frame = PipFrame(container, main, pip, FakeConfig())
        frame.show_pip()
        container.show()
        QTest.qWait(10)
        frame.__dict__.clear()
        delete(container)  # raises the filter's error, if it has one
        self.assertFalse(isValid(main))

    def test_a_press_on_either_surface_gives_the_keys_back_to_the_window(self):
        # macOS: libVLC's view inside the clicked surface took the keys (ui/macos_window.py).
        frame = self.make()
        frame.show_pip()
        with mock.patch("ui.pip.reclaim_first_responder") as reclaim:
            click(self.pip)  # the frame's own click (a swap): the window never sees it
            self.assertEqual(reclaim.call_args_list, [mock.call(self.container.window())] * 2)
            reclaim.reset_mock()
            click(self.main)
            self.assertEqual(reclaim.call_count, 2)
            reclaim.reset_mock()
            send(self.main, QEvent.Type.MouseMove, centre(self.main), NONE, NONE)
            reclaim.assert_not_called()

    def test_back_on_the_small_surface_still_reaches_the_shell(self):
        frame = self.make()
        frame.show_pip()
        back = QSignalSpy(self.pip.backPressed)
        point = centre(self.pip)
        back_button = Qt.MouseButton.BackButton
        send(self.pip, QEvent.Type.MouseButtonPress, point, back_button, back_button)
        self.assertEqual(back.count(), 1)


class LabelCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self):
        window = QWidget()
        window.resize(1280, 720)
        host = OverlayHost(window, window)
        host.resize(1280, 720)
        label = PipLabel(host)
        host.add_overlay(label)
        label.set_info("Nature One HD", "LIVE")
        label.follow(BOTTOM_RIGHT)
        window.show()
        label.show()
        QTest.qWait(10)
        self.addCleanup(window.close)
        self.addCleanup(host.close)
        return host, label

    def host_point(self, label, local):
        return label.mapTo(label.parentWidget(), local.toPoint())


class PipLabelTests(LabelCase):
    def test_follows_the_small_rect(self):
        _, label = self.make()
        self.assertEqual(label.geometry(), BOTTOM_RIGHT)
        label.follow(TOP_LEFT)
        self.assertEqual(label.geometry(), TOP_LEFT)

    def clicks(self, label, button):
        """Click `button`; how many times each of the label's requests came."""
        spies = {
            name: QSignalSpy(getattr(label, f"{name}Requested"))
            for name in ("close", "mute", "swap")
        }
        pill = QSignalSpy(label.pillPressed)
        QTest.mouseClick(label, LEFT, Qt.NoModifier, label.button_rect(button).center().toPoint())
        return {name: spy.count() for name, spy in spies.items()}, pill.count()

    def test_close_emits_close_requested(self):
        _, label = self.make()
        self.assertEqual(self.clicks(label, "close"), ({"close": 1, "mute": 0, "swap": 0}, 0))

    def test_the_speaker_asks_to_mute_or_unmute_the_small_picture(self):
        _, label = self.make()
        self.assertEqual(self.clicks(label, "speaker"), ({"close": 0, "mute": 1, "swap": 0}, 0))

    def test_the_swap_button_asks_for_a_swap(self):
        _, label = self.make()
        self.assertEqual(self.clicks(label, "swap"), ({"close": 0, "mute": 0, "swap": 1}, 0))

    def test_the_pill_then_swap_speaker_and_close_from_left_to_right(self):
        _, label = self.make()
        pill = label.pill_rect()
        swap, speaker, close = (label.button_rect(n) for n in ("swap", "speaker", "close"))
        self.assertLess(pill.right(), swap.left())
        self.assertLessEqual(swap.right(), speaker.left())
        self.assertLessEqual(speaker.right(), close.left())
        self.assertLessEqual(close.right(), label.width())

    def test_the_buttons_are_easy_to_hit_at_a_960_px_window(self):
        # Measured on macOS at 960 x 540: each button took clicks over 19 x 19 px only.
        host = QWidget()
        host.resize(960, 540)
        label = PipLabel(host)
        label.follow(corner_rect(QSize(960, 540), "br"))
        for name in ("swap", "speaker", "close"):
            hit, glyph = label.button_rect(name), label.glyph_rect(name)
            self.assertGreaterEqual(min(hit.width(), hit.height()), 28, name)
            self.assertTrue(hit.contains(glyph), name)  # the drawn button may be smaller
            self.assertTrue(QRectF(label.rect()).contains(hit), name)
        self.assertLess(label.pill_rect().right(), label.button_rect("swap").left())

    def test_the_speaker_shows_whether_the_small_picture_is_heard(self):
        _, label = self.make()
        speaker = label.button_rect("speaker")
        muted = label.grab().toImage().copy(speaker.toAlignedRect())
        self.assertEqual(label.tooltip_at(speaker.center()), "Unmute the small picture")
        label.set_audible(True)
        self.assertNotEqual(label.grab().toImage().copy(speaker.toAlignedRect()), muted)
        self.assertEqual(label.tooltip_at(speaker.center()), "Mute the small picture")

    def test_the_pill_reports_press_moves_and_release_on_screen(self):
        _, label = self.make()
        pressed, moved = QSignalSpy(label.pillPressed), QSignalSpy(label.pillMoved)
        released = QSignalSpy(label.pillReleased)
        start = label.mapToGlobal(label.pill_rect().center().toPoint())
        send(label, QEvent.Type.MouseButtonPress, start)
        send(label, QEvent.Type.MouseMove, start + QPoint(20, 5), NONE)
        send(label, QEvent.Type.MouseButtonRelease, start + QPoint(20, 5), LEFT, NONE)
        self.assertEqual(pressed.at(0), [start])
        self.assertEqual(moved.at(0), [start + QPoint(20, 5)])
        self.assertEqual(released.at(0), [start + QPoint(20, 5)])
        close = label.mapToGlobal(label.button_rect("close").center().toPoint())
        send(label, QEvent.Type.MouseButtonPress, close)
        send(label, QEvent.Type.MouseButtonRelease, close, LEFT, NONE)
        self.assertEqual((pressed.count(), released.count()), (1, 1))  # buttons are not the pill

    def test_the_mask_covers_the_pill_and_buttons_only(self):
        host, label = self.make()
        region = label.mask_region()
        buttons = [label.button_rect(name) for name in ("swap", "speaker", "close")]
        for part in (label.pill_rect(), *buttons):
            self.assertTrue(region.contains(self.host_point(label, part.center())))
        for button in buttons:  # the whole hit area, not only the drawn button
            corners = (button.topLeft(), button.bottomRight() - QPointF(1, 1))
            for corner in corners:
                self.assertTrue(region.contains(self.host_point(label, corner)), corner)
                self.assertTrue(host.mask().contains(self.host_point(label, corner)), corner)
        pill = label.pill_rect()
        self.assertEqual(pill.topLeft().toPoint(), QPoint(13, 13))  # 1u inset
        for outside in (
            label.rect().center(),
            label.rect().bottomRight() - QPoint(5, 5),
            label.rect().bottomLeft() + QPoint(5, -5),
            QPoint(round(pill.right()) + 3, round(pill.center().y())),
        ):
            self.assertFalse(region.contains(label.mapTo(host, outside)), outside)
        self.assertTrue(BOTTOM_RIGHT.contains(region.boundingRect()))
        self.assertTrue(host.mask().contains(self.host_point(label, pill.center())))
        self.assertFalse(host.mask().contains(label.mapTo(host, label.rect().center())))

    def test_a_longer_name_widens_the_pill_and_the_host_mask_follows(self):
        host, label = self.make()
        short = label.pill_rect()
        label.set_info("A considerably longer channel name that cannot fit", "−03:20")
        wide = label.pill_rect()
        self.assertGreater(wide.width(), short.width())
        self.assertLess(wide.right(), label.button_rect("swap").left())  # elided before it
        far_end = wide.topRight() + QPointF(-2, 2)
        self.assertFalse(short.contains(far_end))
        self.assertTrue(host.mask().contains(self.host_point(label, far_end)))
        self.assertEqual(
            label.info(), ("A considerably longer channel name that cannot fit", "−03:20")
        )

    def test_a_title_alone_ends_at_the_pills_padding(self):
        # Polish: a movie swapped small shows its title without a state (no live wording).
        _, label = self.make()
        label.set_info("Demo film", "")
        self.assertEqual(label.shown_text(), ("Demo film", ""))
        layout = label._layout()
        title = QFontMetrics(layout.name_font).horizontalAdvance("Demo film")
        self.assertAlmostEqual(label.pill_rect().width(), 2 * layout.pad + title)
        self.assertLess(label.pill_rect().right(), label.button_rect("swap").left())

    def test_a_mini_player_keeps_the_pill_inside_the_small_rect(self):
        host = QWidget()
        host.resize(360, 203)  # u = 3.6 px; text stays at the 11 px floor
        label = PipLabel(host)
        label.follow(corner_rect(QSize(360, 203), "br"))
        for state in ("LIVE", "−03:20", "−1:02:15", "Connecting…"):
            label.set_info("Nature One HD", state)
            pill, speaker = label.pill_rect(), label.button_rect("speaker")
            self.assertTrue(QRectF(label.rect()).contains(pill), state)
            self.assertLessEqual(pill.right(), speaker.left(), state)
            name, shown = label.shown_text()
            self.assertTrue(name == "" or name.endswith("…"), name)  # never the whole name
            if state == "Connecting…":  # 90 px wide: whole, or cut once the font shrank
                elided = shown.endswith("…") and state.startswith(shown[:-1])
                self.assertTrue(shown == state or elided, shown)
            else:  # the name goes, then the font shrinks: the state is always whole
                self.assertEqual(shown, state)

    def test_the_state_is_never_cut_the_name_goes_first_then_the_font_shrinks(self):
        host = QWidget()
        host.resize(360, 203)
        label = PipLabel(host)
        label.follow(corner_rect(QSize(360, 203), "br"))
        label.set_info("Nature One HD", "LIVE")
        normal = label.state_pixel_size()
        label.set_info("Nature One HD", "−12:34:56")  # wider than the pill's room
        self.assertEqual(label.shown_text(), ("", "−12:34:56"))
        self.assertLess(label.state_pixel_size(), normal)
        self.assertLessEqual(label.pill_rect().right(), label.button_rect("speaker").left())
        label.set_info("Nature One HD", "−03:20")
        self.assertEqual(label.shown_text()[1], "−03:20")

    def test_a_narrow_pip_elides_the_name_before_the_buttons(self):
        host = QWidget()
        host.resize(800, 450)
        label = PipLabel(host)
        label.follow(corner_rect(QSize(800, 450), "br"))
        label.set_info("Nature One HD", "LIVE")
        name, shown = label.shown_text()
        self.assertTrue(name.endswith("…") and len(name) > 1, name)
        self.assertEqual(shown, "LIVE")
        self.assertLessEqual(label.pill_rect().right(), label.button_rect("swap").left())

    def test_a_longer_word_for_live_narrows_before_it_shrinks_under_9_px(self):
        # A mini player (360 px): its PiP is 90 px wide. "DIRECTO" shrank to 7 px; now its
        # letters get narrower at 9 px first, and LIVE is whole in every language.
        self.addCleanup(i18n.set_language, "en")
        for language in ("en", "tr", "de", "es", "fr"):
            self.assertEqual(i18n.set_language(language), language)
            host = QWidget()
            host.resize(360, 203)
            label = PipLabel(host)
            label.follow(corner_rect(QSize(360, 203), "br"))
            for state in ("LIVE", "REC", "−03:20", "−1:02:15"):
                with self.subTest(language=language, state=state):
                    label.set_info("Nature One HD", state)
                    self.assertEqual(label.shown_text()[1], translate(state))  # whole
                    font = label._layout().state_font
                    narrowed = font.stretch() not in (0, 100)
                    if font.pixelSize() > 9:
                        self.assertFalse(narrowed)  # narrower letters only at 9 px or less
                    if state == "LIVE":
                        self.assertGreaterEqual(label.state_pixel_size(), 9)
                    pill, speaker = label.pill_rect(), label.button_rect("speaker")
                    self.assertTrue(QRectF(label.rect()).contains(pill))
                    self.assertLessEqual(pill.right(), speaker.left())
                    self.assertFalse(label.grab().isNull())

    def test_a_small_pip_leaves_the_swap_button_out_before_live_shrinks(self):
        # Under 160 px wide (a 640 px window): the pill (which swaps too) keeps LIVE whole.
        for width, swap in ((360, False), (480, False), (640, True)):
            host = QWidget()
            host.resize(width, round(width * 9 / 16))
            label = PipLabel(host)
            label.follow(corner_rect(host.size(), "br"))
            label.set_info("Nature One HD", "LIVE")
            self.assertEqual(label.state_pixel_size(), MIN_FONT_PX, width)  # not shrunk
            self.assertEqual(not label.button_rect("swap").isEmpty(), swap, width)
            self.assertEqual(label.tooltip_at(label.button_rect("swap").center()) != "", swap)
            speaker, close = label.button_rect("speaker"), label.button_rect("close")
            self.assertLessEqual(label.pill_rect().right(), speaker.left(), width)
            self.assertLessEqual(speaker.right(), close.left(), width)
            self.assertTrue(QRectF(label.rect()).contains(close), width)

    def test_its_parts_name_their_keys(self):
        _, label = self.make()
        self.assertEqual(label.tooltip_at(label.pill_rect().center()), "Swap (S) · drag to move")
        self.assertEqual(
            label.tooltip_at(label.button_rect("close").center()), "Close picture-in-picture (P)"
        )
        speaker = label.button_rect("speaker").center()
        self.assertEqual(label.tooltip_at(speaker), "Unmute the small picture")
        self.assertEqual(label.tooltip_at(label.button_rect("swap").center()), "Swap (S)")
        self.assertEqual(label.tooltip_at(QPointF(label.rect().center())), "")

    def test_audible_state(self):
        _, label = self.make()
        self.assertFalse(label.is_audible())  # the small slot starts muted
        label.set_audible(True)
        self.assertTrue(label.is_audible())
        for state in ("LIVE", "−03:20", "Connecting…"):
            label.set_info("Nature One HD", state)
            self.assertFalse(label.grab().isNull())

    def test_the_speaker_shows_the_small_picture_s_level_while_it_is_heard(self):
        _, label = self.make()
        label.set_info("Two", "LIVE")
        self.assertEqual(label.speaker_icon(), "mute")  # muted: crossed out at any level
        label.set_audible(True)
        self.assertEqual(label.speaker_icon(), "volume_1")  # the PiP's 30 until the mixer says
        glyph = label.glyph_rect("speaker").toAlignedRect()
        low = label.grab().toImage().copy(glyph)
        label.set_level(80)
        self.assertEqual(label.speaker_icon(), "volume_3")
        self.assertNotEqual(label.grab().toImage().copy(glyph), low)
        label.set_level(0)
        self.assertEqual(label.speaker_icon(), "volume_0")
        label.set_audible(False)
        self.assertEqual(label.speaker_icon(), "mute")


class PlayerCase(unittest.TestCase):
    """The frame and label wired as the window wires them, the host glued to the page."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_player(self):
        window = QMainWindow()
        container = QWidget()
        window.setCentralWidget(container)
        main, pip = QWidget(container), QWidget(container)
        frame = PipFrame(container, main, pip, FakeConfig())
        window.setGeometry(100, 100, 1280, 720)
        window.show()
        self.addCleanup(window.close)
        QTest.qWait(20)
        host = OverlayHost(window, container)
        label = PipLabel(host)
        host.add_overlay(label)
        frame.smallRectChanged.connect(label.follow)  # host coordinates are the container's
        label.pillPressed.connect(frame.pill_press)
        label.pillMoved.connect(frame.pill_move)
        label.pillReleased.connect(frame.pill_release)
        frame.show_pip()
        label.set_info("Nature One HD", "LIVE")
        label.show()
        QTest.qWait(20)
        return main, pip, frame, label


class PillGestureTests(PlayerCase):
    def pill_point(self, label):
        return label.mapToGlobal(label.pill_rect().center().toPoint())

    def test_a_click_on_the_pill_swaps_on_release(self):
        _, _, frame, label = self.make_player()
        swaps = QSignalSpy(frame.swapRequested)
        point = self.pill_point(label)
        send(label, QEvent.Type.MouseButtonPress, point)
        send(label, QEvent.Type.MouseMove, point + QPoint(3, 2), NONE)
        self.assertEqual(swaps.count(), 0)
        send(label, QEvent.Type.MouseButtonRelease, point + QPoint(3, 2), LEFT, NONE)
        self.assertEqual(swaps.count(), 1)
        self.assertEqual(frame.small_rect(), BOTTOM_RIGHT)

    def test_a_drag_from_the_pill_snaps_the_pip_to_a_corner(self):
        _, pip, frame, label = self.make_player()
        swaps = QSignalSpy(frame.swapRequested)
        corners = QSignalSpy(frame.cornerChanged)
        point = self.pill_point(label)
        send(label, QEvent.Type.MouseButtonPress, point)
        send(label, QEvent.Type.MouseMove, point + QPoint(-800, -450), NONE)
        self.assertEqual(frame.small_rect(), BOTTOM_RIGHT.translated(-800, -450))
        self.assertEqual(label.geometry(), frame.small_rect())  # the label rides along
        send(label, QEvent.Type.MouseButtonRelease, point + QPoint(-800, -450), LEFT, NONE)
        self.assertEqual(frame.corner(), "tl")
        self.assertEqual(frame.small_rect(), TOP_LEFT)
        self.assertEqual(label.geometry(), TOP_LEFT)
        self.assertEqual(corners.count(), 1)
        self.assertEqual(swaps.count(), 0)

    def test_a_double_click_on_the_pill_swaps_once(self):
        _, _, frame, label = self.make_player()
        swaps = QSignalSpy(frame.swapRequested)
        frame.swapRequested.connect(lambda: frame.set_large("pip"))
        point = self.pill_point(label)
        click(label, point)
        send(label, QEvent.Type.MouseButtonDblClick, point)
        send(label, QEvent.Type.MouseButtonRelease, point, LEFT, NONE)
        self.assertEqual(swaps.count(), 1)


class HostGlueTests(PlayerCase):
    def test_the_label_stays_over_whichever_surface_is_small(self):
        main, pip, frame, label = self.make_player()
        host = label.parentWidget()
        window = main.window()

        def on_screen(widget):
            return QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())

        self.assertEqual(on_screen(label), on_screen(pip))
        frame.set_large("pip")
        self.assertEqual(on_screen(label), on_screen(main))
        window.move(160, 140)
        window.resize(1000, 600)
        QTest.qWait(30)
        self.assertEqual(on_screen(label), on_screen(main))
        self.assertTrue(host.geometry().contains(on_screen(main)))
        self.assertTrue(host.geometry().contains(on_screen(pip)))
        frame.hide_pip()
        self.assertFalse(label.isVisible())  # an empty mask would let the host take every click


if __name__ == "__main__":
    unittest.main()
