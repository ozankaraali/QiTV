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


if __name__ == "__main__":
    unittest.main()
