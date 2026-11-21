"""2.8: TMDB's settings in the config (on by default, no token of the user's) and its credit
in About (Settings' rows for it: tests/test_settings_general.py)."""

import unittest

from services import settings_model as m
from tests import test_config_tv, test_tv_window as base
from ui.settings_about import LOGO_U, TMDB_LOGO_FILE, TMDB_NOTICE


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
    def test_about_credits_tmdb_with_its_notice_and_its_logo_under_it(self):
        # 2.13.1: Settings' About QiTV. TMDB's terms ask for their notice and logo in About,
        # the logo far less prominent than QiTV's own, flush left with the notice's text.
        self.show()
        self.window.open_settings(section_key=m.ABOUT)
        drawer = self.window.settings_drawer
        rows = drawer._right_rows()
        drawer.grab()
        notice = next(i for i, r in enumerate(rows) if r.text == TMDB_NOTICE)
        self.assertEqual(rows[notice + 1].image, TMDB_LOGO_FILE)
        text, logo = drawer._box("right", notice), drawer._box("right", notice + 1)
        self.assertFalse(logo.image.isEmpty())  # the asset is there
        self.assertLessEqual(logo.image.height(), LOGO_U * drawer._units().u / 6)
        self.assertEqual(logo.image.left(), text.title.left())
        self.assertGreaterEqual(logo.image.top(), text.line.bottom())


if __name__ == "__main__":
    unittest.main()
