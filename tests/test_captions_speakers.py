"""Speaker marks (2026-11-08, the owner: "we need diarization otherwise dialogs write single"):
who speaks each caption line (2026-11-09: NVIDIA's streaming Sortformer,
services.captions.sortformer, its front end written as ONNX by services.captions.onnx_graph), how
the captions process tells it and logs it (services.captions.speakers), how a change shows
(Dashes: "- " on a line of its own; Colours: each speaker's own), and its row in a run from
source."""

from array import array
import importlib.util
import logging
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import test_captions_ui as ui_tests  # (as modules: their tests are not run again here)
import test_track_panel as panel_tests

from services.captions import onnx_graph, sortformer, trial_models
from services.captions.sortformer import FRAME_S, HIDDEN, MELS, SPEAKERS, SUB, Settings
from services.captions.speakers import (
    COLOURS,
    DASH,
    DASHES,
    MIN_PART_S,
    OFF,
    RATE,
    WINDOW,
    Colours,
    Part,
    Recent,
    Turn,
    colour_of,
    coloured,
    dashed,
    joined,
    parts_of,
    plain,
)


def told(*runs):
    """Told 10 ms frames from (seconds, speaker) runs: the speaker (0, 1, ...) heard alone, None
    nobody, a tuple of them at once."""
    out = []
    for seconds, who in runs:
        on = set(who) if isinstance(who, tuple) else ({who} if who is not None else set())
        row = [0.9 if k in on else 0.02 for k in range(SPEAKERS)]
        out += [row] * round(seconds / FRAME_S)
    return out


class TurnTests(unittest.TestCase):
    def test_the_log_s_words_for_a_part_s_speaker(self):
        self.assertEqual(Turn(2, True, before=1).tag(), "S2 after S1")  # (S2 heard before too)
        self.assertEqual(Turn(1, False).tag(), "S1")
        self.assertEqual(Turn(1, False, heard=False).tag(), "S1 untold")  # the one before goes on


class RecentTests(unittest.TestCase):
    @staticmethod
    def sound(windows):
        """Whole detector windows, each voiced (1.0) or not (0.0)."""
        return array("f", [1.0 if voiced else 0.0 for voiced in windows for _ in range(WINDOW)])

    def test_a_line_finds_its_windows_verdicts(self):
        recent = Recent(lambda window: window[0] > 0.5)
        step = WINDOW / RATE
        recent.push(10.0, self.sound([True, False, True, True, False, True]))
        self.assertEqual(recent.flags(10.0, 10.0 + 4 * step), ([True, False, True, True], 10.0))
        self.assertEqual(recent.flags(10.0 + 4 * step, 11.0), ([False, True], 10.0 + 4 * step))
        self.assertEqual(recent.flags(30.0, 31.0), ([], 30.0))

    def test_sound_in_blocks_of_any_length_makes_whole_windows(self):
        recent = Recent(None)  # (no detector: all voiced)
        samples = array("f", bytes(4 * 3 * WINDOW))
        recent.push(0.0, samples[:700])
        recent.push(700 / RATE, samples[700:])  # goes on from there
        self.assertEqual(recent.flags(0.0, 1.0)[0], [True] * 3)

    def test_a_jump_starts_the_windows_afresh_and_old_sound_goes(self):
        recent = Recent(None, keep_s=5.0)
        recent.push(0.0, array("f", bytes(4 * (WINDOW + 100))))
        recent.push(3.0, array("f", bytes(4 * WINDOW)))  # a jump: the 100 left over are dropped
        self.assertEqual(recent.flags(0.0, 4.0), ([True, True], 0.0))
        recent.push(20.0, array("f", bytes(4 * WINDOW)))
        self.assertEqual(recent.flags(0.0, 10.0)[0], [])  # older than 5 s before the newest
        self.assertEqual(recent.flags(19.0, 21.0), ([True], 20.0))


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


class ToldTests(unittest.TestCase):
    """What the diarizer's 10 ms frames say about a line: where the speaker changes, who speaks."""

    def test_where_the_speaker_changes_inside_a_line(self):
        frames = told((1.5, 0), (0.3, None), (2.0, 1), (0.2, None), (1.5, 0))
        found = sortformer.changes(frames, 101.0)  # the line's frames from 101 s on
        self.assertEqual(len(found), 2)
        self.assertAlmostEqual(found[0], 102.65, delta=0.01)  # in the gap
        self.assertAlmostEqual(found[1], 104.9, delta=0.01)
        self.assertEqual(sortformer.changes(told((2.0, 1), (1.0, 1)), 0.0), [])

    def test_a_blip_is_no_turn_and_two_at_once_none_either(self):
        frames = told((1.0, 0), (0.1, 1), (1.0, 0), (0.5, (0, 1)), (1.0, 0))
        self.assertEqual(sortformer.changes(frames, 0.0), [])
        self.assertEqual(
            sortformer.labels(told((0.01, 1), (0.01, None), (0.01, (0, 2)))), [2, 0, -1]
        )
        self.assertEqual(sortformer.runs([1, 1, 0, 1, 1, 2, 2, 2], 2), [(1, 0, 5), (2, 5, 8)])

    def test_a_stretch_s_speaker_is_the_one_heard_most_and_none_when_nobody_talks(self):
        self.assertEqual(sortformer.speaker(told((0.4, 0), (1.0, 1), (0.2, None))), 1)
        self.assertIsNone(sortformer.speaker(told((1.0, None))))
        self.assertIsNone(sortformer.speaker([]))
        a, b = [0.2, 0.02, 0.02] + [0.0] * 5, [0.02, 0.2, 0.02] + [0.0] * 5
        unsure = [a, b] * 50  # someone talks, nobody for 15 % of it
        self.assertIsNone(sortformer.speaker(unsure))

    def test_a_short_answer_over_the_silence_after_it_is_told(self):
        # 2026-11-09, the owner: "Kurtuldu mu şimdi?" "Bilemiyorum." both in the asker's colour.
        # Late in a run the model was unsure (about 0.3), and the answer's line went on past it
        answer = [[0.32 if k == 2 else 0.2 if k == 6 else 0.02 for k in range(8)]] * 30
        frames = answer + told((0.5, None))  # 0.3 s said, 0.5 s of silence after it
        self.assertLess(0.32 * 30 / len(frames), sortformer.MIN_SHARE)  # (of all: untold)
        self.assertEqual(sortformer.speaker(frames), 2)

    def test_a_change_goes_to_the_nearest_sentence_end_or_end_of_the_line(self):
        # 2026-11-09, the owner: the colour changed at "geri vereceksin", not where the next
        # speaker began, the next line: a change this near the line's end is that end's
        line = "Benden aldığın 300 milyona geri vereceksin."
        self.assertEqual(len(parts_of(line, 1271.56, 1274.32, changes=[1273.45])), 1)
        two = "Sen gelmeden önce bitti. Ben de çıktım oradan hemen sonra, kimse yoktu."
        parts = parts_of(two, 0.0, 6.0, changes=[3.2])  # nearer the sentence's end than an end
        self.assertEqual([p.text for p in parts], ["Sen gelmeden önce bitti.", two[25:]])
        self.assertEqual((parts[1].start, parts[1].new), (3.2, True))
        long = "bir iki üç dört beş altı yedi sekiz dokuz on on bir on iki on üç on dört"
        cut = parts_of(long, 0.0, 6.0, changes=[3.0])  # no sentence end near: between words
        self.assertEqual(len(cut), 2)
        wide = parts_of(long, 0.0, 6.0, changes=[3.0], change_snap=3.0)  # an end near enough
        self.assertEqual(wide, [Part(long, 0.0, 6.0)])

    def test_parts_split_where_it_changes_and_say_so(self):
        parts = parts_of("Nereye? Eve dönüyorum ben hemen.", 0.0, 3.0, changes=[1.0])
        self.assertEqual([p.text for p in parts], ["Nereye?", "Eve dönüyorum ben hemen."])
        self.assertEqual([p.new for p in parts], [None, True])  # (at the sentence's end near it)
        same = parts_of("Bir şey. Bir şey daha söyleyeyim.", 0.0, 3.0, changes=[])
        self.assertEqual([p.new for p in same], [None, False])  # a sentence's end, the same voice


class CacheTests(unittest.TestCase):
    """The arrival-order speaker cache (transformers' Nemotron3DiarizationSpeakerCache)."""

    SMALL = Settings(chunk=2, right_context=1, fifo=2, cache=16, update_period=2)

    def test_the_chunk_joins_the_fifo_and_what_overflows_it_the_cache_kept_to_its_size(self):
        silence = array("f", [-1.0])
        cache = sortformer.SpeakerCache(silence, self.SMALL)
        heard = {}  # each frame's speaker probabilities: speaker 1 only in frames 2 to 4

        def frame(t):
            heard[t] = [0.9 if k == (1 if 2 <= t <= 4 else 0) else 0.05 for k in range(SPEAKERS)]
            return array("f", [float(t)])

        steps = []
        for step in range(12):
            new = [frame(2 * step), frame(2 * step + 1), frame(2 * step + 2)]  # chunk, right ctx
            inputs = cache.held() + new
            cache.update(inputs, [heard[int(e[0])] if e[0] >= 0 else [0.0] * 8 for e in inputs], 2)
            steps.append((len(cache.embeds), len(cache.fifo)))
            self.assertLessEqual(len(cache.embeds), 16)
        self.assertEqual(steps[:3], [(0, 2), (2, 2), (4, 2)])
        self.assertEqual([e[0] for e in cache.fifo], [22.0, 23.0])  # the newest chunk
        self.assertTrue(cache.compressed)
        self.assertEqual((len(cache.embeds), len(cache.probs)), (16, 16))
        kept = [int(e[0]) for e in cache.embeds if e[0] >= 0]
        self.assertEqual(len(cache.embeds) - len(kept), 8)  # a silence frame for each speaker
        self.assertTrue(any(2 <= t <= 4 for t in kept))  # speaker 1, heard once, stays known
        # each speaker's frames together in the order they came, the speaker heard first first
        self.assertEqual(kept, sorted(kept, key=lambda t: (2 <= t <= 4, t)))
        for embed, probs in zip(cache.embeds, cache.probs):
            if embed[0] < 0:
                self.assertEqual(probs, [0.0] * SPEAKERS)


class VoiceBookTests(unittest.TestCase):
    """Who Sortformer's speakers are across its fresh starts, by prints of their pooled speech."""

    @staticmethod
    def voice(k, seconds):
        """Sound in voice k (the fake print reads it back), `seconds` long."""
        return array("f", [float(k)] * int(seconds * RATE))

    def book(self, **settings):
        def prints(sound):  # a voice's print: its sound's level, as a direction
            k = int(round(sound[len(sound) // 2]))
            return [1.0 if i == k else 0.0 for i in range(4)]

        return sortformer.VoiceBook(prints, probe_s=2.0, enrol_s=10.0, min_enrol_s=5.0, **settings)

    def test_after_a_fresh_start_a_speaker_shows_new_then_its_print_finds_who_it_is(self):
        book = self.book()
        self.assertEqual([book.speaker(0, 0), book.speaker(0, 1)], [0, 1])  # as they come
        book.said(0, 0, self.voice(1, 6.0))  # A, 6 s pooled: remembered
        book.said(0, 1, self.voice(2, 6.0))  # B
        self.assertEqual(book.speaker(1, 0), 2)  # a fresh start: new at once (a fresh colour)
        book.said(1, 0, self.voice(2, 1.0))
        self.assertEqual(book.known(1, 0), 2)  # 1 s: too little to tell
        book.said(1, 0, self.voice(2, 1.0))
        self.assertEqual(book.known(1, 0), 1)  # 2 s: B's voice, B's colour from now on
        book.speaker(1, 1)
        book.said(1, 1, self.voice(3, 2.0))  # nobody heard before: stays new
        self.assertEqual(book.known(1, 1), 3)

    def test_unsure_is_a_fresh_colour_and_a_voice_is_one_speaker_a_stretch(self):
        book = self.book()
        book.speaker(0, 0)
        book.said(0, 0, self.voice(1, 3.0))  # A heard 3 s only: too little to remember
        book.speaker(1, 0)
        book.said(1, 0, self.voice(1, 2.0))
        self.assertEqual(book.known(1, 0), 1)  # not matched to A: new
        book = self.book()
        book.speaker(0, 0)
        book.said(0, 0, self.voice(1, 6.0))
        book.speaker(1, 0)
        book.said(1, 0, self.voice(1, 2.0))  # A
        book.speaker(1, 1)
        book.said(1, 1, self.voice(1, 2.0))  # alike A too, but A is taken in this stretch
        self.assertEqual((book.known(1, 0), book.known(1, 1)), (0, 2))

    def test_without_prints_every_fresh_start_is_new_speakers(self):
        book = sortformer.VoiceBook(None)
        book.speaker(0, 0)
        book.said(0, 0, self.voice(1, 6.0))
        self.assertEqual(book.speaker(1, 0), 1)
        book.said(1, 0, self.voice(1, 6.0))
        self.assertEqual(book.known(1, 0), 1)

    def test_a_frame_by_the_run_s_speakers(self):
        book = sortformer.VoiceBook()
        book.speaker(0, 2)
        self.assertEqual(book.row(0, [0.9, 0.3, 0.7] + [0.0] * 5, 1), [0.7])  # (0, 1 unheard)


class _Tensor:
    def __init__(self, values):
        self.values = values

    def floats(self):
        return array("f", self.values)


class _Api:
    def tensor(self, data, shape, kind):
        return SimpleNamespace(data=data, shape=shape)


class SortformerTests(unittest.TestCase):
    """The streaming around the network (its three ONNX sessions faked): sound in, frames told."""

    SMALL = Settings(chunk=4, right_context=1, fifo=4, cache=32, update_period=4)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        (self.folder / "mel_filters.bin").write_bytes(bytes(4 * MELS * 257))
        (self.folder / "silence_embeds.bin").write_bytes(bytes(4 * HIDDEN))
        self.widest = 0

        self.script: Any = lambda step: 0  # who talks in each step (None: nobody)
        self.steps = 0

        def step(tensor):  # speaker 0 says everything, or as the script says
            self.widest = max(self.widest, tensor.shape[1])
            who = self.script(self.steps)
            self.steps += 1
            return [
                4.0 if k == who else -4.0 for _ in range(tensor.shape[1] * SUB) for k in range(8)
            ]

        runs = {
            sortformer.MEL_GRAPH: lambda t: [0.0] * (t.shape[0] * MELS),
            "embed.onnx": lambda t: [0.0] * (t.shape[1] // SUB * HIDDEN),
            "step_int8.onnx": step,
        }

        def session(api, path, threads):
            run = runs[Path(path).name]
            return SimpleNamespace(run=lambda inputs: [_Tensor(run(inputs[0]))])

        from services.captions import onnx_c

        fakes: dict[str, Any] = {
            "Api": lambda library: _Api(),
            "find_library": lambda: "lib",
            "Session": session,
        }
        for name, value in fakes.items():
            patcher = mock.patch.object(onnx_c, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.diarizer = sortformer.Sortformer(str(self.folder), self.SMALL)

    def test_the_front_end_is_written_beside_the_model_once(self):
        graph = self.folder / sortformer.MEL_GRAPH
        self.assertEqual(graph.read_bytes(), sortformer.mel_graph([0.0] * (MELS * 257)))
        before = graph.stat().st_mtime_ns
        sortformer.Sortformer(str(self.folder), self.SMALL)
        self.assertEqual(graph.stat().st_mtime_ns, before)  # the same: not written again

    def test_sound_in_blocks_of_any_length_is_told_a_chunk_at_a_time(self):
        token = self.diarizer.reset(100.0)
        self.diarizer.push(100.0, array("f", bytes(4 * 10 * RATE)), token)
        # 999 mel frames, 124 embeddings, 30 chunks of 4 told (each needs one ahead): 9.6 s
        self.assertAlmostEqual(self.diarizer.covered(), 109.6, places=6)
        self.assertLessEqual(self.widest, 32 + 4 + 4 + 1)  # cache, fifo, chunk, right context
        whole = self.diarizer.frames(100.0, 110.0)
        token = self.diarizer.reset(100.0)
        for k in range(20):
            self.diarizer.push(100.0 + k / 2, array("f", bytes(4 * RATE // 2)), token)
        self.assertAlmostEqual(self.diarizer.covered(), 109.6, places=6)
        self.assertEqual(self.diarizer.frames(100.0, 110.0), whole)
        self.assertEqual(len(self.diarizer.frames(101.0, 102.0)), 100)
        self.assertEqual(sortformer.speaker(self.diarizer.frames(101.0, 102.0)), 0)
        self.assertAlmostEqual(self.diarizer.time_of(0, 101.004), 101.01)
        self.assertAlmostEqual(self.diarizer.time_of(0, 99.0), 100.0)  # (before its first)

    def test_sound_after_a_gap_or_an_overlap_keeps_its_place(self):
        # 2026-11-09, the owner's live test: Speaker marks off for a moment let sound go, and
        # what came after was told as said earlier
        def covered(*blocks):
            token = self.diarizer.reset(100.0)
            for at, seconds in blocks:
                self.diarizer.push(at, array("f", bytes(4 * round(seconds * RATE))), token)
            return self.diarizer.covered()

        whole = covered((100.0, 4.5))
        self.assertAlmostEqual(covered((100.0, 2.0), (102.5, 2.0)), whole)  # silence between
        self.assertAlmostEqual(covered((100.0, 2.0), (101.5, 3.0)), whole)  # read once
        self.assertGreater(sortformer.JUMP_S, 2.0)  # (a lost 2 s segment is a gap)

    def test_a_jump_starts_afresh_there_and_the_voices_are_kept(self):
        token = self.diarizer.reset(100.0)
        self.diarizer.push(100.0, array("f", bytes(4 * 10 * RATE)), token)
        book, stretch = self.diarizer.book, self.diarizer._stretch
        self.diarizer.push(200.0, array("f", bytes(4 * RATE)), token)  # 90 s on
        self.assertAlmostEqual(self.diarizer.covered(), 200.64, places=6)
        self.assertEqual((self.diarizer.book, self.diarizer._stretch), (book, stretch + 1))
        self.assertEqual(self.diarizer.frames(100.0, 110.0), [])
        self.diarizer.reset(0.0)  # a new run: the voices afresh too
        self.assertIsNot(self.diarizer.book, book)

    def test_after_a_pause_it_starts_afresh_and_a_voice_print_keeps_who_is_who(self):
        # 2026-11-09, the owner ("try 2"): late in a run Sortformer told speakers apart worse
        def told_after(second_voice):
            # speaker 0 for 19 steps (6.1 s), nobody for 3 (0.96 s), then its speaker 0 again
            self.steps, self.script = 0, lambda step: None if 19 <= step < 22 else 0

            def prints(sound):
                return [1.0, 0.0] if sum(sound) > 0 else [0.0, 1.0]

            diarizer = sortformer.Sortformer(
                str(self.folder), self.SMALL, prints=prints, pause_s=0.5, min_stretch_s=0.0
            )
            first = array("f", [0.1] * int(6.6 * RATE))  # (the voice in its sound's sign)
            then = array("f", [-0.1 if second_voice else 0.1] * int(4.4 * RATE))
            token = diarizer.reset(0.0)
            diarizer.push(0.0, first + then, token)
            return diarizer

        same = told_after(second_voice=False)
        self.assertEqual(same._stretch, 1)  # started afresh after the pause
        self.assertEqual(sortformer.speaker(same.frames(9.5, 10.5)), 0)  # the same voice: 0
        other = told_after(second_voice=True)
        self.assertEqual(sortformer.speaker(other.frames(1.0, 5.0)), 0)
        self.assertEqual(sortformer.speaker(other.frames(9.5, 10.5)), 1)  # another voice: 1
        self.assertEqual(len(other.frames(9.5, 10.5)[0]), 2)  # (by the run's speakers)

    def test_a_stopped_run_s_sound_is_let_go(self):
        old = self.diarizer.reset(100.0)
        new = self.diarizer.reset(300.0)
        self.assertEqual(self.diarizer.token, new)
        self.diarizer.push(300.0, array("f", bytes(4 * 2 * RATE)), old)
        self.assertEqual(self.diarizer.covered(), 300.0)
        self.diarizer.push(300.0, array("f", bytes(4 * 2 * RATE)), new)
        self.assertGreater(self.diarizer.covered(), 300.0)


def _fields(data):
    """A protobuf message's fields, in order: (number, int, bytes or float)."""

    def varint(i):
        value = shift = 0
        while True:
            byte = data[i]
            value |= (byte & 0x7F) << shift
            i, shift = i + 1, shift + 7
            if not byte & 0x80:
                return value, i

    out, i = [], 0
    while i < len(data):
        key, i = varint(i)
        if key & 7 == 0:
            value, i = varint(i)
        elif key & 7 == 2:
            size, i = varint(i)
            value, i = data[i : i + size], i + size
        else:
            value, i = struct.unpack("<f", data[i : i + 4])[0], i + 4
        out.append((key >> 3, value))
    return out


class GraphTests(unittest.TestCase):
    """The front end as an ONNX model written by hand (onnx.proto's fields)."""

    def test_a_model_s_fields(self):
        node = onnx_graph.Node("Add", ["x", "w"], ["y"], floats={"alpha": 0.5}, ints={"axes": [-1]})
        data = onnx_graph.model(
            [node], [("x", ["t", 2])], [("y", ["t", 2])], [("w", [2], array("f", [1, 2]))], "g"
        )
        top = _fields(data)
        self.assertEqual(top[:2], [(1, onnx_graph.IR_VERSION), (2, b"qitv")])
        self.assertEqual(_fields(top[2][1]), [(1, b""), (2, 13)])  # the default opset, 13
        graph = _fields(top[3][1])
        self.assertEqual([number for number, _ in graph], [1, 2, 5, 11, 12])
        n = _fields(graph[0][1])
        self.assertEqual(n[:5], [(1, b"x"), (1, b"w"), (2, b"y"), (3, b"add_0"), (4, b"Add")])
        self.assertEqual(_fields(n[5][1]), [(1, b"alpha"), (2, 0.5), (20, 1)])  # FLOAT
        self.assertEqual(_fields(n[6][1]), [(1, b"axes"), (8, 2**64 - 1), (20, 7)])  # INTS
        weight = _fields(graph[2][1])
        self.assertEqual(weight, [(1, 2), (2, 1), (8, b"w"), (9, struct.pack("<2f", 1, 2))])
        name, kind = _fields(graph[3][1])
        self.assertEqual(name, (1, b"x"))
        tensor = _fields(_fields(kind[1])[0][1])
        self.assertEqual(tensor[0], (1, 1))  # float
        self.assertEqual(_fields(tensor[1][1]), [(1, _fields_of_dim("t")), (1, _fields_of_dim(2))])

    def test_the_front_end_s_weights(self):
        window = sortformer.hann()
        self.assertEqual((len(window), window[55], window[56], window[455]), (512, 0.0, 0.0, 0.0))
        self.assertAlmostEqual(window[56 + 199], 1.0, delta=1e-4)  # symmetric: 400 long
        self.assertAlmostEqual(window[56 + 399], 0.0, places=12)

    @unittest.skipUnless(importlib.util.find_spec("sherpa_onnx"), "no sherpa-onnx runtime here")
    def test_onnxruntime_runs_it_as_the_sums_say(self):
        from services.captions import onnx_c

        bins = 257
        filters = [0.0] * (MELS * bins)
        for m in range(MELS):
            filters[m * bins + 2 * m] = 1.0  # band m: bin 2m alone
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mel.onnx"
            path.write_bytes(sortformer.mel_graph(filters))
            api = onnx_c.Api(onnx_c.find_library())
            session = onnx_c.Session(api, path, 1)
            frame = [math.cos(2 * math.pi * 10 * n / 512) + 0.1 for n in range(512)]
            out = session.run([api.tensor(array("f", frame), (1, 512), onnx_c.FLOAT)])
            features = out[0].floats()
        window = sortformer.hann()
        for m in (0, 3, 5, 6, 40):
            re = sum(
                window[n] * frame[n] * math.cos(2 * math.pi * n * 2 * m / 512) for n in range(512)
            )
            im = sum(
                window[n] * frame[n] * math.sin(2 * math.pi * n * 2 * m / 512) for n in range(512)
            )
            expected = math.log(re * re + im * im + sortformer.GUARD)
            self.assertAlmostEqual(features[m], expected, delta=1e-3 * max(1.0, abs(expected)))


def _fields_of_dim(dim):
    """A TensorShapeProto.Dimension's bytes: a size (1) or a name (2)."""
    if isinstance(dim, str):
        return bytes([0x12, len(dim)]) + dim.encode()
    return bytes([0x08, dim])


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


class _Diarizer:
    """The diarizer's face: who speaks when ((from, to, speaker) stretches of the timeline),
    told up to `told_to`."""

    def __init__(self, turns=(), told_to=1e9):
        self.turns, self.told_to = list(turns), told_to
        self.token = 7
        self.heard: list[tuple] = []
        self.fails = False

    def reset(self, at):
        self.heard.append(("reset", at))
        self.token += 1
        return self.token

    def push(self, at, samples, token=None):
        if self.fails:
            raise RuntimeError("no")
        self.heard.append(("push", at, len(samples), token))

    def covered(self):
        return self.told_to

    def frames(self, start, end):
        first = math.ceil(start / FRAME_S - 1e-9)
        out = []
        for f in range(first, math.ceil(end / FRAME_S - 1e-9)):
            who = next((k for a, b, k in self.turns if a <= f * FRAME_S < b), None)
            out.append([0.9 if k == who else 0.02 for k in range(SPEAKERS)])
        return out

    def time_of(self, offset, start):
        return (math.ceil(start / FRAME_S - 1e-9) + offset) * FRAME_S


def _bare_run(model, level=logging.INFO):
    """A captions run without its stream, its sound or its recognizing threads."""
    from services.captions.worker import _Run

    run: Any = object.__new__(_Run)
    run.run, run.model = 3, model
    run.sent = []
    run.link = SimpleNamespace(send=run.sent.append)
    run.recent, run._line_level, run._writes = None, level, "ufak"
    run.stopped, run.viewer = threading.Event(), [0.0]
    run._diary = run._waiting = run._said_by = run._diary_token = None
    run._diarizer_broken = False
    return run


def _until(test, done, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not done() and time.monotonic() < deadline:
        time.sleep(0.01)
    test.assertTrue(done())


class ProcessTests(unittest.TestCase):
    """The captions process tells each line's speakers, sends it and logs who spoke."""

    def run_with(self, diarizer=None, on=True):
        model = SimpleNamespace(diarizer=diarizer, speakers_on=on, speaker_voiced=lambda w: True)
        run = _bare_run(model)
        if diarizer is not None:
            run._diary_token = diarizer.token  # (its thread reset it for this run)
        self.addCleanup(run._wake)
        self.addCleanup(run.stopped.set)
        return run

    def test_a_line_by_another_speaker_goes_out_as_a_change_and_the_log_says_who(self):
        run = self.run_with(_Diarizer([(10.0, 12.0, 0), (12.0, 14.0, 1)]))
        with self.assertLogs("services.captions.worker", logging.INFO) as logs:
            run._write("ufak", "nereye", "Nereye?", 10.0, 12.0)
            run._write("ufak", "eve", "Eve.", 12.0, 14.0)
            _until(self, lambda: len(run.sent) == 2)
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
                "AI captions: [ufak] heard 'eve', shows 'Eve.' · S2 after S1",
            ],
        )

    def test_a_line_of_two_speakers_goes_out_in_parts_each_told(self):
        run = self.run_with(_Diarizer([(10.0, 12.2, 0), (12.5, 14.5, 1)]))
        with self.assertLogs("services.captions.worker", logging.INFO) as logs:
            run._write("ufak", "x", "Nereye gidiyorsun? Eve dönüyorum.", 10.0, 14.5)
            _until(self, lambda: run.sent)
        [message] = run.sent
        self.assertEqual(
            message[:7], ("caption", 3, 10.0, 14.5, "Nereye gidiyorsun? Eve dönüyorum.", 0, 1)
        )
        (first, a, b, turn1, s1), (second, c, d, turn2, s2) = message[7]
        self.assertEqual(
            (first, turn1, s1, second, turn2, s2),
            ("Nereye gidiyorsun?", 0, 1, "Eve dönüyorum.", 1, 2),
        )
        self.assertEqual((a, b, c, d), (10.0, b, b, 14.5))
        self.assertAlmostEqual(b, 12.35, delta=0.02)  # where it changes, in the gap
        self.assertIn("· S1 | S2 after S1", logs.records[0].getMessage())

    def test_a_part_nobody_is_heard_in_goes_on_with_the_speaker_before(self):
        run = self.run_with(_Diarizer([(10.0, 12.0, 1)]))
        with self.assertLogs("services.captions.worker", logging.INFO) as logs:
            run._write("ufak", "a", "Evet.", 10.0, 12.0)
            run._write("ufak", "b", "Hı.", 13.0, 14.0)  # (nobody, as the diarizer heard it)
            _until(self, lambda: len(run.sent) == 2)
        self.assertEqual(run.sent[1], ("caption", 3, 13.0, 14.0, "Hı.", 0, 2))
        self.assertTrue(logs.records[1].getMessage().endswith("· S2 untold"))

    def test_a_line_waits_for_its_speakers_to_be_told_or_for_the_picture_to_near_it(self):
        diarizer = _Diarizer([(10.0, 30.0, 0)], told_to=11.0)
        run = self.run_with(diarizer)
        run._write("ufak", "a", "Bir.", 10.0, 12.0)
        time.sleep(0.2)
        self.assertEqual(run.sent, [])  # told up to 11 s only
        diarizer.told_to = 12.0
        _until(self, lambda: len(run.sent) == 1)
        run.viewer[0] = 15.0
        run._write("ufak", "b", "İki.", 20.0, 22.0)
        run._write("ufak", "c", "Üç.", 22.0, 23.0)
        time.sleep(0.2)
        self.assertEqual(len(run.sent), 1)
        run.viewer[0] = 19.1  # the picture 0.9 s from it: out as it is; the next waits on
        _until(self, lambda: len(run.sent) == 2)
        time.sleep(0.2)
        self.assertEqual(len(run.sent), 2)
        run.viewer[0] = 21.5
        _until(self, lambda: len(run.sent) == 3)
        self.assertEqual([m[4] for m in run.sent], ["Bir.", "İki.", "Üç."])

    def test_another_run_s_speakers_never_tell_a_line(self):
        diarizer = _Diarizer([(10.0, 12.0, 0)])
        run = self.run_with(diarizer)
        run._diary_token = None  # its diarizer not started for this run yet
        run.viewer[0] = 5.0
        run._write("ufak", "a", "Bir.", 10.0, 12.0)
        time.sleep(0.2)
        self.assertEqual(run.sent, [])
        run._diary_token = diarizer.token - 1  # a newer run's now
        run.viewer[0] = 9.5
        _until(self, lambda: run.sent)
        self.assertEqual(run.sent, [("caption", 3, 10.0, 12.0, "Bir.", 0, 0)])  # untold

    def test_the_run_s_sound_goes_to_the_diarizer_in_its_own_thread_from_its_start(self):
        diarizer = _Diarizer()
        run = self.run_with(diarizer)
        run._diary_token = None
        run._hold_for_speakers(10.0, array("f", bytes(4 * RATE)))
        run._hold_for_speakers(11.0, array("f", bytes(4 * RATE)))
        _until(self, lambda: len(diarizer.heard) == 3)
        token = diarizer.token
        self.assertEqual(
            diarizer.heard,
            [("reset", 10.0), ("push", 10.0, RATE, token), ("push", 11.0, RATE, token)],
        )
        self.assertEqual(run._diary_token, token)
        self.assertEqual(len(run.recent.flags(10.0, 12.0)[0]), 2 * RATE // WINDOW)  # kept too

    def test_a_failing_diarizer_lets_the_run_s_lines_go_out_untold_at_once(self):
        diarizer = _Diarizer([(10.0, 12.0, 0)])
        diarizer.fails = True
        run = self.run_with(diarizer)
        with self.assertLogs("services.captions.worker", logging.WARNING) as logs:
            run._hold_for_speakers(10.0, array("f", bytes(4 * RATE)))
            _until(self, lambda: run._diarizer_broken)
        self.assertEqual(
            logs.output[0].split(":", 2)[2], "AI captions: the diarizer failed: RuntimeError"
        )
        run._write("ufak", "a", "Bir.", 10.0, 12.0)
        _until(self, lambda: run.sent)
        self.assertEqual(run.sent, [("caption", 3, 10.0, 12.0, "Bir.", 0, 0)])

    def test_a_piece_s_own_voice_detector_times_its_parts(self):
        run = self.run_with(on=False)
        voiced = PartsTests.windows((2.0, True), (0.4, False), (1.5, True))
        run._write("ufak", "x", "Bir iki üç dört. Beş altı yedi.", 20.0, 23.9, voiced=voiced)
        [message] = run.sent
        self.assertEqual([part[0] for part in message[7]], ["Bir iki üç dört.", "Beş altı yedi."])
        self.assertAlmostEqual(message[7][0][2], 22.2, delta=0.05)
        self.assertEqual([part[3:] for part in message[7]], [(0, 0), (0, 0)])  # none told

    def test_with_speakers_unmarked_no_sound_is_kept_and_no_line_is_told(self):
        diarizer = _Diarizer([(10.0, 12.0, 0)])
        run = self.run_with(diarizer, on=False)
        run._hold_for_speakers(10.0, array("f", [0.1] * RATE))
        self.assertIsNone(run.recent)
        with self.assertLogs("services.captions.worker", logging.INFO) as logs:
            run._write("ufak", "eve", "Eve.", 10.0, 11.0, until=11.5)
        self.assertEqual(run.sent, [("caption", 3, 10.0, 11.5, "Eve.", 0, 0)])  # at once
        self.assertEqual(
            logs.records[0].getMessage(), "AI captions: [ufak] heard 'eve', shows 'Eve.'"
        )
        self.assertEqual(diarizer.heard, [])

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
        run = _bare_run(SimpleNamespace(speakers_on=False), level=logging.DEBUG)
        with self.assertLogs("services.captions.worker", logging.DEBUG) as logs:
            run._write("ufak", "eve", "Eve.", 10.0, 11.0)
        self.assertEqual([r.levelno for r in logs.records], [logging.DEBUG])

    def test_the_diarizer_loads_in_its_own_thread_and_turns_off(self):
        from test_captions_trials import _fake_sherpa

        made = []

        class Diarizer:
            def __init__(self, folder, prints=None):
                made.append(folder)
                self.prints = prints

        from services.captions.worker import _Model

        extractors = []

        class Config:
            def __init__(self, model, num_threads):
                self.model = model

            def validate(self):
                return True

        class Extractor:
            def __init__(self, config):
                extractors.append(config.model)

        sherpa: Any = _fake_sherpa([])
        sherpa.SpeakerEmbeddingExtractorConfig = Config
        sherpa.SpeakerEmbeddingExtractor = Extractor
        spec = {
            "runtime": None,
            "model": {"conv_frontend": "c", "encoder": "e", "decoder": "d", "tokenizer": "t"},
            "vad": "vad.onnx",
            "diarizer": "/models/nemotron-3-diarization",
            "voice_prints": "/models/eres2net.onnx",
        }
        with mock.patch.dict(sys.modules, {"sherpa_onnx": sherpa}):
            with mock.patch.object(sortformer, "Sortformer", Diarizer):
                model = _Model(spec)
                _until(self, lambda: model.diarizer is not None)
        self.assertEqual(made, ["/models/nemotron-3-diarization"])
        self.assertEqual(extractors, ["/models/eres2net.onnx"])  # its voice prints' model
        self.assertTrue(callable(model.diarizer.prints))
        self.assertTrue(model.speakers_on)
        self.assertFalse(model.speaker_voiced(array("f", bytes(4 * WINDOW))))
        model.use_speakers(None)
        self.assertFalse(model.speakers_on)
        model.use_speakers("/models/nemotron-3-diarization")  # on again: loaded already
        self.assertEqual((model.speakers_on, len(made)), (True, 1))

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

    @staticmethod
    def speaker_files():
        from services.captions import models

        return str(models.diarizer_folder()), str(models.print_file())

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
        self.dialogue()
        self.assertEqual(self.captions._cues[0], (10.0, 13.0, "Nereye gidiyorsun? Eve."))
        self.assertTrue(self.captions._speakers_off.isActive())  # the process told a moment on
        self.captions._speakers_off.timeout.emit()
        self.assertEqual(self.sent[-1], ("speakers", None, None))
        self.captions.set_speaker_marks(DASHES)
        self.assertEqual(self.sent[-1], ("speakers", *self.speaker_files()))

    def test_a_step_through_off_leaves_the_speakers_told_and_the_picture_where_it_is(self):
        # 2026-11-09, the owner's live test: Dashes, Off, Colours in half a second
        live: Any = self.window.live_session
        captions: Any = self.captions
        captions._floored, self.captions._tier = live, "small"
        self.captions.set_speaker_marks(COLOURS)
        self.assertEqual(live.floor, 10.0)  # 8 s, and SPEAKER_LEAD_S more for the speakers
        sent = len(self.sent)
        self.captions.set_speaker_marks(OFF)
        self.captions.set_speaker_marks(DASHES)
        self.assertFalse(self.captions._speakers_off.isActive())
        self.assertEqual(self.sent[sent:], [("speakers", *self.speaker_files())])
        self.assertEqual(live.floor, 10.0)
        self.captions.set_speaker_marks(OFF)
        self.captions._speakers_off.timeout.emit()  # off for good: the model's own distance
        self.assertEqual((live.floor, live.shifted), (8.0, False))  # steered back to it

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
            self.assertIsNone(self.captions._diarizer_folder())  # the process loads none

    def test_the_process_loads_the_diarizer_with_the_speech_model(self):
        from services.captions import models

        loaded = []
        captions: Any = self.captions
        captions._process.start = lambda load, detailed_log=False: loaded.append(load)
        self.captions._tier = "small"
        self.captions._load_model()
        self.assertEqual(loaded[0]["diarizer"], str(models.diarizer_folder()))
        self.assertEqual(loaded[0]["voice_prints"], str(models.print_file()))
        self.assertNotIn("speaker", loaded[0])
        self.config["ai_captions_speaker_marks"] = OFF
        self.captions._load_model()
        self.assertIsNone(loaded[1]["diarizer"])

    def test_the_diarizer_downloads_once_unasked_and_the_process_takes_it(self):
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
        self.assertEqual(self.sent[-1], ("speakers", *self.speaker_files()))
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

    def test_the_diarizer_is_pinned_in_its_own_folder(self):
        from services.captions import models

        root = Path(self.tmp.name)
        *downloads, prints = models.needed(models.SPEAKER_KEY, root)
        self.assertEqual(
            [d.file.path for d in downloads],
            ["embed.onnx", "step_int8.onnx", "mel_filters.bin", "silence_embeds.bin"],
        )
        self.assertEqual({d.folder for d in downloads}, {root / models.DIARIZER_FOLDER})
        self.assertEqual(models.diarizer_folder(root), root / models.DIARIZER_FOLDER)
        self.assertEqual(sum(d.file.size for d in downloads), 105561475)  # some 106 MB
        self.assertTrue(all(len(d.file.sha256) == 64 for d in downloads))
        self.assertTrue(all("/resolve/46642c09" in d.file.url for d in downloads))  # pinned
        self.assertEqual(prints.target, models.print_file(root))  # the voice prints' (26 MB)
        self.assertEqual(prints.file.size, 26485263)
        self.assertNotIn(models.SPEAKER_KEY, models.SPECIALISTS)  # not in the first question
        self.assertFalse(models.installed(models.SPEAKER_KEY, root))


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
