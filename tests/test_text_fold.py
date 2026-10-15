import unittest

from services.text_fold import fold


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


if __name__ == "__main__":
    unittest.main()
