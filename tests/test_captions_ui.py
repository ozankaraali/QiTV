"""AI captions on screen (2.9): the languages' names, the Subtitles panel's rows and language
list (with a fake CaptionsController), the Sound panel's language labels, the captions overlay
and Settings → Subtitles → AI captions."""

import os
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget
from test_track_panel import CHANNEL, FILM, FakePlayer, PanelCase
from timer_fixtures import wait_until

from services.captions.languages import QWEN_LANGUAGES, iso_code, own_name, qwen_name
from services.config_manager import ConfigManager
from services.playback import AudioTrackInfo, NullSession
from services.sound_sync import SoundSync
from tests import test_tv_window as base
from ui.captions_texts import size_text
from ui.keymap import Action
from ui.overlays.captions import MAX_LINES, CaptionsOverlay
from ui.overlays.painting import MIN_FONT_PX
from ui.overlays.track_panel import PanelModel, PanelRow
from ui.overlays.transport import Transport
from ui.player_keys import PlayerKeys
from ui.settings_dialog import OptionsDialog, captions_space_text
from ui.track_controls import (
    AI_BACK,
    AI_CAPTIONS,
    AI_LANGUAGE,
    AI_PICK,
    TrackControls,
    audio_names,
    language_label,
)

# The 30 languages Qwen3-ASR hears, by their ISO 639 codes.
QWEN_CODES = (
    "zh en yue ar de fr es pt id it ko ru th vi ja tr hi ms nl sv da fi pl cs fil fa el hu mk ro"
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setUpModule():
    """AI captions' files in a folder of this module's own: the controller's speed check
    (tier.machine_score) never writes the developer's own captions folder."""
    if not os.environ.get("QITV_CAPTIONS_DIR"):
        folder = tempfile.mkdtemp(prefix="qitv-captions-")
        patch = mock.patch.dict(os.environ, {"QITV_CAPTIONS_DIR": folder})
        patch.start()
        unittest.addModuleCleanup(patch.stop)
        unittest.addModuleCleanup(shutil.rmtree, folder, True)


class LanguagesTests(unittest.TestCase):
    def test_qwen_s_thirty_languages_by_their_codes(self):
        codes = QWEN_CODES.split()
        self.assertEqual(len(QWEN_LANGUAGES), 30)
        self.assertEqual([iso_code(name) for name in QWEN_LANGUAGES], codes)
        self.assertEqual([qwen_name(code) for code in codes], QWEN_LANGUAGES)

    def test_two_and_three_letter_codes_regions_and_names(self):
        for given in ("tr", "tur", "TUR", "tr-TR", "tr_TR", "Turkish", "turkish", "Türkçe"):
            with self.subTest(given):
                self.assertEqual(qwen_name(given), "Turkish")
        self.assertEqual(qwen_name("ger"), "German")  # 639-2's bibliographic code
        self.assertEqual(qwen_name("deu"), "German")  # its terminology code
        self.assertEqual(qwen_name("fre"), "French")
        self.assertEqual(qwen_name("pt-BR"), "Portuguese")
        self.assertEqual(qwen_name("zh-yue"), "Cantonese")
        self.assertEqual(qwen_name("yue-HK"), "Cantonese")
        self.assertEqual(qwen_name("zh-TW"), "Chinese")
        self.assertEqual(qwen_name("tgl"), "Filipino")
        self.assertEqual(qwen_name("per"), "Persian")

    def test_codes_that_name_none_of_them(self):
        for given in ("", None, "und", "mul", "qaa", "uk", "ukr", "Swahili", "xx"):
            with self.subTest(given):
                self.assertIsNone(qwen_name(given))

    def test_own_names(self):
        self.assertEqual(own_name("Turkish"), "Türkçe")
        self.assertEqual(own_name("German"), "Deutsch")
        self.assertEqual(own_name("Japanese"), "日本語")
        self.assertEqual(own_name("auto"), "auto")  # none of Qwen's: as it is
        self.assertEqual(iso_code("Klingon"), "")
        self.assertTrue(all(own_name(name) for name in QWEN_LANGUAGES))

    def test_it_needs_no_qt(self):  # the captions process imports it
        probe = "import sys, services.captions.languages; print('PySide6' in sys.modules)"
        done = subprocess.run(
            [sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True
        )
        self.assertEqual(done.stdout.strip(), "False")


def info(track_id, code, codec="mp4a", channels=0):
    return AudioTrackInfo(track_id, code, codec, channels)


class AudioNamesTests(unittest.TestCase):
    def test_each_track_by_its_language_s_own_name(self):
        # An MPEG-TS stream: the player's ids are the PIDs, the media's 1, 2 (paired in order).
        tracks = [(257, "Track 1 - [Turkish]"), (258, "Track 2 - [English]")]
        names = audio_names(tracks, [info(1, "tur"), info(2, "eng", "a52 ")])
        self.assertEqual(names, {257: "Türkçe", 258: "English"})

    def test_two_alike_say_their_codec_and_channels(self):
        tracks = [(1, "Track 1 - [English]"), (2, "Track 2 - [English]"), (3, "Track 3")]
        infos = [info(1, "eng", "mp4a", 2), info(2, "eng", "a52 ", 6), info(3, "ger")]
        self.assertEqual(
            audio_names(tracks, infos),
            {1: "English · AAC stereo", 2: "English · AC-3 5.1", 3: "Deutsch"},
        )

    def test_alike_in_everything_they_say_their_number(self):
        tracks = [(1, "Track 1"), (2, "Track 2")]
        names = audio_names(tracks, [info(1, "spa"), info(2, "spa")])
        self.assertEqual(names, {1: "Español · AAC · Track 1", 2: "Español · AAC · Track 2"})

    def test_a_track_without_a_language_says_so(self):
        tracks = [(1, "Track 1"), (2, "Track 2")]
        names = audio_names(tracks, [info(1, ""), info(2, "und")])
        self.assertEqual(
            names, {1: "Track 1 · language not given", 2: "Track 2 · language not given"}
        )

    def test_libvlc_s_bracketed_language_when_the_media_says_nothing(self):
        tracks = [(5, "Track 1 - [Turkish]"), (6, "Commentary")]
        self.assertEqual(audio_names(tracks, []), {5: "Türkçe", 6: "Commentary"})
        self.assertEqual(audio_names(tracks, None), {5: "Türkçe", 6: "Commentary"})

    def test_a_language_qwen_does_not_hear_by_its_own_name_too(self):
        self.assertEqual(language_label("ukr"), "Українська")
        self.assertEqual(language_label("sqi"), "Shqip")
        self.assertEqual(language_label("qad"), "")  # DVB's audio description: no language
        self.assertEqual(language_label("Klingon"), "Klingon")  # an English name as it is
        self.assertEqual(language_label("xx"), "")


class InfoPlayer(FakePlayer):
    """A player that says what its media says of the audio tracks."""

    def __init__(self, audio, infos):
        super().__init__(audio=audio)
        self.infos = list(infos)

    def audio_track_info(self):
        return list(self.infos)


class SoundLanguagesTests(PanelCase):
    def test_the_rows_name_the_languages_and_one_track_shows_too(self):
        self.player = InfoPlayer(
            [(-1, "Disable"), (257, "Track 1 - [Turkish]"), (258, "Track 2 - [English]")],
            [info(1, "tur"), info(2, "eng")],
        )
        self.player.playing = 257
        self.click_button("audio")
        self.assertEqual(self.shown(), (["*Türkçe", "English"], "In sync"))
        self.click_panel(self.panel.row_rect(1))
        self.assertEqual(self.player.playing, 258)  # selection works as before
        self.player.audio = [(-1, "Disable"), (257, "Track 1 - [Turkish]")]
        self.player.infos = [info(1, "tur")]
        self.player.playing = 257
        self.controls.open_sound()
        self.assertEqual(self.shown(), (["*Türkçe"], "In sync"))

    def test_the_null_session_says_nothing(self):
        self.assertEqual(NullSession().audio_track_info(), [])


class FakeCaptions(QObject):
    """The CaptionsController's face, as TrackControls uses it."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self.offered = True
        self.on = False
        self.status = ""
        self.lang = ("auto", "")
        self.asks = False  # turning on asks first (the download's question): it stays off
        self.asked = False  # the question is up
        self.going = False  # the model downloads (2.9: the phone's sheet says so)
        self.toggles = 0
        self.picked = []
        self.chosen_model = "auto"
        self.models = []  # set_model's calls
        self.model_asks = False  # choosing a model asks its download's question first

    def available(self):
        return self.offered

    def enabled(self):
        return self.on

    def downloading(self):
        return self.going

    def asking(self):
        return self.asked

    def question(self):
        return ("A speech model: 0.9 GB to download once. Download it now?", "Download", "Not now")

    def answer(self, yes):
        """The question answered on a phone: the download begins, or nothing."""
        self.asked, self.going = False, yes

    def status_text(self):
        return self.status

    def toggle(self):
        self.toggles += 1
        if self.on or self.going:  # off, a download on its way called off too
            self.on = self.going = False
            return
        if self.asks:
            self.asked = True
            return
        self.on = True

    def release(self, then):
        """Settings' Delete: off, then the files (CaptionsController.release)."""
        self.on = False
        then()

    def languages(self):
        return [("English", "English"), ("Turkish", "Türkçe"), ("German", "Deutsch")]

    def language(self):
        return self.lang

    def set_language(self, name):
        self.picked.append(name)
        self.lang = ("auto", "") if name == "auto" else (name, "you chose")

    def model(self):
        return self.chosen_model

    def model_choices(self):
        return [
            ("auto", "Automatic (0.6B)"),
            ("small", "Faster (0.6B)"),
            ("large", "More accurate (1.7B)"),
        ]

    def model_text(self):
        return dict(self.model_choices())[self.chosen_model]

    def step_model(self, delta):
        names = [name for name, _text in self.model_choices()]
        self.set_model(names[(names.index(self.chosen_model) + delta) % 3])

    def set_model(self, name):
        self.models.append(name)
        self.chosen_model = name
        if self.model_asks:
            self.on, self.asked = False, True
        self.changed.emit()  # as the controller's: the panel follows


class CaptionRowsTests(PanelCase):
    def setUp(self):
        self.captions = FakeCaptions()
        super().setUp()
        self.current = CHANNEL

    def make_controls(self, panel="default", subtitle_panel="default"):
        controls = TrackControls(
            sound_panel=self.panel,
            subtitle_panel=self.subtitle_panel,
            transport=self.transport,
            session=lambda: self.player,
            current=lambda: self.current,
            playing=lambda: self.playing,
            keys=PlayerKeys(lambda item: f"key-{item['id']}"),
            store=SoundSync(None),
            toast=lambda text, *args: self.toasts.append(text),
            show_transport=self.transport.poke,
            pick_file=self.pick,
            config=self.config,
            captions=lambda: self.captions,
        )
        controls.wire()
        return controls

    def rows(self):
        return self.subtitle_panel.model().rows

    def texts(self):
        return [
            ("*" if r.checked else "") + r.text + (f" [{r.note}]" if r.note else "")
            for r in self.rows()
        ]

    def click_row(self, key):
        index = [r.key for r in self.rows()].index(key)
        self.click(self.subtitle_panel, self.subtitle_panel.row_rect(index).center().toPoint())

    def test_the_row_shows_while_ai_captions_can_be_offered(self):
        self.captions.offered = False
        self.click_button("subtitles")
        self.assertNotIn(AI_CAPTIONS, [r.key for r in self.rows()])
        self.subtitle_panel.dismiss()
        self.captions.offered = True
        self.click_button("subtitles")
        self.assertEqual(
            self.texts(),
            [
                "*Off",
                "English",
                "Français",
                "AI captions",
                "Load a file…",
            ],
        )

    def test_turning_them_on_ticks_the_row_and_the_language_row_comes(self):
        self.captions.lang = ("Turkish", "audio track")
        self.click_button("subtitles")
        self.click_row(AI_CAPTIONS)
        self.assertEqual(self.captions.toggles, 1)
        self.assertTrue(self.subtitle_panel.isVisible())  # back, with them on
        self.assertEqual(
            self.texts(),
            [
                "Off",
                "English",
                "Français",
                "*AI captions",
                "Language [Türkçe · audio track]",
                "Load a file…",
            ],
        )
        self.assertTrue(self.rows()[4].opens)
        self.click_row(AI_CAPTIONS)  # off again: the panel stays
        self.assertFalse(self.captions.on)
        self.assertTrue(self.subtitle_panel.isVisible())
        self.assertNotIn(AI_LANGUAGE, [r.key for r in self.rows()])

    def test_their_first_time_asks_the_panel_gives_way_to_the_question(self):
        self.captions.asks = True
        self.click_button("subtitles")
        self.click_row(AI_CAPTIONS)
        self.assertEqual(self.captions.toggles, 1)
        self.assertFalse(self.subtitle_panel.isVisible())  # the question is in front

    def test_the_row_says_what_they_wait_for(self):
        self.captions.on, self.captions.status = True, "Downloading 34%"
        self.click_button("subtitles")
        row = self.rows()[[r.key for r in self.rows()].index(AI_CAPTIONS)]
        self.assertEqual((row.checked, row.note), (True, "Downloading 34%"))
        self.captions.status = "Preparing…"
        self.captions.changed.emit()  # the panel follows at once
        row = self.rows()[[r.key for r in self.rows()].index(AI_CAPTIONS)]
        self.assertEqual(row.note, "Preparing…")

    def test_ticked_while_the_download_is_on_its_way_and_a_tap_calls_it_off(self):
        # 2.9: unticked, "Preparing…" read as "tap to turn them on": the tap cancelled it.
        self.captions.going, self.captions.status = True, "Preparing…"
        self.click_button("subtitles")
        row = self.rows()[[r.key for r in self.rows()].index(AI_CAPTIONS)]
        self.assertEqual((row.checked, row.note), (True, "Preparing…"))
        self.assertNotIn(AI_LANGUAGE, [r.key for r in self.rows()])  # not on yet
        self.click_row(AI_CAPTIONS)
        self.assertFalse(self.captions.going)

    def test_one_subtitle_source_while_their_download_is_on_its_way_too(self):
        # 2.9: the track shown stayed ticked beside AI captions while their model downloaded.
        self.player.spu = 4  # Français shows
        self.captions.asks = True
        self.click_button("subtitles")
        self.click_row(AI_CAPTIONS)  # the question
        self.captions.answer(True)  # Download: on their way
        self.captions.changed.emit()
        self.assertEqual(self.player.spu, -1)  # the track went
        self.click_button("subtitles")
        self.assertEqual([t for t in self.texts() if t.startswith("*")], ["*AI captions"])
        self.click_row(4)  # a track chosen meanwhile: the download is called off
        self.assertFalse(self.captions.going)
        self.assertEqual(self.player.spu, 4)

    def test_the_language_list_picks_one_and_comes_back(self):
        self.captions.on = True
        self.click_button("subtitles")
        self.click_row(AI_LANGUAGE)
        self.assertEqual(self.subtitle_panel.model().title, "Language")
        self.assertEqual(self.texts(), ["Subtitles", "*Auto", "English", "Türkçe", "Deutsch"])
        self.assertEqual(self.rows()[0].key, AI_BACK)
        self.click_row((AI_PICK, "Turkish"))
        self.assertEqual(self.captions.picked, ["Turkish"])
        self.assertEqual(self.subtitle_panel.model().title, "Subtitles")
        language = self.rows()[[r.key for r in self.rows()].index(AI_LANGUAGE)]
        self.assertEqual(language.note, "Türkçe · you chose")
        self.click_row(AI_LANGUAGE)
        self.assertEqual(self.texts()[1:4], ["Auto", "English", "*Türkçe"])
        self.click_row(AI_BACK)
        self.assertEqual(self.subtitle_panel.model().title, "Subtitles")

    def ai_row(self):
        return self.rows()[[r.key for r in self.rows()].index(AI_CAPTIONS)]

    def test_the_speech_model_steps_on_the_row_and_a_click_on_it_turns_them_on(self):
        # 2.9, the owner's: as a game's quality, "AI captions  ‹ Automatic ›" on the row, before
        # their download too; the row is chosen as any subtitle's.
        self.click_button("subtitles")
        row = self.ai_row()
        self.assertEqual((row.value, row.lit, row.choosable), ("Automatic (0.6B)", False, True))
        self.assertEqual(row.widest, "More accurate (1.7B)")
        index = [r.key for r in self.rows()].index(AI_CAPTIONS)
        more = self.subtitle_panel.step_rect(index, 1).center().toPoint()
        self.click(self.subtitle_panel, more)
        self.assertEqual((self.captions.models, self.captions.toggles), (["small"], 0))
        self.assertEqual((self.ai_row().value, self.ai_row().lit), ("Faster (0.6B)", True))
        less = self.subtitle_panel.step_rect(index, -1).center().toPoint()
        self.click(self.subtitle_panel, less)
        self.click(self.subtitle_panel, less)  # round the three
        self.assertEqual(self.captions.models, ["small", "auto", "large"])
        self.click_row(AI_CAPTIONS)  # elsewhere on it: on, as a subtitle's row
        self.assertEqual(self.captions.toggles, 1)

    def test_the_keys_step_the_model_and_enter_turns_them_on(self):
        self.click_button("subtitles")
        panel = self.subtitle_panel
        index = [r.key for r in self.rows()].index(AI_CAPTIONS)
        while panel.focused() != index:
            panel.move_focus(1)
        self.assertTrue(self.controls.handle(Action.SEEK_FWD))
        self.assertEqual(self.captions.models, ["small"])
        self.assertTrue(self.controls.handle(Action.OK))
        self.assertEqual(self.captions.toggles, 1)

    def test_a_model_that_must_download_first_puts_its_question_in_front(self):
        self.captions.on, self.captions.model_asks = True, True
        self.click_button("subtitles")
        index = [r.key for r in self.rows()].index(AI_CAPTIONS)
        self.click(self.subtitle_panel, self.subtitle_panel.step_rect(index, 1).center().toPoint())
        self.assertFalse(self.subtitle_panel.isVisible())

    def test_left_goes_back_from_the_list_and_it_opens_on_its_own_list_again(self):
        self.captions.on = True
        self.click_button("subtitles")
        self.click_row(AI_LANGUAGE)
        self.assertTrue(self.controls.handle(Action.SEEK_BACK))
        self.assertEqual(self.subtitle_panel.model().title, "Subtitles")
        self.click_row(AI_LANGUAGE)
        self.subtitle_panel.dismiss()
        self.click_button("subtitles")
        self.assertEqual(self.subtitle_panel.model().title, "Subtitles")

    def test_one_subtitle_source_at_a_time(self):
        self.player.spu = 3  # a track shows
        self.click_button("subtitles")
        self.click_row(AI_CAPTIONS)
        self.assertTrue(self.captions.on)
        self.assertEqual(self.player.spu, -1)  # the track went
        self.assertEqual(self.texts()[:3], ["Off", "English", "Français"])  # nothing ticked
        self.click_row(4)  # a track: AI captions go off
        self.assertEqual(self.player.spu, 4)
        self.assertFalse(self.captions.on)
        self.click_row(AI_CAPTIONS)
        self.click_row(-1)  # Off too
        self.assertFalse(self.captions.on)

    def test_turned_on_later_the_track_shown_goes(self):
        self.captions.asks = True
        self.player.spu = 3
        self.click_button("subtitles")
        self.click_row(AI_CAPTIONS)  # the question; its download, then they come on
        self.captions.on = True
        self.captions.changed.emit()
        self.assertEqual(self.player.spu, -1)

    def test_a_file_loaded_turns_them_off(self):
        self.current = FILM
        self.captions.on = True
        path = os.path.join(ROOT, "Lighthouse.en.srt")  # the fake player takes any path
        self.assertTrue(self.controls.load_subtitle_file(path, say=False))
        self.assertFalse(self.captions.on)

    def test_without_a_controller_nothing_changes(self):
        self.captions = None
        self.click_button("subtitles")
        self.assertEqual(self.texts(), ["*Off", "English", "Français", "Load a file…"])


class PhoneChoicesTests(PanelCase):
    """2.9: the panels' choices as the phone remote's Subtitles and Sound sheets list them and
    pick them (TrackControls' own words and rules: one subtitle source at a time)."""

    def setUp(self):
        self.captions = FakeCaptions()
        super().setUp()

    make_controls = CaptionRowsTests.make_controls

    def test_the_choices_are_the_panels_rows(self):
        self.player = InfoPlayer(
            [(-1, "Disable"), (257, "Track 1 - [Turkish]"), (258, "Track 2 - [English]")],
            [info(1, "tur"), info(2, "eng")],
        )
        self.player.playing = 258
        self.player.spu = 3
        controls = self.controls
        self.assertTrue(controls.offers())
        self.assertEqual(controls.sound_choices(), [(257, "Türkçe", False), (258, "English", True)])
        self.assertEqual(
            controls.subtitle_choices(), [(3, "English", True), (4, "Français", False)]
        )
        off = controls.captions_choice()
        self.assertEqual((off.on, off.going, off.status, off.languages), (False, False, "", ()))
        self.captions.on, self.captions.status = True, "Catching up…"
        self.captions.lang = ("Turkish", "audio track")
        on = controls.captions_choice()
        self.assertEqual((on.on, on.status, on.language), (True, "Catching up…", "Turkish"))
        self.assertEqual(on.language_text, "Türkçe · audio track")  # as the Language row's
        self.assertEqual(on.languages[:2], (("auto", "Auto"), ("English", "English")))
        self.captions.offered = False
        self.assertIsNone(controls.captions_choice())

    def test_nothing_plays_nothing_is_offered_or_chosen(self):
        self.playing = False
        controls = self.controls
        self.assertFalse(controls.offers())
        self.assertIsNone(controls.captions_choice())
        self.assertFalse(controls.choose_sound(2))
        self.assertFalse(controls.choose_subtitles(3))
        controls.set_captions(True)
        self.assertEqual((self.player.playing, self.player.spu, self.captions.toggles), (1, -1, 0))

    def test_a_track_is_chosen_as_its_row_chooses_it(self):
        controls = self.controls
        self.assertTrue(controls.choose_sound(2))
        self.assertEqual(self.player.playing, 2)
        for gone in (9, -1):  # a track of another list, and no sound at all
            self.assertFalse(controls.choose_sound(gone))
        self.assertEqual(self.player.playing, 2)
        self.captions.on = True
        self.assertTrue(controls.choose_subtitles(4))
        self.assertEqual(self.player.spu, 4)
        self.assertFalse(self.captions.on)  # one subtitle source at a time
        self.assertFalse(controls.choose_subtitles(9))
        self.assertTrue(controls.choose_subtitles(-1))  # Off
        self.assertEqual(self.player.spu, -1)

    def test_an_open_panel_follows_the_phones_pick(self):
        self.click_button("audio")
        self.assertEqual(self.shown()[0], ["*English", "Deutsch"])
        self.controls.choose_sound(2)
        self.assertEqual(self.shown()[0], ["English", "*Deutsch"])

    def test_ai_captions_are_said_on_or_off_never_toggled(self):
        controls = self.controls
        self.player.spu = 3
        controls.set_captions(True)
        self.assertTrue(self.captions.on)
        self.assertEqual(self.player.spu, -1)  # the track shown went
        controls.set_captions(True)  # the same again (a second tap, a late message)
        self.assertEqual(self.captions.toggles, 1)
        controls.set_captions(False)
        self.assertFalse(self.captions.on)
        controls.set_captions(False)
        self.assertEqual(self.captions.toggles, 2)
        self.captions.going = True  # the model downloads: on their way
        controls.set_captions(True)
        self.assertEqual(self.captions.toggles, 2)
        controls.set_captions(False)  # calls the download off, as the row's tap does
        self.assertEqual((self.captions.toggles, self.captions.going), (3, False))

    def test_their_question_puts_the_panel_aside_and_a_second_on_waits_for_it(self):
        self.captions.asks = True
        self.click_button("subtitles")
        self.controls.set_captions(True)
        self.assertTrue(self.captions.asking())
        self.assertFalse(self.subtitle_panel.isVisible())  # the question in front
        self.controls.set_captions(True)  # another phone, while it asks: its answer decides
        self.assertEqual(self.captions.toggles, 1)

    def test_the_speech_model_is_chosen_as_its_list_does(self):
        self.assertTrue(self.controls.choose_caption_model("small"))
        self.assertFalse(self.controls.choose_caption_model("huge"))
        self.assertEqual(self.captions.models, ["small"])
        choice = self.controls.captions_choice()
        self.assertEqual(choice.model, "small")
        self.assertEqual([m[0] for m in choice.models], ["auto", "small", "large"])

    def test_the_language_is_chosen_while_they_are_on(self):
        controls = self.controls
        self.assertFalse(controls.choose_caption_language("Turkish"))  # off: no Language row
        self.captions.on = True
        self.assertTrue(controls.choose_caption_language("Turkish"))
        self.assertFalse(controls.choose_caption_language("Klingon"))
        self.assertTrue(controls.choose_caption_language("auto"))
        self.assertEqual(self.captions.picked, ["Turkish", "auto"])


class DownloadQuestionTests(unittest.TestCase):
    """2.9: AI captions' download question on the TV's card, answered there or elsewhere (the
    phone remote that turned them on): either answer goes for both."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.captions_controller import CaptionsController
        from ui.overlays.question_card import QuestionCard

        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.card = QuestionCard(self.host)
        self.captions = CaptionsController(self.host, card=self.card)
        self.captions._choose_tier = lambda: "small"
        self.begun = []
        self.captions._begin_download = lambda: self.begun.append(True)
        self.changes = []
        self.captions.changed.connect(lambda: self.changes.append(True))
        missing = mock.patch("services.captions.models.download_size", return_value=900_000_000)
        missing.start()
        self.addCleanup(missing.stop)
        absent = mock.patch("services.captions.models.installed", return_value=False)
        absent.start()
        self.addCleanup(absent.stop)
        measured = mock.patch("services.captions.tier.known_score", return_value=1.0)
        measured.start()
        self.addCleanup(measured.stop)

    def test_a_model_chosen_while_its_question_is_up_is_the_one_asked_for(self):
        # 2.9, the review: the phone stepped to Faster while the TV asked for the 1.7B, and
        # Download fetched the 1.7B.
        chosen = {"tier": "large"}
        self.captions._choose_tier = lambda: chosen["tier"]
        sizes = {"large": 2_300_000_000, "small": 878_702_423}
        with mock.patch(
            "services.captions.models.download_size", side_effect=lambda tier: sizes[tier]
        ):
            self.captions.toggle()
            self.assertIn("2.3 GB", self.captions.question()[0])
            chosen["tier"] = "small"
            self.captions.set_model("small")
        self.assertTrue(self.captions.asking())
        self.assertIn("879 MB", self.captions.question()[0])
        self.assertEqual(self.card.question(), self.captions.question()[0])
        self.captions.answer(True)
        self.assertEqual((self.begun, self.captions._fetching), ([True], "small"))

    def test_the_question_is_asked_with_its_words(self):
        self.assertFalse(self.captions.asking())
        self.captions.toggle()
        self.assertTrue(self.captions.asking())
        text, yes, no = self.captions.question()
        self.assertEqual((yes, no), ("Download", "Not now"))
        self.assertIn("900 MB", text)
        self.assertEqual(self.card.question(), text)
        self.assertTrue(self.card.isVisible())

    def test_answered_elsewhere_the_card_goes_and_the_download_begins(self):
        self.captions.toggle()
        self.captions.answer(True)
        self.assertFalse(self.card.isVisible())
        self.assertFalse(self.card._timer.isActive())  # nothing answers "Not now" later
        self.assertFalse(self.captions.asking())
        self.assertEqual(self.begun, [True])
        self.captions.answer(True)  # answered once
        self.assertEqual(self.begun, [True])

    def test_all_here_already_it_is_unpacked_without_a_question(self):
        # A cancel while unpacking left the whole archive: 0 bytes to fetch, nothing to ask.
        with mock.patch("services.captions.models.download_size", return_value=0):
            self.captions.toggle()
        self.assertFalse(self.captions.asking())
        self.assertFalse(self.card.isVisible())
        self.assertEqual(self.begun, [True])

    def test_not_now_on_the_card_says_the_question_is_gone(self):
        self.captions.toggle()
        self.card.escape()
        self.assertFalse(self.captions.asking())
        self.assertEqual((self.changes, self.begun), ([True], []))  # a phone's closes too


class SpeechModelTests(unittest.TestCase):
    """2.9: the speech model chosen where AI captions are turned on (Settings' choice too)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.captions_controller import CaptionsController

        self.host = QWidget()
        self.addCleanup(self.host.deleteLater)
        self.captions = CaptionsController(self.host)
        self.config = {}
        self.captions._config_get = lambda name, default: self.config.get(name, default)
        self.captions._config_set = self.config.__setitem__
        self.disk = {"small": True, "large": False}
        self.captions._here = lambda name: self.disk[name]
        self.restarts = []
        self.captions._disable = lambda: self.restarts.append("off")
        self.captions.toggle = lambda: self.restarts.append("on")

    def test_the_steps_name_what_automatic_takes_and_go_round(self):
        self.assertEqual(
            self.captions.model_choices(),
            [
                ("auto", "Automatic (0.6B)"),  # the one here
                ("small", "Faster (0.6B)"),
                ("large", "More accurate (1.7B)"),
            ],
        )
        self.assertEqual(self.captions.model_text(), "Automatic (0.6B)")
        for expected in ("small", "large", "auto"):
            self.captions.step_model(1)
            self.assertEqual(self.captions.model(), expected)
        self.captions.step_model(-1)
        self.assertEqual(self.captions.model(), "large")

    def test_chosen_while_on_with_another_they_come_on_again_with_it(self):
        self.captions._enabled, self.captions._tier = True, "small"
        self.captions.set_model("small")  # the one they use: nothing to do
        self.assertEqual(self.restarts, [])
        self.captions.set_model("large")
        self.assertEqual(self.config["ai_captions_model"], "large")
        self.assertEqual(self.restarts, ["off", "on"])  # its question, when it isn't here
        self.assertEqual(self.captions.model_text(), "More accurate (1.7B)")
        self.captions.set_model("huge")
        self.assertEqual(self.config["ai_captions_model"], "large")

    def test_automatic_never_measures_this_computer_on_the_gui_thread(self):
        # 2.9, the review: measured beside the benchmark's own thread, each ran at half speed:
        # the reference machine was given the 0.6B, and the GUI waited 0.45 s.
        from services.captions import tier

        self.disk.update(small=False, large=False)
        warmed = []
        self.captions._warm_tier = lambda: warmed.append(True)
        with (
            mock.patch.object(tier, "known_score", return_value=None),
            mock.patch.object(tier, "machine_score", side_effect=AssertionError("measured")),
            mock.patch.object(tier, "total_ram_bytes", return_value=16 * 1024**3),
            mock.patch.object(tier, "is_arm_mac", return_value=False),
        ):
            self.assertEqual(self.captions.model_text(), "Automatic (1.7B)")  # until known
            self.assertTrue(warmed)
        with (
            mock.patch.object(tier, "known_score", return_value=0.5),
            mock.patch.object(tier, "total_ram_bytes", return_value=16 * 1024**3),
            mock.patch.object(tier, "is_arm_mac", return_value=False),
        ):
            self.assertEqual(self.captions.model_text(), "Automatic (0.6B)")

    def test_turned_on_before_the_speed_is_known_it_comes_on_once_it_is(self):
        from services.captions import tier
        from ui.captions_controller import CaptionsController

        captions = CaptionsController(self.host)
        captions._config_get = lambda name, default: default
        captions._here = lambda name: False
        warmed, decided = [], []
        captions._warm_tier = lambda: warmed.append(True)
        captions._choose_tier = lambda: decided.append(True) or "large"
        with mock.patch.object(tier, "known_score", return_value=None):
            captions.toggle()
        self.assertEqual((warmed, decided), ([True], []))  # nothing decided yet
        with (
            mock.patch.object(tier, "known_score", return_value=1.0),
            mock.patch("services.captions.models.installed", return_value=True),
            mock.patch.object(captions, "_enable") as enable,
        ):
            captions._on_scored()
        enable.assert_called_once_with("large")

    def test_chosen_while_off_it_is_kept_for_their_next_time(self):
        self.captions.set_model("large")
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.captions._choose_tier(), "large")
        self.captions.set_model("auto")
        self.assertEqual(self.captions._choose_tier(), "small")  # the one here


class NoteRowTests(PanelCase):
    def test_a_note_and_a_chevron_widen_the_panel_and_show_whole(self):
        plain = PanelModel("Subtitles", (PanelRow("a", "Language"),))
        self.panel.show_panel(plain)
        width = self.panel.width()
        noted = PanelModel(
            "Subtitles",
            (PanelRow("a", "Language", note="Bahasa Indonesia · audio track", opens=True),),
        )
        self.panel.show_panel(noted)
        self.assertGreater(self.panel.width(), width)
        self.panel.grab()  # paints: nothing raises


class SubtitleFontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_libvlc_draws_subtitles_in_the_captions_family(self):
        from ui.overlays import captions

        with (
            mock.patch.object(captions, "CAPTION_FAMILY", ""),
            mock.patch.object(captions, "QFontInfo") as info,
        ):
            info.return_value.family.return_value = "Segoe UI"
            self.assertEqual(captions.subtitle_font(), "Segoe UI")
            info.return_value.family.return_value = ".AppleSystemUIFont"  # hidden from libVLC
            self.assertEqual(captions.subtitle_font(), "")
        with mock.patch.object(captions, "CAPTION_FAMILY", "Helvetica Neue"):  # a Mac's
            self.assertEqual(captions.subtitle_font(), "")  # libVLC's own is it: no lookup

    def test_the_captions_are_drawn_in_that_family_at_a_regular_weight(self):
        from PySide6.QtGui import QFont

        from ui.overlays import captions

        host = QWidget()
        host.resize(1280, 720)
        self.addCleanup(host.deleteLater)
        with mock.patch.object(captions, "CAPTION_FAMILY", "Helvetica Neue"):
            overlay = captions.CaptionsOverlay(host)
            overlay.show_text("Merhaba")
        self.assertEqual(overlay._font.family(), "Helvetica Neue")
        self.assertEqual(overlay._font.weight(), QFont.Weight.Normal)


class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.transport = Transport(self.host, idle_ms=60_000)
        self.transport.show_live("Harbour Nature", "Rivers")
        self.transport.hide()
        self.overlay = CaptionsOverlay(self.host)
        self.overlay.follow(self.transport)

    def test_it_sits_at_the_bottom_centre_and_takes_no_click(self):
        self.overlay.show_text("Good evening and welcome")
        geometry = self.overlay.geometry()
        self.assertTrue(self.overlay.isVisible())
        self.assertAlmostEqual(geometry.center().x(), 640, delta=2)
        self.assertGreater(geometry.bottom(), 600)
        self.assertLess(geometry.bottom(), 720)
        self.assertEqual(self.overlay.lines(), ["Good evening and welcome"])
        self.assertTrue(self.overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))

    def test_it_rises_above_the_player_controls_while_they_show(self):
        self.overlay.show_text("Good evening")
        low = self.overlay.geometry().bottom()
        self.transport.show()
        self.assertLess(self.overlay.geometry().bottom(), self.transport.controls_top())
        self.assertLess(self.overlay.geometry().bottom(), low)
        self.transport.hide()
        self.assertEqual(self.overlay.geometry().bottom(), low)

    def test_two_lines_at_most_and_a_longer_caption_is_cut(self):
        words = " ".join(["harbour"] * 12)
        self.overlay.show_text(words)
        self.assertEqual(len(self.overlay.lines()), 2)
        self.overlay.show_text(" ".join(["lighthouse"] * 80))
        lines = self.overlay.lines()
        self.assertEqual(len(lines), MAX_LINES)
        self.assertTrue(lines[-1].endswith("…"))
        self.overlay.grab()

    def test_two_lines_are_of_about_one_length(self):
        # 2.9, the owner: "stretching weird": a full top line over a few words made the box
        # as wide as the picture for every long caption.
        from PySide6.QtGui import QFontMetricsF

        text = (
            "da silah alımı konusunda da er tutluk olaylı gösteriz ve bunu herkes biliyor "
            "ama yardıma ikna."
        )
        self.overlay.show_text(text)
        top, bottom = self.overlay.lines()
        fm = QFontMetricsF(self.overlay._font)
        self.assertLess(
            abs(fm.horizontalAdvance(top) - fm.horizontalAdvance(bottom)),
            0.25 * fm.horizontalAdvance(top + " " + bottom),
        )
        self.assertEqual(" ".join((top, bottom)), text)  # nothing lost
        self.overlay.show_text("Good evening and welcome")
        self.assertEqual(self.overlay.lines(), ["Good evening and welcome"])  # one stays one

    def test_it_hides_when_cleared(self):
        self.overlay.show_text("Good evening")
        self.overlay.clear()
        self.assertFalse(self.overlay.isVisible())
        self.assertEqual(self.overlay.text(), "")
        self.overlay.show_text("   ")
        self.assertFalse(self.overlay.isVisible())

    def test_its_size_follows_the_picture(self):
        self.overlay.show_text("Good evening")
        big = self.overlay.font_px()
        self.host.resize(2560, 720)  # wider than 16:9: as a 1280 × 720 picture's
        QApplication.processEvents()
        self.assertEqual(self.overlay.font_px(), big)
        self.host.resize(480, 270)  # a mini player
        QApplication.processEvents()
        self.assertGreaterEqual(self.overlay.font_px(), MIN_FONT_PX)
        self.assertLess(self.overlay.font_px(), big)
        self.assertLessEqual(self.overlay.geometry().right(), 480)


class SizeTextTests(unittest.TestCase):
    def test_sizes_read_as_megabytes_and_gigabytes(self):
        self.assertEqual(size_text(878_702_423), "879 MB")
        self.assertEqual(size_text(2_300_000_000), "2.3 GB")
        self.assertEqual(size_text(2_000_000_000), "2 GB")
        self.assertEqual(captions_space_text(None), "")
        self.assertEqual(captions_space_text(0), "Nothing downloaded")
        self.assertEqual(captions_space_text(880_000_000), "Downloaded: 880 MB")


class FakeModels(SimpleNamespace):
    """services.captions.models' space_used and delete_all, over a number."""

    def space_used(self):
        return self.size

    def delete_all(self):
        self.deleted += 1
        self.size = 0


class CaptionsSettingsTests(base.WindowCase):
    def dialog(self):
        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def models(self, size):
        models = FakeModels(size=size, deleted=0)
        patch = mock.patch.dict(sys.modules, {"services.captions.models": models})
        patch.start()
        self.addCleanup(patch.stop)
        return models

    def test_the_model_and_the_language_are_kept_at_save(self):
        self.models(0)
        dialog = self.dialog()
        self.assertEqual(dialog.ai_captions_model_combo.currentData(), "auto")
        self.assertEqual(dialog.ai_captions_language_combo.currentData(), "auto")
        self.assertEqual(dialog.ai_captions_language_combo.count(), 31)  # Auto and Qwen's 30
        group = dialog.ai_captions_model_combo.parentWidget()
        self.assertEqual(group.objectName(), "aiCaptionsSettings")
        dialog.ai_captions_model_combo.setCurrentIndex(
            dialog.ai_captions_model_combo.findData("large")
        )
        dialog.ai_captions_language_combo.setCurrentIndex(
            dialog.ai_captions_language_combo.findData("Turkish")
        )
        self.assertEqual(dialog.ai_captions_language_combo.currentText(), "Türkçe")
        self.assertTrue(dialog.has_unsaved_changes())
        dialog.save_settings()
        saved = ConfigManager()
        self.assertEqual(
            (saved.ai_captions_model, saved.ai_captions_language), ("large", "Turkish")
        )
        dialog = self.dialog()
        self.assertEqual(dialog.ai_captions_model_combo.currentData(), "large")
        self.assertEqual(dialog.ai_captions_language_combo.currentData(), "Turkish")

    def test_older_settings_read_as_automatic_and_unknown_values_too(self):
        config = ConfigManager()
        for key in ("ai_captions_model", "ai_captions_language"):
            config.config.pop(key, None)
        self.assertEqual((config.ai_captions_model, config.ai_captions_language), ("auto", "auto"))
        config.config.update(ai_captions_model="huge", ai_captions_language="tr")
        self.assertEqual((config.ai_captions_model, config.ai_captions_language), ("auto", "auto"))

    def test_the_space_used_shows_and_delete_asks_first(self):
        models = self.models(880_000_000)
        dialog = self.dialog()
        self.assertTrue(wait_until(lambda: dialog.ai_captions_delete_button.isEnabled()))
        self.assertEqual(dialog.ai_captions_space.text(), "Downloaded: 880 MB")
        captions = FakeCaptions()
        captions.on = True
        playing = mock.patch.object(self.window, "captions", captions, create=True)
        playing.start()
        self.addCleanup(playing.stop)
        no = QMessageBox.StandardButton.No
        with mock.patch.object(QMessageBox, "question", return_value=no) as asked:
            dialog.ai_captions_delete_button.click()
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(models.deleted, 0)
        yes = QMessageBox.StandardButton.Yes
        with mock.patch.object(QMessageBox, "question", return_value=yes):
            dialog.ai_captions_delete_button.click()
        self.assertTrue(wait_until(lambda: models.deleted == 1))
        self.assertTrue(wait_until(lambda: dialog.ai_captions_space.text() == "Nothing downloaded"))
        self.assertFalse(dialog.ai_captions_delete_button.isEnabled())
        self.assertFalse(captions.on)  # they stopped first


class CaptionsWindowTests(base.WindowCase):
    def test_the_overlay_sits_above_the_tiles_under_the_rest(self):
        window, host = self.window, self.window.host
        self.assertIs(window.overlays.captions, window.captions_overlay)
        order = [w for w in host.children() if isinstance(w, QWidget) and w in host._overlays]
        captions = order.index(window.captions_overlay)
        self.assertLess(order.index(window.grid_tiles), captions)
        self.assertLess(captions, order.index(window.overlays.banner))
        self.assertLess(captions, order.index(window.overlays.transport))
        self.assertLess(captions, order.index(window.subtitle_panel))


if __name__ == "__main__":
    unittest.main()
