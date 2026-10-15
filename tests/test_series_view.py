import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.library import Details
from ui.library.series_view import SeriesView, episode_duration

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

    def test_reset_same_episodes_keeps_selection(self):
        view = self.make()
        ss = seasons()
        view.set_seasons(ss)
        view.set_episodes(ss[0], episodes(10))
        view.move_by(0, 1)
        view.move_by(0, 1)
        self.assertEqual(view.current_episode()["series"], 3)
        view.set_episodes(ss[0], episodes(10))
        self.assertEqual(view.current_episode()["series"], 3)
        view.set_episodes(ss[0], episodes(2))  # selected episode gone: row 0
        self.assertEqual(view.current_episode()["series"], 1)

    def test_stale_episodes_ignored(self):
        view = self.make()
        ss = seasons()
        view.set_seasons(ss)
        view.move_by(1, 0)
        view.set_episodes(ss[0], episodes(3))
        self.assertIsNone(view.current_episode())
        self.assertIs(view.current_season(), ss[1])

    def test_seasons_error_retries(self):
        view = self.make()
        retry = []
        view.retryRequested.connect(lambda: retry.append(1))
        view.set_seasons(None, error="down")
        view.activate()
        self.assertEqual(retry, [1])

    def test_seasons_enter_flushes_pending_selection(self):
        view = self.make()
        got = []
        view.seasonSelected.connect(got.append)
        ss = seasons()
        view.set_seasons(ss)
        view.move_by(1, 0)
        view.activate()
        self.assertEqual(got, [ss[1]])

    def test_seasons_enter_with_episodes_moves_down(self):
        view = self.make()
        ss = seasons()
        view.set_seasons(ss)
        view.set_episodes(ss[0], episodes(3))
        view.move_by(0, 1)
        view.move_by(0, -1)
        view.move_by(0, -1)
        self.assertEqual(view.focus_zone, "seasons")
        view.activate()
        self.assertEqual(view.focus_zone, "episodes")

    def test_down_at_last_row_stays(self):
        view = self.make()
        ss = seasons()
        view.set_seasons(ss)
        view.set_episodes(ss[0], episodes(3))
        for _ in range(5):
            view.move_by(0, 1)
        self.assertEqual(view.current_episode()["series"], 3)
        self.assertEqual(view.focus_zone, "episodes")

    def test_up_without_seasons_stays(self):
        view = self.make()
        ss = seasons()
        view.set_episodes(ss[0], episodes(3))
        view.move_by(0, -1)
        self.assertEqual(view.focus_zone, "episodes")

    def test_left_on_first_chip_goes_back(self):
        view = self.make()
        back = []
        view.backRequested.connect(lambda: back.append(1))
        view.set_seasons(seasons())
        view.move_by(-1, 0)
        self.assertEqual(back, [1])

    def test_page_moves_by_visible_rows(self):
        view = self.make()
        ss = seasons()
        view.set_seasons(ss)
        view.set_episodes(ss[0], episodes(100))
        view.show()
        QTest.qWait(30)
        view.page(1)
        first = view.current_episode()["series"]
        self.assertGreater(first, 2)
        view.page(-1)
        self.assertEqual(view.current_episode()["series"], 1)

    def test_start_over_button(self):
        view = self.make()
        got_start, got_play = [], []
        view.startOverRequested.connect(lambda s, e: got_start.append((s, e)))
        view.episodeActivated.connect(lambda s, e: got_play.append((s, e)))
        ss = seasons()
        eps = episodes(3)
        view.set_seasons(ss)
        view.set_progress_provider(lambda ep: {2: 0.4}.get(ep["series"]))
        view.set_episodes(ss[0], eps)
        view.move_by(1, 0)  # row 1 has no progress: ignored
        view.activate()
        self.assertEqual(got_play, [(ss[0], eps[0])])
        view.move_by(0, 1)
        view.move_by(1, 0)
        view.activate()
        self.assertEqual(got_start, [(ss[0], eps[1])])
        view.move_by(-1, 0)
        view.activate()
        self.assertEqual(got_play[-1], (ss[0], eps[1]))

    def test_duration_shapes(self):
        self.assertEqual(episode_duration({"duration": "45"}), "45 min")
        self.assertEqual(episode_duration({"duration": 45}), "45 min")
        self.assertEqual(episode_duration({"duration": 65}), "1h 05m")
        self.assertEqual(episode_duration({"info": {"duration": "00:42:10"}}), "42 min")
        self.assertEqual(episode_duration({"description": {"duration": "01:05:00"}}), "1h 05m")
        self.assertEqual(episode_duration({"info": {"duration_secs": 2700}}), "45 min")
        self.assertEqual(episode_duration({"duration": "45 min"}), "45 min")
        self.assertEqual(episode_duration({}), "")

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
