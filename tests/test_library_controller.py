import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from job_fixtures import ImmediateRunner
from timer_fixtures import elapse

from services.library import RECORDINGS, LibraryModel
from services.provider_ids import (
    ALL_PROVIDERS,
    ALL_PROVIDERS_TITLE,
    provider_of,
    with_provider,
)
from services.thread_cleanup import has_pending_threads
from services.watch_state import WatchState
from ui.keymap import Action
from ui.library.library_screen import LIVE_TILE, GridPosters, LibraryScreen
from ui.library.poster_grid import FAVORITE
from ui.library.search_session import SEARCH_PROMPT
from ui.library_controller import (
    RETRY_TEXT,
    SEARCH_DEBOUNCE_MS,
    UNKNOWN_PROVIDER_NOTICE,
    LibraryController,
)
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


class FakeProviderCatalog(QObject):
    """One provider's own catalog, as the hub hands it out."""

    sectionReady = Signal(str)
    sectionFailed = Signal(str, str)

    def __init__(self, name, hub):
        super().__init__()
        self.name = name
        self.hub = hub
        self.loaded = set()  # sections with content

    def has_content(self, section):
        return section in self.loaded

    def request_section(self, section, *, force=False):
        self.hub.section_requests.append((self.name, section))


class FakeCatalogs(QObject):
    """The Catalogs hub: which providers are ready, who was asked to set up."""

    providerReady = Signal(str)
    providerFailed = Signal(str, str)
    providersChanged = Signal()

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.ready = {"P"}  # the provider shown is up
        self.ensured = []
        self.section_requests = []
        self._by_name = {}

    def names(self):
        return list(self.owner.names)

    def is_ready(self, name):
        return name in self.ready

    def ensure(self, name):
        self.ensured.append(name)

    def catalog(self, name):
        if name not in self._by_name:
            self._by_name[name] = FakeProviderCatalog(name, self)
        return self._by_name[name]


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
        self.mixed = False  # All providers
        self.mix = []  # 2.8: the providers mixed, when not every one
        self.mixes = []  # each select_mix asked
        self.headers = set()  # ids of the provider headings among the groups
        self.catalogs = FakeCatalogs(self)
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

    def all_providers(self):
        return self.mixed

    def mixed_names(self):
        return list(self.mix or self.names) if self.mixed else [self.name]

    def select_mix(self, names):  # 2.8: as BrowsedCatalog's: one is that provider alone
        self.mixes.append(list(names))
        chosen = [n for n in self.names if n in names]
        if len(chosen) == 1:
            self.mixed, self.mix = False, []
            self.select_provider(chosen[0])
            return
        everyone = not chosen or len(chosen) == len(self.names)
        self.select_provider(ALL_PROVIDERS)
        self.mixed, self.name, self.mix = True, ALL_PROVIDERS, [] if everyone else chosen

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
            header = gid in self.headers
            out.append(
                SimpleNamespace(section=section, id=gid, title=title, count=count, header=header)
            )
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
        if name in self.names:  # the shown provider changes at once, as BrowsedCatalog's does
            self.name = name


RECORDING = {
    "id": "recording:News 2026-10-19 21.30.ts",
    "name": "News",
    "path": "/rec/News 2026-10-19 21.30.ts",
    "added": "1761000000",
    "_recording": True,
}


class FakeRecordings(QObject):
    changed = Signal()

    def __init__(self, items=()):
        super().__init__()
        self.list, self.version = list(items), 1

    def items(self):
        return list(self.list)


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
        self.menus = []  # (item, section, global position) of each right-click menu
        self.model = LibraryModel(self.catalog, self.config, self.watch)

    def build(self, jobs=None, logos=None, tmdb=None):
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
            context_menu=lambda item, section, pos: self.menus.append((item, section, pos)),
            tmdb=tmdb,
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

    # Recordings (2.8) ---------------------------------------------------------------------
    def with_recordings(self):
        self.recordings = FakeRecordings([RECORDING])
        self.model = LibraryModel(self.catalog, self.config, self.watch, recordings=self.recordings)

    def test_a_recording_plays_from_its_shelf_without_asking_a_provider(self):
        self.with_recordings()
        self.open_movies()
        self.model.select_genre(RECORDINGS)
        self.assertEqual(self.grid_names(), ["News"])
        self.screen.grid.activated.emit(0)
        item, kind, context = self.played[-1]
        self.assertEqual((item["id"], kind), (RECORDING["id"], "recording"))
        self.assertFalse(context["start_over"])
        self.assertEqual(self.catalog.selected, [])

    def test_a_recordings_right_click_is_its_own(self):
        self.with_recordings()
        self.open_movies()
        self.model.select_genre(RECORDINGS)
        self.screen.grid.contextRequested.emit(0, QPoint(3, 4))
        item, section, _pos = self.menus[-1]
        self.assertEqual((item["id"], section), (RECORDING["id"], "recording"))

    def test_a_provider_without_films_gets_movies_for_its_recordings(self):
        self.catalog.section_list = ["live"]
        self.with_recordings()
        self.build().open("movies")
        self.assertIn("movies", self.screen.nav._tabs)
        self.assertEqual(self.grid_names(), ["News"])
        self.assertNotIn(("section", "movies"), self.catalog.requests)

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

    def test_a_genre_of_one_title_says_title(self):
        # The header read "1 titles".
        self.catalog.item_map[("movies", "d")] = [SALT]
        self.catalog.item_map[("movies", "c")] = [movie(100 + i, f"M{i}") for i in range(1200)]
        ctl = self.open_movies()
        self.assertEqual(self.screen.grid._count_text, "1 title")
        ctl.handle(Action.NAV_LEFT)  # genres
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_ACTIVATE)  # Comedy
        self.assertEqual(self.screen.grid._count_text, "1,200 titles")

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

    def test_a_movie_posters_right_click_menu_is_for_a_movie(self):
        # Play in VLC/MPV and Copy stream link, as for channels.
        self.open_movies()
        grid = self.screen.grid
        grid.contextRequested.emit(1, QPoint(30, 40))
        self.assertEqual(self.menus, [(grid.item(1), "movies", QPoint(30, 40))])
        self.assertEqual(self.played, [])

    def test_a_series_has_no_menu_and_its_episodes_have(self):
        ctl = self.build()
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        self.screen.grid.contextRequested.emit(0, QPoint(1, 2))  # the series: it plays nothing
        self.assertEqual(self.menus, [])
        ctl.handle(Action.NAV_ACTIVATE)
        self.catalog.season_map["77"] = SEASONS
        self.catalog.seasonsReady.emit("77")
        self.catalog.episode_map[("77", "1")] = episodes("1")
        self.catalog.episodesReady.emit("77", "1")
        episode = episodes("1")[1]
        self.screen.series.episodeContextRequested.emit(SEASONS[0], episode, QPoint(5, 6))
        self.assertEqual(self.menus, [(episode, "series", QPoint(5, 6))])
        self.assertEqual(self.played, [])

    def test_a_continue_episode_poster_is_for_an_episode(self):
        ep = {**episodes("1")[0], "season": "1"}
        self.config.watch_history.append(
            {"item_data": ep, "item_type": "episode", "provider_name": "P"}
        )
        cid = WatchState.content_id("P", ep, item_type="episode")
        self.config.playback_positions[cid] = {"position": 60_000, "duration": 1_200_000}
        ctl = self.build()
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        grid = self.screen.grid
        grid.contextRequested.emit(grid.current_index(), QPoint(1, 2))
        self.assertEqual(self.menus, [(with_provider(ep, "P"), "series", QPoint(1, 2))])

    def test_only_the_current_providers_films_have_a_menu(self):
        # Links are asked of the provider QiTV is on: another's title, a channel or a title
        # during a provider switch has none.
        ctl = self.build()
        ctl.open_search()
        self.type_text("sal")
        elapse(WAIT_MS)
        self.assertEqual(self.grid_names(), ["Salt TV", "Salt Roads", "Salt Lake"])
        grid = self.screen.grid
        for index in (0, 2):  # P's channel, Q's film
            grid.contextRequested.emit(index, QPoint(1, 2))
        self.assertEqual(self.menus, [])
        grid.contextRequested.emit(1, QPoint(1, 2))
        self.assertEqual([(i["name"], s) for i, s, _pos in self.menus], [("Salt Roads", "movies")])
        self.catalog.loading = True
        grid.contextRequested.emit(1, QPoint(1, 2))
        self.assertEqual(len(self.menus), 1)

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

    def test_words_from_the_channel_lists_find_are_searched_at_once(self):
        ctl = self.build()
        ctl.open_search("sal")  # its films-and-series row
        self.assertEqual(ctl.query, "sal")
        self.assertEqual(len(self.jobs.workers), 1)  # no pause: the words were typed already
        self.assertEqual(self.grid_names(), ["Salt TV", "Salt Roads", "Salt Lake"])
        self.assertEqual(self.screen.search_field.query, "sal")

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
    def make_ready(self, name):
        """Provider `name` finished its setup: the hub says so, and so does the shown catalog
        when it is the one shown."""
        self.catalog.catalogs.ready.add(name)
        self.catalog.catalogs.providerReady.emit(name)
        if name == self.catalog.name:
            self.catalog.providerReady.emit(name)

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

    def played_names(self):
        return [i["name"] for i, _k, _c in self.played]

    def test_a_hit_from_another_provider_plays_once_that_provider_is_ready(self):
        ctl = self.build()
        notices = []
        ctl.notice.connect(notices.append)
        ctl.open_search()
        self.type_text("salt lake")
        elapse(WAIT_MS)
        self.assertEqual(self.grid_names(), ["Salt Lake"])
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.catalog.catalogs.ensured, ["Q"])  # its own setup, beside
        self.assertEqual(self.catalog.selected, [])  # nothing is switched
        self.assertEqual(notices, [])
        self.assertEqual(self.played, [])
        self.make_ready("Q")
        self.assertEqual((self.played[-1][0]["name"], self.played[-1][1]), ("Salt Lake", "movie"))
        self.assertEqual(provider_of(self.played[-1][0]), "Q")
        self.make_ready("Q")
        self.assertEqual(len(self.played), 1)  # nothing plays twice

    def test_a_title_from_another_provider_plays_without_switching(self):
        # Q is ready already: the title runs at once, the Library stays on P.
        ctl = self.search_salt()
        notices = []
        ctl.notice.connect(notices.append)
        self.catalog.catalogs.ready.add("Q")
        self.activate_name(ctl, "Salt Lake")
        self.assertEqual(notices, [])
        self.assertEqual(self.catalog.selected, [])
        self.assertEqual(self.catalog.current_provider_name(), "P")
        self.assertEqual(provider_of(self.played[-1][0]), "Q")

    def test_a_provider_becoming_ready_that_is_not_waited_for_changes_nothing(self):
        ctl = self.build()
        ctl.open_search()
        self.type_text("salt lake")
        elapse(WAIT_MS)
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_ACTIVATE)
        self.make_ready("P")
        self.assertEqual(self.played, [])
        self.make_ready("Q")
        self.assertEqual(self.played_names(), ["Salt Lake"])

    def test_a_failed_provider_drops_the_pending_hit_and_allows_another_try(self):
        ctl = self.build()
        notices = []
        ctl.notice.connect(notices.append)
        ctl.open_search()
        self.type_text("salt lake")
        elapse(WAIT_MS)
        ctl.handle(Action.NAV_DOWN)
        ctl.handle(Action.NAV_ACTIVATE)
        self.catalog.catalogs.providerFailed.emit("Q", "Couldn't reach the provider")
        self.assertEqual(notices, ["Q: Couldn't reach the provider"])
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.catalog.catalogs.ensured, ["Q", "Q"])
        self.make_ready("Q")
        self.assertEqual(len(self.played), 1)

    def test_a_second_enter_while_waiting_plays_once(self):
        ctl = self.search_salt()
        self.activate_name(ctl, "Salt Lake")
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.played, [])
        self.make_ready("Q")
        self.assertEqual(self.played_names(), ["Salt Lake"])

    def test_the_latest_activation_wins_while_waiting(self):
        ctl = self.search_salt()
        self.catalog.catalogs.ready.discard("P")  # P is still setting up too
        self.activate_name(ctl, "Salt Lake")  # Q
        self.activate_name(ctl, "Salt Roads")  # P, the provider the Library shows
        self.assertEqual(self.played, [])
        self.make_ready("Q")  # the superseded title is not played
        self.assertEqual(self.played, [])
        self.make_ready("P")
        self.assertEqual(self.played_names(), ["Salt Roads"])

    def test_leaving_the_library_drops_the_pending_hit(self):
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
        self.make_ready("Q")
        self.assertEqual(self.played, [])

    def test_leave_drops_the_pending_hit(self):
        ctl = self.search_salt()
        self.activate_name(ctl, "Salt Lake")
        ctl.leave()  # the window left the Library (a menu, the drawer)
        self.make_ready("Q")
        self.assertEqual(self.played, [])

    def test_a_ready_provider_play_drops_the_pending_hit(self):
        ctl = self.search_salt()
        self.activate_name(ctl, "Salt Lake")
        self.activate_name(ctl, "Salt Roads")  # P is ready: it plays at once
        self.assertEqual(self.played_names(), ["Salt Roads"])
        self.make_ready("Q")
        self.assertEqual(len(self.played), 1)

    def test_a_title_of_the_provider_still_loading_waits_for_it(self):
        ctl = self.open_movies()
        notices = []
        ctl.notice.connect(notices.append)
        self.catalog.catalogs.ready.discard("P")  # P's setup is still running (startup)
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.catalog.selected, [])
        self.assertEqual(notices, [])
        self.assertEqual(self.played, [])
        self.make_ready("P")
        self.assertEqual(self.played_names(), ["Dune Songs"])

    def test_choosing_the_librarys_source_keeps_a_waiting_title(self):
        ctl = self.search_salt()
        self.activate_name(ctl, "Salt Lake")  # waits for Q
        actions = ctl._source_menu().actions()
        self.addCleanup(actions[0].parent().deleteLater)
        actions[1].trigger()  # Q joins what the Library shows (2.8: the ticks mix)
        self.assertEqual(self.catalog.selected, [ALL_PROVIDERS])
        self.make_ready("Q")
        self.assertEqual(self.played_names(), ["Salt Lake"])  # still played, once

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

    def reload_movies(self):
        """The provider's films come again (a stale list reloaded, a new setup): the same
        titles in new dicts, as a catalog stores them."""
        self.catalog.version += 1
        for key, items in list(self.catalog.item_map.items()):
            if items is not None:
                self.catalog.item_map[key] = [dict(item) for item in items]

    def test_a_film_chosen_while_its_provider_sets_up_again_plays_where_it_was(self):
        # Reported in 2.2.0: a film chosen in a genre far down the list; its provider was not ready
        # and the Library went back to its first genre, at the top. It plays once its provider
        # is ready, once, and the Library stays on the genre and the title.
        ctl = self.open_movies()
        self.model.select_genre("c")
        ctl.handle(Action.NAV_RIGHT)  # Comedy 1
        self.catalog.catalogs.ready.discard("P")  # set up again (a refresh, a new sign-in)
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.played, [])
        self.reload_movies()
        self.make_ready("P")
        self.assertEqual(self.played_names(), ["Comedy 1"])
        self.assertEqual(self.catalog.requests[-1], ("section", "movies"))  # asked again
        self.catalog.sectionReady.emit("movies")
        self.assertEqual(self.model.genre_id, "c")
        self.assertEqual(self.screen.genres.current_id(), "c")
        grid = self.screen.grid
        self.assertEqual(grid.current_index(), 1)
        self.assertEqual(grid.item(1)["name"], "Comedy 1")
        self.make_ready("P")
        self.assertEqual(len(self.played), 1)  # nothing plays twice

    def test_the_films_arriving_again_keep_the_genre_title_and_scroll(self):
        harbour = [movie(100 + i, f"Harbour {i:03}") for i in range(120)]
        self.catalog.group_list["movies"].append(("h", "Harbour"))
        self.catalog.item_map[("movies", "h")] = harbour
        self.open_movies()
        self.model.select_genre("h")
        grid = self.screen.grid
        grid.set_current_index(90)
        value = grid.verticalScrollBar().value()
        self.assertGreater(value, 0)
        self.reload_movies()
        self.catalog.sectionReady.emit("movies")  # a stale list reloaded
        self.assertEqual(self.model.genre_id, "h")
        self.assertEqual(grid.current_index(), 90)
        self.assertEqual(grid.item(90)["name"], "Harbour 090")
        self.assertIsNot(grid.item(90), harbour[90])  # the new list's
        self.assertEqual(grid.verticalScrollBar().value(), value)

    def test_a_double_click_across_the_films_arriving_again_plays_the_film_pressed(self):
        self.open_movies()
        self.model.select_genre("c")
        grid = self.screen.grid
        point = grid.item_rect(1).center()
        QTest.mouseClick(grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)  # current now
        self.reload_movies()
        self.catalog.sectionReady.emit("movies")  # lands between the two clicks
        QTest.mouseDClick(grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)
        self.assertEqual(self.played_names(), ["Comedy 1"])

    def test_the_genre_shown_loading_again_keeps_its_titles_meanwhile(self):
        ctl = self.open_movies()
        self.model.select_genre("c")
        ctl.handle(Action.NAV_RIGHT)  # Comedy 1
        comedy = self.catalog.item_map[("movies", "c")]
        self.catalog.item_map[("movies", "c")] = None  # a refresh cleared its lists
        self.catalog.version += 1
        self.make_ready("P")
        self.assertIsNone(self.screen.grid._message)  # no "Loading…" over the titles shown
        self.assertEqual(self.grid_names(), ["Comedy 0", "Comedy 1", "Comedy 2"])
        self.catalog.item_map[("movies", "c")] = [dict(item) for item in comedy]
        self.catalog.version += 1
        self.catalog.sectionReady.emit("movies")
        grid = self.screen.grid
        self.assertEqual(grid.current_index(), 1)
        self.assertIsNot(grid.item(1), comedy[1])  # the new list's, on the same title
        self.model.select_genre("d")  # another genre: from its first title
        self.assertEqual(grid.current_index(), 0)

    def test_the_provider_shown_ready_again_keeps_the_series_open(self):
        self.catalog.season_map["77"] = SEASONS
        ctl = self.build()
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        ctl.handle(Action.NAV_ACTIVATE)  # the series view
        self.assertEqual(ctl.mode, "series")
        self.make_ready("P")  # a refresh of the provider it belongs to
        self.assertEqual(ctl.mode, "series")
        self.catalog.name = "Q"
        self.make_ready("Q")  # the Library went to another provider: its series goes
        self.assertEqual(ctl.mode, "browse")

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
        self.assertEqual(self.catalog.catalogs.ensured, ["R"])
        self.assertEqual(self.catalog.selected, [])
        self.make_ready("R")
        self.assertIn(("R", "series"), self.catalog.catalogs.section_requests)
        self.assertNotIn(("seasons", "77"), self.catalog.requests)
        self.make_ready("R")  # a repeat keeps the series waiting
        self.catalog.catalogs.catalog("R").sectionReady.emit("series")
        self.assertIn(("seasons", "77"), self.catalog.requests)
        self.assertEqual(ctl.mode, "series")
        ctl.handle(Action.ESCAPE)
        self.assertEqual(ctl.mode, "search")

    def test_a_series_hit_of_a_provider_rebuilt_since_still_opens(self):
        # The Library heard the section of the catalog it first asked (by name); once
        # Settings rebuilt that provider's catalog (an edit while no page showed it), the new
        # one's section came to no one: nothing opened and nothing was said.
        self.test_a_series_hit_from_another_provider_waits_for_its_section()
        ctl, hub = self.ctl, self.catalog.catalogs
        del hub._by_name["R"]  # the hub dropped it; the next use builds a new one
        hub.ready.discard("R")
        hub.providersChanged.emit()
        ctl.handle(Action.NAV_ACTIVATE)  # R's series hit again
        self.make_ready("R")
        rebuilt = hub.catalog("R")
        rebuilt.sectionReady.emit("series")
        self.assertEqual(self.catalog.requests.count(("seasons", "77")), 2)
        self.assertEqual(ctl.mode, "series")

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
        self.make_ready("R")
        remote = self.catalog.catalogs.catalog("R")
        remote.sectionFailed.emit("series", "Couldn't reach the provider")
        self.assertEqual(notices[-1], "Couldn't load that series")
        remote.sectionReady.emit("series")
        self.assertNotIn(("seasons", "77"), self.catalog.requests)
        self.assertEqual(ctl.mode, "search")

    def test_a_provider_that_left_the_list_says_so(self):
        ctl = self.search_salt()
        notices = []
        ctl.notice.connect(notices.append)
        self.activate_name(ctl, "Salt Lake")
        self.catalog.names.remove("Q")  # removed in Settings while the results were up
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(notices[-1], UNKNOWN_PROVIDER_NOTICE)

    def test_a_waiting_title_whose_provider_is_removed_is_dropped(self):
        ctl = self.search_salt()
        notices = []
        ctl.notice.connect(notices.append)
        self.activate_name(ctl, "Salt Lake")  # waits for Q
        self.catalog.catalogs.providersChanged.emit()  # Q is still listed: it keeps waiting
        self.assertEqual(notices, [])
        self.catalog.names.remove("Q")
        self.catalog.catalogs.providersChanged.emit()
        self.assertEqual(notices, [UNKNOWN_PROVIDER_NOTICE])
        self.make_ready("Q")
        self.assertEqual(self.played, [])

    def test_an_unstamped_continue_title_plays_at_the_provider_that_played_it(self):
        # History from earlier builds has no stamp: Continue stamps it, so the link is asked
        # of its own provider, not of the TV page's.
        ep = {**episodes("1")[0], "season": "1"}
        self.config.watch_history.append(
            {"item_data": ep, "item_type": "episode", "provider_name": "P"}
        )
        cid = WatchState.content_id("P", ep, item_type="episode")
        self.config.playback_positions[cid] = {"position": 60_000, "duration": 1_200_000}
        self.config.selected_provider_name = "Q"  # the TV page is elsewhere
        ctl = self.build()
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(provider_of(self.played[-1][0]), "P")

    def test_source_menu_lists_providers_and_all_providers_and_mixes(self):
        ctl = self.open_movies()
        menu = ctl._source_menu()
        self.addCleanup(menu.deleteLater)
        actions = [a for a in menu.actions() if not a.isSeparator()]
        self.assertEqual([a.text() for a in actions], ["P", "Q", "All providers"])
        self.assertEqual([a.isChecked() for a in actions], [True, False, False])
        actions[1].trigger()  # 2.8: Q joins P
        self.assertEqual(self.catalog.mixes, [["P", "Q"]])
        self.catalog.providerReady.emit(ALL_PROVIDERS)
        self.assertEqual(self.catalog.requests[-1], ("section", "movies"))  # re-opened
        self.assertEqual(self.screen.nav._source, "All providers")  # P and Q: every one

    def test_one_provider_has_no_all_providers_entry(self):
        self.catalog.names = ["P"]
        ctl = self.open_movies()
        menu = ctl._source_menu()
        self.addCleanup(menu.deleteLater)
        self.assertEqual([a.text() for a in menu.actions()], ["P"])

    def mix(self):
        """The Library over both providers (All providers), movies of each under its heading."""
        sep = "\x1f"
        ledger = with_provider(movie(1, "Ledger Nights"), "P")
        salt = with_provider(movie(1, "Salt Roads"), "Q")
        cat = self.catalog
        cat.mixed, cat.name = True, ALL_PROVIDERS
        cat.headers = {f"P{sep}", f"Q{sep}"}
        cat.group_list = {
            "movies": [
                ("*", "All"),
                (f"P{sep}", "P"),
                (f"P{sep}d", "Drama"),
                (f"Q{sep}", "Q"),
                (f"Q{sep}c", "Comedy"),
            ]
        }
        cat.item_map = {
            ("movies", "*"): [ledger, salt],
            ("movies", f"P{sep}d"): [ledger],
            ("movies", f"Q{sep}c"): [salt],
        }
        return ledger, salt

    def test_all_providers_genres_sit_under_each_providers_name(self):
        self.mix()
        ctl = self.build()
        ctl.open("movies")
        cat = self.catalog
        cat.sectionReady.emit("movies")
        self.assertEqual(
            self.screen.genres.rows(), ["GENRES · 3", "All", "P", "Drama", "Q", "Comedy"]
        )
        self.assertEqual(self.screen.nav._source, ALL_PROVIDERS_TITLE)
        ctl.handle(Action.NAV_LEFT)  # genres: the headings are not stops
        self.assertEqual(self.screen.genres.current_id(), "*")
        ctl.handle(Action.NAV_DOWN)
        self.assertEqual(self.screen.genres.current_id(), f"P{chr(31)}d")
        ctl.handle(Action.NAV_DOWN)
        self.assertEqual(self.screen.genres.current_id(), f"Q{chr(31)}c")

    def test_all_providers_titles_name_their_provider_and_play_at_their_own(self):
        ledger, salt = self.mix()
        ctl = self.build()
        ctl.open("movies")
        self.catalog.sectionReady.emit("movies")
        self.assertEqual(self.grid_names(), ["Ledger Nights", "Salt Roads"])
        self.assertEqual(self.model.details(salt).provider, "Q")
        self.catalog.catalogs.ready.add("Q")
        self.screen.grid.set_current_index(1)
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.catalog.selected, [])
        self.assertEqual(provider_of(self.played[-1][0]), "Q")
        self.assertEqual(self.catalog.catalogs.ensured, [])  # Q was ready: nothing started

    def test_all_providers_titles_wait_for_their_provider_without_switching(self):
        self.mix()
        ctl = self.build()
        ctl.open("movies")
        self.catalog.sectionReady.emit("movies")
        self.screen.grid.set_current_index(1)  # Salt Roads at Q, not ready
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.catalog.catalogs.ensured, ["Q"])
        self.assertEqual(self.catalog.selected, [])
        self.assertEqual(self.played, [])
        self.make_ready("Q")
        self.assertEqual(self.played_names(), ["Salt Roads"])
        self.assertEqual(self.catalog.current_provider_name(), ALL_PROVIDERS)

    def test_all_providers_titles_have_their_menu_while_another_provider_sets_up(self):
        # In All providers is_loading() is true while any provider sets up, so no title had
        # a menu meanwhile; a title's links wait for its own provider at the hub.
        self.mix()
        ctl = self.build()
        ctl.open("movies")
        self.catalog.sectionReady.emit("movies")
        self.catalog.loading = True
        self.screen.grid.contextRequested.emit(1, QPoint(1, 2))
        self.assertEqual([(i["name"], s) for i, s, _pos in self.menus], [("Salt Roads", "movies")])

    def test_choosing_all_providers_selects_the_mix(self):
        ctl = self.open_movies()
        menu = ctl._source_menu()
        self.addCleanup(menu.deleteLater)
        [a for a in menu.actions() if a.text() == "All providers"][0].trigger()
        self.assertEqual(self.catalog.selected, [ALL_PROVIDERS])

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

    # focus, typing, layout and failures --------------------------------------------------
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


class SourceMixTests(unittest.TestCase):  # 2.8: the source menu's ticks mix providers
    build = LibraryControllerTests.build

    def setUp(self):
        LibraryControllerTests.setUp(self)
        self.catalog.names = ["P", "Q", "R"]

    def menu(self, ctl):
        return {a.text(): a for a in ctl._source_menu().actions() if a.text()}

    def test_the_ticks_say_what_the_library_shows_and_a_tick_adds_a_provider(self):
        ctl = self.build()
        ctl.open("movies")  # P alone
        actions = self.menu(ctl)
        self.assertEqual([n for n in "PQR" if actions[n].isChecked()], ["P"])
        self.assertFalse(actions["All providers"].isChecked())
        actions["Q"].trigger()
        self.assertEqual(self.catalog.mixes, [["P", "Q"]])
        actions = self.menu(ctl)
        self.assertEqual([n for n in "PQR" if actions[n].isChecked()], ["P", "Q"])
        self.assertEqual(ctl._source_title(), "P, Q")

    def test_untick_one_of_a_mix_and_the_others_stay(self):
        ctl = self.build()
        self.catalog.select_mix(["P", "Q"])
        self.menu(ctl)["Q"].trigger()
        self.assertEqual(self.catalog.mixes[-1], ["P"])
        self.assertEqual(self.catalog.current_provider_name(), "P")
        self.menu(ctl)["P"].trigger()  # the last one stays ticked: something is shown
        self.assertEqual(self.catalog.mixes[-1], ["P"])

    def test_all_providers_ticks_every_one(self):
        ctl = self.build()
        actions = self.menu(ctl)
        actions["All providers"].trigger()
        self.assertEqual(self.catalog.mixes[-1], [])
        actions = self.menu(ctl)
        self.assertTrue(all(actions[n].isChecked() for n in "PQR"))
        self.assertTrue(actions["All providers"].isChecked())
        self.assertEqual(ctl._source_title(), "All providers")


class OpenOnProviderTests(unittest.TestCase):  # 2.8: the list's Movies row is its provider's
    setUp = LibraryControllerTests.setUp
    build = LibraryControllerTests.build

    def test_the_library_opens_on_the_provider_asked_for(self):
        ctl = self.build()
        ctl.open("movies")  # on P, its own source
        ctl.open_on("Q", "series")
        self.assertEqual(self.catalog.selected, ["Q"])
        self.assertEqual(ctl.section, "series")
        ctl.open_on("Q", "movies")  # already there: not asked again
        self.assertEqual(self.catalog.selected, ["Q"])
        self.assertEqual(ctl.section, "movies")

    def test_an_unknown_or_no_provider_keeps_the_library_s_own(self):
        ctl = self.build()
        ctl.open_on("", "movies")
        ctl.open_on("Gone", "movies")
        self.assertEqual(self.catalog.selected, [])
        self.assertEqual(ctl.section, "movies")


class FakeTmdb(QObject):
    """Stands in for ui.tmdb_fill.TmdbFill: what it was asked for, and what it "knows" by
    title name ({"fields": {...}, "poster": url})."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self.known = {}
        self.focused = []  # (name, kind)
        self.wanted = []  # [(name, kind)] per call
        self.closed = False

    def fill(self, item, kind):
        known = self.known.get(item.get("name"))
        return {**item, **known.get("fields", {})} if known else item

    def poster_url(self, item, kind):
        known = self.known.get(item.get("name"))
        return known.get("poster", "") if known else ""

    def focus(self, item, kind):
        self.focused.append((item.get("name") if item else None, kind))

    def want_posters(self, titles):
        self.wanted.append([(item.get("name"), kind) for item, kind in titles])

    def shutdown(self):
        self.closed = True


class TmdbInTheLibraryTests(unittest.TestCase):  # 2.8
    setUp = LibraryControllerTests.setUp
    build = LibraryControllerTests.build
    type_text = LibraryControllerTests.type_text
    grid_names = LibraryControllerTests.grid_names

    def open_movies(self):
        self.tmdb = FakeTmdb()
        ctl = self.build(tmdb=self.tmdb)
        ctl.open("movies")
        self.catalog.sectionReady.emit("movies")
        return ctl

    def at(self, name):
        """The grid index of the title called `name` (the grid sorts them)."""
        return self.grid_names().index(name)

    def test_the_title_in_focus_is_asked_of_tmdb(self):
        self.open_movies()
        self.screen.grid.set_current_index(self.at("Tide Lines"))
        self.assertEqual(self.tmdb.focused[-1], ("Tide Lines", "movie"))

    def test_what_tmdb_knows_fills_the_strip(self):
        self.open_movies()
        grid = self.screen.grid
        grid.set_current_index(self.at("Tide Lines"))
        self.assertEqual(grid._details().plot, "")
        self.tmdb.known["Tide Lines"] = {"fields": {"plot": "Waves.", "cast": "Ana, Bo"}}
        self.tmdb.changed.emit()
        self.assertEqual((grid._details().plot, grid._details().cast), ("Waves.", "Ana, Bo"))

    def test_posterless_titles_on_screen_go_to_tmdb_and_take_its_poster(self):
        self.catalog.item_map[("movies", "d")] = [SALT, {"id": "5", "name": "Bare Film"}]
        self.open_movies()
        grid = self.screen.grid
        grid._request_posters()
        self.assertEqual(self.tmdb.wanted[-1], [("Bare Film", "movie")])
        self.tmdb.known["Bare Film"] = {"poster": "https://image.invalid/b.jpg"}
        bare, salt = grid.item(self.at("Bare Film")), grid.item(self.at("Salt Roads"))
        self.assertEqual(grid._poster_url(bare), "https://image.invalid/b.jpg")
        self.assertEqual(grid._poster_url(salt), SALT["logo"])  # the provider's stays

    def test_a_provider_poster_that_failed_gives_way_to_tmdb_s(self):
        self.posters.failed = lambda url: url == SALT["logo"]
        self.open_movies()
        grid = self.screen.grid
        grid._request_posters()
        self.assertIn(("Salt Roads", "movie"), self.tmdb.wanted[-1])
        self.tmdb.known["Salt Roads"] = {"poster": "https://image.invalid/s.jpg"}
        salt = grid.item(self.at("Salt Roads"))
        self.assertEqual(grid._poster_url(salt), "https://image.invalid/s.jpg")

    def test_a_series_opened_is_asked_for_and_its_details_follow(self):
        self.tmdb = FakeTmdb()
        ctl = self.build(tmdb=self.tmdb)
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(ctl.mode, "series")
        self.assertIn(("Harbour Lights", "series"), self.tmdb.focused)
        self.tmdb.known["Harbour Lights"] = {"fields": {"plot": "Lighthouses."}}
        self.tmdb.changed.emit()
        self.assertEqual(self.screen.series._details.plot, "Lighthouses.")

    def test_retrying_a_series_keeps_tmdb_s_poster(self):  # review #5
        self.tmdb = FakeTmdb()
        self.tmdb.known["Harbour Lights"] = {"poster": "https://image.invalid/h.jpg"}
        ctl = self.build(tmdb=self.tmdb)
        ctl.open("series")
        self.catalog.sectionReady.emit("series")
        ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.screen.series._poster_url, "https://image.invalid/h.jpg")
        self.screen.series.retryRequested.emit()
        self.assertEqual(self.screen.series._poster_url, "https://image.invalid/h.jpg")

    def test_search_hits_are_filled_by_their_own_kind_and_channels_never(self):
        self.tmdb = FakeTmdb()
        ctl = self.build(tmdb=self.tmdb)
        ctl.open_search()
        self.type_text("sal")
        elapse(WAIT_MS)
        self.assertEqual(self.grid_names(), ["Salt TV", "Salt Roads", "Salt Lake"])
        grid = self.screen.grid
        self.tmdb.known["Salt Lake"] = {"fields": {"plot": "A lake.", "genre": "Drama"}}
        grid.set_current_index(2)
        self.assertEqual((grid._details().plot, grid._details().genre), ("A lake.", "Drama"))
        self.assertEqual(self.tmdb.focused[-1], ("Salt Lake", "movie"))
        grid.set_current_index(0)
        self.assertEqual(self.tmdb.focused[-1], ("Salt TV", "live"))
        grid._request_posters()
        self.assertNotIn("Salt TV", [name for name, _kind in self.tmdb.wanted[-1]])

    def test_with_the_real_lookups_a_posterless_film_takes_tmdb_s_poster_and_story(self):
        from opensubtitles_fixtures import FakeHttp, FakeResponse
        from test_tmdb import details

        from services.tmdb import API_URL
        from ui.tmdb_fill import TmdbFill

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        http = FakeHttp()
        answer = details(5, "Bare Film", "2019-01-01", overview="Story.", poster="/b.jpg")
        http.answer("GET", f"{API_URL}/movie/5", FakeResponse(200, answer))
        fill = TmdbFill(
            config=SimpleNamespace(tmdb_details=True, tmdb_token="tok"),
            jobs=ImmediateRunner(),
            cache_path=os.path.join(directory.name, "tmdb.json"),
            language="en",
            http=http,
        )
        self.catalog.item_map[("movies", "d")] = [
            SALT,
            {"id": "5", "name": "Bare Film", "tmdb": "5"},
        ]
        ctl = self.build(tmdb=fill)
        ctl.open("movies")
        self.catalog.sectionReady.emit("movies")
        grid = self.screen.grid
        grid._request_posters()  # the view settled: the posterless one is asked for
        bare = grid.item(self.grid_names().index("Bare Film"))
        self.assertEqual(grid._poster_url(bare), "https://image.tmdb.org/t/p/w342/b.jpg")
        grid.set_current_index(self.grid_names().index("Bare Film"))
        self.assertEqual((grid._details().plot, grid._details().year), ("Story.", "2019"))
        self.assertEqual(len(http.calls), 1)  # SALT has its own poster: never asked

    def test_shutdown_stops_tmdb(self):
        ctl = self.open_movies()
        ctl.shutdown()
        self.assertTrue(self.tmdb.closed)


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

    def test_a_poster_that_failed_is_told_a_logo_never(self):  # 2.8: TMDB's poster then
        self.posters.failed = lambda url: url == "http://p/1"
        self.assertTrue(self.shim.failed("http://p/1"))
        self.assertFalse(self.shim.failed("http://p/2"))
        self.assertFalse(self.shim.failed(LIVE_TILE + "http://l/1"))
        self.assertFalse(GridPosters(FakePosters()).failed("http://p/1"))  # it can't tell

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
