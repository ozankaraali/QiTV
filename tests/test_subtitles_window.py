"""OpenSubtitles' settings in an older config read as empty, and the window gives the
Subtitles panel its OpenSubtitles search (the settings themselves are Settings › Subtitles &
AI captions': tests/test_settings_subtitles.py)."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QWidget

from services.config_manager import ConfigManager
from tests import test_tv_window as base


class SubtitlesConfigTests(base.WindowCase):
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
