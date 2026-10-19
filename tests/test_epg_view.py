from datetime import datetime, timedelta, timezone
import random
import unittest

from services.epg_view import (
    Programme,
    normalize,
    now_next,
    parse_programme,
    programmes_for_channel,
    timeline,
)

PLUS2 = timezone(timedelta(hours=2))


def xmltv(start, stop, title="Show", desc=None):
    raw = {"@start": start, "@stop": stop, "title": {"__text": title}}
    if desc is not None:
        raw["desc"] = {"__text": desc}
    return raw


class ParseProgrammeTests(unittest.TestCase):
    def test_xmltv(self):
        p = parse_programme(xmltv("20261015200000 +0200", "20261015210000 +0200", "News", "Daily"))
        self.assertEqual(p.title, "News")
        self.assertEqual(p.description, "Daily")
        self.assertEqual(p.start, datetime(2026, 10, 15, 20, 0, tzinfo=PLUS2))
        self.assertEqual(p.stop - p.start, timedelta(hours=1))

    def test_xmltv_title_list_and_missing_title(self):
        raw = xmltv("20261015200000 +0200", "20261015210000 +0200")
        raw["title"] = [{"__text": ""}, {"__text": "Nachrichten", "@lang": "de"}]
        self.assertEqual(parse_programme(raw).title, "Nachrichten")
        raw["title"] = None
        self.assertEqual(parse_programme(raw).title, "Untitled")

    def test_stb_naive_times_are_local(self):
        raw = {
            "name": "Film",
            "descr": "d",
            "time": "2026-10-15 20:00:00",
            "time_to": "2026-10-15 22:00:00",
        }
        p = parse_programme(raw)
        self.assertEqual(p.start, datetime(2026, 10, 15, 20, 0).astimezone())
        self.assertIsNotNone(p.start.tzinfo)
        self.assertEqual(p.title, "Film")

    def test_invalid_entries_are_dropped(self):
        self.assertIsNone(parse_programme({"foo": 1}))
        self.assertIsNone(parse_programme(xmltv("20261015210000 +0200", "20261015200000 +0200")))
        self.assertIsNone(parse_programme(xmltv("bad", "20261015200000 +0200")))


class NowNextTests(unittest.TestCase):
    def setUp(self):
        self.programmes = normalize(
            [
                xmltv("20261015210000 +0200", "20261015220000 +0200", "Late"),
                xmltv("20261015200000 +0200", "20261015210000 +0200", "Prime"),
                xmltv("20261015190000 +0200", "20261015200000 +0200", "Early"),
            ]
        )

    def test_sorted(self):
        self.assertEqual([p.title for p in self.programmes], ["Early", "Prime", "Late"])

    def test_now_and_next(self):
        now = datetime(2026, 10, 15, 20, 30, tzinfo=PLUS2)
        current, upcoming = now_next(self.programmes, now)
        self.assertEqual((current.title, upcoming.title), ("Prime", "Late"))
        self.assertAlmostEqual(current.progress(now), 0.5)

    def test_gap_has_no_current_but_a_next(self):
        now = datetime(2026, 10, 15, 18, 0, tzinfo=PLUS2)
        current, upcoming = now_next(self.programmes, now)
        self.assertIsNone(current)
        self.assertEqual(upcoming.title, "Early")

    def test_after_last(self):
        now = datetime(2026, 10, 16, 1, 0, tzinfo=PLUS2)
        self.assertEqual(now_next(self.programmes, now), (None, None))


class ProgrammesForChannelTests(unittest.TestCase):
    def test_filters_ended_and_limits(self):
        manager = type("M", (), {})()
        manager.get_programs_for_channel = lambda channel: [
            xmltv("20261015190000 +0200", "20261015200000 +0200", "Ended"),
            xmltv("20261015200000 +0200", "20261015210000 +0200", "On"),
            xmltv("20261015210000 +0200", "20261015220000 +0200", "Next"),
        ]
        now = datetime(2026, 10, 15, 20, 30, tzinfo=PLUS2)
        result = programmes_for_channel(manager, {"id": 1}, now, limit=1)
        self.assertEqual([p.title for p in result], ["On"])

    def test_manager_errors_give_empty_list(self):
        manager = type("M", (), {})()

        def boom(*args):
            raise RuntimeError("dictionary changed size during iteration")

        manager.get_programs_for_channel = boom
        self.assertEqual(
            programmes_for_channel(manager, {"id": 1}, datetime.now().astimezone()), []
        )


def overlapping_guide():
    """A guide merged from several feeds, as some providers send it: a placeholder over a day
    long starts with a real programme (and is listed first), and the real ones follow."""
    return [
        xmltv("20261018231500 +0200", "20261020015950 +0200", "Placeholder"),
        xmltv("20261018231500 +0200", "20261019010000 +0200", "Late Film"),
        xmltv("20261019010000 +0200", "20261019023000 +0200", "Comedy Night"),
        xmltv("20261019023000 +0200", "20261019050000 +0200", "Drama Hour"),
    ]


AT_0129 = datetime(2026, 10, 19, 1, 29, tzinfo=PLUS2)


DAY = datetime(2026, 10, 15, tzinfo=PLUS2)
HOUR = timedelta(hours=1)


def show(title, start, stop):
    """A programme from `start` to `stop` hours into DAY."""
    return Programme(title, DAY + start * HOUR, DAY + stop * HOUR)


def hours(programmes):
    """The timeline of `programmes` as (title, start, stop), in hours into DAY."""
    return [(p.title, (p.start - DAY) / HOUR, (p.stop - DAY) / HOUR) for p in timeline(programmes)]


class TimelineTests(unittest.TestCase):
    def test_a_placeholder_shows_only_where_the_real_programmes_leave_off(self):
        result = timeline(normalize(overlapping_guide()))
        self.assertEqual(
            [(p.title, f"{p.start:%d %H:%M}", f"{p.stop:%d %H:%M}") for p in result],
            [
                ("Late Film", "18 23:15", "19 01:00"),
                ("Comedy Night", "19 01:00", "19 02:30"),
                ("Drama Hour", "19 02:30", "19 05:00"),
                ("Placeholder", "19 05:00", "20 01:59"),
            ],
        )

    def test_a_programme_without_overlap_is_kept_as_it_is(self):
        programmes = normalize([xmltv("20261015200000 +0200", "20261015210000 +0200", "Prime")])
        self.assertIs(timeline(programmes)[0], programmes[0])

    def test_back_to_back_programmes_are_unchanged(self):
        programmes = [show("A", 10, 11), show("B", 11, 12.5), show("C", 12.5, 13)]
        result = timeline(programmes)
        self.assertEqual(len(result), 3)
        for kept, given in zip(result, programmes):
            self.assertIs(kept, given)

    def test_an_empty_guide(self):
        self.assertEqual(timeline([]), [])
        self.assertEqual(normalize(None), [])
        self.assertEqual(now_next([], DAY), (None, None))

    def test_a_partial_overlap_ends_where_the_next_programme_starts(self):
        self.assertEqual(
            hours([show("A", 20, 21), show("B", 20.5, 21.5)]),
            [("A", 20, 20.5), ("B", 20.5, 21.5)],
        )

    def test_a_day_long_filler_loses_to_a_real_programme_starting_with_it(self):
        news, filler = show("News", 10, 11), show("Filler", 10, 36)
        expected = [("News", 10, 11), ("Filler", 11, 36)]
        self.assertEqual(hours([news, filler]), expected)
        self.assertEqual(hours([filler, news]), expected)  # the filler listed after it

    def test_an_entry_of_twelve_hours_loses_and_one_just_under_does_not(self):
        real = show("Late Show", 0, 1.5)
        self.assertEqual(hours([real, show("Channel One", 0, 12)])[0], ("Late Show", 0, 1.5))
        self.assertEqual(hours([real, show("Marathon", 0, 11.75)]), [("Marathon", 0, 11.75)])

    def test_of_two_fillers_starting_together_the_shorter_shows(self):
        week, day = show("Week Filler", 0, 168), show("Day Filler", 0, 26)
        expected = [("Day Filler", 0, 26), ("Week Filler", 26, 168)]
        self.assertEqual(hours([week, day]), expected)
        self.assertEqual(hours([day, week]), expected)

    def test_of_equal_starts_the_longer_programme_shows(self):
        block, film = show("Movie", 0.5, 1.5), show("River Song", 0.5, 2.5)
        self.assertEqual(hours([block, film]), [("River Song", 0.5, 2.5)])
        self.assertEqual(hours([film, block]), [("River Song", 0.5, 2.5)])

    def test_a_short_placeholder_loses_to_a_longer_real_programme(self):
        filler = show("Channel One HD 01:00", 1, 5)
        real = show("Evening with Friends", 1, 6.5)
        self.assertEqual(hours([filler, real]), [("Evening with Friends", 1, 6.5)])
        self.assertEqual(hours([real, filler]), [("Evening with Friends", 1, 6.5)])

    def test_an_exact_tie_goes_to_the_one_listed_last(self):
        a, b = show("Evening Talk", 10, 11), show("Night Talk", 10, 11)
        self.assertEqual(hours([a, b]), [("Night Talk", 10, 11)])
        self.assertEqual(hours([b, a]), [("Evening Talk", 10, 11)])

    def test_a_long_programme_resumes_after_one_inside_it(self):
        self.assertEqual(
            hours([show("Filler", 0, 24), show("Film", 20, 21)]),
            [("Filler", 0, 20), ("Film", 20, 21), ("Filler", 21, 24)],
        )

    def test_the_outer_programme_resumes_after_the_last_inner_one(self):
        self.assertEqual(
            hours([show("A", 10, 14), show("B", 11, 12), show("C", 12, 13)]),
            [("A", 10, 11), ("B", 11, 12), ("C", 12, 13), ("A", 13, 14)],
        )

    def test_nested_programmes_resume_in_turn(self):
        self.assertEqual(
            hours([show("A", 10, 16), show("B", 11, 15), show("C", 12, 13)]),
            [("A", 10, 11), ("B", 11, 12), ("C", 12, 13), ("B", 13, 15), ("A", 15, 16)],
        )

    def test_every_moment_shows_the_latest_started_programme_on(self):
        def rank(item):
            # The rule written out again: started last; then a placeholder (12 h or more)
            # below the rest, the shorter of two placeholders, the longer of the rest; then
            # the one listed last.
            index, p = item
            span = (p.stop - p.start) / HOUR
            return (p.start, span < 12, span if span < 12 else -span, index)

        rng = random.Random(7)
        for trial in range(400):
            programmes = []
            for i in range(rng.randint(1, 8)):
                start = rng.randrange(0, 40) / 4
                span = rng.choice((rng.randrange(1, 40), rng.randrange(44, 120))) / 4
                programmes.append(show(f"P{i}", start, start + span))
            result = timeline(programmes)
            with self.subTest(trial=trial):
                for before, after in zip(result, result[1:]):
                    self.assertLessEqual(before.stop, after.start)  # never two at once
                for minute in range(0, 40 * 60, 10):
                    moment = DAY + timedelta(minutes=minute)
                    on = [(i, p) for i, p in enumerate(programmes) if p.is_on(moment)]
                    shown = [p for p in result if p.is_on(moment)]
                    if not on:
                        self.assertEqual(shown, [])
                        continue
                    latest = max(on, key=rank)[1]
                    self.assertEqual([p.title for p in shown], [latest.title])
                    self.assertTrue(latest.start <= shown[0].start < shown[0].stop <= latest.stop)

    def test_a_gap_stays_a_gap(self):
        result = timeline(
            normalize(
                [
                    xmltv("20261015200000 +0200", "20261015210000 +0200", "A"),
                    xmltv("20261015220000 +0200", "20261015230000 +0200", "B"),
                ]
            )
        )
        self.assertEqual([p.stop.hour for p in result], [21, 23])


class OverlappingGuideTests(unittest.TestCase):
    """The info line, the banner and the guide's details bar show what the grid shows."""

    def manager(self):
        manager = type("M", (), {})()
        manager.get_programs_for_channel = lambda channel: overlapping_guide()
        return manager

    def test_now_and_next_follow_the_grid(self):
        current, upcoming = now_next(normalize(overlapping_guide()), AT_0129)
        self.assertEqual((current.title, upcoming.title), ("Comedy Night", "Drama Hour"))

    def test_now_and_next_of_an_unresolved_list_follow_the_grid(self):
        programmes = [parse_programme(raw) for raw in overlapping_guide()]
        current, upcoming = now_next(programmes, AT_0129)
        self.assertEqual((current.title, upcoming.title), ("Comedy Night", "Drama Hour"))

    def test_the_first_programme_still_on_is_the_one_on_now(self):
        result = programmes_for_channel(self.manager(), {"id": 1}, AT_0129, limit=1)
        self.assertEqual([p.title for p in result], ["Comedy Night"])
        self.assertTrue(result[0].is_on(AT_0129))

    def test_the_guide_window_gets_the_grid_rows_programmes(self):
        window_start = datetime(2026, 10, 19, 1, 0, tzinfo=PLUS2)
        result = programmes_for_channel(self.manager(), {"id": 1}, window_start, limit=12)
        self.assertEqual([p.title for p in result], ["Comedy Night", "Drama Hour", "Placeholder"])


class ProgrammeTests(unittest.TestCase):
    def test_progress_clamps(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        p = Programme("T", start, start + timedelta(hours=1))
        self.assertEqual(p.progress(start - timedelta(hours=1)), 0.0)
        self.assertEqual(p.progress(start + timedelta(hours=2)), 1.0)
        self.assertTrue(p.is_on(start))
        self.assertFalse(p.is_on(start + timedelta(hours=1)))


if __name__ == "__main__":
    unittest.main()
