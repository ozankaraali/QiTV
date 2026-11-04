"""AI captions' pipeline (2.9): what a speech model wrote that nobody said, the stream's clock,
the pieces speech is cut into, QiTV's libVLC decoding a stream's sound, and when the window
shows each caption."""

from array import array
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import unittest.mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.captions import guards
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
    def test_what_the_broadcasts_showed_never_reaches_the_screen(self):
        self.assertEqual(guards.clean("system."), "")
        self.assertEqual(guards.clean("."), "")
        self.assertEqual(guards.clean("Altyazı M.K."), "")
        self.assertEqual(guards.clean("İzlediğiniz için teşekkür ederim."), "")
        self.assertEqual(guards.clean("Thanks for watching!"), "")
        self.assertEqual(guards.clean("Sen öyle. " * 40), "Sen öyle.")
        self.assertEqual(
            guards.clean("Kolay gelsin. Kolay gelsin. Hoş geldin."),
            "Kolay gelsin. Kolay gelsin. Hoş geldin.",
        )  # said twice: kept
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
    """2.9.1: Turkish is written by ufakzeka-kulak (far better than Qwen's Turkish, 25 times
    faster), the rest by Qwen."""

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

        model = object.__new__(_Model)
        model.recognizer = Recognizer("qwen", "Good evening.")
        model.turkish = Recognizer("ufak", "iyi akşamlar")
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
        self.given(model, "cohere", type(model.turkish)("cohere", "Bonsoir."))
        self.given(model, "european", type(model.turkish)("parakeet", "God aften."))
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
            setattr(model, name, type(model.turkish)(name, "loaded"))
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
        self.given(model, "european", type(model.turkish)("parakeet", "Good evening."))
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
            type(model.turkish)("parakeet", "And I've been at the bank, you know."),
        )
        model.recognizer = type(model.turkish)("qwen", "Et je vais à la banque.")
        self.assertEqual(model.decode(array("f", [0.1]), "French"), "Et je vais à la banque.")
        self.assertIn(("qwen", "language", "French"), heard)
        heard.clear()
        self.assertEqual(  # English, as it should be: Parakeet's
            model.decode(array("f", [0.1]), "English"), "And I've been at the bank, you know."
        )
        self.assertEqual({name for name, *_ in heard}, {"parakeet"})
        heard.clear()
        model.european = type(model.turkish)("parakeet", "Love money, forcément, dans un bureau.")
        self.assertEqual(model.decode(array("f", [0.1]), "French")[:10], "Love money")  # French

    def test_turkish_goes_to_ufak_louder_and_starts_with_a_capital(self):
        model, heard = self.model()
        self.assertEqual(model.decode(array("f", [0.1, 0.05]), "Turkish"), "iyi akşamlar")
        self.assertEqual(heard, [("ufak", "peak", array("f", [0.8])[0])])  # its card's gain
        heard.clear()
        self.assertEqual(model.decode(array("f", [0.1]), "English"), "Good evening.")
        self.assertEqual(heard[0], ("qwen", "language", "English"))
        cap = guards.token_cap(1 / 16000, "English")  # 2.9.3: each piece's own cap
        self.assertEqual(heard[1], ("qwen", "max_new_tokens", str(cap)))
        model.turkish = None  # its files not given: Qwen writes Turkish too
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
            recognizer=object(), decode=lambda samples, language: decoded.append(1) or "Hello."
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

    def test_ufaks_lone_i_joins_the_word_it_begins(self):
        # 2.9.6, the real-use review: "i stanbul" (İstanbul), "i şte" (İşte).
        from services.captions.worker import lone_i_joined

        self.assertEqual(lone_i_joined("ı i stanbul i şte bir"), "ı istanbul işte bir")
        self.assertEqual(lone_i_joined("bu iki ile"), "bu iki ile")  # i inside words stays
        self.assertEqual(lone_i_joined("sonunda i"), "sonunda i")  # nothing after it

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

    def set_caption_floor(self, seconds, *, shift=True):
        self.floor, self.shifted = seconds, shift


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
        with mock.patch("services.captions.models.download_size", return_value=43_000_000):
            self.captions._offer_specialist()
            self.captions._offer_specialist()  # once while QiTV runs
        [(text, _yes, _no)] = self.card.asked
        self.assertIn("43 MB", text)
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
        self.assertTrue(self.loaded[-1]["turkish"].endswith("ufakzeka-kulak"))  # ... with it
        self.assertEqual(self.captions._tier, "large")  # the same Qwen

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
