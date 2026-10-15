import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow
from shiboken6 import isValid

from ui.overlay_host import OverlayHost
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

    def test_hidden_with_window(self):
        self.panel.show()
        QTest.qWait(20)
        self.window.hide()
        QTest.qWait(30)
        self.assertFalse(self.host.isVisible())
        self.window.show()
        QTest.qWait(30)
        self.assertTrue(self.host.isVisible())

    def test_video_surface_is_native(self):
        self.assertGreater(self.surface.native_handle(), 0)


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
        QTest.qWait(QApplication.doubleClickInterval() + 100)
        self.assertEqual(got["clicked"], 1)

    def test_double_click_cancels_the_click(self):
        got = self.collect()
        QTest.mouseClick(self.target(), Qt.MouseButton.LeftButton)
        QTest.mouseDClick(self.target(), Qt.MouseButton.LeftButton)
        QTest.qWait(QApplication.doubleClickInterval() + 100)
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


class ConfigDirOverrideTests(unittest.TestCase):
    def test_env_override(self):
        from config_manager import ConfigManager

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"QITV_CONFIG_DIR": directory}),
        ):
            path = ConfigManager._get_config_path(ConfigManager.__new__(ConfigManager))
        self.assertEqual(path, os.path.join(directory, "config.json"))

    def test_unset_or_empty_override_is_ignored(self):
        from config_manager import ConfigManager

        for value in (None, ""):
            env = {} if value is None else {"QITV_CONFIG_DIR": value}
            with patch.dict(os.environ, env):
                if value is None:
                    os.environ.pop("QITV_CONFIG_DIR", None)
                path = ConfigManager._get_config_path(ConfigManager.__new__(ConfigManager))
            self.assertTrue(path.endswith("config.json"))
            self.assertNotEqual(os.path.dirname(path), "")

    def test_override_skips_legacy_migration(self):
        from config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as directory:
            old = os.getcwd()
            os.chdir(cwd)
            try:
                with open("config.json", "w") as f:
                    f.write("{}")
                manager = ConfigManager.__new__(ConfigManager)
                manager.config_path = os.path.join(directory, "config.json")
                with patch.dict(os.environ, {"QITV_CONFIG_DIR": directory}):
                    manager._migrate_old_config()
                self.assertTrue(os.path.isfile("config.json"))
                self.assertFalse(os.path.exists(manager.config_path))
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()
