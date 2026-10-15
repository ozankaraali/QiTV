import os
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from services import library
from services.library import (
    CONTINUE,
    MY_LIST,
    NEW,
    LibraryModel,
    Sort,
    history_episode,
    history_season,
)
from services.watch_state import WatchState

app = QApplication.instance() or QApplication([])


class Group(SimpleNamespace):
    pass


class FakeCatalog(QObject):
    sectionReady = Signal(str)
    sectionFailed = Signal(str, str)
    groupReady = Signal(str, str)
    groupFailed = Signal(str, str, str)
    providerReady = Signal(str)

    def __init__(self, groups=None, items=None):
        super().__init__()
        self.group_list = groups or {}  # section -> [(id, title, count)]
        self.item_map = items or {}  # (section, gid) -> list | None
        self.requests = []
        self.name = "P"
        self.version = 1
        self.section_list = ["live", "movies", "series"]

    def sections(self):
        return list(self.section_list)

    def current_provider_name(self):
        return self.name

    def content_version(self):
        return self.version

    def groups(self, section):
        return [
            Group(section=section, id=i, title=t, count=c)
            for i, t, c in self.group_list.get(section, [])
        ]

    def items(self, section, gid):
        v = self.item_map.get((section, gid))
        return None if v is None else list(v)

    def request_section(self, section, *, force=False):
        self.requests.append(("section", section))

    def request_group(self, section, gid):
        self.requests.append(("group", section, gid))


def make_config(**kw):
    cfg = SimpleNamespace(favorites=[], watch_history=[], playback_positions={}, saves=0)
    cfg.save_config = lambda: setattr(cfg, "saves", cfg.saves + 1)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def movie(i, name=None, **kw):
    return {"id": str(i), "name": name or f"Movie {i}", **kw}


def build(catalog, cfg=None, section="movies"):
    cfg = cfg or make_config()
    model = LibraryModel(catalog, cfg, WatchState(cfg))
    model.set_section(section)
    catalog.sectionReady.emit(section)
    return model, cfg


def history(cfg, item, item_type, provider="P"):
    cfg.watch_history.append({"item_data": item, "item_type": item_type, "provider_name": provider})


def resume(cfg, item, item_type, pos=60000, dur=6000000, provider="P"):
    cid = WatchState.content_id(provider, item, item_type=item_type)
    cfg.playback_positions[cid] = {"position": pos, "duration": dur}


class LibraryModelTests(unittest.TestCase):
    def test_section_request_and_pinned_visibility(self):
        cat = FakeCatalog(
            {"movies": [("1", "Drama", 2), ("2", "Comedy", 1)]},
            {("movies", "1"): [movie(1), movie(2)], ("movies", "2"): [movie(3)]},
        )
        cfg = make_config()
        model = LibraryModel(cat, cfg, WatchState(cfg))
        model.set_section("movies")
        self.assertIn(("section", "movies"), cat.requests)
        cat.sectionReady.emit("movies")
        self.assertEqual([g.id for g in model.genres()], ["1", "2"])
        self.assertEqual(model.genre_id, "1")
        self.assertEqual(model.genres()[0].count, 2)
        cfg.favorites.append("Movie 1")
        self.assertEqual([g.id for g in model.genres()], [MY_LIST, "1", "2"])
        self.assertTrue(model.genres()[0].pinned)

    def test_continue_movies_and_series(self):
        m1, m2, m3 = movie(1), movie(2), movie(3)
        cfg = make_config()
        history(cfg, m2, "movie")
        history(cfg, m1, "movie")
        history(cfg, m3, "movie")  # no resume point
        history(cfg, movie(4), "movie", provider="Other")
        for m in (m1, m2):
            resume(cfg, m, "movie")
        resume(cfg, movie(4), "movie", provider="Other")
        ep = {"id": "9", "name": "Pilot", "season": "1", "series": "2"}
        history(cfg, ep, "episode")
        resume(cfg, ep, "episode")
        cat = FakeCatalog(
            {"movies": [("1", "G", 3)], "series": [("1", "S", 1)]},
            {("movies", "1"): [m1, m2, m3], ("series", "1"): [movie(7)]},
        )
        model, _ = build(cat, cfg)
        self.assertEqual(model.genres()[0].id, CONTINUE)
        self.assertEqual(model.genre_id, CONTINUE)
        self.assertEqual([i["id"] for i in model.items()], ["2", "1"])
        series, _ = build(cat, cfg, "series")
        self.assertEqual([i["name"] for i in series.items()], ["Pilot"])
        self.assertEqual(series.item_kind(series.items()[0]), "episode")
        self.assertEqual(series.details(series.items()[0]).kind, "episode")

    def test_new_group(self):
        items = [movie(i, added=str(1700000000 + i * 100)) for i in range(60)]
        items.append(movie(100, added="2030-01-01 10:00:00"))
        items.append(movie(101))
        cat = FakeCatalog({"movies": [("1", "G", len(items))]}, {("movies", "1"): items})
        model, _ = build(cat)
        self.assertEqual(model.genres()[0].id, NEW)
        got = model.items()
        self.assertEqual(len(got), 48)
        self.assertEqual(got[0]["id"], "100")
        self.assertEqual(got[1]["id"], "59")
        plain = FakeCatalog({"movies": [("1", "G", 1)]}, {("movies", "1"): [movie(1)]})
        model, _ = build(plain)
        self.assertEqual([g.id for g in model.genres()], ["1"])

    def test_favorites_toggle(self):
        cat = FakeCatalog({"movies": [("1", "G", 2)]}, {("movies", "1"): [movie(1), movie(2)]})
        model, cfg = build(cat)
        spy = []
        model.changed.connect(lambda: spy.append(1))
        item = movie(1)
        self.assertTrue(model.toggle_favorite(item))
        self.assertEqual(cfg.favorites, ["Movie 1"])
        self.assertEqual(cfg.saves, 1)
        self.assertTrue(model.is_favorite(item))
        self.assertEqual([g.id for g in model.genres()][0], MY_LIST)
        self.assertFalse(model.toggle_favorite(item))
        self.assertEqual(cfg.favorites, [])
        self.assertEqual(cfg.saves, 2)
        self.assertTrue(spy)

    def test_toggle_survives_save_failure(self):
        cat = FakeCatalog({"movies": [("1", "G", 1)]}, {("movies", "1"): [movie(1)]})
        model, cfg = build(cat)

        def boom():
            raise PermissionError("locked")

        cfg.save_config = boom
        with self.assertLogs("services.library", level="WARNING"):
            self.assertTrue(model.toggle_favorite(movie(1)))
        self.assertEqual(cfg.favorites, ["Movie 1"])

    def test_genre_filter_folds(self):
        cat = FakeCatalog(
            {"movies": [("1", "İstanbul Films", 1), ("2", "Drama", 1)]},
            {("movies", "1"): [movie(1)]},
        )
        model, _ = build(cat)
        self.assertEqual([g.title for g in model.genres("İst")], ["İstanbul Films"])
        self.assertEqual([g.title for g in model.genres("ist")], ["İstanbul Films"])

    def _sorted_model(self, items):
        cat = FakeCatalog({"movies": [("1", "G", len(items))]}, {("movies", "1"): items})
        model, _ = build(cat)
        model.select_genre("1")
        return model, cat

    def test_sorts(self):
        items = [
            movie(1, "Title 10", year="2001", rating="5.5", added="100"),
            movie(2, "Title 2", releasedate="1999-05-01", rating_5based="4", added="300"),
            movie(3, "Zed (2020)", rating_imdb="9.1"),
            movie(4, "arrival"),
        ]
        model, _ = self._sorted_model(items)
        self.assertEqual(model.sort, Sort.ADDED)
        # New is selected by default (dated items exist); pick the catalog genre
        model.select_genre("1")
        self.assertEqual([i["id"] for i in model.items()], ["2", "1", "3", "4"])
        model.set_sort(Sort.AZ)
        self.assertEqual([i["id"] for i in model.items()], ["4", "2", "1", "3"])
        model.set_sort(Sort.YEAR)
        self.assertEqual([i["id"] for i in model.items()], ["3", "1", "2", "4"])
        model.set_sort(Sort.RATING)
        self.assertEqual([i["id"] for i in model.items()], ["3", "2", "1", "4"])

    def test_available_sorts_and_default(self):
        model, _ = self._sorted_model([movie(1)])
        model.select_genre("1")
        self.assertNotIn(Sort.ADDED, model.available_sorts())
        self.assertEqual(model.sort, Sort.AZ)
        self.assertEqual(len(model.available_sorts()), 3)

    def test_letter_index(self):
        items = [
            movie(1, "10 Things"),
            movie(2, "Arrival"),
            movie(3, "Avatar"),
            movie(4, "Brazil"),
            movie(5, "Émile"),
        ]
        model, _ = self._sorted_model(items)
        model.select_genre("1")
        self.assertEqual(model.sort, Sort.AZ)
        self.assertEqual(model.letter_index(), {"#": 0, "a": 1, "b": 3, "e": 4})
        model.set_sort(Sort.YEAR)
        self.assertEqual(model.letter_index(), {})

    def test_details(self):
        cfg = make_config()
        m = movie(1, duration="01:38:00", rating="7.83", plot="P", genre="Drama", year="2010")
        resume(cfg, m, "movie", pos=2940000, dur=5880000)
        cat = FakeCatalog({"movies": [("1", "Genre One", 1)]}, {("movies", "1"): [m]})
        model, _ = build(cat, cfg)
        model.select_genre("1")
        d = model.details(m)
        self.assertEqual(
            (d.title, d.year, d.duration, d.genre, d.rating, d.plot),
            ("Movie 1", "2010", "1h 38m", "Drama", "7.8", "P"),
        )
        self.assertEqual(d.resume_ms, 2940000)
        self.assertAlmostEqual(d.progress, 0.5)
        self.assertEqual(d.kind, "movie")
        self.assertEqual(model.details(movie(2, duration="42")).duration, "42m")
        self.assertEqual(model.details(movie(2, duration_secs=5400)).duration, "1h 30m")
        self.assertEqual(model.details(movie(2, time="95")).duration, "1h 35m")
        self.assertEqual(model.details(movie(2, rating_5based="4")).rating, "8.0")
        self.assertEqual(model.details(movie(2)).genre, "Genre One")
        self.assertEqual(model.details(movie(2)).rating, "")
        self.assertIsNone(model.details(movie(2)).resume_ms)

    def test_select_genre_requests_and_failures(self):
        cat = FakeCatalog(
            {"movies": [("1", "A", None), ("2", "B", None)]},
            {("movies", "1"): None, ("movies", "2"): None},
        )
        model, _ = build(cat)
        self.assertEqual(cat.requests.count(("group", "movies", "1")), 1)
        self.assertIsNone(model.items())
        changed, failed = [], []
        model.changed.connect(lambda: changed.append(1))
        model.failed.connect(failed.append)
        cat.groupFailed.emit("movies", "2", "other")
        cat.groupFailed.emit("series", "1", "other section")
        self.assertEqual(failed, [])
        cat.groupFailed.emit("movies", "1", "Couldn't load")
        self.assertEqual(failed, ["Couldn't load"])
        cat.item_map[("movies", "1")] = [movie(1)]
        cat.version += 1
        cat.groupReady.emit("movies", "2")
        self.assertEqual(changed, [])
        cat.groupReady.emit("movies", "1")
        self.assertEqual(len(changed), 1)
        self.assertEqual(len(model.items()), 1)

    def test_section_failure(self):
        cat = FakeCatalog()
        cfg = make_config()
        model = LibraryModel(cat, cfg, WatchState(cfg))
        failed = []
        model.failed.connect(failed.append)
        model.set_section("movies")
        cat.sectionFailed.emit("series", "x")
        cat.sectionFailed.emit("movies", "The provider is still loading")
        self.assertEqual(failed, ["The provider is still loading"])

    def test_provider_ready_resets_and_requests(self):
        cat = FakeCatalog({"movies": [("1", "A", 1)]}, {("movies", "1"): [movie(1)]})
        model, _ = build(cat)
        cat.requests.clear()
        cat.name = "Q"
        cat.providerReady.emit("Q")
        self.assertEqual(cat.requests, [("section", "movies")])
        self.assertIsNone(model.genre_id)
        self.assertIsNone(model.items())
        cat.sectionReady.emit("movies")
        self.assertEqual(model.genre_id, "1")

    def test_stale_provider_ignored(self):
        cat = FakeCatalog({"movies": [("1", "A", 1)]}, {("movies", "1"): [movie(1)]})
        model = LibraryModel(cat, make_config(), WatchState(make_config()))
        model.set_section("movies")
        cat.name = "Other"
        cat.sectionReady.emit("movies")
        self.assertIsNone(model.genre_id)

    def test_items_memoized(self):
        items = [movie(i, f"T{i}") for i in range(2000)]
        model, cat = self._sorted_model(items)
        model.select_genre("1")
        model.set_sort(Sort.AZ)
        model.items()
        with mock.patch.object(library, "_az_key", wraps=library._az_key) as spy:
            a = model.items()
            b = model.items()
            model.genres()
            self.assertEqual(spy.call_count, 0)
            self.assertEqual(a, b)
            cat.version += 1
            model.items()
            self.assertEqual(spy.call_count, 2000)


class FixRoundTests(unittest.TestCase):
    def test_continue_keeps_stb_episodes_of_one_series_id(self):
        cfg = make_config()
        eps = [{"id": "7", "name": "Show", "season": "1", "series": str(n)} for n in (1, 2)]
        for ep in eps:
            history(cfg, ep, "episode")
            resume(cfg, ep, "episode")
        cat = FakeCatalog({"series": [("1", "S", 1)]}, {("series", "1"): [movie(7)]})
        model, _ = build(cat, cfg, "series")
        self.assertEqual([i["series"] for i in model.items()], ["1", "2"])

    def test_letter_index_memoized(self):
        items = [movie(i, f"T{i}") for i in range(500)]
        cat = FakeCatalog({"movies": [("1", "G", 500)]}, {("movies", "1"): items})
        model, _ = build(cat)
        model.select_genre("1")
        model.letter_index()
        with mock.patch.object(library, "fold", wraps=library.fold) as spy:
            model.letter_index()
            self.assertEqual(spy.call_count, 0)

    def test_shared_title_and_letter_helpers(self):
        self.assertEqual(library.title_of({"name": "Éclair", "title": "x"}), "Éclair")
        self.assertEqual(library.title_of({"title": 1917}), "1917")
        self.assertEqual(library.title_of({}), "")
        self.assertEqual(library.letter_of("Éclair"), "e")
        self.assertEqual(library.letter_of("İstanbul"), "i")
        self.assertEqual(library.letter_of("1917"), "#")
        self.assertEqual(library.letter_of("#"), "#")
        self.assertEqual(library.letter_of(""), "#")
        self.assertIs(library._title, library.title_of)
        self.assertEqual(library._letter({"name": "Zorro"}), "z")

    def test_parsing_edge_cases(self):
        self.assertEqual(library._minutes({"duration_secs": 0, "duration": "42"}), 42)
        self.assertIsNone(library._year({"year": "0000", "name": "X (1700)"}))
        self.assertEqual(library._year({"year": "0000", "name": "X (1999)"}), 1999)
        self.assertEqual(library._added({"added": "1700000000000"}), 1700000000.0)

    def test_emptied_pinned_genre_moves_selection(self):
        cat = FakeCatalog({"movies": [("1", "G", 1)]}, {("movies", "1"): [movie(1)]})
        cfg = make_config(favorites=["Movie 1"])
        model, _ = build(cat, cfg)
        self.assertEqual(model.genre_id, MY_LIST)
        model.toggle_favorite(movie(1))
        self.assertEqual(model.genre_id, "1")

    def test_group_ready_for_other_genre_updates_new(self):
        cat = FakeCatalog(
            {"movies": [("1", "A", 1), ("2", "B", None)]},
            {("movies", "1"): [movie(1, added="1700000000")], ("movies", "2"): None},
        )
        model, _ = build(cat)
        self.assertEqual(model.genre_id, NEW)
        self.assertEqual(model.genres()[0].count, 1)
        changed = []
        model.changed.connect(lambda: changed.append(1))
        cat.groupReady.emit("movies", "2")
        self.assertEqual(changed, [])
        cat.item_map[("movies", "2")] = [movie(2, added="1700000500")]
        cat.group_list["movies"][1] = ("2", "B", 1)
        cat.version += 1
        cat.groupReady.emit("movies", "2")
        self.assertEqual(len(changed), 1)
        self.assertEqual(model.genres()[0].count, 2)

    def test_section_ready_emits_changed_once(self):
        cat = FakeCatalog({"movies": [("1", "A", 1)]}, {("movies", "1"): [movie(1)]})
        cfg = make_config()
        model = LibraryModel(cat, cfg, WatchState(cfg))
        model.set_section("movies")
        changed = []
        model.changed.connect(lambda: changed.append(1))
        cat.sectionReady.emit("movies")
        self.assertEqual(len(changed), 1)

    def test_save_failure_emits_notice(self):
        cat = FakeCatalog({"movies": [("1", "G", 1)]}, {("movies", "1"): [movie(1)]})
        model, cfg = build(cat)
        notices = []
        model.notice.connect(notices.append)

        def boom():
            raise PermissionError("locked")

        cfg.save_config = boom
        with self.assertLogs("services.library", level="WARNING"):
            model.toggle_favorite(movie(1))
        self.assertEqual(notices, ["Couldn't save your list; it will be lost when QiTV closes"])


class SectionAvailabilityTests(unittest.TestCase):
    def test_a_section_the_provider_lacks_is_empty_without_a_request(self):
        cat = FakeCatalog({"movies": [("1", "A", 1)]}, {("movies", "1"): [movie(1)]})
        cat.section_list = ["live"]
        cfg = make_config()
        model = LibraryModel(cat, cfg, WatchState(cfg))
        model.set_section("movies")
        self.assertEqual(cat.requests, [])
        self.assertEqual(model.items(), [])
        self.assertEqual(model.genres(), [])

    def test_provider_ready_skips_a_section_the_new_provider_lacks(self):
        cat = FakeCatalog({"movies": [("1", "A", 1)]}, {("movies", "1"): [movie(1)]})
        model, _ = build(cat)
        cat.requests.clear()
        cat.name, cat.section_list = "M3U", ["live"]
        cat.version += 1
        cat.providerReady.emit("M3U")
        self.assertEqual(cat.requests, [])
        self.assertEqual(model.items(), [])


class HistorySeasonTests(unittest.TestCase):
    SEASONS = [{"id": "1", "number": "1"}, {"id": "2", "number": "2"}, {"id": "3", "number": "3"}]

    def entry(self, episode, provider="P", item_type="episode"):
        return {"item_data": episode, "item_type": item_type, "provider_name": provider}

    def test_newest_matching_entry_names_the_season(self):
        history = [
            self.entry({"id": "9", "season_id": "3"}),  # another series
            self.entry({"id": "7", "season_id": "2", "season": "2"}),
            self.entry({"id": "7", "season_id": "1"}),
        ]
        self.assertIs(history_season(history, "P", {"id": "7"}, self.SEASONS), self.SEASONS[1])

    def test_series_id_and_season_number(self):
        history = [self.entry({"id": "e5", "series_id": "7", "season": "3"})]
        self.assertIs(history_season(history, "P", {"id": "7"}, self.SEASONS), self.SEASONS[2])

    def test_history_episode_is_the_newest_entry_of_the_series(self):
        newest = {"id": "7", "season_id": "2", "series": "4"}
        history = [
            self.entry({"id": "9", "season_id": "3"}),
            self.entry(newest),
            self.entry({"id": "7", "season_id": "1", "series": "1"}),
        ]
        self.assertIs(history_episode(history, "P", {"id": "7"}), newest)
        self.assertIsNone(history_episode(history, "Q", {"id": "7"}))

    def test_unknown(self):
        series = {"id": "7"}
        self.assertIsNone(history_season([], "P", series, self.SEASONS))
        other = [self.entry({"id": "7", "season_id": "2"}, provider="Q")]
        self.assertIsNone(history_season(other, "P", series, self.SEASONS))
        movie_entry = [self.entry({"id": "7", "season_id": "2"}, item_type="movie")]
        self.assertIsNone(history_season(movie_entry, "P", series, self.SEASONS))
        xtream = [self.entry({"id": "7", "season": "2"})]  # an Xtream episode id, no series
        self.assertIsNone(history_season(xtream, "P", series, self.SEASONS))
        gone = [self.entry({"id": "7", "season_id": "8"})]
        self.assertIsNone(history_season(gone, "P", series, self.SEASONS))
        self.assertIsNone(history_season([None, "x"], "P", series, self.SEASONS))


class LargeGenreTests(unittest.TestCase):
    def test_opening_a_large_genre_parses_each_added_date_once(self):
        items = [movie(i, added=str(1700000000 + i)) for i in range(3000)]
        cat = FakeCatalog(
            {"movies": [("1", "Big", len(items)), ("2", "Small", 10)]},
            {("movies", "1"): items, ("movies", "2"): items[:10]},
        )
        cfg = make_config()
        model = LibraryModel(cat, cfg, WatchState(cfg))
        with mock.patch.object(library, "_added", wraps=library._added) as spy:
            model.set_section("movies")
            cat.sectionReady.emit("movies")
            model.genres()
            model.select_genre("1")
            model.available_sorts()
            model.items()
            model.letter_index()
            model.genres("b")
            self.assertLessEqual(spy.call_count, len(items))
            model.select_genre("2")
            model.set_sort(Sort.ADDED)
            model.items()
            self.assertLessEqual(spy.call_count, len(items))
        self.assertEqual(model.items()[0]["id"], "9")


if __name__ == "__main__":
    unittest.main()
