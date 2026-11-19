import unittest

from services import settings_model as m


def _row(key, system="Darwin"):
    playback = next(s for s in m.sections(system) if s.key == m.PLAYBACK)
    return next(r for r in playback.rows if r.key == key)


def _all_rows(system="Darwin"):
    playback = next(s for s in m.sections(system) if s.key == m.PLAYBACK)
    for row in playback.rows:
        yield row
        if row.section:
            yield from row.section.rows


class SettingsModelTests(unittest.TestCase):
    def test_sections_are_in_the_spec_order(self):
        self.assertEqual(
            tuple(s.key for s in m.sections("Linux")),
            (
                m.PROVIDERS,
                m.GENERAL,
                m.PLAYBACK,
                m.SUBTITLES,
                m.TIMESHIFT,
                m.GUIDE,
                m.REMOTE,
                m.PARENTAL,
                m.FILES,
                m.ADVANCED,
            ),
        )

    def test_step_wraps_both_ways(self):
        row = _row("live_latency")
        self.assertEqual(m.step(row, "stable", 1), "auto")
        self.assertEqual(m.step(row, "auto", -1), "stable")

    def test_an_unknown_value_steps_from_the_default(self):
        row = _row("live_latency")
        self.assertEqual(m.choice_of(row, "turbo").value, "auto")
        self.assertIs(m.is_lit(row, "turbo"), False)
        self.assertEqual(m.step(row, "turbo", 1), "low")

    def test_lit_means_not_the_default(self):
        row = _row("live_latency")
        self.assertTrue(m.is_lit(row, "low"))
        self.assertFalse(m.is_lit(row, "auto"))

    def test_picture_in_picture_is_only_on_macos(self):
        def keys(system):
            return [r.key for r in next(s for s in m.sections(system) if s.key == m.PLAYBACK).rows]

        self.assertIn("pip", keys("Darwin"))
        self.assertNotIn("pip", keys("Linux"))
        self.assertNotIn("pip", keys("Windows"))

    def test_unmoved_sections_open_the_old_tab(self):
        by_key = {s.key: s for s in m.sections("Linux")}
        self.assertEqual(by_key[m.GUIDE].legacy_tab, "epg")
        self.assertEqual(by_key[m.PLAYBACK].legacy_tab, "")

    def test_every_help_fits_one_line(self):
        for row in _all_rows():
            self.assertTrue(row.help, row.key)
            self.assertLessEqual(len(row.help), 90, row.key)

    def test_playback_row_order(self):
        keys = [r.key for r in next(s for s in m.sections("Darwin") if s.key == m.PLAYBACK).rows]
        self.assertEqual(
            keys,
            [
                "live_latency",
                "hardware_decoding",
                "media_4k_versions",
                "start_with_last_channel",
                "mouse_wheel_action",
                "play_in",
                "pip",
                "autoplay",
            ],
        )


if __name__ == "__main__":
    unittest.main()
