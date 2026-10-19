"""Settings → Language: the system's default, English and each catalog's language; the choice
is an optional config key that applies at the next start."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services import i18n
from services.config_manager import ConfigManager
from tests import test_tv_window as base
from ui.settings_dialog import OptionsDialog


class LanguageSettingTests(base.WindowCase):
    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def choices(self, dialog):
        combo = dialog.language_combo
        return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]

    def test_the_choices_are_the_system_s_english_and_each_catalog_s(self):
        with mock.patch.object(i18n, "_system_preferences", return_value=["tr"]):
            dialog = self.dialog()
        self.assertEqual(
            self.choices(dialog),
            [
                ("", "System default (Türkçe)"),
                ("en", "English"),
                ("es", "Español"),
                ("tr", "Türkçe"),
            ],
        )
        self.assertEqual(dialog.language_combo.currentData(), "")  # no key: the system's
        self.assertIn("starts again", dialog.language_note.text())

    def test_a_choice_is_saved_and_the_system_default_saves_no_key(self):
        dialog = self.dialog()
        dialog.language_combo.setCurrentIndex(dialog.language_combo.findData("tr"))
        dialog.save_settings()
        self.assertEqual(ConfigManager().language, "tr")
        self.assertEqual(ConfigManager().config["language"], "tr")
        dialog = self.dialog()
        self.assertEqual(dialog.language_combo.currentData(), "tr")
        dialog.language_combo.setCurrentIndex(dialog.language_combo.findData(""))
        dialog.save_settings()
        self.assertNotIn("language", ConfigManager().config)
        self.assertIsNone(ConfigManager().language)

    def test_older_settings_without_the_key_follow_the_system(self):
        config = ConfigManager()
        config.config.pop("language", None)
        self.assertIsNone(config.language)
        config.config["language"] = 5  # not a code: the system's
        self.assertIsNone(config.language)


if __name__ == "__main__":
    unittest.main()
