import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget
from timer_fixtures import elapse

from ui.overlay_host import OverlayHost
from ui.overlays.pip_label import PipLabel
from ui.pip import PipFrame
from ui.video_surface import VideoSurface

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

    def test_hide_pip_leaves_the_main_surface_large(self):
        frame = self.make()
        frame.show_pip()
        frame.set_large("pip")
        frame.hide_pip()
        self.assertTrue(frame.small_rect().isEmpty())
        self.assertTrue(self.pip.isHidden())
        self.assertFalse(self.main.isHidden())
        self.assertEqual(self.main.geometry(), self.container.rect())

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
        click(self.main, point)  # a later click swaps again
        self.assertEqual(swaps.count(), 2)

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

    def test_close_emits_close_requested(self):
        _, label = self.make()
        closes = QSignalSpy(label.closeRequested)
        mixers = QSignalSpy(label.mixerRequested)
        QTest.mouseClick(label, LEFT, Qt.NoModifier, label.button_rect("close").center().toPoint())
        self.assertEqual(closes.count(), 1)
        self.assertEqual(mixers.count(), 0)

    def test_the_speaker_asks_for_the_mixer(self):
        _, label = self.make()
        closes = QSignalSpy(label.closeRequested)
        mixers = QSignalSpy(label.mixerRequested)
        QTest.mouseClick(label, LEFT, Qt.NoModifier, label.button_rect("mixer").center().toPoint())
        self.assertEqual(mixers.count(), 1)
        self.assertEqual(closes.count(), 0)
        close, mixer = label.button_rect("close"), label.button_rect("mixer")
        self.assertLess(mixer.right(), close.left())  # next to the close button
        self.assertLess(close.right(), label.width())

    def test_a_click_on_the_pill_swaps_like_the_video_below(self):
        _, label = self.make()
        swaps = QSignalSpy(label.swapRequested)
        QTest.mouseClick(label, LEFT, Qt.NoModifier, label.pill_rect().center().toPoint())
        self.assertEqual(swaps.count(), 1)

    def test_the_mask_covers_the_pill_and_buttons_only(self):
        host, label = self.make()
        region = label.mask_region()
        for part in (label.pill_rect(), label.button_rect("close"), label.button_rect("mixer")):
            self.assertTrue(region.contains(self.host_point(label, part.center())))
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
        self.assertLess(wide.right(), label.button_rect("mixer").left())  # elided before it
        far_end = wide.topRight() + QPointF(-2, 2)
        self.assertFalse(short.contains(far_end))
        self.assertTrue(host.mask().contains(self.host_point(label, far_end)))
        self.assertEqual(
            label.info(), ("A considerably longer channel name that cannot fit", "−03:20")
        )

    def test_audible_state(self):
        _, label = self.make()
        self.assertFalse(label.is_audible())  # the small slot starts muted
        label.set_audible(True)
        self.assertTrue(label.is_audible())
        for state in ("LIVE", "−03:20", "Connecting…"):
            label.set_info("Nature One HD", state)
            self.assertFalse(label.grab().isNull())


class HostGlueTests(unittest.TestCase):
    """The overlay host glued to the player page (not to a surface) covers both corners."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_label_stays_over_whichever_surface_is_small(self):
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
        frame.show_pip()
        label.set_info("Nature One HD", "LIVE")
        label.show()
        QTest.qWait(20)

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
