import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from config_manager import ConfigManager


class TvConfigTests(unittest.TestCase):
    def _load(self, saved):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.json"
        if saved is not None:
            path.write_text(json.dumps(saved), encoding="utf-8")
        patches = (
            patch.object(ConfigManager, "_get_config_path", return_value=str(path)),
            patch.object(ConfigManager, "_migrate_old_config"),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return ConfigManager(), path

    def test_new_install_has_tv_defaults(self):
        config, _ = self._load(None)
        self.assertEqual(config.live_latency, "normal")
        self.assertEqual(config.mouse_wheel_action, "channel")
        self.assertTrue(config.start_with_last_channel)
        self.assertIsNone(config.last_live_channel)
        self.assertTrue(config.channel_logos)
        self.assertTrue(config.channel_epg)
        self.assertIn("tv_window", config.config["window_positions"])

    def test_tv_window_starts_where_the_larger_old_window_was(self):
        saved = {
            "window_positions": {
                "channel_list": {"x": 1, "y": 2, "width": 400, "height": 800},
                "video_player": {"x": 50, "y": 60, "width": 1200, "height": 700},
            }
        }
        config, _ = self._load(saved)
        self.assertEqual(
            config.config["window_positions"]["tv_window"],
            {"x": 50, "y": 60, "width": 1200, "height": 700},
        )

    def test_existing_tv_window_is_kept(self):
        tv = {"x": 9, "y": 9, "width": 900, "height": 500}
        config, _ = self._load({"window_positions": {"tv_window": tv}})
        self.assertEqual(config.config["window_positions"]["tv_window"], tv)

    def test_one_time_migration_turns_on_logos_and_epg(self):
        config, path = self._load({"channel_logos": False, "channel_epg": False})
        self.assertTrue(config.channel_logos)
        self.assertTrue(config.channel_epg)
        config.channel_epg = False
        config.save_config()
        reloaded = ConfigManager()
        self.assertFalse(reloaded.channel_epg, "the migration must run only once")
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["tv_defaults_version"],
            ConfigManager.TV_DEFAULTS_VERSION,
        )

    def test_invalid_choices_fall_back_to_defaults(self):
        config, _ = self._load({"live_latency": "turbo", "mouse_wheel_action": 3})
        self.assertEqual(config.live_latency, "normal")
        self.assertEqual(config.mouse_wheel_action, "channel")
        config.live_latency = "low"
        self.assertEqual(config.live_latency, "low")

    def test_last_live_channel_round_trips(self):
        config, _ = self._load(None)
        record = {"provider_name": "P", "group_id": "7", "item": {"id": 1, "name": "One"}}
        config.last_live_channel = record
        config.save_config()
        self.assertEqual(ConfigManager().last_live_channel, record)


if __name__ == "__main__":
    unittest.main()
