import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path
import threading
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from test_tv_window import WindowCase, wait_until

from services.remote.protocol import Closed, Code, Paired, Press, Status, StoreSaved
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
        self.deliver(Press("s1", "down", True, 1))
        QTest.qWait(530)  # 400 ms, then every 40 ms; no keep-alive: stops at 600 ms
        remote.bridge.post(Press("s1", "down", False, 5))
        self.assertTrue(wait_until(lambda: len(fired) >= 5))
        QTest.qWait(200)
        self.assertEqual(fired, ["down"] * 5)


if __name__ == "__main__":
    unittest.main()
