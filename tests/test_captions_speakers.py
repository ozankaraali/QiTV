"""Speaker marks (2026-11-08, the owner: "we need diarization otherwise dialogs write single"):
who speaks each caption line (services.captions.speakers), how the captions process tells it and
logs it, how a change shows (Dashes: "- " on a line of its own; Colours: each speaker's own), and
its row in a run from source."""

from array import array
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import test_captions_ui as ui_tests  # (as modules: their tests are not run again here)
import test_track_panel as panel_tests

from services.captions import speakers, trial_models
from services.captions.speakers import (
    COLOURS,
    DASH,
    DASHES,
    KNOWN_ABOVE,
    MIN_NEW_S,
    MIN_PART_S,
    MIN_VOICED_S,
    OFF,
    RATE,
    WINDOW,
    Colours,
    Part,
    Recent,
    Speakers,
    Turn,
    colour_of,
    coloured,
    dashed,
    joined,
    parts_of,
    plain,
)

A, B, C = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]


def mix(*parts):
    """A print made of voices in these shares ((share, voice), ...)."""
    return [sum(share * voice[i] for share, voice in parts) for i in range(3)]


class SpeakersTests(unittest.TestCase):
    def hear(self, speakers, prints, voiced=2.0):
        out = []
        for i, print_ in enumerate(prints):
            turn = speakers.hear(print_, voiced, 10.0 + 3 * i)
            out.append((turn.speaker, turn.changed))
        return out

    def test_one_voice_going_on_is_one_speaker(self):
        said = self.hear(Speakers(), [A, mix((1, A), (0.2, B)), mix((1, A), (0.1, C))])
        self.assertEqual(said, [(1, False), (1, False), (1, False)])

    def test_a_dialogue_finds_each_voice_again(self):
        # A, B, then A again (the same voice, not a third speaker), then B
        said = self.hear(Speakers(), [A, B, mix((1, A), (0.3, C)), mix((1, B), (0.2, C))])
        self.assertEqual(said, [(1, False), (2, True), (1, True), (2, True)])

    def test_a_line_too_short_to_tell_goes_with_the_speaker_before(self):
        voices = Speakers()
        voices.hear(A, 2.0, 10.0)
        short = voices.hear(B, MIN_VOICED_S - 0.05, 12.0)
        self.assertEqual((short.speaker, short.changed, short.trusted), (1, False, False))
        none = voices.hear(None, 2.0, 13.0)  # no print taken (its sound too short)
        self.assertEqual((none.speaker, none.changed, none.trusted), (1, False, False))
        self.assertTrue(voices.hear(B, 2.0, 14.0).changed)  # long enough: it is another

    def test_with_a_margin_a_part_goes_to_another_voice_only_clearly_the_most_alike(self):
        voices = Speakers(margin=0.05)
        voices.hear(A, 2.0, 10.0)
        voices.hear(B, 2.0, 12.0)  # B speaks last
        both = speakers.unit(mix((1, A), (0.98, B)))  # about as like A as B: B goes on
        self.assertGreater(speakers.cosine(both, A), KNOWN_ABOVE)
        self.assertEqual(voices.hear(both, 2.0, 14.0).speaker, 2)

    def test_with_a_wait_away_from_the_speaker_before_once_the_next_part_agrees(self):
        voices = Speakers(strong=0.2)
        voices.hear(A, 2.0, 10.0)
        voices.hear(B, 2.0, 12.0)  # B speaks last
        leaning = speakers.unit(mix((1, A), (0.8, B)))  # more like A, not by much
        first = voices.hear(leaning, 2.0, 14.0)
        self.assertEqual((first.speaker, first.changed, first.trusted), (2, False, False))
        second = voices.hear(leaning, 2.0, 16.0)  # it agrees: A speaks
        self.assertEqual((second.speaker, second.changed), (1, True))

    def test_strong_evidence_changes_the_speaker_at_once(self):
        voices = Speakers(strong=0.2)
        voices.hear(A, 2.0, 10.0)
        voices.hear(B, 2.0, 12.0)
        turn = voices.hear(mix((1, A), (0.1, C)), 2.0, 14.0)
        self.assertEqual((turn.speaker, turn.changed), (1, True))

    def test_a_mistaken_new_voice_turns_out_an_old_one_and_keeps_its_number(self):
        voices = Speakers(known_above=0.3, new_below=0.3, merge_above=0.6)
        voices.hear(A, 2.0, 10.0)
        odd = speakers.unit(mix((0.25, A), (1, C)))  # A, heard oddly: taken for a new voice
        self.assertEqual(voices.hear(odd, 2.0, 12.0).speaker, 2)
        # more of A as the new voice learns: it comes out as A's, and goes into it
        for at in (14.0, 16.0, 18.0):
            turn = voices.hear(speakers.unit(mix((1, A), (0.9, C))), 2.0, at)
        self.assertEqual(turn.speaker, 1)
        self.assertEqual(voices.hear(A, 2.0, 20.0).speaker, 1)

    def test_six_voices_are_kept(self):
        voices = Speakers()
        prints = [[1.0 if i == k else 0.0 for i in range(7)] for k in range(7)]
        for k, print_ in enumerate(prints[:6]):
            self.assertEqual(voices.hear(print_, 2.0, 10.0 + k).speaker, k + 1)
        self.assertEqual(voices.hear(prints[0], 2.0, 20.0).speaker, 1)  # still known

    def test_a_new_voice_needs_a_line_long_enough_to_be_sure_of(self):
        voices = Speakers()
        voices.hear(A, 2.0, 10.0)
        short = voices.hear(B, MIN_NEW_S - 0.1, 12.0)  # like nobody, but short: A goes on
        self.assertEqual((short.speaker, short.changed, short.trusted), (1, False, False))
        long = voices.hear(B, MIN_NEW_S, 14.0)
        self.assertEqual((long.speaker, long.changed), (2, True))
        back = voices.hear(A, MIN_NEW_S - 0.5, 16.0)  # a voice heard: short is enough
        self.assertEqual((back.speaker, back.changed), (1, True))

    def test_only_a_few_voices_are_kept_and_one_not_heard_for_long_is_forgotten(self):
        voices = Speakers(keep=2)
        self.assertEqual(self.hear(voices, [A, B, C]), [(1, False), (2, True), (3, True)])
        self.assertEqual(voices.hear(A, 2.0, 30.0).speaker, 4)  # A was let go: a new one
        voices = Speakers(forget=60.0)
        voices.hear(A, 2.0, 0.0)
        voices.hear(B, 2.0, 5.0)
        self.assertEqual(voices.hear(A, 2.0, 100.0).speaker, 3)  # not heard for 100 s

    def test_the_log_s_words_for_a_line_s_speaker(self):
        self.assertEqual(Turn(2, 0.314, True).tag(), "S2 0.31 new")
        self.assertEqual(Turn(1, 0.82, False).tag(), "S1 0.82")
        self.assertEqual(Turn(1, None, False).tag(), "S1")
        self.assertEqual(Turn(1, 0.2, False, trusted=False).tag(), "S1 0.20 short")


class RecentTests(unittest.TestCase):
    @staticmethod
    def sound(windows):
        """Whole detector windows, each voiced (1.0) or not (0.0)."""
        return array("f", [1.0 if voiced else 0.0 for voiced in windows for _ in range(WINDOW)])

    def test_a_line_finds_its_voiced_windows_only(self):
        recent = Recent(lambda window: window[0] > 0.5)
        step = WINDOW / RATE
        recent.push(10.0, self.sound([True, False, True, True, False, True]))
        got = recent.voiced(10.0, 10.0 + 4 * step)  # the first four windows
        self.assertEqual(len(got), 3 * WINDOW)
        self.assertEqual(len(recent.voiced(10.0 + 4 * step, 11.0)), WINDOW)

    def test_sound_in_blocks_of_any_length_makes_whole_windows(self):
        recent = Recent(None)  # (no detector: all voiced)
        samples = array("f", bytes(4 * 3 * WINDOW))
        recent.push(0.0, samples[:700])
        recent.push(700 / RATE, samples[700:])  # goes on from there
        self.assertEqual(len(recent.voiced(0.0, 1.0)), 3 * WINDOW)

    def test_a_jump_starts_the_windows_afresh_and_old_sound_goes(self):
        recent = Recent(None, keep_s=5.0)
        recent.push(0.0, array("f", bytes(4 * (WINDOW + 100))))
        recent.push(3.0, array("f", bytes(4 * WINDOW)))  # a jump: the 100 left over are dropped
        self.assertEqual(len(recent.voiced(0.0, 4.0)), 2 * WINDOW)
        recent.push(20.0, array("f", bytes(4 * WINDOW)))
        self.assertEqual(len(recent.voiced(0.0, 10.0)), 0)  # older than 5 s before the newest
        self.assertEqual(len(recent.voiced(19.0, 21.0)), WINDOW)


class PartsTests(unittest.TestCase):
    """A line in the parts that may each be another speaker's (2026-11-08, the owner)."""

    @staticmethod
    def windows(*runs):
        """Voiced flags from (seconds, voiced) runs, a window each 32 ms."""
        step = WINDOW / RATE
        return [voiced for seconds, voiced in runs for _ in range(round(seconds / step))]

    def test_a_line_splits_after_each_sentence_at_the_pause_nearest(self):
        voiced = self.windows((2.0, True), (0.3, False), (1.5, True))
        parts = parts_of("Nereye gidiyorsun böyle? Eve dönüyorum.", 10.0, 13.8, voiced)
        self.assertEqual([p.text for p in parts], ["Nereye gidiyorsun böyle?", "Eve dönüyorum."])
        self.assertAlmostEqual(parts[0].end, 12.15, delta=0.05)  # in the pause, not by letters
        self.assertEqual((parts[0].start, parts[1].start, parts[1].end), (10.0, parts[0].end, 13.8))

    def test_a_long_pause_splits_a_line_with_no_sentence_end(self):
        voiced = self.windows((1.5, True), (0.6, False), (1.5, True))
        parts = parts_of("bu akşam geliyor musun yarın sabah değil", 0.0, 3.6, voiced)
        self.assertEqual(len(parts), 2)
        self.assertAlmostEqual(parts[0].end, 1.8, delta=0.05)

    def test_only_a_line_s_longest_pauses_split_it(self):
        voiced = self.windows(
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
        voiced = self.windows((0.4, True), (0.3, False), (2.0, True))
        parts = parts_of("Evet. Yarın gelirim, merak etme.", 0.0, 2.7, voiced)
        self.assertEqual([p.text for p in parts], ["Evet. Yarın gelirim, merak etme."])
        self.assertGreater(MIN_PART_S, 0.4)

    def test_without_the_voice_detector_by_letters_and_one_word_as_it_is(self):
        parts = parts_of("Bir iki üç. Dört beş altı.", 0.0, 4.0)
        self.assertEqual([p.text for p in parts], ["Bir iki üç.", "Dört beş altı."])
        self.assertAlmostEqual(parts[0].end, 4.0 * 12 / 27, delta=0.01)  # its share of letters
        self.assertEqual(parts_of("Evet.", 1.0, 2.0), [Part("Evet.", 1.0, 2.0)])

    def test_the_windows_may_begin_before_the_line(self):
        voiced = self.windows((1.0, False), (2.0, True), (0.3, False), (1.5, True))
        parts = parts_of("Nereye gidiyorsun böyle? Eve dönüyorum.", 11.0, 14.8, voiced, at=10.0)
        self.assertAlmostEqual(parts[0].end, 13.15, delta=0.05)


class SegmentationTests(unittest.TestCase):
    """Where the speaker changes inside a line, as pyannote's segmentation hears it."""

    @staticmethod
    def frames(*runs):
        """Per-frame labels from (seconds, label) runs (17 ms frames)."""
        from services.captions.turns import FRAME

        return [label for seconds, label in runs for _ in range(round(seconds * RATE / FRAME))]

    def test_the_changes_inside_a_line(self):
        from services.captions.turns import found_in

        labels = self.frames((1.5, 1), (0.3, 0), (2.0, 2), (0.2, 0), (1.5, 1))
        found = found_in(labels, 100.0, 101.0)  # the line begins 1 s in
        self.assertEqual(len(found.changes), 2)
        self.assertAlmostEqual(found.changes[0], 101.67, delta=0.05)  # in the gap
        before = self.frames((2.0, 2), (1.0, 1))  # a change before the line is none of its own
        self.assertEqual(found_in(before, 0.0, 2.5).changes, [])

    def test_a_blip_is_no_turn_and_two_at_once_none_either(self):
        from services.captions.turns import found_in, runs

        labels = self.frames((1.0, 1), (0.1, 2), (1.0, 1), (0.5, -1), (1.0, 1))
        self.assertEqual([r[0] for r in runs(labels, 18)], [1])
        self.assertEqual(found_in(labels, 0.0, 0.0).changes, [])

    def test_parts_split_where_it_changes_and_say_so(self):
        parts = parts_of("Nereye? Eve dönüyorum ben hemen.", 0.0, 3.0, changes=[1.0])
        self.assertEqual([p.text for p in parts], ["Nereye?", "Eve dönüyorum ben hemen."])
        self.assertEqual([p.new for p in parts], [None, True])  # (at the sentence's end near it)
        same = parts_of("Bir şey. Bir şey daha söyleyeyim.", 0.0, 3.0, changes=[])
        self.assertEqual([p.new for p in same], [None, False])  # a sentence's end, the same voice


class PagedTests(unittest.TestCase):
    """A caption too long for two lines in parts at sentences' ends (2026-11-08, the owner)."""

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


class MarkTests(unittest.TestCase):
    def test_a_new_speaker_s_line_starts_with_a_dash_once(self):
        self.assertEqual(dashed("Eve."), "- Eve.")
        self.assertEqual(dashed("- Eve."), "- Eve.")
        self.assertEqual(DASH, "- ")

    def test_two_speakers_together_each_on_a_line_of_their_own(self):
        self.assertEqual(joined("Nereye gidiyorsun?", "Eve.", True), "- Nereye gidiyorsun?\n- Eve.")
        self.assertEqual(joined("- Nereye?", "- Eve.", True), "- Nereye?\n- Eve.")

    def test_one_speaker_going_on_changes_nothing(self):
        self.assertEqual(joined("Merhaba", "nasılsın?", False), "Merhaba nasılsın?")
        # the second speaker goes on: on their own line
        self.assertEqual(
            joined("- Nereye?\n- Eve", "gidiyorum.", False), "- Nereye?\n- Eve gidiyorum."
        )

    def test_each_speaker_s_colour_in_the_order_they_come(self):
        colours = Colours()
        self.assertEqual([colours.of(n) for n in (1, 2, 1, 3, 2, 4)], [0, 1, 0, 2, 1, 3])

    def test_a_fifth_speaker_shares_the_colour_of_the_one_heard_longest_ago(self):
        colours = Colours()
        for n in (1, 2, 3, 4, 1, 3, 4):
            colours.of(n)
        self.assertEqual(colours.of(5), 1)  # S2's yellow: S2 was heard longest ago
        self.assertEqual(colours.of(1), 0)  # the others keep theirs
        self.assertEqual(colours.of(2), 1)  # and S2 too: a colour never changes once given

    def test_a_line_s_colour_goes_with_its_words(self):
        line = coloured("Eve.", 2)
        self.assertEqual(colour_of(line), (2, "Eve."))
        self.assertEqual(colour_of("Eve."), (None, "Eve."))
        self.assertEqual(plain(line), "Eve.")
        self.assertEqual(coloured(coloured("Eve.", 1), 3), coloured("Eve.", 3))  # one colour

    def test_two_speakers_together_each_in_their_colour_with_no_dash(self):
        before, line = coloured("Nereye?", 0), coloured("Eve.", 1)
        self.assertEqual(joined(before, line, True, COLOURS), f"{before}\n{line}")
        # the same speaker goes on: one colour, one line
        self.assertEqual(joined(before, coloured("Hemen.", 0), False, COLOURS), f"{before} Hemen.")

    def test_it_needs_no_qt(self):  # the captions process imports it
        import subprocess

        code = (
            "import sys; import services.captions.speakers; "
            "sys.exit(any(m.startswith('PySide6') for m in sys.modules))"
        )
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(subprocess.run([sys.executable, "-c", code], cwd=root).returncode, 0)


class _Voices:
    """The speaker model's face: voice A for sound above zero, B below."""

    def __init__(self):
        self.read = []

    def embed(self, samples):
        self.read.append(len(samples))
        if len(samples) < speakers.MIN_PRINT_S * RATE:
            return None
        return A if sum(samples) > 0 else B


def _bare_run(model, level=logging.INFO):
    """A captions run without its threads, its sound, or a stream."""
    from services.captions.worker import _Run

    run: Any = object.__new__(_Run)
    run.run, run.model = 3, model
    run.sent = []
    run.link = SimpleNamespace(send=run.sent.append)
    run.speakers, run.recent, run._line_level, run._writes = Speakers(), None, level, "ufak"
    return run


class ProcessTests(unittest.TestCase):
    """The captions process tells each line's speaker, sends it and logs who spoke."""

    def model(self, on=True):
        return SimpleNamespace(voices=_Voices(), speakers_on=on, speaker_voiced=lambda w: True)

    def test_a_line_by_another_speaker_goes_out_as_a_change_and_the_log_says_who(self):
        run = _bare_run(self.model())
        voice_a, voice_b = array("f", [0.1] * 2 * RATE), array("f", [-0.1] * 2 * RATE)
        run._hold_for_speakers(10.0, voice_a)
        run._hold_for_speakers(12.0, voice_b)
        with self.assertLogs("services.captions.worker", logging.INFO) as logs:
            run._write("ufak", "nereye", "Nereye?", 10.0, 12.0)
            run._write("ufak", "eve", "Eve.", 12.0, 14.0)
        self.assertEqual(
            run.sent,
            [
                ("caption", 3, 10.0, 12.0, "Nereye?", 0, 1),
                ("caption", 3, 12.0, 14.0, "Eve.", 1, 2),  # a change, speaker 2
            ],
        )
        said = [record.getMessage() for record in logs.records]
        self.assertEqual(
            said,
            [
                "AI captions: [ufak] heard 'nereye', shows 'Nereye?' · S1",
                "AI captions: [ufak] heard 'eve', shows 'Eve.' · S2 0.00 new",
            ],
        )

    def test_a_line_of_two_speakers_goes_out_in_parts_each_told(self):
        model = self.model()
        model.speaker_voiced = lambda window: window[0] != 0.0  # (silence: unvoiced)
        run = _bare_run(model)
        run._hold_for_speakers(10.0, array("f", [0.1] * 2 * RATE))
        run._hold_for_speakers(12.0, array("f", [0.0] * (RATE // 2)))  # a pause
        run._hold_for_speakers(12.5, array("f", [-0.1] * 2 * RATE))
        with self.assertLogs("services.captions.worker", logging.INFO) as logs:
            run._write("ufak", "x", "Nereye gidiyorsun? Eve dönüyorum.", 10.0, 14.5)
        [message] = run.sent
        self.assertEqual(
            message[:7], ("caption", 3, 10.0, 14.5, "Nereye gidiyorsun? Eve dönüyorum.", 0, 1)
        )
        (first, a, b, turn1, s1), (second, c, d, turn2, s2) = message[7]
        self.assertEqual(
            (first, turn1, s1, second, turn2, s2),
            ("Nereye gidiyorsun?", 0, 1, "Eve dönüyorum.", 1, 2),
        )
        self.assertEqual((a, d), (10.0, 14.5))
        self.assertAlmostEqual(b, 12.25, delta=0.1)  # in the pause
        self.assertIn("· S1 | S2 0.00 new", logs.records[0].getMessage())

    def test_a_piece_s_own_voice_detector_times_its_parts(self):
        run = _bare_run(self.model(on=False))
        voiced = PartsTests.windows((2.0, True), (0.4, False), (1.5, True))
        run._write("ufak", "x", "Bir iki üç dört. Beş altı yedi.", 20.0, 23.9, voiced=voiced)
        [message] = run.sent
        self.assertEqual([part[0] for part in message[7]], ["Bir iki üç dört.", "Beş altı yedi."])
        self.assertAlmostEqual(message[7][0][2], 22.2, delta=0.05)
        self.assertEqual([part[3:] for part in message[7]], [(0, 0), (0, 0)])  # none told

    def test_with_speakers_unmarked_no_sound_is_kept_and_no_line_is_told(self):
        model = self.model(on=False)
        run = _bare_run(model)
        run._hold_for_speakers(10.0, array("f", [0.1] * RATE))
        self.assertIsNone(run.recent)
        with self.assertLogs("services.captions.worker", logging.INFO) as logs:
            run._write("ufak", "eve", "Eve.", 10.0, 11.0, until=11.5)
        self.assertEqual(run.sent, [("caption", 3, 10.0, 11.5, "Eve.", 0, 0)])
        self.assertEqual(
            logs.records[0].getMessage(), "AI captions: [ufak] heard 'eve', shows 'Eve.'"
        )
        self.assertEqual(model.voices.read, [])

    def test_each_line_is_logged_at_info_only_where_test_models_are_offered(self):
        from services.captions import worker

        with mock.patch.object(trial_models, "enabled", return_value=True):
            self.assertEqual(worker.line_level(), logging.INFO)
        with mock.patch.object(trial_models, "enabled", return_value=False):
            self.assertEqual(worker.line_level(), logging.DEBUG)  # a released build: quieter
        # (trial_models.enabled: a run from source, or a packaged one with QITV_TRIAL_MODELS=1)
        with mock.patch.object(sys, "frozen", True, create=True):
            with mock.patch.dict(os.environ, {"QITV_TRIAL_MODELS": "1"}):
                self.assertEqual(worker.line_level(), logging.INFO)
            with mock.patch.dict(os.environ, {"QITV_TRIAL_MODELS": ""}):
                self.assertEqual(worker.line_level(), logging.DEBUG)
        run = _bare_run(self.model(on=False), level=logging.DEBUG)
        with self.assertLogs("services.captions.worker", logging.DEBUG) as logs:
            run._write("ufak", "eve", "Eve.", 10.0, 11.0)
        self.assertEqual([r.levelno for r in logs.records], [logging.DEBUG])

    def test_the_speaker_model_loads_in_its_own_thread_and_turns_off(self):
        from test_captions_trials import _fake_sherpa

        sherpa: Any = _fake_sherpa([])
        made = []

        class Config:
            def __init__(self, model, num_threads):
                self.model = model

            def validate(self):
                return True

        class Extractor:
            def __init__(self, config):
                made.append(config.model)

        sherpa.SpeakerEmbeddingExtractorConfig = Config
        sherpa.SpeakerEmbeddingExtractor = Extractor
        from services.captions.worker import _Model

        spec = {
            "runtime": None,
            "model": {"conv_frontend": "c", "encoder": "e", "decoder": "d", "tokenizer": "t"},
            "vad": "vad.onnx",
            "speaker": "/models/speaker.onnx",
        }
        with mock.patch.dict(sys.modules, {"sherpa_onnx": sherpa}):
            model = _Model(spec)
            deadline = time.monotonic() + 5
            while model.voices is None and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(made, ["/models/speaker.onnx"])
        self.assertTrue(model.speakers_on)
        self.assertFalse(model.speaker_voiced(array("f", bytes(4 * WINDOW))))
        model.use_speakers(None)
        self.assertFalse(model.speakers_on)
        model.use_speakers("/models/speaker.onnx")  # on again: loaded already
        self.assertEqual((model.speakers_on, made), (True, ["/models/speaker.onnx"]))

    def test_the_gui_hears_each_line_s_speaker(self):
        from PySide6.QtWidgets import QApplication

        from services.captions.process import CaptionsProcess

        QApplication.instance() or QApplication([])
        process = CaptionsProcess()
        heard = []
        process.caption.connect(lambda *args: heard.append(args))
        process._supervisor, process._serial = object(), 1  # type: ignore[assignment]
        process._on_message(1, ("caption", 2, 10.0, 11.0, "Eve.", 1, 2))
        process._on_message(1, ("caption", 2, 11.0, 12.0, "Evet."))  # none told
        process._supervisor = None
        parts = [("Eve.", 10.0, 10.5, 1, 2), ("Evet.", 10.5, 11.0, 0, 2)]
        process._supervisor = object()  # type: ignore[assignment]
        process._on_message(1, ("caption", 2, 10.0, 11.0, "Eve. Evet.", 1, 2, parts))
        process._supervisor = None
        self.assertEqual(
            heard,
            [
                (2, 10.0, 11.0, "Eve.", 1, 2, []),
                (2, 11.0, 12.0, "Evet.", 0, 0, []),
                (2, 10.0, 11.0, "Eve. Evet.", 1, 2, parts),
            ],
        )


class ControllerTests(unittest.TestCase):
    """How a change of speaker shows, and the Speaker marks chosen."""

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
        self.here = {"speaker": True}
        captions._here = lambda name: self.here.get(name, True)
        self.offered = mock.patch.object(trial_models, "enabled", return_value=True)
        self.offered.start()
        self.addCleanup(self.offered.stop)
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

    def dialogue(self):
        """Two speakers' lines, no pause between them, then the second goes on after one."""
        self.captions._on_caption(self.number, 10.0, 12.0, "Nereye gidiyorsun?", 0, 1)
        self.captions._on_caption(self.number, 12.1, 13.0, "Eve.", 1, 2)
        self.captions._on_caption(self.number, 14.0, 14.8, "Hemen.", 0, 2)

    def test_dashes_two_speakers_together_each_on_a_line_with_a_dash(self):
        self.dialogue()
        self.assertEqual(
            self.captions._cues,
            [(10.0, 13.0, "- Nereye gidiyorsun?\n- Eve."), (14.0, 14.8, "Hemen.")],
        )
        self.assertEqual(self.at(12.5), "- Nereye gidiyorsun?\n- Eve.")

    def test_dashes_a_new_speaker_s_line_on_its_own_starts_with_a_dash(self):
        self.captions._on_caption(self.number, 10.0, 12.0, "Nereye gidiyorsun?", 0, 1)
        self.captions._on_caption(self.number, 13.0, 14.0, "Eve.", 1, 2)  # after a pause
        self.captions._on_caption(self.number, 14.1, 14.8, "Hemen.", 0, 2)  # the same goes on
        self.assertEqual(self.captions._cues[-1], (13.0, 14.8, "- Eve. Hemen."))
        self.assertEqual(self.at(11.0), "Nereye gidiyorsun?")
        self.assertEqual(self.at(13.5), "- Eve. Hemen.")

    def test_one_speaker_going_on_changes_nothing(self):
        for mark in (DASHES, COLOURS):
            self.config["ai_captions_speaker_marks"] = mark
            self.captions._forget_cues()
            self.captions._on_caption(self.number, 10.0, 12.0, "Merhaba", 0, 0)  # (untold)
            self.captions._on_caption(self.number, 12.1, 13.0, "nasılsın?", 0, 0)
            self.assertEqual(self.captions._cues, [(10.0, 13.0, "Merhaba nasılsın?")])

    def test_two_speakers_that_don_t_fit_two_lines_show_one_after_the_other(self):
        long = "Bu akşam eve gelmeden önce markete uğrayıp ekmek ve süt almayı unutma"
        self.captions._on_caption(self.number, 10.0, 15.0, long, 0, 1)
        self.captions._on_caption(self.number, 15.1, 16.0, "Tamam.", 1, 2)
        self.assertEqual(self.captions._cues, [(10.0, 15.0, long), (15.1, 16.0, "- Tamam.")])

    def test_colours_each_speaker_in_their_own_with_no_dash(self):
        self.captions.set_speaker_marks(COLOURS)
        self.dialogue()
        white, yellow = coloured("Nereye gidiyorsun?", 0), coloured("Eve.", 1)
        self.assertEqual(
            self.captions._cues,
            [(10.0, 13.0, f"{white}\n{yellow}"), (14.0, 14.8, coloured("Hemen.", 1))],
        )
        self.captions._on_caption(self.number, 16.0, 17.0, "Peki.", 1, 1)  # S1 again: white
        self.assertEqual(self.captions._cues[-1][2], coloured("Peki.", 0))
        self.captions._on_caption(self.number + 1, 18.0, 19.0, "Eski.", 1, 2)  # an old run's: gone
        self.captions._run += 1  # a new run: its speakers' colours from white again
        self.captions._on_caption(self.captions._run, 20.0, 21.0, "Yeni.", 0, 2)
        self.assertEqual(self.captions._cues[-1][2], coloured("Yeni.", 0))

    def test_each_part_of_a_line_is_marked_on_its_own(self):
        parts = [("Nereye gidiyorsun?", 10.0, 11.5, 0, 1), ("Eve.", 11.5, 12.5, 1, 2)]
        self.captions._on_caption(self.number, 10.0, 12.5, "Nereye gidiyorsun? Eve.", 0, 1, parts)
        self.assertEqual(self.captions._cues, [(10.0, 12.5, "- Nereye gidiyorsun?\n- Eve.")])
        self.captions.set_speaker_marks(COLOURS)
        self.captions._forget_cues()
        self.captions._on_caption(self.number, 10.0, 12.5, "Nereye gidiyorsun? Eve.", 0, 1, parts)
        white, yellow = coloured("Nereye gidiyorsun?", 0), coloured("Eve.", 1)
        self.assertEqual(self.captions._cues[0][2], f"{white}\n{yellow}")
        self.captions.set_speaker_marks(OFF)
        self.captions._forget_cues()
        self.captions._on_caption(self.number, 10.0, 12.5, "Nereye gidiyorsun? Eve.", 0, 1, parts)
        self.assertEqual(self.captions._cues[0][2], "Nereye gidiyorsun? Eve.")

    def test_a_long_caption_shows_part_by_part_as_each_is_said(self):
        self.overlay.pages = lambda text: [  # (as the overlay pages it: at the sentences)
            page for page in ("Birinci cümle burada bitiyor.", "İkincisi de sonra geliyor.")
        ]
        parts = [
            ("Birinci cümle burada bitiyor.", 10.0, 13.0, 0, 0),
            ("İkincisi de sonra geliyor.", 13.0, 15.0, 0, 0),
        ]
        text = "Birinci cümle burada bitiyor. İkincisi de sonra geliyor."
        self.captions._on_caption(self.number, 10.0, 15.0, text, 0, 0, parts)
        self.assertEqual(self.at(10.4), "Birinci cümle burada bitiyor.")
        self.assertEqual(self.at(12.9), "Birinci cümle burada bitiyor.")  # its words go on
        self.assertEqual(self.at(13.1), "İkincisi de sonra geliyor.")  # when it is said

    def test_a_part_said_soon_after_waits_to_be_read_but_no_flash_and_never_long(self):
        self.overlay.pages = lambda text: ["Evet.", "Hemen geliyorum, bekle beni orada lütfen."]
        parts = [
            ("Evet.", 10.0, 10.2, 0, 0),
            ("Hemen geliyorum, bekle beni orada lütfen.", 10.2, 13.0, 0, 0),
        ]
        self.captions._on_caption(
            self.number, 10.0, 13.0, "Evet. Hemen geliyorum, bekle beni orada lütfen.", 0, 0, parts
        )
        self.assertEqual(self.at(11.2), "Evet.")  # on PAGE_S at least (from 10.3)
        self.assertEqual(self.at(11.35), "Hemen geliyorum, bekle beni orada lütfen.")

    def test_a_long_caption_with_no_times_shows_by_its_letters(self):
        self.overlay.pages = lambda text: text.split(" | ")
        self.captions._on_caption(self.number, 10.0, 16.0, "one two three | four five six")
        self.captions._timing.clear()  # (no times: each part for its share of the letters)
        self.assertEqual(self.at(10.3), "one two three")
        self.assertEqual(self.at(15.0), "four five six")

    def test_off_a_change_shows_as_before_and_the_process_tells_them_no_more(self):
        self.captions.set_speaker_marks(OFF)
        self.assertEqual(self.config["ai_captions_speaker_marks"], OFF)
        self.assertEqual(self.sent[-1], ("speakers", None, None))
        self.dialogue()
        self.assertEqual(self.captions._cues[0], (10.0, 13.0, "Nereye gidiyorsun? Eve."))
        self.captions.set_speaker_marks(DASHES)
        self.assertEqual(self.sent[-1][0], "speakers")
        self.assertTrue(self.sent[-1][1].endswith(".onnx"))

    def test_the_row_steps_round_the_three_from_dashes(self):
        self.assertEqual(self.captions.speaker_marks(), DASHES)  # the default
        self.assertEqual(
            self.captions.speaker_mark_choices(),
            [(OFF, "Off"), (DASHES, "Dashes"), (COLOURS, "Colours")],
        )
        self.captions.step_speaker_marks(1)
        self.assertEqual(self.captions.speaker_marks(), COLOURS)
        self.captions.step_speaker_marks(1)
        self.assertEqual(self.captions.speaker_marks(), OFF)
        self.captions.step_speaker_marks(-1)
        self.assertEqual(self.captions.speaker_marks(), COLOURS)
        self.config["ai_captions_speaker_marks"] = "sparkles"  # (not one: the default)
        self.assertEqual(self.captions.speaker_marks(), DASHES)

    def test_offered_where_test_models_are_and_nowhere_else(self):
        self.assertTrue(self.captions.speaker_marks_offered())
        with mock.patch.object(trial_models, "enabled", return_value=False):
            self.assertEqual(self.captions.speaker_marks(), OFF)
            self.captions.set_speaker_marks(COLOURS)  # not offered: nothing changes
            self.assertNotIn("ai_captions_speaker_marks", self.config)
            self.dialogue()
            self.assertEqual(self.captions._cues[0][2], "Nereye gidiyorsun? Eve.")
            self.assertIsNone(self.captions._speaker_file())  # the process loads no model

    def test_the_process_loads_the_speaker_model_with_the_speech_model(self):
        loaded = []
        captions: Any = self.captions
        captions._process.start = lambda load, detailed_log=False: loaded.append(load)
        self.captions._tier = "small"
        self.captions._load_model()
        self.assertTrue(loaded[0]["speaker"].endswith(".onnx"))
        self.assertTrue(loaded[0]["segmentation"].endswith("model.onnx"))
        self.config["ai_captions_speaker_marks"] = OFF
        self.captions._load_model()
        self.assertIsNone(loaded[1]["speaker"])

    def test_the_speaker_model_downloads_once_unasked_and_the_process_takes_it(self):
        from services.captions import models

        self.here["speaker"] = False
        started = []
        captions: Any = self.captions
        captions._start_download = lambda: started.append(self.captions._fetching)
        self.here["small"] = False
        self.captions._tier = "small"
        self.captions._on_ready()
        self.assertEqual(started, [])  # the speech model first
        self.here["small"] = True
        self.captions._on_ready()
        self.assertEqual(started, [models.SPEAKER_KEY])
        self.captions._fetch_speakers()  # once while QiTV runs
        self.assertEqual(started, [models.SPEAKER_KEY])
        download = object()
        self.captions._download = download
        self.here["speaker"] = True
        self.captions._on_downloaded(download)
        speaker, segmentation = str(models.speaker_file()), str(models.segmentation_file())
        self.assertEqual(self.sent[-1], ("speakers", speaker, segmentation))
        self.assertTrue(self.captions.enabled())  # nothing else restarts

    def test_the_mark_chosen_is_kept_in_the_config_older_builds_ignore(self):
        from services.config_manager import ConfigManager

        config: Any = ConfigManager.__new__(ConfigManager)
        config.config = {}
        self.assertEqual(config.ai_captions_speaker_marks, DASHES)
        config.ai_captions_speaker_marks = COLOURS
        self.assertEqual(config.config, {"ai_captions_speaker_marks": COLOURS})
        config.config["ai_captions_speaker_marks"] = "sparkles"
        self.assertEqual(config.ai_captions_speaker_marks, DASHES)

    def test_the_speaker_model_is_pinned_in_its_own_folder(self):
        from services.captions import models

        root = Path(self.tmp.name)
        download, segmentation = models.needed(models.SPEAKER_KEY, root)
        self.assertEqual(download.folder, root / models.SPEAKER_FOLDER)
        self.assertEqual(download.file.size, 26485263)
        self.assertEqual(models.speaker_file(root), download.target)
        self.assertEqual(segmentation.file.size, 5992913)  # pyannote's segmentation, with it
        self.assertEqual(models.segmentation_file(root), segmentation.target)
        self.assertNotIn(models.SPEAKER_KEY, models.SPECIALISTS)  # not in the first question
        self.assertFalse(models.installed(models.SPEAKER_KEY, Path(self.tmp.name)))


class OverlayTests(unittest.TestCase):
    """The captions keep a caption's own line breaks and each line's colour."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtWidgets import QWidget

        from ui.overlays.captions import CaptionsOverlay

        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.overlay = CaptionsOverlay(self.host)

    def test_two_speakers_lines_each_on_a_line_of_their_own(self):
        self.overlay.show_text("- Nereye  gidiyorsun?\n- Eve.")
        self.assertEqual(self.overlay.lines(), ["- Nereye gidiyorsun?", "- Eve."])
        self.assertEqual(self.overlay.line_colours(), [None, None])
        self.assertEqual(self.overlay.text(), "- Nereye gidiyorsun?\n- Eve.")
        together = "- Nereye gidiyorsun?\n- Eve."
        self.assertEqual(self.overlay.pages(together), [together])

    def test_two_speakers_that_don_t_fit_show_one_after_the_other(self):
        first = "- Bu akşam eve gelmeden önce markete uğrayıp ekmek, süt ve biraz da peynir al"
        text = f"{first}\n- Tamam."
        self.assertEqual(self.overlay.pages(text), [first, "- Tamam."])
        self.overlay.show_text(text)  # (shown whole it would be cut: as one caption then)
        self.assertLessEqual(len(self.overlay.lines()), 2)

    def test_a_caption_too_long_comes_in_parts_at_its_sentences_ends(self):
        first = "Bu akşam yemekten sonra hep birlikte sahile inip biraz yürüyelim mi?"
        second = "Olur ama önce annemi arayıp eve geç kalacağımızı söylemem gerekiyor."
        self.assertEqual(self.overlay.pages(f"{first} {second}"), [first, second])

    def test_each_speaker_s_line_in_its_colour(self):
        from ui.overlays.captions import SPEAKER_COLOURS

        self.overlay.show_text(f"{coloured('Nereye?', 0)}\n{coloured('Eve.', 2)}")
        self.assertEqual(self.overlay.lines(), ["Nereye?", "Eve."])  # no mark shows
        self.assertEqual(self.overlay.line_colours(), [0, 2])
        self.assertEqual(SPEAKER_COLOURS[1:], ("#ffff00", "#00ffff", "#00ff00"))  # teletext's
        long = coloured("Bu akşam eve gelmeden önce markete uğrayıp ekmek ve süt al " * 4, 1)
        parts = self.overlay.pages(long)
        self.assertGreater(len(parts), 1)
        self.assertEqual({colour_of(part)[0] for part in parts}, {1})  # each part in its colour
        self.overlay.show_text(parts[0])
        self.assertEqual(set(self.overlay.line_colours()), {1})
        self.overlay.grab()  # (painted in its colours)


class _MarkingCaptions(ui_tests.FakeCaptions):
    """The controller in a run from source: Speaker marks offered, Dashes."""

    def __init__(self):
        super().__init__()
        self.mark = DASHES

    def speaker_marks_offered(self):
        return True

    def speaker_marks(self):
        return self.mark

    def speaker_mark_choices(self):
        return [(OFF, "Off"), (DASHES, "Dashes"), (COLOURS, "Colours")]

    def step_speaker_marks(self, delta):
        marks = [OFF, DASHES, COLOURS]
        self.mark = marks[(marks.index(self.mark) + (1 if delta > 0 else -1)) % 3]
        self.changed.emit()


class RowTests(panel_tests.PanelCase):
    """The Subtitles panel's Speaker marks row (beside the Test model row)."""

    make_controls = ui_tests.CaptionRowsTests.make_controls

    def rows(self):
        return self.subtitle_panel.model().rows

    def click_row(self, key):
        index = [row.key for row in self.rows()].index(key)
        self.click(self.subtitle_panel, self.subtitle_panel.row_rect(index).center().toPoint())

    def setUp(self):
        self.captions = _MarkingCaptions()
        super().setUp()
        self.current = panel_tests.CHANNEL

    def marks_row(self):
        from ui.track_controls import AI_MARKS

        return next(row for row in self.rows() if row.key == AI_MARKS)

    def test_the_row_says_the_mark_and_a_click_or_the_keys_step_it(self):
        from ui.track_controls import AI_MARKS

        self.click_button("subtitles")
        row = self.marks_row()
        self.assertEqual((row.text, row.value, row.lit), ("Speaker marks", "Dashes", False))
        self.assertEqual(row.widest, "Colours")
        self.click_row(AI_MARKS)
        self.assertEqual(self.captions.mark, COLOURS)
        self.assertEqual((self.marks_row().value, self.marks_row().lit), ("Colours", True))
        self.subtitle_panel.focus_row([r.key for r in self.rows()].index(AI_MARKS))
        self.subtitle_panel.step(1)  # → on: round to Off
        self.assertEqual(self.captions.mark, OFF)
        self.subtitle_panel.step(-1)
        self.assertEqual(self.captions.mark, COLOURS)

    def test_no_row_where_it_isn_t_offered(self):
        from ui.track_controls import AI_MARKS

        captions: Any = self.captions
        captions.speaker_marks_offered = lambda: False
        self.click_button("subtitles")
        self.assertNotIn(AI_MARKS, [row.key for row in self.rows()])


if __name__ == "__main__":
    unittest.main()
