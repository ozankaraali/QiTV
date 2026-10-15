import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

from ui.overlay_host import OverlayHost
from ui.overlays.base import Overlay
from ui.video_surface import VideoSurface


def surface_rect(surface):
    return QRect(surface.mapToGlobal(QPoint(0, 0)), surface.size())


class OverlayHostTests(unittest.TestCase):
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
        self.addCleanup(self.window.close)

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


class ConfigDirOverrideTests(unittest.TestCase):
    def test_env_override(self):
        from config_manager import ConfigManager

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"QITV_CONFIG_DIR": directory}),
        ):
            path = ConfigManager._get_config_path(ConfigManager.__new__(ConfigManager))
        self.assertEqual(path, os.path.join(directory, "config.json"))


if __name__ == "__main__":
    unittest.main()
