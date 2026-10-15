import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import Optional
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

from PySide6.QtCore import (
    QLoggingCategory,
    QMargins,
    QPoint,
    QRect,
    Qt,
    QTimer,
    qInstallMessageHandler,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from config_manager import ConfigManager
from epg_manager import EpgManager
from image_manager import ImageManager
import main as qitv_main
from options import OptionsDialog
from provider_manager import ProviderManager
from services.playback import NO_VLC_MESSAGE, PlaybackState, create_instance
from services.thread_cleanup import has_pending_threads
from tests.fixtures.local_provider import channel_urls, seed_channels, write_local_provider
from ui.overlay_host import OverlayHost
from ui.overlays.drawer import ERROR_TEXT
from ui.tv_controller import WELCOME_TEXT
from ui.tv_window import (
    CLOSE_RETRY_MS,
    CLOSING_TEXT,
    TVWindow,
    _exit_now,
    fit_to_area,
    frame_margins as frame_margins_of,
)

INSTANCE = create_instance(["--vout=dummy", "--aout=dummy", "--quiet"])
HOST_METHODS = ("save_config", "set_provider", "refresh_channels", "refresh_content_list_size")
HOST_ATTRIBUTES = ("config_manager", "provider_manager", "epg_manager", "image_manager")
_OFFSCREEN_NOISE = "This plugin does not support"


def wait_until(predicate, timeout_ms=10000):
    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(20)
        waited += 20
    return predicate()


class WindowCase(unittest.TestCase):
    """A TVWindow over the local fixture provider in a temporary QITV_CONFIG_DIR.

    Without libVLC (NullSession) unless a subclass passes a real instance.
    """

    vlc_instance = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        QLoggingCategory.setFilterRules("qt.qpa.*=false")
        previous = None

        def quiet(mode, context, message):  # the offscreen platform's unsupported features
            if message.startswith(_OFFSCREEN_NOISE):
                return
            if previous is not None:
                previous(mode, context, message)
            else:
                sys.stderr.write(f"{message}\n")

        previous = qInstallMessageHandler(quiet)
        cls.addClassCleanup(qInstallMessageHandler, previous)

    def provider_list(self) -> Optional[list[dict]]:
        return None  # the fixture provider

    def make_window(self) -> TVWindow:
        args = (self.app, self.config, self.provider_manager, self.image_manager, self.epg_manager)
        if self.vlc_instance is not None:
            return TVWindow(*args, vlc_instance=self.vlc_instance)
        with (
            mock.patch("ui.tv_window.create_instance", return_value=None),
            self.assertLogs("ui.tv_window", "WARNING"),
        ):
            return TVWindow(*args)

    def setUp(self):
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.config_dir = directory.name
        env = mock.patch.dict(os.environ, {"QITV_CONFIG_DIR": self.config_dir})
        env.start()
        self.addCleanup(env.stop)
        providers = self.provider_list()
        provider = write_local_provider(self.config_dir, providers)
        self.config = ConfigManager()
        self.prepare_config()
        self.provider_manager = ProviderManager(self.config)
        self.addCleanup(self.provider_manager._cache_writer.shutdown, wait=True)
        if providers is None:
            seed_channels(self.provider_manager, provider)
        self.image_manager = ImageManager(self.config, 10 * 1024 * 1024)
        self.epg_manager = EpgManager(self.config, self.provider_manager)
        self.window = self.make_window()
        self.toasts: list[tuple[str, str]] = []
        toast = self.window.overlays.toast
        show_message = toast.show_message

        def spy(text, kind="info", ms=3000):
            self.toasts.append((text, kind))
            show_message(text, kind, ms)

        toast.show_message = spy
        self.addCleanup(self.close_window)

    def prepare_config(self) -> None:
        """Change the configuration before the window is built."""

    def close_window(self):
        # One close(); the window's retry timer runs the steps (stop, threads, release). The
        # timer check also covers windows that were never shown.
        self.window.close()
        self.assertTrue(
            wait_until(
                lambda: not self.window.isVisible() and not self.window.close_retry.isActive()
            )
        )
        self.assertFalse(has_pending_threads())

    def show(self):
        self.window.show()
        self.assertTrue(QTest.qWaitForWindowExposed(self.window))
        self.window.activateWindow()
        self.assertTrue(QTest.qWaitForWindowActive(self.window))

    def wait_for_drawer(self):
        drawer = self.window.overlays.drawer
        self.assertTrue(wait_until(drawer.isVisible))
        return drawer

    def open_first_channel(self):
        self.show()
        drawer = self.wait_for_drawer()  # no last channel: the drawer opens on the groups
        QTest.keyClick(self.window, Qt.Key.Key_Return)  # into the highlighted group (All)

        def on_an_item():
            node = drawer.highlighted()
            return node is not None and node.layer == "item"

        self.assertTrue(wait_until(on_an_item))
        self.assertEqual(drawer.highlighted().title, "Channel 1")
        QTest.keyClick(self.window, Qt.Key.Key_Return)

    def menu_actions(self):
        return {a.text(): a for m in self.window.menuBar().actions() for a in m.menu().actions()}

    def mini(self):
        QTest.keyClick(self.window, Qt.Key.Key_P, Qt.KeyboardModifier.AltModifier)


class TVWindowTests(WindowCase):
    def test_constructs_and_shows_with_the_overlay_host(self):
        self.show()
        self.assertIsInstance(self.window.host, OverlayHost)
        self.assertIs(self.window.centralWidget(), self.window.surface)

    def test_hosts_the_options_dialog(self):
        for name in HOST_METHODS + HOST_ATTRIBUTES:
            self.assertTrue(hasattr(self.window, name), name)

    def test_close_saves_the_window_geometry(self):
        self.show()
        self.window.setGeometry(150, 120, 960, 540)
        self.assertTrue(wait_until(lambda: self.window.geometry().width() == 960))
        self.close_window()
        saved = ConfigManager().config["window_positions"]["tv_window"]
        self.assertEqual(saved, {"x": 150, "y": 120, "width": 960, "height": 540})

    def test_without_libvlc_playing_explains_why(self):
        self.open_first_channel()
        self.assertTrue(
            wait_until(
                lambda: any(
                    kind == "error" and NO_VLC_MESSAGE in text for text, kind in self.toasts
                )
            )
        )

    def test_notices_are_toasted_once_by_the_started_controller(self):
        self.show()
        self.wait_for_drawer()
        text = "Couldn't refresh Live TV; showing saved channels"
        self.window.nav.notice.emit(text)
        self.assertEqual(self.toasts.count((text, "info")), 1)

    def test_the_guide_is_refreshed_every_two_hours(self):
        timer = self.window.epg_refresh
        self.assertEqual(timer.interval(), 2 * 60 * 60 * 1000)
        self.show()
        self.assertTrue(wait_until(timer.isActive))
        with mock.patch.object(self.window.catalog, "refresh_epg") as refresh_epg:
            timer.timeout.emit()
        refresh_epg.assert_called_once_with()
        self.close_window()
        self.assertFalse(timer.isActive())

    def test_the_transport_covers_the_overlay_host(self):
        self.show()
        drawer = self.wait_for_drawer()
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertFalse(drawer.isVisible())
        host, transport = self.window.host, self.window.overlays.transport
        transport.show_vod("Film", "")
        self.assertTrue(wait_until(host.isVisible))
        self.assertEqual(host.size(), self.window.surface.size())
        self.assertEqual(transport.geometry(), host.rect())
        self.window.resize(self.window.width() - 120, self.window.height() - 80)
        self.assertTrue(wait_until(lambda: host.size() == self.window.surface.size()))
        self.assertEqual(transport.geometry(), host.rect())

    def test_video_is_not_bound_without_native_window_ids(self):
        # libVLC crashes drawing into the offscreen platform's fake window ids.
        with mock.patch.object(self.window.session, "set_window") as set_window:
            self.show()
            self.wait_for_drawer()
        set_window.assert_not_called()

    # ---- menus and keys -----------------------------------------------------------------------

    def test_menus_list_the_tv_actions_with_their_shortcuts(self):
        menus = {action.text(): action.menu() for action in self.window.menuBar().actions()}
        self.assertEqual(list(menus), ["QiTV", "View", "Playback"])

        def shortcuts(menu):
            return {a.text(): a.shortcut().toString() for a in menu.actions()}

        self.assertEqual(shortcuts(menus["QiTV"]), {"Settings…": "Ctrl+,", "Quit": "Ctrl+Q"})
        self.assertEqual(
            shortcuts(menus["View"]),
            {"Fullscreen": "F", "Channel list": "Return", "Mini player": "Alt+P"},
        )
        self.assertEqual(
            shortcuts(menus["Playback"]),
            {"Play/Pause": "Space", "Mute": "M", "Previous channel": "Backspace"},
        )
        # Only modifier combinations are live shortcuts; single keys are hints in the menu.
        contexts = {a.text(): a.shortcutContext() for m in menus.values() for a in m.actions()}
        live = {"Settings…", "Quit", "Mini player"}
        for text, context in contexts.items():
            expected = (
                Qt.ShortcutContext.ApplicationShortcut
                if text in live
                else Qt.ShortcutContext.WidgetShortcut
            )
            self.assertEqual(context, expected, text)

    def test_keys_reach_the_open_drawer_and_never_trigger_menu_actions(self):
        triggered = []
        for text, action in self.menu_actions().items():
            action.triggered.connect(lambda _checked=False, t=text: triggered.append(t))
        self.show()
        drawer = self.wait_for_drawer()
        self.assertEqual(drawer.highlighted().layer, "group")
        QTest.keyClick(self.window, Qt.Key.Key_Return)  # activates: into the group
        self.assertTrue(wait_until(lambda: drawer.highlighted().layer == "item"))
        self.assertEqual(triggered, [])  # not "Channel list" re-opening the drawer
        for key in (Qt.Key.Key_F, Qt.Key.Key_M, Qt.Key.Key_Space):
            QTest.keyClick(self.window, key)
        self.assertEqual(drawer.filter_text, "fm ")
        QTest.keyClick(self.window, Qt.Key.Key_Backspace)  # edits the filter: not "previous"
        self.assertEqual(drawer.filter_text, "fm")
        self.assertTrue(drawer.isVisible())
        self.assertFalse(self.window.isFullScreen())
        self.assertEqual(triggered, [])

    def test_f_and_the_menu_toggle_fullscreen_and_escape_leaves_it(self):
        self.show()
        drawer = self.wait_for_drawer()
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertFalse(drawer.isVisible())
        QTest.keyClick(self.window, Qt.Key.Key_F)
        self.assertTrue(wait_until(self.window.isFullScreen))
        QTest.keyClick(self.window, Qt.Key.Key_Escape)  # nothing open: leaves fullscreen
        self.assertTrue(wait_until(lambda: not self.window.isFullScreen()))
        actions = self.menu_actions()
        actions["Fullscreen"].trigger()
        self.assertTrue(wait_until(self.window.isFullScreen))
        actions["Channel list"].trigger()
        self.assertTrue(drawer.isVisible())

    # ---- mini player --------------------------------------------------------------------------

    def assert_mini(self):
        flags = self.window.windowFlags()
        self.assertTrue(flags & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)
        screen = self.window.screen()
        available = screen.availableGeometry()
        mini = self.window.geometry()
        self.assertEqual(mini.width(), round(screen.geometry().width() * 0.25))
        self.assertAlmostEqual(mini.height(), mini.width() * 9 / 16, delta=1)
        self.assertTrue(available.contains(mini))
        self.assertLessEqual(available.right() - mini.right(), 20)
        self.assertLessEqual(available.bottom() - mini.bottom(), 20)

    def assert_not_mini(self):
        self.assertFalse(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertFalse(self.window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

    def test_mini_player_shrinks_to_the_corner_and_restores(self):
        self.show()
        self.window.setGeometry(100, 100, 960, 540)
        self.assertTrue(wait_until(lambda: self.window.geometry().width() == 960))
        before = self.window.geometry()
        self.mini()
        self.assert_mini()
        self.mini()
        self.assert_not_mini()
        self.assertTrue(wait_until(lambda: self.window.geometry() == before))

    def test_mini_player_from_fullscreen_waits_for_the_fullscreen_exit(self):
        self.show()
        self.window.setGeometry(100, 100, 960, 540)
        self.assertTrue(wait_until(lambda: self.window.geometry().width() == 960))
        QTest.keyClick(self.window, Qt.Key.Key_F)
        self.assertTrue(wait_until(self.window.isFullScreen))
        self.mini()
        self.assertFalse(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(
            wait_until(lambda: self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        )
        self.assertFalse(self.window.isFullScreen())
        self.assert_mini()
        self.mini()
        self.assert_not_mini()
        self.assertTrue(wait_until(lambda: self.window.geometry().width() == 960))

    def test_mini_player_gives_a_maximized_window_back_maximized(self):
        self.show()
        self.window.showMaximized()
        self.assertTrue(wait_until(self.window.isMaximized))
        self.mini()
        self.assertFalse(self.window.isMaximized())
        self.assert_mini()
        self.mini()
        self.assertTrue(wait_until(self.window.isMaximized))
        self.assert_not_mini()

    def test_mini_player_moves_when_the_video_is_dragged(self):
        self.show()
        drawer = self.wait_for_drawer()
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.mini()
        self.assert_mini()
        start = self.window.pos()
        surface = self.window.surface
        QTest.mousePress(surface, Qt.MouseButton.LeftButton, pos=QPoint(40, 40))
        QTest.mouseMove(surface, QPoint(10, 20))
        QTest.mouseRelease(surface, Qt.MouseButton.LeftButton, pos=QPoint(10, 20))
        self.assertEqual(self.window.pos(), start + QPoint(-30, -20))
        QTest.qWait(QApplication.doubleClickInterval() + 100)
        self.assertFalse(drawer.isVisible())  # the drag is not a click on the video

    def press_and_hold(self):
        """Press on the video and hold past the double-click interval (the click is due)."""
        QTest.mousePress(self.window.surface, Qt.MouseButton.LeftButton, pos=QPoint(40, 40))
        QTest.qWait(QApplication.doubleClickInterval() + 100)

    def test_mini_player_drag_after_a_long_hold_is_not_a_click(self):
        self.show()
        drawer = self.wait_for_drawer()
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.mini()
        start = self.window.pos()
        self.press_and_hold()
        QTest.mouseMove(self.window.surface, QPoint(10, 20))
        QTest.mouseRelease(self.window.surface, Qt.MouseButton.LeftButton, pos=QPoint(10, 20))
        self.assertEqual(self.window.pos(), start + QPoint(-30, -20))
        QTest.qWait(100)
        self.assertFalse(drawer.isVisible())

    def test_mini_player_long_press_without_a_drag_is_a_click(self):
        self.show()
        drawer = self.wait_for_drawer()
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.mini()
        self.press_and_hold()
        self.assertFalse(drawer.isVisible())  # not while the button is still down
        QTest.mouseRelease(self.window.surface, Qt.MouseButton.LeftButton, pos=QPoint(40, 40))
        self.assertTrue(wait_until(drawer.isVisible, 2000))

    def test_the_first_click_after_the_mini_player_is_not_swallowed(self):
        self.show()
        drawer = self.wait_for_drawer()
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.mini()
        self.press_and_hold()
        QTest.mouseMove(self.window.surface, QPoint(10, 20))
        QTest.mouseRelease(self.window.surface, Qt.MouseButton.LeftButton, pos=QPoint(10, 20))
        self.mini()  # back to the normal window
        self.assert_not_mini()
        drawer.close_drawer()
        self.window.host.clicked.emit()  # a click on the video around an overlay
        self.assertTrue(drawer.isVisible())

    # ---- closing ------------------------------------------------------------------------------

    def test_close_stops_the_catalog_before_new_jobs_can_start(self):
        # A provider setup still running when the close begins must not start an EPG load.
        catalog = self.window.catalog
        gate = threading.Event()
        self.addCleanup(gate.set)
        create_setup = catalog.factory.provider_setup

        def held_setup(*args):
            worker = create_setup(*args)
            run = worker.run
            worker.run = lambda: (gate.wait(5), run())
            return worker

        started = []
        run_worker = catalog.runner.run_worker

        def record(*args, **kwargs):
            started.append(self.window.isVisible() and closing)
            return run_worker(*args, **kwargs)

        closing = False
        with (
            mock.patch.object(catalog.factory, "provider_setup", side_effect=held_setup),
            mock.patch.object(catalog.runner, "run_worker", side_effect=record),
        ):
            self.show()
            self.assertTrue(wait_until(lambda: len(started) == 1))  # the setup
            closing = True
            self.assertFalse(self.window.close())
            gate.set()
            self.close_window()
        self.assertEqual(started, [False])

    def test_close_stops_then_saves_and_releases_vlc_while_still_visible(self):
        # On macOS libVLC removes its video view on the run loop after a stop; closing the
        # window in the same call crashed. The indexes are saved after the thread wait.
        self.show()
        self.wait_for_drawer()
        window, calls = self.window, []
        waits = iter([True, True, False])

        def pending():
            calls.append("wait")
            return next(waits, False)

        with (
            mock.patch.object(window.catalog, "shutdown", side_effect=lambda: calls.append("x")),
            mock.patch.object(window.session, "stop", side_effect=lambda: calls.append("stop")),
            mock.patch.object(
                window.session,
                "release",
                side_effect=lambda: calls.append(("release", window.isVisible())),
            ),
            mock.patch.object(
                self.image_manager, "save_index", side_effect=lambda: calls.append("images")
            ),
            mock.patch.object(
                self.epg_manager, "save_index", side_effect=lambda: calls.append("epg")
            ),
            mock.patch("ui.tv_window.has_pending_threads", side_effect=pending),
        ):
            self.assertFalse(window.close())
            self.assertEqual(calls, ["x", "stop"])
            self.assertTrue(window.isVisible())
            self.assertTrue(wait_until(lambda: not window.isVisible()))
        self.assertEqual(
            calls,
            ["x", "stop", "wait", "wait", "wait", "images", "epg", ("release", True), "wait"],
        )  # the last check is the final step's, just before the window closes

    def visible_when_shown(self):
        """Record, for each toast shown, whether one was already visible."""
        toast, seen = self.window.overlays.toast, []
        show_message = toast.show_message

        def record(text, kind="info", ms=3000):
            seen.append(toast.isVisible())
            show_message(text, kind, ms)

        toast.show_message = record
        return seen

    def test_closing_notice_stays_visible_for_the_whole_wait(self):
        self.show()
        self.wait_for_drawer()
        toast = self.window.overlays.toast
        waiting = [True]
        with mock.patch("ui.tv_window.has_pending_threads", side_effect=lambda: waiting[0]):
            self.assertFalse(self.window.close())
            self.assertTrue(wait_until(lambda: (CLOSING_TEXT, "info") in self.toasts, 3000))
            for _ in range(3):  # stays up while the wait lasts
                QTest.qWait(400)
                self.assertTrue(toast.isVisible())
                self.assertEqual(self.toasts[-1], (CLOSING_TEXT, "info"))
            self.assertEqual(self.toasts.count((CLOSING_TEXT, "info")), 1)  # not laid out again
            waiting[0] = False
            self.assertTrue(wait_until(lambda: not self.window.isVisible()))

    def test_closing_notice_is_shown_again_only_after_it_hid(self):
        self.show()
        self.wait_for_drawer()
        shown_over = self.visible_when_shown()
        waiting = [True]
        with (
            mock.patch("ui.tv_window.has_pending_threads", side_effect=lambda: waiting[0]),
            mock.patch("ui.tv_window.CLOSING_NOTICE_MS", 200),
        ):
            self.assertFalse(self.window.close())
            self.assertTrue(
                wait_until(lambda: self.toasts.count((CLOSING_TEXT, "info")) >= 3, 5000)
            )
            waiting[0] = False
            self.assertTrue(wait_until(lambda: not self.window.isVisible()))
        self.assertNotIn(True, shown_over)

    def test_repeated_close_keeps_a_single_retry_timer(self):
        # Quit pressed again and again while a worker is pending must not multiply retries.
        self.show()
        self.wait_for_drawer()
        checks, waiting = [], [True]

        def pending():
            checks.append(time.monotonic())
            return waiting[0]

        with mock.patch("ui.tv_window.has_pending_threads", side_effect=pending):
            for _ in range(20):
                self.assertFalse(self.window.close())
            retries = [
                t
                for t in self.window.findChildren(QTimer)
                if t.isActive() and t.isSingleShot() and t.interval() == CLOSE_RETRY_MS
            ]
            self.assertEqual(retries, [self.window.close_retry])
            started = time.monotonic()
            QTest.qWait(500)
            in_window = [c for c in checks if c >= started]
            self.assertLessEqual(len(in_window), 500 // CLOSE_RETRY_MS + 2)  # one chain
            waiting[0] = False
            self.assertTrue(wait_until(lambda: not self.window.isVisible()))

    def test_the_wait_for_background_work_is_bounded(self):
        self.show()
        self.wait_for_drawer()
        with (
            mock.patch("ui.tv_window.has_pending_threads", return_value=True),
            mock.patch("ui.tv_window.CLOSE_THREAD_WAIT_MS", 300),
            mock.patch("ui.tv_window._pending_thread_names", return_value=["EpgLoadWorker"]),
            mock.patch("ui.tv_window.atexit") as at_exit,
            self.assertLogs("ui.tv_window", "WARNING") as logs,
        ):
            self.assertFalse(self.window.close())
            self.assertTrue(wait_until(lambda: not self.window.isVisible(), 3000))
        self.assertIn("EpgLoadWorker", "\n".join(logs.output))
        at_exit.register.assert_called_once_with(_exit_now)  # leaves without destroying it

    def test_a_late_close_retry_after_closing_does_nothing(self):
        self.show()
        self.close_window()
        with mock.patch("ui.tv_window.has_pending_threads") as pending:
            self.assertTrue(self.window.close())
        pending.assert_not_called()

    def test_input_is_ignored_while_closing(self):
        self.show()
        drawer = self.wait_for_drawer()
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertFalse(drawer.isVisible())
        waiting = [True]
        with (
            mock.patch("ui.tv_window.has_pending_threads", side_effect=lambda: waiting[0]),
            mock.patch("ui.tv_window.OptionsDialog") as dialog,
        ):
            self.assertFalse(self.window.close())
            self.menu_actions()["Settings…"].trigger()
            QTest.keyClick(self.window, Qt.Key.Key_F)
            QTest.keyClick(self.window, Qt.Key.Key_Return)
            self.window.toggle_fullscreen()
            self.window.toggle_mini_player()
            QTest.qWait(50)
            dialog.assert_not_called()
            self.assertFalse(self.window.isFullScreen())
            self.assert_not_mini()
            self.assertFalse(drawer.isVisible())
            waiting[0] = False
            self.assertTrue(wait_until(lambda: not self.window.isVisible()))


STUCK_WORKER_SCRIPT = r"""
import os, sys, tempfile, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QITV_CONFIG_DIR"] = tempfile.mkdtemp(prefix="qitv-stuck-")
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication
from config_manager import ConfigManager
from epg_manager import EpgManager
from image_manager import ImageManager
from provider_manager import ProviderManager
from tests.fixtures.local_provider import seed_channels, write_local_provider
import ui.tv_window as tw

app = QApplication([])
tw.CLOSE_THREAD_WAIT_MS = 300
tw.create_instance = lambda *args: None
provider = write_local_provider(os.environ["QITV_CONFIG_DIR"])
config = ConfigManager()
providers = ProviderManager(config)
seed_channels(providers, provider)
window = tw.TVWindow(app, config, providers, ImageManager(config, 1 << 20), EpgManager(config, providers))


class StuckWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def run(self):
        time.sleep(30)


window.catalog.runner.run_worker(StuckWorker(), lambda *a: None, lambda *a: None)
window.show()
QTimer.singleShot(300, window.close)
print("exec returned", app.exec(), flush=True)
"""


class StuckWorkerExitTests(unittest.TestCase):
    def test_quits_cleanly_when_a_worker_never_finishes(self):
        # Qt aborts the process if a QThread that still runs is destroyed at exit.
        repo = Path(__file__).resolve().parent.parent
        env = {**os.environ, "QT_LOGGING_RULES": "qt.qpa.*=false"}
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-c", STUCK_WORKER_SCRIPT],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("exec returned 0", result.stdout)
        self.assertIn("StuckWorker", result.stderr)  # the WARNING names it
        self.assertNotIn("Destroyed while thread", result.stderr)
        self.assertLess(time.monotonic() - started, 25)  # not waiting for the worker


class GeometryRestoreTests(WindowCase):
    def prepare_config(self):
        positions = self.config.config["window_positions"]
        positions["tv_window"] = {"x": 140, "y": 90, "width": 1024, "height": 576}

    def test_restores_the_saved_geometry_at_construction(self):
        geometry = self.window.geometry()
        self.assertEqual(geometry.getRect(), (140, 90, 1024, 576))


class FitToAreaTests(unittest.TestCase):
    AREA = QRect(0, 25, 1024, 743)  # a 1024×768 display below the macOS menu bar
    TITLE = QMargins(0, 28, 0, 0)

    def test_a_window_that_fits_stays_put(self):
        rect = QRect(60, 103, 800, 450)
        self.assertEqual(fit_to_area(rect, self.TITLE, self.AREA), rect)

    def test_a_window_wider_than_the_screen_is_narrowed_and_moved_on(self):
        # CI macOS ARM: [60, 103, 1100, 620] came back from fullscreen at x = -256.
        fitted = fit_to_area(QRect(60, 103, 1100, 620), self.TITLE, self.AREA)
        self.assertEqual(fitted.getRect(), (0, 103, 1024, 620))

    def test_the_frame_stays_inside_on_every_edge(self):
        margins = QMargins(4, 30, 4, 4)
        for rect in (QRect(-300, 50, 500, 300), QRect(900, 600, 500, 300), QRect(10, 0, 400, 300)):
            fitted = fit_to_area(rect, margins, self.AREA)
            self.assertTrue(self.AREA.contains(fitted.marginsAdded(margins)), fitted)
            self.assertEqual(fitted.size(), rect.size())

    def test_a_window_taller_than_the_area_is_shortened(self):
        fitted = fit_to_area(QRect(0, 40, 600, 900), self.TITLE, self.AREA)
        self.assertEqual(fitted.getRect(), (0, 53, 600, 715))


class FullscreenExitTests(WindowCase):
    def test_leaving_fullscreen_returns_inside_the_screen(self):
        # A display smaller than the window from before fullscreen (macOS put it off screen).
        self.show()
        window = self.window
        area = window.screen().availableGeometry()
        window.setGeometry(area.x() + 60, area.y() + 40, area.width() + 76, area.height() // 2)
        before = window.geometry()
        window.toggle_fullscreen()
        self.assertTrue(wait_until(window.isFullScreen))
        window.toggle_fullscreen()
        margins = frame_margins_of(window)
        expected = fit_to_area(before, margins, area)
        self.assertTrue(
            wait_until(lambda: not window.isFullScreen() and window.geometry() == expected),
            f"{window.geometry()} != {expected}",
        )
        self.assertTrue(area.contains(window.frameGeometry()), window.frameGeometry())

    def test_a_window_that_fits_comes_back_unchanged(self):
        self.show()
        window = self.window
        area = window.screen().availableGeometry()
        window.setGeometry(area.x() + 40, area.y() + 40, area.width() // 2, area.height() // 2)
        before = window.geometry()
        window.toggle_fullscreen()
        self.assertTrue(wait_until(window.isFullScreen))
        window.leave_fullscreen()
        self.assertTrue(wait_until(lambda: not window.isFullScreen()))
        QTest.qWait(100)
        self.assertEqual(window.geometry(), before)


@unittest.skipIf(INSTANCE is None, "libVLC is not available")
class VlcWindowTests(WindowCase):
    vlc_instance = INSTANCE

    def session_on(self, url, states=(PlaybackState.OPENING, PlaybackState.PLAYING)):
        session = self.window.session
        return lambda: session.url == url and session.state() in states

    def test_drawer_opens_and_enter_starts_the_first_channel(self):
        self.open_first_channel()
        self.assertTrue(wait_until(self.session_on(channel_urls()[0])))
        overlays = self.window.overlays
        self.assertFalse(overlays.drawer.isVisible())
        self.assertTrue(overlays.banner.isVisible())
        self.assertEqual(overlays.banner.info.name, "Channel 1")
        self.assertTrue(self.window.host.isVisible())

    def test_down_tunes_the_second_channel(self):
        self.open_first_channel()
        self.assertTrue(wait_until(self.session_on(channel_urls()[0])))
        QTest.keyClick(self.window, Qt.Key.Key_Down)
        self.assertTrue(wait_until(self.session_on(channel_urls()[1])))
        self.assertEqual(self.window.zap.context.current()["name"], "Channel 2")


class NoProviderTests(WindowCase):
    def provider_list(self):
        return []

    def test_first_launch_without_providers_shows_the_welcome_toast(self):
        self.show()
        self.assertTrue(wait_until(lambda: (WELCOME_TEXT, "info") in self.toasts))
        toast, host = self.window.overlays.toast, self.window.host
        self.assertTrue(toast.isVisible())
        self.assertTrue(host.isVisible())
        # Laid out for the host's real size, though the host was hidden when it showed.
        self.assertEqual(host.size(), self.window.surface.size())
        self.assertAlmostEqual(toast.geometry().center().x(), host.width() / 2, delta=2)


class FailingProviderTests(WindowCase):
    def provider_list(self):
        missing = Path(self.config_dir) / "missing.m3u"
        return [{"type": "M3UPLAYLIST", "name": "Broken", "url": str(missing)}]

    def test_a_failing_provider_shows_an_error_and_the_retry_row(self):
        self.show()
        self.assertTrue(wait_until(lambda: any(kind == "error" for _, kind in self.toasts)))
        errors = [text for text, kind in self.toasts if kind == "error"]
        self.assertTrue(errors[0].startswith("Live TV: "), errors)
        drawer = self.wait_for_drawer()
        self.assertEqual(drawer.message_for("right"), ERROR_TEXT)
        stack = self.window.host.children()  # bottom to top
        toast = self.window.overlays.toast
        self.assertGreater(stack.index(toast), stack.index(drawer))  # the error stays readable


class OptionsDialogTests(WindowCase):
    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_settings_load_and_save_the_tv_options(self):
        self.config.live_latency = "stable"
        self.config.mouse_wheel_action = "volume"
        self.config.start_with_last_channel = False
        dialog = self.dialog()
        self.assertFalse(hasattr(dialog, "keyboard_remote_checkbox"))
        self.assertEqual(dialog.live_latency_combo.currentData(), "stable")
        self.assertEqual(dialog.mouse_wheel_combo.currentData(), "volume")
        self.assertFalse(dialog.start_last_channel_checkbox.isChecked())
        self.assertEqual(
            [dialog.live_latency_combo.itemText(i) for i in range(3)], ["Low", "Normal", "Stable"]
        )
        self.assertEqual(
            [dialog.mouse_wheel_combo.itemText(i) for i in range(2)], ["Change channel", "Volume"]
        )
        dialog.live_latency_combo.setCurrentIndex(dialog.live_latency_combo.findData("low"))
        dialog.mouse_wheel_combo.setCurrentIndex(dialog.mouse_wheel_combo.findData("channel"))
        dialog.start_last_channel_checkbox.setChecked(True)
        dialog.save_settings()
        saved = ConfigManager()
        self.assertEqual(saved.live_latency, "low")
        self.assertEqual(saved.mouse_wheel_action, "channel")
        self.assertTrue(saved.start_with_last_channel)

    def test_cache_size_is_read_from_the_config_directory(self):
        cache = Path(self.config_dir) / "cache"
        (cache / "probe.bin").write_bytes(b"x" * 1024 * 1024)
        self.provider_manager._cache_writer.submit(lambda: None).result(timeout=5)
        expected = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file()) / (1024 * 1024)
        self.assertAlmostEqual(self.dialog().get_cache_size(), expected, places=3)

    def test_an_epg_change_reloads_the_guide_in_the_background(self):
        dialog = self.dialog()
        dialog.epg_url_input.setText("http://epg.invalid/guide.xml")
        with (
            mock.patch.object(self.window.catalog, "refresh_epg") as refresh_epg,
            mock.patch.object(self.epg_manager, "set_current_epg") as set_current_epg,
        ):
            dialog.save_settings()
        refresh_epg.assert_called_once_with(force=True)
        set_current_epg.assert_not_called()  # never on the GUI thread


class QtPlatformTests(unittest.TestCase):
    def test_linux_with_an_x_display_prefers_xwayland(self):
        environ = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}
        self.assertEqual(qitv_main.qt_platform_override("Linux", environ), "xcb;wayland")

    def test_an_explicit_choice_or_no_x_display_is_left_alone(self):
        override = qitv_main.qt_platform_override
        self.assertIsNone(override("Linux", {"DISPLAY": ":0", "QT_QPA_PLATFORM": "wayland"}))
        self.assertIsNone(override("Linux", {"WAYLAND_DISPLAY": "wayland-0"}))
        self.assertIsNone(override("Darwin", {"DISPLAY": ":0"}))
        self.assertIsNone(override("Windows", {}))


if __name__ == "__main__":
    unittest.main()
