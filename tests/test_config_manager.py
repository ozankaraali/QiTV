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
        # MiLMMT's names reads as Off (2.11.2: no longer read, kept for older builds)
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

    def test_dev_turkish_reading_defaults_to_beam_and_rejects_unknown(self):
        # dev build (2026-11-19): optional; an older build ignores it
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({}), encoding="utf-8")
            with patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)):
                config = ConfigManager()
                self.assertEqual(config.dev_turkish_reading, "beam")
                for stored in ("fast", 5, None, ["lm"]):
                    config.config["dev_turkish_reading"] = stored
                    self.assertEqual(config.dev_turkish_reading, "beam", stored)
                config.dev_turkish_reading = "ilme"
                config.save_config(wait=True)
                self.assertEqual(ConfigManager().dev_turkish_reading, "ilme")

    def test_translations_are_kept_per_spoken_language_and_a_2_11_1_config_has_none(self):
        # 2.11.2, the owner: translation starts Off for every language; the old single target
        # is not carried over, and both old keys stay as they were (older builds read them)
        saved = {"ai_captions_translate_to": "English", "ai_captions_language": "Turkish"}
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(saved), encoding="utf-8")
            with patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)):
                config = ConfigManager()
                self.assertEqual(config.ai_captions_translations, {})
                for stored in ("English", ["German"], {"Klingon": "English"}, None, 5):
                    config.config["ai_captions_translations"] = stored
                    self.assertEqual(config.ai_captions_translations, {}, stored)
                config.ai_captions_translations = {"German": "English", "English": "", "x": "y"}
                self.assertEqual(
                    config.config["ai_captions_translations"], {"German": "English", "English": ""}
                )
                config.save_config(wait=True)
                reloaded = ConfigManager()
                self.assertEqual(
                    reloaded.ai_captions_translations, {"German": "English", "English": ""}
                )
                written = json.loads(config_path.read_text(encoding="utf-8"))
        for key, value in saved.items():
            self.assertEqual(written[key], value)

    def test_the_audio_screens_view_shuffle_and_repeat_are_optional_and_kept(self):
        # Plan 4: an older config has none of them (no view picked, shuffle off, repeat off),
        # and saving one adds no other key (older 2.x builds read the file as it was).
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"ai_captions_model": "small"}), encoding="utf-8")
            with patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)):
                config = ConfigManager()
                self.assertEqual(
                    (config.audio_view, config.audio_shuffle, config.audio_repeat),
                    ("", False, "off"),
                )
                for key, stored in (
                    ("audio_view", "poster"),
                    ("audio_shuffle", "yes"),
                    ("audio_repeat", 2),
                ):
                    config.config[key] = stored
                    self.assertIn(getattr(config, key), ("", False, "off"), key)
                config.audio_view, config.audio_shuffle, config.audio_repeat = "stage", True, "one"
                config.save_config(wait=True)
                reloaded = ConfigManager()
                self.assertEqual(
                    (reloaded.audio_view, reloaded.audio_shuffle, reloaded.audio_repeat),
                    ("stage", True, "one"),
                )
                written = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {k: written[k] for k in ("audio_view", "audio_shuffle", "audio_repeat")},
            {"audio_view": "stage", "audio_shuffle": True, "audio_repeat": "one"},
        )
        self.assertNotIn("audio_view", ConfigManager.default_config())

    def test_ai_captions_for_video_files_are_off_until_turned_on_and_kept(self):
        # An older config has none: off. Only true turns it on.
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"ai_captions_model": "small"}), encoding="utf-8")
            with patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)):
                config = ConfigManager()
                self.assertFalse(config.ai_captions_local_on)
                for stored in ("yes", 1, None, ["on"]):
                    config.config["ai_captions_local_on"] = stored
                    self.assertFalse(config.ai_captions_local_on, stored)
                config.ai_captions_local_on = True
                config.save_config(wait=True)
                reloaded = ConfigManager()
                self.assertTrue(reloaded.ai_captions_local_on)
                self.assertEqual(reloaded.ai_captions_model, "small")

    def test_file_types_are_none_until_registered_and_kept(self):
        # An older config has none; anything but a dict reads as none.
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"ai_captions_model": "small"}), encoding="utf-8")
            with patch.object(ConfigManager, "_get_config_path", return_value=str(config_path)):
                config = ConfigManager()
                self.assertEqual(config.file_types, {})
                for stored in ("videos", ["videos"], 5, None):
                    config.config["file_types"] = stored
                    self.assertEqual(config.file_types, {}, stored)
                config.file_types = {"videos": "/opt/qitv/qitv-linux"}
                config.save_config(wait=True)
                reloaded = ConfigManager()
                self.assertEqual(reloaded.file_types, {"videos": "/opt/qitv/qitv-linux"})
                self.assertEqual(reloaded.ai_captions_model, "small")

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
