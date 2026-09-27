from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import orjson

from config_manager import ConfigManager


class ConfigMigrationTests(unittest.TestCase):
    def test_missing_channel_window_restored_without_losing_provider_or_history(self):
        manager = ConfigManager.__new__(ConfigManager)
        retained = {
            "selected_provider_name": "My provider",
            "data": [
                {"name": "My provider", "type": "M3UPLAYLIST", "url": "https://example.test/live"}
            ],
            "favorites": ["News"],
            "watch_history": [{"content_id": "My provider:42", "position": 123}],
            "playback_positions": {"My provider:42": 123},
        }
        manager.config = deepcopy(retained)
        manager.config["window_positions"] = {"video_player": {"x": 10, "y": 20}}
        with tempfile.TemporaryDirectory() as directory:
            manager.config_path = str(Path(directory) / "config.json")
            manager.update_patcher()
            saved = orjson.loads(Path(manager.config_path).read_bytes())
        for key, value in retained.items():
            self.assertEqual(saved[key], value)
        self.assertEqual(
            saved["window_positions"], ConfigManager.default_config()["window_positions"]
        )
        self.assertFalse(saved["timeshift_enabled"])

    def test_migration_retires_time_limit_without_losing_geometry_consent_or_capacity(self):
        manager = ConfigManager.__new__(ConfigManager)
        manager.config = ConfigManager.default_config()
        geometry = {"x": 20, "y": 30, "width": 550, "height": 650, "splitter_ratio": 0.6}
        manager.config["window_positions"]["channel_list"] = deepcopy(geometry)
        manager.config.update(timeshift_enabled=True, timeshift_minutes=90, timeshift_max_mib=4096)
        with tempfile.TemporaryDirectory() as directory:
            manager.config_path = str(Path(directory) / "config.json")
            manager.update_patcher()
            saved = orjson.loads(Path(manager.config_path).read_bytes())
        self.assertEqual(saved["window_positions"]["channel_list"], geometry)
        self.assertTrue(saved["timeshift_enabled"])
        self.assertNotIn("timeshift_minutes", saved)
        self.assertEqual(saved["timeshift_max_mib"], 4096)

    def test_only_explicit_boolean_consent_permits_disk_recording(self):
        manager = ConfigManager.__new__(ConfigManager)
        for value in (None, "false", "true", 1, [], {}):
            with self.subTest(value=value):
                manager.config = {"timeshift_enabled": value}
                self.assertFalse(manager.timeshift_enabled)
        manager.timeshift_enabled = True
        self.assertTrue(manager.timeshift_enabled)
        manager.timeshift_enabled = False
        self.assertFalse(manager.timeshift_enabled)


if __name__ == "__main__":
    unittest.main()
