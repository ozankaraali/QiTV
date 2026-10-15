from datetime import datetime, timedelta, timezone
import unittest

from services.epg_view import (
    Programme,
    normalize,
    now_next,
    parse_programme,
    programmes_for_channel,
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
        manager.get_programs_for_channel = lambda channel, start, limit: [
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
