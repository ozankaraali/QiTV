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
from PySide6.QtWidgets import QApplication, QComboBox, QMessageBox, QWidget
from test_track_panel import CHANNEL, FILM, FakePlayer, PanelCase
from timer_fixtures import wait_until

from services.captions.languages import QWEN_LANGUAGES, iso_code, own_name, qwen_name
from services.captions.streaming import TRANSLATE_LEAD_S
from services.config_manager import ConfigManager
from services.i18n import translate
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
    AI_TRANSLATE,
    AI_TRANSLATE_PICK,
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
        for given in ("", None, "und", "mul", "qaa", "Swahili", "xx"):
            with self.subTest(given):
                self.assertIsNone(qwen_name(given))

    def test_the_european_languages_only_parakeet_writes(self):
        # 2.9.3: a sound track in Ukrainian (or one chosen by hand) is written by Parakeet.
        from services.captions.languages import CAPTION_LANGUAGES, EUROPEAN, QWEN_LANGUAGES

        for given in ("uk", "ukr", "uk-UA", "Українська"):
            with self.subTest(given):
                self.assertEqual(qwen_name(given), "Ukrainian")
        self.assertEqual(own_name("Slovak"), "Slovenčina")
        self.assertNotIn("Ukrainian", QWEN_LANGUAGES)  # never Qwen's language lock
        self.assertIn("Ukrainian", CAPTION_LANGUAGES)
        self.assertTrue(set(CAPTION_LANGUAGES) - set(QWEN_LANGUAGES) <= EUROPEAN)

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
        self.target = ""  # 2.10: the language they are translated into ("": off)
        self.translated = []  # set_translate_to's calls
        self.translate_asks = False  # a language asks its model's download question first
        self.offs = []  # give_way's `off`, each time

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

    def give_way(self, *, off=False):
        """A subtitle track, Off or a file chosen over them: off, a download too (no toggle).
        `off`: Off chosen (kept for the next video file: CaptionsController.give_way)."""
        self.on = self.going = False
        self.offs.append(off)

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

    def translate_to(self):
        return self.target

    def translate_languages(self):
        return [("Turkish", "Türkçe"), ("English", "English"), ("German", "Deutsch")]

    def set_translate_to(self, name):
        self.translated.append(name)
        self.target = name
        self.asked = bool(name) and self.translate_asks
        self.changed.emit()


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
                "Translate to [Off]",
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

    def test_the_translate_list_starts_at_off_picks_one_and_comes_back(self):
        # 2.10, the owner's: "Translate to" opens a list as Language does, while they are on
        self.captions.on = True
        self.click_button("subtitles")
        row = self.rows()[[r.key for r in self.rows()].index(AI_TRANSLATE)]
        self.assertEqual((row.note, row.opens), ("Off", True))
        self.click_row(AI_TRANSLATE)
        self.assertEqual(self.subtitle_panel.model().title, "Translate to")
        self.assertEqual(self.texts(), ["Subtitles", "*Off", "Türkçe", "English", "Deutsch"])
        self.assertEqual(self.rows()[0].key, AI_BACK)
        self.click_row((AI_TRANSLATE_PICK, "English"))
        self.assertEqual(self.captions.translated, ["English"])
        self.assertEqual(self.subtitle_panel.model().title, "Subtitles")
        row = self.rows()[[r.key for r in self.rows()].index(AI_TRANSLATE)]
        self.assertEqual(row.note, "English")
        self.click_row(AI_TRANSLATE)
        self.assertEqual(self.texts()[1:4], ["Off", "Türkçe", "*English"])
        self.assertTrue(self.controls.handle(Action.SEEK_BACK))  # ← goes back, as Language's
        self.assertEqual(self.subtitle_panel.model().title, "Subtitles")
        self.click_row(AI_TRANSLATE)
        self.click_row((AI_TRANSLATE_PICK, ""))
        self.assertEqual(self.captions.translated, ["English", ""])
        self.click_row(AI_TRANSLATE)
        self.captions.on = False  # off meanwhile (a phone): the list goes, and so does its row
        self.captions.changed.emit()
        self.assertEqual(self.subtitle_panel.model().title, "Subtitles")
        self.assertNotIn(AI_TRANSLATE, [r.key for r in self.rows()])

    def test_a_language_whose_model_must_download_puts_its_question_in_front(self):
        self.captions.on, self.captions.translate_asks = True, True
        self.click_button("subtitles")
        self.click_row(AI_TRANSLATE)
        self.click_row((AI_TRANSLATE_PICK, "Turkish"))
        self.assertTrue(self.captions.asking())
        self.assertFalse(self.subtitle_panel.isVisible())
        self.click_button("subtitles")
        self.assertEqual(self.subtitle_panel.model().title, "Subtitles")  # its own list again

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
        # Review M3: the panel's Off is the viewer's no, kept for the next video file; a track
        # is a choice for this file only
        self.assertEqual(self.captions.offs, [False, True])

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
        self.assertEqual(self.captions.offs, [False])  # a file: for this one only

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
        self.assertEqual(self.captions.offs, [False, True])  # the phone's Off is kept too

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

    def test_the_translation_is_listed_and_chosen_as_its_row_does(self):
        # 2.10: while they are on, as the Translate to row (and Language's)
        controls = self.controls
        off = controls.captions_choice()
        self.assertEqual((off.translate_text, off.translations), ("", ()))
        self.assertFalse(controls.choose_caption_translation("Turkish"))  # off: no row
        self.assertEqual(self.captions.translated, [])
        self.captions.on = True
        choice = controls.captions_choice()
        self.assertEqual((choice.translate, choice.translate_text), ("", "Off"))
        self.assertEqual(
            choice.translations,
            (("", "Off"), ("Turkish", "Türkçe"), ("English", "English"), ("German", "Deutsch")),
        )
        self.assertTrue(controls.choose_caption_translation("Turkish"))
        self.assertFalse(controls.choose_caption_translation("Klingon"))
        self.assertFalse(controls.choose_caption_translation("auto"))
        choice = controls.captions_choice()
        self.assertEqual((choice.translate, choice.translate_text), ("Turkish", "Türkçe"))
        self.assertTrue(controls.choose_caption_translation(""))  # Off
        self.assertEqual(self.captions.translated, ["Turkish", ""])
        self.playing = False
        self.assertFalse(controls.choose_caption_translation("Turkish"))  # nothing plays

    def test_a_translation_that_asks_first_puts_the_panel_aside(self):
        self.captions.on, self.captions.translate_asks = True, True
        self.click_button("subtitles")
        self.assertTrue(self.controls.choose_caption_translation("Turkish"))
        self.assertFalse(self.subtitle_panel.isVisible())  # the question in front


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

    def test_the_1_7b_not_downloaded_automatic_offers_the_0_6b(self):
        # 2.9.1: the 1.7B comes from a community export, which can go.
        self.captions._choose_tier = lambda: "large"
        toasts = []
        self.captions._toast = lambda text, kind="info": toasts.append(text)
        sizes = {"large": 2_300_000_000, "small": 878_702_423}
        with mock.patch(
            "services.captions.models.download_size", side_effect=lambda tier: sizes[tier]
        ):
            self.captions.toggle()
            self.captions.answer(True)
            download = self.captions._download = object()
            self.captions._on_download_failed(download, "Download failed: 404")
            self.assertEqual(toasts, ["Download failed: 404"])  # why, as before
            text, yes, _no = self.captions.question()
            self.assertIn("879 MB", text)
            self.assertEqual(self.card.question(), text)
            self.assertTrue(self.captions.asked_unprompted())  # every phone hears it
            self.captions.answer(True)
        self.assertEqual((self.begun, self.captions._fetching), ([True, True], "small"))

    def test_a_1_7b_the_viewer_chose_is_never_swapped_for_another(self):
        self.captions._choose_tier = lambda: "large"
        self.captions.model = lambda: "large"
        self.captions._toast = lambda text, kind="info": None
        self.captions.toggle()
        self.captions.answer(True)
        download = self.captions._download = object()
        self.captions._on_download_failed(download, "Download failed: 404")
        self.assertFalse(self.captions.asking())

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
        self.captions._toggle = lambda: self.restarts.append("on")  # (not the viewer's toggle)

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


class TranslationTests(unittest.TestCase):
    """2.10: AI captions translated into the language the viewer picks: the list, its model's
    question, the picture held the translator's time further back (a film's sound read further
    ahead), and Automatic's speech model while translating. 2.11.2, the owner: the language is
    remembered per spoken language, and nothing is translated before that language is known
    (a channel plays whose language isn't known yet: the tests pick or detect one)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from test_captions_pipeline import _Card, _Controller, _Live, _Overlay, _Target

        from ui.captions_controller import CaptionsController

        self.window = QWidget()
        self.addCleanup(self.window.deleteLater)
        self.window.controller = _Controller(_Target(live=True))  # a channel ("films|A film")
        self.window.live_session = _Live()
        self.window.live_session.position_s = 100.0
        self.card = _Card()
        self.overlay = _Overlay()
        self.captions = CaptionsController(self.window, card=self.card, overlay=self.overlay)
        process = self.captions._process
        self.sent, self.loaded, self.stopped = [], [], []
        self.alive = True
        process.send = lambda *m: self.sent.append(m)
        process.running = lambda: self.alive
        process.stop = lambda: self.stop()
        process.start = lambda load, detailed_log=False: self.start(load)
        self.config = {"ai_captions_model": "small"}
        self.captions._config_get = lambda name, default: self.config.get(name, default)
        self.captions._config_set = self.config.__setitem__
        self.disk = dict.fromkeys(("small", "large", "turkish", "european", "cohere"), True)
        self.disk["translation"] = True
        self.captions._here = lambda name: self.disk[name]
        self.toasts = []
        self.captions._toast = lambda text, kind="info": self.toasts.append(text)
        self.captions._tier = "small"
        self.captions._enabled = self.captions._ready = True
        self.captions._hold_floor()
        self.captions._start_run()

    def stop(self):
        self.alive = False
        self.stopped.append(True)

    def start(self, load):
        self.alive = True
        self.loaded.append(load)

    def folder(self):
        from services.captions import models

        return str(models.captions_root() / models.TRANSLATION_FOLDER)

    def machine(self, score):
        """This computer as Automatic sees it: measured at `score`, an Intel one with 16 GB."""
        from services.captions import tier

        for name, value in (
            ("known_score", score),
            ("is_arm_mac", False),
            ("total_ram_bytes", 16 * 1024**3),
        ):
            patch = mock.patch.object(tier, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_list_has_the_app_language_first_then_the_rest_by_their_own_names(self):
        from services.captions.mt_languages import MT_LANGUAGES

        with mock.patch("services.i18n.language", return_value="tr"):
            listed = self.captions.translate_languages()
        self.assertEqual(listed[0], ("Turkish", "Türkçe"))
        self.assertEqual(sorted(name for name, _own in listed), sorted(MT_LANGUAGES))  # once each
        rest = [own for _name, own in listed[1:]]
        self.assertEqual(rest, sorted(rest, key=str.casefold))
        self.assertIn(("Chinese (Simplified)", "简体中文"), listed)
        # 2.10, the owner's: "a serbian if they know russian and wants to translate to russian"
        with mock.patch("services.i18n.language", return_value="sr"):
            listed = self.captions.translate_languages()
        self.assertEqual(len(listed), 46)
        owns = [own for _name, own in listed]
        self.assertEqual(owns, sorted(owns, key=str.casefold))
        self.assertIn(("Russian", "Русский"), listed)

    def test_the_list_has_the_languages_used_first_then_the_app_language(self):
        # 2.11.2: the languages any rule translates into (but the one heard), by their own
        # names; then the app language's (a shortcut only: it chooses nothing); then the rest
        from services.captions.mt_languages import MT_LANGUAGES

        self.config["ai_captions_translations"] = {
            "German": "Turkish",
            "French": "English",
            "Arabic": "",
            "Spanish": "Turkish",
        }
        self.captions.set_language("German")
        self.assertEqual(self.captions.recent_translations(), ["English", "Turkish"])
        with mock.patch("services.i18n.language", return_value="de"):
            listed = self.captions.translate_languages()
        self.assertEqual(
            listed[:3], [("English", "English"), ("Turkish", "Türkçe"), ("German", "Deutsch")]
        )
        self.assertEqual(sorted(name for name, _own in listed), sorted(MT_LANGUAGES))  # once each
        rest = [own for _name, own in listed[3:]]
        self.assertEqual(rest, sorted(rest, key=str.casefold))
        self.captions.set_language("English")  # heard: not offered as its own translation
        self.assertEqual(self.captions.recent_translations(), ["Turkish"])
        with mock.patch("services.i18n.language", return_value="tr"):
            listed = self.captions.translate_languages()
        self.assertEqual(listed[0], ("Turkish", "Türkçe"))  # used, and the app's: once
        self.assertEqual(len(listed), 46)

    def test_translation_is_remembered_per_spoken_language(self):
        self.captions.set_language("German")
        self.captions.set_translate_to("English")
        self.assertEqual(self.sent[-1], ("translate", "English", self.folder()))
        self.captions.set_language("English")
        self.assertEqual(self.captions.translate_to(), "")  # never chosen: off
        self.assertEqual(self.sent[-1], ("translate", None))
        self.assertEqual(self.window.live_session.floor, 8.0)
        self.captions.set_translate_to("Turkish")
        self.captions.set_language("German")
        self.assertEqual(self.captions.translate_to(), "English")
        self.assertEqual(
            self.config["ai_captions_translations"], {"German": "English", "English": "Turkish"}
        )
        self.captions.set_translate_to("")  # no translation, remembered as such
        self.assertEqual(
            self.config["ai_captions_translations"], {"German": "", "English": "Turkish"}
        )
        self.assertEqual(self.captions.translate_to(), "")

    def test_nothing_is_translated_or_set_while_the_language_is_unknown(self):
        self.config["ai_captions_translations"] = {"German": "English"}
        self.assertTrue(self.captions.translation_waits())
        changed = []
        self.captions.changed.connect(lambda: changed.append(True))
        with self.assertNoLogs("ui.captions_controller", "INFO"):
            self.captions.set_translate_to("Turkish")
        self.assertEqual(self.config["ai_captions_translations"], {"German": "English"})
        self.assertEqual(self.captions.translate_to(), "")
        self.assertEqual(self.captions.recent_translations(), ["English"])
        self.assertEqual((changed, self.card.asked), ([], []))
        self.assertNotIn("translate", [message[0] for message in self.sent])
        self.captions.set_language("German")
        self.assertFalse(self.captions.translation_waits())

    def test_a_language_detected_mid_run_applies_its_rule(self):
        # on, ready, a live run going, the language unknown (setUp)
        self.config["ai_captions_translations"] = {"German": "English"}
        self.captions._on_detected(self.captions._run, "German")
        self.assertEqual(self.sent[-1], ("translate", "English", self.folder()))
        self.assertEqual(self.window.live_session.floor, 8.0 + TRANSLATE_LEAD_S)
        self.captions._on_detected(self.captions._run, "English")  # the programme changed
        self.assertEqual(self.sent[-1], ("translate", None))
        self.assertEqual(self.window.live_session.floor, 8.0)

    def test_a_language_detected_mid_run_moves_automatics_model_with_its_rule(self):
        # on a slower computer: German detected (translated) takes the 0.6B here, and the
        # 1.7B comes back once the programme turns English (no rule); what was heard survives
        # the reload, and they stay on
        self.config["ai_captions_model"] = "auto"
        self.captions._tier = "large"
        self.machine(1.2)
        installed = mock.patch("services.captions.models.installed", return_value=True)
        installed.start()
        self.addCleanup(installed.stop)
        self.config["ai_captions_translations"] = {"German": "English"}
        self.captions._on_detected(self.captions._run, "German")
        self.assertEqual(self.stopped, [True])
        [load] = self.loaded
        self.assertIn("qwen3-asr-0.6b", load["model"]["encoder"])
        self.assertEqual((load["translate_to"], load["translation"]), ("English", self.folder()))
        self.assertEqual(self.window.live_session.floor, 8.0 + TRANSLATE_LEAD_S)
        self.captions._on_ready()  # the 0.6B loaded: its run starts in what was heard
        self.assertEqual([m for m in self.sent if m[0] == "start"][-1][5], "German")
        self.captions._on_detected(self.captions._run, "English")  # the programme changed
        self.assertEqual(len(self.loaded), 2)
        self.assertIn("qwen3-asr-1.7b", self.loaded[-1]["model"]["encoder"])
        self.assertEqual(self.loaded[-1]["translate_to"], None)
        self.assertEqual(self.window.live_session.floor, 10.0)
        self.assertTrue(self.captions.enabled())

    def test_turned_on_by_hand_automatic_goes_by_the_rule_for_what_plays(self):
        # the language a channel watched before spoke (translated) is not this one's: nothing
        # known here, so Automatic takes the model here with no wait for the speed
        self.config["ai_captions_translations"] = {"German": "English"}
        self.config["ai_captions_model"] = "auto"
        self.machine(None)
        self.captions._warm_tier = lambda: None  # never a benchmark here
        self.captions._language = ("German", "")
        self.captions._disable()
        with mock.patch("services.captions.models.installed", return_value=True):
            self.captions.toggle()
        self.assertTrue(self.captions.enabled())
        self.assertFalse(self.captions._toggle_waits)
        load = self.loaded[-1]
        self.assertIn("qwen3-asr-1.7b", load["model"]["encoder"])
        self.assertEqual((load["translate_to"], load["translation"]), (None, None))

    def test_a_zap_to_a_language_without_a_rule_stops_translating(self):
        from test_captions_pipeline import _Target

        from ui.captions_texts import FROM_VIEWER

        self.config["ai_captions_translations"] = {"German": "English"}
        self.captions.set_language("German")
        self.assertEqual(self.sent[-1], ("translate", "English", self.folder()))
        self.assertEqual(self.window.live_session.floor, 8.0 + TRANSLATE_LEAD_S)
        other = _Target(live=True)
        other.title = "Another channel"
        picks = self.config["ai_captions_picks"]
        self.config["ai_captions_picks"] = {**picks, "films|Another channel": "Turkish"}
        self.window.controller.target = other  # the next channel: its own session
        self.window.live_session.source = ("http://127.0.0.1:1/t/s2/side/3.ts", 3, 0.0)
        runs = self.captions._run
        self.captions._tick()
        self.assertEqual(self.captions._run, runs + 1)  # its run started
        self.assertEqual(self.captions._language, ("Turkish", FROM_VIEWER))
        self.assertEqual(self.sent[-1], ("translate", None))
        self.assertEqual(self.window.live_session.floor, 8.0)

    def test_captions_turned_on_where_the_language_is_unknown_load_untranslated(self):
        self.config["ai_captions_translations"] = {"German": "English"}
        self.captions._language = ("German", "")  # what a channel watched before spoke
        self.captions._disable()
        with mock.patch("services.captions.models.installed", return_value=True):
            self.captions.toggle()
        self.assertTrue(self.captions.enabled())
        load = self.loaded[-1]
        self.assertEqual((load["translate_to"], load["translation"]), (None, None))
        self.assertEqual((self.card.asked, self.window.live_session.floor), ([], 8.0))

    def test_a_language_without_its_model_asks_once_then_it_downloads_and_translates(self):
        from services.captions import models
        from ui.captions_texts import TRANSLATE_QUESTION

        self.captions.set_language("Turkish")
        self.disk["translation"] = False
        sizes = {models.TRANSLATION_KEY: 726_000_000}
        with mock.patch.object(models, "download_size", side_effect=lambda key: sizes[key]):
            self.captions.set_translate_to("English")
            self.captions.set_translate_to("English")  # again while it asks: asked once
        [(text, yes, no)] = self.card.asked
        self.assertEqual(text, translate(TRANSLATE_QUESTION, size="726 MB"))
        self.assertEqual((yes, no), ("Download", "Not now"))
        self.assertTrue(self.captions.asked_unprompted())  # every phone hears it too
        self.assertEqual(self.captions.translate_to(), "English")  # the row says the one asked
        self.assertEqual(self.window.live_session.floor, 8.0)  # nothing held for it yet
        self.assertNotIn("translate", [message[0] for message in self.sent])
        started = []
        self.captions._start_download = lambda: started.append(self.captions._fetching)
        self.captions.answer(True)
        self.assertEqual(started, [models.TRANSLATION_KEY])
        download = self.captions._download = object()
        self.disk["translation"] = True
        self.captions._on_downloaded(download)
        self.assertEqual(self.sent[-1], ("translate", "English", self.folder()))
        self.assertEqual(self.window.live_session.floor, 8.0 + TRANSLATE_LEAD_S)
        self.assertEqual((self.loaded, self.stopped), ([], []))  # the same speech model reads on
        self.assertTrue(self.captions.enabled())

    def test_not_now_leaves_translation_off(self):
        self.captions.set_language("Turkish")
        self.disk["translation"] = False
        with mock.patch("services.captions.models.download_size", return_value=726_000_000):
            self.captions.set_translate_to("English")
        self.captions.answer(False)
        self.assertEqual(self.captions.translate_to(), "")
        self.assertEqual(self.config["ai_captions_translations"], {"Turkish": ""})
        self.assertIsNone(self.captions._download)
        self.assertEqual(self.window.live_session.floor, 8.0)

    def test_off_while_its_question_is_up_takes_the_question_away(self):
        self.captions.set_language("Turkish")
        self.disk["translation"] = False
        with mock.patch("services.captions.models.download_size", return_value=726_000_000):
            self.captions.set_translate_to("English")
        self.captions.set_translate_to("")
        self.assertEqual((self.card.withdrawn, self.captions.asking()), (1, False))
        self.assertEqual(self.captions.translate_to(), "")

    def test_a_download_that_failed_says_why_and_leaves_translation_off(self):
        self.captions.set_language("Turkish")
        self.disk["translation"] = False
        self.captions._begin_download = lambda: setattr(self.captions, "_download", object())
        with mock.patch("services.captions.models.download_size", return_value=726_000_000):
            self.captions.set_translate_to("English")
        self.captions.answer(True)
        self.captions._on_download_failed(self.captions._download, "Download failed: 404")
        self.assertEqual(self.toasts, ["Download failed: 404"])
        self.assertEqual(self.captions.translate_to(), "")
        self.assertEqual(self.config["ai_captions_translations"], {"Turkish": ""})
        self.assertTrue(self.captions.enabled())  # the captions go on, as written

    def test_off_while_its_model_downloads_calls_the_download_off(self):
        cancelled = []

        class Download:
            def running(self):
                return True

            def cancel(self):
                cancelled.append(True)

        self.captions.set_language("Turkish")
        self.disk["translation"] = False
        self.captions._begin_download = lambda: setattr(self.captions, "_download", Download())
        with mock.patch("services.captions.models.download_size", return_value=726_000_000):
            self.captions.set_translate_to("English")
        self.captions.answer(True)
        self.captions._show("Merhaba")  # a caption on screen meanwhile
        self.captions.set_translate_to("")
        self.assertEqual(cancelled, [True])
        self.assertIsNone(self.captions._download)
        self.assertTrue(self.captions.enabled())
        self.assertEqual(self.overlay.text, "Merhaba")  # it stays its time

    def test_a_pick_made_while_another_question_was_up_is_asked_once_that_one_is_answered(self):
        from services.captions import models

        sizes = {"turkish": 278_000_000, models.TRANSLATION_KEY: 726_000_000}
        self.disk.update(turkish=False, translation=False)
        self.captions._language = ("Turkish", "")
        with mock.patch.object(models, "download_size", side_effect=lambda key: sizes[key]):
            self.captions._offer_specialist()  # QiTV's own question first
            self.captions.set_translate_to("English")  # meanwhile: it waits for that one
            self.assertEqual(len(self.card.asked), 1)
            self.captions.answer(False)
        self.assertEqual(len(self.card.asked), 2)
        self.assertIn("726 MB", self.card.asked[-1][0])
        self.assertTrue(self.captions.asking())

    def test_a_languages_own_model_waiting_for_the_translation_model_is_asked_for_after_it(self):
        from services.captions import models

        sizes = {"turkish": 278_000_000, models.TRANSLATION_KEY: 726_000_000}
        self.captions.set_language("Turkish")
        self.disk["translation"] = False
        self.captions._begin_download = lambda: setattr(self.captions, "_download", object())
        with mock.patch.object(models, "download_size", side_effect=lambda key: sizes[key]):
            self.captions.set_translate_to("English")
            self.captions.answer(True)
            self.disk["turkish"] = False
            self.captions._language = ("Turkish", "")
            self.captions._offer_specialist()  # while the translation model downloads: later
            self.assertEqual(len(self.card.asked), 1)
            self.disk["translation"] = True
            self.captions._on_downloaded(self.captions._download)
        self.assertEqual(len(self.card.asked), 2)
        self.assertIn("278 MB", self.card.asked[-1][0])

    def test_a_computer_no_translation_model_suits_says_off_at_once(self):
        from services.captions import models

        self.captions.set_language("Turkish")
        self.config["ai_captions_translations"] = {"Turkish": "English", "German": "English"}
        self.disk["translation"] = False
        changed = []
        self.captions.changed.connect(lambda: changed.append(True))
        with (
            mock.patch.object(models, "download_size", side_effect=models.Unsupported("x")),
            self.assertLogs("ui.captions_controller", "WARNING"),
        ):
            self.captions._start_run()
        self.assertEqual(self.captions.translate_to(), "")
        self.assertEqual(self.config["ai_captions_translations"], {})
        self.assertTrue(changed)  # the row and the phones say Off

    def test_the_load_and_the_command_name_one_folder(self):
        from ui.captions_controller import CaptionsController

        self.captions.set_language("Turkish")
        with mock.patch.object(
            CaptionsController, "_translation_folder", return_value="/elsewhere/mt"
        ):
            self.captions.set_translate_to("English")
            self.captions._load_model()
        self.assertEqual(self.sent[-1], ("translate", "English", "/elsewhere/mt"))
        self.assertEqual(self.loaded[-1]["translation"], "/elsewhere/mt")

    def test_off_nothing_of_translation_is_looked_at_and_nothing_changes(self):
        # the off path as before 2.10: no look at the translation model, the hold the
        # model's, Automatic's model the one here, nothing for the process
        from ui.captions_controller import FILM_AHEAD_S, LEAD_S

        looked = []

        def here(name):
            looked.append(name)
            return self.disk[name]  # (none for the translation model: a look would raise)

        self.captions.set_language("Turkish")
        del self.disk["translation"]
        self.captions._here = here
        self.config["ai_captions_model"] = "auto"
        self.machine(1.2)
        self.sent.clear()
        with mock.patch(
            "services.captions.models.download_size", side_effect=AssertionError("asked")
        ):
            self.captions._start_run()
            self.captions.set_translate_to("")
            self.assertEqual(self.captions._picture_lead(), LEAD_S["small"])
            self.assertEqual(self.captions._film_ahead(), FILM_AHEAD_S)
            self.assertEqual(self.captions._auto_tier(), "large")  # the one here, as before
            self.assertFalse(self.captions._speed_unknown())
            self.captions._load_model()
        self.assertNotIn("translation", looked)
        self.assertEqual(
            (self.loaded[-1]["translate_to"], self.loaded[-1]["translation"]), (None, None)
        )
        self.assertEqual(self.window.live_session.floor, LEAD_S["small"])
        self.assertNotIn("translate", [message[0] for message in self.sent])
        self.assertEqual((self.stopped, self.captions._tier), ([], "small"))

    def test_a_language_whose_model_went_is_asked_for_again_as_the_next_run_starts(self):
        # Settings' Delete took the model the language chosen needs: no hold, no translation,
        # and its question when captions read again (a pick asks again too)
        self.captions.set_language("Turkish")
        self.config["ai_captions_translations"] = {"Turkish": "English"}
        self.disk["translation"] = False
        self.assertEqual(self.captions._picture_lead(), 8.0)
        with mock.patch("services.captions.models.download_size", return_value=726_000_000):
            self.captions._start_run()
        [(text, _yes, _no)] = self.card.asked
        self.assertIn("726 MB", text)

    def test_the_picture_is_held_the_translators_time_further_back_while_translating(self):
        from services.captions.streaming import TRANSLATE_LEAD_S
        from ui.captions_controller import LEAD_S

        live = self.window.live_session
        self.captions.set_language("Turkish")
        self.assertEqual((self.captions._picture_lead(), live.floor), (LEAD_S["small"], 8.0))
        self.captions.set_translate_to("English")
        self.assertEqual(self.captions._picture_lead(), LEAD_S["small"] + TRANSLATE_LEAD_S)
        self.assertEqual(
            (live.floor, live.shifted), (8.0 + TRANSLATE_LEAD_S, False)
        )  # it waits there once
        self.assertEqual(self.sent[-1], ("translate", "English", self.folder()))
        self.captions.set_translate_to("German")  # another language: held as it is
        self.assertEqual(live.floor, 8.0 + TRANSLATE_LEAD_S)
        self.assertEqual(self.sent[-1], ("translate", "German", self.folder()))
        self.captions.set_translate_to("")
        self.assertEqual(
            (self.captions._picture_lead(), live.floor, live.shifted), (8.0, 8.0, False)
        )
        self.assertEqual(self.sent[-1], ("translate", None))
        self.captions._tier = "large"
        self.assertEqual(self.captions._picture_lead(), LEAD_S["large"])
        self.config["ai_captions_translations"] = {"Turkish": "English"}
        self.assertEqual(self.captions._picture_lead(), LEAD_S["large"] + TRANSLATE_LEAD_S)
        self.assertEqual((self.loaded, self.stopped), ([], []))  # the viewer's own speech model

    def test_the_load_names_the_language_and_its_model_only_once_the_model_is_here(self):
        def loaded():
            self.captions._load_model()
            return self.loaded[-1]["translate_to"], self.loaded[-1]["translation"]

        self.captions.set_language("Turkish")
        self.assertEqual(loaded(), (None, None))
        self.config["ai_captions_translations"] = {"Turkish": "English"}
        self.disk["translation"] = False
        self.assertEqual(loaded(), (None, None))
        self.disk["translation"] = True
        self.assertEqual(loaded(), ("English", self.folder()))

    def test_the_hold_is_the_tier_s_lead_and_five_seconds_while_translating(self):
        from ui.captions_controller import LEAD_S

        self.captions.set_language("Turkish")
        self.config["ai_captions_translations"] = {"Turkish": "English"}
        self.assertEqual(self.captions._picture_lead(), LEAD_S["small"] + 5.0)

    def test_the_load_says_whether_this_computer_is_slow_to_translate(self):
        def slow(score, arm=False):
            with (
                mock.patch("services.captions.tier.known_score", return_value=score),
                mock.patch("services.captions.tier.is_arm_mac", return_value=arm),
            ):
                self.captions._load_model()
            return self.loaded[-1]["slow_to_translate"]

        self.assertIs(slow(1.2), True)
        self.assertIs(slow(2.0), False)
        self.assertIs(slow(1.2, arm=True), False)
        self.assertIs(slow(None), False)

    def test_a_films_sound_is_read_further_ahead_while_translating(self):
        from test_captions_pipeline import _Controller, _Session, _Target

        from services.captions.streaming import TRANSLATE_LEAD_S
        from ui.captions_controller import FILM_AHEAD_S

        self.window.controller = _Controller(_Target(live=False, local=True))
        self.window.session = _Session("file:///films/a.mkv", 120_000)
        self.captions.set_language("Turkish")  # the film's

        def starts():
            return [message[3] for message in self.sent if message[0] == "film"]

        self.captions._start_run()
        self.assertEqual(starts(), [120.0 + FILM_AHEAD_S])
        self.captions.set_translate_to("English")  # on during it: read again from further ahead
        self.assertEqual(starts(), [120.0 + FILM_AHEAD_S, 120.0 + FILM_AHEAD_S + TRANSLATE_LEAD_S])
        self.captions.set_translate_to("")  # off: it reads on, ahead as it is
        self.assertEqual(len(starts()), 2)
        self.captions._start_run()  # a seek: as far ahead as before
        self.assertEqual(starts()[-1], 120.0 + FILM_AHEAD_S)

    def test_automatic_takes_the_0_6b_while_translating_on_a_slower_computer(self):
        self.captions.set_language("Turkish")
        self.config["ai_captions_model"] = "auto"
        for score, plain, translating in (
            (1.2, "large", "small"),
            (0.99, "large", "small"),
            (2.0, "large", "large"),
        ):
            with self.subTest(score=score):
                self.machine(score)
                self.config["ai_captions_translations"] = {}
                self.assertEqual(self.captions._auto_tier(), plain)  # the one here
                self.config["ai_captions_translations"] = {"Turkish": "English"}
                self.assertEqual(self.captions._auto_tier(), translating)
        self.disk["translation"] = False  # its model not here: not translated
        self.assertEqual(self.captions._auto_tier(), "large")
        self.disk["translation"] = True
        self.machine(1.2)
        self.disk["small"] = False  # R25: the 0.6B only when it is here, else the 1.7B stays
        self.assertEqual(self.captions._auto_tier(), "large")
        self.disk["large"] = False  # neither here (the first time): the 0.6B, its question
        self.assertEqual(self.captions._auto_tier(), "small")
        self.config["ai_captions_model"] = "large"  # the viewer's own choice wins
        self.disk["translation"] = True
        self.assertEqual(self.captions._choose_tier(), "large")

    def test_automatic_s_model_follows_translation_turned_on_and_off_while_they_are_on(self):
        self.captions.set_language("Turkish")
        self.config["ai_captions_model"] = "auto"
        self.captions._tier = "large"
        self.machine(1.2)
        installed = mock.patch("services.captions.models.installed", return_value=True)
        installed.start()
        self.addCleanup(installed.stop)
        self.captions.set_translate_to("English")  # the 0.6B here takes over (R25: they stay on)
        self.assertEqual(self.stopped, [True])
        [load] = self.loaded
        self.assertIn("qwen3-asr-0.6b", load["model"]["encoder"])
        self.assertEqual((load["translate_to"], load["translation"]), ("English", self.folder()))
        self.assertEqual(self.window.live_session.floor, 8.0 + TRANSLATE_LEAD_S)
        self.assertTrue(self.captions.enabled())
        self.captions.set_translate_to("")  # the 1.7B again
        self.assertEqual(len(self.loaded), 2)
        self.assertIn("qwen3-asr-1.7b", self.loaded[-1]["model"]["encoder"])
        self.assertEqual(self.loaded[-1]["translate_to"], None)
        self.assertEqual(self.window.live_session.floor, 10.0)

    def test_on_a_slower_computer_translation_moves_to_the_0_6b_with_captions_going_on(self):
        # R25: picked while the 1.7B wrote, with the 0.6B here: the 0.6B reads on from the
        # picture, and the captions stay on (before: off and on again)
        self.captions.set_language("Turkish")
        self.config["ai_captions_model"] = "auto"
        self.captions._tier = "large"
        self.machine(1.2)
        installed = mock.patch("services.captions.models.installed", return_value=True)
        installed.start()
        self.addCleanup(installed.stop)
        self.captions._show("Merhaba")  # a caption on screen
        disabled: list = []
        self.captions._disable = lambda: disabled.append(True)  # type: ignore[method-assign]
        self.captions.set_translate_to("English")
        self.assertEqual(disabled, [])  # never turned off
        self.assertEqual(self.overlay.text, "Merhaba")  # it stays its time
        self.assertTrue(self.captions.enabled())
        self.assertEqual((self.card.asked, self.toasts), ([], []))  # nothing asked nor said
        [load] = self.loaded
        self.assertIn("qwen3-asr-0.6b", load["model"]["encoder"])
        self.assertEqual((load["translate_to"], load["translation"]), ("English", self.folder()))
        self.assertNotIn("translate", [message[0] for message in self.sent])  # the load says it
        self.assertEqual(self.window.live_session.floor, 8.0 + TRANSLATE_LEAD_S)
        self.assertFalse(self.captions._stepped_down)  # not a step down: off, the 1.7B again
        self.assertEqual(self.captions._auto_tier(), "small")

    def test_on_a_slower_computer_without_the_0_6b_translation_keeps_the_1_7b(self):
        # R25: no question and no restart; the 1.7B goes on translating, and Automatic's step
        # down asks for the 0.6B if it can't keep up
        from ui.captions_texts import STEP_DOWN_QUESTION

        self.captions.set_language("Turkish")
        self.config["ai_captions_model"] = "auto"
        self.captions._tier = "large"
        self.disk["small"] = False
        self.machine(1.2)
        with mock.patch("services.captions.models.download_size", return_value=600_000_000):
            self.captions.set_translate_to("English")
            self.assertEqual(self.card.asked, [])
            self.assertEqual((self.stopped, self.loaded), ([], []))  # the 1.7B reads on
            self.assertTrue(self.captions.enabled())
            self.assertEqual(self.sent[-1], ("translate", "English", self.folder()))
            self.assertEqual(self.window.live_session.floor, 10.0 + TRANSLATE_LEAD_S)
            self.captions._step_down()  # it can't keep up
        [(text, _yes, _no)] = self.card.asked
        self.assertEqual(text, translate(STEP_DOWN_QUESTION, size="600 MB"))


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

    def test_a_caption_too_long_for_two_lines_comes_in_parts_never_cut(self):
        # 2.9.1, the owner: a long one ended in "…".
        text = (
            "Ankara'da bugün yapılan toplantıda bakan, yeni düzenlemenin önümüzdeki aydan "
            "itibaren yürürlüğe gireceğini söyledi. Muhalefet ise tasarının meclise gelmeden "
            "önce kamuoyuyla paylaşılması gerektiğini, aksi hâlde itiraz edeceklerini belirtti."
        )
        parts = self.overlay.pages(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual(" ".join(parts), text)  # nothing lost
        for part in parts:
            self.overlay.show_text(part)
            self.assertLessEqual(len(self.overlay.lines()), 2)
            self.assertFalse(self.overlay.lines()[-1].endswith("…"), part)
        # 2026-11-08, the owner: "after the dot we can split and show the other part later": a
        # part ends at a sentence's end, or (a sentence too long alone) at a clause's
        self.assertTrue(any(part.endswith("söyledi.") for part in parts))
        self.assertTrue(all(part[-1] in ".," for part in parts), parts)
        self.assertEqual(self.overlay.pages("Good evening"), ["Good evening"])

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

    def test_the_model_is_kept_at_save_and_a_stored_language_is_left_alone(self):
        self.models(0)
        self.window.config_manager.ai_captions_language = "Turkish"  # a 2.11.1 config: not read
        dialog = self.dialog()
        self.assertEqual(dialog.ai_captions_model_combo.currentData(), "auto")
        self.assertIsNone(dialog.findChild(QComboBox, "aiCaptionsLanguage"))
        group = dialog.ai_captions_model_combo.parentWidget()
        self.assertEqual(group.objectName(), "aiCaptionsSettings")
        dialog.ai_captions_model_combo.setCurrentIndex(
            dialog.ai_captions_model_combo.findData("large")
        )
        self.assertTrue(dialog.has_unsaved_changes())
        dialog.save_settings()
        saved = ConfigManager()
        self.assertEqual(saved.ai_captions_model, "large")
        self.assertEqual(saved.config["ai_captions_language"], "Turkish")
        dialog = self.dialog()
        self.assertEqual(dialog.ai_captions_model_combo.currentData(), "large")

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
