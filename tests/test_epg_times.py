import os
import tempfile
from types import SimpleNamespace
import unittest

from services.epg_manager import EpgManager, normalize_programme_stop

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


if __name__ == "__main__":
    unittest.main()
