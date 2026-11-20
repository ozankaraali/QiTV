"""2.8: TMDB's settings in the config (on by default, no token of the user's) and its credit
in About (Settings' rows for it: tests/test_settings_general.py)."""

import unittest

from PySide6.QtGui import QFontMetricsF

from tests import test_config_tv, test_tv_window as base
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


class AboutTests(base.WindowCase):
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
