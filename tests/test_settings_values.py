import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.config_manager import ConfigManager
from services.settings_values import SettingsValues


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

    def test_an_unknown_key_is_an_error(self):
        values = SettingsValues(self.config)
        with self.assertRaises(KeyError):
            values.get("nope")
        with self.assertRaises(KeyError):
            values.set("nope", 1)
        self.assertNotIn("nope", self.config.config)


if __name__ == "__main__":
    unittest.main()
