"""AI captions on screen (2.9): the languages' names, the Subtitles panel's rows and language
list (with a fake CaptionsController), the Sound panel's language labels, the captions overlay
and Settings → Subtitles → AI captions."""

import os
import subprocess
import sys
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
        self.toggles = 0
        self.picked = []

    def available(self):
        return self.offered

    def enabled(self):
        return self.on

    def status_text(self):
        return self.status

    def toggle(self):
        self.toggles += 1
        if self.asks and not self.on:
            return
        self.on = not self.on

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
            self.texts(), ["*Off", "English", "Français", "AI captions", "Load a file…"]
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
