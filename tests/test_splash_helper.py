"""The splash (ui/splash_helper.py): its helper process spawned for real (offscreen), what it
does with "open", "quit" and a closed pipe, the parent's watcher (the "shown" hook, the join, the
kill), the window's fade, the macOS activation policy (its ctypes calls patched), and where
main() starts and ends it."""

import ctypes
import json
import multiprocessing
from multiprocessing import context as mp_context
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from ui import macos_window, splash_helper
from ui.opening_mark import CATCH_UP_MAX_S, OPEN_S, STAGE_PX
from ui.splash_helper import Splash, run_splash

app = QApplication.instance() or QApplication([])

REPO = Path(__file__).resolve().parent.parent
GEOMETRY = (100, 80, STAGE_PX, STAGE_PX)
SPAWN_S = 60.0  # a spawned helper imports PySide6 and makes its QApplication first
SLACK_S = 0.5  # a process's end, on a busy machine
KILL_WAIT_S = splash_helper.KILL_AFTER_S + 1.0  # a watcher that killed a helper has ended


def spawn(target=run_splash):
    """A helper process and the parent's end of its pipe."""
    context = multiprocessing.get_context("spawn")
    conn, child = context.Pipe(duplex=True)
    process = context.Process(target=target, args=(child, GEOMETRY), daemon=True)
    process.start()
    child.close()
    return conn, process


def stubborn(conn, geometry):
    """A helper that says it is shown, then ignores its pipe until it is killed."""
    conn.send(("shown",))
    time.sleep(60)


def pump(seconds):
    """The test's event loop, for `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


class OpacityWindow(QWidget):
    """A window that keeps the opacities it is given (the offscreen platform has none)."""

    def __init__(self):
        super().__init__()
        self.opacities = []

    def setWindowOpacity(self, level):  # noqa: N802 (Qt's name)
        self.opacities.append((time.monotonic(), level))


def unwatched(window):
    """A started Splash with no helper and no watcher: each message it queues is kept, with
    `window`'s opacity at that moment."""
    splash = Splash()
    splash._process = mock.Mock()
    sent = []

    class Queue:
        def put(self, message):
            sent.append((message, window.opacities[-1][1] if window.opacities else None))

    splash._commands = Queue()
    return splash, sent


class HelperProcessTests(unittest.TestCase):
    """run_splash in a process of its own, as main() starts it."""

    def setUp(self):
        self.conn, self.process = spawn()
        self.addCleanup(self.process.kill)

    def wait_shown(self):
        self.assertTrue(self.conn.poll(SPAWN_S), "the helper never said it was shown")
        self.assertEqual(self.conn.recv(), ("shown",))

    def test_helper_quits_when_parent_pipe_closes(self):
        self.wait_shown()
        self.conn.close()  # QiTV died, or ended before its window showed
        self.process.join(2.5)
        self.assertFalse(self.process.is_alive())
        self.assertEqual(self.process.exitcode, 0)

    def test_open_message_ends_after_finish(self):
        self.wait_shown()
        self.conn.send(("open", (0, 0, 1280, 720)))
        sent = time.monotonic()
        self.process.join(10)
        took = time.monotonic() - sent
        self.assertFalse(self.process.is_alive())
        self.assertEqual(self.process.exitcode, 0)
        # the open plays out whole (350 ms), after at most 0.4 s finishing the first draw
        self.assertGreaterEqual(took, OPEN_S)
        self.assertLessEqual(took, CATCH_UP_MAX_S + OPEN_S + SLACK_S)

    def test_quit_ends_it_at_once(self):
        self.wait_shown()
        self.conn.send(("quit",))
        sent = time.monotonic()
        self.process.join(10)
        self.assertFalse(self.process.is_alive())
        self.assertEqual(self.process.exitcode, 0)
        self.assertLess(time.monotonic() - sent, SLACK_S)

    def test_a_quit_before_it_shows_shows_nothing(self):
        # A start that handed over at once: no splash flashes after it.
        self.conn.send(("quit",))
        self.process.join(SPAWN_S)
        self.assertEqual(self.process.exitcode, 0)
        received = []
        try:
            while self.conn.poll():
                received.append(self.conn.recv())
        except EOFError:
            pass
        self.assertNotIn(("shown",), received)


class SplashTests(unittest.TestCase):
    """The parent's side: Splash's start, its watcher thread, the window's fade."""

    def test_start_failure_is_silent(self):
        window = OpacityWindow()
        self.addCleanup(window.deleteLater)
        splash = Splash()
        with (
            mock.patch.object(mp_context.SpawnProcess, "start", side_effect=OSError("no")),
            self.assertLogs("ui.splash_helper", "WARNING"),
        ):
            self.assertFalse(splash.start())
        # QiTV starts as without a splash: the window shows as it is
        splash.before_show(window)
        window.show()
        splash.open_into(window)
        splash.close()
        pump(0.05)
        self.assertEqual(window.opacities, [])

    def test_start_centres_on_the_primary_screen(self):
        area = QGuiApplication.primaryScreen().geometry()
        rect = splash_helper.centred_on_primary()
        self.assertEqual(rect.size().width(), STAGE_PX)
        self.assertEqual(rect.size().height(), STAGE_PX)
        self.assertEqual(rect.center(), area.center())

    def test_shown_calls_the_hook_on_its_thread_and_close_ends_the_helper(self):
        shown, threads = threading.Event(), []

        def on_shown():
            threads.append(threading.current_thread())
            shown.set()

        splash = Splash(on_shown=on_shown)
        self.assertTrue(splash.start(QRect(*GEOMETRY)))
        self.addCleanup(splash._process.kill)
        self.assertTrue(shown.wait(SPAWN_S))
        self.assertIsNot(threads[0], threading.main_thread())  # never the GUI thread
        # this platform (offscreen) fades no window: it is left as it is
        window = OpacityWindow()
        self.addCleanup(window.deleteLater)
        splash.before_show(window)
        self.assertEqual(window.opacities, [])
        splash.close()
        splash._watcher.join(KILL_WAIT_S)
        self.assertFalse(splash._watcher.is_alive())
        self.assertEqual(splash._process.exitcode, 0)  # it quit: it wasn't killed
        self.assertEqual(threads, [threads[0]])  # once

    def test_open_into_fades_the_window_in_as_the_helper_opens(self):
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            shown = threading.Event()
            splash = Splash(on_shown=shown.set)
            self.assertTrue(splash.start(QRect(*GEOMETRY)))
            self.addCleanup(splash._process.kill)
            self.assertTrue(shown.wait(SPAWN_S))
            window = OpacityWindow()
            self.addCleanup(window.deleteLater)
            splash.before_show(window)
            self.assertEqual([level for _, level in window.opacities], [0.0])
            window.show()
            splash.open_into(window)
            self.assertEqual(len(window.opacities), 1)  # it waits for the event loop
            began = time.monotonic()
            pump(splash_helper.FADE_IN_MS / 1000 + 0.3)
        levels = [level for _, level in window.opacities[1:]]
        self.assertEqual(levels[-1], 1.0)  # it always ends opaque
        self.assertEqual(levels, sorted(levels))
        self.assertTrue(any(0.0 < level < 1.0 for level in levels))
        ended = window.opacities[-1][0] - began
        self.assertGreaterEqual(ended, 0.3)
        self.assertLessEqual(ended, splash_helper.FADE_IN_MS / 1000 + SLACK_S)
        splash._watcher.join(CATCH_UP_MAX_S + OPEN_S + KILL_WAIT_S)
        self.assertEqual(splash._process.exitcode, 0)  # it opened and quit by itself

    def test_the_helper_opens_as_the_windows_fade_first_moves(self):
        # The window's first paint can hold the loop after its first turn (a quarter of a
        # second in a Linux test start): "open" waits for the fade's first step, so a lit mark
        # never opens and goes before the window appears.
        window = OpacityWindow()
        self.addCleanup(window.deleteLater)
        splash, sent = unwatched(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            window.show()
            splash.open_into(window)
            app.processEvents()  # the loop's first turn: the fade starts, at 0
            self.assertEqual(sent, [])
            deadline = time.monotonic() + 2
            while not sent and time.monotonic() < deadline:
                pump(0.01)
            pump(splash_helper.FADE_IN_MS / 1000 + 0.2)
        self.assertEqual([message[0] for message, _ in sent], ["open"])
        self.assertGreater(sent[0][1], 0.0)  # the window's opacity as "open" was queued
        self.assertEqual(window.opacities[-1][1], 1.0)

    def test_a_window_that_doesnt_fade_still_gets_the_open(self):
        window = OpacityWindow()
        self.addCleanup(window.deleteLater)
        splash, sent = unwatched(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=False):
            splash.before_show(window)
            window.show()
            splash.open_into(window)
            pump(0.1)
        self.assertEqual([message[0] for message, _ in sent], ["open"])
        self.assertEqual(window.opacities, [])

    def test_a_close_before_the_open_still_fades_the_window_in(self):
        window = OpacityWindow()
        self.addCleanup(window.deleteLater)
        splash, sent = unwatched(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            window.show()
            splash.open_into(window)
            app.processEvents()
            splash.close()
            pump(splash_helper.FADE_IN_MS / 1000 + 0.2)
        self.assertEqual([message[0] for message, _ in sent], ["quit"])
        self.assertEqual(window.opacities[-1][1], 1.0)  # never left transparent

    def test_a_helper_that_ignores_quit_is_killed(self):
        shown = threading.Event()
        with (
            mock.patch.object(splash_helper, "run_splash", stubborn),
            mock.patch.object(splash_helper, "KILL_AFTER_S", 0.5),
        ):
            splash = Splash(on_shown=shown.set)
            self.assertTrue(splash.start(QRect(*GEOMETRY)))
            self.addCleanup(splash._process.kill)
            self.assertTrue(shown.wait(SPAWN_S))
            with self.assertLogs("ui.splash_helper", "WARNING") as logs:
                splash.close()
                sent = time.monotonic()
                splash._watcher.join(5)
        self.assertIn("did not end", logs.output[0])
        self.assertFalse(splash._watcher.is_alive())
        self.assertGreaterEqual(time.monotonic() - sent, 0.5)
        self.assertNotEqual(splash._process.exitcode, 0)  # killed

    def test_the_helper_imports_only_the_mark(self):
        # The helper process imports ui.splash_helper: PySide6's GUI modules and the mark only
        # (PySide6.support is PySide6's own, loaded with it).
        probe = (
            "import json, sys, ui.splash_helper\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
            "('PySide6', 'ui', 'services', 'qdarktheme', 'vlc'))))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=REPO, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = {m for m in json.loads(result.stdout) if not m.startswith("PySide6.support")}
        allowed = {
            "PySide6",
            "PySide6.QtCore",
            "PySide6.QtGui",
            "PySide6.QtWidgets",
            "ui",
            "ui.brand",
            "ui.macos_window",
            "ui.opening_mark",
            "ui.splash_helper",
        }
        self.assertLessEqual(loaded, allowed)
        self.assertIn("ui.opening_mark", loaded)


class HelperWindowTests(unittest.TestCase):
    """The helper's window in this process (the test's QApplication): its kind, its place, and
    the order of its first steps."""

    def setUp(self):
        self.conn, child = multiprocessing.Pipe(duplex=True)
        self.addCleanup(self.conn.close)
        self.ended = []
        self.window = splash_helper._SplashWindow(child, GEOMETRY, lambda: self.ended.append(1))
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(child.close)

    def test_a_frameless_translucent_window_on_top_that_takes_nothing(self):
        flags = self.window.windowFlags()
        for flag in (
            Qt.WindowType.FramelessWindowHint,
            Qt.WindowType.WindowStaysOnTopHint,
            Qt.WindowType.WindowDoesNotAcceptFocus,
            Qt.WindowType.WindowTransparentForInput,
        ):
            self.assertTrue(flags & flag, flag)
        self.assertEqual(self.window.windowType(), Qt.WindowType.SplashScreen)
        self.assertTrue(self.window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(self.window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertEqual(self.window.geometry(), QRect(*GEOMETRY))

    def test_it_says_shown_once_painted_and_quit_hides_it_at_once(self):
        self.assertTrue(self.window.begin())
        self.assertTrue(self.window.isVisible())
        deadline = time.monotonic() + 5
        while not self.conn.poll() and time.monotonic() < deadline:
            pump(0.01)
        self.assertEqual(self.conn.recv(), ("shown",))
        self.conn.send(("quit",))
        pump(0.1)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.ended, [1])

    def test_what_came_before_it_shows_ends_it_unseen(self):
        for message in (("quit",), ("open", (0, 0, 800, 600))):
            with self.subTest(message=message):
                conn, child = multiprocessing.Pipe(duplex=True)
                ended = []
                window = splash_helper._SplashWindow(child, GEOMETRY, lambda: ended.append(1))
                conn.send(message)
                self.assertFalse(window.begin())
                self.assertFalse(window.isVisible())
                self.assertEqual(ended, [1])
                window.deleteLater()
                conn.close()
                child.close()

    def test_a_closed_pipe_ends_it(self):
        self.assertTrue(self.window.begin())
        self.conn.close()
        pump(0.1)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.ended, [1])

    def test_the_mac_splash_leaves_the_dock_before_it_shows(self):
        visible_then = []
        with (
            mock.patch.object(splash_helper, "_platform_name", return_value="cocoa"),
            mock.patch.object(
                splash_helper,
                "_hide_from_dock",
                side_effect=lambda: visible_then.append(self.window.isVisible()),
            ),
        ):
            self.assertTrue(self.window.begin())
        self.assertEqual(visible_then, [False])
        self.assertTrue(self.window.isVisible())

    def test_elsewhere_the_dock_is_left_alone(self):
        with (
            mock.patch.object(splash_helper, "_platform_name", return_value="xcb"),
            mock.patch.object(splash_helper, "_hide_from_dock") as hide,
        ):
            self.assertTrue(self.window.begin())
        hide.assert_not_called()


class DockTests(unittest.TestCase):
    def test_hide_from_dock_makes_the_helper_an_accessory_app(self):
        # [[NSApplication sharedApplication] setActivationPolicy:NSApplicationActivationPolicyAccessory]
        sent = []
        objc = mock.Mock()
        objc.objc_getClass.return_value = 0x1000

        def send(receiver, selector, restype=ctypes.c_void_p, *args):
            sent.append((receiver, selector, restype, args))
            return 0x2000 if selector == b"sharedApplication" else True

        with (
            mock.patch.object(macos_window, "_objc", return_value=objc),
            mock.patch.object(macos_window, "_send", side_effect=send),
        ):
            self.assertTrue(splash_helper._hide_from_dock())
        objc.objc_getClass.assert_called_once_with(b"NSApplication")
        self.assertEqual(
            sent,
            [
                (0x1000, b"sharedApplication", ctypes.c_void_p, ()),
                (0x2000, b"setActivationPolicy:", ctypes.c_bool, ((ctypes.c_long, 1),)),
            ],
        )

    def test_a_failed_call_is_never_a_reason_not_to_show(self):
        with mock.patch.object(macos_window, "_objc", side_effect=OSError("no objc")):
            self.assertFalse(splash_helper._hide_from_dock())


class MainTests(unittest.TestCase):
    """Where main() starts the splash, tells it to open, and ends it."""

    def body(self):
        return (REPO / "main.py").read_text(encoding="utf-8").split("def main()", 1)[1]

    def test_main_starts_splash_before_heavy_imports(self):
        body = self.body()
        start = body.index("splash.start()")
        self.assertLess(body.index("app = QiTVApplication(sys.argv)"), start)
        self.assertLess(start, body.index("instance = claim("))  # it shows while claim waits
        self.assertLess(start, body.index("from ui.tv_window import"))
        self.assertLess(start, body.index("import qdarktheme"))
        # a start that handed over ends it
        handed_over = body[body.index("if handed_over:") : body.index("return 0")]
        self.assertIn("splash.close()", handed_over)
        # the window starts transparent, then the splash opens into it
        show = body.index("window.show()")
        self.assertLess(body.index("splash.before_show(window)"), show)
        self.assertLess(show, body.index("splash.open_into(window)"))
        self.assertIn("splash.close()", body.rsplit("finally:", 1)[1])

    def test_no_splash_for_smoke_or_replace(self):
        import main

        screen = {"QT_QPA_PLATFORM": "xcb;wayland"}
        self.assertTrue(main.wants_splash(["qitv", "Dizi Bölüm 1.mkv"], screen))
        self.assertTrue(main.wants_splash(["qitv"], {}))
        self.assertTrue(main.wants_splash(["qitv"], {"QT_QPA_PLATFORM": "windows:darkmode=2"}))
        self.assertFalse(main.wants_splash(["qitv.exe", "--replace", "C:/old.exe"], screen))
        smoke = dict(screen, QITV_SMOKE="1")
        self.assertFalse(main.wants_splash(["qitv", "--smoke", "--out", "x"], smoke))
        # nothing to draw on: the tests' runs
        for name in ("offscreen", "minimal", "offscreen;xcb"):
            self.assertFalse(main.wants_splash(["qitv"], {"QT_QPA_PLATFORM": name}), name)
        body = self.body()
        self.assertIn("if wants_splash(sys.argv, os.environ):\n        splash.start()", body)


if __name__ == "__main__":
    unittest.main()
