"""Settings → Subtitles (2.2): the OpenSubtitles API key and the optional sign-in, masked;
Sign out clears the sign-in. The window gives the Subtitles panel its OpenSubtitles search."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel, QLineEdit, QWidget

from services import _build_keys
from services.config_manager import ConfigManager
from tests import test_tv_window as base
from ui.settings_dialog import OPENSUBTITLES_BUILT_IN_HINT, OptionsDialog

KEY = "fictional-api-key-0001"


class SubtitlesSettingsTests(base.WindowCase):
    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_the_tab_shows_the_key_and_the_sign_in_masked(self):
        self.config.opensubtitles_api_key = KEY
        self.config.opensubtitles_username = "harbour_viewer"
        self.config.opensubtitles_password = "fictional-password"
        dialog = self.dialog()
        titles = [dialog.options_tab.tabText(i) for i in range(dialog.options_tab.count())]
        self.assertIn("Subtitles", titles)
        password = QLineEdit.EchoMode.Password
        self.assertEqual(dialog.opensubtitles_key_input.text(), KEY)
        self.assertEqual(dialog.opensubtitles_key_input.echoMode(), password)
        self.assertEqual(dialog.opensubtitles_password_input.echoMode(), password)
        self.assertEqual(dialog.opensubtitles_username_input.text(), "harbour_viewer")
        tab = dialog.opensubtitles_key_input.parentWidget()
        self.assertEqual(tab.objectName(), "subtitlesSettings")

    def test_save_keeps_the_key_and_sign_in_and_sign_out_clears_it(self):
        dialog = self.dialog()
        dialog.opensubtitles_key_input.setText(f"  {KEY} ")
        dialog.opensubtitles_username_input.setText(" harbour_viewer ")
        dialog.opensubtitles_password_input.setText("fictional-password")
        dialog.save_settings()
        saved = ConfigManager()
        self.assertEqual(saved.opensubtitles_api_key, KEY)
        self.assertEqual(saved.opensubtitles_username, "harbour_viewer")
        self.assertEqual(saved.opensubtitles_password, "fictional-password")
        dialog = self.dialog()
        dialog.opensubtitles_sign_out_button.click()
        self.assertEqual(dialog.opensubtitles_username_input.text(), "")
        self.assertEqual(dialog.opensubtitles_password_input.text(), "")
        self.assertEqual(dialog.opensubtitles_key_input.text(), KEY)  # the key stays
        dialog.save_settings()
        saved = ConfigManager()
        self.assertEqual((saved.opensubtitles_username, saved.opensubtitles_password), ("", ""))
        self.assertEqual(saved.opensubtitles_api_key, KEY)

    def test_a_build_with_its_own_key_says_yours_is_optional(self):
        def texts(dialog):
            tab = dialog.opensubtitles_key_input.parentWidget()
            return " ".join(label.text() for label in tab.findChildren(QLabel))

        self.assertNotIn(OPENSUBTITLES_BUILT_IN_HINT, texts(self.dialog()))
        with mock.patch.object(_build_keys, "OPENSUBTITLES_API_KEY", "built-in"):
            dialog = self.dialog()
            self.assertIn(OPENSUBTITLES_BUILT_IN_HINT, texts(dialog))
            self.assertEqual(
                dialog.opensubtitles_key_input.placeholderText(), "Optional: your own key"
            )

    def test_older_settings_without_these_keys_read_as_empty(self):
        config = ConfigManager()
        for key in ("opensubtitles_api_key", "opensubtitles_username", "opensubtitles_password"):
            config.config.pop(key, None)
        self.assertEqual(config.opensubtitles_api_key, "")
        self.assertFalse(config.opensubtitles_hearing_impaired)
        self.assertTrue(config.subtitles_dir().endswith("subtitles"))


class SubtitleSearchWindowTests(base.WindowCase):
    def test_the_search_page_covers_the_drawer_and_guide_under_the_toast(self):
        window, host = self.window, self.window.host
        order = [w for w in host.children() if isinstance(w, QWidget) and w in host._overlays]
        page = order.index(window.subtitle_search)
        self.assertLess(order.index(window.overlays.drawer), page)
        self.assertLess(order.index(window.overlays.guide), page)
        self.assertLess(page, order.index(window.overlays.toast))
        self.assertIs(window.overlays.subtitle_search, window.subtitle_search)
        self.assertTrue(window.subtitle_picks.folder.endswith("subtitles"))
        self.assertIsNotNone(window.controller._online)


if __name__ == "__main__":
    unittest.main()
