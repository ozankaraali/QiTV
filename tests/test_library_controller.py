import os
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QRectF, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication
from timer_fixtures import elapse

from services.jobs import ImmediateRunner
from services.library import LibraryModel
from services.thread_cleanup import has_pending_threads
from services.watch_state import WatchState
from ui.keymap import Action
from ui.library.library_screen import LIVE_TILE, GridPosters, LibraryScreen
from ui.library.poster_grid import FAVORITE
from ui.library.search_session import SEARCH_PROMPT
from ui.library_controller import RETRY_TEXT, SEARCH_DEBOUNCE_MS, LibraryController
from ui.theme import PALETTE, qcolor

app = QApplication.instance() or QApplication([])

WAIT_MS = SEARCH_DEBOUNCE_MS + 150


class FakePosters(QObject):
    logoReady = Signal(str)

    def __init__(self):
        super().__init__()
        self.requested = []
        self.pixmaps = {}

    def request(self, urls, replace_pending=False):
        self.requested.append((list(urls), replace_pending))

    def pixmap(self, url):
        return self.pixmaps.get(url)


class FakeCatalog(QObject):
    providerReady = Signal(str)
    providerFailed = Signal(str, str)
    sectionReady = Signal(str)
    sectionFailed = Signal(str, str)
    groupReady = Signal(str, str)
    groupFailed = Signal(str, str, str)
    seasonsReady = Signal(str)
    seasonsFailed = Signal(str, str)
    episodesReady = Signal(str, str)
    episodesFailed = Signal(str, str, str)

    def __init__(self):
        super().__init__()
        self.name = "P"
        self.names = ["P", "Q"]
        self.section_list = ["live", "movies", "series"]
        self.version = 1
        self.group_list = {}  # section -> [(id, title)]
        self.item_map = {}  # (section, gid) -> list | None
        self.season_map = {}  # series id -> list | None
        self.episode_map = {}  # (series id, season id) -> list | None
        self.requests = []
        self.selected = []
        self.loading = False  # a provider setup is running (CatalogService.is_loading)
        self.providerReady.connect(self._settled)
        self.providerFailed.connect(self._settled)

    def _settled(self, *_args):
        self.loading = False

    def is_loading(self):
        return self.loading

    def loading_provider(self):
        if not self.loading:
            return None
        return self.selected[-1] if self.selected else self.name

    def current_provider_name(self):
        return self.name

    def provider_names(self):
        return list(self.names)

    def content_version(self):
        return self.version

    def sections(self):
        return list(self.section_list)

    def groups(self, section):
        out = []
        for gid, title in self.group_list.get(section, []):
            items = self.item_map.get((section, gid))
            count = None if items is None else len(items)
            out.append(SimpleNamespace(section=section, id=gid, title=title, count=count))
        return out

    def items(self, section, gid):
        value = self.item_map.get((section, gid))
        return None if value is None else list(value)

    def request_section(self, section, *, force=False):
        self.requests.append(("section", section))

    def request_group(self, section, gid):
        self.requests.append(("group", section, gid))

    def seasons(self, series):
        value = self.season_map.get(str(series.get("id")))
        return None if value is None else list(value)

    def request_seasons(self, series):
        self.requests.append(("seasons", str(series.get("id"))))

    def episodes(self, series, season):
        value = self.episode_map.get((str(series.get("id")), str(season.get("id"))))
        return None if value is None else list(value)

    def request_episodes(self, series, season):
        self.requests.append(("episodes", str(series.get("id")), str(season.get("id"))))

    def select_provider(self, name=None, *, force_refresh=False):
        self.selected.append(name)
        self.loading = True


class FakeProviderManager:
    def __init__(self, caches):
        self.caches = caches
        self.loads = 0

    def get_all_providers_cached_content(self):
        self.loads += 1
        return list(self.caches)


class RecordingRunner(ImmediateRunner):
    def __init__(self):
        self.workers = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.workers.append(worker)
        return super().run_worker(worker, on_finished, on_error, context)


class DeferredRunner:
    """Holds jobs until the test finishes them, in any order."""

    def __init__(self):
        self.jobs = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.jobs.append((worker, on_finished, on_error, context))
        return worker

    def finish(self, k):
        worker, on_finished, on_error, context = self.jobs[k]
        ImmediateRunner().run_worker(worker, on_finished, on_error, context)

    def pending(self):
        return 0


def make_config(**kw):
    cfg = SimpleNamespace(
        favorites=[], watch_history=[], playback_positions={}, saves=0, selected_provider_name="P"
    )
    cfg.save_config = lambda: setattr(cfg, "saves", cfg.saves + 1)
    for key, value in kw.items():
        setattr(cfg, key, value)
    return cfg


def movie(i, name, **kw):
    return {"id": str(i), "name": name, "logo": f"http://posters.invalid/{i}.jpg", **kw}


SALT = movie(1, "Salt Roads", year="2024", plot="Caravans and coastlines.", genre="Documentary")
TIDE = movie(2, "Tide Lines")
DUNE = movie(3, "Dune Songs")
COMEDY = [movie(10 + i, f"Comedy {i}") for i in range(3)]
SERIES = {"id": "77", "name": "Harbour Lights", "category_id": "5"}
SEASONS = [{"id": "1", "name": "Season 1", "number": "1"}, {"id": "2", "name": "Season 2"}]


def episodes(season_id, n=3):
    return [
        {**SERIES, "number": str(k), "ename": f"Episode {k}", "series": k, "season_id": season_id}
        for k in range(1, n + 1)
    ]


class LibraryControllerTests(unittest.TestCase):
    def setUp(self):
        self.catalog = FakeCatalog()
        self.catalog.group_list = {
            "movies": [("d", "Documentary"), ("c", "Comedy")],
            "series": [("s", "Drama")],
        }
        self.catalog.item_map = {
            ("movies", "d"): [SALT, TIDE, DUNE],
            ("movies", "c"): list(COMEDY),
            ("series", "s"): [SERIES],
        }
        self.config = make_config()
        self.watch = WatchState(self.config)
        self.pm = FakeProviderManager(
            [
                (
                    "P",
                    {
                        "itv": [{"id": "c1", "name": "Salt TV", "logo": "http://logos.invalid/1"}],
                        "vod": [SALT, TIDE, movie(4, "Sand Castle")],
                        "series": [SERIES],
                    },
                ),
                ("Q", {"vod": [movie(9, "Salt Lake")]}),
            ]
        )
        self.posters = FakePosters()
        self.played = []
        self.tuned = []
        self.backs = []
        self.model = LibraryModel(self.catalog, self.config, self.watch)

    def build(self, jobs=None, logos=None):
        self.screen = LibraryScreen(self.posters, logos)
        self.addCleanup(self.screen.deleteLater)
        self.addCleanup(self.screen.close)
        self.screen.resize(1280, 800)
        self.screen.show()
        self.jobs = jobs if jobs is not None else RecordingRunner()
        self.ctl = LibraryController(
            config=self.config,
            catalog=self.catalog,
            model=self.model,
            watch=self.watch,
            screen=self.screen,
            provider_manager=self.pm,
            jobs=self.jobs,
            play=lambda item, kind, context: self.played.append((item, kind, context)),
            tune_live=self.tuned.append,
            back_to_tv=lambda: self.backs.append(True),
        )
        self.addCleanup(self.ctl.shutdown)
        return self.ctl

    def open_movies(self):
        ctl = self.build()
        ctl.open("movies")
        self.catalog.sectionReady.emit("movies")
        return ctl

    def type_text(self, text):
        for ch in text:
            self.ctl.handle(Action.NAV_TEXT, ch)

    def grid_names(self):
        grid = self.screen.grid
        names = []
        k = 0
        while grid.item(k) is not None:
            names.append(grid.item(k)["name"])
            k += 1
        return names

    def tearDown(self):
        self.assertFalse(has_pending_threads())

    # 1 -----------------------------------------------------------------------------------
    def test_open_shows_genres_and_grid_and_moves_between_them(self):
        ctl = self.open_movies()
        self.assertTrue(self.screen.genres.isVisible())
        self.assertTrue(self.screen.grid.isVisible())
        self.assertFalse(self.screen.series.isVisible())
        self.assertIn(("section", "movies"), self.catalog.requests)
        self.assertEqual(self.screen.genres.rows(), ["GENRES · 2", "Documentary", "Comedy"])
        self.assertEqual(self.grid_names(), ["Dune Songs", "Salt Roads", "Tide Lines"])
        self.assertEqual(ctl.zone, "grid")
        ctl.handle(Action.NAV_RIGHT)
        self.assertEqual(self.screen.grid.current_index(), 1)
        ctl.handle(Action.NAV_LEFT)
        ctl.handle(Action.NAV_LEFT)  # column 0: the left edge
        self.assertEqual(ctl.zone, "genres")
        ctl.handle(Action.NAV_RIGHT)
        self.assertEqual(ctl.zone, "grid")

    def test_header_counts_and_sorts(self):
        self.open_movies()
        grid = self.screen.grid
        self.assertEqual(grid._title, "Documentary")
        self.assertEqual(grid._count_text, "3 titles")
        self.assertEqual([value for value, _label in grid._sorts], ["az", "year", "rating"])

    def test_genre_choice_and_sort_reach_the_model(self):
        ctl = self.open_movies()
        ctl.handle(Action.NAV_LEFT)  # genres
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_ACTIVATE)  # Enter: preview now, then the grid
        self.assertEqual(self.model.genre_id, "c")
        self.assertEqual(ctl.zone, "grid")
        self.assertEqual(self.grid_names(), ["Comedy 0", "Comedy 1", "Comedy 2"])
        self.screen.grid.sortRequested.emit("year")
        self.assertEqual(self.model.sort.value, "year")

    def test_nav_up_and_back_down(self):
        ctl = self.open_movies()
        ctl.handle(Action.NAV_UP)  # first row: the top nav
        self.assertEqual(ctl.zone, "nav")
        ctl.handle(Action.NAV_DOWN)
        self.assertEqual(ctl.zone, "grid")

    def test_tabs(self):
        ctl = self.open_movies()
        self.screen.nav.tabRequested.emit("series")
        self.assertEqual(self.model.section, "series")
        self.screen.nav.tabRequested.emit("live")
        self.assertEqual(self.backs, [True])
        self.screen.nav.searchRequested.emit()
        self.assertEqual(ctl.mode, "search")

    def test_m3u_providers_show_live_tv_and_search_only(self):
        self.catalog.section_list = ["live"]
        ctl = self.build()
        with mock.patch.object(self.screen.nav, "set_tabs") as set_tabs:
            ctl.open_search()
        set_tabs.assert_called_with(["live"])

    # 2 -----------------------------------------------------------------------------------
    def test_activating_a_movie_plays_it_and_start_over_restarts(self):
        ctl = self.open_movies()
        ctl.handle(Action.NAV_RIGHT)  # Salt Roads
        ctl.handle(Action.NAV_ACTIVATE)
        item, kind, context = self.played[-1]
        self.assertIs(item, SALT)
        self.assertEqual(kind, "movie")
        self.assertFalse(context["start_over"])
        self.assertEqual([i["name"] for i in context["genre_items"]], self.grid_names())
        self.assertIsNone(context["series"])
        self.screen.grid.startOverRequested.emit(1)
        self.assertTrue(self.played[-1][2]["start_over"])
        self.assertIs(self.played[-1][0], SALT)

    # 3 -----------------------------------------------------------------------------------
    def test_series_loads_seasons_then_episodes_and_plays_an_episode(self):
        ctl = self.build()
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(ctl.mode, "series")
        self.assertTrue(self.screen.series.isVisible())
        self.assertFalse(self.screen.grid.isVisible())
        self.assertIn(("seasons", "77"), self.catalog.requests)
        self.catalog.season_map["77"] = SEASONS
        self.catalog.seasonsReady.emit("77")
        self.assertIn(("episodes", "77", "1"), self.catalog.requests)
        self.catalog.episode_map[("77", "1")] = episodes("1")
        self.catalog.episodesReady.emit("77", "1")
        self.assertEqual(self.screen.series.current_episode()["ename"], "Episode 1")
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_ACTIVATE)
        episode, kind, context = self.played[-1]
        self.assertEqual(kind, "episode")
        self.assertEqual(episode["ename"], "Episode 2")
        self.assertIs(context["series"], SERIES)
        self.assertEqual(context["season"]["id"], "1")
        self.assertEqual(
            [e["ename"] for e in context["episodes"]], ["Episode 1", "Episode 2", "Episode 3"]
        )
        self.assertFalse(context["start_over"])
        self.screen.series.startOverRequested.emit(SEASONS[0], episode)
        self.assertTrue(self.played[-1][2]["start_over"])

    def test_series_opens_the_season_of_the_latest_history_entry(self):
        older = {**episodes("1")[0], "season": "1"}
        latest = {**episodes("2")[1], "season": "2"}
        for ep in (older, latest):  # newest first, as ConfigManager stores it
            self.config.watch_history.insert(
                0, {"item_data": ep, "item_type": "episode", "provider_name": "P"}
            )
        self.catalog.season_map["77"] = SEASONS
        self.catalog.episode_map[("77", "2")] = episodes("2")
        ctl = self.build()
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.screen.series.current_season()["id"], "2")
        self.assertEqual(self.screen.series.current_episode()["season_id"], "2")

    def test_series_back_returns_to_the_grid_with_the_series_current(self):
        ctl = self.build()
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        ctl.handle(Action.NAV_ACTIVATE)
        ctl.handle(Action.ESCAPE)
        self.assertEqual(ctl.mode, "browse")
        self.assertEqual(ctl.zone, "grid")
        self.assertTrue(self.screen.grid.isVisible())
        self.assertIs(self.screen.grid.item(self.screen.grid.current_index()), SERIES)

    def test_continue_episode_plays_without_series_context(self):
        ep = {**episodes("1")[0], "season": "1"}
        self.config.watch_history.append(
            {"item_data": ep, "item_type": "episode", "provider_name": "P"}
        )
        cid = WatchState.content_id("P", ep, item_type="episode")
        self.config.playback_positions[cid] = {"position": 60_000, "duration": 1_200_000}
        ctl = self.build()
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        ctl.handle(Action.NAV_ACTIVATE)
        episode, kind, context = self.played[-1]
        self.assertEqual((episode["ename"], kind), ("Episode 1", "episode"))
        self.assertIsNone(context["series"])
        self.assertIsNone(context["episodes"])

    # 4 -----------------------------------------------------------------------------------
    def test_escape_from_genres_goes_back_to_tv_once_the_filter_is_empty(self):
        ctl = self.open_movies()
        ctl.handle(Action.NAV_LEFT)
        self.type_text("co")
        self.assertEqual(self.screen.genres.rows(), ["GENRES · 1", "Comedy"])
        ctl.handle(Action.ESCAPE)  # clears the filter
        self.assertEqual(self.backs, [])
        self.assertEqual(self.screen.genres.filter_text, "")
        ctl.handle(Action.ESCAPE)
        self.assertEqual(self.backs, [True])

    def test_escape_from_nav_goes_back_to_tv(self):
        ctl = self.open_movies()
        ctl.handle(Action.NAV_UP)
        ctl.handle(Action.ESCAPE)
        self.assertEqual(self.backs, [True])

    # 5 -----------------------------------------------------------------------------------
    def test_search_debounces_and_folds_titles_once(self):
        ctl = self.build()
        ctl.open_search()
        self.assertEqual(ctl.zone, "search")
        self.assertFalse(self.screen.genres.isVisible())
        self.type_text("sal")
        self.assertEqual(self.jobs.workers, [])
        elapse(WAIT_MS)
        self.assertEqual(len(self.jobs.workers), 1)
        self.assertEqual(self.grid_names(), ["Salt TV", "Salt Roads", "Salt Lake"])
        self.assertEqual(
            [title for title, _start, _count in self.screen.grid._sections],
            ["P · Live TV (1)", "P · Movies (1)", "Q · Movies (1)"],
        )
        ctl.handle(Action.NAV_BACKSPACE)
        self.type_text("l")
        elapse(WAIT_MS)
        self.assertEqual(len(self.jobs.workers), 2)
        self.assertEqual(self.pm.loads, 1)  # the second search reused the caches and index

    def test_a_stale_search_result_is_ignored(self):
        runner = DeferredRunner()
        ctl = self.build(jobs=runner)
        ctl.open_search()
        self.type_text("sand")
        elapse(WAIT_MS)
        ctl.handle(Action.ESCAPE)  # cleared while the search runs
        runner.finish(0)  # its result arrives late
        self.assertEqual(self.grid_names(), [])
        self.assertEqual(self.screen.grid._message, SEARCH_PROMPT)

    def test_queries_typed_during_a_slow_first_search_run_once_afterwards(self):
        runner = DeferredRunner()
        ctl = self.build(jobs=runner)
        ctl.open_search()
        for text in ("sa", "l", "t", " r"):
            self.type_text(text)
            elapse(WAIT_MS)
        self.assertEqual(len(runner.jobs), 1)
        runner.finish(0)
        self.assertEqual(len(runner.jobs), 2)
        runner.finish(1)
        self.assertEqual(self.grid_names(), ["Salt Roads"])
        self.assertEqual(self.pm.loads, 1)

    def test_one_letter_shows_the_prompt(self):
        ctl = self.build()
        ctl.open_search()
        self.type_text("s")
        elapse(WAIT_MS)
        self.assertEqual(self.jobs.workers, [])
        self.assertEqual(self.screen.grid._message, SEARCH_PROMPT)
        self.assertEqual(self.screen.search_field.count_text, "")

    def test_provider_ready_searches_the_visible_query_again(self):
        ctl = self.build()
        ctl.open_search()
        self.type_text("salt")
        elapse(WAIT_MS)
        self.catalog.providerReady.emit("P")
        self.assertEqual(len(self.jobs.workers), 2)
        self.assertEqual(self.pm.loads, 2)

    def test_search_snapshot_is_dropped_on_provider_ready_and_refresh(self):
        ctl = self.build()
        ctl.open_search()
        self.type_text("salt")
        elapse(WAIT_MS)
        self.catalog.providerReady.emit("P")
        self.type_text(" r")
        elapse(WAIT_MS)
        self.assertEqual(self.pm.loads, 2)
        ctl.refresh()
        elapse(WAIT_MS)
        self.assertEqual(self.pm.loads, 3)
        self.assertEqual(self.grid_names(), ["Salt Roads"])

    def test_escape_clears_the_query_then_leaves_search(self):
        ctl = self.open_movies()
        ctl.handle(Action.SEARCH)
        self.type_text("salt")
        ctl.handle(Action.ESCAPE)
        self.assertEqual(ctl.query, "")
        self.assertEqual(ctl.mode, "search")
        ctl.handle(Action.ESCAPE)
        self.assertEqual(ctl.mode, "browse")
        self.assertEqual(self.grid_names(), ["Dune Songs", "Salt Roads", "Tide Lines"])

    def test_live_and_movie_hits_of_the_current_provider(self):
        ctl = self.build()
        ctl.open_search()
        self.type_text("salt")
        elapse(WAIT_MS)
        ctl.handle(Action.NAV_DOWN)  # the results
        self.assertEqual(ctl.zone, "grid")
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.tuned[-1]["name"], "Salt TV")
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual((self.played[-1][0]["name"], self.played[-1][1]), ("Salt Roads", "movie"))

    def test_live_hits_use_the_logo_tile(self):
        ctl = self.build(logos=FakePosters())
        ctl.open_search()
        self.type_text("salt")
        elapse(WAIT_MS)
        grid = self.screen.grid
        self.assertEqual(grid._poster_url(grid.item(0)), LIVE_TILE + "http://logos.invalid/1")
        self.assertEqual(grid._poster_url(grid.item(1)), "http://posters.invalid/1.jpg")
        self.assertTrue(grid._tile_provider(grid.item(0)))
        self.assertFalse(grid._tile_provider(grid.item(1)))
        ctl.handle(Action.ESCAPE)  # leaves search for the TV (no section open)
        ctl.open("movies")
        self.assertIsNone(grid._tile_provider)

    def test_one_cache_names_logos_by_their_own_url(self):
        ctl = self.build()
        ctl.open_search()
        self.type_text("salt")
        elapse(WAIT_MS)
        grid = self.screen.grid
        self.assertEqual(grid._poster_url(grid.item(0)), "http://logos.invalid/1")

    # 6 -----------------------------------------------------------------------------------
    def test_a_hit_from_another_provider_plays_once_that_provider_is_ready(self):
        ctl = self.build()
        ctl.open_search()
        self.type_text("salt lake")
        elapse(WAIT_MS)
        self.assertEqual(self.grid_names(), ["Salt Lake"])
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.catalog.selected, ["Q"])
        self.assertEqual(self.played, [])
        self.catalog.name = "Q"
        self.catalog.providerReady.emit("Q")
        self.assertEqual((self.played[-1][0]["name"], self.played[-1][1]), ("Salt Lake", "movie"))
        self.catalog.providerReady.emit("Q")
        self.assertEqual(len(self.played), 1)  # nothing plays twice

    def test_a_different_provider_becoming_ready_drops_the_pending_hit(self):
        ctl = self.build()
        ctl.open_search()
        self.type_text("salt lake")
        elapse(WAIT_MS)
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_ACTIVATE)
        self.catalog.providerReady.emit("P")
        self.catalog.name = "Q"
        self.catalog.providerReady.emit("Q")
        self.assertEqual(self.played, [])

    def test_a_failed_switch_drops_the_pending_hit_and_allows_another_try(self):
        ctl = self.build()
        notices = []
        ctl.notice.connect(notices.append)
        ctl.open_search()
        self.type_text("salt lake")
        elapse(WAIT_MS)
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_ACTIVATE)
        self.catalog.providerFailed.emit("Q", "Couldn't reach the provider")
        self.assertEqual(notices, ["Switching to Q…", "Q: Couldn't reach the provider"])
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.catalog.selected, ["Q", "Q"])
        self.catalog.name = "Q"
        self.catalog.providerReady.emit("Q")
        self.assertEqual(len(self.played), 1)

    def search_salt(self):
        ctl = self.build()
        ctl.open_search()
        self.type_text("salt")
        elapse(WAIT_MS)
        ctl.handle(Action.NAV_DOWN)  # the results
        return ctl

    def activate_name(self, ctl, name):
        self.screen.grid.set_current_index(self.grid_names().index(name))
        ctl.handle(Action.NAV_ACTIVATE)

    def test_a_second_enter_during_the_switch_plays_once(self):  # review (a)
        ctl = self.search_salt()
        self.activate_name(ctl, "Salt Lake")
        self.catalog.name = "Q"  # the provider manager names Q before it is ready
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.played, [])
        self.assertEqual(self.catalog.selected, ["Q"])
        self.catalog.providerReady.emit("Q")
        self.assertEqual([i["name"] for i, _k, _c in self.played], ["Salt Lake"])

    def test_the_latest_activation_wins_during_a_switch(self):  # review (b)
        ctl = self.search_salt()
        notices = []
        ctl.notice.connect(notices.append)
        self.activate_name(ctl, "Salt Lake")  # Q
        self.activate_name(ctl, "Salt Roads")  # back to P, the provider still named current
        self.assertEqual(self.played, [])
        self.assertEqual(self.catalog.selected, ["Q", "P"])
        self.assertEqual(notices, ["Switching to Q…", "Switching to P…"])
        self.catalog.providerReady.emit("P")  # the superseded Q setup never reports
        self.assertEqual([i["name"] for i, _k, _c in self.played], ["Salt Roads"])

    def test_leaving_the_library_drops_the_pending_hit(self):  # review (c)
        ctl = self.build()
        ctl.open_search()
        self.type_text("salt lake")
        elapse(WAIT_MS)
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_ACTIVATE)
        ctl.handle(Action.ESCAPE)  # grid -> search
        ctl.handle(Action.ESCAPE)  # clears the query
        ctl.handle(Action.ESCAPE)  # leaves search: no section, so back to TV
        self.assertEqual(self.backs, [True])
        self.catalog.name = "Q"
        self.catalog.providerReady.emit("Q")
        self.assertEqual(self.played, [])

    def test_leave_drops_the_pending_hit(self):
        ctl = self.search_salt()
        self.activate_name(ctl, "Salt Lake")
        ctl.leave()  # the window left the Library (a menu, the drawer)
        self.catalog.name = "Q"
        self.catalog.providerReady.emit("Q")
        self.assertEqual(self.played, [])

    def test_a_current_provider_play_drops_the_pending_hit(self):
        ctl = self.search_salt()
        self.activate_name(ctl, "Salt Lake")
        self.catalog.loading = False  # Q's setup failed silently elsewhere; P stays current
        self.catalog.providerFailed.emit("Q", "Couldn't reach the provider")
        self.activate_name(ctl, "Salt Roads")
        self.assertEqual([i["name"] for i, _k, _c in self.played], ["Salt Roads"])
        self.catalog.name = "Q"
        self.catalog.providerReady.emit("Q")
        self.assertEqual(len(self.played), 1)

    def test_a_title_of_the_provider_loading_at_startup_waits_without_a_second_setup(self):
        ctl = self.open_movies()
        notices = []
        ctl.notice.connect(notices.append)
        self.catalog.loading = True  # P's setup is still running (startup, Settings)
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.catalog.selected, [])
        self.assertEqual(notices, [])
        self.assertEqual(self.played, [])
        self.catalog.providerReady.emit("P")
        self.assertEqual([i["name"] for i, _k, _c in self.played], ["Dune Songs"])

    def test_the_source_menu_can_pick_the_old_provider_during_a_switch(self):
        ctl = self.search_salt()
        self.activate_name(ctl, "Salt Lake")  # switching to Q; P is still named current
        actions = ctl._source_menu().actions()
        self.addCleanup(actions[0].parent().deleteLater)
        actions[0].trigger()  # P after all
        self.assertEqual(self.catalog.selected, ["Q", "P"])
        actions[0].trigger()  # already the target: no second setup
        self.assertEqual(self.catalog.selected, ["Q", "P"])
        self.catalog.providerReady.emit("P")
        self.assertEqual(self.played, [])  # the Q hit was dropped

    def history_series_view(self, position):
        latest = {**episodes("2")[1], "season": "2"}  # S2 E2
        self.config.watch_history.insert(
            0, {"item_data": latest, "item_type": "episode", "provider_name": "P"}
        )
        cid = WatchState.content_id("P", latest, item_type="episode")
        self.config.playback_positions[cid] = {"position": position, "duration": 1_000_000}
        self.catalog.season_map["77"] = SEASONS
        ctl = self.build()
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        self.model.select_genre("s")  # past Continue, which lists a resumable episode
        ctl.handle(Action.NAV_ACTIVATE)
        self.catalog.episode_map[("77", "2")] = episodes("2")
        self.catalog.episodesReady.emit("77", "2")
        return ctl

    def test_series_opens_at_the_history_episode(self):
        self.history_series_view(position=400_000)
        self.assertEqual(self.screen.series.current_season()["id"], "2")
        self.assertEqual(self.screen.series.current_episode()["ename"], "Episode 2")

    def test_series_opens_after_a_finished_history_episode(self):
        self.history_series_view(position=950_000)
        self.assertEqual(self.screen.series.current_episode()["ename"], "Episode 3")

    def test_leaving_search_returns_to_the_series_that_was_open(self):
        self.catalog.item_map[("series", "s")] = [SERIES, {"id": "78", "name": "Amber Coast"}]
        ctl = self.build()
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        ctl.handle(Action.NAV_RIGHT)  # Harbour Lights, after Amber Coast
        ctl.handle(Action.NAV_ACTIVATE)  # the series view
        ctl.handle(Action.SEARCH)
        ctl.handle(Action.ESCAPE)
        self.assertEqual(ctl.mode, "browse")
        self.assertIs(self.screen.grid.item(self.screen.grid.current_index()), SERIES)

    def test_refresh_keeps_the_genre_and_title(self):
        ctl = self.open_movies()
        self.model.select_genre("c")
        ctl.handle(Action.NAV_RIGHT)
        ctl.refresh()
        self.assertEqual(self.model.genre_id, "c")
        self.assertEqual(self.screen.grid.current_index(), 1)

    def test_a_series_hit_from_another_provider_waits_for_its_section(self):
        self.pm.caches.append(("R", {"series": [dict(SERIES)]}))
        self.catalog.names.append("R")
        ctl = self.build()
        ctl.open_search()
        self.type_text("harbour")
        elapse(WAIT_MS)
        self.assertEqual(self.grid_names(), ["Harbour Lights", "Harbour Lights"])
        ctl.handle(Action.NAV_DOWN)  # the results
        ctl.handle(Action.NAV_DOWN)  # R's copy, in the next section
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.catalog.selected, ["R"])
        self.catalog.name = "R"
        self.catalog.providerReady.emit("R")
        self.assertIn(("section", "series"), self.catalog.requests)
        self.assertNotIn(("seasons", "77"), self.catalog.requests)
        self.catalog.providerReady.emit("R")  # a repeat keeps the series waiting
        self.catalog.sectionReady.emit("series")
        self.assertIn(("seasons", "77"), self.catalog.requests)
        self.assertEqual(ctl.mode, "series")
        ctl.handle(Action.ESCAPE)
        self.assertEqual(ctl.mode, "search")

    def test_a_pending_series_whose_section_fails_is_dropped(self):
        self.pm.caches.append(("R", {"series": [dict(SERIES)]}))
        self.catalog.names.append("R")
        ctl = self.build()
        notices = []
        ctl.notice.connect(notices.append)
        ctl.open_search()
        self.type_text("harbour")
        elapse(WAIT_MS)
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_ACTIVATE)
        self.catalog.name = "R"
        self.catalog.providerReady.emit("R")
        self.catalog.sectionFailed.emit("series", "Couldn't reach the provider")
        self.assertEqual(notices[-1], "Couldn't load that series")
        self.catalog.sectionReady.emit("series")
        self.assertNotIn(("seasons", "77"), self.catalog.requests)
        self.assertEqual(ctl.mode, "search")

    def test_source_menu_lists_providers_and_switches(self):
        ctl = self.open_movies()
        menu = ctl._source_menu()
        self.addCleanup(menu.deleteLater)
        actions = menu.actions()
        self.assertEqual([a.text() for a in actions], ["P", "Q"])
        self.assertEqual([a.isChecked() for a in actions], [True, False])
        actions[1].trigger()
        self.assertEqual(self.catalog.selected, ["Q"])
        self.catalog.name = "Q"
        self.catalog.providerReady.emit("Q")
        self.assertEqual(self.catalog.requests[-1], ("section", "movies"))  # re-opened
        self.assertEqual(self.screen.nav._source, "Q")

    # 7 -----------------------------------------------------------------------------------
    def test_ctrl_d_toggles_the_favorite_and_the_strip_star_turns_accent(self):
        ctl = self.open_movies()
        ctl.handle(Action.NAV_RIGHT)  # Salt Roads
        grid = self.screen.grid

        def star_color():
            buttons = grid._strip_buttons(QRectF(grid.strip_rect()))
            rect = next(r for name, _l, _s, r in buttons if name == FAVORITE)
            image = grid.viewport().grab().toImage()
            dpr = image.devicePixelRatio()
            point = rect.center()
            return QColor(image.pixel(round(point.x() * dpr), round(point.y() * dpr)))

        def near(a, b):
            return all(abs(x - y) <= 24 for x, y in zip(a.getRgb()[:3], b.getRgb()[:3]))

        self.assertFalse(near(star_color(), qcolor(PALETTE.accent)))
        ctl.handle(Action.FAVORITE)
        self.assertEqual(self.config.favorites, ["Salt Roads"])
        self.assertIs(grid.item(grid.current_index()), SALT)
        self.assertTrue(near(star_color(), qcolor(PALETTE.accent)))
        self.screen.grid.favoriteToggled.emit(grid.current_index())
        self.assertEqual(self.config.favorites, [])

    # 8 -----------------------------------------------------------------------------------
    def test_failure_shows_retry_and_enter_requests_the_genre_again(self):
        self.catalog.item_map[("movies", "c")] = None
        ctl = self.open_movies()
        self.model.select_genre("c")
        self.assertEqual(self.catalog.requests.count(("group", "movies", "c")), 1)
        self.model.failed.emit("Couldn't reach the provider")
        self.assertEqual(self.screen.grid._message, RETRY_TEXT)
        self.model.changed.emit()  # an unrelated update keeps the message
        self.assertEqual(self.screen.grid._message, RETRY_TEXT)
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.catalog.requests.count(("group", "movies", "c")), 2)
        self.assertEqual(self.screen.grid._message, "Loading…")

    def test_a_loading_provider_keeps_loading_and_a_shown_genre_keeps_its_titles(self):
        self.catalog.item_map[("movies", "c")] = None
        ctl = self.open_movies()
        self.model.select_genre("c")
        self.model.failed.emit("The provider is still loading")
        self.assertEqual(self.screen.grid._message, "Loading…")
        self.model.select_genre("d")
        self.model.failed.emit("Couldn't reach the provider")  # not about the titles shown
        self.assertIsNone(self.screen.grid._message)
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.played[-1][0]["name"], "Dune Songs")

    # review minors -----------------------------------------------------------------------
    def test_up_from_the_first_genre_reaches_nav_and_down_returns(self):
        ctl = self.open_movies()
        ctl.handle(Action.NAV_LEFT)
        ctl.handle(Action.NAV_UP)  # the first genre
        self.assertEqual(ctl.zone, "nav")
        ctl.handle(Action.NAV_DOWN)
        self.assertEqual(ctl.zone, "genres")

    def test_slash_types_where_text_goes_and_searches_elsewhere(self):
        ctl = self.open_movies()
        ctl.handle(Action.NAV_LEFT)
        ctl.handle(Action.SEARCH, "/")
        self.assertEqual(self.screen.genres.filter_text, "/")
        self.assertEqual(ctl.mode, "browse")
        ctl.handle(Action.NAV_RIGHT)
        ctl.handle(Action.SEARCH, "/")  # from the grid: search
        self.assertEqual(ctl.mode, "search")
        self.type_text("ac")
        ctl.handle(Action.SEARCH, "/")
        self.assertEqual(ctl.query, "ac/")
        ctl.handle(Action.SEARCH)  # Ctrl+F has no text
        self.assertEqual(ctl.query, "ac/")

    def accent_pixels_around_current(self):
        grid = self.screen.grid
        rect = grid.item_rect(grid.current_index()).adjusted(-6, -6, 6, 6)
        image = grid.viewport().grab().toImage()
        accent = qcolor(PALETTE.accent)
        count = 0
        for x in range(max(0, rect.left()), min(image.width(), rect.right())):
            for y in (rect.top() + 4, rect.center().y(), rect.bottom() - 4):
                c = QColor(image.pixel(x, y))
                if all(abs(a - b) <= 30 for a, b in zip(c.getRgb()[:3], accent.getRgb()[:3])):
                    count += 1
        return count

    def test_the_grid_ring_follows_the_focused_zone(self):
        ctl = self.open_movies()
        self.assertGreater(self.accent_pixels_around_current(), 0)
        ctl.handle(Action.NAV_LEFT)  # the genres
        self.assertEqual(self.accent_pixels_around_current(), 0)
        ctl.handle(Action.NAV_RIGHT)
        self.assertGreater(self.accent_pixels_around_current(), 0)

    def test_series_mode_hides_the_genres_and_takes_the_width(self):
        ctl = self.build()
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertFalse(self.screen.genres.isVisible())
        self.assertEqual(self.screen.series.width(), self.screen.width())
        self.assertEqual(self.screen.series.x(), 0)
        ctl.handle(Action.ESCAPE)
        self.assertTrue(self.screen.genres.isVisible())
        self.assertEqual(self.screen.grid.x(), self.screen.genres.width())

    def test_failed_seasons_and_episodes_retry(self):
        ctl = self.build()
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        ctl.handle(Action.NAV_ACTIVATE)
        self.catalog.seasonsFailed.emit("77", "Couldn't reach the provider")
        ctl.handle(Action.NAV_ACTIVATE)  # retry
        self.assertEqual(self.catalog.requests.count(("seasons", "77")), 2)
        self.catalog.season_map["77"] = SEASONS
        self.catalog.seasonsReady.emit("77")
        self.catalog.episodesFailed.emit("77", "1", "Couldn't reach the provider")
        ctl.handle(Action.NAV_ACTIVATE)  # retry
        self.assertEqual(self.catalog.requests.count(("episodes", "77", "1")), 2)
        self.catalog.episode_map[("77", "1")] = episodes("1")
        self.catalog.episodesReady.emit("77", "1")
        self.assertEqual(self.screen.series.current_episode()["ename"], "Episode 1")

    def test_a_section_the_provider_lacks_says_so(self):
        self.catalog.section_list = ["live"]
        ctl = self.build()
        ctl.open("movies")
        self.assertNotIn(("section", "movies"), self.catalog.requests)
        self.assertEqual(self.screen.grid._message, "P has no movies")
        self.assertEqual(self.screen.genres.rows(), [])


class GridPostersTests(unittest.TestCase):
    def setUp(self):
        self.posters = FakePosters()
        self.logos = FakePosters()
        self.shim = GridPosters(self.posters, self.logos)

    def test_requests_go_to_their_cache(self):
        self.assertEqual(self.shim.logo_key("http://l/1"), LIVE_TILE + "http://l/1")
        self.shim.request(["http://p/1", LIVE_TILE + "http://l/1"], replace_pending=True)
        self.assertEqual(self.posters.requested, [(["http://p/1"], True)])
        self.assertEqual(self.logos.requested, [(["http://l/1"], True)])

    def test_one_cache_needs_no_prefix_and_emits_once(self):
        shim = GridPosters(self.posters, self.posters)
        self.assertEqual(shim.logo_key("http://l/1"), "http://l/1")
        shim.request(["http://p/1", "http://l/1"], replace_pending=True)
        self.assertEqual(self.posters.requested, [(["http://p/1", "http://l/1"], True)])
        ready = []
        shim.logoReady.connect(ready.append)
        self.posters.logoReady.emit("http://l/1")
        self.assertEqual(ready, ["http://l/1"])

    def test_logos_are_the_cache_pixmaps_themselves(self):
        self.assertIsNone(self.shim.pixmap(LIVE_TILE + "http://l/1"))
        logo = QPixmap(100, 50)
        self.logos.pixmaps["http://l/1"] = logo
        ready = []
        self.shim.logoReady.connect(ready.append)
        self.logos.logoReady.emit("http://l/1")
        self.assertEqual(ready, [LIVE_TILE + "http://l/1"])
        self.assertIs(self.shim.pixmap(LIVE_TILE + "http://l/1"), logo)

    def test_posters_pass_through(self):
        pixmap = QPixmap(10, 15)
        self.posters.pixmaps["http://p/1"] = pixmap
        self.assertIs(self.shim.pixmap("http://p/1"), pixmap)
        ready = []
        self.shim.logoReady.connect(ready.append)
        self.posters.logoReady.emit("http://p/1")
        self.assertEqual(ready, ["http://p/1"])


class LogoTilePaintTests(unittest.TestCase):
    def test_a_channel_logo_is_centred_on_a_raised_tile(self):
        posters = FakePosters()
        host = LibraryScreen(posters)
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        host.resize(1280, 800)
        host.show()
        logo = QPixmap(100, 50)
        logo.fill(QColor("#ffffff"))
        posters.pixmaps["http://l/1"] = logo
        grid = host.grid
        grid.set_poster_url_provider(lambda item: "http://l/1")
        grid.set_tile_provider(lambda item: True)
        grid.set_sections([("", [{"name": "Salt TV"}, {"name": "Tide TV"}])])
        grid.set_current_index(0)
        rect = grid.item_rect(1)
        image = grid.viewport().grab().toImage()
        centre = QColor(image.pixel(rect.center().x(), rect.center().y()))
        edge = QColor(image.pixel(rect.left() + 6, rect.top() + 12))
        self.assertEqual(centre.name(), "#ffffff")
        self.assertEqual(edge.name(), qcolor(PALETTE.panel_raised).name())


if __name__ == "__main__":
    unittest.main()
