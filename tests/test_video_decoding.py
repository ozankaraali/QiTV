"""Video decoding: software by default, the hardware decoder only when Settings asks for it.

The Mac's hardware decoder (VideoToolbox) showed about half the pictures of an interlaced TV
channel and froze it at times (13.8 of 25 pictures a second); VLC's software decoder showed
all 25, as mpv does.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services import playback
from services.config_manager import ConfigManager
from tests import test_config_tv
from tests import test_tv_window as base
from ui.settings_dialog import OptionsDialog

HARDWARE_FLAGS = ("--avcodec-hw", "--no-videotoolbox")


class DecoderArgsTests(unittest.TestCase):
    def test_software_decoding_by_default_on_every_platform(self):
        for system, expected in (
            ("Darwin", ["--avcodec-hw=none", "--no-videotoolbox"]),
            ("Windows", ["--avcodec-hw=none"]),  # no VideoToolbox module: an unknown option
            ("Linux", ["--avcodec-hw=none"]),
        ):
            with (
                self.subTest(system=system),
                mock.patch("services.playback.platform.system", return_value=system),
            ):
                self.assertEqual(playback.decoder_args(hardware=False), expected)
                args = playback.default_args()
                self.assertEqual([a for a in args if a.startswith(HARDWARE_FLAGS)], expected)

    def test_hardware_decoding_leaves_vlc_its_own_choice(self):
        self.assertEqual(playback.decoder_args(hardware=True), [])
        args = playback.default_args(hardware_decoding=True)
        self.assertFalse([a for a in args if a.startswith(HARDWARE_FLAGS)])

    @unittest.skipIf(playback.vlc is None, "libVLC is not installed")
    def test_libvlc_accepts_both_choices(self):
        for hardware in (False, True):
            with self.subTest(hardware=hardware):
                instance = playback.create_instance(
                    [*playback.default_args(hardware_decoding=hardware), "--aout=dummy"]
                )
                self.assertIsNotNone(instance)
                instance.release()


class DecodingConfigTests(unittest.TestCase):
    _load = test_config_tv.TvConfigTests._load

    def test_off_by_default_and_only_true_turns_it_on(self):
        config, _ = self._load(None)
        self.assertFalse(config.hardware_decoding)
        for value in (None, "true", 1):
            config.config["hardware_decoding"] = value
            self.assertFalse(config.hardware_decoding)
        config.hardware_decoding = True
        self.assertTrue(config.hardware_decoding)


class DecodingWindowTests(base.WindowCase):
    def test_settings_offers_hardware_decoding_as_experimental_and_saves_it(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        box = dialog.hardware_decoding_checkbox
        self.assertFalse(box.isChecked())
        self.assertIn("experimental", box.text().lower())
        self.assertIn("restart", box.toolTip().lower())
        box.setChecked(True)
        dialog.save_settings()
        self.assertTrue(ConfigManager().hardware_decoding)

    def test_the_window_creates_libvlc_with_the_setting(self):
        self.config.hardware_decoding = True
        with mock.patch("ui.tv_window.create_instance", return_value=None) as create:
            with self.assertLogs("ui.tv_window", "WARNING"):
                window = base.TVWindow(
                    self.app,
                    self.config,
                    self.provider_manager,
                    self.image_manager,
                    self.epg_manager,
                )
        self.addCleanup(window.deleteLater)
        (args,), _ = create.call_args
        self.assertFalse([a for a in args if a.startswith(HARDWARE_FLAGS)])
        self.assertIn("--no-volume-save", args)  # the app's own arguments too


if __name__ == "__main__":
    unittest.main()
