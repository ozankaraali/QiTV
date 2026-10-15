import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from config_manager import ConfigManager
from options import OptionsDialog
from tests import test_tv_window as base


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
        texts = " ".join(
            label.text() for label in dialog.findChildren(type(dialog.timeshift_size_label))
        )
        self.assertIn("Without disk, each live channel keeps about two minutes in memory.", texts)
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

    def test_the_slider_loads_the_saved_size(self):
        self.config.timeshift_enabled = True
        self.config.timeshift_max_mib = 8192
        dialog = self.dialog()
        self.assertTrue(dialog.timeshift_checkbox.isChecked())
        self.assertEqual(dialog.timeshift_size_slider.value(), 8192)
        self.assertEqual(dialog.timeshift_1080p_estimate.text(), "About 2 h 23 min")


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

    def test_automatic_is_stored_as_zero(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        dialog.max_streams_input.setValue(2)
        dialog.max_streams_input.setValue(0)
        dialog.apply_provider()
        self.assertEqual(dialog.edited_provider.get("max_streams", 0), 0)


if __name__ == "__main__":
    unittest.main()
