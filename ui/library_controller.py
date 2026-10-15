"""The Library's behaviour: wires the LibraryModel, the catalog and search to the LibraryScreen.

The screen's widgets never take keyboard focus. TVWindow resolves keys with the "library"
context and hands them to `handle`, which routes them to the focused zone: the top nav, the
genre column, the grid, the series view or the search field.

Search runs in a SearchWorker through the JobRunner. Every search carries a generation, so
only the newest search's hits are shown. The cached catalogs and their folded index are kept
between keystrokes and dropped when a provider becomes ready or on `refresh()`.

A search hit from another provider selects that provider first and plays once it is ready.
Only one such action waits at a time; a newer one replaces it, and it is dropped when a
different provider becomes ready.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import itertools
import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, QPoint, QTimer, Signal
from PySide6.QtWidgets import QMenu

from services.library import PINNED_TITLES, SORT_LABELS, Details, LibraryModel, Sort
from services.search import Hit, SearchWorker
from services.watch_state import RESUME_MAX_RATIO, WatchState
from ui.keymap import Action
from ui.library.library_screen import LIVE_TILE, LibraryScreen
from ui.library.poster_grid import poster_url

logger = logging.getLogger(__name__)

SEARCH_DEBOUNCE_MS = 250
LOADING_TEXT = "Loading…"
EMPTY_TEXT = "Nothing here"
RETRY_TEXT = "Couldn't load — press Enter to retry"
SEARCH_PROMPT = "Type to search every provider's movies, series and channels"
SEARCHING_TEXT = "Searching…"
SEARCH_FAILED_TEXT = "Couldn't search — type to try again"
SECTION_LABELS = {"live": "Live TV", "movies": "Movies", "series": "Series"}
ZONES = ("nav", "genres", "grid", "series", "search")


@dataclass(frozen=True)
class _Pending:
    """A search hit waiting for its provider to become ready."""

    provider: str
    section: str
    item: dict
    start_over: bool = False


def _context(**values) -> dict:
    """The play context handed to the player (resume and autoplay)."""
    context: dict = {
        "start_over": False,
        "series": None,
        "season": None,
        "episodes": None,
        "genre_items": None,
    }
    context.update(values)
    return context


class LibraryController(QObject):
    notice = Signal(str)  # a message for the window's toast

    def __init__(
        self,
        *,
        config,
        catalog,
        model: LibraryModel,
        watch: WatchState,
        screen: LibraryScreen,
        provider_manager,
        jobs,
        play: Callable[[dict, str, dict], None],
        tune_live: Callable[[dict], None],
        back_to_tv: Callable[[], None],
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._config = config
        self._catalog = catalog
        self._model = model
        self._watch = watch
        self._screen = screen
        self._provider_manager = provider_manager
        self._jobs = jobs
        self._play = play
        self._tune_live = tune_live
        self._back_to_tv = back_to_tv
        self._closed = False
        self._mode = "browse"
        self._zone = "grid"
        self._nav_return = "grid"  # the zone ↓ returns to from the top nav
        self._section: Optional[str] = None  # the Movies/Series section last opened
        self._opened: Optional[tuple[str, str]] = None  # (provider, section) the model shows
        # browse grid
        self._grid_content = ""  # what the grid holds: "browse", "search" or nothing yet
        self._grid_signature: Optional[tuple] = None  # (provider, section, genre, sort) shown
        self._failed = False
        self._restore_item: Optional[dict] = None  # the browse title to make current again
        # series view
        self._series: Optional[dict] = None
        self._series_details: Optional[Details] = None
        self._series_back = "browse"
        # search
        self._query = ""
        self._generation = 0
        self._snapshot = 0  # bumped whenever the cached catalogs must be read again
        self._caches: Optional[list] = None
        self._index: Optional[list] = None
        self._hits: Optional[list[Hit]] = None
        self._flat: list[Hit] = []
        self._hit_of: dict[int, Hit] = {}
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._run_search)
        # cross-provider
        self._pending: Optional[_Pending] = None
        self._pending_series: Optional[_Pending] = None  # ready provider, section loading
        self._switching_to: Optional[str] = None
        self._wire()

    # ---- wiring -------------------------------------------------------------------------------

    def _wire(self) -> None:
        model, catalog, screen = self._model, self._catalog, self._screen
        model.changed.connect(self._on_model_changed)
        model.failed.connect(self._on_model_failed)
        model.notice.connect(self.notice)
        catalog.providerReady.connect(self.on_provider_ready)
        catalog.providerFailed.connect(self._on_provider_failed)
        catalog.sectionReady.connect(self._on_section_ready)
        catalog.sectionFailed.connect(self._on_section_failed)
        catalog.seasonsReady.connect(self._on_seasons_ready)
        catalog.seasonsFailed.connect(self._on_seasons_failed)
        catalog.episodesReady.connect(self._on_episodes_ready)
        catalog.episodesFailed.connect(self._on_episodes_failed)
        genres, grid, series, nav = screen.genres, screen.grid, screen.series, screen.nav
        genres.genreSelected.connect(model.select_genre)
        genres.genreActivated.connect(self._on_genre_activated)
        genres.filterChanged.connect(self._on_filter_changed)
        grid.activated.connect(self._activate_index)
        grid.startOverRequested.connect(lambda i: self._activate_index(i, start_over=True))
        grid.favoriteToggled.connect(self._toggle_favorite)
        grid.sortRequested.connect(lambda value: model.set_sort(Sort(value)))
        grid.leftEdge.connect(self._on_left_edge)
        grid.upEdge.connect(self._on_up_edge)
        series.seasonSelected.connect(self._load_episodes)
        series.episodeActivated.connect(self._play_episode)
        series.startOverRequested.connect(
            lambda season, episode: self._play_episode(season, episode, start_over=True)
        )
        series.retryRequested.connect(self._retry_series)
        series.backRequested.connect(self._leave_series)
        nav.tabRequested.connect(self._on_tab)
        nav.searchRequested.connect(self.open_search)
        nav.sourceRequested.connect(self._show_source_menu)
        screen.search_field.clicked.connect(lambda: self._focus("search"))

    def shutdown(self) -> None:
        """The window is closing: a running search's result and pending actions are dropped."""
        self._closed = True
        self._search_timer.stop()
        self._generation += 1
        self._pending = self._pending_series = None

    # ---- state --------------------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def zone(self) -> str:
        return self._zone

    @property
    def query(self) -> str:
        return self._query

    def _focus(self, zone: str) -> None:
        if zone == "nav" and self._zone != "nav":
            self._nav_return = self._zone
        self._zone = zone
        self._screen.genres.set_focused(zone == "genres")
        self._screen.nav.set_focused(zone == "nav")
        self._screen.search_field.set_focused(zone == "search")

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._screen.set_mode(mode)

    def _sync_nav(self, active: str) -> None:
        nav = self._screen.nav
        nav.set_tabs(self._catalog.sections())
        nav.set_source_name(self._catalog.current_provider_name())
        nav.set_active(active)

    # ---- opening ------------------------------------------------------------------------------

    def open(self, section: str) -> None:
        """Show Movies or Series. Re-opening the section on screen keeps its genre and title."""
        if self._closed:
            return
        self._section = section
        self._leave_series_state()
        self._set_mode("browse")
        self._sync_nav(section)
        self._show_browse_grid()
        key = (self._catalog.current_provider_name(), section)
        if self._opened != key:
            self._opened = key
            self._restore_item = None
            self._screen.genres.escape()  # the old section's filter
            self._model.set_section(section)  # emits changed
        else:
            self._on_model_changed()
        self._focus("grid")

    def open_search(self) -> None:
        if self._closed:
            return
        if self._grid_content == "browse":  # the series view keeps its series current
            self._restore_item = self._current_item()
        self._leave_series_state()
        self._set_mode("search")
        self._sync_nav("search")
        if self._grid_content != "search":
            grid = self._screen.grid
            self._grid_content = "search"
            self._grid_signature = None
            grid.set_poster_url_provider(self._search_poster_url)
            grid.set_details_provider(self._search_details)
            grid.set_progress_provider(lambda _item: None)
            grid.set_letter_index(None)
            grid.set_header("", "", [], "")
            self._show_hits()
        self._screen.search_field.set_query(self._query)
        self._focus("search")

    def _show_browse_grid(self) -> None:
        if self._grid_content == "browse":
            return
        grid = self._screen.grid
        self._grid_content = "browse"
        self._grid_signature = None
        grid.set_poster_url_provider(poster_url)
        grid.set_details_provider(self._model.details)
        grid.set_progress_provider(self._model.progress)

    def refresh(self) -> None:
        """Read the cached catalogs again for search, and repaint from the model.

        The genre and the current title stay; provider changes reach the model on their own.
        """
        if self._closed:
            return
        self._drop_snapshot()
        self._on_model_changed()
        if self._grid_content == "search" and self._query.strip():
            self._search_timer.stop()
            self._run_search()

    # ---- keys ---------------------------------------------------------------------------------

    def handle(self, action: Action, payload: str = "") -> bool:
        """Route a resolved key to the focused zone; False when the Library has no use for it."""
        if self._closed:
            return False
        if action is Action.SEARCH:
            self.open_search()
            return True
        handler = {
            "nav": self._handle_nav,
            "genres": self._handle_genres,
            "grid": self._handle_grid,
            "series": self._handle_series,
            "search": self._handle_search,
        }[self._zone]
        return handler(action, payload)

    def _handle_grid(self, action: Action, payload: str) -> bool:
        grid = self._screen.grid
        moves = {
            Action.NAV_LEFT: (-1, 0),
            Action.NAV_RIGHT: (1, 0),
            Action.NAV_UP: (0, -1),
            Action.NAV_DOWN: (0, 1),
        }
        if action in moves:
            if grid.current_index() < 0:  # nothing listed: only the way out
                if action is Action.NAV_LEFT:
                    self._on_left_edge()
                elif action is Action.NAV_UP:
                    self._on_up_edge()
                return True
            grid.move_by(*moves[action])
        elif action in (Action.NAV_PAGE_UP, Action.NAV_PAGE_DOWN):
            grid.page(-1 if action is Action.NAV_PAGE_UP else 1)
        elif action is Action.NAV_HOME:
            grid.home()
        elif action is Action.NAV_END:
            grid.end()
        elif action is Action.NAV_ACTIVATE:
            if self._failed and self._grid_content == "browse":
                self._retry()
            else:
                grid.activate()
        elif action is Action.FAVORITE:
            if grid.current_index() >= 0:
                self._toggle_favorite(grid.current_index())
        elif action is Action.ESCAPE:
            self._focus("search" if self._mode == "search" else "genres")
        elif action in (Action.NAV_TEXT, Action.NAV_BACKSPACE):
            if self._mode == "search":
                self._focus("search")
                return self._handle_search(action, payload)
            if action is Action.NAV_TEXT and payload.strip():
                grid.jump_to_letter(payload.strip()[0])
        else:
            return False
        return True

    def _handle_genres(self, action: Action, payload: str) -> bool:
        genres = self._screen.genres
        if action in (Action.NAV_UP, Action.NAV_DOWN):
            before = genres.current_id()
            genres.move_by(-1 if action is Action.NAV_UP else 1)
            if action is Action.NAV_UP and genres.current_id() == before:
                self._focus("nav")  # ↑ on the first genre
        elif action in (Action.NAV_PAGE_UP, Action.NAV_PAGE_DOWN):
            genres.page(-1 if action is Action.NAV_PAGE_UP else 1)
        elif action in (Action.NAV_RIGHT, Action.NAV_ACTIVATE):
            if genres.current_id() is None:
                self._focus("grid")
            else:
                genres.activate()  # genreActivated focuses the grid
        elif action is Action.NAV_TEXT:
            genres.type_text(payload)
        elif action is Action.NAV_BACKSPACE:
            genres.backspace()
        elif action is Action.ESCAPE:
            if not genres.escape():
                self._back_to_tv()
        else:
            return False
        return True

    def _handle_nav(self, action: Action, payload: str) -> bool:
        nav = self._screen.nav
        if action in (Action.NAV_LEFT, Action.NAV_RIGHT):
            nav.move_by(-1 if action is Action.NAV_LEFT else 1)
        elif action is Action.NAV_ACTIVATE:
            nav.activate()
        elif action is Action.NAV_DOWN:
            back = self._nav_return
            if back == "genres" and self._mode == "search":
                back = "grid"
            self._focus(back if back != "nav" else "grid")
        elif action is Action.ESCAPE:
            self._back_to_tv()
        else:
            return False
        return True

    def _handle_series(self, action: Action, payload: str) -> bool:
        series = self._screen.series
        moves = {
            Action.NAV_LEFT: (-1, 0),
            Action.NAV_RIGHT: (1, 0),
            Action.NAV_UP: (0, -1),
            Action.NAV_DOWN: (0, 1),
        }
        if action in moves:
            series.move_by(*moves[action])
        elif action in (Action.NAV_PAGE_UP, Action.NAV_PAGE_DOWN):
            series.page(-1 if action is Action.NAV_PAGE_UP else 1)
        elif action is Action.NAV_ACTIVATE:
            series.activate()
        elif action in (Action.ESCAPE, Action.NAV_BACKSPACE):
            series.escape()  # backRequested
        else:
            return False
        return True

    def _handle_search(self, action: Action, payload: str) -> bool:
        if action is Action.NAV_TEXT:
            self._set_query(self._query + payload)
        elif action is Action.NAV_BACKSPACE:
            if self._query:
                self._set_query(self._query[:-1])
        elif action is Action.NAV_DOWN:
            if self._screen.grid.current_index() >= 0:
                self._focus("grid")
        elif action is Action.NAV_UP:
            self._focus("nav")
        elif action is Action.NAV_ACTIVATE:
            if self._search_timer.isActive():
                self._search_timer.stop()
                self._run_search()
            if self._screen.grid.current_index() >= 0:
                self._focus("grid")
        elif action is Action.ESCAPE:
            if self._query:
                self._search_timer.stop()
                self._generation += 1  # a search under way no longer counts
                self._query = ""
                self._screen.search_field.set_query("")
                self._hits = None
                self._show_hits()
            else:
                self._leave_search()
        else:
            return False
        return True

    # ---- zone edges, tabs and source ----------------------------------------------------------

    def _on_left_edge(self) -> None:
        if self._mode == "browse":
            self._focus("genres")

    def _on_up_edge(self) -> None:
        self._focus("search" if self._mode == "search" else "nav")

    def _on_tab(self, tab: str) -> None:
        if tab == "live":
            self._back_to_tv()
        else:
            self.open(tab)

    def _leave_search(self) -> None:
        if self._section is None:
            self._back_to_tv()
        else:
            self.open(self._section)

    def _show_source_menu(self, pos: QPoint) -> None:
        menu = self._source_menu()
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(pos)

    def _source_menu(self) -> QMenu:
        menu = QMenu(self._screen)
        current = self._catalog.current_provider_name()
        for name in self._catalog.provider_names():
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name == current)
            action.triggered.connect(lambda _checked=False, n=name: self._choose_source(n))
        return menu

    def _choose_source(self, name: str) -> None:
        if self._closed or name == self._catalog.current_provider_name():
            return
        self._switching_to = name
        self._catalog.select_provider(name)  # the model re-opens its section when ready

    # ---- model --------------------------------------------------------------------------------

    def _section_available(self) -> bool:
        return self._model.section in self._catalog.sections()

    def _on_model_changed(self) -> None:
        if self._closed:
            return
        genres = self._screen.genres
        available = self._section_available()
        genres.set_genres(self._model.genres(genres.filter_text) if available else [])
        if self._zone != "genres":  # don't pull the highlight from under the user
            genres.set_current(self._model.genre_id)
        if self._grid_content == "browse":
            self._refresh_grid(available)

    def _genre_title(self) -> str:
        genre_id = self._model.genre_id
        return next((g.title for g in self._model.genres() if g.id == genre_id), "")

    def _refresh_grid(self, available: bool) -> None:
        grid, model = self._screen.grid, self._model
        signature = (
            self._catalog.current_provider_name(),
            model.section,
            model.genre_id,
            model.sort,
        )
        same = signature == self._grid_signature
        self._grid_signature = signature
        if not same:
            self._failed = False
        if not available:
            label = SECTION_LABELS.get(model.section, model.section).lower()
            grid.set_header("", "", [], "")
            grid.set_sections([])
            grid.set_letter_index(None)
            grid.set_message(f"{self._catalog.current_provider_name()} has no {label}")
            return
        items = model.items()
        sorts: list[tuple[str, str]] = []
        if model.genre_id not in PINNED_TITLES:
            sorts = [(s.value, SORT_LABELS[s]) for s in model.available_sorts()]
        count = "" if items is None else f"{len(items):,} titles"
        grid.set_header(self._genre_title(), count, sorts, model.sort.value)
        if not items:
            grid.set_sections([])
            grid.set_letter_index(None)
            if items is None:
                grid.set_message(RETRY_TEXT if self._failed else LOADING_TEXT)
            else:
                grid.set_message(EMPTY_TEXT)
            return
        grid.set_message(None)
        grid.set_sections([("", items)], keep_current=same)
        grid.set_letter_index(model.letter_index())
        restore, self._restore_item = self._restore_item, None
        if restore is not None:  # back from search: the title that was current
            index = next((k for k, item in enumerate(items) if item is restore), -1)
            if index >= 0:
                grid.set_current_index(index)

    def _on_model_failed(self, message: str) -> None:
        logger.info("Library content failed to load (%s)", message)
        self._failed = True
        if self._grid_content == "browse" and self._model.items() is None:
            grid = self._screen.grid
            grid.set_sections([])
            grid.set_message(RETRY_TEXT)

    def _retry(self) -> None:
        self._failed = False
        if self._model.genre_id is None:
            self._model.set_section(self._model.section)
        else:
            self._model.select_genre(self._model.genre_id)

    def _on_genre_activated(self, _genre_id: str) -> None:
        if self._mode == "series":  # a click on a genre beside the series view
            self._leave_series()
        self._focus("grid")

    def _on_filter_changed(self, text: str) -> None:
        genres = self._screen.genres
        genres.set_genres(self._model.genres(text) if self._section_available() else [])

    def _current_item(self) -> Optional[dict]:
        grid = self._screen.grid
        return grid.item(grid.current_index())

    def _toggle_favorite(self, index: int) -> None:
        item = self._screen.grid.item(index)
        if item is None:
            return
        self._model.toggle_favorite(item)  # changed: the browse grid repaints its strip
        if self._grid_content == "search":
            self._screen.grid.set_details_provider(self._search_details)

    # ---- activation ---------------------------------------------------------------------------

    def _activate_index(self, index: int, start_over: bool = False) -> None:
        if self._closed:
            return
        if self._grid_content == "search":
            if 0 <= index < len(self._flat):
                self._activate_hit(self._flat[index], start_over)
            return
        item = self._screen.grid.item(index)
        if item is None:
            return
        kind = self._model.item_kind(item)
        if kind == "series":
            self._open_series(item, back="browse")
        elif kind == "episode":  # Continue in Series: no series dict, so no autoplay
            self._play(item, "episode", _context(start_over=start_over))
        else:
            items = self._model.items()
            self._play(item, "movie", _context(start_over=start_over, genre_items=items))

    def _activate_hit(self, hit: Hit, start_over: bool) -> None:
        if hit.provider == self._catalog.current_provider_name():
            self._run_hit(hit.section, hit.item, start_over)
            return
        self._pending = _Pending(hit.provider, hit.section, hit.item, start_over)
        self._pending_series = None
        if self._switching_to != hit.provider:
            self._switching_to = hit.provider
            self._catalog.select_provider(hit.provider)

    def _run_hit(self, section: str, item: dict, start_over: bool) -> None:
        if section == "live":
            self._tune_live(item)
        elif section == "series":
            self._open_series(item, back="search" if self._grid_content == "search" else "browse")
        else:
            self._play(item, "movie", _context(start_over=start_over))

    def on_provider_ready(self, name: str) -> None:
        """Runs the pending cross-provider action when `name` is its provider, else drops it."""
        if self._closed:
            return
        self._switching_to = None
        self._drop_snapshot()
        if self._opened is not None:
            self._opened = (name, self._model.section)  # the model re-opens its section
        if self._mode == "series":
            self._leave_series()
        self._sync_nav("search" if self._mode == "search" else self._model.section)
        pending, self._pending = self._pending, None
        self._pending_series = None
        if pending is None or pending.provider != name:
            return
        if pending.section == "series":
            # Seasons are asked for once the new provider's series section is loaded.
            self._pending_series = pending
            self._catalog.request_section("series")
            return
        self._run_hit(pending.section, pending.item, pending.start_over)

    def _on_provider_failed(self, name: str, message: str) -> None:
        if self._closed:
            return
        self._switching_to = None
        pending = self._pending
        if pending is not None and pending.provider == name:
            self._pending = None
            self.notice.emit(f"{name}: {message}" if name else message)

    def _on_section_ready(self, section: str) -> None:
        pending = self._pending_series
        if section != "series" or pending is None or self._closed:
            return
        if pending.provider != self._catalog.current_provider_name():
            return
        self._pending_series = None
        self._run_hit(pending.section, pending.item, pending.start_over)

    def _on_section_failed(self, section: str, message: str) -> None:
        if section == "series" and self._pending_series is not None:
            self._pending_series = None
            logger.info("The series section failed to load (%s)", message)
            self.notice.emit("Couldn't load that series")

    # ---- series -------------------------------------------------------------------------------

    def _open_series(self, series: dict, *, back: str) -> None:
        self._series = series
        self._series_back = back
        self._set_mode("series")
        self._focus("series")
        view = self._screen.series
        details = self._search_details(series) if back == "search" else self._model.details(series)
        self._series_details = details
        view.set_series(series, details)
        view.set_progress_provider(self._episode_progress)
        seasons = self._catalog.seasons(series)
        if seasons is None:
            self._catalog.request_seasons(series)
        else:
            self._show_seasons(seasons)

    def _is_current_series(self, series_id: str) -> bool:
        return self._series is not None and str(self._series.get("id")) == series_id

    def _on_seasons_ready(self, series_id: str) -> None:
        if self._is_current_series(series_id) and self._series is not None:
            self._show_seasons(self._catalog.seasons(self._series) or [])

    def _on_seasons_failed(self, series_id: str, message: str) -> None:
        if self._is_current_series(series_id):
            self._screen.series.set_seasons(None, error=message)

    def _show_seasons(self, seasons: list[dict]) -> None:
        view = self._screen.series
        target = self._history_season(seasons)
        if target is not None:
            # The view keeps the season whose episodes are loading when the seasons arrive.
            view.set_episodes(target, None)
        view.set_seasons(seasons)
        season = view.current_season()
        if season is not None:
            self._load_episodes(season)

    def _history_season(self, seasons: list[dict]) -> Optional[dict]:
        """The season of the newest history entry for the open series, when it is known.

        STB episodes copy the series dict (so their id is the series id) and carry
        `season_id`; an entry may also name its `series_id`. Otherwise it is unknown.
        """
        if self._series is None or not seasons:
            return None
        series_id = str(self._series.get("id"))
        provider = self._catalog.current_provider_name()
        for entry in self._config.watch_history or []:
            episode = entry.get("item_data")
            if (
                entry.get("provider_name") != provider
                or entry.get("item_type") != "episode"
                or not isinstance(episode, dict)
            ):
                continue
            owner = episode.get("series_id")
            if owner is None and episode.get("season_id") is not None:
                owner = episode.get("id")
            if owner is None or str(owner) != series_id:
                continue
            season_id, number = episode.get("season_id"), episode.get("season")
            for season in seasons:
                if season_id is not None and str(season.get("id")) == str(season_id):
                    return season
            for season in seasons:
                if number not in (None, "") and str(season.get("number")) == str(number):
                    return season
            return None
        return None

    def _load_episodes(self, season: dict) -> None:
        if self._series is None:
            return
        episodes = self._catalog.episodes(self._series, season)
        if episodes is None:
            self._catalog.request_episodes(self._series, season)
        else:
            self._screen.series.set_episodes(season, episodes)

    def _season_by_id(self, season_id: str) -> Optional[dict]:
        season = self._screen.series.current_season()
        if season is not None and str(season.get("id")) == season_id:
            return season
        return None

    def _on_episodes_ready(self, series_id: str, season_id: str) -> None:
        season = self._season_by_id(season_id)
        if self._is_current_series(series_id) and season is not None and self._series:
            self._screen.series.set_episodes(season, self._catalog.episodes(self._series, season))

    def _on_episodes_failed(self, series_id: str, season_id: str, message: str) -> None:
        season = self._season_by_id(season_id)
        if self._is_current_series(series_id) and season is not None:
            self._screen.series.set_episodes(season, None, error=message)

    def _retry_series(self) -> None:
        if self._series is None:
            return
        season = self._screen.series.current_season()
        if season is None or self._catalog.seasons(self._series) is None:
            details = self._series_details or self._model.details(self._series)
            self._screen.series.set_series(self._series, details)
            self._catalog.request_seasons(self._series)
        else:
            self._screen.series.set_episodes(season, None)
            self._catalog.request_episodes(self._series, season)

    def _play_episode(self, season: dict, episode: dict, start_over: bool = False) -> None:
        series = self._series
        episodes = self._catalog.episodes(series, season) if series is not None else None
        self._play(
            episode,
            "episode",
            _context(start_over=start_over, series=series, season=season, episodes=episodes),
        )

    def _episode_progress(self, episode: dict) -> Optional[float]:
        """Resume progress, or the watched share (≥ 90%) that shows the check mark."""
        cid = WatchState.content_id(
            self._catalog.current_provider_name(), episode, item_type="episode"
        )
        saved = (self._config.playback_positions or {}).get(cid) or {}
        position, duration = int(saved.get("position") or 0), int(saved.get("duration") or 0)
        if position <= 0 or duration <= 0:
            return None
        share = min(1.0, position / duration)
        if share >= RESUME_MAX_RATIO or self._watch.resume_position(cid) is not None:
            return share
        return None

    def _leave_series_state(self) -> None:
        self._series = None
        self._series_details = None

    def _leave_series(self) -> None:
        back = self._series_back
        self._leave_series_state()
        self._set_mode(back)
        self._focus("grid" if self._screen.grid.current_index() >= 0 else _empty_zone(back))

    # ---- search -------------------------------------------------------------------------------

    def _set_query(self, text: str) -> None:
        self._query = text
        self._screen.search_field.set_query(text)
        self._search_timer.start()  # restarts the debounce

    def _drop_snapshot(self) -> None:
        self._snapshot += 1
        self._caches = self._index = None

    def _run_search(self) -> None:
        if self._closed:
            return
        self._generation += 1
        text = self._query
        if not text.strip():
            self._hits = None
            self._show_hits()
            return
        if self._hits is None and self._grid_content == "search":
            self._screen.grid.set_message(SEARCHING_TEXT)
        worker = SearchWorker(self._provider_manager, text, caches=self._caches, index=self._index)
        self._jobs.run_worker(
            worker,
            self._on_search_done,
            self._on_search_failed,
            context=(self._generation, self._snapshot),
        )

    def _on_search_done(self, context: tuple[int, int], payload) -> None:
        generation, snapshot = context
        caches, index, hits = payload
        if snapshot == self._snapshot and self._index is None:
            self._caches, self._index = caches, index  # titles are folded once per snapshot
        if self._closed or generation != self._generation:
            return  # a newer search, or none, owns the grid
        self._hits = list(hits)
        self._show_hits()

    def _on_search_failed(self, context: tuple[int, int], message: str) -> None:
        generation, _snapshot = context
        logger.warning("Search failed (%s)", message)
        if self._closed or generation != self._generation:
            return
        self._hits = []
        self._flat, self._hit_of = [], {}
        if self._grid_content == "search":
            self._screen.grid.set_sections([])
            self._screen.grid.set_message(SEARCH_FAILED_TEXT)
            self._screen.search_field.set_count_text("")

    def _show_hits(self) -> None:
        if self._grid_content != "search":
            return
        grid, field = self._screen.grid, self._screen.search_field
        hits = self._hits
        sections: list[tuple[str, list[dict]]] = []
        flat: list[Hit] = []
        for (provider, section), group in itertools.groupby(
            hits or [], key=lambda h: (h.provider, h.section)
        ):
            found = list(group)
            label = SECTION_LABELS.get(section, section)
            sections.append((f"{provider} · {label} ({len(found):,})", [h.item for h in found]))
            flat.extend(found)
        self._flat = flat
        self._hit_of = {id(h.item): h for h in flat}
        grid.set_sections(sections)
        if hits is None:
            grid.set_message(SEARCH_PROMPT)
            field.set_count_text("")
        elif not flat:
            grid.set_message(f"Nothing matches “{self._query.strip()}”")
            field.set_count_text("No results")
        else:
            grid.set_message(None)
            field.set_count_text(f"{len(flat):,} result" + ("" if len(flat) == 1 else "s"))

    def _search_poster_url(self, item: dict) -> str:
        hit = self._hit_of.get(id(item))
        if hit is not None and hit.section == "live":
            logo = str(item.get("logo") or "")
            return LIVE_TILE + logo if logo else ""
        return poster_url(item)

    def _search_details(self, item: dict) -> Details:
        """The strip of a search hit: the model's parsing, with the hit's own kind and resume."""
        hit = self._hit_of.get(id(item))
        section = hit.section if hit is not None else "movies"
        resume = None
        if hit is not None and section == "movies":
            cid = WatchState.content_id(hit.provider, item, item_type="movie")
            resume = self._watch.resume_position(cid)
        return replace(
            self._model.details(item),
            genre=str(item.get("genre") or item.get("genres_str") or ""),
            kind="series" if section == "series" else "movie",
            resume_ms=resume,
            progress=None,
        )


def _empty_zone(mode: str) -> str:
    """The zone to focus when a mode has nothing in its grid."""
    return "search" if mode == "search" else "genres"
