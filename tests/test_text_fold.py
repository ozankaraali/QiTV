import unittest

from services.text_fold import fold, fold_all


class FoldTests(unittest.TestCase):
    def test_turkish_and_accents(self):
        self.assertIn(fold("ist"), fold("İstanbul"))
        self.assertEqual(fold("ısı"), fold("isi"))
        self.assertIn(fold("cafe"), fold("Café"))
        self.assertIn(fold("ş"), fold("Şov"))

    def test_plain_text_unchanged_apart_from_case(self):
        self.assertEqual(fold("News 24"), "news 24")
        self.assertEqual(fold(""), "")

    def test_ascii_fast_path_equals_full_fold(self):
        import unicodedata

        for text in ["News 24", "HELLO World", "", "Salt Roads (2024)", "a-b_c ~!"]:
            full = "".join(
                c
                for c in unicodedata.normalize("NFKD", text.casefold())
                if not unicodedata.combining(c)
            ).replace("ı", "i")
            self.assertEqual(fold(text), full)

    def test_every_character_folds_as_the_reference(self):
        # The reference is the per-character definition; fold() may get there faster.
        import sys
        import unicodedata

        def reference(text):
            return "".join(
                c
                for c in unicodedata.normalize("NFKD", text.casefold())
                if not unicodedata.combining(c)
            ).replace("ı", "i")

        chars = [chr(code) for code in range(sys.maxunicode + 1) if not 0xD800 <= code < 0xE000]
        for start in range(0, len(chars), 4096):
            text = "".join(chars[start : start + 4096])
            self.assertEqual(fold(text), reference(text), hex(ord(chars[start])))
        for text in ["Ístanbul Café", "ǅemal", "ﬁlm ﬂow", "Ⅻ ①", "e\u0301\u0327", "ıI İ i̇"]:
            self.assertEqual(fold(text), reference(text), text)

    def test_fold_all_folds_each_text_as_it_would_alone(self):
        texts = ["News 24", "İstanbul Café", "ısı", "", "Ǆ ﬁlm", "e\u0301\u0327", "Saltmarsh HD"]
        self.assertEqual(fold_all(texts), [fold(t) for t in texts])
        self.assertEqual(fold_all(["a", "B"]), ["a", "b"])
        self.assertEqual(fold_all([]), [])

    def test_fold_all_in_chunks_equals_one_pass(self):
        texts = [f"Kanal {n} Café İz" if n % 3 else f"News {n}" for n in range(25)]
        self.assertEqual(fold_all(texts, chunk=4), [fold(t) for t in texts])
        self.assertEqual(fold_all(texts, chunk=1000), [fold(t) for t in texts])

    def test_fold_all_keeps_the_rows_when_a_text_has_a_line_break(self):
        texts = ["One\nTwo", "Three", "Fôur"]
        self.assertEqual(fold_all(texts), [fold(t) for t in texts])


if __name__ == "__main__":
    unittest.main()
