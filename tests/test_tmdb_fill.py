"""TMDB in the Library: what is looked up and when, and what the Library then shows.

Every title and id here is made up; the HTTP stand-in answers like TMDB's API.
"""

import os
import tempfile
from types import SimpleNamespace
import unittest

from PySide6.QtWidgets import QApplication
from job_fixtures import ImmediateRunner
from opensubtitles_fixtures import FakeHttp, FakeResponse
from test_tmdb import details

from services.tmdb import API_URL
from ui.tmdb_fill import BATCH, RETRY_S, TmdbFill

API = API_URL


def answer_ids(http, *ids):
    for n in ids:
        http.answer(
            "GET",
            f"{API}/movie/{n}",
            FakeResponse(
                200,
                details(n, f"Film {n}", "2019-01-01", overview=f"Story {n}.", poster=f"/{n}.jpg"),
            ),
        )


def film(n, **kw):
    return {"name": f"Film {n}", "tmdb": str(n), **kw}


class TmdbFillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "cache", "tmdb.json")
        self.config = SimpleNamespace(tmdb_details=True, tmdb_token="tok")
        self.http = FakeHttp()
        self.clock = [100.0]
        self.fill = self.make()
        self.changes = []
        self.fill.changed.connect(lambda: self.changes.append(True))

    def make(self):
        return TmdbFill(
            config=self.config,
            jobs=ImmediateRunner(),
            cache_path=self.path,
            language="en",
            http=self.http,
            now=lambda: self.clock[0],
        )

    def asked(self):
        return [url.rsplit("/", 1)[-1] for _m, url, _kw in self.http.calls]

    def test_the_title_in_focus_is_looked_up_and_fills_its_gaps(self):
        answer_ids(self.http, 7)
        item = film(7, description="")
        self.fill.focus(item, "movie")
        self.assertEqual(self.asked(), [])  # after a pause on it
        self.fill.run_now()
        self.assertEqual(self.asked(), ["7"])
        self.assertEqual(self.changes, [True])
        filled = self.fill.fill(item, "movie")
        self.assertEqual((filled["plot"], filled["year"]), ("Story 7.", "2019"))
        self.assertEqual(
            self.fill.poster_url(item, "movie"), "https://image.tmdb.org/t/p/w342/7.jpg"
        )
        self.assertNotIn("plot", item)

    def test_a_title_known_is_not_asked_again(self):
        answer_ids(self.http, 7)
        self.fill.focus(film(7), "movie")
        self.fill.run_now()
        self.fill.focus(film(7), "movie")  # the same title, a new dict (a reloaded list)
        self.fill.run_now()
        self.assertEqual(self.asked(), ["7"])

    def test_a_later_start_reads_the_disk_cache(self):
        answer_ids(self.http, 7)
        self.fill.focus(film(7), "movie")
        self.fill.run_now()
        later = self.make()
        later.focus(film(7), "movie")
        later.run_now()
        self.assertEqual(self.asked(), ["7"])
        self.assertEqual(later.fill(film(7), "movie")["plot"], "Story 7.")

    def test_series_are_looked_up_as_tv(self):
        self.http.answer(
            "GET",
            f"{API}/tv/8",
            FakeResponse(200, {"id": 8, "name": "Show 8", "overview": "Nights."}),
        )
        self.fill.focus({"name": "Show 8", "tmdb": "8"}, "series")
        self.fill.run_now()
        self.assertEqual(
            self.fill.fill({"name": "Show 8", "tmdb": "8"}, "series")["plot"], "Nights."
        )

    def test_episodes_and_recordings_are_not_looked_up(self):
        self.fill.focus(film(7), "episode")
        self.fill.focus(film(7, _recording=True), "recording")
        self.fill.want_posters([(film(9, _recording=True), "movie")])
        self.fill.run_now()
        self.assertEqual(self.http.calls, [])

    def test_off_or_without_a_token_nothing_is_asked_or_filled(self):
        answer_ids(self.http, 7)
        self.fill.focus(film(7), "movie")
        self.fill.run_now()
        for config in ({"tmdb_details": False}, {"tmdb_token": ""}):
            with self.subTest(config=config):
                self.config.tmdb_details, self.config.tmdb_token = True, "tok"
                for key, value in config.items():
                    setattr(self.config, key, value)
                self.assertEqual(self.fill.fill(film(7), "movie"), film(7))
                self.assertEqual(self.fill.poster_url(film(7), "movie"), "")
                self.fill.focus(film(8), "movie")
                self.fill.run_now()
        self.assertEqual(self.asked(), ["7"])

    def test_posterless_titles_on_screen_are_looked_up_in_batches(self):
        ids = range(1, 2 * BATCH + 2)
        answer_ids(self.http, *ids)
        self.fill.want_posters([(film(n), "movie") for n in ids])
        self.assertEqual(self.asked(), [str(n) for n in ids])  # batch after batch, at once
        self.assertEqual(len(self.changes), 3)
        self.assertTrue(self.fill.poster_url(film(1), "movie").endswith("/1.jpg"))

    def test_the_title_in_focus_goes_first(self):
        answer_ids(self.http, 1, 2, 7)
        self.fill.focus(film(7), "movie")
        self.fill.want_posters([(film(1), "movie"), (film(2), "movie")])
        self.assertEqual(self.asked(), ["7", "1", "2"])

    def test_a_new_screen_replaces_the_titles_still_waiting(self):
        answer_ids(self.http, *range(1, 20))
        jobs = []  # holds the first batch in flight
        self.fill._jobs = SimpleNamespace(run_worker=lambda *a: jobs.append(a))
        self.fill.want_posters([(film(n), "movie") for n in range(1, 10)])
        self.fill.want_posters([(film(n), "movie") for n in (15, 16)])
        self.fill._jobs = ImmediateRunner()
        worker, on_done, _on_error, batch = jobs[0]
        ImmediateRunner().run_worker(worker, on_done, None, batch)
        self.assertEqual(self.asked(), ["1", "2", "3", "4", "15", "16"])

    def test_a_refused_token_is_not_used_again(self):
        self.http.answer("GET", f"{API}/movie/7", FakeResponse(401, {}))
        answer_ids(self.http, 8)
        self.fill.focus(film(7), "movie")
        self.fill.run_now()
        self.fill.focus(film(8), "movie")
        self.fill.run_now()
        self.assertEqual(self.asked(), ["7"])
        self.config.tmdb_token = "new"
        self.fill.run_now()
        self.assertEqual(self.asked(), ["7", "8"])

    def test_a_busy_server_pauses_the_lookups(self):
        self.http.answer(
            "GET", f"{API}/movie/7", FakeResponse(429, {}, headers={"Retry-After": "5"})
        )
        answer_ids(self.http, 8)
        self.fill.focus(film(7), "movie")
        self.fill.run_now()
        self.fill.focus(film(8), "movie")
        self.fill.run_now()
        self.assertEqual(self.asked(), ["7"])
        self.clock[0] += 6
        self.fill.run_now()
        self.assertEqual(self.asked(), ["7", "8"])

    def test_a_title_that_could_not_be_asked_waits_before_another_try(self):
        self.http.answer("GET", f"{API}/movie/7", OSError("down"))
        self.fill.focus(film(7), "movie")
        self.fill.run_now()
        self.fill.focus(film(7), "movie")
        self.fill.run_now()
        self.assertEqual(self.asked(), ["7"])
        self.clock[0] += RETRY_S + 1
        self.fill.focus(film(7), "movie")
        self.fill.run_now()
        self.assertEqual(self.asked(), ["7", "7"])

    def test_after_shutdown_nothing_is_asked(self):
        self.fill.shutdown()
        self.fill.focus(film(7), "movie")
        self.fill.want_posters([(film(8), "movie")])
        self.fill.run_now()
        self.assertEqual(self.http.calls, [])


if __name__ == "__main__":
    unittest.main()
