import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMainWindow

from services.config_manager import ConfigManager


class WindowConfigMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_language_models_answer_survives_a_restart(self):
        # 2.9.6's first question ("download the speech models for your languages?"): the yes was
        # set on the object only and asked again after every restart.
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({}), encoding="utf-8")
            with patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)):
                config = ConfigManager()
                self.assertFalse(config.ai_captions_language_models)
                config.ai_captions_language_models = True
                config.save_config(wait=True)
                reloaded = ConfigManager()
                self.assertTrue(reloaded.ai_captions_language_models)
                reloaded.config["ai_captions_language_models"] = "yes"  # not a bool: not a yes
                self.assertFalse(reloaded.ai_captions_language_models)

    def test_a_config_with_settings_this_build_no_longer_has_reads_and_keeps_them(self):
        # A run from source's Test model and Speaker marks (2026-11-08) are gone in 2.10: a
        # config that holds their choices reads as any other, and keeps them as they were (older
        # builds read them).
        saved = {
            "ai_captions_trial_model": "tr-moonm-avg",
            "ai_captions_speaker_marks": "colours",
            "ai_captions_model": "small",
        }
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(saved), encoding="utf-8")
            with patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)):
                config = ConfigManager()
                self.assertEqual(config.ai_captions_model, "small")
                config.save_config(wait=True)
                written = json.loads(config_path.read_text(encoding="utf-8"))
        for key, value in saved.items():
            self.assertEqual(written[key], value)

    def test_the_language_captions_are_translated_into_is_off_or_one_of_the_models(self):
        # 2.10: an older config has none (Off) and keeps what it has; anything but one of
        # MiLMMT's names reads as Off
        saved = {"ai_captions_model": "small", "ai_captions_language": "Turkish"}
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(saved), encoding="utf-8")
            with patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)):
                config = ConfigManager()
                self.assertEqual(config.ai_captions_translate_to, "")
                self.assertEqual(config.ai_captions_language, "Turkish")
                for stored in ("tr", "Klingon", 5, ["Turkish"], None):
                    config.config["ai_captions_translate_to"] = stored
                    self.assertEqual(config.ai_captions_translate_to, "", stored)
                config.ai_captions_translate_to = "Klingon"
                self.assertEqual(config.config["ai_captions_translate_to"], "")
                config.ai_captions_translate_to = "Chinese (Simplified)"
                config.save_config(wait=True)
                reloaded = ConfigManager()
                self.assertEqual(reloaded.ai_captions_translate_to, "Chinese (Simplified)")
                written = json.loads(config_path.read_text(encoding="utf-8"))
        for key, value in saved.items():
            self.assertEqual(written[key], value)

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
            with patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)):
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
            with patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)):
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


class NewConfigTests(unittest.TestCase):
    """A new config.json is written as 2.1 wrote it (the fixture, which ends in a newline as
    the repository's files do; without "prefer_https" since 2.7.1, which chooses HTTPS by
    what answers), and an older config gains the settings it lacks."""

    FRESH = Path(__file__).parent / "fixtures" / "fresh_config.json"

    def config_at(self, directory, saved=None):
        config_path = Path(directory) / "config.json"
        if saved is not None:
            config_path.write_text(json.dumps(saved), encoding="utf-8")
        with patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)):
            ConfigManager()
        return config_path.read_bytes()

    def test_a_new_config_is_the_same_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.config_at(directory) + b"\n", self.FRESH.read_bytes())

    def test_an_older_config_gains_each_setting_it_lacks(self):
        fresh = json.loads(self.FRESH.read_bytes())
        older = {key: fresh[key] for key in ("data", "window_positions", "channel_epg")}
        older.update(selected_provider_name="Mine", tv_defaults_version=2)
        with tempfile.TemporaryDirectory() as directory:
            patched = json.loads(self.config_at(directory, older))
        self.assertEqual(patched, {**fresh, "selected_provider_name": "Mine"})


if __name__ == "__main__":
    unittest.main()
