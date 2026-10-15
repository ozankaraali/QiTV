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
from urllib.parse import urlsplit

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
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import main as qitv_main
from services import update_checker
from services.config_manager import ConfigManager, get_app_version
from services.epg_manager import EpgManager
from services.image_manager import ImageManager
from services.playback import NO_VLC_MESSAGE, PlaybackState, create_instance
from services.provider_manager import ProviderManager
from services.thread_cleanup import has_pending_threads
from services.vlc_profile import PlayKind
from tests.fixtures import local_xtream
from tests.fixtures.local_provider import (
    PROVIDER_NAME,
    channel_urls,
    seed_channels,
    write_local_provider,
)
from tests.fixtures.local_xtream import XtreamFixture
from tests.http_fixtures import LocalSource
from tests.test_keymap import resolve_text
from tests.ts_fixtures import make_stream
from ui.brand import logo_aspect
from ui.keymap import SHORTCUTS, shortcut_keys
from ui.overlay_host import OverlayHost
from ui.overlays.drawer import ERROR_TEXT
from ui.settings_dialog import OptionsDialog
from ui.shortcuts_dialog import ShortcutsDialog, table_rows
from ui.tv_menus import ISSUES_URL, AboutDialog
from ui.tv_window import (
    ABANDONED_EXIT_CODE,
    CLOSE_RETRY_MS,
    CLOSING_TEXT,
    WATCH_SAVE_DELAY_MS,
    TVWindow,
    _exit_now,
    fit_to_area,
    frame_margins as frame_margins_of,
)

INSTANCE = create_instance(["--vout=dummy", "--aout=dummy", "--quiet"])
HOST_METHODS = ("save_config", "set_provider", "reload_epg")
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
    seed_fixture = False  # seed the local fixture's channels for a custom provider list

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
        if providers is None or self.seed_fixture:
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
        """Every menu entry by its text, submenus' entries included."""
        found = {}

        def walk(menu):
            for action in menu.actions():
                if action.menu() is not None:
                    walk(action.menu())
                elif action.text():
                    found[action.text()] = action

        for top in self.window.menuBar().actions():
            walk(top.menu())
        return found

    def mini(self):
        QTest.keyClick(self.window, Qt.Key.Key_P, Qt.KeyboardModifier.AltModifier)


class TVWindowTests(WindowCase):
    def test_constructs_and_shows_with_the_overlay_host(self):
        self.show()
        window = self.window
        self.assertIsInstance(window.host, OverlayHost)
        self.assertIs(window.centralWidget(), window.pages)
        self.assertEqual(
            [window.pages.widget(i) for i in range(window.pages.count())],
            [window.player_page, window.library_page],
        )
        self.assertIs(window.pages.currentWidget(), window.player_page)
        # The video container a second (PiP) surface can join: the surface fills it.
        self.assertIs(window.surface.parentWidget(), window.player_page)
        self.assertEqual(window.surface.geometry(), window.player_page.rect())

    def test_the_player_page_paints_the_black_behind_the_video(self):
        # macOS never repaints under an opaque native view: after the Library page, its last
        # pixels stayed where the video goes (seen on screen with QScreen.grabWindow). The
        # surface lets Qt paint the page's black under it instead.
        window, surface = self.window, self.window.surface
        self.assertTrue(window.player_page.autoFillBackground())
        self.assertEqual(window.player_page.palette().window().color().name(), "#000000")
        self.assertFalse(surface.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent))
        self.assertFalse(surface.autoFillBackground())
        self.assertTrue(surface.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))
        self.show()
        window.screens.open_library("movies")
        window.screens.back_to_tv()
        QTest.qWait(50)
        middle = surface.mapTo(window, surface.rect().center())
        self.assertEqual(window.grab().toImage().pixelColor(middle).name(), "#000000")

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

    def test_a_loading_hint_shows_from_startup_until_the_channels_arrive(self):
        toast, hints = self.window.overlays.toast, []
        set_hint = toast.set_hint

        def spy(text):
            hints.append(text)
            set_hint(text)

        toast.set_hint = spy
        self.show()
        self.wait_for_drawer()
        self.assertTrue(wait_until(lambda: hints[-1:] == [None]))
        name = self.provider_manager.providers[0]["name"]
        self.assertEqual(hints, [f"Loading {name}…", None])

    def test_notices_are_toasted_once_by_the_started_controller(self):
        self.show()
        self.wait_for_drawer()
        text = "Couldn't refresh Live TV; showing saved channels"
        self.window.nav.notice.emit(text)
        self.assertEqual(self.toasts.count((text, "info")), 1)

    def test_watch_state_is_saved_off_the_tune_path(self):
        self.assertEqual(self.window.watch.save_delay_ms, WATCH_SAVE_DELAY_MS)
        self.assertGreater(WATCH_SAVE_DELAY_MS, 0)

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

    def test_mouse_back_and_forward_step_through_the_drawer_layers(self):
        self.show()
        drawer = self.wait_for_drawer()
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertFalse(drawer.isVisible())
        back, forward = Qt.MouseButton.BackButton, Qt.MouseButton.ForwardButton
        QTest.mouseClick(self.window.surface, back, pos=QPoint(40, 40))
        self.assertTrue(drawer.isVisible())  # closed, Back opens it (as v1.13.8 showed the list)
        self.assertEqual(drawer.focus_side, "right")
        over_rows = drawer._columns["right"].view.geometry().center()
        handle = self.window.host.windowHandle()
        QTest.mouseClick(handle, back, pos=over_rows)  # over the drawer: a layer up
        self.assertEqual(drawer.focus_side, "left")
        QTest.mouseClick(handle, forward, pos=over_rows)  # and down again
        self.assertEqual(drawer.focus_side, "right")
        self.assertTrue(drawer.isVisible())

    def test_video_is_not_bound_without_native_window_ids(self):
        # libVLC crashes drawing into the offscreen platform's fake window ids.
        with mock.patch.object(self.window.session, "set_window") as set_window:
            self.show()
            self.wait_for_drawer()
        set_window.assert_not_called()

    # ---- menus and keys -----------------------------------------------------------------------

    def test_menus_list_the_tv_actions_with_their_shortcuts(self):
        menus = {action.text(): action.menu() for action in self.window.menuBar().actions()}
        self.assertEqual(list(menus), ["QiTV", "File", "View", "Playback", "Providers", "Help"])

        def shortcuts(menu):
            return [(a.text(), a.shortcut().toString()) for a in menu.actions()]

        self.assertEqual(
            shortcuts(menus["QiTV"]),
            [
                ("About QiTV", ""),
                ("Check for Updates…", ""),
                ("Settings…", "Ctrl+,"),
                ("Quit", "Ctrl+Q"),
            ],
        )
        self.assertEqual(shortcuts(menus["File"]), [("Open File…", "Ctrl+O"), ("Export", "")])
        export = menus["File"].actions()[1].menu()
        self.assertEqual(
            [a.text() for a in export.actions()],
            ["Cached Content…", "All Live Channels…", "Complete Series (Fetch All)…"],
        )
        self.assertEqual(
            shortcuts(menus["View"]),
            [
                ("Live TV", "Ctrl+1"),
                ("Movies", "Ctrl+2"),
                ("Series", "Ctrl+3"),
                ("Search", "Ctrl+F"),
                ("Guide", "G"),
                ("Channel List", "Return"),
                ("Fullscreen", "F"),
                ("Mini Player", "Alt+P"),
            ],
        )
        self.assertEqual(
            shortcuts(menus["Playback"]),
            [
                ("Play/Pause", "Space"),
                ("Mute", "M"),
                ("Previous Channel", "Backspace"),
                ("Go Live", "L"),
            ],
        )
        self.assertEqual(
            shortcuts(menus["Help"]), [("Keyboard Shortcuts…", ""), ("Report an Issue…", "")]
        )
        # Only modifier combinations are live shortcuts; single keys are hints in the menu.
        actions = self.menu_actions()
        live = {
            "Settings…",
            "Quit",
            "Open File…",
            "Live TV",
            "Movies",
            "Series",
            "Search",
            "Mini Player",
        }
        for text, action in actions.items():
            if action.shortcut().isEmpty():
                continue
            expected = (
                Qt.ShortcutContext.ApplicationShortcut
                if text in live
                else Qt.ShortcutContext.WidgetShortcut
            )
            self.assertEqual(action.shortcutContext(), expected, text)

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

    def test_g_opens_the_guide_which_takes_the_keys_until_g_closes_it(self):
        self.open_first_channel()
        guide, zap = self.window.overlays.guide, self.window.zap
        self.assertTrue(wait_until(lambda: zap.context.current() is not None))  # tuned
        QTest.keyClick(self.window, Qt.Key.Key_G)
        self.assertTrue(guide.isVisible())
        self.assertEqual(guide.current_channel()["name"], "Channel 1")
        QTest.keyClick(self.window, Qt.Key.Key_Down)  # moves in the guide: no zap
        self.assertEqual(guide.current_channel()["name"], "Channel 2")
        self.assertEqual(zap.context.current()["name"], "Channel 1")
        QTest.keyClick(self.window, Qt.Key.Key_G)
        self.assertFalse(guide.isVisible())
        card = self.window.overlays.next_card
        self.assertIs(card.parentWidget(), self.window.host)  # hosted over the video
        self.assertIs(card.parentWidget(), guide.parentWidget())

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
        actions["Channel List"].trigger()
        self.assertTrue(drawer.isVisible())

    def test_ctrl_f_opens_the_search_from_the_player_the_drawer_and_the_guide(self):
        self.show()
        drawer = self.wait_for_drawer()
        window, ctrl = self.window, Qt.KeyboardModifier.ControlModifier
        for setup in (lambda: None, lambda: QTest.keyClick(window, Qt.Key.Key_Escape)):
            window.screens.back_to_tv()
            if not drawer.isVisible():
                drawer.open_drawer()
            setup()  # the drawer open, then closed (the player)
            QTest.keyClick(window, Qt.Key.Key_F, ctrl)
            self.assertTrue(window.screens.library_shown())
            self.assertEqual(window.library_controller.mode, "search")
            self.assertFalse(window.isFullScreen())
        window.screens.back_to_tv()
        drawer.close_drawer()
        window.controller.tune_live(window.catalog.items("live", "*")[0])
        QTest.keyClick(window, Qt.Key.Key_G)
        self.assertTrue(window.overlays.guide.isVisible())
        QTest.keyClick(window, Qt.Key.Key_F, ctrl)
        self.assertTrue(window.screens.library_shown())
        self.assertFalse(window.overlays.guide.isVisible())

    def test_f11_toggles_fullscreen_in_the_library(self):
        self.show()
        self.wait_for_drawer()
        self.window.screens.open_library("movies")
        QTest.keyClick(self.window, Qt.Key.Key_F11)
        self.assertTrue(wait_until(self.window.isFullScreen))
        self.assertTrue(self.window.screens.library_shown())

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
from services.config_manager import ConfigManager
from services.epg_manager import EpgManager
from services.image_manager import ImageManager
from services.provider_manager import ProviderManager
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
        # Abandoned work is a failure for whoever ran QiTV (not 0), but no abort.
        self.assertEqual(result.returncode, ABANDONED_EXIT_CODE, result.stderr[-2000:])
        self.assertNotEqual(ABANDONED_EXIT_CODE, 0)
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


class OffScreenRestoreTests(WindowCase):
    """A geometry saved on a monitor that is gone (Minor 13)."""

    def prepare_config(self):
        positions = self.config.config["window_positions"]
        positions["tv_window"] = {"x": 5000, "y": 4000, "width": 640, "height": 400}

    def test_the_restored_window_is_moved_onto_the_screen_at_startup(self):
        self.show()
        window = self.window
        area = window.screen().availableGeometry()
        self.assertTrue(
            wait_until(lambda: area.contains(window.frameGeometry())), f"{window.geometry()}"
        )
        self.assertEqual(window.size().toTuple(), (640, 400))


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
    def test_a_change_asked_while_the_last_one_animates_waits_for_it(self):
        # AppKit drops a fullscreen change made while the previous one still animates: on
        # CI's macOS 15 a leave right after the entry left the window in fullscreen (macOS 13
        # too, within ~0.6 s). The second request waits for the platform to report the first.
        self.show()
        window, asked = self.window, []
        set_state = window.setWindowState

        def spy(state):
            asked.append(bool(state & Qt.WindowState.WindowFullScreen))
            set_state(state)

        with (
            mock.patch.object(window, "setWindowState", side_effect=spy),
            mock.patch.object(type(window), "_animates_fullscreen", return_value=True),
        ):
            window.toggle_fullscreen()
            window.toggle_fullscreen()  # at once: the entry has not been reported yet
            self.assertEqual(asked, [True])
            self.assertTrue(
                wait_until(lambda: asked == [True, False] and not window.isFullScreen(), 5000),
                f"{asked}",
            )
            window.toggle_fullscreen()
            window.leave_fullscreen()  # Esc right after F: the entry is undone once reported
            self.assertTrue(wait_until(lambda: asked[-1] is False and len(asked) == 4, 5000))
            self.assertTrue(wait_until(lambda: not window.isFullScreen(), 5000))

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

    def test_an_exit_the_window_did_not_ask_for_returns_to_the_geometry_from_before(self):
        # The green button: the platform leaves fullscreen itself and may restore another
        # frame (macOS 26 restored the size first asked for).
        self.show()
        window = self.window
        area = window.screen().availableGeometry()
        window.setGeometry(area.x() + 40, area.y() + 40, area.width() // 2, area.height() // 2)
        before = window.geometry()
        window.toggle_fullscreen()
        self.assertTrue(wait_until(window.isFullScreen))
        window.setWindowState(window.windowState() & ~Qt.WindowState.WindowFullScreen)
        window.setGeometry(area.x(), area.y(), area.width() // 3, area.height() // 3)
        self.assertTrue(
            wait_until(lambda: window.geometry() == before, 4000), f"{window.geometry()}"
        )

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


class LivePipelineWindowTests(WindowCase):
    """A live channel on a local HTTP source plays through the time-shift pipeline."""

    vlc_instance = INSTANCE

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.payload = make_stream(60)  # 60 s of H.264 and AAC in MPEG-TS, about 2 MB

    def setUp(self):
        stop, payload = threading.Event(), self.payload

        def paced(request):  # a live-looking stream, a few times real time
            try:
                for offset in range(0, len(payload), 188 * 20):
                    if stop.wait(0.02):
                        return
                    request.wfile.write(payload[offset : offset + 188 * 20])
            except OSError:
                pass

        route = lambda _request, _count: (200, {"Content-Type": "video/mp2t"}, paced)  # noqa: E731
        self.source = LocalSource({"/live/1.ts": route, "/live/2.ts": route})
        self.addCleanup(self.source.close)  # after the window closed (cleanups run last first)
        self.addCleanup(stop.set)
        urls = [f"{self.source.url}/live/1.ts", f"{self.source.url}/live/2.ts"]
        patcher = mock.patch("tests.fixtures.local_provider.channel_urls", return_value=urls)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_a_tuned_channel_plays_from_the_loopback_buffer(self):
        window = self.window
        modes = []
        window.live_session.modeChanged.connect(
            lambda mode: modes.append((mode, window.session.url, window.session.kind))
        )
        self.open_first_channel()
        self.assertTrue(wait_until(lambda: modes))
        mode, url, kind = modes[0]
        self.assertEqual(mode, "pipeline")
        self.assertTrue(url.startswith("http://127.0.0.1:"), url)
        self.assertNotEqual(urlsplit(url).port, self.source.server.server_port)
        # /<server token>/<session>/stream/<segment>.ts
        self.assertRegex(urlsplit(url).path, r"^/[0-9a-f]+/[0-9a-f]+/stream/\d+\.ts$")
        self.assertEqual(kind, PlayKind.LIVE_PIPELINE)
        self.assertEqual(window.budget.holders(PROVIDER_NAME), ["main"])
        self.assertEqual(window.engine.root_dir, Path(self.config_dir) / "timeshift")

    def test_close_stops_the_live_session_then_the_engine_then_the_rest(self):
        window, order = self.window, []

        def spy(name, method):
            def call(*args, **kwargs):
                order.append(name)
                return method(*args, **kwargs)

            return call

        live, engine, catalog = window.live_session, window.engine, window.catalog
        for obj, name in ((live, "stop"), (engine, "shutdown"), (catalog, "shutdown")):
            patcher = mock.patch.object(
                obj, name, spy(f"{type(obj).__name__}.{name}", getattr(obj, name))
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        self.open_first_channel()
        self.assertTrue(wait_until(lambda: live.is_pipeline()))
        order.clear()  # LiveSession.start() stops first too
        with self.assertNoLogs("services.live.live_session", "WARNING"):  # no close-cap warning
            self.close_window()  # it also checks that no background thread is left
        self.assertEqual(
            order[:3], ["LiveSession.stop", "LiveEngine.shutdown", "CatalogService.shutdown"]
        )

    def test_a_windows_self_update_stops_the_pipeline_before_waiting_for_threads(self):
        self.show()
        self.assertTrue(wait_until(self.window.engine.is_available))
        with (
            mock.patch("services.update_checker.subprocess") as process,
            mock.patch.object(self.app, "closeAllWindows") as close_all,
        ):
            update_checker._perform_windows_update("C:/Downloads/qitv.exe", "https://release")
            self.assertTrue(wait_until(lambda: process.Popen.called))
        self.assertFalse(self.window.engine.is_available())
        self.assertEqual(process.Popen.call_args[0][0][:2], ["C:/Downloads/qitv.exe", "--replace"])
        close_all.assert_called_once_with()

    def test_a_self_update_that_cannot_launch_brings_time_shift_back(self):
        window = self.window
        modes = []
        window.live_session.modeChanged.connect(modes.append)
        self.open_first_channel()
        self.assertTrue(wait_until(lambda: modes == ["pipeline"]))
        first = window.engine
        with (
            mock.patch("services.update_checker.subprocess") as process,
            mock.patch("services.update_checker.QMessageBox"),
            mock.patch("services.update_checker._polish_msgbox"),
            mock.patch.object(self.app, "closeAllWindows") as close_all,
        ):
            process.Popen.side_effect = OSError("not a valid application")
            update_checker._perform_windows_update("C:/Downloads/qitv.exe", "https://release")
            # Retried every 25 ms while VLC's stop job runs: wait inside the patches.
            self.assertTrue(wait_until(lambda: process.Popen.called and window.engine is not first))
        close_all.assert_not_called()
        self.assertIsNot(window.engine, first)
        self.assertFalse(first.is_available())
        self.assertTrue(wait_until(window.engine.is_available))
        hooks = [stop() for stop, _undo in update_checker._before_update]
        self.assertIn(window.stop_live, hooks)  # a later update stops it again
        # The channel plays again, time-shifted, on the new helper.
        self.assertTrue(wait_until(lambda: modes[-1:] == ["pipeline"] and len(modes) == 2, 5000))
        self.assertEqual(window.zap.context.current()["name"], "Channel 1")


# ---- the Library, menus, exports (Phase 3 Task 12) -------------------------------------------

XTREAM = local_xtream.PROVIDER_NAME
LIBRARY_WAIT_MS = 15000  # a provider setup and a section over the local HTTP fixture


class TwoProviderCase(WindowCase):
    """The local M3U fixture and a fake Xtream provider served on 127.0.0.1 (no network).

    With libVLC when it is available: playback is checked through the session's play calls,
    which both sessions receive, and through its state when it really plays.
    """

    vlc_instance = INSTANCE
    seed_fixture = True
    selected = PROVIDER_NAME  # the provider QiTV starts on

    def setUp(self):
        self.xtream = XtreamFixture()
        self.addCleanup(self.xtream.close)  # after the window closed (cleanups run last first)
        super().setUp()

    def provider_list(self):
        fixture = {
            "type": "M3UPLAYLIST",
            "name": PROVIDER_NAME,
            "url": str(Path(self.config_dir) / "fixture.m3u"),
        }
        return [fixture, self.xtream.provider()]

    def prepare_config(self):
        self.config.selected_provider_name = self.selected

    def wait_ready(self, name):
        catalog = self.window.catalog
        self.assertTrue(
            wait_until(
                lambda: catalog.current_provider_name() == name and not catalog.is_loading(),
                LIBRARY_WAIT_MS,
            ),
            catalog.current_provider_name(),
        )

    def play_spy(self):
        """The URLs the player is asked to play, in order."""
        urls, session = [], self.window.session
        play = session.play

        def spy(url, **kwargs):
            urls.append(url)
            return play(url, **kwargs)

        patcher = mock.patch.object(session, "play", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return urls

    def grid(self):
        return self.window.library_page.grid

    def wait_for_titles(self):
        grid = self.grid()
        self.assertTrue(wait_until(lambda: grid.current_index() >= 0, LIBRARY_WAIT_MS))
        return grid

    def wait_idle(self):
        """libVLC stops on a worker: the state follows (NullSession is always idle)."""
        session = self.window.session
        self.assertTrue(wait_until(lambda: session.state() == PlaybackState.IDLE))


class LibraryWindowTests(TwoProviderCase):
    def test_view_movies_opens_the_library_stops_live_and_hides_the_overlays(self):
        plays = self.play_spy()
        self.open_first_channel()
        window = self.window
        self.assertTrue(wait_until(lambda: plays[-1:] == [channel_urls()[0]]))
        if INSTANCE is not None:
            playing = (PlaybackState.OPENING, PlaybackState.PLAYING)
            self.assertTrue(wait_until(lambda: window.session.state() in playing))
        window.screens.notify("Keeps the overlay host up")
        self.assertTrue(wait_until(window.host.isVisible))
        actions = self.menu_actions()
        actions["Movies"].trigger()
        self.assertIs(window.pages.currentWidget(), window.library_page)
        self.wait_idle()
        self.assertFalse(window.host.isVisible())
        # The stream's slot goes back once the release gate has let go of it.
        self.assertTrue(wait_until(lambda: window.budget.holders(PROVIDER_NAME) == []))
        self.assertTrue(window.library_toast.isVisible())  # messages show on the Library
        for text in ("Guide", "Channel List", "Play/Pause", "Mute", "Previous Channel"):
            self.assertFalse(actions[text].isEnabled(), text)  # the player's keys
        self.assertTrue(actions["Fullscreen"].isEnabled())

    def test_live_tv_tunes_the_last_channel_again(self):
        plays = self.play_spy()
        self.open_first_channel()
        first = channel_urls()[0]
        self.assertTrue(wait_until(lambda: plays == [first]))
        actions = self.menu_actions()
        actions["Movies"].trigger()
        self.wait_idle()
        actions["Live TV"].trigger()
        self.assertIs(self.window.pages.currentWidget(), self.window.player_page)
        self.assertTrue(wait_until(lambda: plays == [first, first]))
        self.assertEqual(self.window.zap.context.current()["name"], "Channel 1")
        self.assertTrue(self.window.host.isVisible())  # the banner names it
        self.assertTrue(actions["Play/Pause"].isEnabled())

    def test_export_cached_content_writes_the_file_and_says_so(self):
        self.show()
        self.wait_for_drawer()
        target = Path(self.config_dir) / "exports" / "channels.m3u"
        target.parent.mkdir()
        answer = (str(target), "M3U files (*.m3u)")
        with mock.patch("ui.export_flow.QFileDialog.getSaveFileName", return_value=answer) as ask:
            self.menu_actions()["Cached Content…"].trigger()
        default = Path.home() / "Downloads" / f"{PROVIDER_NAME}-cached.m3u"
        self.assertEqual(ask.call_args[0][1:], ("Export", str(default), "M3U files (*.m3u)"))
        done = ("Exported 2 items to channels.m3u", "info")
        self.assertTrue(wait_until(lambda: done in self.toasts, 5000), self.toasts)
        text = target.read_text(encoding="utf-8")
        self.assertEqual(text.count("#EXTINF"), 2)
        self.assertIn(channel_urls()[0], text)

    def test_an_export_the_section_cannot_make_says_why_and_asks_nothing(self):
        self.show()
        self.wait_for_drawer()
        with mock.patch("ui.export_flow.QFileDialog.getSaveFileName") as ask:
            self.menu_actions()["Complete Series (Fetch All)…"].trigger()
        ask.assert_not_called()
        self.assertIn("Complete export is only available for STB series", self.toasts[-1][0])

    def test_the_providers_menu_lists_both_and_switches(self):
        self.show()
        self.wait_for_drawer()
        menu = self.window.menus.providers_menu

        def entries():
            return [(a.text(), a.isChecked()) for a in menu.actions()]

        self.assertEqual(entries(), [(PROVIDER_NAME, True), (XTREAM, False)])
        group = menu.actions()[0].actionGroup()
        self.assertTrue(group is not None and group.isExclusive())
        menu.actions()[1].trigger()
        self.wait_ready(XTREAM)
        self.assertTrue(wait_until(lambda: entries() == [(PROVIDER_NAME, False), (XTREAM, True)]))

    def test_esc_while_exporting_cancels_the_export_only(self):
        self.show()
        drawer = self.wait_for_drawer()
        with mock.patch.object(self.window.exports, "cancel", return_value=True) as cancel:
            QTest.keyClick(self.window, Qt.Key.Key_Escape)
        cancel.assert_called_once_with()
        self.assertTrue(drawer.isVisible())  # the Esc went to the export


class XtreamLibraryTests(TwoProviderCase):
    selected = XTREAM

    def test_a_movie_plays_and_esc_returns_to_the_library_where_it_was(self):
        plays = self.play_spy()
        self.show()
        self.wait_ready(XTREAM)
        window = self.window
        self.menu_actions()["Movies"].trigger()
        grid = self.wait_for_titles()
        QTest.keyClick(window, Qt.Key.Key_Right)
        current = grid.item(grid.current_index())
        self.assertIn(current["name"], local_xtream.movie_titles())
        self.assertEqual(grid.current_index(), 1)
        QTest.keyClick(window, Qt.Key.Key_Return)
        self.assertIs(window.pages.currentWidget(), window.player_page)
        self.assertTrue(wait_until(lambda: plays == [current["cmd"]]))
        self.assertTrue(current["cmd"].startswith(self.xtream.url))
        if INSTANCE is not None:
            playing = (PlaybackState.OPENING, PlaybackState.PLAYING, PlaybackState.BUFFERING)
            self.assertTrue(wait_until(lambda: window.session.state() in playing))
        QTest.keyClick(window, Qt.Key.Key_Escape)
        self.assertIs(window.pages.currentWidget(), window.library_page)
        self.assertEqual(grid.current_index(), 1)
        self.assertIs(grid.item(1), current)  # the same title, not a reloaded list
        self.wait_idle()

    def test_choosing_movies_in_the_drawer_opens_the_library(self):
        self.show()
        self.wait_ready(XTREAM)
        drawer = self.wait_for_drawer()  # no channels here: it opens on the idle player
        for key in (Qt.Key.Key_Left, Qt.Key.Key_Down):  # the sections, then Movies
            QTest.keyClick(self.window, key)
        self.assertEqual(drawer.highlighted().title, "Movies")
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        self.assertIs(self.window.pages.currentWidget(), self.window.library_page)
        self.assertFalse(drawer.isVisible())
        self.assertEqual(self.window.library_controller.section, "movies")
        self.wait_for_titles()

    def test_a_search_hit_on_another_provider_plays_once_it_is_ready(self):
        # Review Focus 4: the play waits for its provider, and the shell's own provider
        # change (it drops older requests) never drops it.
        plays = self.play_spy()
        self.show()
        self.wait_ready(XTREAM)
        window = self.window
        actions = self.menu_actions()
        actions["Movies"].trigger()
        self.wait_for_titles()  # cached: the search reads every provider's saved catalog
        self.provider_manager._cache_writer.submit(lambda: None).result(timeout=5)
        window.menus.providers_menu.actions()[0].trigger()
        self.wait_ready(PROVIDER_NAME)
        actions["Search"].trigger()
        self.assertEqual(window.library_controller.mode, "search")
        for letter in "quiet":
            QTest.keyClick(window, letter)
        grid = self.grid()
        self.assertTrue(
            wait_until(
                lambda: grid.current_index() >= 0 and grid.item(0)["name"] == "Quiet Pier",
                LIBRARY_WAIT_MS,
            )
        )
        QTest.keyClick(window, Qt.Key.Key_Down)  # from the field to the results
        QTest.keyClick(window, Qt.Key.Key_Return)
        self.wait_ready(XTREAM)
        self.assertTrue(wait_until(lambda: len(plays) == 1, LIBRARY_WAIT_MS))
        self.assertTrue(plays[0].endswith("/102.mp4"), plays)
        QTest.qWait(300)
        self.assertEqual(len(plays), 1)  # nothing plays twice, no channel after it
        self.assertIs(window.pages.currentWidget(), window.player_page)
        self.assertTrue(window.controller.vod_current())


class LibraryReturnTests(WindowCase):
    """Esc back to the Library and the mini player (review findings)."""

    MOVIE = {"id": "m1", "name": "Quiet Pier", "cmd": "http://127.0.0.1:9/movie/1.mp4"}

    def held_links(self):
        """The catalog keeps every link request open: the test settles them."""
        ids = []

        def request_link(item, *, section, is_episode=False):
            ids.append(len(ids) + 1000)
            return ids[-1]

        patcher = mock.patch.object(self.window.catalog, "request_link", side_effect=request_link)
        patcher.start()
        self.addCleanup(patcher.stop)
        return ids

    def play_from_the_library(self):
        self.show()
        self.wait_for_drawer()
        screens = self.window.screens
        screens.open_library("movies")
        screens.play(self.MOVIE, "movie", {})
        self.assertFalse(screens.library_shown())

    def test_esc_while_the_link_is_pending_returns_and_drops_it(self):
        ids = self.held_links()
        plays = []
        self.window.session.play = lambda url, **kwargs: plays.append(url)
        self.play_from_the_library()
        self.assertEqual(len(ids), 1)  # asked for, not answered yet
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertTrue(self.window.screens.library_shown())
        self.window.catalog.linkReady.emit(ids[0], self.MOVIE["cmd"])
        QTest.qWait(20)
        self.assertEqual(plays, [])  # the link came too late: dropped, never opened

    def test_esc_after_the_link_failed_returns(self):
        ids = self.held_links()
        self.play_from_the_library()
        self.window.catalog.linkFailed.emit(ids[0], "Couldn't reach the provider")
        self.assertTrue(any(kind == "error" for _text, kind in self.toasts))
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertTrue(self.window.screens.library_shown())

    def test_a_live_channel_chosen_after_the_movie_keeps_esc(self):
        ids = self.held_links()
        self.play_from_the_library()
        channel = self.window.catalog.items("live", "*")[0]
        self.window.controller.tune(channel, group_key="*", channels=[channel])
        self.window.overlays.banner.dismiss()  # Esc closes what is open first
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertFalse(self.window.screens.library_shown())  # live TV now: no way back
        self.assertFalse(self.window.controller.overlay_open())

    def test_the_library_leaves_the_mini_player_first(self):
        self.show()
        self.wait_for_drawer()
        self.mini()
        self.assertTrue(self.window.mini_player_active())
        QTest.keyClick(self.window, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.window.screens.library_shown())
        self.assertFalse(self.window.mini_player_active())
        self.assertFalse(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)


class DrawerSettingsTests(WindowCase):
    def test_the_drawer_reaches_settings_and_add_provider_with_arrows_and_enter(self):
        self.show()
        drawer = self.wait_for_drawer()  # on the sections and the groups
        with mock.patch("ui.tv_window.OptionsDialog") as dialog:
            for key in (Qt.Key.Key_Left, Qt.Key.Key_Left, Qt.Key.Key_Down):
                QTest.keyClick(self.window, key)  # the sources column, then ⚙ Settings
            self.assertEqual(drawer.highlighted().title, "Settings")
            QTest.keyClick(self.window, Qt.Key.Key_Return)
            dialog.assert_called_once_with(self.window)
            dialog.return_value.exec.assert_called_once_with()
            dialog.return_value.start_new_provider.assert_not_called()
            QTest.keyClick(self.window, Qt.Key.Key_Down)
            self.assertEqual(drawer.highlighted().title, "Add provider")
            QTest.keyClick(self.window, Qt.Key.Key_Return)
            dialog.return_value.start_new_provider.assert_called_once_with()
        self.assertEqual(dialog.call_count, 2)
        self.assertTrue(drawer.isVisible())

    def test_add_provider_opens_the_providers_tab_on_a_new_entry(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        before = dialog.provider_combo.count()
        dialog.start_new_provider()
        self.assertIs(dialog.options_tab.currentWidget(), dialog.providers_tab)
        self.assertEqual(dialog.provider_combo.count(), before + 1)
        self.assertEqual(dialog.provider_combo.currentIndex(), before)
        self.assertEqual(dialog.name_input.text(), "")


class HelpAndAboutTests(WindowCase):
    def test_every_key_in_the_shortcuts_table_works_in_its_screen(self):
        menu_keys = {
            action.shortcut().toString()
            for action in self.menu_actions().values()
            if not action.shortcut().isEmpty()
        }
        for context, rows in SHORTCUTS.items():
            for keys, what in rows:
                for key in shortcut_keys(keys):
                    with self.subTest(context=context, key=key, what=what):
                        if context == "menu":
                            self.assertIn(QKeySequence(key).toString(), menu_keys)
                            continue
                        self.assertIsNotNone(resolve_text(key, context))

    def test_help_lists_the_table(self):
        with mock.patch("ui.tv_menus.ShortcutsDialog") as dialog:
            self.menu_actions()["Keyboard Shortcuts…"].trigger()
        dialog.assert_called_once_with(self.window)
        dialog.return_value.exec.assert_called_once_with()
        shown = ShortcutsDialog(self.window)
        self.addCleanup(shown.deleteLater)
        rows = table_rows()
        self.assertEqual(len(rows), len(SHORTCUTS) + sum(len(r) for r in SHORTCUTS.values()))
        self.assertEqual(shown.table.rowCount(), len(rows))
        self.assertEqual(shown.table.item(0, 0).text(), "Watching TV")

    def test_about_names_the_version_and_the_links(self):
        with mock.patch("ui.tv_menus.AboutDialog") as dialog:
            self.menu_actions()["About QiTV"].trigger()
        dialog.assert_called_once_with(self.window)
        dialog.return_value.exec.assert_called_once_with()
        about = AboutDialog(self.window)
        self.addCleanup(about.deleteLater)
        text = about.text.text()
        self.assertIn(f"QiTV {get_app_version()}", text)
        self.assertIn(ISSUES_URL, text)
        # The same cropped logo as the top bar and the card, above the text, at 64 px.
        logo = about.logo.pixmap()
        self.assertEqual(logo.deviceIndependentSize().height(), 64)
        self.assertAlmostEqual(logo.width() / logo.height(), logo_aspect(), delta=0.02)
        layout = about.layout()
        self.assertLess(layout.indexOf(about.logo), layout.indexOf(about.text))

    def test_check_for_updates_and_report_an_issue(self):
        with (
            mock.patch("ui.tv_menus.check_for_updates") as check,
            mock.patch("ui.tv_menus.QDesktopServices.openUrl") as open_url,
        ):
            actions = self.menu_actions()
            actions["Check for Updates…"].trigger()
            actions["Report an Issue…"].trigger()
        check.assert_called_once_with(self.config, manual=True)
        self.assertEqual(open_url.call_args[0][0].toString(), ISSUES_URL)


class NoProviderTests(WindowCase):
    def provider_list(self):
        return []

    def test_first_launch_without_providers_shows_the_welcome_card(self):
        self.show()
        card, host = self.window.overlays.welcome, self.window.host
        self.assertTrue(wait_until(card.isVisible))
        self.assertTrue(host.isVisible())
        self.assertFalse(self.window.overlays.toast.isVisible())  # no hint under it
        # Laid out for the host's real size, though the host was hidden when it showed.
        self.assertEqual(host.size(), self.window.surface.size())
        self.assertAlmostEqual(card.geometry().center().x(), host.width() / 2, delta=2)
        self.assertAlmostEqual(card.geometry().center().y(), host.height() / 2, delta=2)

    def test_its_button_and_enter_open_settings_on_a_new_provider(self):
        self.show()
        card = self.window.overlays.welcome
        self.assertTrue(wait_until(card.isVisible))
        added = []
        card.addRequested.connect(lambda: added.append(True))
        with mock.patch("ui.tv_window.OptionsDialog") as dialog:
            QTest.mouseClick(
                card, Qt.MouseButton.LeftButton, pos=card.button_rect().center().toPoint()
            )
            self.assertEqual(added, [True])
            dialog.assert_called_once_with(self.window)
            dialog.return_value.start_new_provider.assert_called_once_with()
            dialog.return_value.exec.assert_called_once_with()
            QTest.keyClick(self.window, Qt.Key.Key_Return)  # OK on a remote
        self.assertEqual(added, [True, True])
        self.assertEqual(dialog.call_count, 2)
        self.assertFalse(self.window.overlays.drawer.isVisible())

    def test_the_card_hides_once_a_provider_is_saved(self):
        self.show()
        card = self.window.overlays.welcome
        self.assertTrue(wait_until(card.isVisible))
        write_local_provider(self.config_dir)  # what Settings would save
        self.provider_manager.providers = [
            {"type": "M3UPLAYLIST", "name": PROVIDER_NAME, "url": "missing.m3u"}
        ]
        self.window.set_provider(force_update=True)
        self.assertFalse(card.isVisible())


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
        self.assertFalse(hasattr(dialog, "smooth_paused_seek_checkbox"))
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
