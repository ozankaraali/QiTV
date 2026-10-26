"""2.8: TMDB in Settings (on by default, the user's own token) and its credit in About."""

import unittest
from unittest import mock

from services import tmdb
from services.config_manager import ConfigManager
from tests import test_config_tv, test_tv_window as base
from ui.settings_dialog import OptionsDialog
from ui.tv_menus import AboutDialog, about_text


class TmdbConfigTests(unittest.TestCase):
    _load = test_config_tv.TvConfigTests._load

    def test_on_by_default_without_a_token_of_the_user_s(self):
        config, _ = self._load(None)
        self.assertIs(config.tmdb_details, True)
        self.assertEqual(config.tmdb_token, "")

    def test_an_older_build_s_config_reads_and_only_false_turns_it_off(self):
        for saved, expected in (
            ({}, True),
            ({"tmdb_details": False}, False),
            ({"tmdb_details": 0}, True),
        ):
            with self.subTest(saved=saved):
                config, _ = self._load(saved)
                self.assertIs(config.tmdb_details, expected)


class TmdbSettingsTests(base.WindowCase):
    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_settings_turns_tmdb_off_and_on(self):
        dialog = self.dialog()
        box = dialog.tmdb_checkbox
        self.assertTrue(box.isChecked())
        self.assertIn("TMDB", box.text())
        self.assertIn("nothing about you", box.toolTip())
        box.setChecked(False)
        dialog.save_settings()
        self.assertFalse(ConfigManager().tmdb_details)
        box.setChecked(True)
        dialog.save_settings()
        self.assertTrue(ConfigManager().tmdb_details)

    def test_the_user_s_own_token_is_kept_masked_and_without_spaces(self):
        dialog = self.dialog()
        field = dialog.tmdb_token_input
        self.assertEqual(field.echoMode(), field.EchoMode.Password)
        field.setText(" eyJ.made-up \n")
        dialog.save_settings()
        self.assertEqual(ConfigManager().tmdb_token, "eyJ.made-up")

    def test_a_build_with_a_token_says_so(self):
        with mock.patch.object(tmdb._build_keys, "TMDB_API_TOKEN", "built"):
            dialog = self.dialog()
        self.assertIn("built", dialog.tmdb_token_input.placeholderText().lower())

    def test_about_credits_tmdb_with_its_logo(self):
        text = about_text()
        self.assertIn(
            "This product uses the TMDB API but is not endorsed or certified by TMDB.", text
        )
        about = AboutDialog(self.window)
        self.addCleanup(about.deleteLater)
        self.assertFalse(about.tmdb_logo.pixmap().isNull())


if __name__ == "__main__":
    unittest.main()
