import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.config_manager import SAVE_BACKOFF_S, ConfigManager


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

    def test_new_install_has_no_settings_only_the_old_windows_read(self):
        config, path = self._load(None)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        for key in (
            "keyboard_remote_mode",
            "smooth_paused_seek",
            "show_info_panel",
            "show_stb_content_info",
        ):
            with self.subTest(key=key):
                self.assertNotIn(key, on_disk)
                self.assertFalse(hasattr(config, key))

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
        with patch("services.config_manager.json.dumps", return_value=Exploding()):
            with self.assertRaises((OSError, TypeError)):
                config.save_config()
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["config.json"])
        from services.multikeydict import MultiKeyDict

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
            patch("services.config_manager.os.replace", side_effect=replace),
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
            patch("services.config_manager.os.replace", side_effect=replace),
            patch("time.sleep"),
            self.assertLogs("services.config_manager", "WARNING") as logs,
            self.assertRaises(PermissionError),
        ):
            config.save_config()
        self.assertEqual(len(calls), 6)
        self.assertNotIn(path.parent.name, "\n".join(logs.output))  # no paths in the log
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["favorites"], ["Old"])
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["config.json"])

    def test_after_a_failed_save_the_next_saves_do_not_retry_for_a_while(self):
        # A config.json that stays locked (Windows antivirus) cost 250 ms at every save.
        config, path = self._load({"favorites": ["Old"]})
        config.favorites = ["New"]
        replace, calls = self._flaky_replace(100)
        clock = [1000.0]
        with (
            patch("services.config_manager.os.replace", side_effect=replace),
            patch("services.config_manager.time.monotonic", side_effect=lambda: clock[0]),
            patch("time.sleep") as sleep,
            self.assertLogs("services.config_manager", "WARNING"),
        ):
            self.assertRaises(PermissionError, config.save_config)
            self.assertEqual((len(calls), sleep.call_count), (6, 5))
            self.assertTrue(config.save_failed())
            clock[0] += SAVE_BACKOFF_S - 1
            self.assertRaises(PermissionError, config.save_config)
            self.assertEqual((len(calls), sleep.call_count), (7, 5))  # one try, no wait
            clock[0] += SAVE_BACKOFF_S + 1  # no failure since: a lock is waited out again
            self.assertRaises(PermissionError, config.save_config)
            self.assertEqual((len(calls), sleep.call_count), (13, 10))
        config.save_config()  # unlocked
        self.assertFalse(config.save_failed())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["favorites"], ["New"])

    def test_a_save_without_retries_tries_once(self):
        config, path = self._load({"favorites": ["Old"]})
        config.favorites = ["New"]
        replace, calls = self._flaky_replace(100)
        with (
            patch("services.config_manager.os.replace", side_effect=replace),
            patch("time.sleep") as sleep,
            self.assertLogs("services.config_manager", "WARNING"),
            self.assertRaises(PermissionError),
        ):
            config.save_config(retry=False)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["config.json"])

    def test_a_locked_config_does_not_stop_the_start(self):
        # The startup patch saves the new defaults; a locked file made QiTV fail to start.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.json"
        path.write_text(json.dumps({"favorites": ["Old"]}), encoding="utf-8")
        replace, _ = self._flaky_replace(100)
        with (
            patch.object(ConfigManager, "_get_config_path", return_value=str(path)),
            patch.object(ConfigManager, "_migrate_old_config"),
            patch("services.config_manager.os.replace", side_effect=replace),
            patch("time.sleep"),
            self.assertLogs("services.config_manager", "WARNING") as logs,
        ):
            config = ConfigManager()
        self.assertTrue(config.save_failed())  # the window says so once it shows
        self.assertEqual(config.favorites, ["Old"])
        self.assertTrue(any("Access is denied" in line for line in logs.output))
        self.assertNotIn(directory.name, "\n".join(logs.output))  # no paths in the log
        self.assertNotIn("Traceback", "\n".join(logs.output))

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


class ConfigDirTests(unittest.TestCase):
    """Resolving the config path touches nothing; the manager creates the directory."""

    def test_resolving_the_path_creates_no_directory(self):
        # Without this, test_unset_or_empty_override_is_ignored made the real config dir.
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "APPDATA": home, "QITV_CONFIG_DIR": ""}
            with patch.dict(os.environ, env):
                path = ConfigManager._get_config_path(ConfigManager.__new__(ConfigManager))
            self.assertTrue(path.startswith(home), path)
            self.assertFalse(os.path.exists(os.path.dirname(path)))
            self.assertEqual(os.listdir(home), [])

    def _portable_path(self, program_dir, **sys_values):
        """The config path when portable.txt sits in `program_dir` (and nowhere else)."""
        flag = os.path.join(program_dir, "portable.txt")
        exists = os.path.exists
        with (
            patch.dict(os.environ, {"QITV_CONFIG_DIR": ""}),
            patch("os.path.exists", side_effect=lambda path: path == flag or exists(path)),
            patch.multiple("sys", create=True, **sys_values),
        ):
            return ConfigManager._get_config_path(ConfigManager.__new__(ConfigManager))

    def test_portable_source_run_keeps_the_config_beside_main_py(self):
        root = str(Path(__file__).resolve().parents[1])  # where main.py is
        self.assertTrue(os.path.isfile(os.path.join(root, "main.py")))
        path = self._portable_path(root, frozen=False)
        self.assertEqual(path, os.path.join(root, "config.json"))

    def test_portable_frozen_app_keeps_the_config_beside_the_executable(self):
        with tempfile.TemporaryDirectory() as program_dir:
            executable = os.path.join(program_dir, "qitv.exe")
            path = self._portable_path(program_dir, frozen=True, executable=executable)
        self.assertEqual(path, os.path.join(program_dir, "config.json"))

    def test_the_manager_creates_its_directory(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "new", "qitv")
            with patch.dict(os.environ, {"QITV_CONFIG_DIR": target}):
                config = ConfigManager()
            self.assertTrue(os.path.isfile(os.path.join(target, "config.json")))
            self.assertEqual(config.get_config_dir(), target)


class TimeshiftConfigTests(unittest.TestCase):
    _load = TvConfigTests._load

    def test_defaults(self):
        config, _ = self._load(None)
        self.assertFalse(config.timeshift_enabled)
        self.assertEqual(config.timeshift_max_mib, 2048)

    def test_existing_config_gains_the_keys_and_loses_the_time_limit(self):
        saved = {"timeshift_enabled": True, "timeshift_minutes": 90, "timeshift_max_mib": 4096}
        config, path = self._load(saved)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("timeshift_minutes", on_disk)
        self.assertTrue(on_disk["timeshift_enabled"])
        self.assertEqual(on_disk["timeshift_max_mib"], 4096)
        config, path = self._load({})
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertIs(on_disk["timeshift_enabled"], False)
        self.assertEqual(on_disk["timeshift_max_mib"], 2048)

    def test_only_a_literal_true_grants_disk_consent(self):
        config, _ = self._load(None)
        for value in (None, "false", "true", 1, [], {}):
            with self.subTest(value=value):
                config.config["timeshift_enabled"] = value
                self.assertFalse(config.timeshift_enabled)
        config.timeshift_enabled = True
        self.assertTrue(config.timeshift_enabled)
        config.timeshift_enabled = False
        self.assertFalse(config.timeshift_enabled)

    def test_odd_sizes_are_coerced_then_clamped(self):
        config, _ = self._load(None)
        for value, expected in (
            (4096.7, 4096),
            ("4096", 4096),
            (True, 256),
            (float("nan"), 2048),
            (float("inf"), 2048),
        ):
            with self.subTest(value=value):
                config.config["timeshift_max_mib"] = value
                self.assertEqual(config.timeshift_max_mib, expected)

    def test_size_is_clamped(self):
        config, _ = self._load(None)
        for value, expected in ((1, 256), (99999, 16384), (4096, 4096), ("junk", 2048)):
            with self.subTest(value=value):
                config.config["timeshift_max_mib"] = value
                self.assertEqual(config.timeshift_max_mib, expected)
        config.timeshift_max_mib = 10
        self.assertEqual(config.timeshift_max_mib, 256)


class RamRingConfigTests(unittest.TestCase):
    """Owner (M5): every stream not on the disk ring keeps this much in memory."""

    _load = TvConfigTests._load

    def test_defaults_to_256_mib(self):
        config, path = self._load(None)
        self.assertEqual(config.timeshift_ram_mib, 256)
        self.assertEqual(ConfigManager.default_config()["timeshift_ram_mib"], 256)
        config, path = self._load({})
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["timeshift_ram_mib"], 256)

    def test_takes_one_of_three_sizes(self):
        config, _ = self._load(None)
        for value, expected in ((128, 128), (512, 512), ("512", 512), (300, 256), ("x", 256)):
            with self.subTest(value=value):
                config.config["timeshift_ram_mib"] = value
                self.assertEqual(config.timeshift_ram_mib, expected)
        config.timeshift_ram_mib = 128
        self.assertEqual(config.config["timeshift_ram_mib"], 128)


class PipConfigTests(unittest.TestCase):
    """Phase 4: the PiP's corner and its own volume level."""

    _load = TvConfigTests._load

    def test_defaults(self):
        config, _ = self._load(None)
        self.assertEqual(config.pip_corner, "br")
        self.assertEqual(config.pip_volume, 30)

    def test_the_corner_is_one_of_four(self):
        config, _ = self._load(None)
        for value in ("tl", "tr", "bl", "br"):
            config.pip_corner = value
            self.assertEqual(config.pip_corner, value)
        for value in ("middle", None, 3):
            with self.subTest(value=value):
                config.config["pip_corner"] = value
                self.assertEqual(config.pip_corner, "br")

    def test_the_volume_is_a_clamped_level(self):
        config, _ = self._load(None)
        for value, expected in ((45, 45), ("60", 60), (-5, 0), (250, 100), ("junk", 30)):
            with self.subTest(value=value):
                config.config["pip_volume"] = value
                self.assertEqual(config.pip_volume, expected)
        config.pip_volume = 55
        self.assertEqual(config.config["pip_volume"], 55)


if __name__ == "__main__":
    unittest.main()
