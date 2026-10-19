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
from services.provider_ids import ALL_PROVIDERS, provider_of, with_provider
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
        self.names = ["P", "Q"]
        self.mixed = False  # All providers
        self.headers = set()  # ids of the provider headings among the groups
        self.version = 1
        self.section_list = ["live", "movies", "series"]

    def all_providers(self):
        return self.mixed

    def provider_names(self):
        return list(self.names)

    def sections(self):
        return list(self.section_list)

    def current_provider_name(self):
        return self.name

    def content_version(self):
        return self.version

    def groups(self, section):
        return [
            Group(section=section, id=i, title=t, count=c, header=i in self.headers)
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

    def test_a_title_without_a_name_is_kept_by_its_title(self):
        film = {"id": "7", "title": "Untold", "category_id": "1"}
        cat = FakeCatalog({"movies": [("1", "G", 1)]}, {("movies", "1"): [film]})
        model, cfg = build(cat)
        self.assertTrue(model.toggle_favorite(film))
        self.assertEqual(cfg.favorites, ["Untold"])
        self.assertTrue(model.is_favorite(film))

    def test_toggle_survives_save_failure(self):
        cat = FakeCatalog({"movies": [("1", "G", 1)]}, {("movies", "1"): [movie(1)]})
        model, cfg = build(cat)

        def boom():
            raise PermissionError("locked")

        cfg.save_config = boom
        with self.assertLogs("services.favorites", level="WARNING"):
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

    def test_a_title_without_a_genre_never_takes_a_shelfs_or_all_s_name(self):
        # "Continue", "New", "My list" and the provider's "All" are no genres.
        m = movie(1)
        cfg = make_config()
        history(cfg, m, "movie")
        resume(cfg, m, "movie")
        cat = FakeCatalog(
            {"movies": [("*", "All", 1), ("1", "Drama", 1), ("None", "Unknown Category", 1)]},
            {("movies", "*"): [m], ("movies", "1"): [m], ("movies", "None"): [m]},
        )
        model, _ = build(cat, cfg)
        self.assertEqual(model.genre_id, CONTINUE)
        self.assertEqual(model.details(m).genre, "")
        for genre_id in ("*", "None"):
            model.select_genre(genre_id)
            self.assertEqual(model.details(m).genre, "", genre_id)
        model.select_genre("1")
        self.assertEqual(model.details(m).genre, "Drama")  # a real genre still names it
        self.assertEqual(model.details(movie(2, genre="Crime")).genre, "Crime")

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

    def two_genres(self):
        cat = FakeCatalog(
            {"movies": [("1", "A", 1), ("2", "B", 1)]},
            {("movies", "1"): [movie(1)], ("movies", "2"): [movie(2)]},
        )
        model, _ = build(cat)
        model.select_genre("2")
        return cat, model

    def test_the_provider_shown_ready_again_keeps_the_genre(self):
        # Reported in 2.2.0: a film chosen in a genre far down the list; its provider set up again
        # and the Library went back to its first genre, at the top.
        cat, model = self.two_genres()
        cat.requests.clear()
        cat.version += 1
        cat.providerReady.emit("P")  # a refresh, a new sign-in: its lists come again
        self.assertEqual(cat.requests, [("section", "movies")])
        self.assertEqual(model.genre_id, "2")
        self.assertEqual(model.items(), [movie(2)])
        cat.sectionReady.emit("movies")
        self.assertEqual(model.genre_id, "2")

    def test_the_section_arriving_again_keeps_the_genre_while_it_is_listed(self):
        cat, model = self.two_genres()
        cat.version += 1
        cat.sectionReady.emit("movies")  # a stale list reloaded, or asked for by another page
        self.assertEqual(model.genre_id, "2")
        cat.group_list["movies"] = [("1", "A", 1)]  # the provider dropped the genre
        cat.version += 1
        cat.sectionReady.emit("movies")
        self.assertEqual(model.genre_id, "1")

    def test_a_kept_genre_whose_titles_went_with_the_reload_asks_for_them(self):
        cat, model = self.two_genres()
        cat.item_map[("movies", "2")] = None  # a portal's genre, loaded on demand
        cat.requests.clear()
        cat.version += 1
        cat.sectionReady.emit("movies")
        self.assertEqual(model.genre_id, "2")
        self.assertEqual(cat.requests, [("group", "movies", "2")])

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

    def test_a_letter_needs_only_the_start_of_the_title_folded(self):
        import random

        def reference(title):
            first = library.fold(title)[:1]
            return first if "a" <= first <= "z" else "#"

        pieces = ["É", "e\u0301", "\u0301" * 9, "ß", "İ", "ı", "ﬁ", "1", " ", "Ω", "z", "Ž", "x"]
        rng = random.Random(7)
        titles = ["", "\u0301" * 12 + "Abc", "\u0300" * 8 + "z", "ﬁlm"]
        titles += [
            "".join(rng.choice(pieces) for _ in range(rng.randint(1, 14))) for _ in range(3000)
        ]
        for title in titles:
            self.assertEqual(library.letter_of(title), reference(title), repr(title))
        long_title = "Zebra " + "très " * 50
        with mock.patch.object(library, "fold", wraps=library.fold) as spy:
            self.assertEqual(library.letter_of(long_title), "z")
        self.assertEqual(spy.call_args.args[0], long_title[: library._LETTER_PREFIX])

    def test_my_list_without_favorites_scans_nothing(self):
        items = [movie(i) for i in range(30)]
        cat = FakeCatalog({"movies": [("1", "G", 30)]}, {("movies", "1"): items})
        model, cfg = build(cat)
        with mock.patch.object(
            library.favorites, "favorite_key", wraps=library.favorites.favorite_key
        ) as spy:
            self.assertEqual(model._my_list_items(), [])
            self.assertEqual(spy.call_count, 0)
        cfg.favorites.append("Movie 3")
        self.assertEqual(model._my_list_items(), [items[3]])


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
        with self.assertLogs("services.favorites", level="WARNING"):
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


DEMO_TV, HARBOR_IPTV = "Demo TV", "Harbor IPTV"
SEP = "\x1f"  # the separator of provider_ids.mixed_group_id


def mixed_catalog():
    """The Library over both providers: the mixed All, then a heading and the genres of each."""
    ledger = with_provider(movie(1, "Ledger Nights"), DEMO_TV)
    salt = with_provider(movie(1, "Salt Roads"), HARBOR_IPTV)  # the same id at another provider
    tide = with_provider(movie(2, "Tide Lines"), HARBOR_IPTV)
    cat = FakeCatalog(
        {
            "movies": [
                ("*", "All", 3),
                (f"{DEMO_TV}{SEP}", DEMO_TV, None),
                (f"{DEMO_TV}{SEP}d", "Drama", 1),
                (f"{HARBOR_IPTV}{SEP}", HARBOR_IPTV, None),
                (f"{HARBOR_IPTV}{SEP}d", "Drama", 1),
                (f"{HARBOR_IPTV}{SEP}c", "Comedy", 1),
            ]
        },
        {
            ("movies", "*"): [ledger, salt, tide],
            ("movies", f"{DEMO_TV}{SEP}d"): [ledger],
            ("movies", f"{HARBOR_IPTV}{SEP}d"): [salt],
            ("movies", f"{HARBOR_IPTV}{SEP}c"): [tide],
        },
    )
    cat.mixed = True
    cat.name = ALL_PROVIDERS
    cat.names = [DEMO_TV, HARBOR_IPTV]
    cat.headers = {f"{DEMO_TV}{SEP}", f"{HARBOR_IPTV}{SEP}"}
    return cat, ledger, salt, tide


class MixedProvidersTests(unittest.TestCase):
    def test_genres_sit_under_each_providers_heading(self):
        cat, *_ = mixed_catalog()
        model, _ = build(cat)
        self.assertEqual(
            [(g.header, g.title) for g in model.genres()],
            [
                (False, "All"),
                (True, DEMO_TV),
                (False, "Drama"),
                (True, HARBOR_IPTV),
                (False, "Drama"),
                (False, "Comedy"),
            ],
        )
        self.assertEqual(model.genre_id, "*")  # never a heading

    def test_a_filter_keeps_a_heading_only_above_a_match(self):
        cat, *_ = mixed_catalog()
        model, _ = build(cat)
        self.assertEqual(
            [(g.header, g.title) for g in model.genres("com")],
            [(True, HARBOR_IPTV), (False, "Comedy")],
        )
        self.assertEqual(model.genres("zzz"), [])

    def test_a_providers_genre_lists_its_titles(self):
        cat, *_ = mixed_catalog()
        model, _ = build(cat)
        model.select_genre(f"{HARBOR_IPTV}{SEP}d")
        self.assertEqual([i["name"] for i in model.items()], ["Salt Roads"])

    def test_continue_mixes_providers_newest_first(self):
        cat, *_ = mixed_catalog()
        cfg = make_config()
        ledger, salt = movie(1, "Ledger Nights"), movie(
            1, "Salt Roads"
        )  # as the history keeps them
        history(cfg, salt, "movie", provider=HARBOR_IPTV)  # the newest first
        history(cfg, ledger, "movie", provider=DEMO_TV)
        history(cfg, movie(5, "Gone"), "movie", provider="Removed")  # no longer a provider
        resume(cfg, ledger, "movie", pos=600_000, provider=DEMO_TV)
        resume(cfg, salt, "movie", pos=300_000, provider=HARBOR_IPTV)
        resume(cfg, movie(5), "movie", provider="Removed")
        model, _ = build(cat, cfg)
        self.assertEqual(model.genre_id, CONTINUE)
        items = model.items()
        self.assertEqual([provider_of(i) for i in items], [HARBOR_IPTV, DEMO_TV])
        self.assertEqual([i["name"] for i in items], ["Salt Roads", "Ledger Nights"])
        # the same id at two providers keeps two resume points
        self.assertAlmostEqual(model.progress(items[0]), 300_000 / 6_000_000)
        self.assertAlmostEqual(model.progress(items[1]), 600_000 / 6_000_000)

    def test_one_provider_continue_is_stamped_with_its_provider(self):
        cat = FakeCatalog({"movies": [("1", "G", 1)]}, {("movies", "1"): [movie(1)]})
        cfg = make_config()
        history(cfg, movie(1), "movie")
        history(cfg, movie(2), "movie", provider="Q")  # another provider's: not listed
        resume(cfg, movie(1), "movie")
        resume(cfg, movie(2), "movie", provider="Q")
        model, _ = build(cat, cfg)
        self.assertEqual(model.items(), [with_provider(movie(1), "P")])  # stamped with its provider

    def test_my_list_and_new_mix_providers(self):
        cat, ledger, salt, tide = mixed_catalog()
        ledger["added"], salt["added"], tide["added"] = "1700000300", "1700000100", "1700000200"
        cfg = make_config(favorites=["Ledger Nights", "Salt Roads"])
        model, _ = build(cat, cfg)
        self.assertEqual([g.id for g in model.genres()[:2]], [NEW, MY_LIST])
        model.select_genre(NEW)
        self.assertEqual(
            [provider_of(i) for i in model.items()], [DEMO_TV, HARBOR_IPTV, HARBOR_IPTV]
        )
        model.select_genre(MY_LIST)
        self.assertEqual(
            [(provider_of(i), i["name"]) for i in model.items()],
            [(DEMO_TV, "Ledger Nights"), (HARBOR_IPTV, "Salt Roads")],
        )

    def test_a_star_belongs_to_the_titles_provider(self):
        cat, _ledger, salt, _tide = mixed_catalog()
        cfg = make_config(provider_favorites=[{"name": "Salt Roads", "provider": HARBOR_IPTV}])
        model, _ = build(cat, cfg)
        self.assertTrue(model.is_favorite(salt))
        self.assertFalse(model.is_favorite(with_provider(movie(1, "Salt Roads"), DEMO_TV)))

    def test_a_providers_all_or_titles_without_a_category_name_no_genre(self):
        # Under a provider's heading (mixed ids) its "All" and its "Unknown Category"
        # group named the strip's genre; with one provider they name none.
        cat, ledger, *_ = mixed_catalog()
        for own, title in (("*", "All"), ("None", "Unknown Category")):
            gid = f"{DEMO_TV}{SEP}{own}"
            cat.group_list["movies"].append((gid, title, 1))
            cat.item_map[("movies", gid)] = [ledger]
            model, _ = build(cat)
            model.select_genre(gid)
            self.assertEqual(model.details(movie(9)).genre, "")
        model.select_genre(f"{DEMO_TV}{SEP}d")
        self.assertEqual(model.details(movie(9)).genre, "Drama")

    def test_the_strip_names_the_provider_only_while_mixed(self):
        cat, ledger, *_ = mixed_catalog()
        model, _ = build(cat)
        self.assertEqual(model.details(ledger).provider, DEMO_TV)
        cat.mixed = False
        self.assertEqual(model.details(ledger).provider, "")

    def test_another_providers_lists_arriving_keep_the_genre(self):
        cat, *_ = mixed_catalog()
        model, _ = build(cat)
        model.select_genre(f"{HARBOR_IPTV}{SEP}c")
        cat.sectionReady.emit("movies")  # another provider's section landed
        self.assertEqual(model.genre_id, f"{HARBOR_IPTV}{SEP}c")
        cat.requests.clear()
        cat.providerReady.emit(DEMO_TV)  # one provider of the mix is ready
        self.assertEqual(cat.requests, [("section", "movies")])
        self.assertEqual(model.genre_id, f"{HARBOR_IPTV}{SEP}c")
        cat.providerReady.emit(ALL_PROVIDERS)  # the mix ready again: the genre stays
        cat.sectionReady.emit("movies")
        self.assertEqual(model.genre_id, f"{HARBOR_IPTV}{SEP}c")
        cat.mixed, cat.name = False, DEMO_TV  # the source changed: start over
        cat.providerReady.emit(DEMO_TV)
        self.assertIsNone(model.genre_id)
        cat.sectionReady.emit("movies")
        self.assertEqual(model.genre_id, "*")


class AddedFastPathTests(unittest.TestCase):
    def test_a_unix_time_as_text_and_an_absurd_one(self):
        self.assertEqual(library._added({"added": "1700000000"}), 1700000000.0)
        self.assertEqual(library._added({"added": "1700000000000"}), 1700000000.0)
        self.assertIsNone(library._added({"added": "0"}))
        self.assertIsNone(library._added({"added": "9" * 400}))


class AllListsEverythingTests(unittest.TestCase):
    """The loaded titles come from All alone when All holds them (200,000 channels' worth of
    genre lists repeating it cost a third of a second), but not for a portal that gives its
    titles genre by genre."""

    def catalog(self, kind, groups=None, entries=None):
        cat = FakeCatalog(
            {"movies": groups or [("*", "All", 3), ("1", "Drama", 2), ("2", "Comedy", 1)]},
            {
                ("movies", "*"): [movie(1), movie(2), movie(3)],
                ("movies", "1"): [movie(1), movie(2)],
                ("movies", "2"): [movie(9, "Only in a genre")],
            },
        )
        cat.provider_type = lambda: kind
        if entries is not None:
            cat.provider_entry = lambda name: {"type": entries[name]}
        asked = []
        items = cat.items
        cat.items = lambda section, gid: asked.append(gid) or items(section, gid)
        return cat, asked

    def test_xtream_loads_what_all_lists_and_reads_no_genre(self):
        cat, asked = self.catalog("XTREAM")
        model, _cfg = build(cat)
        self.assertEqual([m["id"] for m in model._loaded_items()], ["1", "2", "3"])
        self.assertNotIn("2", asked)

    def test_a_portal_adds_its_genres_to_all(self):
        cat, _asked = self.catalog("STB")
        model, _cfg = build(cat)
        self.assertEqual([m["id"] for m in model._loaded_items()], ["1", "2", "3", "9"])

    def test_all_providers_use_all_unless_one_is_a_portal(self):
        groups = [("*", "All", 3), ("P", "P", None), ("1", "Drama", 2), ("2", "Comedy", 1)]
        for kinds, expected in (
            ({"P": "XTREAM"}, ["1", "2", "3"]),
            ({"P": "STB"}, ["1", "2", "3", "9"]),
        ):
            cat, _asked = self.catalog("ALL", groups, kinds)
            cat.mixed = True
            cat.headers = {"P"}
            model, _cfg = build(cat)
            provider_groups = cat.groups

            def with_providers(section, _groups=provider_groups):
                return [
                    Group(**{**vars(g), "provider": "P" if g.id == "P" else ""})
                    for g in _groups(section)
                ]

            cat.groups = with_providers
            model._memo.clear()
            self.assertEqual([m["id"] for m in model._loaded_items()], expected, kinds)


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
