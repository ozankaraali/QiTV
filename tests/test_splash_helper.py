"""The splash (ui/splash_helper.py): its helper process spawned for real (offscreen), what it
does with "open", "handover", "quit" and a closed pipe, the parent's watcher (the "shown" hook,
the join, the kill, a failed helper's exit code), the window's opening (the fade on "opening",
the hand-over onto the player's mark, the window's own top-levels fading with it), the macOS
activation policy (its ctypes calls patched), and where main() starts and ends it.

Timings: lower bounds and order prove the behaviour; upper bounds are generous (CI's runners
are slow) or left out."""

import ctypes
import gc
import json
import multiprocessing
from multiprocessing import context as mp_context
from multiprocessing.connection import wait
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
from motion_fixtures import pin_motion

from ui import macos_window, splash_helper
from ui.opening_mark import (
    CATCH_UP_MAX_S,
    DRAWN_AT,
    FILLED_AT,
    HOLD_FILL_S,
    LIT_UNTIL,
    OPEN_S,
    STAGE_PX,
    STILL_T,
    OpeningMark,
)
from ui.splash_helper import Splash, run_splash

app = QApplication.instance() or QApplication([])

REPO = Path(__file__).resolve().parent.parent
GEOMETRY = (100, 80, STAGE_PX, STAGE_PX)
NEAR = (140, 130)  # the player's mark, a short glide from GEOMETRY
SPAWN_S = 60.0  # a spawned helper imports PySide6 and makes its QApplication first
WAIT_S = 10.0  # the most a test waits for what takes a second at most
KILL_WAIT_S = splash_helper.KILL_AFTER_S + 1.0  # a watcher that killed a helper has ended
FADE_S = splash_helper.FADE_IN_MS / 1000
ORIGIN = 1234.5  # the splash's loop origin, on time.monotonic


def spawn(target=run_splash):
    """A helper process and the parent's end of its pipe."""
    context = multiprocessing.get_context("spawn")
    conn, child = context.Pipe(duplex=True)
    process = context.Process(target=target, args=(child, GEOMETRY), daemon=True)
    process.start()
    child.close()
    return conn, process


def stubborn(conn, geometry, lit=False):
    """A helper that says it is shown, then ignores its pipe until it is killed."""
    conn.send(("shown",))
    time.sleep(60)


LATE_S = 1.5  # a helper that shows this late: after the window is on screen


def late(conn, geometry, lit=False):
    """The real helper, slow to start (a busy computer, a slow disk)."""
    time.sleep(LATE_S)
    run_splash(conn, geometry, lit)


def dockless(conn, geometry, lit=False):
    """The real helper on a Mac whose activation-policy call fails."""
    with (
        mock.patch.object(splash_helper, "_platform_name", return_value="cocoa"),
        mock.patch.object(macos_window, "_objc", side_effect=OSError("no objc")),
    ):
        run_splash(conn, geometry, lit)


def failing(conn, geometry, lit=False):
    """A helper that fails as it starts, as a crash would: only its exit code says so."""
    os._exit(3)


def pump(seconds):
    """The test's event loop, for `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def pump_until(done, timeout=WAIT_S):
    """The test's event loop until `done()` (True), or `timeout` (False)."""
    deadline = time.monotonic() + timeout
    while not done():
        if time.monotonic() >= deadline:
            return False
        app.processEvents()
        time.sleep(0.005)
    return True


class OpacityWindow(QWidget):
    """A window that keeps the opacities it is given (the offscreen platform has none)."""

    def __init__(self, parent=None, flags=Qt.WindowType.Widget):
        super().__init__(parent, flags)
        self.opacities = []

    def setWindowOpacity(self, level):  # noqa: N802 (Qt's name)
        self.opacities.append((time.monotonic(), level))


def unwatched(window, *, shown=True):
    """A started Splash with no helper and no watcher, its window at GEOMETRY (on screen once
    `shown`, as its "shown" says): each message it queues is kept, with `window`'s opacity
    and the time at that moment."""
    splash = Splash()
    splash._process = mock.Mock()
    splash._place = QRect(*GEOMETRY)
    splash._shown = shown
    sent = []

    class Queue:
        def put(self, message):
            sent.append((message, opacity(window), time.monotonic()))

    splash._commands = Queue()
    return splash, sent


def opacity(window):
    return window.opacities[-1][1] if window.opacities else None


def names(sent):
    return [message[0] for message, _, _ in sent]


def rising(window):
    """When the window's fade first rose above 0 (None before)."""
    return next((when for when, level in window.opacities if level > 0.0), None)


class Litter:
    """A plain object, made by the thousand to take the memory a freed one leaves."""


class PaintedMark(QWidget):
    """A mark that keeps when it painted."""

    def __init__(self):
        super().__init__()
        self.painted = []

    def paintEvent(self, event):  # noqa: N802 (Qt's name)
        self.painted.append(time.monotonic())


class FakeOverlay:
    """The player page's OpeningOverlay as Splash sees it: whether it draws a file's mark, the
    mark (its loop origin and place; let go, it shows and paints), and each hold with the
    window's opacity then."""

    def __init__(self, window, *, drawing=True, origin=ORIGIN, at=NEAR):
        self.window = window
        self.drawing = drawing
        self.mark = PaintedMark()
        self.mark.setGeometry(*at, STAGE_PX, STAGE_PX)
        self.mark.origin = origin
        self.holds = []
        self.fades = []  # whether each let-go fades the mark in
        self.loop_origin = None
        self.loop_held = None

    def opening(self):
        return self.drawing

    def hold_mark(self, held, *, fade=False):
        self.holds.append((held, opacity(self.window)))
        if not held:
            self.fades.append(fade)
        self.mark.setVisible(not held and self.drawing)


class HelperProcessTests(unittest.TestCase):
    """run_splash in a process of its own, as main() starts it."""

    def setUp(self):
        pin_motion(self)
        self.conn, self.process = spawn()
        self.addCleanup(self.process.kill)

    def wait_shown(self):
        """("shown", its loop origin): a time on this process's monotonic clock too."""
        self.assertTrue(self.conn.poll(SPAWN_S), "the helper never said it was shown")
        name, origin = self.conn.recv()
        self.assertEqual(name, "shown")
        self.assertLess(time.monotonic() - SPAWN_S, origin)
        self.assertLessEqual(origin, time.monotonic())

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
        self.assertTrue(self.conn.poll(WAIT_S))
        self.assertEqual(self.conn.recv(), ("opening",))  # its draw completed: it opens out
        opening = time.monotonic()
        self.process.join(WAIT_S)
        ended = time.monotonic()
        self.assertFalse(self.process.is_alive())
        self.assertEqual(self.process.exitcode, 0)
        # the open plays out whole (350 ms), after at most 0.4 s finishing the first draw (the
        # cap itself: test_opening_mark; here only a bound CI's slow runners keep)
        self.assertGreaterEqual(ended - opening, OPEN_S - 0.05)
        self.assertLessEqual(opening - sent, 10 * CATCH_UP_MAX_S)

    def test_after_a_handover_it_glides_and_waits_for_quit(self):
        # It glides onto the player's mark and loops on there; one never told to quit ends
        # by itself HANDOVER_LIMIT_MS after its glide.
        self.wait_shown()
        self.conn.send(("handover", (*NEAR, STAGE_PX, STAGE_PX)))
        sent = time.monotonic()
        glide_and_wait = FADE_S + splash_helper.HANDOVER_LIMIT_MS / 1000
        self.process.join(glide_and_wait - 0.3)
        self.assertTrue(self.process.is_alive())
        self.process.join(3 * glide_and_wait)
        self.assertFalse(self.process.is_alive())
        self.assertEqual(self.process.exitcode, 0)
        # Qt's coarse timers may fire up to 5 % early
        self.assertGreaterEqual(time.monotonic() - sent, glide_and_wait - 0.1)

    def test_quit_ends_it_at_once(self):
        self.wait_shown()
        self.conn.send(("quit",))
        sent = time.monotonic()
        self.process.join(WAIT_S)
        self.assertFalse(self.process.is_alive())
        self.assertEqual(self.process.exitcode, 0)
        self.assertLess(time.monotonic() - sent, 3.0)  # (about 0.1 s; CI's runners are slow)
        heard = []
        try:
            while self.conn.poll():
                heard.append(self.conn.recv())
        except EOFError:
            pass
        self.assertNotIn(("opening",), heard)  # it quit: its mark never opened out

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
        self.assertNotIn("shown", [message[0] for message in received])


class SplashTests(unittest.TestCase):
    """The parent's side: Splash's start, its watcher thread, the window's opening."""

    def setUp(self):
        pin_motion(self)

    def window(self, layer=False):
        """A window (and, `layer`, a Tool window of its own that shows with it, as the player
        page's OverlayHost does), each keeping its opacities."""
        window = OpacityWindow()
        self.addCleanup(window.deleteLater)
        if not layer:
            return window
        return window, OpacityWindow(window, Qt.WindowType.Tool)

    def test_start_failure_is_silent(self):
        window = self.window()
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

    def test_it_centres_as_the_bootloaders_splash_does(self):
        # PyInstaller's Tk script puts its image at int(x + 0.5 * (width - 240)): on a screen
        # of odd size, Qt's own centring would land a pixel off it.
        for area, top_left in (
            (QRect(0, 0, 1367, 769), (563, 264)),
            (QRect(1920, -40, 1366, 768), (1920 + 563, -40 + 264)),
        ):
            with self.subTest(area=area):
                screen = mock.Mock()
                screen.geometry.return_value = area
                with mock.patch.object(QGuiApplication, "primaryScreen", return_value=screen):
                    rect = splash_helper.centred_on_primary()
                self.assertEqual((rect.x(), rect.y()), top_left)
                self.assertEqual((rect.width(), rect.height()), (STAGE_PX, STAGE_PX))

    def test_over_the_bootloaders_splash_the_helper_starts_on_the_lit_frame(self):
        # The bootloader's splash is the held, lit frame: the helper's mark first shows that
        # frame (its loop's origin set back to it), then loops on from there.
        shown = threading.Event()
        splash = Splash(on_shown=shown.set)
        self.assertTrue(splash.start(QRect(*GEOMETRY), lit=True))
        self.addCleanup(splash._process.kill)
        self.assertTrue(shown.wait(SPAWN_S))
        heard = time.monotonic()
        self.assertIsNotNone(splash._origin)
        # held from its first paint (the still's frame then, the origin being now): at most
        # SPAWN_S ago
        self.assertLessEqual(heard - splash._origin, SPAWN_S)
        self.assertGreaterEqual(heard - splash._origin, 0.0)
        self.assertTrue(splash._lit)  # the player's mark is held lit on that origin too
        splash.close()
        splash._watcher.join(KILL_WAIT_S)

    def test_shown_calls_the_hook_on_its_thread_and_close_ends_the_helper(self):
        shown, threads = threading.Event(), []

        def on_shown():
            threads.append(threading.current_thread())
            shown.set()

        ended = []
        splash = Splash(on_shown=on_shown, on_end=lambda: ended.append(threading.current_thread()))
        self.assertTrue(splash.start(QRect(*GEOMETRY)))
        self.addCleanup(splash._process.kill)
        self.assertTrue(shown.wait(SPAWN_S))
        self.assertIsNot(threads[0], threading.main_thread())  # never the GUI thread
        self.assertEqual(ended, [])  # it runs on
        # this platform (offscreen) fades no window: it is left as it is
        window = self.window()
        splash.before_show(window)
        self.assertEqual(window.opacities, [])
        with self.assertNoLogs("ui.splash_helper", "WARNING"):
            splash.close()
            splash._watcher.join(KILL_WAIT_S)
        self.assertFalse(splash._watcher.is_alive())
        self.assertEqual(splash._process.exitcode, 0)  # it quit: it wasn't killed
        self.assertEqual(threads, [threads[0]])  # once
        self.assertEqual(ended, [threads[0]])  # once it ended, on the watcher's thread

    def test_a_helper_that_ends_unshown_calls_the_end_hook_only(self):
        # The bootloader's splash waits for "shown": a helper that never shows lets it go too.
        shown, ended = [], []
        with mock.patch.object(splash_helper, "run_splash", failing):
            splash = Splash(on_shown=lambda: shown.append(1), on_end=lambda: ended.append(1))
            with self.assertLogs("ui.splash_helper", "WARNING"):
                self.assertTrue(splash.start(QRect(*GEOMETRY)))
                self.addCleanup(splash._process.kill)
                splash._watcher.join(SPAWN_S)
        self.assertFalse(splash._watcher.is_alive())
        self.assertEqual((shown, ended), ([], [1]))

    def test_an_end_hook_that_fails_is_only_logged(self):
        def broken():
            raise RuntimeError("broken")

        with mock.patch.object(splash_helper, "run_splash", failing):
            splash = Splash(on_end=broken)
            with self.assertLogs("ui.splash_helper", "WARNING") as logs:
                self.assertTrue(splash.start(QRect(*GEOMETRY)))
                self.addCleanup(splash._process.kill)
                splash._watcher.join(SPAWN_S)
        self.assertFalse(splash._watcher.is_alive())
        self.assertTrue(any("end hook failed" in line for line in logs.output), logs.output)

    def test_open_into_fades_the_window_in_as_the_helper_opens(self):
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            shown = threading.Event()
            splash = Splash(on_shown=shown.set)
            self.assertTrue(splash.start(QRect(*GEOMETRY)))
            self.addCleanup(splash._process.kill)
            self.assertTrue(shown.wait(SPAWN_S))
            window = self.window()
            splash.before_show(window)
            self.assertEqual([level for _, level in window.opacities], [0.0])
            window.show()
            splash.open_into(window)
            self.assertEqual(len(window.opacities), 1)  # it waits for the event loop
            self.assertTrue(pump_until(lambda: opacity(window) == 1.0))
        levels = [level for _, level in window.opacities[1:]]
        self.assertEqual(levels, sorted(levels))
        self.assertTrue(any(0.0 < level < 1.0 for level in levels))
        self.assertGreaterEqual(window.opacities[-1][0] - rising(window), 0.3)
        splash._watcher.join(WAIT_S)
        self.assertEqual(splash._process.exitcode, 0)  # it opened and quit by itself

    def test_a_window_ready_before_the_splash_shows_shows_as_it_is(self):
        # Nothing of the splash is on screen yet (PyInstaller's still may be): the window
        # never waits transparent behind nothing. It shows as it is, and the splash, told to
        # open before it showed, ends unseen.
        window = self.window()
        splash, sent = unwatched(window, shown=False)
        overlay = FakeOverlay(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            window.show()
            splash.open_into(window, overlay)
            self.assertTrue(pump_until(lambda: sent))
            splash._relay.tell(("ended",))
            self.assertTrue(pump_until(lambda: len(overlay.holds) == 2))
        self.assertEqual(names(sent), ["open"])
        self.assertEqual(window.opacities, [])  # never transparent
        self.assertEqual(overlay.fades, [True])  # the player's mark fades in

    def test_a_splash_that_shows_late_never_comes_after_the_window(self):
        # A real helper that starts after the window is on screen, in main()'s order: the
        # window is never transparent, the helper ends unseen, and PyInstaller's still goes
        # only once the window is drawn (never an empty screen between them).
        shown, closed, drawn = [], [], []
        window = self.window()
        window.paintEvent = lambda event: drawn.append(time.monotonic())
        with (
            mock.patch.object(splash_helper, "run_splash", late),
            mock.patch.object(splash_helper, "_fades_windows", return_value=True),
        ):
            splash = Splash(on_shown=lambda: shown.append(1))
            self.assertTrue(splash.start(QRect(*GEOMETRY)))
            self.addCleanup(splash._process.kill)
            splash.before_show(window)
            window.show()
            splash_helper.when_drawn(window, lambda: closed.append(time.monotonic()))
            splash.open_into(window)
            self.assertTrue(pump_until(lambda: not splash._watcher.is_alive(), SPAWN_S))
        self.assertEqual(window.opacities, [])
        self.assertEqual(shown, [])  # it ended unseen
        self.assertEqual(splash._process.exitcode, 0)
        self.assertTrue(drawn and closed)
        self.assertGreater(closed[0], drawn[0])  # the still went once the window was drawn

    def test_the_window_fades_as_the_mark_opens_out(self):
        # "open" goes once the window is on screen; the window fades as the helper says its
        # mark opens out ("opening", after its draw completed).
        window = self.window()
        splash, sent = unwatched(window)
        with (
            mock.patch.object(splash_helper, "_fades_windows", return_value=True),
            # no fallback meanwhile: only "opening" can start the fade
            mock.patch.object(splash_helper, "OPENING_WAIT_MS", 3_600_000),
        ):
            splash.before_show(window)
            window.show()
            splash.open_into(window)
            self.assertTrue(pump_until(lambda: sent))
            self.assertEqual(names(sent), ["open"])
            pump(0.1)
            self.assertEqual([level for _, level in window.opacities], [0.0])  # it waits
            splash._relay.tell(("opening",))
            told = time.monotonic()
            self.assertTrue(pump_until(lambda: opacity(window) == 1.0))
        self.assertGreaterEqual(rising(window), told)

    def test_without_opening_the_window_fades_0_6_s_after_open(self):
        window = self.window()
        splash, sent = unwatched(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            window.show()
            splash.open_into(window)
            self.assertTrue(pump_until(lambda: opacity(window) == 1.0))
        self.assertEqual(names(sent), ["open"])
        waited = rising(window) - sent[0][2]
        # Qt's coarse timers may fire up to 5 % early (seen: 0.599 s, four copies at once)
        self.assertGreaterEqual(waited, 0.9 * splash_helper.OPENING_WAIT_MS / 1000)
        self.assertLess(waited, 3 * splash_helper.OPENING_WAIT_MS / 1000)

    def test_the_windows_own_top_levels_fade_in_with_it(self):
        # The player page's OverlayHost (title, controls, toasts) is a window of its own: it
        # starts transparent and fades in step with the window, never left at 0.
        window, layer = self.window(layer=True)
        splash, sent = unwatched(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            self.assertEqual([level for _, level in layer.opacities], [0.0])
            window.show()  # (the layer stays hidden: this platform can't raise a Tool window)
            splash.open_into(window)
            self.assertTrue(pump_until(lambda: sent))
            splash._relay.tell(("opening",))
            self.assertTrue(pump_until(lambda: opacity(window) == 1.0))
        self.assertEqual(
            [level for _, level in layer.opacities], [level for _, level in window.opacities]
        )
        self.assertEqual(opacity(layer), 1.0)

    def test_the_hand_over_is_chosen_only_while_the_overlay_draws_near_enough(self):
        far = (GEOMETRY[0] + splash_helper.GLIDE_MAX_PX + 1, GEOMETRY[1])
        cases = (
            ("drawing on the splash's loop", dict(), "offscreen", False, "handover"),
            ("not drawing", dict(drawing=False), "offscreen", False, "open"),
            ("drawing on another loop", dict(origin=ORIGIN + 1), "offscreen", False, "open"),
            ("farther than a short glide", dict(at=far), "offscreen", False, "open"),
            ("less motion asked for", dict(), "offscreen", True, "open"),
            ("Wayland", dict(), "wayland", False, "open"),
        )
        for case, overlay_args, platform_name, reduced, chosen in cases:
            with (
                self.subTest(case),
                mock.patch.object(splash_helper, "_platform_name", return_value=platform_name),
                mock.patch.object(OpeningMark, "reduced_motion", return_value=reduced),
            ):
                window = self.window()
                splash, sent = unwatched(window)
                splash._origin = ORIGIN
                overlay = FakeOverlay(window, **overlay_args)
                window.show()
                splash.open_into(window, overlay)
                self.assertEqual(overlay.holds, [(True, None)])  # unseen from now
                self.assertEqual(overlay.loop_origin(), ORIGIN)  # on the splash's loop
                self.assertFalse(overlay.loop_held())  # (not lit from a still)
                self.assertTrue(pump_until(lambda: sent))
                self.assertEqual(names(sent)[0], chosen)
                if chosen == "handover":
                    self.assertEqual(sent[0][0][1], (*NEAR, STAGE_PX, STAGE_PX))
                    continue
                # One mark at a time: the player's shows once the splash's has gone.
                pump(0.1)
                self.assertEqual(overlay.holds, [(True, None)])
                splash._relay.tell(("ended",))
                self.assertTrue(pump_until(lambda: len(overlay.holds) == 2))
                self.assertFalse(overlay.holds[1][0])
                self.assertEqual(overlay.fades, [True])  # it fades in: the splash's has gone
                self.assertIsNone(overlay.loop_origin())  # a later file's from its start

    def test_under_reduced_motion_it_opens_and_never_glides(self):
        # As the system asks (no mock): the mark stays where it is, and its open is plain.
        pin_motion(self, reduced=True)
        window = self.window()
        splash, sent = unwatched(window)
        splash._origin = ORIGIN
        overlay = FakeOverlay(window)
        window.show()
        splash.open_into(window, overlay)
        self.assertTrue(pump_until(lambda: sent))
        self.assertEqual(names(sent), ["open"])

    def test_the_players_mark_is_held_lit_when_the_splash_came_from_a_still(self):
        window = self.window()
        splash, sent = unwatched(window)
        splash._origin, splash._lit = ORIGIN, True
        overlay = FakeOverlay(window)
        window.show()
        splash.open_into(window, overlay)
        self.assertTrue(overlay.loop_held())
        splash._let_go()
        self.assertFalse(overlay.loop_held())  # a later file's draws as before

    def test_a_splash_dropped_with_its_fallback_due_still_fades_the_window(self):
        # PySide 6.11's singleShot keeps no reference to a bound method's object: once the
        # Splash went, its fallback ran on whatever took its memory (CI: "'_SubTest' object
        # has no attribute '_fade_step'"), and the window never faded in.
        window = self.window()
        splash, sent = unwatched(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
        window.show()
        splash.open_into(window)
        self.assertTrue(pump_until(lambda: sent))
        self.assertEqual(names(sent), ["open"])
        del splash
        gc.collect()
        litter = [Litter() for _ in range(5000)]  # the memory taken again
        self.assertTrue(pump_until(lambda: opacity(window) == 1.0))
        self.assertTrue(litter)

    def test_the_player_mark_shows_only_after_the_fade(self):
        window = self.window()
        splash, sent = unwatched(window)
        splash._origin = ORIGIN
        overlay = FakeOverlay(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            window.show()
            splash.open_into(window, overlay)
            self.assertTrue(pump_until(lambda: sent))
            self.assertEqual(names(sent), ["handover"])
            # the window fades at once, as the mark glides
            self.assertTrue(pump_until(lambda: rising(window) is not None, 2.0))
            splash._relay.tell(("arrived", sent[0][0][1]))  # the helper's glide reached it
            self.assertTrue(pump_until(lambda: "quit" in names(sent)))
        self.assertEqual(overlay.holds, [(True, 0.0), (False, 1.0)])  # shown once faded in
        self.assertEqual(overlay.fades, [False])  # at once: the splash's stands on it
        self.assertIsNone(overlay.loop_origin())  # a later file's draws from its start
        self.assertEqual(names(sent), ["handover", "bare", "quit"])  # then the splash goes
        # the splash drops its light only once the player's mark has painted: never neither
        self.assertTrue(overlay.mark.painted)
        self.assertGreater(sent[1][2], overlay.mark.painted[0])
        self.assertGreaterEqual(
            sent[2][2] - sent[1][2], splash_helper.HANDOVER_OVERLAP_MS / 1000 - 0.01
        )

    def test_a_player_mark_that_never_paints_still_lets_the_splash_go(self):
        window = self.window()
        splash, sent = unwatched(window)
        splash._origin = ORIGIN
        overlay = FakeOverlay(window)
        overlay.mark.setVisible = lambda visible: None  # it never shows, so it never paints
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            window.show()
            splash.open_into(window, overlay)
            self.assertTrue(pump_until(lambda: sent))
            splash._relay.tell(("arrived", sent[0][0][1]))
            self.assertTrue(pump_until(lambda: "quit" in names(sent)))
        self.assertEqual(names(sent), ["handover", "bare", "quit"])
        waited = sent[1][2] - window.opacities[-1][0]
        self.assertGreaterEqual(waited, splash_helper.PAINT_WAIT_MS / 1000 - 0.01)

    def test_the_swap_waits_for_the_splash_to_stand_on_the_players_mark(self):
        window = self.window()
        splash, sent = unwatched(window)
        splash._origin = ORIGIN
        overlay = FakeOverlay(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            window.show()
            splash.open_into(window, overlay)
            self.assertTrue(pump_until(lambda: opacity(window) == 1.0))
            pump(0.2)
            self.assertEqual(overlay.holds, [(True, 0.0)])  # faded in, but still gliding
            self.assertEqual(names(sent), ["handover"])
            splash._relay.tell(("arrived", sent[0][0][1]))
            self.assertTrue(pump_until(lambda: "quit" in names(sent)))
        self.assertEqual(overlay.holds, [(True, 0.0), (False, 1.0)])

    def test_the_splash_follows_a_player_mark_that_moves(self):
        # The window manager places the window after it is on screen: the player's mark (its
        # OverlayHost) moves with it, and the splash glides on to where it is now.
        window = self.window()
        splash, sent = unwatched(window)
        splash._origin = ORIGIN
        overlay = FakeOverlay(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            window.show()
            splash.open_into(window, overlay)
            self.assertTrue(pump_until(lambda: rising(window) is not None))
            moved = (NEAR[0], NEAR[1] + 30)
            overlay.mark.move(*moved)
            self.assertTrue(pump_until(lambda: len(sent) == 2))
            self.assertEqual(sent[1][0], ("handover", (*moved, STAGE_PX, STAGE_PX)))
            splash._relay.tell(("arrived", sent[0][0][1]))  # the first glide's end: not there
            self.assertTrue(pump_until(lambda: opacity(window) == 1.0))
            pump(0.2)
            self.assertEqual(names(sent), ["handover", "handover"])
            splash._relay.tell(("arrived", sent[1][0][1]))
            self.assertTrue(pump_until(lambda: "quit" in names(sent)))
        self.assertEqual(names(sent), ["handover", "handover", "bare", "quit"])

    def test_a_player_mark_that_moves_too_far_has_the_splash_open_out(self):
        window = self.window()
        splash, sent = unwatched(window)
        splash._origin = ORIGIN
        overlay = FakeOverlay(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            window.show()
            splash.open_into(window, overlay)
            self.assertTrue(pump_until(lambda: rising(window) is not None))
            overlay.mark.move(GEOMETRY[0] + splash_helper.GLIDE_MAX_PX + 1, GEOMETRY[1])
            self.assertTrue(pump_until(lambda: len(sent) == 2))
            self.assertTrue(pump_until(lambda: opacity(window) == 1.0))
        self.assertEqual(names(sent), ["handover", "open"])
        self.assertEqual(overlay.holds, [(True, 0.0)])  # unseen until the splash has gone

    def test_an_overlay_that_closes_during_the_glide_falls_back_to_the_open(self):
        window = self.window()
        splash, sent = unwatched(window)
        splash._origin = ORIGIN
        overlay = FakeOverlay(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            window.show()
            splash.open_into(window, overlay)
            self.assertTrue(pump_until(lambda: rising(window) is not None))
            overlay.drawing = False  # the film's picture showed
            self.assertTrue(pump_until(lambda: opacity(window) == 1.0))
            pump(0.1)
        self.assertEqual(names(sent), ["handover", "open"])  # the splash's mark opens out
        self.assertEqual(overlay.holds, [(True, 0.0)])  # the player's: unseen until it ends
        splash._relay.tell(("ended",))
        self.assertTrue(pump_until(lambda: len(overlay.holds) == 2))

    def test_a_splash_that_ends_lets_the_player_mark_show(self):
        window = self.window()
        splash, sent = unwatched(window)
        splash._origin = ORIGIN
        overlay = FakeOverlay(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            window.show()
            splash.open_into(window, overlay)
            self.assertTrue(pump_until(lambda: sent))
            splash._relay.tell(("ended",))  # it crashed, or was killed
            self.assertTrue(pump_until(lambda: len(overlay.holds) == 2))
            self.assertFalse(overlay.holds[-1][0])
            self.assertTrue(pump_until(lambda: opacity(window) == 1.0))
        self.assertEqual(names(sent), ["handover"])

    def test_a_window_that_doesnt_fade_still_gets_the_open(self):
        window = self.window()
        splash, sent = unwatched(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=False):
            splash.before_show(window)
            window.show()
            splash.open_into(window)
            self.assertTrue(pump_until(lambda: sent))
        self.assertEqual(names(sent), ["open"])
        self.assertEqual(window.opacities, [])

    def test_a_close_before_the_open_still_fades_the_window_in(self):
        window = self.window()
        splash, sent = unwatched(window)
        with mock.patch.object(splash_helper, "_fades_windows", return_value=True):
            splash.before_show(window)
            window.show()
            splash.open_into(window)
            app.processEvents()
            splash.close()
            self.assertTrue(pump_until(lambda: opacity(window) == 1.0))  # never left at 0
        self.assertEqual(names(sent), ["quit"])

    def test_the_window_wait_counts_from_the_loops_first_turn(self):
        # main()'s work after show() (open_paths, DDE, the control port) runs before the loop:
        # the wait for a window that never says it is exposed starts only then.
        window = QWidget()
        self.addCleanup(window.deleteLater)
        window.create()  # a native window that is never shown: no expose comes
        called = []
        splash_helper._WhenDrawn(window, lambda: called.append(time.monotonic()))
        time.sleep(0.7)  # longer than WINDOW_WAIT_MS
        first_turn = time.monotonic()
        self.assertTrue(pump_until(lambda: called))
        self.assertGreaterEqual(called[0] - first_turn, splash_helper.WINDOW_WAIT_MS / 1000 - 0.05)

    def test_stop_ends_the_helper_at_once_without_the_watcher(self):
        # QiTV's crash: the watcher may never run again (a windowed Windows build's error
        # dialog holds Python's lock), so the helper ends from here, whatever it does.
        shown = threading.Event()
        with mock.patch.object(splash_helper, "run_splash", stubborn):
            splash = Splash(on_shown=shown.set)
            self.assertTrue(splash.start(QRect(*GEOMETRY)))
            self.addCleanup(splash._process.kill)
            self.assertTrue(shown.wait(SPAWN_S))
            with mock.patch.object(splash, "_commands") as commands:
                splash.stop()
            commands.put.assert_not_called()  # nothing for the watcher to send
            # (a stubborn helper sleeps 60 s; its end read from its sentinel: the watcher may
            # reap it first)
            self.assertTrue(wait([splash._process.sentinel], WAIT_S))
        with self.assertNoLogs("ui.splash_helper", "WARNING"):  # QiTV's own end of it
            splash._watcher.join(KILL_WAIT_S)

    def test_stop_never_raises(self):
        Splash().stop()  # never started
        splash = Splash()
        splash._process = mock.Mock()
        splash._process.terminate.side_effect = OSError("no such process")
        splash.stop()
        splash._process.terminate.assert_called_once_with()

    def test_qitvs_crash_ends_the_splash(self):
        # main() hands Splash.stop to its crash hook: an error nothing handled ends the
        # helper as it is logged.
        import main

        self.addCleanup(setattr, sys, "excepthook", sys.excepthook)
        logged = []
        sys.excepthook = lambda *exc: logged.append(exc[0])
        shown = threading.Event()
        with mock.patch.object(splash_helper, "run_splash", stubborn):
            splash = Splash(on_shown=shown.set)
            self.assertTrue(splash.start(QRect(*GEOMETRY)))
            self.addCleanup(splash._process.kill)
            self.assertTrue(shown.wait(SPAWN_S))
            main.close_on_crash(splash.stop)
            sys.excepthook(RuntimeError, RuntimeError("the window failed"), None)
            self.assertTrue(wait([splash._process.sentinel], WAIT_S))
        self.assertEqual(logged, [RuntimeError])
        splash._watcher.join(KILL_WAIT_S)

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
                splash._watcher.join(WAIT_S)
        self.assertEqual(len(logs.output), 1)  # killed by QiTV: no exit code reported
        self.assertIn("did not end", logs.output[0])
        self.assertFalse(splash._watcher.is_alive())
        self.assertGreaterEqual(time.monotonic() - sent, 0.5)
        self.assertNotEqual(splash._process.exitcode, 0)  # killed

    def test_a_helper_that_cannot_leave_the_dock_says_so_in_qitvs_log(self):
        # Its own process has no log handler: a Dock icon would leave no trace otherwise.
        shown = threading.Event()
        with mock.patch.object(splash_helper, "run_splash", dockless):
            splash = Splash(on_shown=shown.set)
            with self.assertLogs("ui.splash_helper", "WARNING") as logs:
                self.assertTrue(splash.start(QRect(*GEOMETRY)))
                self.addCleanup(splash._process.kill)
                self.assertTrue(shown.wait(SPAWN_S))
                splash.close()
                splash._watcher.join(KILL_WAIT_S)
        self.assertEqual(
            logs.output,
            [
                "WARNING:ui.splash_helper:The splash could not leave the Dock (OSError): it may "
                "show an icon there while QiTV starts."
            ],
        )
        self.assertEqual(splash._process.exitcode, 0)  # it showed, then quit

    def test_a_helper_that_fails_says_so_in_qitvs_log(self):
        # A packaged QiTV has no stderr: the helper's own lines are lost, its exit code isn't.
        with mock.patch.object(splash_helper, "run_splash", failing):
            splash = Splash()
            with self.assertLogs("ui.splash_helper", "WARNING") as logs:
                self.assertTrue(splash.start(QRect(*GEOMETRY)))
                self.addCleanup(splash._process.kill)
                splash._watcher.join(SPAWN_S)
        self.assertFalse(splash._watcher.is_alive())
        self.assertEqual(
            logs.output, ["WARNING:ui.splash_helper:The splash ended with exit code 3."]
        )

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
        pin_motion(self)
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
        self.assertTrue(pump_until(self.conn.poll))
        self.assertEqual(self.conn.recv(), ("shown", self.window.origin))
        self.assertIsNotNone(self.window.origin)
        self.assertLess(self.window.frame().t, 0.25)  # its loop begins: the line draws
        self.assertEqual(self.window.frame().light, 1.0)  # with all of its light
        self.conn.send(("quit",))
        self.assertTrue(pump_until(lambda: self.ended))
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.ended, [1])

    def test_open_ends_it_through_the_marks_own_end(self):
        # Its finish's `then` quits it: the mark's own end runs first (its timer stops, it is
        # done), then the splash's.
        self.assertTrue(self.window.begin())
        self.conn.send(("open", (0, 0, 800, 600)))
        self.assertTrue(pump_until(lambda: self.ended))
        self.assertTrue(self.window._done)  # OpeningMark._end ran
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.conn.recv()[0], "shown")
        self.assertEqual(self.conn.recv(), ("opening",))

    def test_a_handover_glides_onto_the_players_mark_and_quit_ends_it(self):
        self.assertTrue(self.window.begin())
        target = QRect(*NEAR, STAGE_PX, STAGE_PX)
        self.conn.send(("handover", (*NEAR, STAGE_PX, STAGE_PX)))
        self.assertTrue(pump_until(lambda: self.window.geometry() == target))
        self.assertTrue(pump_until(self.conn.poll))
        self.assertEqual(self.conn.recv()[0], "shown")
        self.assertTrue(pump_until(self.conn.poll))
        self.assertEqual(self.conn.recv(), ("arrived", (*NEAR, STAGE_PX, STAGE_PX)))
        self.assertTrue(self.window.isVisible())  # it loops on there
        frame = self.window.frame()
        self.assertEqual((frame.opening, frame.fade), (0.0, 1.0))  # no grow, no fade
        pump(0.1)
        self.assertFalse(self.window.bare)  # its light until the player's mark shows
        self.conn.send(("bare",))
        self.assertTrue(pump_until(lambda: self.window.bare))
        self.assertEqual(self.ended, [])
        self.conn.send(("quit",))
        self.assertTrue(pump_until(lambda: self.ended))
        self.assertFalse(self.window.isVisible())

    def test_a_second_handover_glides_on_from_where_it_is(self):
        self.assertTrue(self.window.begin())
        moved = (NEAR[0], NEAR[1] + 30)
        self.conn.send(("handover", (*NEAR, STAGE_PX, STAGE_PX)))
        pump(0.1)  # under way
        self.conn.send(("handover", (*moved, STAGE_PX, STAGE_PX)))
        self.assertTrue(pump_until(lambda: self.window.geometry() == QRect(*moved, 240, 240)))
        pump(0.1)
        received = []
        while self.conn.poll():
            received.append(self.conn.recv())
        arrived = [message for message in received if message[0] == "arrived"]
        self.assertEqual(arrived, [("arrived", (*moved, STAGE_PX, STAGE_PX))])  # the last only

    def test_bare_mid_glide_waits_for_the_glide(self):
        self.assertTrue(self.window.begin())
        target = QRect(*NEAR, STAGE_PX, STAGE_PX)
        self.conn.send(("handover", (*NEAR, STAGE_PX, STAGE_PX)))
        self.conn.send(("bare",))
        self.assertTrue(pump_until(lambda: self.window.bare))
        self.assertEqual(self.window.geometry(), target)  # bare only once it stands there

    def test_what_came_before_it_shows_ends_it_unseen(self):
        for message in (
            ("quit",),
            ("open", (0, 0, 800, 600)),
            ("handover", (0, 0, STAGE_PX, STAGE_PX)),
        ):
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

    def test_a_lit_helper_starts_on_the_stills_frame(self):
        # Over the bootloader's splash (the build's still: the outline drawn whole, unfilled,
        # no light): its first frame is that frame, which then fills and blooms.
        conn, child = multiprocessing.Pipe(duplex=True)
        self.addCleanup(conn.close)
        self.addCleanup(child.close)
        window = splash_helper._SplashWindow(child, GEOMETRY, lambda: None, lit=True)
        self.addCleanup(window.deleteLater)
        before = time.monotonic()
        self.assertTrue(window.begin())
        after = time.monotonic()
        self.assertGreaterEqual(window.origin, before)  # held lit from now: no shifted loop
        self.assertLessEqual(window.origin, after)
        frame = window.frame()
        self.assertAlmostEqual(frame.t, DRAWN_AT, delta=0.02)
        self.assertEqual((frame.opening, frame.fade), (0.0, 1.0))
        # The still is its bare tile and glyph: its light and shadow come in as it takes over.
        self.assertLess(frame.light, 0.2)
        self.assertTrue(pump_until(conn.poll))
        self.assertEqual(conn.recv(), ("shown", window.origin))
        light_in = splash_helper.LIGHT_IN_MS / 1000
        self.assertTrue(pump_until(lambda: window.frame().light == 1.0, timeout=10 * light_in))

    def test_a_lit_helper_never_shows_the_glyph_out_before_it_opens(self):
        # The owner's "opens, closes and opens": a mark lit from the first moment stays lit.
        conn, child = multiprocessing.Pipe(duplex=True)
        self.addCleanup(conn.close)
        self.addCleanup(child.close)
        window = splash_helper._SplashWindow(child, GEOMETRY, lambda: None, lit=True)
        self.addCleanup(window.deleteLater)
        self.assertTrue(window.begin())
        origin = window.origin
        for step in range(0, 3600 * 20):  # an hour of loop time, by the window's own clock
            frame = window._frame(origin + step * 0.05)
            self.assertFalse(frame.still)
            self.assertGreaterEqual(frame.t, FILLED_AT if step * 0.05 >= HOLD_FILL_S else DRAWN_AT)
            self.assertLessEqual(frame.t, LIT_UNTIL)
        # "open" from the held state, once filled: nothing to catch up, so "opening" goes back
        # at once
        pump(HOLD_FILL_S + 0.05)
        conn.send(("open", (0, 0, 800, 600)))
        self.assertTrue(pump_until(lambda: window._finish is not None))
        self.assertEqual(window._finish.catch_up_s, 0.0)
        self.assertFalse(window._finish.plain)
        heard = []

        def hear():
            while conn.poll():
                heard.append(conn.recv())
            return ("opening",) in heard

        self.assertTrue(pump_until(hear))

    def test_under_reduced_motion_a_lit_helper_holds_still(self):
        pin_motion(self, reduced=True)
        conn, child = multiprocessing.Pipe(duplex=True)
        self.addCleanup(conn.close)
        self.addCleanup(child.close)
        window = splash_helper._SplashWindow(child, GEOMETRY, lambda: None, lit=True)
        self.addCleanup(window.deleteLater)
        self.assertTrue(window.begin())
        for later in (0.0, 0.9, 7.0, 3000.0):
            frame = window._frame(window.origin + later)
            self.assertEqual((frame.t, frame.still), (STILL_T, True))

    def test_under_reduced_motion_it_holds_the_lit_frame_and_fades_plainly(self):
        pin_motion(self, reduced=True)
        conn, child = multiprocessing.Pipe(duplex=True)
        self.addCleanup(conn.close)
        self.addCleanup(child.close)
        ended = []
        window = splash_helper._SplashWindow(child, GEOMETRY, lambda: ended.append(1))
        self.addCleanup(window.deleteLater)
        self.assertTrue(window.begin())
        frame = window.frame()
        self.assertTrue(frame.still)
        self.assertEqual(frame.t, STILL_T)
        conn.send(("open", (0, 0, 800, 600)))
        self.assertTrue(pump_until(lambda: window._finish is not None))
        pump(OPEN_S / 2)
        frame = window.frame()
        self.assertEqual((frame.still, frame.opening), (True, 0.0))  # nothing grows
        self.assertLess(frame.fade, 1.0)
        self.assertTrue(pump_until(lambda: ended))

    def test_a_closed_pipe_ends_it(self):
        self.assertTrue(self.window.begin())
        self.conn.close()
        self.assertTrue(pump_until(lambda: self.ended))
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

    def test_a_mac_splash_that_cannot_leave_the_dock_tells_qitv_why(self):
        with (
            mock.patch.object(splash_helper, "_platform_name", return_value="cocoa"),
            mock.patch.object(macos_window, "_objc", side_effect=OSError("no objc")),
        ):
            self.assertTrue(self.window.begin())  # it shows all the same
        self.assertTrue(pump_until(self.conn.poll))
        self.assertEqual(self.conn.recv(), ("dock", "OSError"))  # before it shows
        self.assertTrue(pump_until(self.conn.poll))
        self.assertEqual(self.conn.recv()[0], "shown")

    def test_elsewhere_the_dock_is_left_alone(self):
        with (
            mock.patch.object(splash_helper, "_platform_name", return_value="xcb"),
            mock.patch.object(splash_helper, "_hide_from_dock") as hide,
        ):
            self.assertTrue(self.window.begin())
        hide.assert_not_called()


class DockTests(unittest.TestCase):
    def test_hide_from_dock_makes_the_helper_an_accessory_app(self):
        # [[NSApplication sharedApplication] setActivationPolicy:Accessory] (1)
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
            self.assertIsNone(splash_helper._hide_from_dock())  # done: nothing to say
        objc.objc_getClass.assert_called_once_with(b"NSApplication")
        self.assertEqual(
            sent,
            [
                (0x1000, b"sharedApplication", ctypes.c_void_p, ()),
                (0x2000, b"setActivationPolicy:", ctypes.c_bool, ((ctypes.c_long, 1),)),
            ],
        )

    def test_a_failed_call_is_never_a_reason_not_to_show(self):
        # It says why instead (the helper tells QiTV's log: it has none of its own).
        with mock.patch.object(macos_window, "_objc", side_effect=OSError("no objc")):
            self.assertEqual(splash_helper._hide_from_dock(), "OSError")
        objc = mock.Mock()
        objc.objc_getClass.return_value = 0x1000
        with (
            mock.patch.object(macos_window, "_objc", return_value=objc),
            mock.patch.object(macos_window, "_send", return_value=False),
        ):
            self.assertEqual(splash_helper._hide_from_dock(), "no NSApplication")
        with (
            mock.patch.object(macos_window, "_objc", return_value=objc),
            mock.patch.object(
                macos_window,
                "_send",
                side_effect=lambda receiver, selector, *args: selector == b"sharedApplication",
            ),
        ):
            self.assertEqual(splash_helper._hide_from_dock(), "the policy was refused")


class MainTests(unittest.TestCase):
    """Where main() starts the splash, tells it to open, and ends it."""

    def body(self):
        return (REPO / "main.py").read_text(encoding="utf-8").split("def main()", 1)[1]

    def test_main_starts_splash_before_heavy_imports(self):
        body = self.body()
        start = body.index("splash.start(lit=bootloader.shows)")
        self.assertLess(body.index("app = QiTVApplication(sys.argv)"), start)
        self.assertLess(start, body.index("instance = claim("))  # it shows while claim waits
        self.assertLess(start, body.index("from ui.tv_window import"))
        self.assertLess(start, body.index("import qdarktheme"))
        # a start that handed over ends it
        handed_over = body[body.index("if handed_over:") : body.index("return 0")]
        self.assertIn("splash.close()", handed_over)
        # the window starts transparent (over a splash on screen), then the splash opens into
        # it; PyInstaller's still stays until the window is drawn
        show = body.index("window.show()")
        self.assertLess(body.index("splash.before_show(window)"), show)
        self.assertLess(show, body.index("splash.open_into(window, window.opening_overlay)"))
        self.assertLess(show, body.index('when_drawn(window, lambda: bootloader.close("'))
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
        self.assertIn("with_splash = wants_splash(sys.argv, os.environ)", body)
        self.assertIn("if not (with_splash and splash.start(lit=bootloader.shows)):", body)


if __name__ == "__main__":
    unittest.main()
