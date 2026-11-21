"""2.13.1, caption translation's pace (no Qt): the translator's threads by machine, the lead a
line's translation gets (measured from the translator's warm-up, grown when translations come
late, lowered only at a natural point), and the targets whose pronouns need the line before."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from services.captions import tier
from services.captions.mt_languages import GENDERED, MT_LANGUAGES, NOT_GENDERED, needs_context
from services.captions.streaming import (
    TRANSLATE_LEAD_MAX_S,
    TRANSLATE_LEAD_MIN_S,
    TRANSLATE_LEAD_QUIET_S,
    TRANSLATE_LEAD_S,
    TRANSLATE_LEAD_STEP_S,
    TranslationLead,
    measured_lead,
    translate_times,
)


class TranslatorThreadsTests(unittest.TestCase):
    def test_four_threads_from_six_physical_cores_and_on_any_arm64_mac(self):
        for physical, arm, threads in (
            (2, False, 2),
            (4, False, 2),  # the reference i7 (4 cores, 8 threads): the speech model has them
            (5, False, 2),
            (6, False, 4),
            (8, False, 4),
            (16, False, 4),  # a 7950X: never more (decoding is memory-bound)
            (4, True, 4),  # any arm64 Mac
            (8, True, 4),
        ):
            with self.subTest(physical=physical, arm=arm):
                self.assertEqual(tier.translator_threads(physical=physical, arm_mac=arm), threads)

    def test_on_this_machine_by_its_physical_cores(self):
        with (
            mock.patch.object(tier, "physical_cores", return_value=6),
            mock.patch.object(tier, "is_arm_mac", return_value=False),
        ):
            self.assertEqual(tier.translator_threads(), 4)
        with (
            mock.patch.object(tier, "physical_cores", return_value=4),
            mock.patch.object(tier, "is_arm_mac", return_value=False),
        ):
            self.assertEqual(tier.translator_threads(), 2)


class PhysicalCoresTests(unittest.TestCase):
    """Physical cores without a dependency: macOS's sysctl, Linux's topology (else
    /proc/cpuinfo), Windows' processor records; else half the logical ones."""

    def count(self, platform, logical=8, **patches):
        with (
            mock.patch.object(sys, "platform", platform),
            mock.patch.object(tier.os, "cpu_count", return_value=logical),
        ):
            with mock.patch.multiple(tier, **patches):
                return tier._physical_cores()

    def test_macos_asks_sysctl(self):
        asked = []

        def sysctl(name):
            asked.append(name)
            return 4

        self.assertEqual(self.count("darwin", _sysctl_int=sysctl), 4)
        self.assertEqual(asked, ["hw.physicalcpu"])
        self.assertEqual(self.count("darwin", _sysctl_int=lambda name: None), 4)  # 8 / 2

    def topology(self, root, siblings):
        for cpu, listed in enumerate(siblings):
            folder = Path(root) / f"cpu{cpu}" / "topology"
            folder.mkdir(parents=True)
            (folder / "thread_siblings_list").write_text(listed + "\n")
        (Path(root) / "cpufreq").mkdir()  # (not a CPU)

    def test_linux_counts_each_cores_siblings_once(self):
        with tempfile.TemporaryDirectory() as root:
            # 4 cores, 2 threads each (the reference i7 under Linux)
            self.topology(root, ["0,4", "1,5", "2,6", "3,7", "0,4", "1,5", "2,6", "3,7"])
            self.assertEqual(tier._linux_physical(Path(root), Path(root) / "none"), 4)
        with tempfile.TemporaryDirectory() as root:
            self.topology(root, ["0", "1", "2", "3"])  # hel: 4 cores, no SMT
            self.assertEqual(tier._linux_physical(Path(root), Path(root) / "none"), 4)

    def test_linux_without_topology_reads_proc_cpuinfo(self):
        blocks = []
        for cpu in range(12):  # 6 cores, 2 threads each
            blocks.append(f"processor\t: {cpu}\nphysical id\t: 0\ncore id\t\t: {cpu % 6}\n")
        with tempfile.TemporaryDirectory() as root:
            info = Path(root) / "cpuinfo"
            info.write_text("\n".join(blocks))
            self.assertEqual(tier._linux_physical(Path(root) / "no-sysfs", info), 6)
            info.write_text("processor\t: 0\nBogoMIPS\t: 50.00\n\nprocessor\t: 1\n")
            self.assertIsNone(tier._linux_physical(Path(root) / "no-sysfs", info))

    def test_linux_and_windows_fall_back_to_half_the_logical_cores(self):
        def broken(*args, **kwargs):
            raise OSError("no")

        self.assertEqual(self.count("linux", 8, _linux_physical=broken), 4)
        self.assertEqual(self.count("linux", 8, _linux_physical=lambda: None), 4)
        self.assertEqual(self.count("linux", 4, _linux_physical=lambda: 4), 4)
        self.assertEqual(self.count("win32", 12, _windows_physical=lambda: 6), 6)
        self.assertEqual(self.count("win32", 12, _windows_physical=broken), 6)
        self.assertEqual(self.count("linux", 1, _linux_physical=lambda: None), 1)
        self.assertEqual(self.count("linux", 4, _linux_physical=lambda: 9), 4)  # never more

    def test_windows_counts_its_processor_core_records(self):
        def record(size):  # (relation, size, then the rest of the record)
            return (0).to_bytes(4, "little") + size.to_bytes(4, "little") + bytes(size - 8)

        self.assertEqual(tier._core_records(b"".join(record(48) for _ in range(6))), 6)
        self.assertEqual(tier._core_records(record(48) + record(80)), 2)
        self.assertEqual(tier._core_records(b""), 0)
        self.assertEqual(tier._core_records((0).to_bytes(8, "little")), 0)  # a size of 0


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


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


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

    def test_grown_it_stays_at_natural_points_till_five_quiet_minutes_then_steps_down(self):
        clock = Clock()
        lead = TranslationLead(clock)
        lead.measure(0.43)
        lead.natural()
        lead.late()
        lead.late()
        self.assertEqual((lead.value, lead.measured), (4.5, 2.5))
        clock.now += TRANSLATE_LEAD_QUIET_S - 1.0
        self.assertFalse(lead.natural())  # a zap, a seek, play after a pause: it stays grown
        self.assertEqual(lead.value, 4.5)
        clock.now += 1.0  # five minutes with no late line
        self.assertTrue(lead.natural())
        self.assertEqual(lead.value, 3.5)  # a step toward the measured one
        self.assertFalse(lead.natural())  # the next step: after as long again
        clock.now += TRANSLATE_LEAD_QUIET_S
        self.assertTrue(lead.natural())
        self.assertEqual(lead.value, 2.5)
        clock.now += TRANSLATE_LEAD_QUIET_S
        self.assertFalse(lead.natural())  # the measured one: no lower
        self.assertEqual(TRANSLATE_LEAD_QUIET_S, 300.0)

    def test_a_late_line_starts_the_quiet_minutes_again(self):
        clock = Clock()
        lead = TranslationLead(clock)
        lead.measure(0.43)
        lead.natural()
        lead.late()
        clock.now += 250.0
        for _ in range(4):
            lead.late()  # up to the cap: still late
        self.assertEqual(lead.value, 7.0)
        lead.late()  # at the cap: late again
        clock.now += 250.0
        self.assertFalse(lead.natural())  # 250 s since the last late one
        clock.now += 50.0
        self.assertTrue(lead.natural())
        self.assertEqual(lead.value, 6.0)

    def test_a_measurement_after_it_grew_never_takes_it_below_what_it_grew_to(self):
        clock = Clock()
        lead = TranslationLead(clock)
        lead.late()
        self.assertEqual(lead.value, 6.0)  # unmeasured: from five
        lead.measure(0.43)
        self.assertFalse(lead.natural())
        self.assertEqual(lead.value, 6.0)
        clock.now += TRANSLATE_LEAD_QUIET_S
        self.assertTrue(lead.natural())
        self.assertEqual(lead.value, 5.0)

    def test_unmeasured_a_late_translation_grows_it_from_five(self):
        clock = Clock()
        lead = TranslationLead(clock)
        self.assertTrue(lead.late())
        self.assertEqual(lead.value, 6.0)
        self.assertFalse(lead.natural())  # grown it stays
        clock.now += TRANSLATE_LEAD_QUIET_S
        self.assertTrue(lead.natural())  # five quiet minutes: back to five
        self.assertEqual(lead.value, 5.0)

    def test_a_time_saved_by_an_earlier_run_is_where_it_starts_till_this_run_times_its_own(self):
        lead = TranslationLead()
        lead.seed(0.43)
        self.assertEqual((lead.value, lead.measured), (5.0, 2.5))
        self.assertTrue(lead.natural())  # captions on: from the saved time
        self.assertEqual(lead.value, 2.5)
        self.assertTrue(lead.measure(0.85))  # this run's own replaces it: 4.0 s, at once
        self.assertEqual((lead.value, lead.measured, lead.fastest), (4.0, 4.0, 0.85))
        lead.seed(0.1)  # (a saved time no longer counts once this run timed its own)
        self.assertEqual(lead.measured, 4.0)
        self.assertFalse(lead.measure(1.2))  # the quickest of this run's own stays
        self.assertEqual((lead.measured, lead.fastest), (4.0, 0.85))


class SavedTimesTests(unittest.TestCase):
    def test_the_config_keeps_a_time_per_thread_count_and_nothing_else(self):
        self.assertEqual(translate_times({"2": 0.77, "4": 0.48}), {"2": 0.77, "4": 0.48})
        self.assertEqual(translate_times({"2": 1}), {"2": 1.0})
        for broken in (None, "0.43", ["2", 0.4], 5):
            self.assertEqual(translate_times(broken), {}, broken)
        self.assertEqual(
            translate_times(
                {"2": "fast", "x": 0.4, "0": 0.4, "4": -1.0, "8": 0.0, "6": 99.0, "3": True}
            ),
            {},
        )
        self.assertEqual(translate_times({"4": 0.4561234}), {"4": 0.456})


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
