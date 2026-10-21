"""Settings → Phone remote, the pairing screen over the TV and the menu's Phone Remote… entry."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from services.config_manager import ConfigManager
from services.remote.protocol import Paired
from tests import test_tv_window as base
from ui.settings_dialog import OptionsDialog

DEVICE = {"id": "d1", "name": "Phone A", "paired_at": 1.0, "last_used": 2.0, "keys": []}
URL = "http://192.168.1.20:5384/#1.-.AAAA.BBBB"


class RemoteSettingsTests(base.WindowCase):
    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_tab_reflects_and_saves(self):
        dialog = self.dialog()
        titles = [dialog.options_tab.tabText(i) for i in range(dialog.options_tab.count())]
        self.assertIn("Phone remote", titles)
        self.assertEqual(dialog.remote_tab.objectName(), "remoteSettings")
        self.assertFalse(dialog.remote_enabled_checkbox.isChecked())
        self.assertTrue(dialog.remote_relay_checkbox.isChecked())
        dialog.remote_enabled_checkbox.setChecked(True)
        with mock.patch("services.remote.hub.RemoteHub") as hub:
            dialog.save_settings()
        saved = ConfigManager().remote
        self.assertEqual((saved["version"], saved["enabled"], saved["relay"]), (1, True, True))
        hub.return_value.start.assert_called_once()
        self.window.remote.hub = None  # the stub isn't a real thread to stop

    def test_saving_keeps_paired_phones(self):
        self.config.remote = {"version": 1, "enabled": False, "devices": [DEVICE], "room": "r"}
        dialog = self.dialog()
        dialog.remote_relay_checkbox.setChecked(False)
        dialog.save_settings()
        saved = ConfigManager().remote
        self.assertEqual(saved["devices"], [DEVICE])
        self.assertEqual(saved["room"], "r")
        self.assertFalse(saved["relay"])

    def test_tab_lists_phones_and_remove_calls_remote(self):
        self.config.remote = {"version": 1, "devices": [DEVICE]}
        dialog = self.dialog()
        phones = dialog.remote_phones_list
        self.assertEqual(phones.count(), 1)
        self.assertIn("Phone A", phones.item(0).text())
        self.assertFalse(dialog.remote_remove_button.isEnabled())
        phones.setCurrentRow(0)
        self.assertTrue(dialog.remote_remove_button.isEnabled())
        with mock.patch.object(self.window.remote, "remove_device") as remove:
            dialog.remote_remove_button.click()
        remove.assert_called_once_with("d1")
        self.assertEqual(phones.count(), 0)

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

    def test_phone_remote_when_off_opens_its_settings(self):
        with mock.patch.object(self.window, "options_dialog") as options:
            self.window.open_phone_remote()
        options.assert_called_once_with(tab="remote")
        self.assertFalse(self.window.pair_overlay.isVisible())

    def test_phone_remote_when_on_shows_the_code(self):
        self.show()
        self.window.remote.hub = mock.Mock()
        self.addCleanup(setattr, self.window.remote, "hub", None)
        self.window.open_phone_remote()
        self.assertTrue(self.window.pair_overlay.isVisible())
        self.window.remote.hub.set_pairing.assert_called_with(True)


class ReviewFixSettingsTests(base.WindowCase):
    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_relay_status_follows(self):
        dialog = self.dialog()
        remote = self.window.remote
        remote.statusChanged.emit({"home_url": "http://192.168.1.20:5384/", "relay": "resting"})
        self.assertIn("daily limit", dialog.remote_relay_label.text())
        remote.statusChanged.emit({"home_url": "http://192.168.1.20:5384/", "relay": "online"})
        self.assertEqual(dialog.remote_relay_label.text(), "Connected")
        remote.statusChanged.emit({"home_url": "", "relay": "replaced"})
        self.assertIn("Another QiTV", dialog.remote_relay_label.text())

    def test_phone_list_follows_pairings_while_open(self):
        dialog = self.dialog()
        self.window.remote.devicesChanged.emit([{"id": "d9", "name": "Phone B", "last_used": 1.0}])
        self.assertIn("Phone B", dialog.remote_phones_list.item(0).text())

    def test_a_closed_dialog_leaves_no_listeners(self):
        from PySide6.QtCore import QCoreApplication, QEvent

        dialog = OptionsDialog(self.window)
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        with mock.patch("sys.excepthook") as hook:  # PySide reports a slot's error there
            self.window.remote.statusChanged.emit({"home_url": "", "relay": "off"})
            self.window.remote.devicesChanged.emit([])
        hook.assert_not_called()

    def test_a_newer_builds_settings_are_left_alone(self):
        newer = {"version": 2, "enabled": True, "future": "kept"}
        self.config.remote = dict(newer)
        dialog = self.dialog()
        self.assertFalse(dialog.remote_enabled_checkbox.isEnabled())
        dialog.save_settings()
        self.assertEqual(ConfigManager().remote, newer)
        self.window.remote.apply_config()
        self.assertIsNone(self.window.remote.hub)

    def test_overlay_says_when_the_remote_could_not_start(self):
        from services.remote.protocol import Status

        self.show()
        overlay = self.window.pair_overlay
        with mock.patch.object(self.window.remote, "set_pairing"):
            overlay.open()
        self.window.remote.bridge.post(Status("", "error"))
        self.assertTrue(overlay.error_text())
        overlay.grab()


if __name__ == "__main__":
    unittest.main()
