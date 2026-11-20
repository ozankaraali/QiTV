import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.config_manager import ConfigManager
from services.settings_model import Context, sections
from services.settings_values import SettingsValues


def _steppers(rows):
    for row in rows:
        if row.choices:
            yield row
        if row.section is not None:
            yield from _steppers(row.section.rows)


def mock_save_raising(config):
    """config.save_config raising, as writing config.json at once fails (a locked file)."""
    return patch.object(config, "save_config", side_effect=PermissionError(13, "Access is denied"))


class SettingsValuesTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "config.json"
        self.path.write_text(json.dumps({}), encoding="utf-8")
        patcher = patch.object(ConfigManager, "_get_config_path", return_value=str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = ConfigManager()

    def test_set_writes_the_property_and_runs_its_hook(self):
        calls = []
        values = SettingsValues(self.config, {"live_latency": lambda: calls.append(1)})
        values.set("live_latency", "low")
        self.assertEqual(self.config.live_latency, "low")
        self.assertEqual(values.get("live_latency"), "low")
        self.assertEqual(calls, [1])

    def test_a_row_without_a_hook_just_sets(self):
        values = SettingsValues(self.config)
        values.set("hardware_decoding", False)
        self.assertFalse(values.get("hardware_decoding"))

    def test_play_in_maps_onto_the_two_old_keys(self):
        values = SettingsValues(self.config)
        values.set("play_in", "mpv")
        self.assertFalse(self.config.play_in_vlc)
        self.assertTrue(self.config.play_in_mpv)
        self.assertEqual(values.get("play_in"), "mpv")
        values.set("play_in", "vlc")
        self.assertTrue(self.config.play_in_vlc)
        self.assertFalse(self.config.play_in_mpv)
        self.assertEqual(values.get("play_in"), "vlc")
        values.set("play_in", "qitv")
        self.assertFalse(self.config.play_in_vlc)
        self.assertFalse(self.config.play_in_mpv)
        self.assertEqual(values.get("play_in"), "qitv")

    def test_play_in_reads_vlc_when_both_old_keys_are_on(self):
        self.config.play_in_vlc = True
        self.config.play_in_mpv = True
        self.assertEqual(SettingsValues(self.config).get("play_in"), "vlc")

    def test_saving_never_waits(self):
        values = SettingsValues(self.config)
        with patch.object(self.config, "save_config") as save:
            values.set("live_latency", "low")
            values.set("play_in", "mpv")
        self.assertEqual(save.call_count, 2)
        for call in save.call_args_list:
            self.assertFalse(call.kwargs.get("wait", False))

    def test_fast_steps_end_on_the_last_value_without_waiting(self):
        self.config.write_in_background()
        values = SettingsValues(self.config)
        for step in range(50):
            values.set("mouse_wheel_action", "volume" if step % 2 == 0 else "channel")
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().mouse_wheel_action, "channel")

    def test_a_save_that_raises_is_logged_and_the_step_still_applies(self):
        # Only a config without its background writer raises (QiTV's has one): the value is
        # kept in memory, the next save writes it, and its hook runs all the same.
        calls = []
        values = SettingsValues(self.config, {"live_latency": lambda: calls.append(1)})
        with (
            mock_save_raising(self.config),
            self.assertLogs("services.settings_values", "WARNING") as logs,
        ):
            values.set("live_latency", "low")
        self.assertEqual(self.config.live_latency, "low")
        self.assertEqual(calls, [1])
        self.assertIn("live_latency", "\n".join(logs.output))

    def test_a_property_without_a_setter_is_no_row_to_set(self):
        # epg_expiration is worked out from two others: never written as a key of its own.
        values = SettingsValues(self.config, {"epg_expiration": lambda: self.fail("its hook")})
        before = dict(self.config.config)
        with self.assertRaises(KeyError):
            values.set("epg_expiration", 60)
        self.assertEqual(self.config.config, before)
        self.assertEqual(values.get("epg_expiration"), self.config.epg_expiration)

    def test_every_row_starts_at_the_configs_default_and_keeps_each_choice(self):
        # The rows' defaults and choices are the config's own: a fresh config reads as each
        # row's default (shown unlit), and each choice written reads back as itself (none is
        # changed by the property's coerce).
        values = SettingsValues(self.config)
        context = Context(languages=(("en", "English"), ("de", "Deutsch")), image_cache_mb=100)
        rows = [row for s in sections("Darwin", (), context) for row in _steppers(s.rows)]
        keys = [row.key for row in rows]
        self.assertIn("covered_pip", keys)  # the PiP rows too, on Linux
        for key in (  # General's and Advanced's (plan 3)
            "language",
            "check_updates",
            "channel_logos",
            "tmdb_details",
            "ssl_verify",
            "secure_dns",
            "detailed_log",
            "max_cache_image_size",
        ):
            self.assertIn(key, keys)
        for row in rows:
            with self.subTest(row.key):
                self.assertEqual(values.get(row.key), row.default)
                for choice in row.choices:
                    values.set(row.key, choice.value)
                    self.assertEqual(values.get(row.key), choice.value)
                    self.assertEqual(type(values.get(row.key)), type(choice.value))

    def test_a_token_is_kept_without_its_spaces(self):
        # As the old Save kept it: a pasted token's spaces and line breaks are no part of it.
        values = SettingsValues(self.config)
        values.set("tmdb_token", " eyJ.made \n up ")
        self.assertEqual(self.config.tmdb_token, "eyJ.madeup")
        self.assertEqual(values.get("tmdb_token"), "eyJ.madeup")

    def test_an_unknown_key_is_an_error(self):
        values = SettingsValues(self.config)
        with self.assertRaises(KeyError):
            values.get("nope")
        with self.assertRaises(KeyError):
            values.set("nope", 1)
        self.assertNotIn("nope", self.config.config)


if __name__ == "__main__":
    unittest.main()
