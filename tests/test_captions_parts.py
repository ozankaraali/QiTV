"""A caption line in parts (2026-11-08, the owner: "after the dot we can split and show the other
part later"): services.captions.parts splits a line after its sentences and at its longest
pauses, the captions process sends a line's parts with it, and the captions show a long line a
part at a time, each as it is said."""

import logging
import os
import tempfile
import threading
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.captions.parts import MIN_PART_S, RATE, WINDOW, Part, parts_of


def windows(*runs):
    """Voiced flags from (seconds, voiced) runs, a window each 32 ms."""
    step = WINDOW / RATE
    return [voiced for seconds, voiced in runs for _ in range(round(seconds / step))]


class PartsTests(unittest.TestCase):
    def test_a_line_splits_after_each_sentence_at_the_pause_nearest(self):
        voiced = windows((2.0, True), (0.3, False), (1.5, True))
        parts = parts_of("Nereye gidiyorsun böyle? Eve dönüyorum.", 10.0, 13.8, voiced)
        self.assertEqual([p.text for p in parts], ["Nereye gidiyorsun böyle?", "Eve dönüyorum."])
        self.assertAlmostEqual(parts[0].end, 12.15, delta=0.05)  # in the pause, not by letters
        self.assertEqual((parts[0].start, parts[1].start, parts[1].end), (10.0, parts[0].end, 13.8))

    def test_a_long_pause_splits_a_line_with_no_sentence_end(self):
        voiced = windows((1.5, True), (0.6, False), (1.5, True))
        parts = parts_of("bu akşam geliyor musun yarın sabah değil", 0.0, 3.6, voiced)
        self.assertEqual(len(parts), 2)
        self.assertAlmostEqual(parts[0].end, 1.8, delta=0.05)

    def test_only_a_line_s_longest_pauses_split_it(self):
        voiced = windows(
            (1.0, True),
            (0.5, False),
            (1.0, True),
            (0.7, False),
            (1.0, True),
            (0.45, False),
            (1.0, True),
        )
        words = "bir iki üç dört beş altı yedi sekiz"
        self.assertEqual(len(parts_of(words, 0.0, 5.65, voiced)), 4)
        two = parts_of(words, 0.0, 5.65, voiced, most=2)  # the 0.7 s and 0.5 s pauses
        self.assertEqual([p.text for p in two], ["bir iki", "üç dört", "beş altı yedi sekiz"])
        self.assertAlmostEqual(two[1].end, 2.85, delta=0.05)

    def test_a_short_part_goes_with_a_neighbour(self):
        voiced = windows((0.4, True), (0.3, False), (2.0, True))
        parts = parts_of("Evet. Yarın gelirim, merak etme.", 0.0, 2.7, voiced)
        self.assertEqual([p.text for p in parts], ["Evet. Yarın gelirim, merak etme."])
        self.assertGreater(MIN_PART_S, 0.4)

    def test_without_the_voice_detector_by_letters_and_one_word_as_it_is(self):
        parts = parts_of("Bir iki üç. Dört beş altı.", 0.0, 4.0)
        self.assertEqual([p.text for p in parts], ["Bir iki üç.", "Dört beş altı."])
        self.assertAlmostEqual(parts[0].end, 4.0 * 12 / 27, delta=0.01)  # its share of letters
        self.assertEqual(parts_of("Evet.", 1.0, 2.0), [Part("Evet.", 1.0, 2.0)])

    def test_the_windows_may_begin_before_the_line(self):
        voiced = windows((1.0, False), (2.0, True), (0.3, False), (1.5, True))
        parts = parts_of("Nereye gidiyorsun böyle? Eve dönüyorum.", 11.0, 14.8, voiced, at=10.0)
        self.assertAlmostEqual(parts[0].end, 13.15, delta=0.05)

    def test_it_needs_no_qt(self):  # the captions process imports it
        import subprocess
        import sys

        code = (
            "import sys; import services.captions.parts; "
            "sys.exit(any(m.startswith('PySide6') for m in sys.modules))"
        )
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(subprocess.run([sys.executable, "-c", code], cwd=root).returncode, 0)


class PagedTests(unittest.TestCase):
    """A caption too long for two lines in parts at sentences' ends."""

    def test_whole_sentences_together_while_they_fit(self):
        from ui.overlays.captions import paged

        text = "Bugün hava çok güzel. Parka gidelim mi? Olur, gidelim. Ama önce yemek yiyelim."
        pages = paged(text, lambda part: len(part) <= 45)
        self.assertEqual(
            pages,
            ["Bugün hava çok güzel. Parka gidelim mi?", "Olur, gidelim. Ama önce yemek yiyelim."],
        )

    def test_a_sentence_too_long_alone_at_its_clauses_then_between_words(self):
        from ui.overlays.captions import paged

        text = (
            "Toplantıda bakan, yeni düzenlemenin önümüzdeki aydan itibaren yürürlüğe "
            "gireceğini söyledi. Kısa."
        )
        pages = paged(text, lambda part: len(part) <= 45)
        self.assertEqual(pages[0], "Toplantıda bakan,")
        self.assertEqual(" ".join(pages), text)  # no word cut, none lost
        self.assertTrue(all(len(page) <= 45 for page in pages))
        self.assertEqual(paged("Kısa.", lambda part: True), ["Kısa."])


def _bare_run():
    """A captions run without its stream, its sound or its recognizing threads."""
    from services.captions.worker import _Run

    run: Any = object.__new__(_Run)
    run.run, run.model = 3, SimpleNamespace()
    run.sent = []
    run.link = SimpleNamespace(send=run.sent.append)
    run._writes = "Qwen3-ASR"
    run.stopped, run.viewer = threading.Event(), [0.0]
    return run


class ProcessTests(unittest.TestCase):
    """The captions process sends each line, in parts when it has more than one, and logs it."""

    def test_a_line_goes_out_at_once_and_the_log_says_what_it_heard_at_debug(self):
        run = _bare_run()
        with self.assertLogs("services.captions.worker", logging.DEBUG) as logs:
            run._write("Qwen3-ASR", "eve", "Eve.", 10.0, 11.0, until=11.5)
        self.assertEqual(run.sent, [("caption", 3, 10.0, 11.5, "Eve.")])
        self.assertEqual(
            [(r.levelno, r.getMessage()) for r in logs.records],
            [(logging.DEBUG, "AI captions: [Qwen3-ASR] heard 'eve', shows 'Eve.'")],
        )

    def test_a_piece_s_own_voice_detector_times_its_parts(self):
        run = _bare_run()
        voiced = windows((2.0, True), (0.4, False), (1.5, True))
        text = "Bir iki üç dört. Beş altı yedi."
        run._write("Qwen3-ASR", "x", text, 20.0, 23.9, voiced=voiced)
        [message] = run.sent
        self.assertEqual(message[:5], ("caption", 3, 20.0, 23.9, text))
        self.assertEqual([part[0] for part in message[5]], ["Bir iki üç dört.", "Beş altı yedi."])
        self.assertAlmostEqual(message[5][0][2], 22.2, delta=0.05)

    def test_the_gui_hears_each_line_and_its_parts(self):
        from PySide6.QtWidgets import QApplication

        from services.captions.process import CaptionsProcess

        QApplication.instance() or QApplication([])
        process = CaptionsProcess()
        heard = []
        process.caption.connect(lambda *args: heard.append(args))
        process._supervisor, process._serial = object(), 1  # type: ignore[assignment]
        process._on_message(1, ("caption", 2, 11.0, 12.0, "Evet."))
        parts = [("Eve.", 10.0, 10.5), ("Evet.", 10.5, 11.0)]
        process._on_message(1, ("caption", 2, 10.0, 11.0, "Eve. Evet.", parts))
        process._supervisor = None
        self.assertEqual(
            heard, [(2, 11.0, 12.0, "Evet.", []), (2, 10.0, 11.0, "Eve. Evet.", parts)]
        )


class ControllerTests(unittest.TestCase):
    """A long caption shows part by part, each as it is said."""

    def setUp(self):
        from PySide6.QtWidgets import QApplication, QWidget
        from test_captions_pipeline import _Live, _Overlay

        from ui.captions_controller import CaptionsController

        self.app = QApplication.instance() or QApplication([])
        self.window = QWidget()
        self.window.live_session = _Live()
        self.overlay = _Overlay()
        self.captions = CaptionsController(self.window, overlay=self.overlay)
        captions: Any = self.captions
        self.sent: list[tuple] = []
        captions._process.send = lambda *m: self.sent.append(m)
        captions._process.running = lambda: True
        self.config: dict[str, Any] = {}
        captions._config_get = lambda name, default: self.config.get(name, default)
        captions._config_set = self.config.__setitem__
        captions._here = lambda name: True
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = mock.patch.dict(os.environ, {"QITV_CAPTIONS_DIR": self.tmp.name})
        folder.start()
        self.addCleanup(folder.stop)
        self.captions._enabled = self.captions._ready = True
        self.captions._start_run()
        self.number = self.captions._run

    def at(self, position):
        self.window.live_session.position_s = position
        self.captions._last = None
        self.captions._tick()
        return self.overlay.text

    def test_a_line_s_parts_are_one_caption_timed_part_by_part(self):
        parts = [("Nereye gidiyorsun?", 10.0, 11.5), ("Eve.", 11.5, 12.5)]
        self.captions._on_caption(self.number, 10.0, 12.5, "Nereye gidiyorsun? Eve.", parts)
        self.assertEqual(self.captions._cues, [(10.0, 12.5, "Nereye gidiyorsun? Eve.")])
        self.assertEqual(self.captions._timing[10.0], [(2, 10.0, 11.5), (1, 11.5, 12.5)])

    def test_a_long_caption_shows_part_by_part_as_each_is_said(self):
        self.overlay.pages = lambda text: [  # (as the overlay pages it: at the sentences)
            page for page in ("Birinci cümle burada bitiyor.", "İkincisi de sonra geliyor.")
        ]
        parts = [
            ("Birinci cümle burada bitiyor.", 10.0, 13.0),
            ("İkincisi de sonra geliyor.", 13.0, 15.0),
        ]
        text = "Birinci cümle burada bitiyor. İkincisi de sonra geliyor."
        self.captions._on_caption(self.number, 10.0, 15.0, text, parts)
        self.assertEqual(self.at(10.4), "Birinci cümle burada bitiyor.")
        self.assertEqual(self.at(12.9), "Birinci cümle burada bitiyor.")  # its words go on
        self.assertEqual(self.at(13.1), "İkincisi de sonra geliyor.")  # when it is said

    def test_a_part_said_soon_after_waits_to_be_read_but_no_flash_and_never_long(self):
        self.overlay.pages = lambda text: ["Evet.", "Hemen geliyorum, bekle beni orada lütfen."]
        parts = [
            ("Evet.", 10.0, 10.2),
            ("Hemen geliyorum, bekle beni orada lütfen.", 10.2, 13.0),
        ]
        self.captions._on_caption(
            self.number, 10.0, 13.0, "Evet. Hemen geliyorum, bekle beni orada lütfen.", parts
        )
        self.assertEqual(self.at(11.2), "Evet.")  # on PAGE_S at least (from 10.3)
        self.assertEqual(self.at(11.35), "Hemen geliyorum, bekle beni orada lütfen.")

    def test_a_long_caption_with_no_times_shows_by_its_letters(self):
        self.overlay.pages = lambda text: text.split(" | ")
        self.captions._on_caption(self.number, 10.0, 16.0, "one two three | four five six")
        self.captions._timing.clear()  # (no times: each part for its share of the letters)
        self.assertEqual(self.at(10.3), "one two three")
        self.assertEqual(self.at(15.0), "four five six")


class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_a_caption_too_long_comes_in_parts_at_its_sentences_ends(self):
        from PySide6.QtWidgets import QWidget

        from ui.overlays.captions import CaptionsOverlay

        host = QWidget()
        host.resize(1280, 720)
        self.addCleanup(host.deleteLater)
        overlay = CaptionsOverlay(host)
        first = "Bu akşam yemekten sonra hep birlikte sahile inip biraz yürüyelim mi?"
        second = "Olur ama önce annemi arayıp eve geç kalacağımızı söylemem gerekiyor."
        self.assertEqual(overlay.pages(f"{first} {second}"), [first, second])


if __name__ == "__main__":
    unittest.main()
