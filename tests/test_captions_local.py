"""AI captions come back for a video file: turned on for the last one, they come on again by
themselves once the next one plays, when nothing would be asked, downloaded or measured for it
and the video shows none of its own subtitles; ones that came on by themselves go off before
anything that isn't a video file opens. A real CaptionsController (its process and its speed
benchmark faked) over the TV controller's fakes."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QWidget
import test_tv_controller as tv
from test_tv_controller import CHANNELS, ControllerTestCase, play_context

from ui.track_controls import SUBTITLES

FILM = "/videos/Dizi Bölüm 1.mkv"
NEXT = "/videos/Dizi Bölüm 2.mkv"
SONG = "/music/Şarkı.mp3"


def setUpModule():
    """AI captions' files in a folder of this module's own (never the developer's own)."""
    if not os.environ.get("QITV_CAPTIONS_DIR"):
        folder = tempfile.mkdtemp(prefix="qitv-captions-")
        patch = mock.patch.dict(os.environ, {"QITV_CAPTIONS_DIR": folder})
        patch.start()
        unittest.addModuleCleanup(patch.stop)
        unittest.addModuleCleanup(shutil.rmtree, folder, True)


class _Card:
    """The download question's card: what it was asked."""

    def __init__(self):
        self.answered = mock.Mock()
        self.asked, self.withdrawn = [], 0

    def ask(self, text, yes, no):
        self.asked.append(text)

    def withdraw(self):
        self.withdrawn += 1


class LocalCaptionsTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        from ui.captions_controller import CaptionsController

        self.controller.start()
        self.config.ai_captions_local_on = False
        self.host = QWidget()  # the window, as the captions controller reads it
        self.addCleanup(self.host.deleteLater)
        self.host.controller = self.controller
        self.host.config_manager = self.config
        self.card = _Card()
        self.captions = CaptionsController(self.host, card=self.card)
        self.addCleanup(self.captions._timer.stop)
        self.loads, self.warmed = [], []
        self.captions._load_model = lambda: self.loads.append(self.captions._tier)
        self.captions._warm_tier = lambda: self.warmed.append(True)  # never a benchmark here
        self.window.captions = self.captions
        self.installed, self.score = True, 1.0
        self.missing: set[str] = set()  # models not here though the others are
        self.looked: list[str] = []  # each look on the disk (models.installed)

        def installed(tier, root=None):
            self.looked.append(tier)
            return self.installed and tier not in self.missing

        for name, fake in (
            ("services.captions.models.installed", installed),
            ("services.captions.models.download_size", lambda tier, root=None: 900_000_000),
            ("services.captions.models.full_size", lambda tier, root=None: 900_000_000),
            ("services.captions.tier.known_score", lambda: self.score),
        ):
            patch = mock.patch(name, side_effect=fake)
            patch.start()
            self.addCleanup(patch.stop)

    def film(self, path=FILM):
        """A video file opens and plays."""
        self.controller.play_file(path, size=1234)
        self.session.stateChanged.emit("playing")

    def recorded_play(self):
        """What `captions.enabled()` said as each open reached the player."""
        seen = []
        play = self.session.play

        def recording(url, **kwargs):
            seen.append(self.captions.enabled())
            play(url, **kwargs)

        self.session.play = recording
        return seen

    def test_comes_back_on_for_local_video(self):
        self.config.ai_captions_local_on = True
        self.film()
        self.assertTrue(self.captions.enabled())
        self.assertEqual(self.loads, ["large"])  # Automatic's, here and fast enough
        self.assertEqual((self.card.asked, self.warmed), ([], []))
        self.assertTrue(self.config.ai_captions_local_on)

    def test_whether_their_model_is_here_is_not_looked_at_again_for_each_file(self):
        # Review: no disk on the GUI thread at every file's play (the one looked at last; a
        # download or Settings' Delete looks again).
        self.config.ai_captions_local_on = True
        self.film()
        self.controller.tune_live(CHANNELS[0], group_key="10", channels=list(CHANNELS))
        self.ready()  # off for the channel
        self.film(NEXT)
        self.assertTrue(self.captions.enabled())
        self.assertEqual(self.looked, ["large"])  # once

    def test_off_for_the_last_one_they_stay_off(self):
        self.film()
        self.assertFalse(self.captions.enabled())

    def test_stays_off_without_model_or_speed(self):
        self.config.ai_captions_local_on = True
        self.installed = False
        self.film()
        self.assertFalse(self.captions.enabled())
        self.assertFalse(self.captions.downloading())
        self.assertEqual(self.card.asked, [])  # no question over the film
        self.score = None  # this computer's speed not measured yet
        self.film(NEXT)
        self.assertFalse(self.captions.enabled())
        self.assertEqual(self.warmed, [])  # nor a benchmark started for it
        self.assertEqual(self.card.asked, [])

    def translated_with_the_speed_unknown(self):
        """Translated, Automatic goes by this computer's speed first (_speed_unknown); QiTV
        has just started: not measured yet. Its models are here."""
        self.config.ai_captions_local_on = True
        self.config.ai_captions_translate_to = "German"
        self.score = None

    def test_translated_they_come_on_once_the_speed_is_measured(self):
        # Ruling 13, the friend's setup: a Turkish series translated into German, double-clicked
        # as QiTV starts. Measured in the background, nothing shown or asked meanwhile.
        self.translated_with_the_speed_unknown()
        self.film()
        self.assertEqual(self.warmed, [True])
        self.assertFalse(self.captions.enabled())
        self.assertFalse(self.captions._toggle_waits)  # no wait of the viewer's
        self.assertEqual((self.captions.status_text(), self.card.asked), ("", []))
        self.score = 1.0
        self.captions._on_scored()  # (the benchmark's signal)
        self.assertTrue(self.captions.enabled())
        self.assertEqual(self.card.asked, [])
        self.assertTrue(self.config.ai_captions_local_on)
        self.controller.tune_live(CHANNELS[0], group_key="10", channels=list(CHANNELS))
        self.ready()
        self.assertFalse(self.captions.enabled())  # on by themselves: off for live TV

    def test_translated_a_title_changed_before_the_speed_is_known_brings_nothing_on(self):
        self.translated_with_the_speed_unknown()
        self.film()
        self.controller.tune_live(CHANNELS[0], group_key="10", channels=list(CHANNELS))
        self.ready()
        self.score = 1.0
        self.captions._on_scored()
        self.assertFalse(self.captions.enabled())

    def test_translated_what_changed_before_the_speed_is_known_keeps_them_off(self):
        # Still the same film, but its own subtitles were turned on meanwhile, or the viewer
        # turned AI captions off and on (theirs, then: not by themselves).
        self.translated_with_the_speed_unknown()
        self.film()
        self.session.subtitle_on = 3
        self.score = 1.0
        self.captions._on_scored()
        self.assertFalse(self.captions.enabled())
        self.session.subtitle_on = -1
        self.score = None
        self.film(NEXT)
        self.captions.toggle()  # the viewer's: waits for the speed as before
        self.assertTrue(self.captions._toggle_waits)
        self.assertIsNone(self.captions._auto_pending)
        self.score = 1.0
        self.captions._on_scored()
        self.assertTrue(self.captions.enabled())
        self.controller.tune_live(CHANNELS[0], group_key="10", channels=list(CHANNELS))
        self.ready()
        self.assertTrue(self.captions.enabled())  # the viewer's: on for live TV too

    def test_off_by_the_title_their_model_question_goes_with_them(self):
        # Review: on by themselves, another model chosen that isn't here asks its download's
        # question; the channel tuned next takes it away (else Download turned them on there).
        self.config.ai_captions_local_on = True
        self.missing = {"small"}
        self.film()
        self.captions.set_model("small")
        self.assertTrue(self.captions.asking())
        self.assertEqual(len(self.card.asked), 1)
        self.controller.tune_live(CHANNELS[0], group_key="10", channels=list(CHANNELS))
        self.ready()
        self.assertFalse(self.captions.asking())
        self.assertEqual(self.card.withdrawn, 1)
        self.assertFalse(self.captions.enabled())
        self.assertTrue(self.config.ai_captions_local_on)

    def test_off_by_the_title_their_wait_for_the_speed_goes_with_them(self):
        # On by themselves with the 0.6B chosen; Automatic chosen then, translated, waits for
        # this computer's speed: the channel tuned meanwhile ends the wait.
        self.config.ai_captions_local_on = True
        self.config.ai_captions_model = "small"
        self.config.ai_captions_translate_to = "German"
        self.score = None
        self.film()
        self.assertTrue(self.captions.enabled())
        self.captions.set_model("auto")
        self.assertTrue(self.captions._toggle_waits)
        self.controller.tune_live(CHANNELS[0], group_key="10", channels=list(CHANNELS))
        self.ready()
        self.score = 1.0
        self.captions._on_scored()
        self.assertFalse(self.captions.enabled())

    def test_subtitle_files_on_their_way_block_too(self):
        # Review: files opened with the video load once it plays (the window's), and a file
        # just added is chosen before libVLC lists its track.
        self.config.ai_captions_local_on = True
        self.window.subtitles_due = lambda: True
        self.film()
        self.assertFalse(self.captions.enabled())
        self.window.subtitles_due = lambda: False
        self.controller.play_file(NEXT, size=1234)
        self.session.subtitle_chosen = "/videos/Dizi Bölüm 2.de.srt"  # added, not listed yet
        self.assertEqual(self.session.subtitle_track(), -1)
        self.session.stateChanged.emit("playing")
        self.assertFalse(self.captions.enabled())

    def test_off_chosen_while_the_speed_is_measured_wins(self):
        # Review: Off picked during the silent measurement (or after the viewer's own on while
        # it waits): nothing comes on once it is known.
        self.translated_with_the_speed_unknown()
        self.film()
        self.assertIsNotNone(self.captions._auto_pending)
        self.assertTrue(self.controller.track_controls().choose_subtitles(-1))
        self.score = 1.0
        self.captions._on_scored()
        self.assertFalse(self.captions.enabled())
        self.score = None
        self.captions.toggle()  # the viewer's on: waits for the speed
        self.assertTrue(self.captions._toggle_waits)
        self.assertTrue(self.controller.track_controls().choose_subtitles(-1))
        self.score = 1.0
        self.captions._on_scored()
        self.assertFalse(self.captions.enabled())

    def test_a_subtitle_file_loaded_over_auto_on_ones_keeps_the_field(self):
        # Ruling 14: subtitle files opened with the video load once it plays.
        self.config.ai_captions_local_on = True
        self.film()
        self.assertTrue(self.captions.enabled())
        self.assertTrue(self.controller.drop_subtitles(["/videos/Dizi Bölüm 1.de.srt"]))
        self.assertFalse(self.captions.enabled())
        self.assertTrue(self.config.ai_captions_local_on)  # the next video file: on again
        self.session.subtitle_on = -1  # (it has no subtitles of its own)
        self.film(NEXT)
        self.assertTrue(self.captions.enabled())
        self.controller.tune_live(CHANNELS[0], group_key="10", channels=list(CHANNELS))
        self.ready()
        self.assertFalse(self.captions.enabled())

    def test_a_subtitle_file_or_track_over_the_viewers_ones_keeps_the_field(self):
        self.film()
        self.captions.toggle()  # on: the viewer's
        self.assertTrue(self.config.ai_captions_local_on)
        self.assertTrue(self.controller.drop_subtitles(["/videos/Dizi Bölüm 1.de.srt"]))
        self.assertFalse(self.captions.enabled())
        self.assertTrue(self.config.ai_captions_local_on)
        self.captions.toggle()  # on again, then a track of the film's own chosen over them
        self.assertTrue(self.controller.track_controls().choose_subtitles(3))
        self.assertEqual(self.session.subtitle_on, 3)
        self.assertFalse(self.captions.enabled())
        self.assertTrue(self.config.ai_captions_local_on)
        self.captions.toggle()  # on again, then the panel's row for that track
        self.panel_row(3)
        self.assertEqual(self.session.subtitle_on, 3)
        self.assertFalse(self.captions.enabled())
        self.assertTrue(self.config.ai_captions_local_on)  # a choice for this file only

    def panel_row(self, key):
        """The Subtitles panel's row `key` chosen (its rowChosen). These fakes have no panel
        to show again after it: without one, TrackControls._open steps to the next track,
        as the S key does, so that re-show is left out."""
        controls = self.controller.track_controls()
        with mock.patch.object(controls, "_open"):
            controls._on_row(SUBTITLES, key)

    def choose_off(self, how):
        """Off, from the phone's list (choose_subtitles) or the Subtitles panel's row."""
        if how == "phone":
            self.assertTrue(self.controller.track_controls().choose_subtitles(-1))
        else:
            self.panel_row(-1)
        self.assertEqual(self.session.subtitle_on, -1)

    def test_off_over_ones_that_came_on_by_themselves_is_kept_for_the_next_file(self):
        # Review M3: Off only gave way, so captions that came on by themselves came back on
        # for every next episode. Off during a video file is the viewer's no to subtitles.
        for how in ("phone", "panel"):
            with self.subTest(how=how):
                self.config.ai_captions_local_on = True
                self.film()
                self.assertTrue(self.captions.enabled())  # on by themselves
                self.choose_off(how)
                self.assertFalse(self.captions.enabled())
                self.assertFalse(self.config.ai_captions_local_on)
                self.film(NEXT)
                self.assertFalse(self.captions.enabled())  # the next episode: still off

    def test_off_over_the_viewers_ones_is_kept_too(self):
        self.film()
        self.captions.toggle()  # on: the viewer's
        self.assertTrue(self.config.ai_captions_local_on)
        self.choose_off("panel")
        self.assertFalse(self.config.ai_captions_local_on)

    def test_off_over_a_channel_or_a_song_leaves_the_video_files_choice(self):
        self.config.ai_captions_local_on = True
        self.controller.tune_live(CHANNELS[0], group_key="10", channels=list(CHANNELS))
        self.ready()
        self.captions.toggle()  # on for the channel
        self.choose_off("phone")
        self.assertFalse(self.captions.enabled())
        self.assertTrue(self.config.ai_captions_local_on)  # live TV is no video file
        self.controller.play_file(SONG)
        self.session.stateChanged.emit("playing")
        self.choose_off("panel")
        self.assertTrue(self.config.ai_captions_local_on)  # nor is a song

    def test_what_played_before_the_file_is_no_play_of_it(self):
        # A provider's film is stopped for the file; its input still closing, the file waits,
        # and a "playing" still reported meanwhile is the film's.
        self.config.ai_captions_local_on = True
        self.controller.play_vod(tv.VodTests.MOVIE, "movie", play_context())
        self.catalog.linkReady.emit(self.catalog.links[-1][0], "http://vod/heat.mkv")
        self.session.holding = True
        self.controller.play_file(FILM, size=1234)
        self.session.stateChanged.emit("playing")
        self.assertFalse(self.captions.enabled())
        self.session.let_go()  # released: the file opens
        self.assertTrue(self.session.url.endswith(".mkv") and self.session.url.startswith("file:"))
        self.session.stateChanged.emit("playing")
        self.assertTrue(self.captions.enabled())

    def test_own_subtitles_block(self):
        self.config.ai_captions_local_on = True
        self.session.subtitle_on = 3  # the file's own subtitles show
        self.film()
        self.assertFalse(self.captions.enabled())
        self.assertTrue(self.controller.own_subtitles_shown())
        self.session.subtitle_on = -1
        self.assertFalse(self.controller.own_subtitles_shown())

    def test_once_per_video_not_at_every_play(self):
        # The viewer turned the film's own subtitles off after they blocked the captions: a
        # pause and its play again bring nothing on.
        self.config.ai_captions_local_on = True
        self.session.subtitle_on = 3
        self.film()
        self.session.subtitle_on = -1
        self.session.stateChanged.emit("paused")
        self.session.stateChanged.emit("playing")
        self.assertFalse(self.captions.enabled())

    def test_auto_on_turns_off_before_next_open(self):
        self.config.ai_captions_local_on = True
        self.film()
        self.assertTrue(self.captions.enabled())
        seen = self.recorded_play()
        self.controller.tune_live(CHANNELS[0], group_key="10", channels=list(CHANNELS))
        self.ready()
        self.assertEqual(seen, [False])  # off before the channel opened: no hold behind live
        self.assertTrue(self.config.ai_captions_local_on)  # kept for the next video file

    def test_auto_on_never_relays_the_next_providers_film(self):
        self.config.ai_captions_local_on = True
        self.film()
        self.assertTrue(self.captions.enabled())
        self.controller.play_vod(tv.VodTests.MOVIE, "movie", play_context())
        self.catalog.linkReady.emit(self.catalog.links[-1][0], "http://vod/heat.mkv")
        relay = self.session.plays[-1][1].get("relay")
        if relay is not None:
            self.addCleanup(relay.stop)
        self.assertIsNone(relay)
        self.assertFalse(self.captions.enabled())

    def test_the_next_video_keeps_them_and_a_phone_taking_over_turns_them_off(self):
        self.config.ai_captions_local_on = True
        self.film()
        seen = self.recorded_play()
        self.film(NEXT)
        self.assertEqual(seen, [True])  # the next episode opens with them
        self.assertEqual(self.loads, ["large"])  # loaded once
        self.captions.target_changing(True)  # (asked again: as before)
        self.assertTrue(self.captions.enabled())
        self.controller._before_new_target(None)  # a phone's Watch takes the title
        self.assertFalse(self.captions.enabled())
        self.controller._before_new_target(None)  # asked again: nothing more
        self.assertFalse(self.captions.enabled())
        self.assertTrue(self.config.ai_captions_local_on)

    def test_toggle_records_intent_while_waiting(self):
        self.score, self.installed = None, False
        self.film()
        self.captions.toggle()  # on: Automatic waits for this computer's speed
        self.assertEqual(self.warmed, [True])
        self.assertFalse(self.captions.enabled())
        self.assertTrue(self.config.ai_captions_local_on)  # what the viewer asked for
        self.captions.toggle()  # pressed again before it was known: off
        self.assertFalse(self.config.ai_captions_local_on)

    def test_a_toggle_that_asks_records_on_and_one_while_asked_off(self):
        self.installed = False
        self.film()
        self.captions.toggle()  # on: the download's question
        self.assertTrue(self.captions.asking())
        self.assertTrue(self.config.ai_captions_local_on)
        self.captions.toggle()  # while it is up
        self.assertFalse(self.config.ai_captions_local_on)

    def test_user_toggle_records_field_and_owns_state(self):
        self.film()
        self.captions.toggle()
        self.assertTrue(self.captions.enabled())
        self.assertTrue(self.config.ai_captions_local_on)
        self.controller.tune_live(CHANNELS[0], group_key="10", channels=list(CHANNELS))
        self.ready()
        self.assertTrue(self.captions.enabled())  # the viewer's: on for the channel too
        self.captions.toggle()  # off on live TV: the video files' choice stays
        self.assertTrue(self.config.ai_captions_local_on)

    def test_a_user_toggle_makes_auto_on_ones_the_viewers(self):
        self.config.ai_captions_local_on = True
        self.film()
        self.captions.toggle()  # off
        self.assertFalse(self.config.ai_captions_local_on)
        self.captions.toggle()  # on again: the viewer's now
        self.assertTrue(self.config.ai_captions_local_on)
        self.controller.tune_live(CHANNELS[0], group_key="10", channels=list(CHANNELS))
        self.ready()
        self.assertTrue(self.captions.enabled())

    def test_another_model_chosen_is_no_toggle(self):
        # Auto-on, then the 0.6B chosen: they come on again with it, still by themselves, and
        # what the viewer asked for stays.
        self.config.ai_captions_local_on = True
        self.film()
        self.captions.set_model("small")
        self.assertTrue(self.captions.enabled())
        self.assertEqual(self.loads, ["large", "small"])
        self.assertTrue(self.config.ai_captions_local_on)
        self.controller.tune_live(CHANNELS[0], group_key="10", channels=list(CHANNELS))
        self.ready()
        self.assertFalse(self.captions.enabled())

    def test_songs_never_touch_field(self):
        for kept in (False, True):
            with self.subTest(kept=kept):
                self.config.ai_captions_local_on = kept
                self.controller.play_file(SONG)
                with mock.patch.object(self.captions, "local_video_playing") as asked:
                    self.session.stateChanged.emit("playing")
                asked.assert_not_called()
                self.assertFalse(self.captions.enabled())
                self.captions.toggle()  # on for the song
                self.assertTrue(self.captions.enabled())
                self.captions.toggle()
                self.assertEqual(self.config.ai_captions_local_on, kept)


if __name__ == "__main__":
    unittest.main()
