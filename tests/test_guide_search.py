"""The Recordings tab's find in the guide (2.9.7): programmes to record, the nearest channels
first, a slice of channels at a time."""

from datetime import datetime, timedelta
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from services.epg_view import Programme
from services.guide_search import SLICE, GuideSearch

app = QApplication.instance() or QApplication([])
NOW = datetime(2026, 11, 5, 20, 0).astimezone()


def show(title, hours):
    start = NOW + timedelta(hours=hours)
    return Programme(title, start, start + timedelta(hours=1), "", None)


GUIDE = {
    "1": [show("Evening News", -2), show("Late News", 1), show("Mountain Rivers", 3)],
    "2": [show("News at Ten", 2)],
    "3": [show("Night Match", 1)],
}


class GuideSearchTests(unittest.TestCase):
    def setUp(self):
        self.asked = []

        def programmes(channel, start, limit):
            self.asked.append(channel["id"])
            return GUIDE.get(channel["id"], [])

        self.search = GuideSearch(programmes, lambda: NOW)
        self.found, self.done = [], []
        self.search.found.connect(self.found.extend)
        self.search.finished.connect(lambda: self.done.append(True))

    def run_out(self):
        for _ in range(200):
            if self.done:
                return
            QCoreApplication.processEvents()

    def test_it_finds_programmes_nearest_sources_first(self):
        near = [{"id": "2", "name": "B"}]
        wide = [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}, {"id": "3", "name": "C"}]
        self.search.start("news", [near, wide])
        self.run_out()
        self.assertEqual(
            [(c["id"], p.title) for c, p in self.found],
            [("2", "News at Ten"), ("1", "Late News")],  # B first; each once
        )

    def test_a_past_programme_is_not_found(self):
        self.search.start("evening", [[{"id": "1", "name": "A"}]])
        self.run_out()
        self.assertEqual(self.found, [])
        self.assertEqual(self.done, [True])

    def test_it_walks_in_slices_and_can_be_cancelled(self):
        many = [{"id": str(n), "name": f"C{n}"} for n in range(SLICE * 3)]
        self.search.start("news", [many])
        self.assertLessEqual(len(self.asked), SLICE)  # nothing waits: a slice per turn
        self.search.cancel()
        for _ in range(20):
            QCoreApplication.processEvents()
        self.assertLessEqual(len(self.asked), SLICE * 2)
        self.assertEqual(self.done, [])

    def test_a_new_query_replaces_the_one_under_way(self):
        wide = [{"id": "1", "name": "A"}, {"id": "3", "name": "C"}]
        self.search.start("news", [wide])
        self.search.start("match", [wide])
        self.run_out()
        self.assertEqual([p.title for _c, p in self.found], ["Night Match"])


if __name__ == "__main__":
    unittest.main()
