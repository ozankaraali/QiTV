"""2.11.2, the owner: the language AI captions are translated into is remembered per spoken
language ("German → English", "English → Türkçe", "English → no translation"); a language never
chosen for, or not known yet, is not translated (services.captions.translation_rules)."""

import unittest

from services.captions.translation_rules import clean, target_for, used_targets, with_rule


class TranslationRulesTests(unittest.TestCase):
    def test_clean_keeps_languages_and_targets_only(self):
        self.assertEqual(
            clean({"German": "English", "English": "", "Klingon": "English", "Turkish": "Elvish"}),
            {"German": "English", "English": ""},
        )
        self.assertEqual(clean("English"), {})
        for stored in (None, 5, ["German"], {"German": 5}, {"German": None}):
            with self.subTest(stored=stored):
                self.assertEqual(clean(stored), {})

    def test_a_language_without_a_rule_or_unknown_is_not_translated(self):
        rules = {"German": "English", "English": ""}
        self.assertEqual(
            (target_for(rules, "German"), target_for(rules, "English")), ("English", "")
        )
        self.assertEqual((target_for(rules, "French"), target_for(rules, "")), ("", ""))

    def test_no_translation_is_remembered_and_unknown_sets_nothing(self):
        self.assertEqual(with_rule({"English": "Turkish"}, "English", ""), {"English": ""})
        self.assertEqual(with_rule({"German": "English"}, "", "Turkish"), {"German": "English"})
        rules = {"German": "English"}
        self.assertEqual(with_rule(rules, "English", "Turkish"), {**rules, "English": "Turkish"})
        self.assertEqual(rules, {"German": "English"})  # a new dict: the one given stays

    def test_used_targets_leave_out_the_heard_language_and_sort_by_own_name(self):
        rules = {"German": "English", "English": "Turkish", "French": "English", "Arabic": ""}
        self.assertEqual(used_targets(rules), ["English", "Turkish"])  # English < Türkçe
        self.assertEqual(used_targets(rules, "English"), ["Turkish"])
        # a caption language MiLMMT names otherwise (Qwen's Chinese: its Simplified)
        self.assertEqual(used_targets({"English": "Chinese (Simplified)"}, "Chinese"), [])


if __name__ == "__main__":
    unittest.main()
