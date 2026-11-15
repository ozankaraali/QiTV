import unittest

from services.captions.streaming import MAX_LINE_CHARS, Word, line_end


def words_of(text: str, start: float = 0.0, step: float = 0.3) -> list[Word]:
    """A sentence's words, one every `step` seconds from `start`."""
    return [Word(w, i, i, start + i * step, start + i * step) for i, w in enumerate(text.split())]


class LineEndTest(unittest.TestCase):
    def test_a_sentence_end_ends_a_line_and_the_rest_waits_for_more(self) -> None:
        words = words_of("Nereye gidiyorsun? Eve dönüyorum")
        self.assertEqual(line_end(words, False), 1)
        self.assertIsNone(line_end(words[2:], False))  # no end yet: more words first
        self.assertEqual(line_end(words[2:], True), 1)  # all of it: the line ends there
        self.assertIsNone(line_end([], True))

    def test_a_long_line_is_cut_at_its_last_comma(self) -> None:
        words = words_of("Bunu söylerken, " + " ".join(["kelime"] * 20), step=0.1)
        self.assertEqual(line_end(words, False), 1)  # "Bunu söylerken,"

    def test_a_long_line_without_a_comma_is_cut_at_a_word(self) -> None:
        words = words_of(" ".join(["kelime"] * 30), step=0.1)
        cut = line_end(words, False)
        assert cut is not None
        self.assertLess(cut, len(words) - 1)
        self.assertLessEqual(len(" ".join(w.text for w in words[: cut + 1])), MAX_LINE_CHARS)

    def test_a_line_ends_before_it_runs_past_six_seconds(self) -> None:
        words = words_of("bir iki üç dört beş", step=2.5)  # "dört" starts 7.5 s in
        self.assertEqual(line_end(words, True), 2)


if __name__ == "__main__":
    unittest.main()
