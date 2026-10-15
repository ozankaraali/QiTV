import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.library import Details
from ui.library.series_view import SeriesView

app = QApplication.instance() or QApplication([])


class FakePosters(QObject):
    logoReady = Signal(str)

    def request(self, urls):
        pass

    def pixmap(self, url):
        return None


def details():
    return Details(
        "Salt Roads",
        "2024",
        "1h",
        "Documentary",
        "7.8",
        "A plot. " * 40,
        None,
        None,
        False,
        "series",
    )


def seasons(n=3):
    return [{"id": str(i), "name": f"Season {i}"} for i in range(1, n + 1)]


def episodes(n=10):
    return [
        {"id": "s", "season_id": "1", "series": i, "ename": f"Episode {i}", "duration": "45 min"}
        for i in range(1, n + 1)
    ]


class SeriesViewTests(unittest.TestCase):
    def make(self):
        view = SeriesView(FakePosters())
        self.addCleanup(view.close)
        view.resize(1280, 720)
        view.set_series({"id": "s", "name": "Salt Roads"}, details())
        return view

    def test_season_selection_is_debounced(self):
        view = self.make()
        got = []
        view.seasonSelected.connect(got.append)
        ss = seasons()
        view.set_seasons(ss)
        view.move_by(1, 0)
        view.move_by(1, 0)
        self.assertEqual(got, [])
        QTest.qWait(400)
        self.assertEqual(got, [ss[2]])

    def test_activate_episode(self):
        view = self.make()
        got = []
        view.episodeActivated.connect(lambda s, e: got.append((s, e)))
        ss = seasons()
        eps = episodes()
        view.set_seasons(ss)
        view.set_episodes(ss[0], eps)
        self.assertEqual(view.focus_zone, "episodes")
        view.activate()
        self.assertEqual(got, [(ss[0], eps[0])])

    def test_up_from_first_episode_focuses_seasons(self):
        view = self.make()
        ss = seasons()
        view.set_seasons(ss)
        view.set_episodes(ss[0], episodes())
        view.move_by(0, 1)
        view.move_by(0, -1)
        self.assertEqual(view.focus_zone, "episodes")
        view.move_by(0, -1)
        self.assertEqual(view.focus_zone, "seasons")
        view.move_by(0, 1)
        self.assertEqual(view.focus_zone, "episodes")

    def test_error_activate_retries_and_escape_goes_back(self):
        view = self.make()
        retry, back = [], []
        view.retryRequested.connect(lambda: retry.append(1))
        view.backRequested.connect(lambda: back.append(1))
        ss = seasons()
        view.set_seasons(ss)
        view.set_episodes(ss[0], None, error="boom")
        view.activate()
        self.assertEqual(retry, [1])
        self.assertTrue(view.escape())
        self.assertEqual(back, [1])

    def test_watched_badge(self):
        view = self.make()
        ss = seasons()
        view.set_seasons(ss)
        view.set_progress_provider(lambda ep: {1: 0.95, 2: 0.4}.get(ep["series"]))
        view.set_episodes(ss[0], episodes(3))
        self.assertEqual([view.row_badges(i) for i in range(3)], ["✓", "", ""])

    def test_large_list_paints_fast(self):
        view = self.make()
        ss = seasons()
        view.set_seasons(ss)
        view.set_progress_provider(lambda ep: 0.5)
        view.show()
        QTest.qWait(50)
        start = time.perf_counter()
        view.set_episodes(ss[0], episodes(2000))
        view.grab()
        self.assertLess(time.perf_counter() - start, 0.1)


if __name__ == "__main__":
    unittest.main()
