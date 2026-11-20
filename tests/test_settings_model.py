import json
from pathlib import Path
import unittest

from scripts import i18n_extract as extract
from services import picture, settings_model as m

ROOT = Path(__file__).resolve().parents[1]


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

    def test_a_step_of_nothing_leaves_the_value_as_it_is(self):
        row = _row("live_latency")
        self.assertEqual(m.step(row, "normal", 0), "normal")
        self.assertEqual(m.step(row, "turbo", 0), "turbo")  # even one the row does not know

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

    def test_files_without_rows_says_why(self):
        by_key = {s.key: s for s in m.sections("Linux")}
        self.assertEqual(by_key[m.FILES].rows, ())
        self.assertTrue(by_key[m.FILES].empty_text)  # none of its rows can be (from source)

    def test_providers_list_its_rows_then_add_and_free(self):
        # The providers' rows, as its caller builds them, then ＋ Add provider and Free
        # channels.
        given = (m.Row("provider/A", "A", literal=True),)
        providers = m.sections("Linux", given)[0]
        self.assertEqual(providers.key, m.PROVIDERS)
        self.assertEqual(
            [row.key for row in providers.rows], ["provider/A", m.ADD_PROVIDER, m.FREE_CHANNELS]
        )
        add, free = providers.rows[1:]
        self.assertEqual((add.text, add.kind, add.literal), ("＋ Add provider", m.ACTION, True))
        self.assertEqual((free.text, free.kind), ("Free channels", m.OPENS))
        for row in (add, free):
            self.assertTrue(row.help, row.key)
            self.assertLessEqual(len(row.help), 90, row.key)
        self.assertEqual(
            [row.key for row in m.sections("Linux")[0].rows], [m.ADD_PROVIDER, m.FREE_CHANNELS]
        )
        # A literal choice is shown as it is given (a number, a text in the language already).
        self.assertEqual(m.Choice(3, "3", literal=True).shown(), "3")

    def test_every_help_fits_one_line(self):
        for row in _all_rows():
            self.assertTrue(row.help, row.key)
            self.assertLessEqual(len(row.help), 90, row.key)

    def test_a_restart_rows_help_leaves_the_restart_to_its_note(self):
        # The note ("Applies when QiTV starts again.") follows the help of such a row.
        restart = [row for row in _all_rows() if row.restart]
        self.assertTrue(restart)
        for row in restart:
            self.assertNotIn("restart", row.help.lower(), row.key)

    def test_every_choice_is_a_catalog_text(self):
        # A choice is looked up with its context (a setting's On and Off have their own
        # entries): the context given to N_ and the Choice's must be the same.
        template = json.loads((ROOT / "i18n" / "template.json").read_text(encoding="utf-8"))
        for row in _all_rows():
            for choice in row.choices:
                key = f"{choice.ctx}|{choice.text}" if choice.ctx else choice.text
                self.assertIn(key, template, row.key)
        self.assertEqual({choice.ctx for choice in m.ON_OFF}, {"setting value"})

    def test_each_section_title_tells_the_translators_what_it_is(self):
        entries, problems = extract.collect([ROOT / "services" / "settings_model.py"], ROOT)
        self.assertEqual(problems, [])
        for section in m.sections("Linux"):
            comments = entries[section.text].comments
            self.assertIn("a settings section's title", comments, section.key)

    def test_deinterlacing_is_auto_or_off_and_its_help_names_the_restart(self):
        # Auto and Off, in the "deinterlacing" context; only back to Auto waits for a restart,
        # so the help says it and the row has no restart note.
        row = _row("deinterlace", "Linux")
        self.assertEqual(tuple(c.value for c in row.choices), picture.DEINTERLACING)
        self.assertEqual([c.shown() for c in row.choices], ["Auto", "Off"])
        self.assertEqual({c.ctx for c in row.choices}, {"deinterlacing"})
        self.assertEqual(row.default, picture.AUTO)
        self.assertFalse(row.restart)
        self.assertIn("QiTV starts again", row.help)

    def test_explicit_kinds_override_the_derived_one(self):
        layer = m.Section("layer", "Layer", empty_text="No hidden groups")
        # Without one, the kind is derived as plan 1 did: choices step, a layer opens, else acts.
        self.assertEqual(m.Row("a", "A", "", m.ON_OFF, True).kind, m.STEP)
        self.assertEqual(m.Row("b", "B", section=layer).kind, m.OPENS)
        self.assertEqual(m.Row("c", "C").kind, m.ACTION)
        self.assertEqual(_row("live_latency").kind, m.STEP)
        self.assertEqual(_row("autoplay").kind, m.OPENS)
        # Given, it stands.
        self.assertEqual(m.Row("d", "D", "", m.ON_OFF, True, kind=m.INFO).kind, m.INFO)
        self.assertEqual(m.Row("e", "E", section=layer, kind=m.ACTION).kind, m.ACTION)
        self.assertEqual(m.Row("f", "F", kind=m.TEXT).kind, m.TEXT)
        self.assertEqual(m.Row("g", "Remove provider…", kind=m.DANGER).kind, m.DANGER)
        self.assertEqual(m.Row("h", "Address and login", kind=m.OPENS).kind, m.OPENS)
        with self.assertRaises(ValueError):
            m.Row("i", "I", kind="nonsense")
        row = m.Row("p", "Demo TV", section=layer, sub="Xtream · in use", note="2")
        self.assertEqual((row.sub, row.note), ("Xtream · in use", "2"))
        self.assertEqual(layer.empty_text, "No hidden groups")
        self.assertEqual(m.Section("s", "S").empty_text, "")

    def test_playback_row_order(self):
        keys = [r.key for r in next(s for s in m.sections("Darwin") if s.key == m.PLAYBACK).rows]
        self.assertEqual(
            keys,
            [
                "live_latency",
                "hardware_decoding",
                "deinterlace",
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
