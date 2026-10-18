"""Video decoding: software by default, the hardware decoder only when Settings asks for it.

The Mac's hardware decoder (VideoToolbox) showed about half the pictures of an interlaced TV
channel and froze it at times (13.8 of 25 pictures a second); VLC's software decoder showed
all 25, as mpv does.

2.1: with hardware decoding, a stream whose picture stands still while its sound plays opens
again in software (`services.picture_watch`; the lab that reproduced the freeze is in its
docstring), and a live channel stays in software for a day (tests/test_live_session.py).
"""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services import picture_watch, playback
from services.config_manager import ConfigManager
from services.picture_watch import Counters, FrozenPicture
from services.vlc_profile import PlayKind
from tests import test_config_tv, test_playback_session as sessions, test_tv_window as base
from ui.settings_dialog import OptionsDialog

HARDWARE_FLAGS = ("--avcodec-hw", "--no-videotoolbox")
SOFTWARE = [":avcodec-hw=none", ":no-videotoolbox"]  # on the Mac
TICK = 0.25  # PlaybackSession's poll


class Stream:
    """libVLC's media counters as each tick reads them: per second, `shown` pictures shown,
    `lost` dropped as late, `video` blocks given to the video decoder, `audio` blocks of
    audio decoded (a 25 fps channel with AAC at 48 kHz)."""

    def __init__(self, watch: FrozenPicture) -> None:
        self.watch, self.now, self.counts = watch, 0.0, [0.0, 0.0, 0.0, 0.0]

    def play(self, seconds, *, shown=25, lost=0, video=25, audio=47, playing=True):
        """Ticks for `seconds`; the times the watch said the picture froze."""
        fired = []
        for _ in range(round(seconds / TICK)):
            self.now = round(self.now + TICK, 6)
            for i, rate in enumerate((shown, lost, video, audio)):
                self.counts[i] += rate * TICK
            if self.watch.update(self.now, playing, Counters(*(int(c) for c in self.counts))):
                fired.append(self.now)
        return fired


class FrozenPictureTests(unittest.TestCase):
    """What the lab's frozen picture looks like in libVLC's counters: VLC plays, video keeps
    reaching the decoder, audio keeps decoding, and no picture is shown (nor dropped)."""

    def setUp(self):
        self.stream = Stream(FrozenPicture())

    def test_a_picture_that_stands_still_while_the_sound_plays_froze(self):
        self.assertEqual(self.stream.play(5.0), [])
        # The last picture came at 5 s: 2 s later (FROZEN_S) the watch says so, once.
        self.assertEqual(self.stream.play(10.0, shown=0), [7.0])
        self.assertEqual(picture_watch.FROZEN_S, 2.0)

    def test_pictures_that_keep_coming_never_froze(self):
        self.assertEqual(self.stream.play(60.0), [])
        self.assertEqual(self.stream.play(60.0, shown=1, video=1), [])  # one a second

    def test_a_stalled_source_is_no_frozen_picture(self):
        # Nothing arrives: VLC plays out its buffer, then nothing decodes, sound included.
        self.stream.play(5.0)
        self.stream.play(0.5, shown=0)
        self.assertEqual(self.stream.play(10.0, shown=0, video=0, audio=0), [])

    def test_an_audio_only_stream_is_no_frozen_picture(self):
        self.assertEqual(self.stream.play(30.0, shown=0, video=0), [])

    def test_pictures_dropped_as_late_are_the_displays_not_the_decoders(self):
        # A window the system throttles: the decoder sends pictures, they come too late.
        self.stream.play(5.0)
        self.assertEqual(self.stream.play(10.0, shown=0, lost=25), [])

    def test_a_pause_or_a_wait_for_data_is_no_frozen_picture(self):
        self.stream.play(5.0)
        self.assertEqual(self.stream.play(10.0, shown=0, playing=False), [])
        # Playing again (still without a new picture), the 2 s count from the last tick
        # that found it waiting (15 s).
        self.assertEqual(self.stream.play(1.75, shown=0), [])
        self.assertEqual(self.stream.play(1.0, shown=0), [17.0])

    def test_the_first_picture_of_a_stream_gets_longer(self):
        # A tune between keyframes waits for one in software too (FIRST_PICTURE_S).
        self.assertEqual(picture_watch.FIRST_PICTURE_S, 4.0)
        self.assertEqual(self.stream.play(3.75, shown=0), [])
        self.assertEqual(self.stream.play(1.0, shown=0), [4.25])

    def test_a_decoder_that_waited_for_a_keyframe_gets_time_to_show_it(self):
        # The lab's 20 s GOP after a clock reset: VLC gives the decoder nothing until the next
        # I picture (both decoders wait), then the hardware decoder showed it 1.8 s later. A
        # reopen then would join after that I picture and wait for the next one.
        self.stream.play(5.0)
        self.assertEqual(self.stream.play(4.0, shown=0, video=0), [])
        self.assertEqual(self.stream.play(3.75, shown=0), [])  # as a stream's first picture
        self.assertEqual(self.stream.play(10.0), [])

    def test_a_decoder_that_started_again_and_shows_nothing_froze(self):
        self.stream.play(5.0)
        self.stream.play(4.0, shown=0, video=0)
        # Video reaches the decoder again from 9.25 s: FIRST_PICTURE_S later, nothing shown.
        self.assertEqual(self.stream.play(10.0, shown=0), [13.25])

    def test_a_restart_watches_the_next_stream(self):
        self.stream.play(5.0)
        self.assertEqual(self.stream.play(3.0, shown=0), [7.0])
        self.stream.watch.restart()
        self.stream.counts = [0.0, 0.0, 0.0, 0.0]  # a new media counts from 0
        self.assertEqual(self.stream.play(5.0, shown=0), [12.25])  # its first picture: 4 s


class SoftwareOptionsTests(unittest.TestCase):
    def test_one_media_decodes_in_software_on_every_platform(self):
        for system, expected in (
            ("Darwin", SOFTWARE),
            ("Windows", [":avcodec-hw=none"]),
            ("Linux", [":avcodec-hw=none"]),
        ):
            with (
                self.subTest(system=system),
                mock.patch("services.playback.platform.system", return_value=system),
            ):
                self.assertEqual(playback.software_options(), expected)
                # The instance's software arguments, as media options (which override them).
                instance = playback.decoder_args(hardware=False)
                self.assertEqual([":" + a[2:] for a in instance], expected)


class _StatsMedia(sessions._FakeMedia):
    def __init__(self, url, player):
        super().__init__(url)
        self.player = player
        self.released = 0

    def get_stats(self, stats):
        shown, lost, video, audio = self.player.counts
        stats.displayed_pictures, stats.lost_pictures = shown, lost
        stats.decoded_video, stats.decoded_audio = video, audio
        return True

    def release(self):
        self.released += 1


class _StatsPlayer(sessions._FakePlayer):
    """A player that plays at once and reports media counters (`counts`) and a position."""

    def __init__(self):
        super().__init__()
        self.media, self.counts, self.ms, self.length = None, [0, 0, 0, 0], 0, 0
        self.stats_read = 0

    def set_media(self, media):
        super().set_media(media)
        self.media = media
        self.counts = [0, 0, 0, 0]  # a new media counts from 0

    def get_media(self):
        self.stats_read += 1
        return self.media

    def get_state(self):
        self._call("get_state")
        return playback.vlc.State.Playing

    def get_time(self):
        self._call("get_time")
        return self.ms

    def get_length(self):
        self._call("get_length")
        return self.length


class _StatsInstance(sessions._FakeInstance):
    def media_new(self, url):
        return _StatsMedia(url, self.player)


class _InlineRunner:
    """Ends libVLC's input at once (the session's worker, run where it is asked for)."""

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        worker.run()
        on_finished(context, None)
        return worker

    def pending(self):
        return 0


@unittest.skipIf(playback.vlc is None, "libVLC is not installed")
class HardwareFallbackTests(unittest.TestCase):
    """PlaybackSession with hardware decoding: a frozen picture opens the stream again in
    software where it plays; a time-shift stream is reopened by its owner (LiveSession)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, *, hardware=True):
        self.now = 1000.0
        self.player = _StatsPlayer()
        session = playback.PlaybackSession(
            _StatsInstance(self.player),
            runner=_InlineRunner(),
            poll_ms=60_000,  # ticked by the test
            hardware=hardware,
            clock=lambda: self.now,
        )
        self.addCleanup(session.release)
        self.froze = []
        session.pictureFroze.connect(lambda: self.froze.append(self.now))
        return session

    def media(self):
        return [call[1] for call in self.player.calls if call[0] == "set_media" and call[1]]

    def options(self):
        return self.player.media.options

    def tick(self, session, seconds, *, shown=25, video=25, audio=47):
        for _ in range(round(seconds / TICK)):
            self.now += TICK
            counts = self.player.counts
            for i, rate in ((0, shown), (2, video), (3, audio)):
                counts[i] += round(rate * TICK)
            session._tick()

    def test_a_live_stream_whose_picture_froze_opens_again_in_software(self):
        session = self.make()
        with mock.patch("services.playback.platform.system", return_value="Darwin"):
            session.play(sessions.A, kind=PlayKind.LIVE)
            self.assertFalse(set(SOFTWARE) & set(self.options()))  # hardware first
            self.tick(session, 5.0)
            with self.assertLogs("services.playback", "INFO") as logs:
                self.tick(session, 2.0, shown=0)
            self.assertEqual(self.media(), [sessions.A, sessions.A])  # opened again ...
            self.assertEqual(self.options()[-2:], SOFTWARE)  # ... in software
            self.assertEqual(self.froze, [self.now])
            self.assertEqual(len(logs.records), 1)
            self.assertNotIn(sessions.A, logs.output[0])  # a stream address may carry a login
            read = self.player.stats_read
            self.tick(session, 30.0, shown=0)  # software now: nothing more to watch
        self.assertEqual(self.player.stats_read, read)
        self.assertEqual(len(self.media()), 2)
        self.assertEqual(self.froze, [self.now - 30.0])

    def test_a_film_opens_again_where_it_plays(self):
        session = self.make()
        self.player.length = 600_000
        session.play("file:///film.mkv", kind=PlayKind.VOD_FILE)
        self.tick(session, 3.0)
        self.player.ms = 42_000
        self.tick(session, 2.0, shown=0)
        self.assertEqual(len(self.media()), 2)
        self.tick(session, TICK)  # it plays: back where it was
        self.assertIn(("set_time", 42_000), self.player.calls)

    def test_a_time_shift_stream_is_left_to_its_owner(self):
        session = self.make()
        session.play("http://127.0.0.1:8000/s/1/0.ts", kind=PlayKind.LIVE_PIPELINE)
        self.tick(session, 3.0)
        self.tick(session, 2.0, shown=0)
        self.assertEqual(len(self.froze), 1)
        self.assertEqual(len(self.media()), 1)  # LiveSession reopens it where it plays
        session.play("http://127.0.0.1:8000/s/1/3.ts", kind=PlayKind.LIVE_PIPELINE, software=True)
        self.assertEqual(self.options()[-1], playback.software_options()[-1])

    def test_the_same_stream_opened_again_stays_in_software(self):
        # A reconnect, a retry: the stream that froze in this player stays in software.
        session = self.make()
        session.play(sessions.A, kind=PlayKind.LIVE)
        self.tick(session, 3.0)
        self.tick(session, 2.0, shown=0)
        session.play(sessions.A, kind=PlayKind.LIVE)
        self.assertEqual(self.options()[-1], playback.software_options()[-1])
        session.play(sessions.B, kind=PlayKind.LIVE)
        self.assertFalse(set(playback.software_options()) & set(self.options()))
        self.assertTrue(session.hardware)

    def test_a_stream_asked_for_in_software_is_not_watched(self):
        session = self.make()
        session.play(sessions.A, kind=PlayKind.LIVE, software=True)
        self.assertEqual(self.options()[-1], playback.software_options()[-1])
        self.tick(session, 10.0, shown=0)
        self.assertEqual((self.player.stats_read, self.froze), (0, []))

    def test_software_decoding_changes_nothing(self):
        # Settings' default: the instance decodes in software; media are as they were.
        session = self.make(hardware=False)
        session.play(sessions.A, kind=PlayKind.LIVE, software=True)
        self.assertEqual(self.options(), playback.media_options(PlayKind.LIVE))
        self.tick(session, 10.0, shown=0)
        self.assertEqual((self.player.stats_read, self.froze, len(self.media())), (0, [], 1))
        self.assertFalse(session.hardware)

    def test_media_statistics_are_released_each_time(self):
        session = self.make()
        session.play(sessions.A, kind=PlayKind.LIVE)
        opened = self.player.media.released  # the open lets go of its own reference
        self.tick(session, 1.0)
        self.assertGreater(self.player.stats_read, 0)
        self.assertEqual(self.player.media.released - opened, self.player.stats_read)


@unittest.skipIf(playback.vlc is None, "libVLC is not installed")
class SoftwareOverrideTests(unittest.TestCase):
    """The fallback's premise, on libVLC itself (muted and offscreen): a media option makes
    one media decode in software on an instance that decodes in hardware."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_media_option_overrides_the_instances_hardware_decoder(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        log = Path(directory.name) / "libvlc.log"
        instance = playback.create_instance(
            [
                *playback.decoder_args(hardware=True),
                "--aout=dummy",
                "--vout=dummy",
                "--no-video-title-show",
                "--verbose=2",
                "--file-logging",
                f"--logfile={log}",
            ]
        )
        self.assertIsNotNone(instance)
        self.addCleanup(instance.release)
        session = playback.PlaybackSession(instance, poll_ms=50, hardware=True)
        self.addCleanup(sessions.release_and_wait, self, session)
        clip = Path(sessions.__file__).parent / "fixtures" / "live.ts"
        session.play(clip.as_uri(), kind=PlayKind.VOD_STREAM, software=True)
        self.assertTrue(
            sessions.wait_for(lambda: session.state() is playback.PlaybackState.PLAYING)
        )
        # "using …", not the "looking for …" line before it (a loaded runner falls between).
        chosen = "using video decoder module"
        self.assertTrue(sessions.wait_for(lambda: chosen in log.read_text()))
        session.stop()
        decoders = [
            line for line in log.read_text().splitlines() if "using video decoder module" in line
        ]
        self.assertTrue(decoders)
        self.assertTrue(all('"avcodec"' in line for line in decoders), decoders)


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
        self.assertIn("goes back to software", box.toolTip().lower())  # the 2.1 fallback
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


@unittest.skipIf(base.INSTANCE is None, "libVLC is not available")
class SoftwareWindowTests(base.WindowCase):
    vlc_instance = base.INSTANCE

    def test_neither_player_watches_for_frozen_pictures(self):
        self.assertFalse(self.window.session.hardware)
        self.assertFalse(self.window.pip_session.hardware)


@unittest.skipIf(base.INSTANCE is None, "libVLC is not available")
class HardwareWindowTests(base.WindowCase):
    vlc_instance = base.INSTANCE

    def prepare_config(self):
        self.config.hardware_decoding = True

    def test_both_players_watch_for_frozen_pictures(self):
        self.assertTrue(self.window.session.hardware)
        self.assertTrue(self.window.pip_session.hardware)


if __name__ == "__main__":
    unittest.main()
