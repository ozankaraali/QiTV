from datetime import datetime, timedelta, timezone
import os
import tempfile
from types import SimpleNamespace
import unittest

from services.epg_manager import EpgManager, normalize_programme_stop
from services.epg_time import parse_xmltv_local
from services.epg_view import programmes_for_channel
from services.multikeydict import MultiKeyDict

XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="news.test"><display-name>News</display-name></channel>
  <programme channel="news.test" start="20261015233000 +0200" stop="20261015003000 +0200">
    <title>Late News</title>
  </programme>
</tv>
"""


class NormalizeProgrammeStopTests(unittest.TestCase):
    def test_overnight_stop_moves_to_next_day(self):
        self.assertEqual(
            normalize_programme_stop("20261015233000 +0200", "20261015003000 +0200"),
            "20261016003000 +0200",
        )

    def test_regular_programme_is_unchanged(self):
        self.assertEqual(
            normalize_programme_stop("20261015200000 +0200", "20261015210000 +0200"),
            "20261015210000 +0200",
        )

    def test_different_offsets_are_compared_as_instants(self):
        # 23:30 +0200 == 21:30 UTC; 22:15 +0000 is later, so not overnight.
        self.assertEqual(
            normalize_programme_stop("20261015233000 +0200", "20261015221500 +0000"),
            "20261015221500 +0000",
        )

    def test_times_without_timezone(self):
        self.assertEqual(
            normalize_programme_stop("20261015233000", "20261015003000"), "20261016003000"
        )

    def test_unparseable_values_are_returned_unchanged(self):
        self.assertEqual(normalize_programme_stop("garbage", "20261015003000"), "20261015003000")


class IndexProgrammesTests(unittest.TestCase):
    def test_indexed_programme_carries_corrected_stop(self):
        manager = EpgManager.__new__(EpgManager)
        manager.config_manager = SimpleNamespace(
            xmltv_channel_map=SimpleNamespace(get_keys=lambda key: None)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "guide.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(XMLTV)
            programs = manager._index_programs(path)
        [programme] = programs["news.test"]
        self.assertEqual(programme["@stop"], "20261016003000 +0200")


class OffsetLessGuideTests(unittest.TestCase):
    """A guide whose times carry no UTC offset ("20261101200000") is read as local
    wall-clock time everywhere: the lookup window as the guide's own view does."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = SimpleNamespace(
            epg_source="URL",
            epg_list_window_hours=24,
            xmltv_channel_map=MultiKeyDict(),
            get_config_dir=lambda: directory.name,
        )
        provider = SimpleNamespace(current_provider={"type": "M3UPLAYLIST", "name": "P"})
        self.manager = EpgManager(self.config, provider)
        self.manager._epg_source = "guide"
        now = datetime.now().replace(microsecond=0)  # local wall-clock time, as the guide
        local = "%Y%m%d%H%M%S"
        guide = MultiKeyDict()
        guide[("ch1",)] = [
            {
                "@start": (now - start).strftime(local),
                "@stop": (now - stop).strftime(local),
                "title": title,
            }
            for title, start, stop in (
                ("Ended", timedelta(hours=3), timedelta(hours=1)),
                ("On now", timedelta(minutes=30), timedelta(minutes=-30)),
                ("Later", timedelta(hours=-23), timedelta(hours=-24)),
                ("Beyond", timedelta(hours=-25), timedelta(hours=-26)),
            )
        ]
        self.manager.epg = guide

    def test_the_lookup_window_reads_them_as_local_time(self):
        with self.assertLogs("services.epg_manager", "INFO") as logs:
            titles = [p["title"] for p in self.manager.get_programs_for_channel(CHANNEL)]
            self.manager.get_programs_for_channel(CHANNEL)  # noted once per guide
        self.assertEqual(titles, ["On now", "Later"])
        self.assertEqual(len([line for line in logs.output if "UTC offset" in line]), 1)

    def test_the_guide_shows_what_is_on_now(self):
        now = datetime.now().astimezone()
        shown = programmes_for_channel(self.manager, CHANNEL, now)
        self.assertEqual([p.title for p in shown][:2], ["On now", "Later"])
        self.assertTrue(shown[0].is_on(now))

    def test_both_forms_parse_alike(self):
        self.assertEqual(
            parse_xmltv_local(" 20261101200000 "), datetime(2026, 11, 1, 20).astimezone()
        )
        self.assertEqual(
            parse_xmltv_local("20261101200000 +0100"),
            datetime(2026, 11, 1, 19, tzinfo=timezone.utc),
        )
        with self.assertRaises(ValueError):
            parse_xmltv_local("soon")


CHANNEL = {"name": "x", "xmltv_id": "ch1"}


if __name__ == "__main__":
    unittest.main()
