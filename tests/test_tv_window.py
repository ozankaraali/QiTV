import os
from pathlib import Path
import tempfile
from typing import Optional
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from config_manager import ConfigManager
from epg_manager import EpgManager
from image_manager import ImageManager
from options import OptionsDialog
from provider_manager import ProviderManager
from services.playback import PlaybackState, create_instance
from services.thread_cleanup import has_pending_threads
from tests.fixtures.local_provider import channel_urls, seed_channels, write_local_provider
from ui.overlay_host import OverlayHost
from ui.overlays.drawer import ERROR_TEXT
from ui.tv_controller import WELCOME_TEXT
from ui.tv_window import NO_VIDEO_TEXT, TVWindow

INSTANCE = create_instance(["--vout=dummy", "--aout=dummy", "--quiet"])
NEEDS_VLC = unittest.skipIf(INSTANCE is None, "libVLC is not available")
HOST_METHODS = ("save_config", "set_provider", "refresh_channels", "refresh_content_list_size")
HOST_ATTRIBUTES = ("config_manager", "provider_manager", "epg_manager", "image_manager")


def wait_until(predicate, timeout_ms=10000):
    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(20)
        waited += 20
    return predicate()


class WindowCase(unittest.TestCase):
    """A TVWindow over the local fixture provider in a temporary QITV_CONFIG_DIR."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def provider_list(self) -> Optional[list[dict]]:
        return None  # the fixture provider

    def make_window(self) -> TVWindow:
        return TVWindow(
            self.app,
            self.config,
            self.provider_manager,
            self.image_manager,
            self.epg_manager,
            vlc_instance=INSTANCE,
        )

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

    def close_window(self):
        # The close runs in steps (stop, threads, release); close() returns True once accepted.
        self.assertTrue(wait_until(self.window.close))
        self.assertFalse(self.window.isVisible())
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


@NEEDS_VLC
class TVWindowTests(WindowCase):
    def session_on(self, url, states=(PlaybackState.OPENING, PlaybackState.PLAYING)):
        session = self.window.session
        return lambda: session.url == url and session.state() in states

    def test_constructs_and_shows_with_the_overlay_host(self):
        self.show()
        self.assertIsInstance(self.window.host, OverlayHost)
        self.assertIs(self.window.centralWidget(), self.window.surface)

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

    def test_navigation_notices_are_toasted(self):
        text = "Couldn't refresh Live TV; showing saved channels"
        self.window.nav.notice.emit(text)
        self.assertEqual(self.toasts, [(text, "info")])

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

    def test_close_stops_then_releases_vlc_before_the_window_closes(self):
        # On macOS libVLC removes its video view on the run loop after a stop; closing the
        # window in the same call crashed.
        self.show()
        self.wait_for_drawer()
        session = self.window.session
        self.addCleanup(session.release)
        calls = []
        with (
            mock.patch.object(session, "stop", side_effect=lambda: calls.append("stop")),
            mock.patch.object(session, "release", side_effect=lambda: calls.append("release")),
        ):
            self.assertFalse(self.window.close())
            self.assertEqual(calls, ["stop"])
            self.assertTrue(self.window.isVisible())
            self.assertTrue(wait_until(lambda: not self.window.isVisible()))
        self.assertEqual(calls, ["stop", "release"])

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

    def menu_actions(self):
        return {a.text(): a for m in self.window.menuBar().actions() for a in m.menu().actions()}

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

    def test_host_is_attached_again_every_time_it_shows(self):
        # macOS drops a child window from its parent when the child is ordered out.
        with mock.patch("ui.tv_window.attach_child_window", return_value=True) as attach:
            self.show()
            self.wait_for_drawer()
            self.assertTrue(wait_until(lambda: attach.call_count == 1))
            QTest.keyClick(self.window, Qt.Key.Key_Escape)
            self.assertFalse(self.window.host.isVisible())
            QTest.keyClick(self.window, Qt.Key.Key_Return)  # the drawer again
            self.assertTrue(wait_until(lambda: attach.call_count == 2))
        attach.assert_called_with(self.window, self.window.host)

    def test_video_is_not_bound_without_native_window_ids(self):
        # libVLC crashes drawing into the offscreen platform's fake window ids.
        with mock.patch.object(self.window.session, "set_window") as set_window:
            self.show()
            self.wait_for_drawer()
        set_window.assert_not_called()

    def test_banner_shows_a_logo_that_arrives_later(self):
        self.open_first_channel()
        banner = self.window.overlays.banner
        self.assertTrue(wait_until(lambda: banner.info is not None and banner.isVisible()))
        self.assertIsNone(banner.info.logo)
        url = "http://logos.invalid/one.png"
        self.window.zap.context.channels[0]["logo"] = url
        logo = QPixmap(8, 8)
        with mock.patch.object(
            self.window.logos, "pixmap", side_effect=lambda u: logo if u == url else None
        ):
            self.window.logos.logoReady.emit("http://logos.invalid/other.png")
            self.assertIsNone(banner.info.logo)
            self.window.logos.logoReady.emit(url)
        self.assertIs(banner.info.logo, logo)
        self.assertEqual(banner.info.name, "Channel 1")

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

    def test_mini_player_shrinks_to_the_corner_and_restores(self):
        self.show()
        self.window.setGeometry(100, 100, 960, 540)
        self.assertTrue(wait_until(lambda: self.window.geometry().width() == 960))
        before = self.window.geometry()
        QTest.keyClick(self.window, Qt.Key.Key_P, Qt.KeyboardModifier.AltModifier)
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
        QTest.keyClick(self.window, Qt.Key.Key_P, Qt.KeyboardModifier.AltModifier)
        self.assertFalse(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertFalse(self.window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(wait_until(lambda: self.window.geometry() == before))


@NEEDS_VLC
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


@NEEDS_VLC
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


class NoVlcTests(WindowCase):
    def make_window(self):
        with (
            mock.patch("ui.tv_window.create_instance", return_value=None),
            self.assertLogs("ui.tv_window", "WARNING"),
        ):
            return TVWindow(
                self.app,
                self.config,
                self.provider_manager,
                self.image_manager,
                self.epg_manager,
            )

    def test_without_libvlc_playing_explains_why(self):
        self.open_first_channel()
        self.assertTrue(
            wait_until(
                lambda: any(kind == "error" and NO_VIDEO_TEXT in text for text, kind in self.toasts)
            )
        )


@NEEDS_VLC
class OptionsDialogTests(WindowCase):
    def test_settings_load_and_save_the_tv_options(self):
        self.config.live_latency = "stable"
        self.config.mouse_wheel_action = "volume"
        self.config.start_with_last_channel = False
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
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
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        self.assertAlmostEqual(dialog.get_cache_size(), expected, places=3)


if __name__ == "__main__":
    unittest.main()
