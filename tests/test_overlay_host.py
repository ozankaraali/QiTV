import os
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QRegion, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QStackedWidget, QVBoxLayout, QWidget
from shiboken6 import isValid
from timer_fixtures import elapse, wait_until

from ui.overlay_host import RESTORE_REPORT_MS, OverlayHost
from ui.overlays.base import Overlay
from ui.video_surface import VideoSurface


def surface_rect(surface):
    return QRect(surface.mapToGlobal(QPoint(0, 0)), surface.size())


class HostCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QMainWindow()
        self.surface = VideoSurface()
        self.window.setCentralWidget(self.surface)
        self.window.setGeometry(100, 100, 800, 450)
        self.window.show()
        QTest.qWait(20)
        self.host = OverlayHost(self.window, self.surface)
        self.panel = Overlay()
        self.host.add_overlay(self.panel)
        self.panel.setGeometry(0, 0, 200, 100)
        self.addCleanup(lambda: isValid(self.window) and self.window.close())


class OverlayHostTests(HostCase):
    def test_a_restore_reported_before_the_state_settles_still_shows_the_overlays(self):
        # X11: the restore's last event came while the window still said minimized, and no
        # event followed (the Linux smoke's "host covers the video surface after a restore").
        self.panel.show()
        QTest.qWait(20)
        self.assertTrue(self.host.isVisible())
        with mock.patch.object(self.window, "isMinimized", return_value=True):
            self.host.eventFilter(self.window, QEvent(QEvent.Type.WindowStateChange))
            self.assertFalse(self.host.isVisible())
        self.assertTrue(wait_until(self.host.isVisible, 2000))

    def test_overlays_stack_in_the_order_they_are_added(self):
        # One built with the host as its parent already (the PiP label) sat under every overlay
        # added after it: the transport, full-size, then took the clicks on its buttons.
        early = Overlay(self.host)
        later = Overlay()
        self.host.add_overlay(later)
        self.host.add_overlay(early)
        stack = [c for c in self.host.children() if c in (self.panel, later, early)]
        self.assertEqual(stack, [self.panel, later, early])
        for overlay in (self.panel, later, early):
            overlay.setGeometry(0, 0, 200, 100)
            overlay.show()
        self.assertIs(self.host.childAt(50, 50), early)

    def test_hidden_until_an_overlay_is_visible(self):
        self.assertFalse(self.host.isVisible())
        self.panel.show()
        QTest.qWait(20)
        self.assertTrue(self.host.isVisible())
        self.panel.hide()
        QTest.qWait(20)
        self.assertFalse(self.host.isVisible())

    def test_glued_to_surface_through_move_and_resize(self):
        self.panel.show()
        QTest.qWait(20)
        self.assertEqual(self.host.geometry(), surface_rect(self.surface))
        self.window.move(160, 140)
        QTest.qWait(30)
        self.assertEqual(self.host.geometry(), surface_rect(self.surface))
        self.window.resize(1000, 560)
        QTest.qWait(30)
        self.assertEqual(self.host.geometry(), surface_rect(self.surface))

    def test_mask_is_union_of_visible_overlays(self):
        self.panel.show()
        QTest.qWait(20)
        self.assertEqual(self.host.mask().boundingRect(), QRect(0, 0, 200, 100))

    def test_clicks_on_the_pip_reach_it_unless_an_overlay_claims_them(self):
        # Phase 4: a PiP under the host's overlays takes the clicks no overlay claims there.
        class Buttons(Overlay):  # a bar with one button inside the hole
            def hole_claims(self):
                return QRegion(QRect(500, 300, 30, 30))

        through = Overlay()
        through.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # the banner
        bar = Buttons()
        for overlay, rect in ((through, QRect(0, 250, 800, 200)), (bar, QRect(0, 280, 800, 170))):
            self.host.add_overlay(overlay)
            overlay.setGeometry(rect)
            overlay.show()
        self.panel.setGeometry(360, 240, 100, 100)  # a panel over the PiP keeps its area
        self.panel.show()
        self.host.set_hole(QRect(400, 260, 200, 112))
        mask = self.host.mask()
        self.assertFalse(mask.contains(QPoint(560, 350)))  # the PiP's own
        self.assertTrue(mask.contains(QPoint(510, 310)))  # the bar's button over it
        self.assertTrue(mask.contains(QPoint(420, 300)))  # the panel over it
        self.assertTrue(mask.contains(QPoint(100, 300)))  # beside it: the overlays'
        self.host.set_hole(QRect())
        self.assertTrue(self.host.mask().contains(QPoint(560, 350)))

    def test_hidden_with_window(self):
        self.panel.show()
        QTest.qWait(20)
        self.window.hide()
        QTest.qWait(30)
        self.assertFalse(self.host.isVisible())
        self.window.show()
        QTest.qWait(30)
        self.assertTrue(self.host.isVisible())

    def test_hidden_while_another_page_hides_the_surface(self):
        # The TV window's Library page replaces the player page (and its surface).
        pages = QStackedWidget()
        player = QWidget()
        self.surface.setParent(player)
        player_layout = QVBoxLayout(player)
        player_layout.addWidget(self.surface)
        library = QWidget()
        pages.addWidget(player)
        pages.addWidget(library)
        self.window.setCentralWidget(pages)
        QTest.qWait(20)
        self.panel.show()
        self.assertTrue(wait_until(self.host.isVisible))
        pages.setCurrentWidget(library)
        QTest.qWait(20)
        self.assertFalse(self.host.isVisible())
        self.panel.update()  # an overlay still shown changes nothing while the page is away
        self.host.overlay_changed()
        self.assertFalse(self.host.isVisible())
        pages.setCurrentWidget(player)
        self.assertTrue(wait_until(self.host.isVisible))
        self.assertEqual(self.host.geometry(), surface_rect(self.surface))

    def test_video_surface_is_native(self):
        self.assertGreater(self.surface.native_handle(), 0)


class PlatformReports(QObject):
    """Swallows the platform's reports of window state changes (spontaneous events)."""

    def eventFilter(self, obj, event):
        return event.type() == QEvent.Type.WindowStateChange and event.spontaneous()


class RestoreFromDockTests(HostCase):
    """macOS animates a restore out of the Dock and reports it once the window has landed,
    while Qt reports a restore it asked for itself (showNormal()) at once. A host shown on
    Qt's word sat in its final place before the window had flown in (the owner's report)."""

    def setUp(self):
        super().setUp()
        animated = patch("ui.overlay_host.animates_restore", return_value=True)
        animated.start()
        self.addCleanup(animated.stop)
        self.panel.show()
        QTest.qWait(20)

    def minimize(self):
        self.window.showMinimized()
        self.assertTrue(wait_until(lambda: not self.host.isVisible()))
        QTest.qWait(20)  # and the platform's report of it

    def test_overlays_wait_for_the_platform_to_report_the_restore(self):
        self.minimize()
        self.window.showNormal()  # the offscreen platform reports it on the next event loop pass
        self.assertFalse(self.window.isMinimized())
        self.assertFalse(self.host.isVisible())
        self.assertTrue(wait_until(self.host.isVisible, RESTORE_REPORT_MS // 2))  # not the fallback
        self.assertEqual(self.host.geometry(), surface_rect(self.surface))

    def test_overlays_return_after_a_while_without_the_platform_report(self):
        self.minimize()
        reports = PlatformReports()
        self.window.windowHandle().installEventFilter(reports)  # runs before the host's filter
        self.window.showNormal()
        elapse(RESTORE_REPORT_MS // 2)
        self.assertFalse(self.host.isVisible())
        elapse(RESTORE_REPORT_MS // 2 + 200)
        self.assertTrue(self.host.isVisible())

    def test_elsewhere_overlays_return_with_the_window(self):
        with patch("ui.overlay_host.animates_restore", return_value=False):
            self.minimize()
            self.window.showNormal()
            self.assertTrue(self.host.isVisible())


def wheel(widget, dy, modifiers=Qt.KeyboardModifier.NoModifier):
    event = QWheelEvent(
        QPointF(5, 5),
        QPointF(5, 5),
        QPoint(0, 0),
        QPoint(0, dy),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


class PointerBehaviourMixin:
    """Run against both VideoSurface and OverlayHost: they must debounce identically."""

    def target(self):
        raise NotImplementedError

    def collect(self):
        got = {"clicked": 0, "doubleClicked": 0, "steps": []}
        t = self.target()
        t.clicked.connect(lambda: got.__setitem__("clicked", got["clicked"] + 1))
        t.doubleClicked.connect(lambda: got.__setitem__("doubleClicked", got["doubleClicked"] + 1))
        t.wheelSteps.connect(lambda n, m: got["steps"].append(n))
        return got

    def test_press_fires_one_click_after_the_interval(self):
        got = self.collect()
        QTest.mouseClick(self.target(), Qt.MouseButton.LeftButton)
        self.assertEqual(got["clicked"], 0)
        elapse(QApplication.doubleClickInterval() + 100)
        self.assertEqual(got["clicked"], 1)

    def test_double_click_cancels_the_click(self):
        got = self.collect()
        QTest.mouseClick(self.target(), Qt.MouseButton.LeftButton)
        QTest.mouseDClick(self.target(), Qt.MouseButton.LeftButton)
        elapse(QApplication.doubleClickInterval() + 100)
        self.assertEqual(got["doubleClicked"], 1)
        self.assertEqual(got["clicked"], 0)

    def test_small_wheel_deltas_accumulate(self):
        got = self.collect()
        for _ in range(12):
            wheel(self.target(), 10)
        self.assertEqual(got["steps"], [-1])  # scroll up (positive delta) = previous


class VideoSurfacePointerTests(PointerBehaviourMixin, HostCase):
    def target(self):
        return self.surface


class OverlayHostPointerTests(PointerBehaviourMixin, HostCase):
    def setUp(self):
        super().setUp()
        self.panel.show()
        QTest.qWait(20)

    def target(self):
        return self.host


class LifetimeTests(HostCase):
    def test_remove_overlay_and_destroyed_overlay(self):
        self.panel.show()
        QTest.qWait(20)
        self.host.remove_overlay(self.panel)
        self.assertEqual(self.host.visible_overlays(), [])
        other = Overlay()
        self.host.add_overlay(other)
        other.setGeometry(0, 0, 10, 10)
        other.show()
        other.deleteLater()
        QTest.qWait(30)
        self.assertEqual(self.host.visible_overlays(), [])

    def test_filters_survive_window_destruction(self):
        self.panel.show()
        QTest.qWait(20)
        self.window.deleteLater()
        QTest.qWait(50)
        self.host.eventFilter(self.window, QEvent(QEvent.Type.Move))  # must not raise

    def test_reattaches_when_native_window_changes(self):
        calls = []
        with patch(
            "ui.overlay_host.attach_child_window", lambda p, c: calls.append(int(c.winId())) or True
        ):
            self.panel.show()
            QTest.qWait(20)
            self.panel.hide()
            QTest.qWait(20)
            self.host.winId()
            self.host.destroy()  # drops the native window; show() recreates a new one
            self.host.create()
            self.panel.show()
            QTest.qWait(20)
        self.assertGreaterEqual(len(calls), 1)

    def test_attaches_again_after_every_show(self):
        # macOS drops a child window from its parent when the child is ordered out.
        calls = []
        with patch("ui.overlay_host.attach_child_window", lambda p, c: calls.append(c) or True):
            self.panel.show()
            QTest.qWait(20)
            self.panel.hide()
            QTest.qWait(20)
            self.panel.show()
            QTest.qWait(20)
        self.assertEqual(calls, [self.host, self.host])

    def test_stays_on_top_overlays_stay_above_raised_overlays(self):
        top = Overlay()
        top.stays_on_top = True
        self.host.add_overlay(top)
        later = Overlay()  # created and added after: would cover the top overlay
        self.host.add_overlay(later)
        stack = self.host.children()
        self.assertGreater(stack.index(top), stack.index(later))
        top.show()
        self.panel.show()
        self.panel.raise_()
        stack = self.host.children()
        self.assertGreater(stack.index(top), stack.index(self.panel))


class ConfigDirOverrideTests(unittest.TestCase):
    def test_env_override(self):
        from services.config_manager import ConfigManager

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"QITV_CONFIG_DIR": directory}),
        ):
            path = ConfigManager._get_config_path(ConfigManager.__new__(ConfigManager))
        self.assertEqual(path, os.path.join(directory, "config.json"))

    def test_unset_or_empty_override_is_ignored(self):
        from services.config_manager import ConfigManager

        for value in (None, ""):
            env = {} if value is None else {"QITV_CONFIG_DIR": value}
            with patch.dict(os.environ, env):
                if value is None:
                    os.environ.pop("QITV_CONFIG_DIR", None)
                path = ConfigManager._get_config_path(ConfigManager.__new__(ConfigManager))
            self.assertTrue(path.endswith("config.json"))
            self.assertNotEqual(os.path.dirname(path), "")

    def test_a_config_json_in_the_current_directory_stays_put(self):
        # Pre-1.0 QiTV kept config.json in the current directory, and later versions moved
        # one found there into the config directory: whatever file sat where QiTV started.
        from services.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as directory:
            decoy = os.path.join(cwd, "config.json")
            with open(decoy, "w", encoding="utf-8") as f:
                f.write('{"favorites": ["Decoy"]}')
            config_path = os.path.join(directory, "config.json")
            old = os.getcwd()
            os.chdir(cwd)
            try:
                with (
                    patch.dict(os.environ),
                    patch.object(ConfigManager, "_get_config_path", return_value=config_path),
                ):
                    os.environ.pop("QITV_CONFIG_DIR", None)
                    manager = ConfigManager()
            finally:
                os.chdir(old)
            with open(decoy, encoding="utf-8") as f:
                self.assertEqual(f.read(), '{"favorites": ["Decoy"]}')
            self.assertNotIn("Decoy", manager.favorites or [])


if __name__ == "__main__":
    unittest.main()
