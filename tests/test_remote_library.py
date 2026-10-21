"""A phone's Library (ui/remote_library.py): a section's genres, then a genre's titles, a page at
a time, each found by the find box; from its own LibraryModel over the TV's cache, so browsing on
the phone leaves the TV's Library where it is and sorts nothing the TV sorted."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication
from test_library_model import FakeCatalog, history, make_config, movie, resume
from timer_fixtures import wait_until

from services import library
from services.library import LibraryCache, LibraryModel
from services.remote.protocol import MAX_MESSAGE
from services.watch_state import WatchState
from ui.remote_library import GENRES_PAGE, LIBRARY_PAGE, PhoneLibrary

FRAME = 65535


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
        self.cache = LibraryCache()
        self.library = self.make()

    def make(self):
        library = PhoneLibrary(
            self.catalog, self.config, WatchState(self.config), self.sent.append, cache=self.cache
        )
        self.addCleanup(library.deleteLater)
        return library

    def ready(self, section="movies"):
        self.catalog.sectionReady.emit(section)

    def last(self, kind=None):
        found = [m for m in self.sent if kind is None or m["t"] == kind]
        return found[-1] if found else None

    def genre_keys(self):
        return {g["title"]: g["id"] for g in self.last("genres")["rows"] if "id" in g}

    def opened(self):
        self.library.genres("movies", 0, "")
        self.ready()
        self.assertTrue(wait_until(lambda: not self.last("genres")["loading"]))
        return self.genre_keys()

    def test_genres_load_then_arrive(self):
        self.library.genres("movies", 0, "")
        self.assertIn(("section", "movies"), self.catalog.requests)
        self.assertTrue(self.last("genres")["loading"])
        self.ready()
        self.assertTrue(wait_until(lambda: not self.last("genres")["loading"]))
        rows = self.last("genres")["rows"]
        self.assertEqual([g["title"] for g in rows], ["Drama", "Comedy"])
        self.assertEqual(rows[0]["count"], 120)
        self.assertNotIn(rows[0]["id"], ("1", "2"))  # the phone sees keys, not genre ids

    def test_find_searches_the_genres(self):
        self.opened()
        self.library.genres("movies", 0, "com")
        self.assertEqual([g["title"] for g in self.last("genres")["rows"]], ["Comedy"])

    def test_finding_genres_finds_titles_too_as_the_desktop_search_does(self):
        self.opened()
        self.library.genres("movies", 0, "comedy")
        message = self.last("genres")
        self.assertEqual([g["title"] for g in message["rows"]], ["Comedy"])
        self.assertEqual([t["title"] for t in message["titles"]], ["Comedy one"])
        self.library.genres("movies", 0, "one comedy")  # every word, in any order
        self.assertEqual([t["title"] for t in self.last("genres")["titles"]], ["Comedy one"])
        kind, (item, item_kind, _genre) = self.library.handles.get(
            self.last("genres")["titles"][0]["id"]
        )
        self.assertEqual((kind, item["name"], item_kind), ("title", "Comedy one", "movie"))

    def test_a_found_series_is_a_series_while_an_episode_is_in_progress(self):
        series = {"id": "s1", "name": "Harbour Watch"}
        self.catalog.item_map[("series", "9")] = [series]
        episode = {"id": "e1", "name": "Harbour Watch 1x01", "series_name": "Harbour Watch"}
        history(self.config, episode, "episode")
        resume(self.config, episode, "episode")
        self.library.genres("series", 0, "")
        self.ready("series")
        self.assertTrue(wait_until(lambda: not self.last("genres")["loading"]))
        self.assertEqual(self.library.model.genre_id, library.CONTINUE)  # the shelf it sits on
        self.library.genres("series", 0, "watch")
        found = self.last("genres")["titles"]
        self.assertEqual([t["title"] for t in found], ["Harbour Watch"])
        kind, (item, item_kind, _genre) = self.library.handles.get(found[0]["id"])
        self.assertEqual((item["id"], item_kind), ("s1", "series"))

    def test_found_titles_come_a_page_at_a_time_the_first_word_first(self):
        self.catalog.item_map[("movies", "2")].append(movie(501, "Old movie"))
        self.opened()
        self.library.genres("movies", 0, "movie")
        message = self.last("genres")
        self.assertEqual(message["found"], 121)
        self.assertEqual((len(message["titles"]), message["pages"]), (LIBRARY_PAGE, 3))
        self.assertEqual(message["titles"][0]["title"], "Movie 0")  # starts with the word
        self.library.genres("movies", 2, "movie")
        last = self.last("genres")
        self.assertEqual(last["rows"], [])  # the genres came with the first page
        self.assertEqual(last["titles"][-1]["title"], "Old movie")  # only has the word

    def test_one_letter_finds_genres_only(self):
        self.opened()
        self.library.genres("movies", 0, "c")
        message = self.last("genres")
        self.assertEqual([g["title"] for g in message["rows"]], ["Comedy"])
        self.assertEqual(message["titles"], [])

    def test_a_genres_titles_a_page_at_a_time(self):
        drama = self.opened()["Drama"]
        self.library.titles("movies", drama, 0, "")
        message = self.last("library")
        self.assertEqual((message["genre"], message["title"]), (drama, "Drama"))
        self.assertEqual((message["count"], message["pages"]), (120, 3))
        self.assertEqual(len(message["items"]), LIBRARY_PAGE)
        first = message["items"][0]
        self.assertEqual((first["title"], first["sub"]), ("Movie 0", "1998"))
        self.library.titles("movies", drama, 2, "")
        self.assertEqual(len(self.last("library")["items"]), 20)
        self.assertNotIn("http", json.dumps(self.sent))

    def test_find_searches_the_whole_genre(self):
        drama = self.opened()["Drama"]
        self.library.titles("movies", drama, 0, "movie 11")
        titles = [i["title"] for i in self.last("library")["items"]]
        self.assertEqual(titles, ["Movie 11"] + [f"Movie {i}" for i in range(110, 120)])

    def test_typing_folds_the_genres_titles_once(self):
        drama = self.opened()["Drama"]
        self.library.titles("movies", drama, 0, "")
        with mock.patch.object(library, "fold", wraps=library.fold) as spy:
            self.library.titles("movies", drama, 0, "movie 1")
            folded = spy.call_count
            self.library.titles("movies", drama, 0, "movie 11")
        self.assertGreaterEqual(folded, 120)
        self.assertLessEqual(spy.call_count - folded, 1)  # only the find itself

    def test_browsing_leaves_the_tvs_library_alone(self):
        tv = LibraryModel(self.catalog, self.config, WatchState(self.config), cache=self.cache)
        self.addCleanup(tv.deleteLater)
        tv.set_section("movies")
        self.ready()
        tv.select_genre("2")
        keys = self.opened()
        self.library.titles("movies", keys["Drama"], 0, "")
        self.library.genres("series", 0, "")
        self.ready("series")
        self.assertEqual((tv.section, tv.genre_id), ("movies", "2"))

    def test_the_tvs_sorted_genre_is_not_sorted_again(self):
        tv = LibraryModel(self.catalog, self.config, WatchState(self.config), cache=self.cache)
        self.addCleanup(tv.deleteLater)
        tv.set_section("movies")
        self.ready()
        tv.select_genre("1")
        tv.set_sort(library.Sort.AZ)
        tv.items()
        drama = self.opened()["Drama"]
        self.library.model.set_sort(library.Sort.AZ)
        with mock.patch.object(library, "_az_key", wraps=library._az_key) as spy:
            self.library.titles("movies", drama, 0, "")
        self.assertEqual(spy.call_count, 0)

    def test_continue_comes_first_with_its_progress(self):
        watched = movie(3)
        history(self.config, watched, "movie")
        resume(self.config, watched, "movie", pos=600_000, dur=6_000_000)
        keys = self.opened()
        self.assertEqual(next(iter(keys)), "Continue")
        self.library.titles("movies", "", 0, "")  # "": the first shelf
        message = self.last("library")
        self.assertEqual(message["title"], "Continue")
        [item] = message["items"]
        self.assertAlmostEqual(item["progress"], 0.1)

    def test_provider_headings_are_not_genres(self):
        self.catalog.group_list["movies"].insert(0, ("h", "Provider A", None))
        self.catalog.headers = {"h"}
        self.opened()
        self.assertEqual(self.last("genres")["rows"][0], {"title": "Provider A", "header": True})

    def test_nothing_changed_nothing_pushed(self):
        self.opened()
        count = len(self.sent)
        self.library.model.changed.emit()
        for _ in range(5):
            QCoreApplication.processEvents()
        self.assertFalse(wait_until(lambda: len(self.sent) > count, timeout_ms=600))

    def test_a_failed_load_says_so_and_asking_again_retries(self):
        self.catalog.group_list["movies"] = []  # nothing came
        self.library.genres("movies", 0, "")
        self.catalog.sectionFailed.emit("movies", "offline")
        self.assertTrue(wait_until(lambda: self.last("genres").get("failed")))
        before = self.catalog.requests.count(("section", "movies"))
        self.library.genres("movies", 0, "")
        self.assertEqual(self.catalog.requests.count(("section", "movies")), before + 1)

    def test_a_tap_finds_the_title_and_its_genre(self):
        drama = self.opened()["Drama"]
        self.library.titles("movies", drama, 0, "")
        first = self.last("library")["items"][0]
        kind, (item, item_kind, genre_items) = self.library.handles.get(first["id"])
        self.assertEqual((kind, item["name"], item_kind), ("title", "Movie 0", "movie"))
        self.assertEqual(len(genre_items), 120)

    def test_the_largest_pages_fit_a_frame_in_any_script(self):
        long = "字" * 300  # 3 bytes a letter; titles are cut to what a row shows
        titles = [movie(i, f"{long}{i}", year=long) for i in range(120)]
        genres = [(str(i), f"{long}{i}", 120) for i in range(150)]
        self.catalog.group_list["movies"] = genres
        for gid, _title, _count in genres:
            self.catalog.item_map[("movies", gid)] = titles
        keys = self.opened()
        self.library.genres("movies", 0, "字字")  # 100 genres and 50 titles at once
        self.library.titles("movies", next(iter(keys.values())), 0, "")
        for message in (self.last("genres"), self.last("library")):
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
            self.assertLessEqual(len(encoded), MAX_MESSAGE, message["t"])
        self.assertEqual(len(self.last("genres")["titles"]), LIBRARY_PAGE)

    def test_many_genres_come_a_page_at_a_time_inside_a_frame(self):
        many = [(str(i), f"Genre {i} " + "x" * 200, 1) for i in range(450)]
        self.catalog.group_list["movies"] = many
        self.opened()
        message = self.last("genres")
        self.assertEqual((len(message["rows"]), message["pages"]), (GENRES_PAGE, 5))
        self.assertLess(len(json.dumps(message, ensure_ascii=False).encode()), FRAME // 2)


if __name__ == "__main__":
    unittest.main()
