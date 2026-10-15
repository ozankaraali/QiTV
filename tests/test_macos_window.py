import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import subprocess
import sys
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
# Tests that show and activate real windows run only when asked (CI's macOS jobs do): they
# take the focus from whoever uses the Mac, and the suite runs often on shared machines.
GUI_TESTS_ENV = "QITV_GUI_TESTS"


def gui_session() -> bool:
    """Whether this process has a macOS window server session (none over SSH alone)."""
    try:
        cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics") or "")
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation") or "")
    except OSError:
        return False
    cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    session = cg.CGSessionCopyCurrentDictionary()
    if not session:
        return False
    cf.CFRelease(session)
    return True


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


@unittest.skipUnless(sys.platform == "darwin", "macOS animates a restore out of the Dock")
@unittest.skipUnless(
    os.environ.get(GUI_TESTS_ENV) == "1",
    f"shows and activates a real window for about 5 s: set {GUI_TESTS_ENV}=1 to run it",
)
class DockRestoreTests(unittest.TestCase):
    """A real minimize and restore on the cocoa platform (tests/dock_fixtures.py).

    Restored by Qt (showNormal()), the overlay window was on screen in its final place for the
    whole genie animation of the TV window out of the Dock: "the UI renders first, then the
    Mac animation". It must appear only once the window has landed, from the Dock too.
    """

    @classmethod
    def setUpClass(cls):
        if not gui_session():
            raise unittest.SkipTest("no macOS GUI session (window server) to show a window in")
        child = Path(__file__).resolve().with_name("dock_fixtures.py")
        env = {**os.environ, "QT_QPA_PLATFORM": "cocoa"}
        run = subprocess.run(
            [sys.executable, "-X", "faulthandler", str(child)],
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=90,
        )
        if run.returncode != 0 or not run.stdout.strip():
            raise AssertionError(f"dock_fixtures failed ({run.returncode}):\n{run.stderr[-3000:]}")
        cls.result = json.loads(run.stdout.strip().splitlines()[-1])

    def restore(self, how):
        entry = next(e for e in self.result["restores"] if e["restore"] == how)
        self.assertTrue(self.result["shown"], "the overlay never showed")
        self.assertTrue(entry["docked"], f"the window never went to the Dock: {entry}")
        self.assertTrue(entry["host_hidden_in_dock"], entry)
        self.assertTrue(entry["restored"], f"the window or the overlays never came back: {entry}")
        return entry

    def check_landed_first(self, entry):
        self.assertTrue(entry["host_shows"], entry)
        early = [show for show in entry["host_shows"] if show["parent_miniaturized"]]
        self.assertEqual(early, [], f"the overlay window showed while still in the Dock: {entry}")
        if not entry["active"]:
            self.skipTest("QiTV could not become the active app: an early overlay stays hidden")
        if entry["animated"]:
            self.assertEqual(entry["host_on_screen_while_animating"], 0, entry)
            # On the platform's report, not after RESTORE_REPORT_MS without it.
            self.assertIsNotNone(entry["shown_after_landing_s"], f"never landed: {entry}")
            self.assertLess(entry["shown_after_landing_s"], 1.0, entry)
        self.assertTrue(entry["host_on_screen_at_end"], entry)

    def test_restored_by_qt_the_overlays_appear_once_the_window_has_landed(self):
        self.check_landed_first(self.restore("showNormal"))

    def test_restored_from_the_dock_the_overlays_appear_once_the_window_has_landed(self):
        self.check_landed_first(self.restore("deminiaturize:"))


if __name__ == "__main__":
    unittest.main()
