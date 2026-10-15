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

    def test_save_replaces_the_file_atomically(self):
        # Finding m4: save_config truncated config.json before writing it, so a failure
        # mid-write (full disk, crash) lost every setting.
        config, path = self._load({"favorites": ["Kept"]})
        config.save_config()
        before = path.read_text(encoding="utf-8")

        class Exploding:
            def decode(self, *args):
                raise OSError("disk full")

        config.favorites = ["Lost"]
        with patch("config_manager.json.dumps", return_value=Exploding()):
            with self.assertRaises((OSError, TypeError)):
                config.save_config()
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["config.json"])
        from multikeydict import MultiKeyDict

        self.assertIsInstance(config.xmltv_channel_map, MultiKeyDict)
        config.save_config()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["favorites"], ["Lost"])
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["config.json"])

    def _flaky_replace(self, failures):
        """os.replace that raises PermissionError `failures` times (Windows AV/indexer)."""
        real_replace, calls = os.replace, []

        def replace(src, dst):
            calls.append((src, dst))
            if len(calls) <= failures:
                raise PermissionError(13, "Access is denied", src, None, dst)
            return real_replace(src, dst)

        return replace, calls

    def test_save_retries_a_locked_file(self):
        config, path = self._load({"favorites": ["Old"]})
        config.favorites = ["New"]
        replace, calls = self._flaky_replace(5)
        with (
            patch("config_manager.os.replace", side_effect=replace),
            patch("time.sleep") as sleep,
        ):
            config.save_config()
        self.assertEqual(len(calls), 6)  # the first try and five retries
        self.assertEqual([c.args for c in sleep.call_args_list], [(0.05,)] * 5)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["favorites"], ["New"])
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["config.json"])

    def test_save_gives_up_on_a_file_that_stays_locked(self):
        config, path = self._load({"favorites": ["Old"]})
        config.favorites = ["New"]
        replace, calls = self._flaky_replace(100)
        with (
            patch("config_manager.os.replace", side_effect=replace),
            patch("time.sleep"),
            self.assertLogs("config_manager", "WARNING") as logs,
            self.assertRaises(PermissionError),
        ):
            config.save_config()
        self.assertEqual(len(calls), 6)
        self.assertNotIn(path.parent.name, "\n".join(logs.output))  # no paths in the log
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["favorites"], ["Old"])
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["config.json"])

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
