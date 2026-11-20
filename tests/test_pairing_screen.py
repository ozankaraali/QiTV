"""The phone remote's pairing screen over the TV, the menu's Phone Remote… entry, and a phone
removed while the remote is off (Settings › Phone remote itself: tests/test_settings_remote.py).
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from services.config_manager import ConfigManager
from services.remote.protocol import Paired
from tests import test_tv_window as base

DEVICE = {"id": "d1", "name": "Phone A", "paired_at": 1.0, "last_used": 2.0, "keys": []}
URL = "http://192.168.1.20:5384/#1.-.AAAA.BBBB"


class RemoveDeviceTests(base.WindowCase):
    def test_remove_without_the_remote_running_edits_the_config(self):
        self.config.remote = {"version": 1, "devices": [DEVICE]}
        self.window.remote.remove_device("d1")
        self.assertEqual(ConfigManager().remote["devices"], [])


class PairingScreenTests(base.WindowCase):
    def test_overlay_opens_pairing_and_esc_closes(self):
        self.show()
        overlay = self.window.pair_overlay
        with mock.patch.object(self.window.remote, "set_pairing") as pairing:
            overlay.open()
            self.assertTrue(overlay.isVisible())
            pairing.assert_called_with(True)
            QTest.keyClick(self.window, Qt.Key.Key_Escape)
            self.assertFalse(overlay.isVisible())
            pairing.assert_called_with(False)

    def test_a_click_closes_it(self):
        self.show()
        overlay = self.window.pair_overlay
        with mock.patch.object(self.window.remote, "set_pairing"):
            overlay.open()
            QTest.mouseClick(overlay, Qt.MouseButton.LeftButton)
        self.assertFalse(overlay.isVisible())

    def test_qr_encodes_the_code_url(self):
        self.show()
        overlay = self.window.pair_overlay
        with mock.patch.object(self.window.remote, "set_pairing"):
            overlay.open()
        self.window.remote.codeChanged.emit(URL)
        self.assertEqual(overlay.url(), URL)
        self.assertGreaterEqual(overlay.module_count(), 21)
        overlay.grab()  # paints without error
        self.window.remote.codeChanged.emit("")
        self.assertEqual(overlay.module_count(), 0)
        overlay.grab()

    def test_paired_closes_the_pairing_screen(self):
        self.show()
        overlay = self.window.pair_overlay
        with mock.patch.object(self.window.remote, "set_pairing"):
            overlay.open()
            self.window.remote.bridge.post(Paired("s1", "d1", "Phone A"))
            self.assertTrue(base.wait_until(lambda: not overlay.isVisible()))

    def test_menu_has_phone_remote(self):
        self.assertIn("Phone Remote…", self.menu_actions())

    def test_phone_remote_when_off_turns_it_on_and_shows_the_code(self):
        self.show()
        with mock.patch("services.remote.hub.RemoteHub") as hub:
            self.window.open_phone_remote()
        self.assertTrue(ConfigManager().remote["enabled"])
        hub.return_value.start.assert_called_once()
        self.assertTrue(self.window.pair_overlay.isVisible())
        self.window.remote.hub = None  # the stub isn't a real thread to stop

    def test_phone_remote_sits_in_the_mac_app_menu(self):
        from PySide6.QtGui import QAction

        action = self.menu_actions()["Phone Remote…"]
        self.assertEqual(action.menuRole(), QAction.MenuRole.ApplicationSpecificRole)

    def test_phone_remote_when_on_shows_the_code(self):
        self.show()
        self.window.remote.hub = mock.Mock()
        self.addCleanup(setattr, self.window.remote, "hub", None)
        self.window.open_phone_remote()
        self.assertTrue(self.window.pair_overlay.isVisible())
        self.window.remote.hub.set_pairing.assert_called_with(True)


class PairingNoticeTests(base.WindowCase):
    def test_overlay_says_when_the_remote_could_not_start(self):
        from services.remote.protocol import Status

        self.show()
        overlay = self.window.pair_overlay
        with mock.patch.object(self.window.remote, "set_pairing"):
            overlay.open()
        self.window.remote.bridge.post(Status("", "error"))
        self.assertTrue(overlay.error_text())
        overlay.grab()

    def test_a_code_only_this_network_can_use_says_so(self):
        # The relay out of reach: the code opens the computer's own address, which a phone on
        # another network (or a guest Wi-Fi's) never reaches.
        from services.remote.protocol import Code, Status

        self.show()
        overlay = self.window.pair_overlay
        with mock.patch.object(self.window.remote, "set_pairing"):
            overlay.open()
        post = self.window.remote.bridge.post
        post(Status("http://192.168.1.20:5384/", "away"))
        post(Code(URL))
        self.assertIn("remote.qitv.app", overlay.note_text())
        overlay.grab()
        post(Status("http://192.168.1.20:5384/", "online"))
        post(Code("https://remote.qitv.app/#1.ROOM.AAAA.BBBB"))
        self.assertEqual(overlay.note_text(), "")
        post(Status("http://192.168.1.20:5384/", "off"))  # the relay turned off in Settings
        post(Code(URL))
        self.assertEqual(overlay.note_text(), "")


class QrTests(base.WindowCase):
    def test_qr_has_room_for_the_logo(self):
        self.show()
        overlay = self.window.pair_overlay
        with mock.patch.object(self.window.remote, "set_pairing"):
            overlay.open()
        url = "https://remote.qitv.app/#1." + "R" * 32 + "." + "P" * 43 + "." + "C" * 22
        self.window.remote.codeChanged.emit(url)
        import segno

        self.assertEqual(overlay.module_count(), len(segno.make(url, error="h").matrix))
        overlay.grab()


if __name__ == "__main__":
    unittest.main()
