import ctypes
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from ui import macos_window
from ui.overlay_host import OverlayHost
from ui.overlays.toast import Toast

PARENT_VIEW, CHILD_VIEW = 101, 202
PARENT_WINDOW, CHILD_WINDOW = 1001, 2002
NORMAL_LEVEL = 0


class FakeObjC:
    """Records the Objective-C messages attach_child_window sends."""

    def __init__(self):
        self.sent = []

    def __call__(self, receiver, selector, restype=ctypes.c_void_p, *args):
        self.sent.append((receiver, selector, tuple(value for _type, value in args)))
        if selector == b"window":
            return {PARENT_VIEW: PARENT_WINDOW, CHILD_VIEW: CHILD_WINDOW}[receiver]
        if selector == b"level":
            return NORMAL_LEVEL
        return None


class View:
    def __init__(self, view):
        self.view = view

    def winId(self):
        return self.view


class AttachChildWindowTests(unittest.TestCase):
    """The overlay panel on macOS: a child of the TV window, at its level, never hidden when
    QiTV is not the active app (a wheel zap over the inactive window shows the banner)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def attach(self):
        fake = FakeObjC()
        with (
            mock.patch.object(macos_window, "_send", fake),
            mock.patch.object(macos_window.QGuiApplication, "platformName", return_value="cocoa"),
        ):
            attached = macos_window.attach_child_window(View(PARENT_VIEW), View(CHILD_VIEW))
        return attached, fake.sent

    def test_the_panel_is_a_child_shown_while_inactive_at_the_window_level(self):
        attached, sent = self.attach()
        self.assertTrue(attached)
        self.assertIn((PARENT_WINDOW, b"addChildWindow:ordered:", (CHILD_WINDOW, 1)), sent)
        # Qt makes a Qt::Tool panel hide on deactivation, at NSFloatingWindowLevel (3).
        self.assertIn((CHILD_WINDOW, b"setHidesOnDeactivate:", (False,)), sent)
        self.assertIn((CHILD_WINDOW, b"setLevel:", (NORMAL_LEVEL,)), sent)
        attach = sent.index((PARENT_WINDOW, b"addChildWindow:ordered:", (CHILD_WINDOW, 1)))
        level = sent.index((CHILD_WINDOW, b"setLevel:", (NORMAL_LEVEL,)))
        self.assertGreater(level, attach)  # after AppKit's own changes on attach

    def test_other_platforms_send_nothing(self):
        fake = FakeObjC()
        with mock.patch.object(macos_window, "_send", fake):
            self.assertFalse(macos_window.attach_child_window(View(1), View(2)))
        self.assertEqual(fake.sent, [])


class HostShowTests(unittest.TestCase):
    """Showing an overlay never activates QiTV: on macOS QWidget.raise_() activates the app
    (QT_MAC_SET_RAISE_PROCESS), so a banner over the inactive window would steal the focus.
    The child window is ordered above its parent anyway."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def show_host(self, attached):
        window = QWidget()
        window.resize(640, 360)
        surface = QWidget(window)
        surface.resize(640, 360)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        host = OverlayHost(window, surface)
        overlay = Toast(host)
        host.add_overlay(overlay)
        window.show()
        with (
            mock.patch("ui.overlay_host.attach_child_window", return_value=attached),
            mock.patch.object(host, "raise_") as raise_,
        ):
            overlay.show_message("Channel 2", "info", 1000)
        self.assertTrue(host.isVisible())
        return raise_

    def test_a_host_attached_as_a_child_window_is_not_raised(self):
        self.show_host(attached=True).assert_not_called()

    def test_elsewhere_the_host_is_raised_over_the_window(self):
        self.show_host(attached=False).assert_called()


if __name__ == "__main__":
    unittest.main()
