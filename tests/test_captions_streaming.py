import unittest

from services.captions.streaming import MAX_LINE_CHARS, SETTLED_S, Anchors, Lines, words_of


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


class AnchorsTest(unittest.TestCase):
    def test_fed_seconds_map_through_their_block_across_a_gap(self) -> None:
        anchors = Anchors()
        anchors.add(100.0, 2.0)  # fed 0..2 s -> timeline 100..102
        anchors.add(110.0, 2.0)  # a gap: fed 2..4 s -> timeline 110..112
        self.assertAlmostEqual(anchors.timeline(1.5), 101.5)
        self.assertAlmostEqual(anchors.timeline(3.0), 111.0)
        anchors.forget_before(3.0)
        self.assertAlmostEqual(anchors.timeline(3.5), 111.5)


if __name__ == "__main__":
    unittest.main()
