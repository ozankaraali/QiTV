"""2.8: TMDB in Settings (on by default, the user's own token) and its credit in About."""

import unittest
from unittest import mock

from PySide6.QtGui import QFontMetricsF

from services import tmdb
from services.config_manager import ConfigManager
from tests import test_config_tv, test_tv_window as base
from ui.settings_dialog import OptionsDialog
from ui.tv_menus import ABOUT_LOGO_PX, SMALL_TEXT, AboutDialog, about_text


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

    def test_closing_settings_tells_the_library_tmdb_turned_off(self):  # review #7
        config = self.window.config_manager
        config.tmdb_token = "tok"
        self.window.screens.tmdb.settings_changed()  # on, with a token: as the window starts
        changes = []
        self.window.screens.tmdb.changed.connect(lambda: changes.append(True))

        class Closing:
            def __init__(self, _window):
                pass

            def exec(self):
                config.tmdb_details = False  # what the user chose

        with mock.patch("ui.tv_window.OptionsDialog", Closing):
            self.window.options_dialog()
        self.assertEqual(changes, [True])

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

    def test_about_credits_what_qitv_uses_tmdb_among_them(self):
        text = about_text()
        names = ("VLC", "Qt", "Python", "OpenSubtitles", "iptv-org", "TMDB")
        for name in (*names, "Jellyfin", "Emby", "Chromecast"):  # 2.8: what it works with
            self.assertIn(name, text)
        self.assertIn("THIRD-PARTY-NOTICES.md", text)  # every credit and license
        # TMDB's notice is in the credits' own small text: the same size, the same left edge
        self.assertIn(
            # TMDB's terms' own template (2.8)
            "<small>QiTV uses TMDB and the TMDB APIs but is not endorsed, certified, or"
            " otherwise approved by TMDB.</small>",
            text,
        )
        about = AboutDialog(self.window)
        self.addCleanup(about.deleteLater)
        logo = about.tmdb_logo.pixmap()
        self.assertFalse(logo.isNull())
        tall = logo.height() / logo.devicePixelRatio()
        self.assertLessEqual(tall, ABOUT_LOGO_PX / 6)  # far less prominent than QiTV's own
        about.show()
        about.layout().activate()
        self.assertEqual(about.tmdb_logo.x(), about.text.x())  # flush left with the text
        # 2.8.1: and with its letters, which start their small font's left bearing into the
        # line: the logo's picture starts as many device pixels in
        ratio = logo.devicePixelRatio()
        bearing = QFontMetricsF(about.text.font()).leftBearing("Q") * SMALL_TEXT
        image = logo.toImage()
        ink = next(
            x
            for x in range(image.width())
            if any(image.pixelColor(x, y).alpha() for y in range(image.height()))
        )
        self.assertEqual(ink, int(bearing * ratio))  # whole pixels, as text snaps to them
        self.assertEqual(about.tmdb_logo.width(), round(logo.width() / ratio))


if __name__ == "__main__":
    unittest.main()
