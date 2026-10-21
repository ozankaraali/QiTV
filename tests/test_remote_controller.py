import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path
import threading
import unittest
from unittest import mock

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from test_tv_window import WindowCase, wait_until

from services.remote.protocol import Closed, Code, Held, Paired, Press, Status, StoreSaved
from ui.keymap import Action


class RemoteDispatchTests(WindowCase):
    def remote(self):
        return self.window.remote

    def test_arrow_moves_like_keyboard_in_drawer(self):
        self.show()
        drawer = self.wait_for_drawer()  # no last channel: the drawer opens on the groups
        self.remote().fire("ok")  # into the highlighted group, as Enter does

        def on_an_item():
            node = drawer.highlighted()
            return node is not None and node.layer == "item"

        self.assertTrue(wait_until(on_an_item))
        self.assertEqual(drawer.highlighted().title, "Channel 1")
        self.remote().fire("down")
        self.assertTrue(wait_until(lambda: drawer.highlighted().title == "Channel 2"))
        QTest.keyClick(self.window, Qt.Key.Key_Up)  # the keyboard agrees on where it is
        self.assertTrue(wait_until(lambda: drawer.highlighted().title == "Channel 1"))

    def test_volume_in_library_stays_in_library(self):
        self.show()
        self.window.screens.open_library("movies")
        self.assertTrue(wait_until(self.window.screens.library_shown))
        controller = self.window.controller
        with mock.patch.object(controller, "handle", wraps=controller.handle) as handle:
            self.remote().fire("volup")
        handle.assert_called_once_with(Action.VOL_UP, "")
        self.assertTrue(self.window.screens.library_shown())

    def test_channel_in_library_returns_to_tv(self):
        self.show()
        self.window.screens.open_library("movies")
        self.assertTrue(wait_until(self.window.screens.library_shown))
        self.remote().fire("chdown")
        self.assertTrue(wait_until(lambda: not self.window.screens.library_shown()))

    def test_pip_closes_the_small_picture_whatever_the_tv_shows(self):
        self.show()
        self.wait_for_drawer()  # the channel list is open: P there opens its highlighted channel
        controller = self.window.controller
        with (
            mock.patch.object(controller, "pip_shown", return_value=True),
            mock.patch.object(controller, "close_pip") as close,
            mock.patch.object(controller, "handle") as handle,
        ):
            self.remote().fire("pip")
        close.assert_called_once_with()
        handle.assert_not_called()

    def test_pip_opens_as_the_p_key_while_none_is_open(self):
        self.show()
        controller = self.window.controller
        with mock.patch.object(controller, "handle") as handle:
            self.remote().fire("pip")
        handle.assert_called_once_with(Action.PIP, "")

    def test_fullscreen_brings_qitv_forward_first_from_the_background(self):
        """macOS ignores a fullscreen change asked of a window in the background (the phone's
        button while another app is in front): QiTV comes forward, then changes."""
        window = self.window
        active = [False]
        with (
            mock.patch("ui.tv_window.app_active", side_effect=lambda: active[0]),
            mock.patch.object(window, "activateWindow") as activate,
            mock.patch.object(window, "toggle_fullscreen") as toggle,
        ):
            self.remote().fire("fullscreen")
            activate.assert_called_once()
            toggle.assert_not_called()  # not yet: macOS would drop it
            active[0] = True
            self.assertTrue(wait_until(lambda: toggle.called))
        toggle.assert_called_once()

    def test_fullscreen_from_the_background_changes_even_if_qitv_stays_behind(self):
        window = self.window
        with (
            mock.patch("ui.tv_window.app_active", return_value=False),
            mock.patch.object(window, "activateWindow"),
            mock.patch.object(window, "toggle_fullscreen") as toggle,
        ):
            self.remote().fire("fullscreen")
            self.assertTrue(wait_until(lambda: toggle.called, timeout_ms=3000))
        toggle.assert_called_once()

    def test_fullscreen_in_front_changes_at_once(self):
        window = self.window
        with (
            mock.patch("ui.tv_window.app_active", return_value=True),
            mock.patch.object(window, "activateWindow") as activate,
            mock.patch.object(window, "toggle_fullscreen") as toggle,
        ):
            self.remote().fire("fullscreen")
        toggle.assert_called_once()
        activate.assert_not_called()

    def test_movies_and_tv_screens(self):
        self.show()
        self.remote().fire("movies")
        self.assertTrue(wait_until(self.window.screens.library_shown))
        self.assertEqual(self.window.library_controller.section, "movies")
        self.remote().fire("tv")
        self.assertTrue(wait_until(lambda: not self.window.screens.library_shown()))

    def test_a_press_from_another_thread_reaches_the_gui(self):
        self.show()
        self.wait_for_drawer()
        fired = []
        self.remote().fire = fired.append  # what a press turns into
        bridge = self.remote().bridge
        thread = threading.Thread(target=bridge.post, args=(Press("s1", "ok", True, 1),))
        thread.start()
        thread.join()
        self.assertTrue(wait_until(lambda: fired == ["ok"]))


class RemoteEventTests(WindowCase):
    def deliver(self, event):
        self.window.remote.bridge.post(event)
        QTest.qWait(10)

    def test_remote_off_by_default_starts_nothing(self):
        self.window.remote.apply_config()
        self.assertIsNone(self.window.remote.hub)

    def test_store_saved_persists_config(self):
        data = {"version": 1, "enabled": False, "devices": [], "relay": True}
        self.deliver(StoreSaved(data))
        self.assertEqual(self.window.config_manager.remote, data)
        saved = json.loads(Path(self.window.config_manager.config_path).read_text())
        self.assertEqual(saved["remote"], data)

    def test_devices_and_code_and_status_signals(self):
        got = {}
        remote = self.window.remote
        remote.devicesChanged.connect(lambda devices: got.setdefault("devices", devices))
        remote.codeChanged.connect(lambda url: got.setdefault("code", url))
        remote.statusChanged.connect(lambda status: got.setdefault("status", status))
        device = {"id": "d1", "name": "Phone A", "paired_at": 1.0, "last_used": 2.0, "keys": []}
        self.deliver(StoreSaved({"version": 1, "devices": [device]}))
        self.deliver(Code("http://192.168.1.20:5384/#1.-.a.b"))
        self.deliver(Status("http://192.168.1.20:5384/", "off"))
        self.assertEqual(got["devices"], [{"id": "d1", "name": "Phone A", "last_used": 2.0}])
        self.assertEqual(got["code"], "http://192.168.1.20:5384/#1.-.a.b")
        self.assertEqual(got["status"], {"home_url": "http://192.168.1.20:5384/", "relay": "off"})

    def test_paired_toast(self):
        self.deliver(Paired("s1", "d1", "Phone A"))
        self.assertIn(("Phone A can now control QiTV", "info"), self.toasts)

    def test_closed_drops_the_hold(self):
        fired = []
        remote = self.window.remote
        remote.fire = fired.append
        self.deliver(Press("s1", "down", True, 1))
        self.deliver(Closed("s1", "d1"))
        QTest.qWait(700)
        self.assertEqual(fired, ["down"])

    def test_hold_repeats_on_the_gui_thread(self):
        fired = []
        remote = self.window.remote
        remote.fire = fired.append
        alive = QTimer()  # the phone's keep-alive while the button is down
        alive.setInterval(100)
        alive.timeout.connect(lambda: remote.bridge.post(Held("s1")))
        self.deliver(Press("s1", "down", True, 1))
        alive.start()
        QTest.qWait(530)  # one press, then from 400 ms one every 40 ms
        alive.stop()
        remote.bridge.post(Press("s1", "down", False, 5))
        self.assertTrue(wait_until(lambda: len(fired) >= 5))
        QTest.qWait(200)
        # Where the phone let go, however late this machine's timers ran: overshoot steps back.
        self.assertEqual(fired.count("down") - fired.count("up"), 5)
        self.assertTrue(set(fired) <= {"down", "up"})


class ReviewFixControllerTests(WindowCase):
    def deliver(self, event):
        self.window.remote.bridge.post(event)
        QTest.qWait(10)

    def test_store_saved_keeps_the_settings_switches(self):
        device = {"id": "d1", "name": "Phone A", "paired_at": 1.0, "last_used": 2.0, "keys": []}
        self.window.config_manager.remote = {
            "version": 1,
            "enabled": True,
            "relay": False,
            "relay_url": "https://relay.example",
        }
        self.deliver(
            StoreSaved(
                {
                    "version": 1,
                    "enabled": False,
                    "relay": True,
                    "relay_url": "https://remote.qitv.app",
                    "devices": [device],
                }
            )
        )
        saved = self.window.config_manager.remote
        self.assertEqual((saved["enabled"], saved["relay"]), (True, False))
        self.assertEqual(saved["relay_url"], "https://relay.example")
        self.assertEqual(saved["devices"], [device])

    def test_removal_toast_waits_for_the_hub(self):
        from unittest import mock as _mock

        device = {"id": "d1", "name": "Phone A", "paired_at": 1.0, "last_used": 2.0, "keys": []}
        self.window.config_manager.remote = {"version": 1, "devices": [device]}
        self.window.remote.apply_config = lambda: None
        self.window.remote._names_from(self.window.config_manager.remote)
        self.window.remote.hub = _mock.Mock()
        self.addCleanup(setattr, self.window.remote, "hub", None)
        self.window.remote.remove_device("d1")
        self.assertNotIn(("Phone A can no longer control QiTV", "info"), self.toasts)
        self.deliver(StoreSaved({"version": 1, "devices": []}))
        self.assertIn(("Phone A can no longer control QiTV", "info"), self.toasts)


if __name__ == "__main__":
    unittest.main()
