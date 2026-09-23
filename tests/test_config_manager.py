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

    def test_returning_from_mpv_restores_player_without_resetting_saved_state(self):
        # The MPV build removes only the retired embedded player's geometry.
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
                player = QMainWindow()
                restored = QMainWindow()
                try:
                    # This is the call that prevented VideoPlayer from starting.
                    config.apply_window_settings("video_player", player)
                    player.setGeometry(80, 100, 640, 360)
                    config.save_window_settings(player, "video_player")
                    reloaded = ConfigManager()
                    reloaded.apply_window_settings("video_player", restored)
                    self.assertEqual(restored.geometry(), player.geometry())
                    self.assertEqual(
                        reloaded.config["window_positions"]["channel_list"], channel_geometry
                    )
                    for key in ("selected_provider_name", "favorites", "playback_positions"):
                        self.assertEqual(reloaded.config[key], saved_state[key])
                finally:
                    player.deleteLater()
                    restored.deleteLater()
                    self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
