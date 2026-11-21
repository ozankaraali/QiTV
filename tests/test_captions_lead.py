"""2.13.1, caption translation's pace (no Qt): the translator's threads by machine, the lead a
line's translation gets (measured from the translator's warm-up, grown when translations come
late, lowered only at a natural point), and the targets whose pronouns need the line before."""

import unittest

from services.captions import tier
from services.captions.mt_languages import GENDERED, MT_LANGUAGES, NOT_GENDERED, needs_context
from services.captions.streaming import (
    TRANSLATE_LEAD_MAX_S,
    TRANSLATE_LEAD_MIN_S,
    TRANSLATE_LEAD_S,
    TRANSLATE_LEAD_STEP_S,
    TranslationLead,
    measured_lead,
)


class TranslatorThreadsTests(unittest.TestCase):
    def test_four_threads_from_eight_logical_cores_and_on_any_arm64_mac(self):
        for cpus, arm, threads in (
            (2, False, 2),
            (4, False, 2),  # hel's Neoverse-N1: the speech model's 2 and these 2
            (6, False, 2),
            (8, False, 4),  # the reference i7: 4 cores, 8 threads
            (16, False, 4),
            (32, False, 4),  # a 7950X: never more (decoding is memory-bound)
            (8, True, 4),
            (4, True, 4),  # any arm64 Mac
        ):
            with self.subTest(cpus=cpus, arm=arm):
                self.assertEqual(tier.translator_threads(cpus, arm_mac=arm), threads)

    def test_with_the_speech_models_threads_they_never_take_more_than_the_machine_has(self):
        for cpus in (2, 4, 8, 10, 12, 16, 32, 64):
            with self.subTest(cpus=cpus):
                both = tier.translator_threads(cpus, arm_mac=False) + tier.model_threads(cpus)
                self.assertLessEqual(both, max(4, cpus))


class MeasuredLeadTests(unittest.TestCase):
    def test_four_times_the_medium_line_and_half_a_second_rounded_up_to_a_half(self):
        # the 2026-11-21 report's machines: a 7950X, hel, the reference i7
        self.assertEqual(measured_lead(0.43), 2.5)
        self.assertEqual(measured_lead(0.85), 4.0)
        self.assertEqual(measured_lead(1.0), 4.5)
        self.assertEqual(measured_lead(0.625), 3.0)  # 3.0 exactly: not 3.5
        self.assertEqual(measured_lead(0.626), 3.5)
        self.assertEqual(measured_lead(0.375), 2.0)

    def test_it_is_two_to_five_seconds(self):
        self.assertEqual(measured_lead(0.05), TRANSLATE_LEAD_MIN_S)
        self.assertEqual(measured_lead(0.0), 2.0)
        self.assertEqual(measured_lead(1.2), 5.0)
        self.assertEqual(measured_lead(30.0), TRANSLATE_LEAD_S)
        self.assertEqual((TRANSLATE_LEAD_S, TRANSLATE_LEAD_MIN_S), (5.0, 2.0))


class TranslationLeadTests(unittest.TestCase):
    def test_five_seconds_until_the_translators_speed_is_known(self):
        lead = TranslationLead()
        self.assertEqual((lead.value, lead.measured), (5.0, None))
        self.assertFalse(lead.natural())  # nothing measured: nothing to lower to
        self.assertEqual(lead.value, 5.0)

    def test_a_measured_lower_lead_waits_for_a_natural_point(self):
        lead = TranslationLead()
        self.assertFalse(lead.measure(0.43))  # 2.5 s: not mid-playback
        self.assertEqual((lead.value, lead.measured), (5.0, 2.5))
        self.assertTrue(lead.natural())
        self.assertEqual(lead.value, 2.5)
        self.assertFalse(lead.natural())  # there already

    def test_a_measured_higher_lead_is_taken_at_once(self):
        lead = TranslationLead()
        lead.measure(0.43)
        lead.natural()
        self.assertEqual(lead.value, 2.5)
        self.assertFalse(lead.measure(1.0))  # a slower warm-up (beside the speech model) ...
        self.assertEqual((lead.value, lead.measured), (2.5, 2.5))  # ... the quiet one stays
        fresh = TranslationLead()
        fresh.value = 2.0
        self.assertTrue(fresh.measure(0.85))  # 4.0 s: raised at once
        self.assertEqual(fresh.value, 4.0)

    def test_late_translations_grow_it_a_second_at_a_time_to_seven(self):
        lead = TranslationLead()
        lead.measure(0.43)
        lead.natural()
        grown = []
        for _ in range(7):
            changed = lead.late()
            grown.append((changed, lead.value))
        self.assertEqual(
            grown,
            [
                (True, 3.5),
                (True, 4.5),
                (True, 5.5),
                (True, 6.5),
                (True, TRANSLATE_LEAD_MAX_S),
                (False, 7.0),
                (False, 7.0),
            ],
        )
        self.assertEqual(TRANSLATE_LEAD_STEP_S, 1.0)

    def test_grown_it_is_lowered_to_the_measured_lead_only_at_a_natural_point(self):
        lead = TranslationLead()
        lead.measure(0.43)
        lead.natural()
        lead.late()
        lead.late()
        self.assertEqual(lead.value, 4.5)
        self.assertEqual(lead.measured, 2.5)  # mid-playback: it stays grown
        self.assertTrue(lead.natural())  # a zap, a seek, play after a pause
        self.assertEqual(lead.value, 2.5)

    def test_unmeasured_a_late_translation_grows_it_from_five(self):
        lead = TranslationLead()
        self.assertTrue(lead.late())
        self.assertEqual(lead.value, 6.0)
        self.assertFalse(lead.natural())  # nothing measured: grown it stays


class GenderedTargetsTests(unittest.TestCase):
    def test_each_of_the_46_targets_is_classed_once(self):
        self.assertEqual(GENDERED | NOT_GENDERED, frozenset(MT_LANGUAGES))
        self.assertEqual(GENDERED & NOT_GENDERED, frozenset())
        self.assertEqual(len(GENDERED) + len(NOT_GENDERED), 46)

    def test_english_and_most_european_targets_take_the_line_before(self):
        for name in (
            "English",
            "German",
            "French",
            "Spanish",
            "Italian",
            "Portuguese",
            "Russian",
            "Polish",
            "Czech",
            "Arabic",
            "Hebrew",
            "Hindi",
        ):
            self.assertTrue(needs_context(name), name)
        for name in (
            "Turkish",
            "Finnish",
            "Hungarian",
            "Persian",
            "Chinese (Simplified)",
            "Chinese (Traditional)",
            "Japanese",
            "Korean",
            "Indonesian",
            "Malay",
        ):
            self.assertFalse(needs_context(name), name)
        self.assertFalse(needs_context("Ukrainian"))  # not one of MiLMMT's targets
        self.assertFalse(needs_context(""))


if __name__ == "__main__":
    unittest.main()
