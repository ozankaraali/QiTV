"""A phone's Library (ui/remote_library.py): Movies and Series by genre, a page at a time, from
its own LibraryModel, so browsing on the phone leaves the TV's Library where it is."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import unittest

from PySide6.QtCore import QCoreApplication
from test_library_model import FakeCatalog, history, make_config, movie, resume
from timer_fixtures import wait_until

from services.library import LibraryModel
from services.watch_state import WatchState
from ui.remote_library import LIBRARY_PAGE, MAX_BYTES, PhoneLibrary


def catalog_with(count=120):
    titles = [movie(i, year="1998", cmd=f"http://example.invalid/{i}") for i in range(count)]
    return FakeCatalog(
        {"movies": [("1", "Drama", count), ("2", "Comedy", 1)], "series": [("9", "Crime", 1)]},
        {("movies", "1"): titles, ("movies", "2"): [movie(500, "Comedy one")]},
    )


class PhoneLibraryTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog_with()
        self.config = make_config()
        self.sent = []
        self.library = self.make()

    def make(self):
        library = PhoneLibrary(self.catalog, self.config, WatchState(self.config), self.sent.append)
        self.addCleanup(library.deleteLater)
        return library

    def ready(self, section="movies"):
        self.catalog.sectionReady.emit(section)

    def last(self):
        return self.sent[-1]

    def test_asks_its_section_and_says_it_is_loading(self):
        self.library.ask("movies", "", 0)
        self.assertIn(("section", "movies"), self.catalog.requests)
        message = self.last()
        self.assertEqual(
            (message["t"], message["section"], message["page"]), ("library", "movies", 0)
        )
        self.assertTrue(message["loading"])

    def test_once_ready_pushes_genres_and_the_first_page(self):
        self.library.ask("movies", "", 0)
        self.ready()
        self.assertTrue(wait_until(lambda: not self.last()["loading"]))
        message = self.last()
        self.assertEqual([g["title"] for g in message["genres"]], ["Drama", "Comedy"])
        self.assertEqual(message["genres"][0]["count"], 120)
        self.assertEqual(message["genre"], message["genres"][0]["id"])
        self.assertEqual(len(message["items"]), LIBRARY_PAGE)
        self.assertEqual(message["pages"], 3)
        item = message["items"][0]
        self.assertEqual((item["title"], item["sub"]), ("Movie 0", "1998"))
        self.assertNotIn("http", json.dumps(message))

    def test_pages_follow_without_the_genres(self):
        self.library.ask("movies", "", 0)
        self.ready()
        self.library.ask("movies", self.last()["genre"], 2)
        message = self.last()
        self.assertEqual((message["page"], len(message["items"])), (2, 20))
        self.assertNotIn("genres", message)

    def test_genre_keys_are_opaque_and_choose_the_genre(self):
        self.library.ask("movies", "", 0)
        self.ready()
        genres = self.last()["genres"]
        self.assertNotIn(genres[1]["id"], ("1", "2"))
        self.library.ask("movies", genres[1]["id"], 0)
        message = self.last()
        self.assertEqual(message["genre"], genres[1]["id"])
        self.assertEqual([i["title"] for i in message["items"]], ["Comedy one"])
        self.library.ask("movies", "made-up", 0)  # an unknown key keeps the genre shown
        self.assertEqual(self.last()["genre"], genres[1]["id"])

    def test_browsing_leaves_the_tvs_library_alone(self):
        tv = LibraryModel(self.catalog, self.config, WatchState(self.config))
        self.addCleanup(tv.deleteLater)
        tv.set_section("movies")
        self.ready()
        tv.select_genre("2")
        self.library.ask("series", "", 0)
        self.ready("series")
        self.library.ask("movies", "", 0)
        self.assertEqual((tv.section, tv.genre_id), ("movies", "2"))

    def test_continue_comes_first_with_its_progress(self):
        watched = movie(3)
        history(self.config, watched, "movie")
        resume(self.config, watched, "movie", pos=600_000, dur=6_000_000)
        self.library.ask("movies", "", 0)
        self.ready()
        self.assertTrue(wait_until(lambda: not self.last()["loading"]))
        message = self.last()
        self.assertEqual(message["genres"][0]["title"], "Continue")
        self.assertEqual(message["genre"], message["genres"][0]["id"])
        [item] = message["items"]
        self.assertAlmostEqual(item["progress"], 0.1)

    def test_provider_headings_are_not_genres(self):
        self.catalog.group_list["movies"].insert(0, ("h", "Provider A", None))
        self.catalog.headers = {"h"}
        self.library.ask("movies", "", 0)
        self.ready()
        heading = self.last()["genres"][0]
        self.assertEqual(heading, {"title": "Provider A", "header": True})

    def test_nothing_changed_nothing_pushed(self):
        self.library.ask("movies", "", 0)
        self.ready()
        self.assertTrue(wait_until(lambda: not self.last()["loading"]))
        count = len(self.sent)
        self.library.model.changed.emit()
        for _ in range(5):
            QCoreApplication.processEvents()
        self.assertFalse(wait_until(lambda: len(self.sent) > count, timeout_ms=600))

    def test_a_failed_load_says_so_and_asking_again_retries(self):
        self.library.ask("movies", "", 0)
        self.catalog.sectionFailed.emit("movies", "offline")
        self.assertTrue(wait_until(lambda: self.last().get("failed")))
        before = self.catalog.requests.count(("section", "movies"))
        self.library.ask("movies", "", 0)
        self.assertEqual(self.catalog.requests.count(("section", "movies")), before + 1)

    def test_a_tap_finds_the_title_and_its_genre(self):
        self.library.ask("movies", "", 0)
        self.ready()
        self.assertTrue(wait_until(lambda: not self.last()["loading"]))
        first = self.last()["items"][0]
        kind, (item, item_kind, genre_items) = self.library.handles.get(first["id"])
        self.assertEqual((kind, item["name"], item_kind), ("title", "Movie 0", "movie"))
        self.assertEqual(len(genre_items), 120)

    def test_a_long_genre_list_fits_a_frame(self):
        many = [(str(i), f"Genre {i} " + "x" * 120, 1) for i in range(3000)]
        self.catalog.group_list["movies"] = many
        self.catalog.item_map[("movies", "2999")] = [movie(1)]
        self.library.ask("movies", "", 0)
        self.ready()
        last_key = None
        for genre in self.last()["genres"]:
            last_key = genre["id"]
        self.library.ask("movies", last_key, 0)
        message = self.last()
        size = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(size, MAX_BYTES)
        self.assertIn(message["genre"], [g.get("id") for g in message["genres"]])


if __name__ == "__main__":
    unittest.main()
