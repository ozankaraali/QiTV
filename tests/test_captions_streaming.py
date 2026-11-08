from array import array
import logging
import queue
import threading
from types import SimpleNamespace
import unittest

from services.captions.streaming import (
    HOLDBACK_S,
    LATE_S,
    MARGIN_S,
    MAX_LINE_CHARS,
    SETTLED_S,
    Anchors,
    Lines,
    StreamWriter,
    words_of,
)


def tokens_of(text: str, start: float = 0.0, step: float = 0.3) -> tuple[list[str], list[float]]:
    """A sentence as a recognizer's tokens: one per word, each with a leading space."""
    tokens = [" " + w for w in text.split()]
    return tokens, [start + i * step for i in range(len(tokens))]


class WordsTest(unittest.TestCase):
    def test_marks_and_pieces_join_their_word(self) -> None:
        words = words_of(
            [" Bu", "gün", ",", " gel", "di", "."], [0, 0.1, 0.2, 0.5, 0.6, 0.7], 10.0, -1.0
        )
        self.assertEqual([w.text for w in words], ["Bugün,", "geldi."])
        self.assertEqual((words[0].start, words[1].end), (10.0, 10.7))


class LinesTest(unittest.TestCase):
    def test_a_sentence_end_ends_a_line_once_it_is_settled(self) -> None:
        lines = Lines()
        tokens, times = tokens_of("Nereye gidiyorsun? Eve dönüyorum")
        last = times[-1]
        # not settled yet: nothing
        self.assertEqual(lines.take(tokens, times, 0.0, last, False), [])
        found = lines.take(tokens, times, 0.0, last + SETTLED_S + 0.5, False)
        self.assertEqual([t for t, _, _ in found], ["Nereye gidiyorsun?"])
        # the rest waits for its end, or the pause
        self.assertEqual(lines.take(tokens, times, 0.0, last + 5, False), [])
        rest = lines.take(tokens, times, 0.0, last + 5, True)
        self.assertEqual([t for t, _, _ in rest], ["Eve dönüyorum."])  # the pause ends it

    def test_two_sentences_are_two_lines(self) -> None:
        lines = Lines()
        tokens, times = tokens_of("Geldi. Gitti.")
        found = lines.take(tokens, times, 5.0, 100.0, False)
        self.assertEqual([t for t, _, _ in found], ["Geldi.", "Gitti."])
        self.assertEqual(found[0][1], 5.0)

    def test_a_long_line_is_cut_at_its_last_comma(self) -> None:
        lines = Lines()
        text = "Bunu söylerken, " + " ".join(["kelime"] * 20)
        tokens, times = tokens_of(text, step=0.1)
        found = lines.take(tokens, times, 0.0, 100.0, False)
        self.assertEqual(found[0][0], "Bunu söylerken,")
        self.assertTrue(all(len(t) <= MAX_LINE_CHARS for t, _, _ in found))

    def test_a_line_cut_for_length_gets_no_full_stop(self) -> None:
        lines = Lines()
        tokens, times = tokens_of("Bunu söylerken, " + " ".join(["kelime"] * 20), step=0.1)
        found = lines.take(tokens, times, 0.0, 100.0, False)
        self.assertEqual(found[0][0], "Bunu söylerken,")
        self.assertFalse(found[1][0].endswith("."))

    def test_a_long_line_without_a_comma_is_cut_at_a_word(self) -> None:
        lines = Lines()
        tokens, times = tokens_of(" ".join(["kelime"] * 30), step=0.1)
        found = lines.take(tokens, times, 0.0, 100.0, False)
        self.assertTrue(found)
        self.assertTrue(all(len(t) <= MAX_LINE_CHARS for t, _, _ in found))

    def test_a_pause_writes_everything_and_the_next_segment_starts_over(self) -> None:
        lines = Lines()
        tokens, times = tokens_of("tamam o zaman")
        self.assertEqual(
            [t for t, _, _ in lines.take(tokens, times, 0.0, 0.5, True)], ["tamam o zaman."]
        )
        lines.reset()
        tokens, times = tokens_of("Yarın görüşürüz.")
        self.assertEqual(
            [t for t, _, _ in lines.take(tokens, times, 3.0, 50.0, False)], ["Yarın görüşürüz."]
        )

    def test_a_word_split_anew_by_beam_search_is_not_written_twice(self) -> None:
        lines = Lines()
        # a line is written: "Futbol severler." (the first word as one token)
        tokens = [" Futbol", " sever", "ler", "."]
        times = [0.0, 0.5, 0.6, 0.7]
        out = lines.take(tokens, times, 0.0, 10.0, False)
        self.assertEqual([t for t, _, _ in out], ["Futbol severler."])
        # beam search now splits the written first word into two tokens, and more words come:
        # counted by tokens, the next line would start inside "severler" ("ler. Gelsin.")
        tokens2 = [" Fut", "bol", " sever", "ler", ".", " Gelsin", "."]
        times2 = [0.0, 0.05, 0.5, 0.6, 0.7, 2.0, 2.3]
        out = lines.take(tokens2, times2, 0.0, 10.0, False)
        self.assertEqual([t for t, _, _ in out], ["Gelsin."])
        self.assertEqual(lines.take(tokens2, times2, 0.0, 20.0, True), [])

    # 2026-11-08, the owner's live test: under dialogue with music beneath it nobody paused, and
    # a line waited for its sentence's end or a pause: it came after the picture reached it.
    SAID = "Geçen hafta sonu arkadaşlarla birlikte deniz kenarında uzun bir yürüyüş yaptık"

    def test_a_line_the_picture_is_about_to_reach_is_written_with_what_is_heard(self) -> None:
        tokens, times = tokens_of(self.SAID, start=10.0)  # 11 words, 10.0 .. 13.0 s
        frontier = times[-1] + SETTLED_S / 2  # the newest words not settled, no sentence end
        self.assertEqual(Lines().take(tokens, times, 0.0, frontier, False), [])  # it waits
        not_yet = Lines().take(tokens, times, 0.0, frontier, False, due=9.0)
        self.assertEqual(not_yet, [])  # the picture is not near its first word yet
        lines = Lines()
        found = lines.take(tokens, times, 0.0, frontier, False, due=10.0)
        self.assertEqual(len(found), 1)
        text, start, end = found[0]
        words = self.SAID.split()
        # the words heard but the last one (it may still change), with no mark added
        heard = [w for w, t in zip(words, times) if t <= frontier - HOLDBACK_S][: len(words) - 1]
        self.assertEqual(text, " ".join(heard))
        self.assertEqual(start, 10.0)
        # the rest is the next line's, once its sentence ends or the pause comes
        rest = lines.take(tokens, times, 0.0, frontier + 5, True)
        self.assertEqual([t for t, _, _ in rest], [" ".join(words[len(heard) :]) + "."])

    def test_a_scrap_waits_for_more_words_until_the_picture_is_past_it(self) -> None:
        # a picture near the live edge: everything heard is due at once, and lines came a word
        # each; a word or two waits a moment for more
        tokens, times = tokens_of("şimdi buraya gel bakalım hemen", start=10.0, step=0.5)
        frontier = 10.1 + HOLDBACK_S  # "şimdi" heard, "buraya" too new to write
        self.assertEqual(Lines().take(tokens, times, 0.0, frontier, False, due=10.0), [])
        late = 10.0 + MARGIN_S + LATE_S  # the picture is LATE_S past its first word
        found = Lines().take(tokens, times, 0.0, frontier, False, due=late)
        self.assertEqual([t for t, _, _ in found], ["şimdi"])
        frontier = 11.5 + HOLDBACK_S  # four words heard, 1.5 s of them: a line to read
        found = Lines().take(tokens, times, 0.0, frontier, False, due=10.0)
        self.assertEqual([t for t, _, _ in found], ["şimdi buraya gel bakalım"])

    def test_a_short_sentence_is_no_scrap(self) -> None:
        tokens, times = tokens_of("Evet. Tamam o zaman", start=10.0, step=0.3)
        found = Lines().take(tokens, times, 0.0, 10.9 + HOLDBACK_S, False, due=10.0)
        self.assertEqual([t for t, _, _ in found], ["Evet."])

    def test_a_line_written_at_the_last_moment_keeps_to_the_length_rules(self) -> None:
        text = "Bunu söylerken, " + " ".join(["kelime"] * 20)
        tokens, times = tokens_of(text, step=0.1)
        found = Lines().take(tokens, times, 0.0, times[-1] + 0.6, False, due=0.0)
        self.assertEqual(found[0][0], "Bunu söylerken,")  # cut at its comma, as ever
        self.assertTrue(all(len(t) <= MAX_LINE_CHARS for t, _, _ in found))


class _Recognizer:
    """A streaming recognizer's face: `words` (text, second of sound fed) are decoded 1.2 s
    behind the sound fed; no pause is ever heard (dialogue over music)."""

    def __init__(self, words: list[tuple[str, float]]) -> None:
        self.words = words

    def create_stream(self):
        stream = SimpleNamespace(fed=0.0)
        stream.accept_waveform = lambda rate, samples: setattr(
            stream, "fed", stream.fed + len(samples) / rate
        )
        return stream

    def is_ready(self, stream) -> bool:
        return False

    def decode_stream(self, stream) -> None:
        pass

    def get_result_all(self, stream):
        heard = [(w, t) for w, t in self.words if t <= stream.fed - 1.2]
        return SimpleNamespace(
            tokens=[" " + w for w, _ in heard], timestamps=[t for _, t in heard], start_time=0.0
        )

    def is_endpoint(self, stream) -> bool:
        return False

    def reset(self, stream) -> None:
        pass


def _live(writer: StreamWriter, viewer: list, clock: list, lead: float, until: float):
    """Sound placed from timeline 0 at live pace, 0.1 s blocks, the picture `lead` seconds
    behind it; each line with where the picture was when it came out."""
    out = []
    t = 0.0
    while t < until:
        clock[0] = t
        viewer[0] = max(0.0, t - lead)
        for line in writer.feed(t, array("f", bytes(4 * 1600))):
            out.append((line, viewer[0]))
        t = round(t + 0.1, 6)
    return out


class StreamWriterTest(unittest.TestCase):
    WORDS = [(w, 10.0 + 0.4 * i) for i, w in enumerate(LinesTest.SAID.split())]

    def test_a_line_is_out_before_the_picture_reaches_it_with_nobody_pausing(self) -> None:
        viewer, clock = [0.0], [0.0]
        writer = StreamWriter(_Recognizer(self.WORDS), viewer, clock=lambda: clock[0])
        out = _live(writer, viewer, clock, lead=8.0, until=20.0)
        self.assertTrue(out)
        (text, start, _), position = out[0]
        self.assertTrue(text.startswith("Geçen hafta sonu"))
        self.assertAlmostEqual(start, 10.0)
        self.assertLessEqual(position, start - MARGIN_S + 0.11)  # out in time for the picture
        written = " ".join(line[0] for line, _ in out).split()
        self.assertEqual(written, LinesTest.SAID.split()[: len(written)])  # each word once

    def test_without_the_picture_it_waits_for_a_sentence_end_or_a_pause(self) -> None:
        viewer, clock = [0.0], [0.0]
        writer = StreamWriter(_Recognizer(self.WORDS))
        self.assertEqual(_live(writer, viewer, clock, lead=8.0, until=20.0), [])


class StreamLogTest(unittest.TestCase):
    """2026-11-08, the owner: "Write to logs which one writes"."""

    def test_each_streamed_line_is_logged_by_the_model_that_wrote_it(self) -> None:
        from services.captions.worker import TURKISH, _Run

        sent: list[tuple] = []
        run = object.__new__(_Run)
        run.stopped, run.blocks, run.rtf, run.run = threading.Event(), queue.Queue(), 0.5, 7
        run.link = SimpleNamespace(send=sent.append)
        run._said, run.language, run.viewer = None, TURKISH, [0.0]
        run.model = SimpleNamespace(writes=lambda language: "FastConformer r2 · tr-fc-r2")
        # (no join of a lone "i" here: that is ufak's, "Deniz'i" is a suffix)
        words = [("deniz'i", 1.0), ("on", 1.3), ("iki", 1.6), ("kez", 1.9), ("gördüm.", 2.2)]
        run.writer = StreamWriter(_Recognizer(words), run.viewer)
        t = 0.0
        while t < 6.0:
            run.blocks.put((t, array("f", bytes(4 * 1600))))
            t += 0.1
        run.blocks.put(None)
        with self.assertLogs("services.captions.worker", logging.INFO) as logs:
            run._stream()
        captions = [m for m in sent if m[0] == "caption"]
        self.assertEqual([m[4] for m in captions], ["Deniz'i 12 kez gördüm."])  # as the others
        self.assertIn(
            "AI captions: [FastConformer r2 · tr-fc-r2] heard \"deniz'i on iki kez gördüm.\", "
            "shows \"Deniz'i 12 kez gördüm.\"",
            [record.getMessage() for record in logs.records],
        )
        self.assertIn(
            "AI captions: run 7: [FastConformer r2 · tr-fc-r2] writes now",
            [record.getMessage() for record in logs.records],
        )


class AnchorsTest(unittest.TestCase):
    def test_fed_seconds_map_through_their_block_across_a_gap(self) -> None:
        anchors = Anchors()
        anchors.add(100.0, 2.0)  # fed 0..2 s -> timeline 100..102
        anchors.add(110.0, 2.0)  # a gap: fed 2..4 s -> timeline 110..112
        self.assertAlmostEqual(anchors.timeline(1.5), 101.5)
        self.assertAlmostEqual(anchors.timeline(3.0), 111.0)
        anchors.forget_before(3.0)
        self.assertAlmostEqual(anchors.timeline(3.5), 111.5)

    def test_timeline_seconds_map_back_to_seconds_fed(self) -> None:
        anchors = Anchors()
        anchors.add(100.0, 2.0)
        anchors.add(110.0, 2.0)
        self.assertAlmostEqual(anchors.fed_at(101.5), 1.5)
        self.assertAlmostEqual(anchors.fed_at(111.0), 3.0)
        self.assertAlmostEqual(anchors.fed_at(105.0), 2.0)  # in the gap: where the next begins


if __name__ == "__main__":
    unittest.main()
