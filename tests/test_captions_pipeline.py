"""AI captions' pipeline (2.9): what a speech model wrote that nobody said, the stream's clock,
the pieces speech is cut into, QiTV's libVLC decoding a stream's sound, and when the window
shows each caption."""

from array import array
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock
import unittest.mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.captions import guards, text_language
from services.captions.pieces import RATE, WINDOW, Cutter
from services.captions.stream_clock import StreamClock, TsTimes, pts_seconds

FIXTURE = Path(__file__).parent / "fixtures" / "live.ts"


def setUpModule():
    """AI captions' files in a folder of this module's own: the controller's speed check
    (tier.machine_score) never writes the developer's own captions folder."""
    if not os.environ.get("QITV_CAPTIONS_DIR"):
        folder = tempfile.mkdtemp(prefix="qitv-captions-")
        patch = mock.patch.dict(os.environ, {"QITV_CAPTIONS_DIR": folder})
        patch.start()
        unittest.addModuleCleanup(patch.stop)
        unittest.addModuleCleanup(shutil.rmtree, folder, True)


class GuardTests(unittest.TestCase):
    def test_a_subscribe_outro_alone_is_the_models_habit(self):
        # 2026-11-08, the Turkish Moonshine trial on a drama: "Abone ol." over a short blip.
        for said in (
            "Abone ol.",
            "Abone olun!",
            "Beğen ve abone ol.",
            "Kanalıma abone olmayı unutmayın.",
        ):
            self.assertEqual(guards.clean(said), "", said)
        # Inside a longer line, or another sentence: as said.
        self.assertEqual(
            guards.clean("Bu programa abone olun, her hafta buradayız."),
            "Bu programa abone olun, her hafta buradayız.",
        )
        self.assertEqual(guards.clean("Ve lütfen."), "Ve lütfen.")  # no outro word: a line

    def test_what_the_broadcasts_showed_never_reaches_the_screen(self):
        self.assertEqual(guards.clean("system."), "")
        self.assertEqual(guards.clean("."), "")
        self.assertEqual(guards.clean("Altyazı M.K."), "")
        self.assertEqual(guards.clean("İzlediğiniz için teşekkür ederim."), "")
        self.assertEqual(guards.clean("Thanks for watching!"), "")
        self.assertEqual(guards.clean("Sen öyle. " * 40), "")  # a loop over music: none of it said
        self.assertEqual(guards.clean("Sen öyle. " * 3), "Sen öyle. Sen öyle. Sen öyle.")  # as said
        self.assertEqual(
            guards.clean("Kolay gelsin. Kolay gelsin. Hoş geldin."),
            "Kolay gelsin. Kolay gelsin. Hoş geldin.",
        )  # said twice: kept

    def test_a_loop_over_sung_music_is_not_a_caption(self):
        # 2026-11-07, the Turkish Moonshine trial over songs: number loops shown as digits
        self.assertEqual(guards.clean("Bu, " + "iki bin on yedide " * 6), "")
        self.assertEqual(guards.clean("Bu, " + "bir de bu, " * 10), "")
        self.assertEqual(guards.clean("Bu, " + "iki, " * 20), "")
        # the owner's live log: three times, the last with "!" (shown as "2017'de" three times)
        self.assertEqual(
            guards.clean("Bu, iki bin on yedide, iki bin on yedide, iki bin on yedide!"), ""
        )
        # two words said three times is a shout: it stays as said
        self.assertEqual(
            guards.clean("Ne yaptın, ne yaptın, ne yaptın!"), "Ne yaptın, ne yaptın, ne yaptın!"
        )
        # people repeat for real (the owner, 2026-11-07): shown as said
        self.assertEqual(guards.clean("Gol! Gol! Gol! Gol! Gol!"), "Gol! Gol! Gol! Gol! Gol!")
        self.assertEqual(guards.clean("Hayır hayır hayır, gitme."), "Hayır hayır hayır, gitme.")
        self.assertEqual(guards.clean("  Five others were  injured. "), "Five others were injured.")

    def test_the_language_the_model_heard_is_not_part_of_the_caption(self):
        # 2.9: the 1.7B with no language given, on a Turkish recording.
        self.assertEqual(
            guards.clean("language Turkish<asr_text>Gedällle halkoyunları ekibinin sahnesi."),
            "Gedällle halkoyunları ekibinin sahnesi.",
        )
        self.assertEqual(guards.clean("language None<asr_text>"), "")  # nothing heard
        self.assertEqual(guards.clean("<asr_text>Hello."), "Hello.")
        self.assertEqual(
            guards.clean("The language English is spoken."), "The language English is spoken."
        )

    def test_a_piece_may_only_say_so_much(self):
        self.assertEqual(guards.token_cap(0), guards.TOKENS_BASE)
        self.assertGreater(guards.token_cap(6.0), guards.token_cap(2.0))

    def test_a_script_of_many_tokens_a_word_has_room_for_its_words(self):
        # 2.9.3: the cap cut Qwen's Hindi short (57 % of words wrong, 34 % without the cap).
        english = guards.token_cap(6.0, "English")
        self.assertGreaterEqual(guards.token_cap(6.0, "Hindi"), 2.5 * english)
        self.assertGreater(guards.token_cap(6.0, None), english)  # not known yet: more room
        self.assertEqual(guards.token_cap(6.0, "Russian"), english)


class TextLanguageTests(unittest.TestCase):
    """The language a caption is written in, from what the model wrote on the broadcasts."""

    def test_a_caption_in_another_languages_letters_is_not_the_channels(self):
        # 2.9: locked to Turkish, the model wrote a line of Korean speech in Urdu's letters.
        from services.captions.text_language import written_in

        self.assertFalse(written_in("بیکویه جمہور باشکنی لیجے میونگ جمہور باشکنی.", "Turkish"))
        self.assertTrue(written_in("Merduan ve Li C Miong Askeri Kıta'yı Selamladı.", "Turkish"))
        self.assertTrue(written_in("Erdoğan ile görüştü: 대한민국.", "Turkish"))  # a little
        self.assertTrue(written_in("Привет, как дела?", "Russian"))
        self.assertFalse(written_in("Hello, how are you?", "Russian"))
        self.assertTrue(written_in("今日は東京で会議があります。", "Japanese"))
        self.assertTrue(written_in("OK.", "Chinese"))  # too little to tell

    def test_the_languages_the_model_wrote(self):
        from services.captions.text_language import guess

        cases = {
            "Tamam tamam problem yok ya kostümleri ben hallederim zaten merak etmeyin.": "Turkish",
            "Feda edenlerin canı şahidi. Bizler de söz veriyoruz.": "Turkish",
            "Rising food and fuel prices have been major issues for voters.": "English",
            "Die Regierung hat das nicht gesagt, und es ist auch nicht so.": "German",
            "Le gouvernement ne veut pas que les prix montent dans la ville.": "French",
            "El gobierno dijo que los precios no van a subir en la ciudad.": "Spanish",
            "好了。我们走吧。": "Chinese",
            "جد بكر، أنت بكر.": "Arabic",
            "И я огням лар, салю гню уленин": "Russian",
            "맛, 맛처럼 맛이.": "Korean",
        }
        for text, language in cases.items():
            with self.subTest(text=text):
                self.assertEqual(guess(text), language)

    def test_too_little_to_tell_says_so(self):
        from services.captions.text_language import guess

        self.assertIsNone(guess("Okay."))
        self.assertIsNone(guess("å ja."))

    def test_the_pieces_lock_the_language_once_they_agree(self):
        from services.captions.text_language import Votes

        votes = Votes()
        self.assertIsNone(votes.add("Turkish"))
        self.assertIsNone(votes.leading())  # one says nothing yet
        self.assertIsNone(votes.add("Chinese"))  # a short piece over music
        self.assertIsNone(votes.add("Turkish"))
        self.assertEqual(votes.leading(), "Turkish")
        self.assertEqual(votes.add("Turkish"), "Turkish")  # three of the last four
        self.assertEqual(votes.locked, "Turkish")
        self.assertIsNone(votes.add("Arabic"))  # locked: no more votes


def _packet(pid, payload, start=False):
    header = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF, 0x10])
    body = payload[:184]
    return header + body + b"\xff" * (184 - len(body))


def _section(table_id, body):
    length = len(body) + 5 + 4  # the header after the length, the body, a CRC (unchecked)
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF, 0, 1, 0xC1, 0, 0])
    return b"\x00" + head + body + b"\0\0\0\0"


def _pts_bytes(pts):
    return bytes(
        [
            0x21 | ((pts >> 29) & 0x0E),
            (pts >> 22) & 0xFF,
            0x01 | ((pts >> 14) & 0xFE),
            (pts >> 7) & 0xFF,
            0x01 | ((pts << 1) & 0xFE),
        ]
    )


def _pes(stream_id, pts):
    return b"\x00\x00\x01" + bytes([stream_id, 0, 0, 0x80, 0x80, 5]) + _pts_bytes(pts) + b"\0" * 8


def _stream(video_pts, audio_pts, language=b"tur"):
    pmt_pid = 0x100
    pat = _section(0x00, bytes([0, 1, 0xE0 | (pmt_pid >> 8), pmt_pid & 0xFF]))
    lang = bytes([0x0A, 4]) + language + b"\x00"
    streams = (
        bytes([0x1B, 0xE1, 0x01, 0xF0, 0x00]) + bytes([0x0F, 0xE1, 0x02, 0xF0, len(lang)]) + lang
    )
    pmt = _section(0x02, bytes([0xE1, 0x01, 0xF0, 0x00]) + streams)
    out = _packet(0, pat, True) + _packet(pmt_pid, pmt, True)
    for v, a in zip(video_pts, audio_pts):
        out += _packet(0x101, _pes(0xE0, v), True) + _packet(0x102, _pes(0xC0, a), True)
    return out


class TurkishModelTests(unittest.TestCase):
    """Turkish is written by our Turkish Moonshine (2.10; 2.9.1: ufakzeka-kulak, far better than
    Qwen's Turkish), the rest by Qwen and the specialists."""

    def model(self):
        from services.captions.worker import _Model

        heard = []

        class Recognizer:
            def __init__(self, name, text):
                self.name, self.text = name, text

            def create_stream(self):
                recognizer = self

                class Stream:
                    result = SimpleNamespace(text=recognizer.text)

                    def set_option(self, key, value):
                        heard.append((recognizer.name, key, value))

                    def accept_waveform(self, rate, samples):
                        heard.append((recognizer.name, "peak", max(samples)))

                return Stream()

            def decode_stream(self, stream):
                pass

        class Beam:
            def transcribe(self, samples, width=None):
                heard.append(("moonshine", "peak", max(samples)))
                return " iyi akşamlar "

        model = object.__new__(_Model)
        model.recognizer = Recognizer("qwen", "Good evening.")
        model.beam = model.turkish = Beam()
        model.european = model.cohere = None
        model._folders, model._loading = {}, {}
        return model, heard

    @staticmethod
    def given(model, name, recognizer):
        """`recognizer` as the loaded specialist `name` ("cohere", "european")."""
        model._folders[name] = Path("unused")
        setattr(model, name, recognizer)

    def test_coheres_languages_go_to_it_told_the_language(self):
        # 2.9.3: Cohere Transcribe wrote its 14 languages best (FLEURS and broadcasts).
        model, heard = self.model()
        self.given(model, "cohere", type(model.recognizer)("cohere", "Bonsoir."))
        self.given(model, "european", type(model.recognizer)("parakeet", "God aften."))
        self.assertEqual(model.decode(array("f", [0.1]), "French"), "Bonsoir.")
        self.assertEqual(heard[0], ("cohere", "language", "fr"))
        heard.clear()
        self.assertEqual(model.decode(array("f", [0.1]), "Danish"), "God aften.")  # not Cohere's
        self.assertEqual(heard[0][0], "parakeet")
        heard.clear()
        model.decode(array("f", [0.1]), "Hindi")  # neither's: Qwen's
        self.assertEqual(heard[0], ("qwen", "language", "Hindi"))

    def test_a_specialist_loads_once_needed_and_qwen_writes_meanwhile(self):
        model, heard = self.model()
        model._folders = {"cohere": Path("unused"), "european": Path("unused")}
        loads = []
        go = threading.Event()

        def load(name, done):
            loads.append(name)
            go.wait(5)
            setattr(model, name, type(model.recognizer)(name, "loaded"))
            done.set()

        model._load = load
        self.assertEqual(model.decode(array("f", [0.1]), "French"), "Good evening.")  # Qwen's
        self.assertEqual(loads, ["cohere"])
        model.decode(array("f", [0.1]), "French")
        self.assertEqual(loads, ["cohere"])  # once
        go.set()
        model._loading["cohere"].wait(5)
        self.assertEqual(model.decode(array("f", [0.1]), "French"), "loaded")
        # A language only Parakeet writes waits for it (Qwen can't write it at all).
        self.assertEqual(model.decode(array("f", [0.1]), "Ukrainian"), "loaded")
        self.assertEqual(loads, ["cohere", "european"])

    def test_parakeets_languages_go_to_it_and_qwen_is_never_told_one_it_doesnt_know(self):
        # 2.9.3: Parakeet TDT 0.6B v3 writes its European languages, better than Qwen.
        model, heard = self.model()
        self.given(model, "european", type(model.recognizer)("parakeet", "Good evening."))
        self.assertEqual(model.decode(array("f", [0.1]), "English"), "Good evening.")
        self.assertEqual([name for name, *_ in heard], ["parakeet"])  # no gain, no language
        heard.clear()
        model.decode(array("f", [0.1]), "Ukrainian")
        self.assertEqual(heard[0][0], "parakeet")
        heard.clear()
        model.decode(array("f", [0.1]), "Japanese")
        self.assertEqual(heard[0], ("qwen", "language", "Japanese"))
        heard.clear()
        model.european = None  # its files not given: Qwen writes, with no language set
        model._folders.clear()
        model.decode(array("f", [0.1]), "Ukrainian")
        self.assertNotIn("language", [key for _name, key, *_ in heard])

    def test_a_piece_parakeet_wrote_in_english_is_qwens_told_the_language(self):
        # 2.9.3: on a French broadcast with English words in it, Parakeet (never told the
        # language) wrote English: "And I've been at the bank."
        model, heard = self.model()
        self.given(
            model,
            "european",
            type(model.recognizer)("parakeet", "And I've been at the bank, you know."),
        )
        model.recognizer = type(model.recognizer)("qwen", "Et je vais à la banque.")
        self.assertEqual(model.decode(array("f", [0.1]), "French"), "Et je vais à la banque.")
        self.assertIn(("qwen", "language", "French"), heard)
        heard.clear()
        self.assertEqual(  # English, as it should be: Parakeet's
            model.decode(array("f", [0.1]), "English"), "And I've been at the bank, you know."
        )
        self.assertEqual({name for name, *_ in heard}, {"parakeet"})
        heard.clear()
        model.european = type(model.recognizer)(
            "parakeet", "Love money, forcément, dans un bureau."
        )
        self.assertEqual(model.decode(array("f", [0.1]), "French")[:10], "Love money")  # French

    def test_turkish_goes_to_our_moonshine_as_it_sounds_and_the_rest_to_qwen(self):
        model, heard = self.model()
        self.assertEqual(model.decode(array("f", [0.1, 0.05]), "Turkish"), "iyi akşamlar")
        self.assertEqual(heard, [("moonshine", "peak", array("f", [0.1])[0])])  # no gain
        heard.clear()
        self.assertEqual(model.decode(array("f", [0.1]), "English"), "Good evening.")
        self.assertEqual(heard[0], ("qwen", "language", "English"))
        cap = guards.token_cap(1 / 16000, "English")  # 2.9.3: each piece's own cap
        self.assertEqual(heard[1], ("qwen", "max_new_tokens", str(cap)))
        model.turkish = model.beam = None  # its files not given: Qwen writes Turkish too
        self.assertEqual(model.decode(array("f", [0.1]), "Turkish"), "Good evening.")

    def test_a_capital_only_where_a_sentence_starts(self):
        # 2.9.2, the owner: each line began with a capital mid-sentence, with no full stops.
        from services.captions.worker import _Run

        run = object.__new__(_Run)
        run._said = None
        lines = [
            ("ayde çok güzel bir fikir ve", 10.0, 12.0),  # the first: a sentence starts
            ("yarın da gideriz", 12.2, 13.5),  # straight on: the same sentence
            ("ne konuşuyorsun", 15.0, 16.0),  # after a 1.5 s pause: a new one
            ("tamam. iyi geceler", 16.3, 17.5),  # straight on, as written
            ("işte böyle", 17.7, 18.5),
        ]
        cased = [run.sentence_case(text, start, end) for text, start, end in lines]
        self.assertEqual(
            cased,
            [
                "Ayde çok güzel bir fikir ve",
                "yarın da gideriz",
                "Ne konuşuyorsun",
                "tamam. iyi geceler",
                "işte böyle",
            ],
        )
        run._said = ("Gel bakalım.", 20.0)
        self.assertEqual(run.sentence_case("içeri gir", 20.1, 21.0), "İçeri gir")  # after a stop

    def test_a_turkish_capital_is_turkish(self):
        from services.captions.worker import turkish_capital

        self.assertEqual(turkish_capital("ırmak taştı"), "Irmak taştı")
        self.assertEqual(turkish_capital("“evet” dedi"), "“Evet” dedi")
        self.assertEqual(turkish_capital("Ankara"), "Ankara")
        self.assertEqual(turkish_capital("..."), "...")


class WrittenToTests(unittest.TestCase):
    """2.9, the review: "Can't keep up" and Automatic's step down measured how far the sound
    was read, which a model too slow for it never shortened."""

    def run_with(self, *pieces, working=None):
        import queue

        from services.captions.worker import _Run

        run = object.__new__(_Run)
        run.pieces = queue.Queue()
        for piece in pieces:
            run.pieces.put(piece)
        run.newest, run._working = 140.0, working
        return run

    def piece(self, start, end):
        from services.captions.pieces import Piece

        return Piece(start, end, array("f"), 1.0)

    def test_the_captions_are_written_to_what_the_model_has_not_heard_yet(self):
        self.assertEqual(self.run_with().written_to(), 140.0)  # all read is heard
        late = self.run_with(self.piece(101.0, 104.0), self.piece(104.5, 107.0))
        self.assertEqual(late.written_to(), 101.0)  # the oldest waiting
        self.assertEqual(self.run_with(working=99.0).written_to(), 99.0)

    def test_a_piece_the_picture_passed_is_not_recognized(self):
        import threading

        run = self.run_with(self.piece(90.0, 95.0), self.piece(101.0, 104.0))
        decoded, sent = [], []
        run.model = SimpleNamespace(
            recognizer=object(),
            decode=lambda samples, language, fast=False: decoded.append(1) or "Hello.",
        )
        run.link = SimpleNamespace(send=sent.append)
        run.viewer, run.language, run.run, run.rtf = [100.0], "English", 1, 0.5
        run.stopped = threading.Event()
        thread = threading.Thread(target=run._recognize, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not [m for m in sent if m[0] == "caption"] and time.monotonic() < deadline:
            time.sleep(0.01)
        run.stopped.set()
        run.pieces.put(None)
        thread.join(5)
        self.assertEqual(len(decoded), 1)  # 90-95 s: the picture is at 100 s
        self.assertEqual([m[2] for m in sent if m[0] == "caption"], [101.0])  # its start

    def test_each_line_is_logged_by_the_model_that_wrote_it(self):
        # 2026-11-08, the owner: "Write to logs which one writes"
        import logging
        import threading

        run = self.run_with(self.piece(101.0, 104.0))
        sent = []
        run.model = SimpleNamespace(
            recognizer=object(),
            decode=lambda samples, language, fast=False: "Hello there.",
            wrote=lambda: "qwen3-asr-0.6b",
        )
        run.link = SimpleNamespace(send=sent.append)
        run.viewer, run.language, run.run, run.rtf = [100.0], "English", 4, 0.5
        run._writes = "qwen3-asr-0.6b"  # as the run began: no change to say
        run.stopped = threading.Event()
        with self.assertLogs("services.captions.worker", logging.DEBUG) as logs:
            thread = threading.Thread(target=run._recognize, daemon=True)
            thread.start()
            deadline = time.monotonic() + 5
            while not [m for m in sent if m[0] == "caption"] and time.monotonic() < deadline:
                time.sleep(0.01)
            run.stopped.set()
            run.pieces.put(None)
            thread.join(5)
        self.assertEqual(
            [r.getMessage() for r in logs.records],
            ["AI captions: [qwen3-asr-0.6b] heard 'Hello there.', shows 'Hello there.'"],
        )


MT_FOLDER = Path("qitv-mt-milmmt-46-1b")
TWO_PARTS = "Bir iki üç dört. Beş altı yedi."  # a line in two parts (services.captions.parts)
# ... the voice detector's windows over its 3.9 s: a pause between its sentences
TWO_PARTS_VOICED = [
    voiced
    for seconds, voiced in ((2.0, True), (0.4, False), (1.5, True))
    for _ in range(round(seconds * RATE / WINDOW))
]


class TranslationTests(unittest.TestCase):
    """2.10: the captions process translates each line into the viewer's language before it
    shows, in order, and sends the original when the translation would come too late; speech's
    own pace never waits for it, nor any line for the model's load. A fake translator: no
    model."""

    def setUp(self):
        from services.captions import tier, translate, worker

        self.run_class, self.film_class = worker._Run, worker._FilmRun
        self.sent: list = []
        self.link: Any = SimpleNamespace(send=self.sent.append)
        self.viewer = [100.0, 1.0]
        self.made: list = []  # (folder, threads) of each translator made
        self.calls: list = []  # (text, source, target, seconds it had) of each translation
        self.answer: Any = lambda text, source, target, deadline: f"<{target}> {text}"
        self.warm: Any = lambda: True  # its load: True when it loaded
        self.speed: Any = None  # 2.13.1: what its timed warm-up gives (None: nothing)
        self.contexts: list = []  # 2.13.1: the line before and its translation, each call's
        self.second_try = False  # 2.13.1: its last call translated the line alone after a leak
        test = self

        class Translator:  # services.captions.translate.Translator's interface
            def __init__(self, folder, threads=2, library=None):
                test.made.append((Path(folder), threads))

            error = "OSError: no tokenizer.json"  # why it is of no use, once warm() is False

            def warm(self):
                return test.warm()

            def speed(self):
                return test.speed

            @property
            def second_try(self):
                return test.second_try

            def translate(self, text, source, target, deadline=None, context=None):
                test.calls.append((text, source, target, deadline - time.monotonic()))
                test.contexts.append(context)
                return test.answer(text, source, target, deadline)

        patch = mock.patch.object(translate, "Translator", Translator)
        patch.start()
        self.addCleanup(patch.stop)
        # 2.13.1: the translator's threads are this computer's (tier.translator_threads): 2 here
        threads = mock.patch.object(tier, "translator_threads", return_value=2)
        threads.start()
        self.addCleanup(threads.stop)

    def until(self, done):
        """`done()`, waited for (5 s at most)."""
        deadline = time.monotonic() + 5
        while not done() and time.monotonic() < deadline:
            time.sleep(0.01)
        return done()

    def translation(self, target="English", folder=MT_FOLDER, loaded=True):
        from services.captions.worker import _Translation

        translation = _Translation(self.link, self.viewer)
        translation.set(target, folder)
        if loaded:
            self.assertTrue(self.until(translation._ready))
        return translation

    def a_run(self, translation, language="Turkish", number=1, newest=110.0, follow=True):
        """A captions run without its stream, its sound or its threads; the current one (as the
        command loop makes it), begun with the picture waiting."""
        run: Any = object.__new__(self.run_class)
        run.run, run.language, run.newest, run.translation = number, language, newest, translation
        run.link, run.viewer, run.stopped = self.link, self.viewer, threading.Event()
        run._writes = "qwen"
        if translation is not None and follow:
            translation.follow(run)
        return run

    def plays(self, translation, position):
        """The picture seen moving, at `position` (as the command loop stamps it)."""
        self.viewer[0] = position
        translation.moved = time.monotonic()

    def written(self, *line, number=1, **times):
        """A line as the process sends it with translation off (as before 2.10)."""
        sent: list = []
        run = self.a_run(None, "Turkish", number)
        run.link = SimpleNamespace(send=sent.append)
        run._write("qwen", "heard", *line, **times)
        return sent[0]

    def captions(self, count):
        """What went out once `count` lines have (5 s at most)."""
        self.until(lambda: len(self.sent) >= count)
        return list(self.sent)

    def held(self):
        """An event the fake translator waits for (set at the test's end at the latest)."""
        event = threading.Event()
        self.addCleanup(event.set)
        return event

    @staticmethod
    def outcomes(logs):
        """What the log said became of each line."""
        said = ("AI captions: translated", "AI captions: shown as written")
        return [r.getMessage() for r in logs.records if r.getMessage().startswith(said)]

    def test_translated_line_carries_its_original(self):
        # Plan 4 (Words on the audio screen): the translation takes the line's place, its parts
        # stay (each said sentence's time: the Stage shows the line said part by part) and the
        # line as heard comes after them; one of one part has none ([]: a reader of the shape
        # before still reads it).
        written = self.written(TWO_PARTS, 104.0, 107.9, 108.5, TWO_PARTS_VOICED)
        self.assertEqual(len(written[5]), 2)
        run = self.a_run(self.translation("English"), "Turkish")
        with self.assertLogs("services.captions.worker", logging.DEBUG) as logs:
            run._write("qwen", "heard", TWO_PARTS, 104.0, 107.9, 108.5, TWO_PARTS_VOICED)
            run._write("qwen", "heard", "Üç.", 109.0, 110.0)
            sent = self.captions(2)
        translated = "<English> " + TWO_PARTS
        self.assertEqual(
            sent,
            [
                ("caption", 1, 104.0, 108.5, translated, written[5], TWO_PARTS),
                ("caption", 1, 109.0, 110.0, "<English> Üç.", [], "Üç."),
            ],
        )
        self.assertEqual(
            [call[:3] for call in self.calls],
            [(TWO_PARTS, "Turkish", "English"), ("Üç.", "Turkish", "English")],
        )
        self.assertEqual(self.made, [(MT_FOLDER, 2)])
        self.assertEqual(
            self.outcomes(logs),
            [
                f"AI captions: translated {TWO_PARTS!r}: {translated!r}",
                "AI captions: translated 'Üç.': '<English> Üç.'",
            ],
        )

    def test_untranslated_line_has_no_original(self):
        # A line sent as written (translation off, the target's own language or one MiLMMT
        # lacks, or too late for it) keeps the shape before: 5 slots, 6 with its parts; the
        # GUI reads no original from it.
        from services.captions.process import caption_of

        lone = self.written("Bir.", 104.0, 105.0)
        parted = self.written(TWO_PARTS, 104.0, 107.9, voiced=TWO_PARTS_VOICED)
        self.assertEqual((len(lone), len(parted)), (5, 6))
        translation = self.translation("English")
        run = self.a_run(translation, "English", newest=120.0)  # the target's own
        run._write("qwen", "heard", "Bir.", 104.0, 105.0)
        run._write("qwen", "heard", TWO_PARTS, 104.0, 107.9, voiced=TWO_PARTS_VOICED)
        run.language, self.viewer[0] = "Turkish", 104.2  # 0.1 s from its first word: late
        run._write("qwen", "heard", TWO_PARTS, 104.0, 107.9, voiced=TWO_PARTS_VOICED)
        self.assertEqual(self.captions(3), [lone, parted, parted])
        self.assertEqual(self.calls, [])
        # caption_of: (run, start, end, text, parts, original)
        self.assertEqual(caption_of(lone), (1, 104.0, 105.0, "Bir.", [], ""))
        self.assertEqual(caption_of(parted)[4:], (list(parted[5]), ""))

    def test_a_line_in_the_target_or_an_unknown_or_uncovered_language_goes_out_as_written(self):
        written = self.written(TWO_PARTS, 104.0, 107.9, voiced=TWO_PARTS_VOICED)
        self.assertEqual(len(written), 6)  # with its parts
        run = self.a_run(self.translation("English"))
        with self.assertLogs("services.captions.worker", logging.DEBUG) as logs:
            # the target's own; Automatic before the language settles; one MiLMMT lacks
            for language in ("English", "", "Ukrainian"):
                run.language = language
                run._write("qwen", "heard", TWO_PARTS, 104.0, 107.9, voiced=TWO_PARTS_VOICED)
            self.assertEqual(self.captions(3), [written] * 3)
        self.assertEqual((self.calls, self.outcomes(logs)), ([], []))  # nor a word in the log

    def test_a_line_whose_translation_misses_its_time_goes_out_as_written_with_its_parts(self):
        written = self.written(TWO_PARTS, 104.0, 107.9, voiced=TWO_PARTS_VOICED)
        translation = self.translation("English")
        run = self.a_run(translation, "Turkish", newest=120.0)
        self.viewer[0] = 104.2  # waiting where its first word shows (104.3): 0.2 s past due
        with self.assertLogs("services.captions.worker", logging.DEBUG) as logs:
            run._write("qwen", "heard", TWO_PARTS, 104.0, 107.9, voiced=TWO_PARTS_VOICED)
            self.assertEqual(self.captions(1), [written])
            self.assertEqual(self.calls, [])  # not even begun

            def gives_up(text, source, target, deadline):  # as Translator at its deadline
                time.sleep(max(0.0, deadline - time.monotonic()) + 0.01)

            self.answer = gives_up
            self.plays(translation, 103.8)  # 0.5 s to its first word: 0.2 s to translate
            run._write("qwen", "heard", TWO_PARTS, 104.0, 107.9, voiced=TWO_PARTS_VOICED)
            self.assertEqual(self.captions(2), [written, written])
            self.answer = lambda text, source, target, deadline: None  # a bad output
            self.plays(translation, 102.0)
            run._write("qwen", "heard", TWO_PARTS, 104.0, 107.9, voiced=TWO_PARTS_VOICED)
            self.assertEqual(self.captions(3), [written] * 3)
        self.assertEqual(len(self.calls), 2)
        said = self.outcomes(logs)
        self.assertEqual(len(said), 3)
        self.assertRegex(said[0], r"\d\.\d s late for its translation")
        self.assertRegex(said[1], r"not translated in the \d\.\d s it had")
        self.assertRegex(said[2], r"no usable translation")

    def test_a_translation_has_till_the_picture_gets_to_the_lines_first_word(self):
        from services.captions.worker import DEADLINE_MARGIN_S

        translation = self.translation("English")
        run = self.a_run(translation, "Turkish", newest=105.0)
        run._write("qwen", "heard", "Bir.", 104.0, 105.0)
        self.captions(1)
        # a run begins with the picture waiting: it plays on once 7.5 s behind the sound read
        # (2.5 s from now), then 4.3 s to the line's first word
        self.assertAlmostEqual(self.calls[0][3], 2.5 + 4.3 - DEADLINE_MARGIN_S, delta=0.5)
        self.plays(translation, 101.0)
        run._write("qwen", "heard", "İki.", 104.0, 105.0)
        self.captions(2)
        self.assertAlmostEqual(self.calls[1][3], 3.3 - DEADLINE_MARGIN_S, delta=0.5)
        self.viewer[1] = 2.0  # watched at 2x: it gets there in half the time
        self.plays(translation, 101.0)
        run._write("qwen", "heard", "Üç.", 104.0, 105.0)
        self.captions(3)
        self.assertAlmostEqual(self.calls[2][3], 3.3 / 2 - DEADLINE_MARGIN_S, delta=0.5)

    def test_a_slow_translation_keeps_the_lines_after_it_waiting_in_order(self):
        go = self.held()

        def answer(text, source, target, deadline):
            if text == "İki.":
                go.wait(5)
            return f"<{target}> {text}"

        self.answer = answer
        run = self.a_run(self.translation("English"), "Turkish")
        run._write("qwen", "heard", "Bir.", 104.0, 104.5)
        run._write("qwen", "heard", "İki.", 104.5, 105.0)  # slow
        run.language = "English"  # ("language", name) mid-run: a line not to translate
        run._write("qwen", "heard", "Three.", 105.0, 105.5)
        self.captions(1)
        time.sleep(0.1)  # time enough for a line to overtake the slow one
        self.assertEqual([m[4] for m in self.sent], ["<English> Bir."])
        go.set()
        texts = [m[4] for m in self.captions(3)]
        self.assertEqual(texts, ["<English> Bir.", "<English> İki.", "Three."])

    def test_a_new_run_or_a_stop_drops_the_lines_waiting(self):
        gates = {"Bir.": self.held(), "Dört.": self.held()}
        began: list = []

        def answer(text, source, target, deadline):
            began.append(text)
            if text in gates:
                gates[text].wait(5)
            return f"<{target}> {text}"

        self.answer = answer
        translation = self.translation("English")
        old = self.a_run(translation, "Turkish", 1)
        old._write("qwen", "heard", "Bir.", 104.0, 104.5)  # being translated
        old._write("qwen", "heard", "İki.", 104.5, 105.0)  # waiting
        self.until(lambda: began)
        new = self.a_run(translation, "Turkish", 2)  # a seek, another channel: a new run
        new._write("qwen", "heard", "Üç.", 104.0, 105.0)
        gates["Bir."].set()
        self.assertEqual(
            self.captions(1), [("caption", 2, 104.0, 105.0, "<English> Üç.", [], "Üç.")]
        )
        new._write("qwen", "heard", "Dört.", 105.0, 106.0)  # being translated
        new._write("qwen", "heard", "Beş.", 106.0, 107.0)  # waiting
        self.until(lambda: "Dört." in began)
        translation.follow(None)  # ("stop",): captions off
        gates["Dört."].set()
        self.a_run(translation, "Turkish", 3)._write("qwen", "heard", "Altı.", 107.0, 108.0)
        self.assertEqual(
            self.captions(2)[1], ("caption", 3, 107.0, 108.0, "<English> Altı.", [], "Altı.")
        )
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(began, ["Bir.", "Üç.", "Dört.", "Altı."])  # "İki.", "Beş." never begun

    def test_a_run_read_again_after_a_jump_keeps_its_lines_waiting(self):
        # a resync: the run reads no more, but its lines still show (the controller keeps the
        # run's captions while it starts the next, ui.captions_controller)
        go, began = self.held(), threading.Event()

        def answer(text, source, target, deadline):
            began.set()
            go.wait(5)
            return f"<{target}> {text}"

        self.answer = answer
        run = self.a_run(self.translation("English"), "Turkish")
        run._write("qwen", "heard", "Bir.", 104.0, 104.5)  # being translated
        run._write("qwen", "heard", "İki.", 104.5, 105.0)  # waiting
        began.wait(5)
        logs = self.enterContext(self.assertLogs("services.captions.worker", logging.DEBUG))
        run.stopped.set()  # _resync: the stream's stamps jumped
        go.set()
        self.assertEqual(
            self.captions(2),
            [
                ("caption", 1, 104.0, 104.5, "<English> Bir.", [], "Bir."),
                self.written("İki.", 104.5, 105.0),
            ],
        )
        self.assertEqual([call[0] for call in self.calls], ["Bir."])  # no more time spent on it
        self.assertEqual(
            self.outcomes(logs)[-1], "AI captions: shown as written, its run reads no more: 'İki.'"
        )

    def test_off_sends_the_lines_waiting_as_written_at_once(self):
        go, began = self.held(), threading.Event()

        def answer(text, source, target, deadline):
            began.set()
            go.wait(5)
            return f"<{target}> {text}"

        self.answer = answer
        translation = self.translation("English")
        run = self.a_run(translation, "Turkish")
        run._write("qwen", "heard", TWO_PARTS, 104.0, 107.9, voiced=TWO_PARTS_VOICED)
        run._write("qwen", "heard", "Üç.", 108.0, 109.0)
        began.wait(5)  # the first being translated, the second waiting
        logs = self.enterContext(self.assertLogs("services.captions.worker", logging.DEBUG))
        translation.set(None)
        as_written = [
            self.written(TWO_PARTS, 104.0, 107.9, voiced=TWO_PARTS_VOICED),
            self.written("Üç.", 108.0, 109.0),
        ]
        self.assertEqual(self.sent, as_written)  # at once, the one being translated too
        run._write("qwen", "heard", "Dört.", 109.0, 110.0)
        self.assertEqual(self.sent[2:], [self.written("Dört.", 109.0, 110.0)])  # straight out
        self.assertIsNone(translation._translator)  # its memory back (the final review)
        translation.set("English")  # on again: the model loads again
        go.set()
        self.assertTrue(self.until(translation._ready))
        run._write("qwen", "heard", "Beş.", 110.0, 111.0)
        sent = self.captions(4)
        self.assertEqual(sent[3], ("caption", 1, 110.0, 111.0, "<English> Beş.", [], "Beş."))
        self.assertEqual(len(sent), 4)  # the first went once: its translation never came
        said = [message for message in self.outcomes(logs) if TWO_PARTS in message]
        self.assertEqual(said, [f"AI captions: shown as written, translation off: {TWO_PARTS!r}"])
        self.assertEqual(self.made, [(MT_FOLDER, 2)] * 2)

    def test_off_lets_the_model_go_and_another_target_loads_it_again(self):
        # the final review: after ("translate", None) the model (0.7 to 1 GB) stayed loaded
        # until captions went off
        translation = self.translation("English")
        translation.set(None)
        self.assertIsNone(translation._translator)
        self.assertFalse(translation._ready())
        translation.set("German")
        self.assertTrue(self.until(translation._ready))
        self.assertEqual(self.made, [(MT_FOLDER, 2)] * 2)

    def test_off_while_the_model_loads_keeps_none_of_it(self):
        go, began = self.held(), threading.Event()

        def warm():
            began.set()
            return go.wait(5)

        self.warm = warm
        translation = self.translation("English", loaded=False)
        self.assertTrue(began.wait(5))
        with self.assertLogs("services.captions.worker", logging.INFO) as logs:
            translation.set(None)
            go.set()
            self.assertTrue(
                self.until(lambda: any("not kept" in r.getMessage() for r in logs.records))
            )
        self.assertIsNone(translation._translator)
        translation.set("English")  # wanted again: loaded again
        self.assertTrue(self.until(translation._ready))
        self.assertEqual(len(self.made), 2)

    def test_a_dead_translator_thread_never_keeps_a_line(self):
        # the final review's insurance: lines queued for a thread that is gone would never show
        go, began = self.held(), threading.Event()

        def answer(text, source, target, deadline):
            began.set()
            go.wait(5)
            return f"<{target}> {text}"

        self.answer = answer
        translation = self.translation("English")
        run = self.a_run(translation, "Turkish")
        run._write("qwen", "heard", "Bir.", 104.0, 104.5)  # being translated
        run._write("qwen", "heard", "İki.", 104.5, 105.0)  # waiting
        self.assertTrue(began.wait(5))
        translation._thread = SimpleNamespace(is_alive=lambda: False)  # as if it had died
        run._write("qwen", "heard", "Üç.", 105.0, 105.5)
        self.assertEqual(
            self.sent,
            [
                self.written("Bir.", 104.0, 104.5),
                self.written("İki.", 104.5, 105.0),
                self.written("Üç.", 105.0, 105.5),
            ],
        )  # at once, in order
        go.set()

    def test_a_model_that_did_not_load_says_why_in_the_owners_log(self):
        self.warm = lambda: False
        with self.assertLogs("services.captions.worker", logging.WARNING) as logs:
            translation = self.translation("English", loaded=False)
            self.assertTrue(self.until(lambda: translation.reserve() == 0.0))
        [said] = [r.getMessage() for r in logs.records if "did not load" in r.getMessage()]
        self.assertIn("OSError: no tokenizer.json", said)

    def test_speech_never_waits_for_translation_nor_counts_its_time(self):
        import queue

        from services.captions import worker
        from services.captions.pieces import Piece

        go = self.held()
        self.answer = lambda text, source, target, deadline: go.wait(5) and "Good evening."
        run = self.a_run(self.translation("English"), "French", newest=140.0)
        run.pieces, run._working, run.rtf = queue.Queue(), None, 0.5
        for start in (101.0, 104.5):
            run.pieces.put(Piece(start, start + 3.0, array("f"), 1.0))
        decoded = []

        def decode(samples, language, fast=False):
            decoded.append(1)
            return "Bonsoir à tous."

        run.model = SimpleNamespace(recognizer=object(), decode=decode)

        def caught_up():  # a progress written to all the sound read
            return [m for m in self.sent if m[0] == "progress" and m[4] == 140.0]

        with mock.patch.object(worker, "_PROGRESS_S", 0.05):
            thread = threading.Thread(target=run._recognize, daemon=True)
            thread.start()
            self.until(lambda: len(decoded) == 2 and caught_up())
            run.stopped.set()
            run.pieces.put(None)
            thread.join(5)
        self.assertEqual(len(decoded), 2)  # the second read while the first's translation waits
        self.assertTrue(caught_up())  # the lines waiting for translation are written
        self.assertLess(run.rtf, 0.4)  # speech's own time alone: 0.5, 0.4, 0.32
        self.assertEqual([m for m in self.sent if m[0] == "caption"], [])  # still translating

    def test_the_model_loads_once_it_is_wanted_and_no_line_waits_for_it(self):
        go = self.held()
        self.warm = lambda: go.wait(5)
        translation = self.translation("English", loaded=False)
        self.assertTrue(self.until(lambda: self.made))  # loading, before any line
        run = self.a_run(translation, "Turkish")
        run._write("qwen", "heard", "Bir.", 104.0, 105.0)
        self.assertEqual(self.sent, [self.written("Bir.", 104.0, 105.0)])  # at once, as written
        go.set()
        self.assertTrue(self.until(translation._ready))
        run._write("qwen", "heard", "İki.", 105.0, 106.0)
        self.assertEqual(
            self.captions(2)[1], ("caption", 1, 105.0, 106.0, "<English> İki.", [], "İki.")
        )

    def test_the_model_is_loaded_once_and_kept_and_anew_from_another_folder(self):
        with self.assertLogs("services.captions.worker", logging.INFO) as logs:
            translation = self.translation("English", MT_FOLDER)
            translation.set("English", str(MT_FOLDER))  # the same folder, as the spec has it
            translation.set("German")  # another language: the model kept (off lets it go)
            self.assertTrue(translation._ready())
        self.assertEqual(self.made, [(MT_FOLDER, 2)])
        [load] = [r.getMessage() for r in logs.records]
        self.assertRegex(
            load, r"loading the translation model \(qitv-mt-milmmt-46-1b\) took \d+\.\d s"
        )
        translation.set("German", "another-folder")  # downloaded again elsewhere
        self.assertTrue(self.until(lambda: len(self.made) == 2 and translation._ready()))
        self.assertEqual(self.made[1], (Path("another-folder"), 2))

    def test_a_model_that_does_not_load_is_said_once_and_not_tried_again(self):
        self.warm = lambda: False  # (services.captions.translate logs why)
        with self.assertLogs("services.captions.worker", logging.DEBUG) as logs:
            translation = self.translation("English", MT_FOLDER, loaded=False)
            self.until(lambda: [r for r in logs.records if r.levelno == logging.WARNING])
            run = self.a_run(translation, "Turkish")
            run._write("qwen", "heard", "Bir.", 104.0, 105.0)
            translation.set("German", str(MT_FOLDER))  # the same folder: not again
            run._write("qwen", "heard", "İki.", 105.0, 106.0)
            time.sleep(0.05)  # time for a load it should not try
        written = [self.written("Bir.", 104.0, 105.0), self.written("İki.", 105.0, 106.0)]
        self.assertEqual(self.sent, written)  # as written, at once
        self.assertEqual(self.made, [(MT_FOLDER, 2)])
        [warning] = [r.getMessage() for r in logs.records if r.levelno >= logging.WARNING]
        self.assertIn("caption translation is unavailable", warning)
        self.assertEqual(self.outcomes(logs), [])  # no word per line
        self.assertFalse([r for r in logs.records if "took" in r.getMessage()])  # not loaded

        def raises():
            raise RuntimeError("no model")

        self.warm = raises
        with self.assertLogs("services.captions.worker", logging.WARNING) as logs:
            translation.set("German", "another-folder")  # another folder: tried
            self.until(lambda: logs.records)
            time.sleep(0.05)  # time for a second word it should not say
        self.assertEqual(len(self.made), 2)
        self.assertFalse(translation._ready())
        [warning] = [r.getMessage() for r in logs.records]
        self.assertTrue(warning.endswith("did not load: RuntimeError: no model"), warning)

    def budget(self, run):
        """How long a piece of `run` may grow now (its cutter's length), sound read to 101.1 s."""
        import queue

        run.cutter, run.rtf = Cutter(lambda window: False), 0.5
        run.pieces, run._working = queue.Queue(), None
        run._take(101.0, array("f", bytes(4 * 1600)))
        return run.cutter.max_s

    def test_while_translating_a_piece_may_grow_the_translators_time_less(self):
        from services.captions.streaming import TRANSLATE_LEAD_S

        self.viewer[0] = 90.0  # the sound read 11.1 s ahead of the picture
        translation = self.translation("English")
        for kind in (self.run_class, self.film_class):
            run = self.a_run(None)  # translation off: as before 2.10
            run.__class__ = kind
            plain = self.budget(run)
            self.assertAlmostEqual(plain, (11.1 - 0.6 - 0.5) / 1.5)  # END_PAUSE_S, _MARGIN_S
            written_to = run.written_to()
            run.translation = translation
            translation.follow(run)
            self.assertAlmostEqual(self.budget(run), plain - TRANSLATE_LEAD_S / 1.5, msg=kind)
            self.assertEqual(run.written_to(), written_to)  # what "Catching up" reads: as it was
            self.viewer[0] -= TRANSLATE_LEAD_S  # the picture held that much further back (5)
            self.assertAlmostEqual(self.budget(run), plain)  # pieces as long as without it
            self.viewer[0] += TRANSLATE_LEAD_S
            translation.set(None)  # turned off mid-run
            self.assertEqual(self.budget(run), plain)
            translation.set("English")  # on again (its model loads again)
            self.assertAlmostEqual(self.budget(run), plain - TRANSLATE_LEAD_S / 1.5)
            self.assertTrue(self.until(translation._ready))  # (loaded while the fake is in)

    def test_the_translators_time_is_kept_while_its_model_loads_and_not_once_it_failed(self):
        from services.captions.streaming import TRANSLATE_LEAD_S

        go = self.held()
        self.warm = lambda: go.wait(5) and False  # it loads, then fails
        translation = self.translation("English", loaded=False)
        self.assertTrue(self.until(lambda: self.made))
        self.assertEqual(translation.reserve(), TRANSLATE_LEAD_S)  # loading
        with self.assertLogs("services.captions.worker", logging.WARNING):
            go.set()
            self.assertTrue(self.until(lambda: translation.reserve() == 0.0))  # failed
        translation.set("German")  # the same folder: still none
        self.assertEqual(translation.reserve(), 0.0)
        self.assertEqual(self.translation(None, loaded=False).reserve(), 0.0)  # no target
        self.assertEqual(self.translation("English", None, loaded=False).reserve(), 0.0)

    def test_the_turkish_writer_keeps_the_translators_time_as_it_is_turned_on_and_off(self):
        import queue

        from services.captions.streaming import TRANSLATE_LEAD_S

        translation = self.translation("English")
        run = self.a_run(translation, "Turkish")
        seen: list = []

        class Writer:  # the growing writer, as the run's stream uses it
            reserve = 0.0

            def push(self, at, floats):
                pass

            def work(self):
                seen.append(self.reserve)
                return []

        run.writer, run.blocks, run.rtf = Writer(), queue.Queue(), 0.5
        run.model = SimpleNamespace(writes=lambda language: "moonshine")
        thread = threading.Thread(target=run._stream, daemon=True)
        thread.start()
        run.blocks.put((100.0, array("f", bytes(4 * 1600))))
        self.assertTrue(self.until(lambda: seen))
        translation.set(None)  # off mid-run
        self.assertTrue(self.until(lambda: seen[-1] == 0.0))
        translation.set("English")
        self.assertTrue(self.until(lambda: seen[-1] == TRANSLATE_LEAD_S))
        run.blocks.put(None)
        thread.join(5)
        self.assertTrue(self.until(translation._ready))  # (loaded again while the fake is in)
        self.assertEqual(seen[0], TRANSLATE_LEAD_S)

    def serve(self, *script):
        """The process's command loop over `script`, with a fake model; its runs are bare ones
        (self.runs). A callable in the script runs between two commands."""
        from services.captions import worker

        self.runs: list = []

        def run(link, model, number, where, start, track, language, viewer, *rest, **kwargs):
            made = self.a_run(kwargs.get("translation"), language, number, follow=False)
            made.viewer, made.start, made.stop = viewer, lambda: None, made.stopped.set
            self.runs.append(made)
            return made

        steps = iter(script + (("quit",),))

        def recv():
            step = next(steps)
            while callable(step):
                step()
                step = next(steps)
            return step

        def model(spec):  # the speech model loads (and puts its runtime on sys.path) ...
            time.sleep(0.05)
            if spec.get("fails"):
                raise RuntimeError("no speech model")
            self.speech.append(len(self.made))  # ... before the translator is made
            return SimpleNamespace()

        self.speech: list = []
        conn: Any = SimpleNamespace(recv=recv, send=self.sent.append)
        with (
            mock.patch.object(worker, "_Model", model),
            mock.patch.object(worker, "_Run", run),
            mock.patch.object(worker, "_FilmRun", run),
        ):
            worker._serve(worker._Link(conn))

    def test_the_process_translates_from_its_load_and_as_the_command_says(self):
        other = Path("another-folder")
        self.serve(
            ("load", {"translation": str(MT_FOLDER), "translate_to": "English"}),
            lambda: self.assertTrue(self.until(lambda: self.made)),  # loading: no line asked
            ("start", 5, "http://127.0.0.1/side", 100.0, None, "Turkish"),
            ("film", 6, "film.mkv", 0.0, None, "French", ()),
            ("translate", "German", str(other)),  # a model downloaded after the speech one
            lambda: self.until(lambda: len(self.made) == 2 and self.runs[1].translation._ready()),
            lambda: self.runs[1]._write("qwen", "heard", "Bonsoir.", 104.0, 105.0),
            lambda: self.captions(2),
            ("translate", None),
            lambda: self.runs[1]._write("qwen", "heard", "Merci.", 105.0, 106.0),
        )
        first, second = self.runs
        self.assertIs(first.translation, second.translation)  # one for the process
        self.assertEqual(self.speech, [0])  # the translator found the speech model's runtime
        self.assertEqual(self.made, [(MT_FOLDER, 2), (other, 2)])
        self.assertEqual(
            self.sent,
            [
                ("ready",),
                ("caption", 6, 104.0, 105.0, "<German> Bonsoir.", [], "Bonsoir."),
                ("caption", 6, 105.0, 106.0, "Merci."),  # off: straight out
            ],
        )

    def test_a_stop_drops_the_lines_being_translated_and_waiting(self):
        go, began = self.held(), threading.Event()

        def answer(text, source, target, deadline):
            began.set()
            go.wait(5)
            return f"<{target}> {text}"

        self.answer = answer

        def done():  # the translator has nothing left
            translation = self.runs[0].translation
            return translation._busy is None and not translation._lines

        self.serve(
            ("load", {"translation": str(MT_FOLDER), "translate_to": "English"}),
            ("start", 5, "http://127.0.0.1/side", 100.0, None, "Turkish"),
            lambda: self.until(lambda: self.runs[0].translation._ready()),
            lambda: self.runs[0]._write("qwen", "heard", "Bir.", 104.0, 105.0),
            lambda: self.runs[0]._write("qwen", "heard", "İki.", 105.0, 106.0),
            lambda: began.wait(5),
            ("stop",),  # captions off
            lambda: go.set(),
            lambda: self.assertTrue(self.until(done)),
        )
        self.assertEqual(self.sent, [("ready",)])  # neither went out
        self.assertEqual([call[0] for call in self.calls], ["Bir."])

    def test_no_translator_without_a_speech_model(self):
        with self.assertLogs("services.captions.worker", logging.WARNING):
            self.serve(
                ("load", {"translation": str(MT_FOLDER), "translate_to": "English", "fails": True}),
                lambda: time.sleep(0.05),
            )
        self.assertEqual(self.sent, [("failed", "RuntimeError")])
        self.assertEqual(self.made, [])

    def test_a_first_line_while_the_picture_plays_is_given_no_time_for_a_hold(self):
        from services.captions.worker import DEADLINE_MARGIN_S

        self.serve(
            ("load", {"translation": str(MT_FOLDER), "translate_to": "English"}),
            ("start", 5, "http://127.0.0.1/side", 100.0, None, "Turkish"),
            lambda: self.until(lambda: self.runs[0].translation._ready()),
            ("viewer", 100.5),  # it plays
            lambda: setattr(self.runs[0], "newest", 105.5),  # a waiting one would wait 2.5 s
            lambda: self.runs[0]._write("qwen", "heard", "Bir.", 104.0, 105.0),
            lambda: self.captions(2),
        )
        self.assertAlmostEqual(self.calls[0][3], 104.3 - 100.5 - DEADLINE_MARGIN_S, delta=0.5)

    def test_the_detailed_log_gets_each_line_from_the_process(self):
        # each line logs at LINE_LEVEL (DEBUG), which the process's handler let through only at
        # INFO: Settings' detailed log showed no line, translated or not
        from services.captions import worker

        sent: list = []
        logger = logging.getLogger("qitv-test-captions-process")
        logger.propagate = False
        self.addCleanup(setattr, logger, "handlers", [])
        link: Any = SimpleNamespace(send=sent.append)
        logger.handlers[:] = [worker._LogToOwner(link)]
        for detailed in (True, False):  # the process's root level, as captions_main sets it
            logger.setLevel(logging.DEBUG if detailed else logging.INFO)
            logger.log(worker.LINE_LEVEL, "AI captions: translated 'Bir.': 'One.'")
        self.assertEqual(sent, [("log", logging.DEBUG, "AI captions: translated 'Bir.': 'One.'")])

    def test_a_target_without_the_models_folder_translates_nothing_and_says_so_once(self):
        with self.assertLogs("services.captions.worker", logging.INFO) as logs:
            self.serve(
                ("load", {"translate_to": "English", "translation": None}),
                ("translate", "German"),
                ("start", 5, "http://127.0.0.1/side", 100.0, None, "Turkish"),
                lambda: self.runs[0]._write("qwen", "heard", "Bir.", 104.0, 105.0),
            )
        self.assertEqual(len([r for r in logs.records if "translation model" in r.getMessage()]), 1)
        self.assertEqual(self.sent[1:], [("caption", 5, 104.0, 105.0, "Bir.")])  # straight out
        self.assertEqual(self.made, [])

    # ---- 2.13.1: threads, the warm-up's speed, the GUI's lead, lateness, the line before ----
    def test_the_translator_runs_on_the_threads_this_computer_gives_it(self):
        from services.captions import tier

        with mock.patch.object(tier, "translator_threads", return_value=4):
            self.translation("English")
        self.assertEqual(self.made, [(MT_FOLDER, 4)])

    def test_the_timed_warm_up_is_told_to_the_gui(self):
        self.speed = 0.43
        with self.assertLogs("services.captions.worker", logging.INFO) as logs:
            self.translation("English")
        self.assertEqual(self.sent, [("translate_speed", 0.43, 2)])  # (its threads: 2 here)
        self.assertTrue([r for r in logs.records if "medium line in 0.43 s" in r.getMessage()])
        self.sent.clear()
        self.speed = None  # nothing timed (it gave nothing): the GUI keeps its lead
        self.translation("German", "another-folder")
        self.assertEqual(self.sent, [])

    def test_the_reserve_is_the_lead_the_gui_gives(self):
        from services.captions.streaming import TRANSLATE_LEAD_S

        translation = self.translation("English")
        self.assertEqual(translation.reserve(), TRANSLATE_LEAD_S)  # none given: 2.10's
        translation.set_lead(2.5)
        self.assertEqual(translation.reserve(), 2.5)
        translation.set(None)
        self.assertEqual(translation.reserve(), 0.0)  # off
        translation.set("English")
        self.assertEqual(translation.reserve(), 2.5)  # on again: the lead it was given
        translation.set_lead(None)  # (a load that names none)
        self.assertEqual(translation.reserve(), 2.5)
        self.assertTrue(self.until(translation._ready))  # (loaded again while the fake is in)

    def test_the_process_takes_the_lead_from_its_load_and_as_the_command_says(self):
        seen: list = []
        self.serve(
            (
                "load",
                {"translation": str(MT_FOLDER), "translate_to": "English", "translate_lead": 4.0},
            ),
            ("start", 5, "http://127.0.0.1/side", 100.0, None, "Turkish"),
            lambda: seen.append(self.runs[0].translation.reserve()),
            ("translate_lead", 3.0),
            lambda: seen.append(self.runs[0].translation.reserve()),
            lambda: self.until(lambda: self.runs[0].translation._ready()),  # (the fake's load)
        )
        self.assertEqual(seen, [4.0, 3.0])

    def test_a_line_late_for_its_translation_that_had_the_lead_says_the_lead_is_short(self):
        go = self.held()

        def answer(text, source, target, deadline):
            if text == "Bir.":
                go.wait(5)
            return f"<{target}> {text}"

        self.answer = answer
        translation = self.translation("English")
        translation.set_lead(2.0)
        run = self.a_run(translation, "Turkish", newest=104.0)
        self.plays(translation, 100.0)
        run._write("qwen", "heard", "Bir.", 100.5, 101.0)  # being translated
        run._write("qwen", "heard", "İki.", 101.5, 102.0)  # 1.5 s to its deadline: enough
        began = time.monotonic()
        while time.monotonic() - began < 1.7:  # the picture plays on (the command loop's view)
            self.plays(translation, 100.0 + time.monotonic() - began)
            time.sleep(0.05)
        go.set()  # its deadline passed while the line before was translated
        self.assertTrue(self.until(lambda: len(self.sent) >= 3))
        self.assertIn(("translate_late", 1, 2.0), self.sent)
        self.assertEqual(self.sent[-1], self.written("İki.", 101.5, 102.0))  # as written

    def test_a_line_cut_off_at_its_deadline_that_had_the_lead_says_so_too(self):
        def gives_up(text, source, target, deadline):  # as Translator at its deadline
            time.sleep(max(0.0, deadline - time.monotonic()) + 0.01)

        self.answer = gives_up
        translation = self.translation("English")
        translation.set_lead(2.0)
        run = self.a_run(translation, "Turkish", newest=104.0)
        self.plays(translation, 100.0)
        run._write("qwen", "heard", "Bir.", 101.2, 102.0)  # 1.2 s to translate it
        self.assertTrue(self.until(lambda: len(self.sent) >= 2))
        self.assertEqual(
            self.sent, [("translate_late", 1, 2.0), self.written("Bir.", 101.2, 102.0)]
        )

    def test_a_line_written_late_for_the_lead_does_not_count(self):
        # a run's first lines after a seek: written with the picture already near their words
        def gives_up(text, source, target, deadline):
            time.sleep(max(0.0, deadline - time.monotonic()) + 0.01)

        self.answer = gives_up
        translation = self.translation("English")
        translation.set_lead(2.0)
        run = self.a_run(translation, "Turkish", newest=104.0)
        self.plays(translation, 100.0)
        run._write("qwen", "heard", "Bir.", 100.4, 101.0)  # 0.4 s: well short of the lead
        self.plays(translation, 100.5)
        run._write("qwen", "heard", "İki.", 100.6, 101.0)  # past due already
        self.assertEqual(
            self.captions(2),
            [self.written("Bir.", 100.4, 101.0), self.written("İki.", 100.6, 101.0)],
        )
        time.sleep(0.05)
        self.assertNotIn("translate_late", [message[0] for message in self.sent])

    def test_into_a_target_with_he_and_she_a_line_goes_with_the_one_before(self):
        run = self.a_run(self.translation("English"), "Turkish")
        run._write("qwen", "heard", "Ayşe bugün aradı.", 104.0, 105.0)
        run._write("qwen", "heard", "Yarın sabah geliyor.", 106.0, 107.0)
        run._write("qwen", "heard", "Gitti.", 107.5, 108.0)
        self.captions(3)
        self.assertEqual(
            self.contexts,
            [
                None,
                ("Ayşe bugün aradı.", "<English> Ayşe bugün aradı."),
                ("Yarın sabah geliyor.", "<English> Yarın sabah geliyor."),
            ],
        )

    def test_a_line_before_of_under_three_words_is_none(self):
        # the owner: "Yes." before a line made the leak's second translation often
        run = self.a_run(self.translation("English"), "Turkish")
        run._write("qwen", "heard", "Evet.", 104.0, 104.5)
        run._write("qwen", "heard", "Ayşe aradı.", 105.0, 106.0)
        run._write("qwen", "heard", "Yarın sabah geliyor.", 106.5, 107.5)
        run._write("qwen", "heard", "Sonra gidecek.", 108.0, 109.0)
        self.captions(4)
        self.assertEqual(self.contexts, [None, None, None, self.contexts[3]])
        self.assertEqual(
            self.contexts[3], ("Yarın sabah geliyor.", "<English> Yarın sabah geliyor.")
        )

    def test_into_a_target_without_them_each_line_goes_alone(self):
        run = self.a_run(self.translation("Turkish"), "English")
        run._write("qwen", "heard", "She called.", 104.0, 105.0)
        run._write("qwen", "heard", "She came.", 105.5, 106.0)
        self.captions(2)
        self.assertEqual(self.contexts, [None, None])

    def test_the_line_before_is_never_one_across_a_gap_a_run_a_language_or_a_target(self):
        translation = self.translation("English")
        run = self.a_run(translation, "Turkish")
        run._write("qwen", "heard", "Bir iki üç.", 104.0, 105.0)
        run._write("qwen", "heard", "Dört beş altı.", 115.5, 116.0)  # 10.5 s on: another scene
        run._write("qwen", "heard", "Yedi sekiz dokuz.", 116.5, 117.0)  # (with the one before)
        self.captions(3)
        run.language = "German"  # ("language", name): another language heard
        run._write("qwen", "heard", "Eins zwei drei.", 117.5, 118.0)
        self.captions(4)
        new = self.a_run(translation, "German", 2)  # a zap, a tune, a seek: a new run
        new._write("qwen", "heard", "Vier fünf sechs.", 118.5, 119.0)
        self.captions(5)
        translation.set("French")  # another target
        self.assertTrue(self.until(translation._ready))
        new._write("qwen", "heard", "Sieben acht neun.", 119.5, 120.0)
        self.captions(6)
        before = ("Dört beş altı.", "<English> Dört beş altı.")
        self.assertEqual(self.contexts, [None, None, before, None, None, None])

    def test_a_line_before_that_went_out_as_written_is_none(self):
        self.answer = lambda text, source, target, deadline: (
            None if text == "Bir iki üç." else f"<{target}> {text}"
        )
        run = self.a_run(self.translation("English"), "Turkish")
        run._write("qwen", "heard", "Bir iki üç.", 104.0, 105.0)  # no usable translation
        run._write("qwen", "heard", "Dört beş altı.", 105.5, 106.0)
        run.language = "English"  # a line not to translate breaks the chain too
        run._write("qwen", "heard", "Seven eight nine.", 106.5, 107.0)
        run.language = "Turkish"
        run._write("qwen", "heard", "On on bir.", 107.5, 108.0)
        self.captions(4)
        self.assertEqual(self.contexts, [None, None, None])

    def test_lines_written_before_a_raise_say_the_lead_they_were_written_under(self):
        # the review: a burst written under 2.0 s, translated after the GUI raised the lead,
        # said 3.0 s and raised it again (several waits for one slow spell)
        go = self.held()

        def answer(text, source, target, deadline):
            if text == "Bir iki üç.":
                go.wait(5)
            return f"<{target}> {text}"

        self.answer = answer
        translation = self.translation("English")
        translation.set_lead(2.0)
        run = self.a_run(translation, "Turkish", newest=104.0)
        self.plays(translation, 100.0)
        run._write("qwen", "heard", "Bir iki üç.", 100.5, 101.0)  # being translated
        for second, text in ((101.5, "Dört."), (101.6, "Beş."), (101.7, "Altı.")):
            run._write("qwen", "heard", text, second, second + 0.1)  # 1.5 s or more: the lead's
        translation.set_lead(3.0)  # the GUI raised it meanwhile
        began = time.monotonic()
        while time.monotonic() - began < 2.0:  # the picture plays past their deadlines
            self.plays(translation, 100.0 + time.monotonic() - began)
            time.sleep(0.05)
        go.set()
        self.assertTrue(self.until(lambda: len(self.sent) >= 7))
        late = [message for message in self.sent if message[0] == "translate_late"]
        self.assertEqual(late, [("translate_late", 1, 2.0)] * 3)

    def test_an_unspaced_line_before_counts_its_words_by_its_characters(self):
        # the review: a Japanese or Chinese clause is one "word" by its spaces
        run = self.a_run(self.translation("English"), "Japanese")
        run._write("qwen", "heard", "母は今日来ました。", 104.0, 105.0)  # 9 characters: words
        run._write("qwen", "heard", "はい。", 105.5, 106.0)
        run._write("qwen", "heard", "台所にいます。", 106.5, 107.0)  # (after "はい。": none)
        self.captions(3)
        self.assertEqual(
            self.contexts, [None, ("母は今日来ました。", "<English> 母は今日来ました。"), None]
        )

    def test_a_deadline_missed_on_the_second_try_is_not_the_leads(self):
        # the line before leaked into it: translated again alone, and that ran out of its time
        def answer(text, source, target, deadline):
            time.sleep(max(0.0, deadline - time.monotonic()) + 0.01)
            self.second_try = self.contexts[-1] is not None

        self.answer = answer
        translation = self.translation("English")
        translation.set_lead(2.0)
        run = self.a_run(translation, "Turkish", newest=104.0)
        self.plays(translation, 100.0)
        with translation._lock:  # the line before went out translated
            translation._before = worker_said(run, "Ayşe bugün aradı.", 99.0, 100.9)
        run._write("qwen", "heard", "Yarın geliyor.", 101.2, 102.0)  # 1.2 s: the lead's
        self.assertTrue(self.until(lambda: len(self.sent) >= 1))
        time.sleep(0.05)
        self.assertIsNotNone(self.contexts[-1])
        self.assertEqual(self.sent, [self.written("Yarın geliyor.", 101.2, 102.0)])  # no word
        self.sent.clear()
        self.plays(translation, 100.0)
        run._write("qwen", "heard", "Sonra gidecek.", 101.2, 102.0)  # alone: its first try
        self.assertTrue(self.until(lambda: len(self.sent) >= 2))
        self.assertIsNone(self.contexts[-1])
        self.assertEqual(self.sent[0], ("translate_late", 1, 2.0))

    def test_the_gui_hears_the_translators_speed_and_its_late_lines(self):
        from PySide6.QtWidgets import QApplication

        from services.captions.process import CaptionsProcess

        QApplication.instance() or QApplication([])
        process = CaptionsProcess()
        heard: list = []
        process.translateSpeed.connect(lambda *args: heard.append(("speed", *args)))
        process.translateLate.connect(lambda run, lead: heard.append(("late", run, lead)))
        process._supervisor, process._serial = object(), 1  # type: ignore[assignment]
        process._on_message(1, ("translate_speed", 0.43, 4))
        process._on_message(1, ("translate_speed", 0.5))  # (no threads said: 0)
        process._on_message(1, ("translate_late", 3, 2.5))
        process._supervisor = None
        self.assertEqual(heard, [("speed", 0.43, 4), ("speed", 0.5, 0), ("late", 3, 2.5)])


class StreamClockTests(unittest.TestCase):
    def test_the_tables_the_first_stamps_and_the_language_are_read_in_any_chunks(self):
        data = _stream([90_000, 93_600], [89_100, 92_700])
        times = TsTimes()
        for i in range(0, len(data), 100):  # chunks that cut packets
            times.feed(data[i : i + 100])
        self.assertEqual(times.video_pid, 0x101)
        self.assertEqual([(t.pid, t.language) for t in times.audio], [(0x102, "tur")])
        self.assertEqual(times.first_video_pts, 90_000)
        self.assertEqual(times.first_audio_pts, 89_100)
        self.assertFalse(times.jumped)

    def test_a_jump_in_the_pictures_stamps_ends_the_run(self):
        times = TsTimes()
        times.feed(_stream([90_000, 90_000 + 5 * 90_000], [90_000, 90_100]))
        self.assertTrue(times.jumped)

    def test_vlcs_blocks_land_on_the_timeline_from_the_segments_start(self):
        times = TsTimes()
        times.feed(_stream([90_000], [90_000 - 9_000]))  # the sound starts 0.1 s earlier
        clock = StreamClock(100.0, times)
        self.assertAlmostEqual(clock.place(5_000_000, 1600, RATE), 99.9)
        self.assertAlmostEqual(clock.place(5_100_000, 1600, RATE), 100.0)
        self.assertIsNone(clock.place(9_000_000, 1600, RATE))  # VLC's blocks stepped
        self.assertTrue(clock.broken)

    def test_a_run_read_again_past_a_step_reads_on_from_where_its_sound_ended(self):
        # 2026-11-09, the owner's channel: its clock leapt and its sound was anchored again
        # many times a second; the reader started over 25 times from the same second
        times = TsTimes()
        times.feed(_stream([90_000], [90_000]))
        clock = StreamClock(100.0, times)
        self.assertAlmostEqual(clock.place(5_000_000, 1600, RATE), 100.0)
        self.assertIsNone(clock.place(60_000_000, 1600, RATE))  # VLC's blocks stepped 55 s
        clock.rebase(100.1)  # read on: from where the sound before ended
        self.assertAlmostEqual(clock.place(60_000_000, 1600, RATE), 100.1)
        self.assertAlmostEqual(clock.place(60_100_000, 1600, RATE), 100.2)

    def test_a_run_that_reads_on_never_asks_to_read_again(self):
        from services.captions.worker import _Run

        def bare(read_on):
            run: Any = object.__new__(_Run)
            run.stopped, run.run, run.read_on, run._steps = threading.Event(), 7, read_on, 0
            run._resynced, run.newest = False, 100.0
            run.sent, run.placed = [], []
            run.link = SimpleNamespace(send=run.sent.append)
            times = TsTimes()
            times.feed(_stream([90_000], [90_000]))
            run.clock = StreamClock(100.0, times)

            def take(at, floats):
                run.placed.append(round(at, 2))
                run.newest = at + len(floats) / RATE

            run._take = take
            return run

        block = bytes(2 * 1600)
        first = bare(read_on=False)
        for us in (5_000_000, 5_100_000, 60_000_000):  # a step of 55 s
            first._on_block(us, block)
        [(kind, number, newest)] = first.sent  # once: read again past it
        self.assertEqual((kind, number, round(newest, 3)), ("resync", 7, 100.2))
        again = bare(read_on=True)  # ... that run reads on past every next step
        with self.assertLogs("services.captions.worker", "INFO") as logs:
            for us in (5_000_000, 5_100_000, 60_000_000, 60_100_000, 9_000_000, 9_100_000):
                again._on_block(us, block)
        self.assertEqual(again.sent, [])
        self.assertEqual(again.placed, [100.0, 100.1, 100.2, 100.3, 100.4, 100.5])
        self.assertEqual(len(logs.records), 1)  # said once

    def test_the_process_hears_the_picture_s_rate_and_reads_faster_from_1_25x(self):
        from services.captions import worker

        viewer, seen = [100.0, 1.0], [None]
        for second in range(6):
            viewer[0] = 100.0 + 2.0 * second
            worker._rate_seen(viewer, seen, 1000.0 + second)
        self.assertAlmostEqual(viewer[1], 2.0, delta=0.1)
        viewer[0] = 400.0  # a seek: not a rate
        worker._rate_seen(viewer, seen, 1007.0)
        self.assertAlmostEqual(viewer[1], 2.0, delta=0.1)
        run: Any = object.__new__(worker._Run)
        run.viewer = viewer
        self.assertTrue(run.fast())
        viewer[1] = 1.1
        self.assertFalse(run.fast())
        model: Any = object.__new__(worker._Model)
        widths = []

        def transcribe(samples, width=None):
            widths.append(width)
            return ""

        model.beam = SimpleNamespace(transcribe=transcribe)
        model._wrote, model.names = threading.local(), {}
        model.name = lambda role: "beam"
        model.decode(array("f"), worker.TURKISH, fast=True)
        model.decode(array("f"), worker.TURKISH)
        self.assertEqual(widths, [1, None])  # its best path alone at 2x, the beam at 1x

    def test_a_slow_computer_reads_turkish_plainly_only_while_translating(self):
        from services.captions import worker

        class On:
            def __init__(self):
                self.on = True

            def reserve(self):
                return 5.0 if self.on else 0.0

        def run(slow, translation):
            made: Any = object.__new__(worker._Run)
            made.viewer = [100.0, 1.0]
            made.model = SimpleNamespace(slow_to_translate=slow)
            made.translation = translation
            return made

        on = On()
        slow = run(True, on)
        self.assertTrue(slow.fast())
        on.on = False  # translation turned off mid-run: the beam comes back
        self.assertFalse(slow.fast())
        on.on = True
        self.assertFalse(run(False, on).fast())  # a fast computer keeps its beam
        self.assertFalse(run(True, None).fast())
        model: Any = object.__new__(worker._Model)
        widths = []
        model.beam = SimpleNamespace(
            transcribe=lambda samples, width=None: widths.append(width) or ""
        )
        model._wrote, model.names = threading.local(), {}
        model.name = lambda role: "beam"
        model.decode(array("f"), worker.TURKISH, fast=slow.fast())
        on.on = False
        model.decode(array("f"), worker.TURKISH, fast=slow.fast())
        self.assertEqual(widths, [1, None])

    def test_a_beam_read_narrowed_for_once(self):
        from services.captions import moon_beam

        reader: Any = object.__new__(moon_beam.BeamReader)
        reader.settings, reader.scores = moon_beam.Settings(), None

        class Model:
            def steps(self, encoded):
                return mock.MagicMock()

        reader.model = Model()
        seen = []
        with mock.patch.object(
            moon_beam, "search", lambda st, cap, settings, lm: seen.append(settings.width)
        ):
            reader.decode(SimpleNamespace(silent=False, frames=40), width=1)
            reader.decode(SimpleNamespace(silent=False, frames=40))
        self.assertEqual(seen, [1, moon_beam.WIDTH])

    def test_a_radio_channel_is_timed_by_its_sound(self):
        # The helper cuts and times an audio-only channel by its sound (services.live.ts).
        pmt_pid = 0x100
        pat = _section(0x00, bytes([0, 1, 0xE0 | (pmt_pid >> 8), pmt_pid & 0xFF]))
        pmt = _section(0x02, bytes([0xE1, 0x02, 0xF0, 0x00]) + bytes([0x0F, 0xE1, 0x02, 0xF0, 0]))
        data = _packet(0, pat, True) + _packet(pmt_pid, pmt, True)
        data += _packet(0x102, _pes(0xC0, 90_000), True) + _packet(0x102, _pes(0xC0, 91_800), True)
        times = TsTimes()
        times.feed(data)
        clock = StreamClock(30.0, times)
        self.assertTrue(clock.ready())
        self.assertAlmostEqual(clock.place(1_000_000, 1600, RATE), 30.0)

    def test_stamps_across_the_wrap(self):
        self.assertAlmostEqual(pts_seconds(100, (1 << 33) - 800), 0.01)


class CutterTests(unittest.TestCase):
    def cutter(self, speech):
        """A cutter whose detector hears speech where `speech(t)` says (t: the window's
        start, in seconds of what was pushed)."""
        clock = {"t": 0.0}

        def is_speech(window):
            said = speech(clock["t"])
            clock["t"] += WINDOW / RATE
            return said

        return Cutter(is_speech)

    def push(self, cutter, seconds, start=0.0):
        pieces = []
        step = 0.1
        for i in range(round(seconds / step)):
            pieces += cutter.push(start + i * step, array("f", bytes(4 * int(RATE * step))))
        return pieces

    def test_a_pause_ends_a_piece(self):
        cutter = self.cutter(lambda t: 1.0 <= t < 3.0 or 4.0 <= t < 5.0)
        pieces = self.push(cutter, 6.0)
        self.assertEqual(len(pieces), 2)
        self.assertAlmostEqual(pieces[0].start, 0.7, delta=0.07)  # 2.9.3: 0.3 s before
        self.assertAlmostEqual(pieces[0].end, 3.25, delta=0.07)  # and 0.25 s after
        self.assertGreater(pieces[0].speech_share, 0.75)

    def test_speech_without_a_pause_is_cut_at_the_length_the_lead_allows(self):
        cutter = self.cutter(lambda t: not (3.0 <= t < 3.1))  # one short pause between words
        cutter.set_max(4.0)
        pieces = self.push(cutter, 9.0)
        self.assertGreaterEqual(len(pieces), 2)
        self.assertAlmostEqual(pieces[0].end, 3.05, delta=0.1)  # at the pause, not at 4 s
        for piece in pieces:
            self.assertLessEqual(piece.end - piece.start, 4.0 + 1e-6)

    def test_the_length_stays_between_two_and_ten_seconds(self):
        cutter = self.cutter(lambda t: True)
        cutter.set_max(0.5)
        self.assertEqual(cutter.max_s, 2.0)
        cutter.set_max(60)
        self.assertEqual(cutter.max_s, 10.0)  # 2.9.3: 6 s pieces were written worse

    def test_a_piece_cut_at_its_length_says_it_was_split(self):
        # 2.9.4: its sentence goes on in the next; a pause's end is not a split.
        cutter = self.cutter(lambda t: not (8.0 <= t < 9.0))
        cutter.set_max(4.0)
        pieces = self.push(cutter, 10.0)
        self.assertTrue(pieces[0].split)  # at its length, mid-speech
        self.assertFalse(pieces[-1].split)  # the pause at 8 s ended it

    def test_a_language_only_a_model_not_here_writes_gets_no_guesses(self):
        # 2.9.6: Parakeet (Ukrainian and eight more) comes once wanted; "Not now": nothing, not
        # Qwen guessing at a language it doesn't know.
        from services.captions.worker import _Model

        model = object.__new__(_Model)
        model.recognizer, model.turkish, model.european, model.cohere = object(), None, None, None
        model._folders, model._loading = {}, {}
        self.assertEqual(model.decode(array("f", bytes(64)), "Ukrainian"), "")

    def test_a_typographic_apostrophe_keeps_a_suffix_in_its_word(self):
        # "Deniz’e de" read as "deniz", "e", "de": two Portuguese words (2026-11-08)
        self.assertIsNone(text_language.guess("Deniz’e de"))

    def test_qwen_writes_for_good_only_where_no_specialist_writes(self):
        from services.captions.worker import TURKISH, _Run

        run = object.__new__(_Run)
        run.model = SimpleNamespace(
            turkish=object(), writer=lambda language: "cohere" if language == "English" else None
        )
        for language, expected in (
            ("", True),
            ("English", False),
            (TURKISH, False),
            ("Hindi", True),
        ):
            run.language = language
            self.assertIs(run.qwen_writes(), expected, language)

    def test_a_split_piece_loses_the_full_stop_a_model_ends_it_with(self):
        from services.captions.worker import unfinished

        self.assertEqual(
            unfinished("Rising food and fuel prices have been."),
            "Rising food and fuel prices have been",
        )
        self.assertEqual(unfinished("Is it over?"), "Is it over?")
        self.assertEqual(unfinished("And then..."), "And then...")
        self.assertEqual(unfinished("通货膨胀是选民最关心的议题。"), "通货膨胀是选民最关心的议题")
        self.assertEqual(unfinished("No stop here"), "No stop here")

    def test_a_comma_long_pause_no_longer_ends_a_piece(self):
        # 2.9.3: a 0.35 s pause ended a piece, and the models heard sentences in scraps.
        cutter = self.cutter(lambda t: 1.0 <= t < 3.0 or 3.4 <= t < 5.0)
        pieces = self.push(cutter, 7.0)
        self.assertEqual(len(pieces), 1)

    def voiced_cutter(self, voiced):
        """A cutter whose detector holds "speech" all along, and whose detector without the
        hold hears a voice where `voiced(t)` says (2.9.5)."""
        clock = {"t": 0.0}

        def is_voiced(window):
            said = voiced(clock["t"])
            clock["t"] += WINDOW / RATE
            return said

        return Cutter(lambda window: True, is_voiced)

    def test_our_moonshines_turkish_is_read_in_whole_utterances(self):
        # 2026-11-07, the owner: right after a tune its 2 s pieces lost "gün" and "iyi"; its
        # Turkish goes to the growing writer instead (no forced cuts), the rest as before
        import queue

        from services.captions.growing import GrowingWriter
        from services.captions.worker import TURKISH, _Run

        for moonshine, language, growing in (
            (True, TURKISH, True),
            (False, TURKISH, False),
            (True, "English", False),
        ):
            run = object.__new__(_Run)
            run.stopped, run.writer, run.blocks = threading.Event(), None, queue.Queue()
            run.model = SimpleNamespace(
                moonshine=moonshine,
                is_speech=lambda window: False,
                is_voiced=lambda window: False,
            )
            run.language, run.viewer, run.rtf = language, [100.0], 0.5
            run.cutter = Cutter(lambda window: False)
            with mock.patch("services.captions.worker.threading.Thread"):
                run._maybe_stream()
            self.assertEqual(isinstance(run.writer, GrowingWriter), growing, language)
            run._take(101.0, array("f", bytes(4 * 1600)))  # the picture 1 s behind: a small lead
            if growing:
                self.assertEqual(run.blocks.qsize(), 1)  # the sound goes to it whole
            else:
                self.assertEqual(run.cutter.max_s, 2.0)  # short pieces, as the lead allows

    def test_a_piece_that_reached_its_length_is_cut_at_its_longest_pause(self):
        # 2.9.5: the held detector showed no pause between words: the cut fell anywhere.
        cutter = self.voiced_cutter(lambda t: not (2.5 <= t < 2.6 or 3.2 <= t < 3.45))
        cutter.set_max(4.0)
        pieces = self.push(cutter, 6.0) + cutter.flush()
        self.assertTrue(pieces[0].split)
        self.assertAlmostEqual(pieces[0].end, 3.33, delta=0.07)  # inside the longer pause
        self.assertAlmostEqual(pieces[1].start, pieces[0].end)

    def test_a_phrase_pause_late_in_a_piece_ends_it_there(self):
        cutter = self.voiced_cutter(lambda t: not (1.0 <= t < 1.4 or 3.0 <= t < 3.4))
        cutter.set_max(4.0)
        pieces = self.push(cutter, 6.0) + cutter.flush()
        self.assertFalse(pieces[0].split)  # at a pause: the model's full stop stays
        self.assertAlmostEqual(pieces[0].end, 3.25, delta=0.07)  # not at the early pause
        self.assertGreaterEqual(pieces[1].start, pieces[0].end)  # the next one after it
        self.assertLess(pieces[1].start, 3.45)

    def test_a_gap_in_the_sound_closes_the_open_piece(self):
        cutter = self.cutter(lambda t: True)
        pieces = self.push(cutter, 1.0) + self.push(cutter, 1.0, start=10.0)
        self.assertEqual(len(pieces), 1)
        self.assertLess(pieces[0].end, 1.01)


class DecoderTests(unittest.TestCase):
    """QiTV's own libVLC decodes a stream's sound as fast as its bytes come."""

    def test_the_fixtures_sound_decodes_at_16_khz_on_its_timeline(self):
        from services.vlc_bundle import use_bundled_vlc

        use_bundled_vlc()
        from services.captions.decoder import AudioDecoder

        data = FIXTURE.read_bytes()
        times = TsTimes()
        clock = StreamClock(50.0, times)
        placed = []
        decoder = AudioDecoder(
            lambda us, pcm: placed.append((clock.place(us, len(pcm) // 2, RATE), len(pcm)))
        )
        decoder.start()
        try:
            for i in range(0, len(data), 64 * 1024):
                times.feed(data[i : i + 64 * 1024])
                decoder.feed(data[i : i + 64 * 1024])
            decoder.end()
            deadline = time.monotonic() + 20
            while not decoder.finished() and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            decoder.stop()
        seconds = sum(n for _, n in placed) / 2 / RATE
        self.assertAlmostEqual(seconds, 12.0, delta=0.3)
        starts = [at for at, _ in placed if at is not None]
        self.assertEqual(len(starts), len(placed))
        self.assertAlmostEqual(
            starts[0], 50.0 + pts_seconds(times.first_audio_pts, times.first_video_pts), delta=0.01
        )
        self.assertAlmostEqual(starts[-1], 50.0 + 12.0, delta=0.3)


class _Live:
    def __init__(self):
        self.position_s = 0.0
        self.floor = 0.0
        self.source = ("http://127.0.0.1:1/t/s1/side/3.ts", 3, 0.0)

    def position(self):
        return self.position_s

    def caption_source(self, after=None):
        self.asked_after = after
        if after is not None:
            return ("http://127.0.0.1:1/t/s1/side/9.ts", 9, after + 0.4)
        return self.source

    def set_caption_floor(self, seconds, *, shift=True, whole=False):
        self.floor, self.shifted, self.whole = seconds, shift, whole

    def reach_caption_floor(self):
        self.reaches = getattr(self, "reaches", 0) + 1


class _Overlay:
    def __init__(self):
        self.text = ""

    def show_text(self, text):
        self.text = text

    def clear(self):
        self.text = ""


class ControllerTimingTests(unittest.TestCase):
    """When the window shows a caption, with the process and the session faked."""

    def setUp(self):
        from PySide6.QtWidgets import QApplication, QWidget

        from ui.captions_controller import CaptionsController

        self.app = QApplication.instance() or QApplication([])
        self.window = QWidget()
        self.window.live_session = _Live()
        self.overlay = _Overlay()
        self.captions = CaptionsController(self.window, overlay=self.overlay)
        self.sent = []
        self.captions._process.send = lambda *m: self.sent.append(m)
        self.captions._process.running = lambda: True
        self.captions._enabled = self.captions._ready = True
        self.captions._start_run()

    def at(self, position):
        self.window.live_session.position_s = position
        self.captions._last = None
        self.captions._tick()
        return self.overlay.text

    def test_a_caption_shows_from_its_words_start_for_what_they_last(self):
        run = self.captions._run
        self.assertEqual(self.sent[0][:4], ("start", run, "http://127.0.0.1:1/t/s1/side/3.ts", 0.0))
        self.captions._on_caption(run, 10.0, 12.0, "Merhaba")
        self.captions._on_caption(run, 12.5, 13.0, "Nasılsın")  # after a pause
        self.assertEqual(self.at(10.2), "")  # the piece's lead-in: not the words yet
        self.assertEqual(self.at(10.35), "Merhaba")  # its first word
        self.assertEqual(self.at(12.75), "Merhaba")  # on until the next line's first word
        self.assertEqual(self.at(12.85), "Nasılsın")
        self.assertEqual(self.at(13.9), "Nasılsın")  # held long enough to read
        self.assertEqual(self.at(14.1), "")

    def test_at_2x_a_caption_stays_as_long_for_the_viewer_to_read(self):
        # 2026-11-09, the owner: buffered channels watched at 2x
        run = self.captions._run
        self.captions._on_caption(run, 10.0, 11.0, "Nasılsın")
        self.assertEqual(self.at(10.35), "Nasılsın")
        self.assertEqual(self.at(11.55), "Nasılsın")  # 1x: 0.6 s past its words
        self.assertEqual(self.at(11.65), "")
        self.captions._forget_cues()
        self.captions._rate, self.captions._rate_from = 2.0, None  # (as measured: the next test)
        self.captions._on_caption(run, 20.0, 21.0, "Nasılsın")
        self.assertEqual(self.at(22.6), "Nasılsın")  # 2.4 s of the picture's: 1.2 s the viewer's
        self.assertEqual(self.at(22.8), "")
        self.captions._rate = 1.05  # stay-live's gentle steering: as at 1x
        self.assertEqual(self.captions._scale(), 1.0)
        self.captions._rate = 3.0
        self.assertEqual(self.captions._scale(), 2.0)  # (at most RATE_CAP)

    def test_the_picture_s_rate_is_measured_from_its_position(self):
        self.captions._rate_from = None
        for second in range(5):
            self.captions._measure_rate(100.0 + 2.0 * second, 1000.0 + second)
        self.assertAlmostEqual(self.captions._rate, 2.0, delta=0.1)
        self.captions._measure_rate(500.0, 1006.0)  # a seek: not a rate
        self.assertAlmostEqual(self.captions._rate, 2.0, delta=0.1)

    def test_the_end_of_a_line_cut_off_on_its_own_joins_it(self):
        # 2.9.5, the owner: a sentence's last words came alone ("ötesine geçiyor").
        run = self.captions._run
        self.captions._on_caption(run, 10.0, 16.0, "Uzay aracı bu yıl ilk kez Mars'ın")
        self.captions._on_caption(run, 16.0, 17.2, "ötesine geçiyor.")  # cut, no pause
        joined = "Uzay aracı bu yıl ilk kez Mars'ın ötesine geçiyor."
        self.assertEqual(self.captions._cues, [(10.0, 17.2, joined)])
        self.assertEqual(self.at(16.5), joined)
        self.captions._on_caption(run, 16.0, 17.2, "ötesine geçiyor.")  # read again
        self.assertEqual(self.captions._cues, [(10.0, 17.2, joined)])  # not shown twice

    def test_a_caption_that_went_does_not_come_back_when_the_next_comes_late(self):
        # 2.9.6, the real-use review: cleared, then on again for 0.25 s when a late line moved
        # its end to the late line's start.
        run = self.captions._run
        self.captions._on_caption(run, 10.0, 12.0, "Merhaba")
        self.assertEqual(self.at(12.5), "Merhaba")
        self.assertEqual(self.at(12.7), "")  # its time is over
        self.captions._on_caption(run, 13.0, 14.0, "Nasılsın")  # late
        self.assertEqual(self.at(12.8), "")  # not on again
        self.assertEqual(self.at(13.4), "Nasılsın")

    def rewind_to(self, position):
        """The picture goes back to `position` as it plays (a seek), then plays on a tick."""
        self.captions._last = (self.window.live_session.position_s, time.monotonic())
        self.window.live_session.position_s = position
        self.captions._tick()  # the seek
        self.captions._tick()
        return self.overlay.text

    def test_a_rewind_shows_the_lines_the_run_read_again(self):
        # 2.13.4 (bug scan #14): a line whose time on screen had ended never showed again
        run = self.captions._run
        self.captions._on_caption(run, 10.0, 12.0, "Merhaba")
        self.captions._on_progress(run, 30.0, 0.3)  # read on to 30 s
        self.assertEqual(self.at(10.5), "Merhaba")
        self.assertEqual(self.at(20.0), "")  # its time is over
        self.assertEqual(self.rewind_to(10.5), "Merhaba")
        self.assertEqual(self.captions._run, run)  # read already: no new run

    def test_a_rewind_before_the_run_shows_what_the_new_run_writes(self):
        self.window.live_session.source = ("http://127.0.0.1:1/t/s1/side/3.ts", 3, 50.0)
        self.captions._start_run()  # a run that reads from 50 s
        run = self.captions._run
        self.captions._on_caption(run, 60.0, 62.0, "Merhaba")
        self.assertEqual(self.at(60.5), "Merhaba")
        self.window.live_session.position_s = 70.0  # its time is over
        self.assertEqual(self.at(70.0), "")
        self.rewind_to(35.5)
        self.assertEqual(self.captions._run, run + 1)  # read again from there
        self.captions._on_caption(run + 1, 35.0, 37.0, "Selam")
        self.captions._tick()
        self.assertEqual(self.overlay.text, "Selam")

    def test_a_late_end_does_not_join_a_line_already_on_screen(self):
        run = self.captions._run
        first = "Uzay aracı bu yıl ilk kez Mars'ın"
        self.captions._on_caption(run, 10.0, 16.0, first)
        self.assertEqual(self.at(11.0), first)  # on screen
        self.captions._on_caption(run, 16.0, 17.2, "ötesine geçiyor.")
        self.assertEqual(len(self.captions._cues), 2)  # its own line: nothing wraps again
        self.assertEqual(self.at(12.0), first)
        self.assertEqual(self.at(16.5), "ötesine geçiyor.")

    def test_an_end_that_adds_no_part_joins_a_caption_in_parts(self):
        self.overlay.pages = lambda text: [text[i : i + 40] for i in range(0, len(text), 40)]
        run = self.captions._run
        before = "x" * 60  # two parts, the second half empty
        self.captions._on_caption(run, 10.0, 16.0, before)
        self.captions._on_caption(run, 16.0, 19.0, "bunlar netlik")  # 3 s: within 3.5 s
        self.assertEqual(self.captions._cues, [(10.0, 19.0, before + " bunlar netlik")])

    def test_a_line_joins_the_one_before_only_when_both_fit_and_nothing_parts_them(self):
        self.overlay.pages = lambda text: [text] if len(text) <= 40 else [text[:40], text[40:]]
        run = self.captions._run
        self.captions._on_caption(run, 10.0, 16.0, "Uzay aracı bu yıl ilk kez Mars'ın")
        self.captions._on_caption(run, 16.0, 17.2, "ötesine geçiyor.")  # two lines' worth
        self.captions._on_caption(run, 20.0, 22.0, "Yeni bir cümle")
        self.captions._on_caption(run, 22.8, 23.5, "başlıyor.")  # after a pause
        self.assertEqual(len(self.captions._cues), 4)

    def test_a_long_line_is_read_before_the_next_by_at_most_a_second(self):
        # 2.9.5, the owner: "superseded ... not giving time to read".
        run = self.captions._run
        first = "Beyaz saray seçim sonuçlarını bekliyor, İran politikasında belirsizlik var."
        second = "Dışişleri bakanı bugün yaptığı açıklamada görüşmelerin süreceğini söyledi."
        self.captions._on_caption(run, 10.0, 13.0, first)  # 75 letters in 3 s
        self.captions._on_caption(run, 13.6, 17.0, second)
        self.captions._on_caption(run, 30.0, 31.0, "Sonra.")
        self.assertEqual(self.at(14.5), first)  # its words gone, still being read
        self.assertEqual(self.at(14.95), second)  # 1 s after its first word at most
        self.assertEqual(self.at(30.35), "Sonra.")  # after a pause: on time again

    def test_a_long_caption_shows_in_parts_across_its_words(self):
        # 2.9.1: the overlay's parts (pages), each for its share of the words by its letters.
        self.overlay.pages = lambda text: text.split(" | ")
        run = self.captions._run
        self.captions._on_caption(run, 10.0, 16.0, "one two three | four five six")
        self.assertEqual(self.at(10.3), "one two three")
        self.assertEqual(self.at(15.0), "four five six")  # past the first part's share
        self.assertEqual(self.at(16.4), "four five six")  # held after the words

    def test_a_caption_shows_from_the_first_word_its_piece_leads_in_to(self):
        from services.captions.pieces import LEAD_IN_S
        from ui.captions_controller import FIRST_WORD_S

        self.assertEqual(FIRST_WORD_S, LEAD_IN_S)

    def test_a_short_pause_keeps_the_line_on_until_the_next_a_long_one_for_its_reading(self):
        # 2.9, the owner: the lines went before the next came ("hard to read").
        run = self.captions._run
        long_line = "Beyaz saray seçim sonuçlarını bekliyor, İran politikasında belirsizlik var."
        self.captions._on_caption(run, 50.0, 52.0, "Kısa.")
        self.captions._on_caption(run, 54.5, 56.0, long_line)  # a 2.8 s pause before it
        self.captions._on_caption(run, 80.0, 81.0, "Sonra.")  # 24 s of music before it
        self.assertEqual(self.at(54.7), "Kısa.")  # bridged
        self.assertEqual(self.at(54.9), long_line)
        reading = 54.8 + len(long_line) / 15.0  # longer than its words and the hold
        self.assertEqual(self.at(reading - 0.1), long_line)
        self.assertEqual(self.at(reading + 0.1), "")  # not on over the music
        self.assertEqual(self.at(80.35), "Sonra.")

    def test_the_distance_follows_the_large_picture_after_a_swap(self):
        import types

        main, other = self.window.live_session, _Live()
        live_target = types.SimpleNamespace(live=True, source="p", title="ch", item={})
        controller = types.SimpleNamespace(live=main, now_playing=lambda: live_target)
        self.window.controller = controller
        self.captions._floored = None
        self.captions._tier = "small"
        self.captions._hold_floor()
        self.assertEqual(main.floor, 8.0)
        controller.live = other  # swapped: the other picture is large now
        self.captions._tick()
        self.assertEqual((main.floor, main.shifted), (0.0, False))  # steered back, not shifted
        self.assertEqual(other.floor, 8.0)
        self.captions._drop_floor()
        self.assertEqual((other.floor, other.shifted), (0.0, True))  # off: it stays where it is

    def test_while_the_picture_waits_for_the_distance_it_says_starting_captions(self):
        run = self.captions._run
        self.captions._on_caption(run, 10.0, 12.0, "Merhaba")
        changed = []
        self.captions.changed.connect(lambda: changed.append(True))
        self.window.live_session.caption_waiting = lambda: True
        self.assertEqual(self.at(10.5), "Starting captions…")
        self.assertEqual(self.captions.status_text(), "Starting captions…")
        self.assertEqual(changed, [True])
        self.window.live_session.caption_waiting = lambda: False
        self.assertEqual(self.at(10.5), "Merhaba")
        self.assertEqual(changed, [True, True])

    def _load(self):
        """The model loading, its process faked."""
        self.captions._loading, self.captions._ready = True, False

    def _turned_on_loading(self):
        """Captions turned on, their model loading."""
        self.captions._enabled = self.captions._ready = False
        self.enterContext(mock.patch.object(self.captions._process, "running", lambda: False))
        self.enterContext(mock.patch.object(self.captions, "_load_model", self._load))
        self.captions._enable("small")

    def test_starting_captions_shows_on_the_picture_until_their_first_words_are_written(self):
        # 2.10.1: the picture no longer waits for the model, only for their distance; it plays
        # on, saying so while the model loads and until the first run has written past it. The
        # row says "Preparing…" while it loads, as before.
        self._turned_on_loading()
        self.assertEqual(self.at(100.0), "Starting captions…")
        self.assertEqual(self.captions.status_text(), "Preparing…")
        self.captions._on_ready()
        run = self.captions._run
        self.captions._on_caption(run, 100.0, 103.0, "Merhaba")
        self.captions._on_progress(run, 101.0, 0.2, 101.0)
        self.assertEqual(self.at(100.5), "Starting captions…")  # written 0.5 s past it
        self.captions._on_progress(run, 103.0, 0.2, 103.0)
        self.assertEqual(self.at(100.5), "Merhaba")  # 2.5 s past it
        self.assertEqual(self.captions.status_text(), "")
        self.assertEqual(self.at(100.6), "Merhaba")

    def test_a_first_run_that_writes_nothing_says_starting_captions_not_for_long(self):
        from ui.captions_controller import START_WAIT_S

        self._turned_on_loading()
        self.captions._on_ready()
        self.assertEqual(self.at(5.0), "Starting captions…")
        assert self.captions._starting is not None
        self.captions._starting -= START_WAIT_S  # ready that long ago, nothing written
        self.assertEqual(self.at(5.0), "")

    def test_a_model_loaded_again_while_on_keeps_showing_what_was_written(self):
        # translation turned on (or Automatic's step down): the captions written stay and show
        run = self.captions._run
        self.captions._on_caption(run, 10.0, 12.0, "Merhaba")
        self.enterContext(mock.patch.object(self.captions._process, "stop", lambda: None))
        self.enterContext(mock.patch.object(self.captions, "_load_model", self._load))
        self.captions._switch("small", stepped=False)
        self.assertEqual(self.at(10.5), "Merhaba")

    def test_a_caption_from_a_run_before_is_not_shown(self):
        self.captions._on_caption(self.captions._run - 1, 10.0, 12.0, "old")
        self.assertEqual(self.at(10.5), "")

    def test_a_late_caption_shows_for_what_is_left_and_a_too_late_one_never(self):
        run = self.captions._run
        self.window.live_session.position_s = 11.0
        self.captions._on_caption(run, 10.0, 12.0, "late")
        self.assertEqual(self.at(11.0), "late")
        self.captions._on_caption(run, 3.0, 5.0, "gone")
        self.assertEqual(self.at(11.5), "late")

    def test_another_session_reads_again_and_forgets_the_channel_before(self):
        run = self.captions._run
        self.captions._on_caption(run, 10.0, 12.0, "one")
        self.window.live_session.source = ("http://127.0.0.1:1/t/s2/side/0.ts", 0, 0.0)
        self.at(10.5)
        self.assertEqual(self.captions._run, run + 1)
        self.assertEqual(self.captions._cues, [])

    def test_a_seek_past_what_was_read_reads_again_from_there(self):
        run = self.captions._run
        self.captions._on_progress(run, 20.0, 0.3)
        self.window.live_session.position_s = 10.0
        self.captions._last = (10.0, time.monotonic())
        self.window.live_session.position_s = 60.0
        self.captions._tick()
        self.assertEqual(self.captions._run, run + 1)

    def test_a_model_that_did_not_load_leaves_no_child_behind(self):
        stopped = []
        self.captions._process.stop = lambda: stopped.append(True)
        self.captions._on_failed("ModuleNotFoundError")
        self.assertEqual(stopped, [True])
        self.assertFalse(self.captions.enabled())

    def test_catching_up_only_when_what_was_heard_is_about_to_fall_behind(self):
        # 2.9: a film's 1.7B captions ran 3-6 s ahead, all in time, and the row said
        # "Catching up…" at every dip under the 6 s they start with.
        self.window.live_session.position_s = 100.0
        for newest, said in ((106.0, ""), (103.0, ""), (101.2, ""), (100.6, "Catching up…")):
            self.captions._newest = self.captions._written = newest
            self.assertEqual(self.captions.status_text(), said, newest)

    def test_cant_keep_up_only_when_slow_and_late(self):
        import time as clock

        run = self.captions._run
        self.window.live_session.position_s = 100.0
        self.captions._on_progress(run, 140.0, 1.06)  # slow lines, but far ahead: in time
        self.assertIsNone(self.captions._slow_since)
        self.captions._on_progress(run, 100.5, 1.06)  # slow and about to be late
        self.assertIsNotNone(self.captions._slow_since)
        self.captions._slow_since = clock.monotonic() - 31.0
        self.assertEqual(self.captions.status_text(), "Can't keep up")
        self.captions._on_progress(run, 108.0, 1.06)  # ahead again
        self.assertIsNone(self.captions._slow_since)

    def test_a_model_with_sound_waiting_for_it_cant_keep_up_whatever_its_speed(self):
        # 2.9.1, the owner on live: the 1.7B at 0.85 wrote each line after its words; none
        # showed and Automatic never stepped down (the speed under 0.9 said "in time").
        run = self.captions._run
        self.window.live_session.position_s = 100.0
        self.captions._on_progress(run, 110.0, 0.85, 99.6)  # 10 s read, written to 99.6
        self.assertIsNotNone(self.captions._slow_since)
        since = self.captions._slow_since
        self.captions._on_progress(run, 110.0, 0.5, 100.4)  # its speed dips: still late
        self.assertEqual(self.captions._slow_since, since)
        self.captions._on_progress(run, 100.8, 0.5, 100.8)  # nothing waits, 0.8 s ahead
        self.assertEqual(self.captions._slow_since, since)  # not in time yet: kept
        self.captions._on_progress(run, 110.0, 0.5, 104.0)  # in time again
        self.assertIsNone(self.captions._slow_since)

    def test_a_stalled_edge_with_nothing_waiting_is_no_slow_model(self):
        run = self.captions._run
        self.window.live_session.position_s = 100.0
        self.captions._on_progress(run, 100.4, 0.3, 100.4)  # nothing read past it, a fast model
        self.assertIsNone(self.captions._slow_since)

    def test_late_is_how_far_the_captions_are_written_not_the_sound_read(self):
        # 2.9, the review: a model 1.2x slow on live: the sound read stayed 10 s ahead, the
        # captions fell behind it, and nothing said so.
        run = self.captions._run
        self.window.live_session.position_s = 100.0
        self.captions._on_progress(run, 110.0, 1.2, 100.3)  # read 10 s ahead, written to 100.3
        self.assertIsNotNone(self.captions._slow_since)
        self.assertEqual(self.captions.status_text(), "Catching up…")
        self.captions._slow_since = time.monotonic() - 31.0
        self.assertEqual(self.captions.status_text(), "Can't keep up")
        self.captions._on_progress(run, 110.0, 1.2, 109.0)  # caught up
        self.assertIsNone(self.captions._slow_since)
        self.assertEqual(self.captions.status_text(), "")

    def test_the_download_says_how_far_it_is_where_the_captions_will_be(self):
        # 2.9: answered, the question went and nothing said the download had begun.
        from services.captions.download import CANCELLED

        class Download:
            def running(self):
                return True

            def cancel(self):
                pass

        self.captions._enabled = False
        download = self.captions._download = Download()
        self.captions._on_download_progress(download, 12, 100)
        self.assertEqual(self.overlay.text, "Downloading 12%")
        self.captions._on_download_preparing(download)
        self.assertEqual(self.overlay.text, "Preparing…")
        self.captions._cancel_download()
        self.assertEqual(self.overlay.text, "")
        again = self.captions._download = Download()
        self.captions._unpacking = False  # as a new answer's download begins
        self.captions._on_download_progress(again, 50, 100)
        self.assertEqual(self.overlay.text, "Downloading 50%")
        self.captions._on_download_failed(again, CANCELLED)
        self.assertEqual(self.overlay.text, "")

    def test_a_cancelled_downloads_late_answer_changes_nothing(self):
        from services.captions.download import CANCELLED

        class Download:
            def running(self):
                return False

        old, new = Download(), Download()
        toasts = []
        self.captions._toast = lambda text, kind="info": toasts.append(text)
        self.captions._enabled = False
        self.captions._leaving_download, self.captions._download = old, new
        self.captions._on_download_failed(old, CANCELLED)
        self.captions._on_downloaded(old)
        self.assertIs(self.captions._download, new)
        self.assertFalse(self.captions.enabled())
        self.assertEqual(toasts, [])
        self.captions._on_download_failed(new, CANCELLED)  # the viewer's own cancel: no toast
        self.assertEqual(toasts, [])

    def test_after_a_jump_it_reads_again_past_it_not_before(self):
        run = self.captions._run
        self.sent.clear()
        self.captions._on_resync(run, 20.0)
        self.captions._start_run()
        self.assertEqual(self.window.live_session.asked_after, 20.0)
        self.assertEqual(self.sent[0][2], "http://127.0.0.1:1/t/s1/side/9.ts")
        self.assertTrue(self.sent[0][7])  # and reads on past the next steps

    def test_a_jumpy_clock_reads_again_once_and_never_from_the_same_second(self):
        # 2026-11-09, the owner's channel: 25 runs in 27 s, each from 2422.0 s
        live = self.window.live_session
        self.captions._run_start = 2422.0
        self.captions._on_resync(self.captions._run, 2422.0)  # it placed nothing
        self.captions._start_run()
        self.assertGreater(live.asked_after, 2422.0)  # the next segment, not the same one

    def test_a_jump_s_place_is_its_channel_s_alone(self):
        # 2026-11-09: past the old channel's jump (2422 s) was waited for on the new one
        from ui.captions_controller import RESYNC_TRIES

        live = self.window.live_session
        self.captions._on_resync(self.captions._run, 2422.0)
        live.caption_source = lambda after=None: None if after is not None else live.source
        for _ in range(RESYNC_TRIES):
            self.captions._start_run()  # (its restart timer, each second)
        self.sent.clear()
        self.captions._start_run()  # then from the picture
        self.assertEqual(self.sent[0][:2], ("start", self.captions._run))
        self.captions._on_resync(self.captions._run, 2422.0)
        self.captions._session_key = "another channel"
        self.captions._slow_since = 1.0
        loads: list = []
        captions: Any = self.captions
        captions._process.start = lambda *args, **kwargs: loads.append(args)
        self.sent.clear()
        self.captions._tick()  # a new tune: its own picture, its speed judged anew
        self.assertEqual(self.sent[0][0], "start")
        self.assertIsNone(self.captions._slow_since)  # (no "Can't keep up" left from before)
        self.assertFalse(self.sent[0][7])
        self.assertEqual(loads, [])  # the models stay loaded: only a new run

    def test_a_run_that_hears_nothing_reads_again_from_the_picture_twice_at_most(self):
        import time as clock

        from ui.captions_controller import NO_SOUND_S

        self.captions._last = (0.0, clock.monotonic())
        for expected in (True, True, False):
            self.sent.clear()
            run = self.captions._run
            self.captions._on_progress(run, self.captions._run_start, 0.5)  # no sound yet
            self.captions._run_began = clock.monotonic() - NO_SOUND_S - 1
            self.captions._last = None
            self.captions._tick()
            started = any(m[0] == "start" for m in self.sent)
            self.assertEqual(started, expected)

    def test_a_language_the_pieces_agreed_on_is_the_runs_channel(self):
        self.captions._run_channel = "provider|A"
        self.captions._on_detected(self.captions._run, "Turkish")
        self.assertEqual(self.captions._detected, {"provider|A": "Turkish"})

    def test_an_ended_run_starts_again_once_and_keeps_its_captions(self):
        run = self.captions._run
        self.captions._on_caption(run, 10.0, 12.0, "kept")
        self.captions._on_ended(run, "TimeoutError")
        self.at(10.5)  # the next tick: the same session, nothing cleared
        self.assertEqual(self.captions._cues, [(10.0, 12.0, "kept")])
        self.assertEqual(self.captions._run, run)
        self.captions._start_run()  # the restart
        self.assertEqual(self.captions._run, run + 1)
        self.assertFalse(self.captions._restart.isActive())

    def test_off_gives_the_picture_back_and_clears_the_screen(self):
        self.captions._on_caption(self.captions._run, 10.0, 12.0, "x")
        self.at(10.5)
        self.captions._process.stop = lambda: None
        self.captions._disable()
        self.assertEqual(self.overlay.text, "")
        self.assertEqual(self.window.live_session.floor, 0.0)


def worker_said(run, shown, start, end, language="Turkish", target="English"):
    """A line that went out translated (services.captions.worker._Said), for the next one."""
    from services.captions.worker import _Said

    return _Said(run, language, target, start, end, shown, f"<{target}> {shown}")


class _Card:
    def __init__(self):
        from unittest.mock import Mock

        self.answered = Mock()
        self.asked, self.withdrawn = [], 0

    def ask(self, text, yes, no):
        self.asked.append((text, yes, no))

    def withdraw(self):
        self.withdrawn += 1


class StepDownTests(unittest.TestCase):
    """2.9 (the owner's call): Automatic's 1.7B that can't keep up steps down to the 0.6B while
    QiTV runs, its download's question first (once) when it isn't here."""

    def setUp(self):
        from PySide6.QtWidgets import QApplication, QWidget

        from ui.captions_controller import CaptionsController

        self.app = QApplication.instance() or QApplication([])
        self.window = QWidget()
        self.window.live_session = _Live()
        self.window.live_session.position_s = 100.0
        self.overlay, self.card = _Overlay(), _Card()
        self.captions = CaptionsController(self.window, card=self.card, overlay=self.overlay)
        process = self.captions._process
        self.sent, self.loaded, self.stopped = [], [], []
        process.send = lambda *m: self.sent.append(m)
        process.running = lambda: True
        process.stop = lambda: self.stopped.append(True)
        process.start = lambda load, detailed_log=False: self.loaded.append(load)
        self.config = {}
        self.captions._config_get = lambda name, default: self.config.get(name, default)
        self.captions._config_set = self.config.__setitem__
        self.disk = {
            "small": True,
            "large": True,
            "turkish": True,
            "turkish_lm": True,  # 2.12.1: the Turkish model's LM
            "european": True,
            "cohere": True,
        }
        self.captions._here = lambda name: self.disk[name]
        self.toasts = []
        self.captions._toast = lambda text, kind="info": self.toasts.append(text)
        self.captions._tier = "large"
        self.captions._enabled = self.captions._ready = True
        self.captions._start_run()
        self.captions._on_caption(self.captions._run, 99.0, 101.0, "written")

    def fall_behind(self):
        import time as clock

        self.captions._on_progress(self.captions._run, 100.5, 1.06)  # slow, about to be late
        self.captions._slow_since = clock.monotonic() - 31.0
        self.captions._last = None
        self.captions._tick()

    def test_behind_it_switches_at_once_and_keeps_what_it_wrote(self):
        self.captions._slow_since = time.monotonic() - 10.0
        self.captions._tick()  # slow for a while only: the 1.7B goes on
        self.assertEqual((self.loaded, self.stopped), ([], []))
        self.fall_behind()
        self.assertEqual(self.stopped, [True])
        [load] = self.loaded
        self.assertIn("qwen3-asr-0.6b", load["model"]["encoder"])
        self.assertEqual(self.captions._tier, "small")
        self.assertEqual(
            self.toasts, ["AI captions switched to the faster speech model to keep up"]
        )
        self.assertEqual(self.captions.model_text(), "Automatic (0.6B)")
        self.assertEqual(self.captions.status_text(), "Preparing…")  # not "Can't keep up"
        self.assertEqual(self.overlay.text, "written")  # shown on while the 0.6B loads
        self.assertEqual(self.window.live_session.floor, 10.0)  # held as it was: no new wait
        run = self.captions._run
        self.captions._on_ready()
        self.assertEqual(self.captions._run, run + 1)  # it reads on from the picture
        self.assertEqual(self.captions._cues, [(99.0, 101.0, "written")])
        self.fall_behind()  # the 0.6B can't either: nothing smaller, it says so
        self.assertEqual(len(self.loaded), 1)
        self.assertEqual(self.captions.status_text(), "Can't keep up")
        self.captions._disable()
        self.assertEqual(self.captions._auto_tier(), "small")  # while QiTV runs

    def test_a_languages_own_model_not_here_is_asked_for_once_then_loaded(self):
        # 2.9.6, the owner: all 3.3-4.8 GB came at once, and the question's size looked random.
        from unittest import mock

        self.disk["turkish"] = False
        self.captions._language = ("Turkish", "")
        with mock.patch("services.captions.models.download_size", return_value=278_000_000):
            self.captions._offer_specialist()
            self.captions._offer_specialist()  # once while QiTV runs
        [(text, _yes, _no)] = self.card.asked
        self.assertIn("278 MB", text)
        self.assertTrue(self.captions.asked_unprompted())  # every phone hears it too
        started = []
        self.captions._start_download = lambda: started.append(self.captions._fetching)
        self.captions.answer(True)
        self.assertEqual(started, ["turkish"])
        download = object()
        self.captions._download = download
        self.disk["turkish"] = True
        self.captions._on_downloaded(download)
        self.assertEqual(self.stopped, [True])  # the captions process again ...
        self.assertTrue(
            self.loaded[-1]["turkish"].endswith("qitv-asr-tr-moonshine-mid")
        )  # ... with it
        self.assertEqual(self.captions._tier, "large")  # the same Qwen

    def test_after_the_first_questions_yes_a_languages_model_comes_unasked(self):
        # 2.9.6, the owner: say once what may come (880 MB to start, up to 3.3 GB), then each
        # language's model downloads as its language plays.
        from unittest import mock

        self.config["ai_captions_language_models"] = True
        self.disk["cohere"] = False
        self.captions._language = ("English", "")
        started = []
        self.captions._start_download = lambda: started.append(self.captions._fetching)
        with mock.patch("services.captions.models.download_size", return_value=1_700_000_000):
            self.captions._offer_specialist()
        self.assertEqual((self.card.asked, started), ([], ["cohere"]))

    # ---- 2.12.1: the Turkish model's LM ------------------------------------------------------
    def lm_started(self) -> list:
        """The Turkish model here without its LM (an install from before 2.12.1), Turkish
        playing; the downloads started, by what they fetch."""
        self.disk["turkish_lm"] = False
        self.captions._language = ("Turkish", "")
        started: list = []
        self.captions._start_download = lambda: started.append(self.captions._fetching)
        return started

    def test_an_install_from_before_the_lm_fetches_it_unasked_once(self):
        from unittest import mock

        started = self.lm_started()
        with mock.patch(
            "services.captions.models.download_size", side_effect=AssertionError("asked")
        ):
            self.captions._offer_specialist()
            self.captions._offer_specialist()  # once while QiTV runs
        self.assertEqual((self.card.asked, started), ([], ["turkish_lm"]))  # no question
        download = object()
        self.captions._download = download
        self.disk["turkish_lm"] = True
        self.sent.clear()
        self.captions._on_downloaded(download)
        self.assertEqual((self.stopped, self.loaded), ([], []))  # no restart for it ...
        [told] = [message for message in self.sent if message[0] == "turkish_lm"]
        lm = os.path.join("qitv-asr-tr-moonshine-mid", "lm", "lm.int8.onnx")
        self.assertTrue(told[1].endswith(lm))  # ... read with it from the next piece
        self.assertEqual(self.toasts, [])

    def test_while_the_lm_downloads_the_captions_say_what_they_would_say(self):
        # the review: the Subtitles note and the phones said "Downloading N%" for the 39 MB
        started = self.lm_started()
        before = self.captions.status_text()
        self.captions._offer_specialist()
        self.assertEqual(started, ["turkish_lm"])
        self.captions._download = object()
        self.captions._percent = 40
        self.assertEqual(self.captions.status_text(), before)
        self.assertNotIn("Downloading", self.captions.status_text())
        self.assertFalse(self.captions.downloading())
        self.captions._fetching = "turkish"  # a language's own model: said, as before
        self.assertEqual(self.captions.status_text(), "Downloading 40%")
        self.assertTrue(self.captions.downloading())

    def test_the_lm_waits_while_another_question_shows_or_a_download_runs(self):
        started = self.lm_started()
        self.captions._asking = True
        self.captions._offer_specialist()
        self.captions._asking = False
        self.captions._download = object()
        self.captions._offer_specialist()
        self.assertEqual(started, [])
        self.captions._download = None
        self.captions._offer_specialist()  # the next start
        self.assertEqual(started, ["turkish_lm"])

    def test_an_lm_that_does_not_download_is_only_logged_and_not_fetched_again(self):
        started = self.lm_started()
        self.captions._offer_specialist()
        download = object()
        self.captions._download = download
        with self.assertLogs("ui.captions_controller", "WARNING"):
            self.captions._on_download_failed(download, "Download failed: 404 Client Error")
        self.assertEqual((self.toasts, self.stopped, self.card.asked), ([], [], []))
        self.captions._offer_specialist()
        self.assertEqual(started, ["turkish_lm"])  # not again while QiTV runs

    def test_the_load_names_the_lm_once_it_is_here_beside_the_model(self):
        self.captions._load_model()
        lm = os.path.join("qitv-asr-tr-moonshine-mid", "lm", "lm.int8.onnx")
        self.assertTrue(self.loaded[-1]["turkish_lm"].endswith(lm))
        self.disk["turkish_lm"] = False
        self.captions._load_model()
        self.assertIsNone(self.loaded[-1]["turkish_lm"])
        self.disk.update(turkish=False, turkish_lm=True)
        self.captions._load_model()
        self.assertEqual((self.loaded[-1]["turkish"], self.loaded[-1]["turkish_lm"]), (None, None))

    def test_a_turkish_download_whose_lm_failed_still_brings_the_model(self):
        # its own files came, then the LM failed: the model writes Turkish, not Qwen
        from unittest import mock

        started = self.lm_started()
        self.disk["turkish"] = False
        self.config["ai_captions_language_models"] = True
        with mock.patch("services.captions.models.download_size", return_value=317_000_000):
            self.captions._offer_specialist()
        self.assertEqual(started, ["turkish"])
        download = object()
        self.captions._download = download
        self.disk["turkish"] = True
        with self.assertLogs("ui.captions_controller", "WARNING"):
            self.captions._on_download_failed(download, "Download failed: 404 Client Error")
        self.assertEqual(self.toasts, [])
        self.assertEqual(self.stopped, [True])  # the captions process again, with the model
        self.assertTrue(self.loaded[-1]["turkish"].endswith("qitv-asr-tr-moonshine-mid"))
        self.assertIsNone(self.loaded[-1]["turkish_lm"])
        self.captions._offer_specialist()
        self.assertEqual(started, ["turkish"])  # its LM not again while QiTV runs

    def test_a_turkish_download_that_failed_before_the_model_came_says_so_as_before(self):
        self.disk.update(turkish=False, turkish_lm=False)
        self.captions._fetching = "turkish"
        download = object()
        self.captions._download = download
        self.captions._on_download_failed(download, "Download failed: 404 Client Error")
        self.assertEqual(self.toasts, ["Download failed: 404 Client Error"])
        self.assertEqual(self.stopped, [])

    def test_the_first_question_says_what_it_starts_with_and_what_it_may_come_to(self):
        from unittest import mock

        self.captions._enabled = False
        self.captions._process.running = lambda: False
        self.captions._speed_unknown = lambda: False
        self.captions._choose_tier = lambda: "small"
        with (
            mock.patch("services.captions.models.installed", return_value=False),
            mock.patch("services.captions.models.download_size", return_value=893_000_000),
            mock.patch("services.captions.models.full_size", return_value=3_330_000_000),
        ):
            self.captions.toggle()
        [(text, _yes, _no)] = self.card.asked
        self.assertIn("893 MB", text)
        self.assertIn("3.3 GB", text)
        self.captions._start_download = lambda: None
        self.captions.answer(True)
        self.assertIs(self.config.get("ai_captions_language_models"), True)

    def test_a_model_that_is_here_is_not_asked_for_and_one_not_here_is_not_given(self):
        from unittest import mock

        self.captions._language = ("English", "")
        self.disk["european"] = False
        with mock.patch("services.captions.models.download_size", return_value=1):
            self.captions._offer_specialist()  # Cohere writes English: it's here
        self.assertEqual(self.card.asked, [])
        self.captions._load_model()
        self.assertIsNone(self.loaded[-1]["european"])  # the process never looks for it
        self.assertTrue(self.loaded[-1]["cohere"])

    def test_while_another_model_writes_the_language_qwen_is_not_stepped_down(self):
        # 2.9.6, the real-use review: English is Cohere's; Qwen only wrote while Cohere loaded,
        # and stepping it down restarted the process: Cohere loaded again, 101 s to a caption.
        import time as clock

        self.captions._on_progress(self.captions._run, 100.5, 1.06, 100.5, False)
        self.captions._slow_since = clock.monotonic() - 31.0
        self.captions._last = None
        self.captions._tick()
        self.assertEqual((self.loaded, self.stopped), ([], []))
        self.assertEqual(self.captions.status_text(), "Can't keep up")  # it still says so

    def test_the_viewers_own_choice_never_changes(self):
        self.config["ai_captions_model"] = "large"
        self.fall_behind()
        self.assertEqual((self.loaded, self.card.asked), ([], []))
        self.assertEqual(self.captions.status_text(), "Can't keep up")

    def test_not_here_it_asks_once_and_not_now_leaves_the_1_7b(self):
        self.disk["small"] = False
        changed = []
        self.captions.changed.connect(lambda: changed.append(True))
        with mock.patch("services.captions.models.download_size", return_value=878_702_423):
            self.fall_behind()
        [(text, yes, no)] = self.card.asked
        self.assertIn("879 MB", text)
        self.assertEqual((yes, no), ("Download", "Not now"))
        self.assertTrue(self.captions.asked_unprompted())  # every phone hears it
        self.assertTrue(changed)
        self.captions._on_answer(False)
        self.assertFalse(self.captions.asking())
        self.fall_behind()
        self.assertEqual(len(self.card.asked), 1)  # once while QiTV runs
        self.assertEqual((self.loaded, self.captions._tier), ([], "large"))
        self.assertEqual(self.captions.status_text(), "Can't keep up")

    def test_download_said_yes_the_1_7b_captions_on_until_the_0_6b_is_here(self):
        self.disk["small"] = False
        started = []

        class Download:
            def running(self):
                return True

            def cancel(self):
                pass

        download = Download()

        def begin():
            started.append(self.captions._fetching)
            self.captions._download = download

        self.captions._begin_download = begin
        with mock.patch("services.captions.models.download_size", return_value=878_702_423):
            self.fall_behind()
        self.captions._on_answer(True)
        self.assertEqual(started, ["small"])
        self.assertTrue(self.captions.enabled())
        self.captions._on_download_progress(download, 40, 100)
        self.assertEqual(self.captions.status_text(), "Downloading 40%")  # the row says it
        self.captions._last = None
        self.captions._tick()
        self.assertEqual(self.overlay.text, "written")  # the picture shows the captions
        self.assertEqual(self.captions.model_text(), "Automatic (1.7B)")
        self.disk["small"] = True
        self.captions._on_downloaded(download)
        self.assertEqual(self.captions._tier, "small")
        self.assertEqual(len(self.loaded), 1)
        self.assertEqual(self.captions.model_text(), "Automatic (0.6B)")

    def test_a_model_chosen_while_it_downloads_stays(self):
        self.disk["small"] = False
        self.captions._begin_download = lambda: setattr(self.captions, "_download", object())
        with mock.patch("services.captions.models.download_size", return_value=878_702_423):
            self.fall_behind()
        self.captions._on_answer(True)
        self.config["ai_captions_model"] = "large"
        self.captions._on_downloaded(self.captions._download)
        self.assertEqual((self.captions._tier, self.loaded), ("large", []))

    def test_the_process_gone_while_it_asks_or_downloads_leaves_captions_off(self):
        # 2.9, the review: the download finished after "Couldn't start" and the 0.6B came on.
        self.disk["small"] = False
        cancelled = []

        class Download:
            def running(self):
                return True

            def cancel(self):
                cancelled.append(True)

        with mock.patch("services.captions.models.download_size", return_value=878_702_423):
            self.fall_behind()
        self.captions._on_failed("gone")
        self.assertEqual(self.card.withdrawn, 1)
        self.assertFalse(self.captions.asking())
        self.captions._enabled = True
        self.captions._download = Download()
        self.captions._on_failed("gone")
        self.assertEqual(cancelled, [True])
        self.assertIsNone(self.captions._download)
        self.assertFalse(self.captions.enabled())

    def test_turned_off_while_it_asks_the_question_goes(self):
        self.disk["small"] = False
        with mock.patch("services.captions.models.download_size", return_value=878_702_423):
            self.fall_behind()
        self.captions._disable()
        self.assertEqual(self.card.withdrawn, 1)
        self.assertFalse(self.captions.asking())


class _Target:
    def __init__(self, live, local=False, item=None):
        self.live, self.local, self.item = live, local, item or {}
        self.source, self.title = "films", "A film"


class _Controller:
    def __init__(self, target, session=None):
        self.target, self.session = target, session
        self.relayable = False  # a provider's film the film relay carries (TVController's)
        self.relays = 0

    def now_playing(self):
        return self.target

    def film_relay_wanted(self):
        return self.relayable

    def relay_film(self):
        self.relays += 1
        self.session.through = _Relay(self.session.url)
        return True


class _Relay:
    def __init__(self, source, followable=True):
        self.source, self.follower_url = source, "http://127.0.0.1:5000/t/follower.mkv"
        self.can_follow = followable
        self.stops = 0

    def followable(self):
        return self.can_follow

    def stop(self):
        self.stops += 1


class _Session:
    def __init__(self, url, ms):
        self.url, self.ms = url, ms
        self.through = None  # the film relay it plays through
        self.settled = True  # placed(): opened and where it resumes

    def time_ms(self):
        return self.ms

    def relay(self):
        return self.through

    def placed(self):
        return self.settled

    def audio_track(self):
        return 2

    def audio_tracks(self):
        return [(-1, "Disable"), (2, "Track 1")]


class _Catalogs:
    def __init__(self, kinds):
        self.kinds = kinds

    def provider(self, name):
        return {"type": self.kinds[name]}


class FilmCaptionsTests(unittest.TestCase):
    """A film's captions: its sound read a little ahead of the picture, on the film's clock."""

    def setUp(self):
        from PySide6.QtWidgets import QApplication, QWidget

        from ui.captions_controller import FILM_AHEAD_S, CaptionsController

        self.ahead = FILM_AHEAD_S
        self.app = QApplication.instance() or QApplication([])
        self.window = QWidget()
        self.window.controller = _Controller(_Target(live=False, local=True))
        self.window.session = _Session("file:///films/a.mkv", 120_000)
        self.window.catalogs = _Catalogs({"Server": "JELLYFIN", "Provider": "XTREAM"})
        self.overlay = _Overlay()
        self.captions = CaptionsController(self.window, overlay=self.overlay)
        self.sent = []
        self.captions._process.send = lambda *m: self.sent.append(m)
        self.captions._process.running = lambda: True
        self.captions._supported = True
        self.captions._enabled = self.captions._ready = True

    def test_a_film_from_the_disk_reads_from_a_little_past_the_picture(self):
        self.assertTrue(self.captions.available())
        self.captions._start_run()
        kind, run, location, start = self.sent[0][:4]
        self.assertEqual((kind, location), ("film", "file:///films/a.mkv"))
        self.assertAlmostEqual(start, 120.0 + self.ahead)
        self.assertEqual(self.sent[1], ("viewer", 120.0))
        self.captions._on_caption(run, 127.0, 129.0, "Bonjour")
        self.window.session.ms = 127_500
        self.captions._last = None
        self.captions._tick()
        self.assertEqual(self.overlay.text, "Bonjour")

    def test_another_audio_track_is_read_from_the_picture_in_its_own_language(self):
        # 2.9: the owner switched a film's sound from English to Turkish with captions on,
        # and they stayed English: the run read the first track, locked to its language.
        tracks = {2: "eng", 5: "tur"}
        session = self.window.session
        session.playing = 2
        session.audio_track = lambda: session.playing
        session.audio_tracks = lambda: [(-1, "Disable"), (2, "English"), (5, "Türkçe")]
        session.audio_track_info = lambda: [
            unittest.mock.Mock(id=i, language_code=code) for i, code in tracks.items()
        ]
        self.captions._resolve_language()
        self.captions._start_run()
        run = self.captions._run
        self.assertEqual(self.sent[0][4:6], (2, "English"))
        self.captions._on_caption(run, 123.0, 125.0, "Hello")
        self.captions._last = None
        self.captions._tick()
        self.assertEqual(self.captions._run, run)  # the same track: nothing read again
        session.playing = -1  # the player busy for a moment: no change seen
        self.captions._tick()
        self.assertEqual(self.captions._run, run)
        session.playing = 5
        self.sent.clear()
        self.captions._tick()
        self.assertEqual(self.captions._run, run + 1)
        kind, _run, _location, start, track, language = self.sent[0][:6]
        self.assertEqual((kind, track, language), ("film", 5, "Turkish"))
        self.assertAlmostEqual(start, 120.0 + self.ahead)
        self.assertEqual(self.captions._cues, [])  # the English ones went
        self.assertEqual(self.captions.language()[0], "Turkish")

    def test_each_run_takes_the_language_of_the_track_it_reads(self):
        # 2.9: on the owner's film the panel said Turkish while the English track played and
        # was read: the language had been worked out at another moment than the track.
        session = self.window.session
        session.playing = 13
        session.audio_track = lambda: session.playing
        session.audio_tracks = lambda: [(-1, "Disable"), (1, "English"), (13, "Track 13")]
        session.audio_track_info = lambda: [
            unittest.mock.Mock(id=1, language_code="English"),
            unittest.mock.Mock(id=13, language_code="tur"),
        ]
        self.captions._resolve_language()  # while the Turkish track played
        session.playing = 1
        self.captions._start_run()
        self.assertEqual(self.sent[0][4:6], (1, "English"))
        self.assertEqual(self.captions.language()[0], "English")

    def test_a_run_begun_on_the_default_track_reads_again_with_the_one_named(self):
        # 2.9, the review: a reopen chooses the viewer's track again once it plays; the run,
        # begun before, read the media's default.
        session = self.window.session
        session.playing = -1  # the player busy as the run began: the film's default
        session.audio_track = lambda: session.playing
        session.audio_tracks = lambda: [(-1, "Disable"), (13, "Track 13")]
        session.audio_track_info = lambda: [unittest.mock.Mock(id=13, language_code="tur")]
        self.captions._start_run()
        run = self.captions._run
        self.assertEqual(self.sent[0][4:6], (None, ""))
        self.captions._last = None
        self.captions._tick()
        self.assertEqual(self.captions._run, run)  # still busy: nothing yet
        session.playing = 13
        self.sent.clear()
        self.captions._tick()
        self.assertEqual(self.captions._run, run + 1)
        self.assertEqual(self.sent[0][4:6], (13, "Turkish"))

    def test_a_language_the_model_heard_is_forgotten_with_its_track(self):
        session = self.window.session
        session.playing = 2
        session.audio_track = lambda: session.playing
        self.captions._start_run()
        self.captions._on_detected(self.captions._run, "English")
        session.playing = 5  # a track that names no language
        self.captions._last = None
        self.captions._tick()
        self.assertEqual(self.sent[-2][5], "")  # the model hears this one anew
        self.assertEqual(self.captions._detected, {})

    def test_a_seek_in_the_film_reads_again_from_there(self):
        self.captions._start_run()
        run = self.captions._run
        self.captions._last = (120.0, time.monotonic())
        self.window.session.ms = 600_000
        self.captions._tick()
        self.assertEqual(self.captions._run, run + 1)
        self.assertAlmostEqual(self.sent[-2][3], 600.0 + self.ahead)

    def test_a_media_servers_film_is_read_as_it_is(self):
        self.window.session.url = "http://server.local/Videos/1/stream"
        self.window.controller.target = _Target(live=False, item={"_provider": "Server"})
        with unittest.mock.patch(
            "services.provider_ids.provider_of", lambda item: item["_provider"]
        ):
            self.assertTrue(self.captions.available())
            self.assertEqual(self.captions._film()[0], "http://server.local/Videos/1/stream")

    def provider_film(self):
        self.window.session.url = "http://provider.local/movie/u/p/7.mkv"
        self.window.controller = _Controller(
            _Target(live=False, item={"_provider": "Provider"}), self.window.session
        )
        patch = unittest.mock.patch(
            "services.provider_ids.provider_of", lambda item: item["_provider"]
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_providers_film_is_read_through_its_film_relay_never_again(self):  # 2.9
        self.provider_film()
        self.assertFalse(self.captions.available())  # one the relay can't carry (HLS)
        self.window.controller.relayable = True
        self.assertTrue(self.captions.available())  # offered: the relay will carry it
        self.assertIsNone(self.captions._film())  # not through one yet: nothing read
        self.window.session.through = _Relay(self.window.session.url)
        self.captions._start_run()
        kind, _run, location = self.sent[0][:3]
        self.assertEqual((kind, location), ("film", "http://127.0.0.1:5000/t/follower.mkv"))
        self.window.session.through = _Relay(self.window.session.url, followable=False)
        self.assertIsNone(self.captions._film())  # a relay the provider's answer declined

    def test_on_during_a_providers_film_sends_it_through_a_relay_once(self):
        self.provider_film()
        self.window.controller.relayable = True
        self.captions._enabled = False
        self.captions._enable("small")
        self.assertEqual(self.window.controller.relays, 1)
        self.captions._tick()
        self.captions._tick()
        self.assertEqual(self.window.controller.relays, 1)
        films = [m for m in self.sent if m[0] == "film"]
        self.assertEqual(len(films), 1)
        self.assertEqual(films[0][2], "http://127.0.0.1:5000/t/follower.mkv")

    def test_off_keeps_the_films_relay_and_on_again_reads_it_at_once(self):
        self.provider_film()
        self.window.controller.relayable = True
        relay = self.window.session.through = _Relay(self.window.session.url)
        self.captions._disable()
        self.assertIs(self.window.session.relay(), relay)  # no second reopen: kept to its end
        self.assertEqual(relay.stops, 0)
        self.captions._enable("small")
        self.assertEqual(self.window.controller.relays, 0)

    def test_a_film_still_opening_is_read_once_it_plays_where_it_resumes(self):
        self.window.session.settled = False
        self.captions._start_run()
        self.assertEqual(self.sent, [])
        self.captions._tick()
        self.assertEqual(self.sent, [])
        self.window.session.settled = True
        self.captions._tick()
        self.assertEqual(self.sent[0][:2], ("film", self.captions._run))
        self.assertAlmostEqual(self.sent[0][3], 120.0 + self.ahead)


if __name__ == "__main__":
    unittest.main()
