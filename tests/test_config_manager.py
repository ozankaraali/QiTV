import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMainWindow

from config_manager import ConfigManager


class WindowConfigMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_a_1x_config_gains_the_tv_window_without_resetting_saved_state(self):
        # A 1.x config names only the old windows. A missing window entry once kept the
        # player from starting (KeyError in 1.13.7); the TV window's entry is added instead.
        channel_geometry = {
            "x": 120,
            "y": 140,
            "width": 580,
            "height": 720,
            "splitter_ratio": 0.6,
            "splitter_content_info_ratio": 0.4,
        }
        saved_state = {
            "selected_provider_name": "Saved provider",
            "favorites": ["Saved channel"],
            "playback_positions": {"Saved movie": {"position": 45000, "duration": 90000}},
            "window_positions": {"channel_list": channel_geometry},
            "check_updates": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(saved_state), encoding="utf-8")
            with (
                patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)),
                patch.object(ConfigManager, "_migrate_old_config"),
            ):
                config = ConfigManager()
                window = QMainWindow()
                restored = QMainWindow()
                try:
                    config.apply_window_settings("tv_window", window)
                    window.setGeometry(80, 100, 640, 360)
                    config.save_window_settings(window, "tv_window")
                    reloaded = ConfigManager()
                    reloaded.apply_window_settings("tv_window", restored)
                    self.assertEqual(restored.geometry(), window.geometry())
                    # Kept as it was: going back to 1.x finds its windows where they were.
                    self.assertEqual(
                        reloaded.config["window_positions"]["channel_list"], channel_geometry
                    )
                    for key in ("selected_provider_name", "favorites", "playback_positions"):
                        self.assertEqual(reloaded.config[key], saved_state[key])
                finally:
                    window.deleteLater()
                    restored.deleteLater()
                    self.app.processEvents()


class WatchHistoryIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_stb_episodes_sharing_a_series_id_are_kept_apart(self):
        from services.watch_state import WatchState

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            with (
                patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)),
                patch.object(ConfigManager, "_migrate_old_config"),
            ):
                config = ConfigManager()
                watch = WatchState(config)
                ep1 = {"id": "7", "name": "Show", "season": "1", "series": "1"}
                ep2 = {"id": "7", "name": "Show", "season": "1", "series": "2"}
                movie = {"id": "9", "name": "Film"}
                watch.record_play("P", ep1, item_type="episode", link="a")
                watch.record_play("P", ep2, item_type="episode", link="b")
                watch.record_play("P", movie, item_type="movie", link="c")
                watch.record_play("P", movie, item_type="movie", link="d")
                history = config.watch_history
                self.assertEqual([h["link"] for h in history], ["d", "b", "a"])
                self.assertEqual(history[1]["content_id"], "P:7:s1:e2")
                self.assertEqual(history[0]["content_id"], "P:9")


if __name__ == "__main__":
    unittest.main()
