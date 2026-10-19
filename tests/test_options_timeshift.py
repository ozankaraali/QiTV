import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog

from services import recording
from services.config_manager import ConfigManager
from tests import test_tv_window as base
from ui.settings_dialog import OptionsDialog


class TimeshiftSettingsTests(base.WindowCase):
    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def tab_titles(self, dialog):
        return [dialog.options_tab.tabText(i) for i in range(dialog.options_tab.count())]

    def test_time_shift_tab_exists_with_the_new_copy(self):
        dialog = self.dialog()
        self.assertIn("Time-shift", self.tab_titles(dialog))
        self.assertEqual(
            dialog.timeshift_checkbox.text(),
            "Use disk for a longer time-shift buffer.\nQiTV deletes the oldest data "
            "automatically and cleans up when you stop, change channel or quit.",
        )
        self.assertEqual(dialog.timeshift_size_slider.minimum(), 256)
        self.assertEqual(dialog.timeshift_size_slider.maximum(), 16384)
        # The time-shift tab's copy (Settings names MPV for the external players).
        tab = dialog.timeshift_checkbox.parentWidget()
        self.assertEqual(tab.objectName(), "timeshiftSettings")
        texts = " ".join(
            label.text() for label in tab.findChildren(type(dialog.timeshift_size_label))
        )
        self.assertIn("Without disk, each picture keeps its memory per stream", texts)
        self.assertNotIn("MPV", texts)
        self.assertNotIn("MPV", dialog.timeshift_checkbox.text())

    def test_the_slider_follows_consent(self):
        dialog = self.dialog()
        group = dialog.timeshift_size_slider.parentWidget()
        self.assertFalse(dialog.timeshift_checkbox.isChecked())
        self.assertFalse(group.isEnabled())
        dialog.timeshift_checkbox.setChecked(True)
        self.assertTrue(group.isEnabled())

    def test_saving_writes_both_keys(self):
        dialog = self.dialog()
        dialog.timeshift_checkbox.setChecked(True)
        dialog.timeshift_size_slider.setValue(4096)
        dialog.save_settings()
        saved = ConfigManager()
        self.assertTrue(saved.timeshift_enabled)
        self.assertEqual(saved.timeshift_max_mib, 4096)

    def test_memory_per_stream_offers_three_sizes_with_its_rewind(self):
        dialog = self.dialog()
        combo = dialog.timeshift_ram_combo
        self.assertEqual([combo.itemData(i) for i in range(combo.count())], [128, 256, 512])
        self.assertEqual(combo.currentData(), 256)
        self.assertTrue(combo.isEnabled())  # it is for every stream not on disk
        self.assertEqual(dialog.timeshift_ram_estimate.text(), "≈ 4 min of HD at 8 Mb/s")
        combo.setCurrentIndex(combo.findData(512))
        self.assertEqual(dialog.timeshift_ram_estimate.text(), "≈ 9 min of HD at 8 Mb/s")
        dialog.save_settings()
        self.assertEqual(ConfigManager().timeshift_ram_mib, 512)

    def test_memory_per_stream_loads_the_saved_size(self):
        self.config.timeshift_ram_mib = 128
        dialog = self.dialog()
        self.assertEqual(dialog.timeshift_ram_combo.currentData(), 128)
        self.assertEqual(dialog.timeshift_ram_estimate.text(), "≈ 2 min of HD at 8 Mb/s")

    def test_cancel_discards_changes(self):
        dialog = self.dialog()
        dialog.timeshift_checkbox.setChecked(True)
        dialog.timeshift_size_slider.setValue(8192)
        dialog.reject()
        saved = ConfigManager()
        self.assertFalse(saved.timeshift_enabled)
        self.assertEqual(saved.timeshift_max_mib, 2048)

    def test_the_recordings_folder_is_chosen_saved_and_reset(self):
        # 2.3: an optional key; without it, the system's Movies (Videos) folder's QiTV.
        dialog = self.dialog()
        self.assertEqual(dialog.recordings_folder.text(), recording.default_folder())
        self.assertFalse(dialog.recordings_default_button.isEnabled())
        dialog.save_settings()
        self.assertNotIn("recordings_dir", ConfigManager().config)  # unchanged: not written
        chosen = os.path.join(self.config_dir, "Rec")
        with mock.patch.object(QFileDialog, "getExistingDirectory", return_value=chosen):
            dialog.recordings_change_button.click()
        self.assertEqual(dialog.recordings_folder.text(), chosen)
        dialog.save_settings()
        self.assertEqual(ConfigManager().recordings_dir, chosen)

        dialog = self.dialog()  # it opens with the saved one
        self.assertEqual(dialog.recordings_folder.text(), chosen)
        self.assertTrue(dialog.recordings_default_button.isEnabled())
        with mock.patch.object(QFileDialog, "getExistingDirectory", return_value=""):
            dialog.recordings_change_button.click()  # cancelled: it stays
        self.assertEqual(dialog.recordings_folder.text(), chosen)
        dialog.recordings_default_button.click()
        self.assertEqual(dialog.recordings_folder.text(), recording.default_folder())
        dialog.save_settings()
        self.assertEqual(ConfigManager().recordings_dir, "")

    def test_open_folder_shows_the_folder_or_says_there_is_none_yet(self):
        dialog = self.dialog()
        with mock.patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            dialog.recordings_open_button.click()
        self.assertEqual(opened.call_args.args[0].toLocalFile(), recording.default_folder())
        self.assertEqual(dialog.recordings_status.text(), "")
        with mock.patch.object(QDesktopServices, "openUrl", return_value=False):
            dialog.recordings_open_button.click()
        self.assertEqual(dialog.recordings_status.text(), "There are no recordings yet")

    def test_fresh_install_gets_the_defaults(self):
        defaults = ConfigManager.default_config()
        self.assertIs(defaults["timeshift_enabled"], False)
        self.assertEqual(defaults["timeshift_max_mib"], 2048)
        dialog = self.dialog()
        self.assertFalse(dialog.timeshift_checkbox.isChecked())
        self.assertEqual(dialog.timeshift_size_slider.value(), 2048)

    def test_the_slider_loads_the_saved_size(self):
        self.config.timeshift_enabled = True
        self.config.timeshift_max_mib = 8192
        dialog = self.dialog()
        self.assertTrue(dialog.timeshift_checkbox.isChecked())
        self.assertEqual(dialog.timeshift_size_slider.value(), 8192)
        self.assertEqual(dialog.timeshift_1080p_estimate.text(), "About 2 h 23 min")
        self.assertEqual(dialog.timeshift_4k_estimate.text(), "About 46 min")


class EpgExpirationSettingsTests(base.WindowCase):
    """The guide's update period offers units the guide knows, saved as it reads them."""

    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def units(self, dialog):
        combo = dialog.epg_expiration_combo
        return [combo.itemText(i) for i in range(combo.count())]

    def test_it_offers_hours_days_weeks_and_months(self):
        dialog = self.dialog()
        self.assertEqual(self.units(dialog), ["Hours", "Days", "Weeks", "Months"])
        dialog.epg_expiration_spinner.setValue(2)
        dialog.epg_expiration_combo.setCurrentText("Weeks")
        dialog.save_settings()
        saved = ConfigManager()
        self.assertEqual(saved.epg_expiration_unit, "Weeks")
        self.assertEqual(saved.epg_expiration, 2 * 7 * 24 * 60 * 60)

    def test_the_old_spelling_opens_as_months_and_saves_so(self):
        self.config.config["epg_expiration_unit"] = "Monthes"
        dialog = self.dialog()
        self.assertEqual(dialog.epg_expiration_combo.currentText(), "Months")
        dialog.save_settings()
        self.assertEqual(ConfigManager().config["epg_expiration_unit"], "Months")

    def test_a_saved_minutes_period_is_kept(self):
        self.config.config["epg_expiration_unit"] = "Minutes"
        dialog = self.dialog()
        self.assertEqual(self.units(dialog), ["Minutes", "Hours", "Days", "Weeks", "Months"])
        self.assertEqual(dialog.epg_expiration_combo.currentText(), "Minutes")
        dialog.save_settings()
        self.assertEqual(ConfigManager().epg_expiration_unit, "Minutes")


class MaxStreamsTests(base.WindowCase):
    def test_max_streams_round_trips_through_the_provider_editor(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        spin = dialog.max_streams_input
        self.assertEqual((spin.minimum(), spin.maximum()), (0, 10))
        self.assertEqual(spin.specialValueText(), "Automatic")
        self.assertEqual(spin.value(), 0)
        spin.setValue(3)
        dialog.apply_provider()
        self.assertEqual(dialog.edited_provider["max_streams"], 3)
        dialog.save_settings()
        self.assertEqual(self.provider_manager.providers[0]["max_streams"], 3)
        again = OptionsDialog(self.window)
        self.addCleanup(again.deleteLater)
        self.assertEqual(again.max_streams_input.value(), 3)

    def test_a_hand_edited_bad_value_opens_as_automatic(self):
        for bad in ("many", [], {}, float("inf"), float("nan"), None, -3, 99):
            with self.subTest(bad=bad):
                self.provider_manager.providers[0]["max_streams"] = bad
                dialog = OptionsDialog(self.window)
                self.addCleanup(dialog.deleteLater)
                expected = 10 if bad == 99 else 0
                self.assertEqual(dialog.max_streams_input.value(), expected)

    def test_automatic_is_stored_as_zero(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        dialog.max_streams_input.setValue(2)
        dialog.max_streams_input.setValue(0)
        dialog.apply_provider()
        self.assertEqual(dialog.edited_provider.get("max_streams", 0), 0)


class ExternalPlayerSettingsTests(base.WindowCase):
    """Playing in VLC or MPV is picked in-app; Settings keeps it as a mode."""

    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_the_options_read_as_a_mode_with_a_hint(self):
        dialog = self.dialog()
        self.assertEqual(dialog.play_in_vlc_checkbox.text(), "Always play in VLC")
        self.assertEqual(dialog.play_in_mpv_checkbox.text(), "Always play in MPV")
        self.assertEqual(
            dialog.external_players_hint.text(),
            "Any channel or movie can also go to VLC or MPV from its right-click menu or the"
            " player controls.",
        )
        self.assertFalse(dialog.play_in_vlc_checkbox.isChecked())
        self.assertFalse(dialog.play_in_mpv_checkbox.isChecked())

    def test_one_player_at_most_is_saved(self):
        dialog = self.dialog()
        dialog.play_in_vlc_checkbox.setChecked(True)
        dialog.play_in_mpv_checkbox.setChecked(True)  # the controller would prefer VLC
        self.assertFalse(dialog.play_in_vlc_checkbox.isChecked())
        dialog.save_settings()
        saved = ConfigManager()
        self.assertFalse(saved.play_in_vlc)
        self.assertTrue(saved.play_in_mpv)

    def test_a_saved_mode_opens_checked_and_can_be_turned_off(self):
        self.config.play_in_vlc = True
        dialog = self.dialog()
        self.assertTrue(dialog.play_in_vlc_checkbox.isChecked())
        dialog.play_in_vlc_checkbox.setChecked(False)
        dialog.save_settings()
        self.assertFalse(ConfigManager().play_in_vlc)


if __name__ == "__main__":
    unittest.main()
