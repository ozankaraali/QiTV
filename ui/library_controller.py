"""The Library's behaviour: wires the LibraryModel, the catalog and search to the LibraryScreen.

The screen's widgets never take keyboard focus. TVWindow resolves keys with the "library"
context and hands them to `handle`, which routes them to the focused zone: the top nav, the
genre column, the grid, the series view or the search field.

Search lives in a SearchSession. Every activation (a title, an episode, a search hit) goes
through one PendingAction: it runs at once when the title's own provider is ready, otherwise
that provider's setup starts beside whatever the Library or the TV page shows and the action
runs once it is ready (no source switch, no notice). Leaving the Library, or playing something
else, drops it.

The Library's source is a provider or All providers (the source menu); it never touches the
TV page's source and never stops what plays.

A right-click on a film's poster or an episode row asks the shell for its menu (Play in VLC,
Play in MPV, Copy stream link: `context_menu(item, section, global position)`, section "movies"
or "series"). Links are asked of the provider QiTV is on, so a title of another provider, or
any title during a provider switch, has no menu; nor has a series or a channel.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtWidgets import QMenu

from services import i18n
from services.catalog import PROVIDER_LOADING_MESSAGE
from services.failure_reasons import shown_message
from services.i18n import N_, tr, tr_n, translate
from services.library import (
    PINNED_TITLES,
    SORT_LABELS,
    Details,
    LibraryModel,
    Sort,
    history_episode,
    history_season,
)
from services.navigation import episode_key
from services.pending_action import LOAD, RUN, Pending, PendingAction
from services.provider_ids import (
    ALL_PROVIDERS,
    ALL_PROVIDERS_TITLE,
    provider_of,
    split_group_id,
)
from services.search import Hit
from services.watch_state import RESUME_MAX_RATIO, WatchState
from ui.context_actions import HIDE_TEXT
from ui.keymap import Action
from ui.library.library_screen import LibraryScreen
from ui.library.poster_grid import poster_url
from ui.library.search_session import DEBOUNCE_MS, SearchSession
from ui.library.top_nav import REFRESH_TEXT

logger = logging.getLogger(__name__)


def _titles(count: int) -> str:
    """The grid header's count: "1 title", "1,200 titles"."""
    # i18n: the number of films or series in the Library's genre (beside its name)
    return tr_n("{n} title", "{n} titles", count, n=i18n.number(count))


def _no_section_text(source: str, section: str) -> str:
    """What the grid says when the source has no such section (an M3U playlist's movies)."""
    if section == "movies":
        return tr("{source} has no movies", source=source)  # i18n: {source}: a provider's name
    if section == "series":
        return tr("{source} has no series", source=source)  # i18n: {source}: a provider's name
    return tr("{source} has no live TV", source=source)  # i18n: {source}: a provider's name


SEARCH_DEBOUNCE_MS = DEBOUNCE_MS
# What the grid or a toast shows: shown with translate().
LOADING_TEXT = N_("Loading…")
EMPTY_TEXT = N_("Nothing here")  # i18n: a Library genre without titles
RETRY_TEXT = N_("Couldn't load — press Enter to retry")
SERIES_FAILED_NOTICE = N_("Couldn't load that series")
UNKNOWN_PROVIDER_NOTICE = N_("That title's provider is no longer in your list")
REFRESHING_TEXT = N_("Refreshing films and series…")  # i18n: a toast (2.8: the Library's ↻)
_MOVES = {
    Action.NAV_LEFT: (-1, 0),
    Action.NAV_RIGHT: (1, 0),
    Action.NAV_UP: (0, -1),
    Action.NAV_DOWN: (0, 1),
}


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
    searchChanged = Signal()  # the search's hits changed (the phone remote mirrors them)

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
        context_menu: Optional[Callable[[dict, str, QPoint], None]] = None,
        tmdb=None,
        parent: Optional[QObject] = None,
    ):
        """`tmdb` (2.8, a ui.tmdb_fill.TmdbFill) fills titles' gaps from TMDB; None: never."""
        super().__init__(parent)
        self._config = config
        self._tmdb = tmdb
        self._catalog = catalog
        self._model = model
        self._watch = watch
        self._screen = screen
        self._play = play
        self._tune_live = tune_live
        self._back_to_tv = back_to_tv
        self._context_menu = context_menu
        # 2.8: parental controls hide a genre (set_hiding); none until set.
        self._hide_genre: Optional[Callable[[str, str, str, str], None]] = None
        self._may_hide: Callable[[], bool] = lambda: False
        self._closed = False
        self._mode = "browse"
        self._zone = "grid"
        self._nav_return = "grid"  # the zone ↓ returns to from the top nav
        self._section: Optional[str] = None  # the Movies/Series section last opened
        self._opened: Optional[tuple[str, str]] = None  # (provider, section) the model shows
        # the grid
        self._grid_content = ""  # what the grid holds: "browse", "search" or nothing yet
        self._grid_signature: Optional[tuple] = None  # (provider, section, genre, sort) shown
        self._failed = False
        self._restore_item: Optional[dict] = None  # the browse title to make current again
        # the series view
        self._series: Optional[dict] = None
        self._series_provider = ""
        self._series_details: Optional[Details] = None
        self._series_back = "browse"
        self._history_episode: Optional[dict] = None  # the series' newest watched episode
        # search and cross-provider activations
        self._search = SearchSession(
            provider_manager,
            jobs,
            model,
            watch,
            self,
            loaded=getattr(catalog, "loaded_contents", None),
        )
        self._pending = PendingAction()
        self._ran_for = ""  # the shown provider whose ready just ran the pending action
        # The catalog whose section signals are followed, by provider: a catalog the hub
        # rebuilds (an edit in Settings) is a new object, followed when it is next asked.
        self._watched_sections: dict[str, object] = {}
        self._wire()

    # ---- wiring -------------------------------------------------------------------------------

    def _wire(self) -> None:
        model, catalog, screen = self._model, self._catalog, self._screen
        model.changed.connect(self._on_model_changed)
        model.failed.connect(self._on_model_failed)
        model.notice.connect(self.notice)
        catalog.providerReady.connect(self._on_provider_ready)
        # An activation waits for its own provider, which need not be the one shown.
        catalog.catalogs.providerReady.connect(self._on_pending_ready)
        catalog.catalogs.providerFailed.connect(self._on_provider_failed)
        catalog.catalogs.providersChanged.connect(self._on_providers_changed)
        catalog.seasonsReady.connect(self._on_seasons_ready)
        catalog.seasonsFailed.connect(self._on_seasons_failed)
        catalog.episodesReady.connect(self._on_episodes_ready)
        catalog.episodesFailed.connect(self._on_episodes_failed)
        genres, grid, series, nav = screen.genres, screen.grid, screen.series, screen.nav
        genres.genreSelected.connect(model.select_genre)
        genres.genreActivated.connect(lambda _id: self._focus("grid"))
        genres.filterChanged.connect(self._on_filter_changed)
        grid.activated.connect(self._activate_index)
        grid.startOverRequested.connect(lambda i: self._activate_index(i, start_over=True))
        grid.favoriteToggled.connect(self._toggle_favorite)
        grid.sortRequested.connect(lambda value: model.set_sort(Sort(value)))
        grid.leftEdge.connect(self._on_left_edge)
        grid.upEdge.connect(self._on_up_edge)
        grid.contextRequested.connect(self._on_grid_context)
        series.seasonSelected.connect(self._load_episodes)
        series.episodeActivated.connect(self._activate_episode)
        series.startOverRequested.connect(
            lambda season, episode: self._activate_episode(season, episode, start_over=True)
        )
        series.retryRequested.connect(self._retry_series)
        series.backRequested.connect(self._leave_series)
        series.episodeContextRequested.connect(self._on_episode_context)
        nav.tabRequested.connect(self._on_tab)
        nav.searchRequested.connect(self.open_search)
        nav.sourceRequested.connect(self._show_source_menu)
        genres.genreMenuRequested.connect(self._show_genre_menu)
        nav.refreshRequested.connect(self.refresh_lists)
        screen.search_field.clicked.connect(lambda: self._focus("search"))
        self._search.changed.connect(self._show_hits)
        self._search.changed.connect(self.searchChanged)
        if self._tmdb is not None:  # 2.8
            model.set_filler(self._tmdb.fill)
            grid.focusChanged.connect(self._on_grid_focus)
            grid.set_settled_handler(self._on_grid_settled)
            self._tmdb.changed.connect(self._on_tmdb_changed)

    def shutdown(self) -> None:
        """The window is closing: a running search's result and a pending action are dropped."""
        self._closed = True
        self._search.shutdown()
        self._pending.cancel()
        if self._tmdb is not None:
            self._tmdb.shutdown()

    # ---- TMDB (2.8) ---------------------------------------------------------------------------

    def _grid_kind(self, item: dict) -> str:
        """A grid title's kind: a search hit's own, else the shown section's."""
        if self._grid_content == "search":
            return self._search.kind_of(item)
        return self._model.item_kind(item)

    def _poster(self, item: dict, own: str, kind: Optional[str] = None) -> str:
        """A title's poster: its provider's (`own`), else TMDB's when the provider has none or
        it could not be loaded. `kind`: the title's, a grid title's by default."""
        if self._tmdb is None or (own and not self._screen.grid_posters.failed(own)):
            return own
        return self._tmdb.poster_url(item, kind or self._grid_kind(item)) or own

    def _on_grid_focus(self, index: int) -> None:
        item = self._screen.grid.item(index)
        if item is not None and self._tmdb is not None:
            self._tmdb.focus(item, self._grid_kind(item))

    def _on_grid_settled(self, items: list[dict]) -> None:
        if self._tmdb is None:
            return
        posters = self._screen.grid_posters
        posterless = []
        for item in items:
            kind = self._grid_kind(item)
            own = poster_url(item)
            if kind in ("movie", "series") and (not own or posters.failed(own)):
                posterless.append((item, kind))
        self._tmdb.want_posters(posterless)

    def _on_tmdb_changed(self) -> None:
        if self._closed:
            return
        self._screen.grid.refresh_details()
        series = self._series
        if series is not None and self._tmdb is not None:
            back = self._series_back
            details = (
                self._search.details(series) if back == "search" else self._model.details(series)
            )
            self._series_details = details
            own = poster_url(series)
            poster = self._poster(series, own, "series")
            self._screen.series.set_details(details, poster if poster != own else "")

    def leave(self) -> None:
        """The window left the Library: nothing waiting for a provider plays any more."""
        self._pending.cancel()

    # ---- state --------------------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def zone(self) -> str:
        return self._zone

    @property
    def query(self) -> str:
        return self._search.query

    @property
    def section(self) -> Optional[str]:
        """The Movies or Series section opened last; None until one was."""
        return self._section

    def _focus(self, zone: str) -> None:
        if zone == "nav" and self._zone != "nav":
            self._nav_return = self._zone
        self._zone = zone
        self._screen.genres.set_focused(zone == "genres")
        self._screen.nav.set_focused(zone == "nav")
        self._screen.grid.set_focused(zone == "grid")
        self._screen.search_field.set_focused(zone == "search")

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._screen.set_mode(mode)

    def _tabs(self) -> list[str]:
        """The provider's sections, and Movies for the recordings when it has no films (2.8)."""
        tabs = list(self._catalog.sections())
        if "movies" not in tabs and self._model.lists_recordings():
            tabs.append("movies")
        return tabs

    def _sync_nav(self, active: str) -> None:
        nav = self._screen.nav
        nav.set_tabs(self._tabs())
        nav.set_source_name(self._source_title())
        nav.set_active(active)

    def _source_title(self) -> str:
        """The Library's source as the user reads it: a provider's name, the names it mixes
        (2.8), or All providers."""
        name = str(self._catalog.current_provider_name())
        if name != ALL_PROVIDERS:
            return name
        mixed = self._catalog.mixed_names()
        if len(mixed) < len(self._catalog.provider_names()):
            return ", ".join(mixed)
        return translate(ALL_PROVIDERS_TITLE)

    def _series_owner(self) -> str:
        """The provider of the series open (the one shown while none is)."""
        return self._series_provider or self._catalog.current_provider_name()

    def _go_back_to_tv(self) -> None:
        self.leave()
        self._back_to_tv()

    # ---- opening ------------------------------------------------------------------------------

    def open(self, section: str) -> None:
        """Show Movies or Series. Re-opening the section on screen keeps its genre and title."""
        if self._closed:
            return
        self._section = section
        self._series = None
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

    def open_on(self, provider: str, section: str) -> None:
        """Show Movies or Series of `provider` (a provider's name, or All providers): the
        channel list's rows open the Library on the provider they are listed under (2.8). An
        empty or unknown one keeps the Library's own source."""
        if self._closed:
            return
        if provider == ALL_PROVIDERS:
            self._mix_sources([])  # every provider, not a mix the Library kept (review M6)
        elif provider in self._catalog.provider_names():
            self._choose_source(provider)
        self.open(section)

    def remote_search(self, query: str) -> None:
        """The phone remote's typing: the search shows and searches `query` as it is now."""
        if self._closed:
            return
        if not query:
            self._search.set_query("")
        self.open_search(query)

    def search_hits(self) -> list[Hit]:
        """What the search shows now (none while it has no query or is still searching)."""
        return list(self._search.hits or [])

    def activate_hit(self, hit: Hit) -> None:
        """A search result tapped on the phone remote: as Enter on it."""
        self._activate_hit(hit, start_over=False)

    def activate_title(self, item: dict, kind: str, genre_items: Optional[list]) -> None:
        """A title tapped in the phone remote's Library: as Enter on its poster there."""
        provider = provider_of(item) or self._catalog.current_provider_name()
        self._activate_item(item, provider, kind, False, genre_items)

    def open_search(self, query: str = "") -> None:
        """The search; `query` (typed elsewhere: the channel list's find) is searched at once."""
        if self._closed:
            return
        if query:
            self._search.set_query(query)
            self._search.run_now()
        if self._grid_content == "browse":  # the series view keeps its series current
            self._restore_item = self._current_item()
        self._series = None
        self._set_mode("search")
        self._sync_nav("search")
        if self._grid_content != "search":
            grid = self._screen.grid
            self._grid_content = "search"
            self._grid_signature = None
            logo_key = self._screen.grid_posters.logo_key
            grid.set_poster_url_provider(
                lambda item: self._poster(item, self._search.poster_url(item, logo_key))
            )
            grid.set_tile_provider(self._search.is_channel)
            grid.set_details_provider(self._search.details)
            grid.set_progress_provider(lambda _item: None)
            grid.set_key_provider(None)
            grid.set_letter_index(None)
            grid.set_header("", "", [], "")
            self._show_hits()
        self._screen.search_field.set_query(self._search.query)
        self._focus("search")

    def _show_browse_grid(self) -> None:
        if self._grid_content == "browse":
            return
        grid = self._screen.grid
        self._grid_content = "browse"
        self._grid_signature = None
        grid.set_poster_url_provider(lambda item: self._poster(item, poster_url(item)))
        grid.set_tile_provider(None)
        grid.set_details_provider(self._model.details)
        grid.set_progress_provider(self._model.progress)
        grid.set_key_provider(self._model.identity)  # a genre loaded again keeps its title

    def refresh(self) -> None:
        """Read the cached catalogs again for search, and repaint from the model.

        The genre and the current title stay; provider changes reach the model on their own.
        """
        if self._closed:
            return
        self._search.drop_snapshot()
        self._on_model_changed()
        if self._grid_content == "search":
            self._search.rerun()

    # ---- keys ---------------------------------------------------------------------------------

    def handle(self, action: Action, payload: str = "") -> bool:
        """Route a resolved key to the focused zone; False when the Library has no use for it."""
        if self._closed:
            return False
        if action is Action.SEARCH:
            if payload and self._zone in ("search", "genres"):
                return self.handle(Action.NAV_TEXT, payload)  # "/" types where text goes
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
        if action in _MOVES:
            if grid.current_index() < 0:  # nothing listed: only the way out
                if action is Action.NAV_LEFT:
                    self._on_left_edge()
                elif action is Action.NAV_UP:
                    self._on_up_edge()
                return True
            grid.move_by(*_MOVES[action])
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
                self._go_back_to_tv()
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
            if back == "genres" and self._mode != "browse":
                back = "grid"
            self._focus(back if back != "nav" else "grid")
        elif action is Action.ESCAPE:
            self._go_back_to_tv()
        else:
            return False
        return True

    def _handle_series(self, action: Action, payload: str) -> bool:
        series = self._screen.series
        if action in _MOVES:
            series.move_by(*_MOVES[action])
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
        search, field = self._search, self._screen.search_field
        if action is Action.NAV_TEXT:
            search.set_query(search.query + payload)
            field.set_query(search.query)
        elif action is Action.NAV_BACKSPACE:
            if search.query:
                search.set_query(search.query[:-1])
                field.set_query(search.query)
        elif action is Action.NAV_DOWN:
            if self._screen.grid.current_index() >= 0:
                self._focus("grid")
        elif action is Action.NAV_UP:
            self._focus("nav")
        elif action is Action.NAV_ACTIVATE:
            search.run_now()
            if self._screen.grid.current_index() >= 0:
                self._focus("grid")
        elif action is Action.ESCAPE:
            if search.query:
                search.clear()
                field.set_query("")
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
            self._go_back_to_tv()
        else:
            self.open(tab)

    def _leave_search(self) -> None:
        if self._section is None:
            self._go_back_to_tv()
        else:
            self.open(self._section)

    def _show_source_menu(self, pos: QPoint) -> None:
        menu = self._source_menu()
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(pos)

    def _source_menu(self) -> QMenu:
        """A tick for each provider the Library shows (2.8: several mix), and All providers."""
        menu = QMenu(self._screen)
        names = self._catalog.provider_names()
        shown = self._shown_providers()
        for name in names:
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name in shown)
            action.triggered.connect(lambda _checked=False, n=name: self._tick_source(n))
        if len(names) > 1:  # with one provider there is nothing to mix
            menu.addSeparator()
            action = menu.addAction(translate(ALL_PROVIDERS_TITLE))
            action.setCheckable(True)
            action.setChecked(self._catalog.all_providers() and len(shown) == len(names))
            action.triggered.connect(lambda _checked=False: self._mix_sources([]))
        menu.addSeparator()
        refresh = menu.addAction(translate(REFRESH_TEXT))
        refresh.triggered.connect(lambda _checked=False: self.refresh_lists())
        return menu

    def set_hiding(
        self,
        hide: Callable[[str, str, str, str], None],
        may_hide: Callable[[], bool],
    ) -> None:
        """Parental controls (2.8): `hide(section, provider, genre id, title)` hides a genre,
        offered on its right-click while `may_hide()` (a PIN is set)."""
        self._hide_genre, self._may_hide = hide, may_hide

    def _show_genre_menu(self, gid: str, pos: QPoint) -> None:
        menu = self.genre_menu(gid)
        if menu is not None:
            menu.aboutToHide.connect(menu.deleteLater)
            menu.popup(pos)

    def genre_menu(self, gid: str) -> Optional[QMenu]:
        """A genre's right-click menu: Hide (parental controls); None for a pinned row (My
        list, Continue …), a genre not shown, or without a PIN."""
        hide = self._hide_genre
        if hide is None or not self._may_hide() or self._closed:
            return None
        genre = next((g for g in self._model.genres() if g.id == gid and not g.header), None)
        if genre is None or genre.pinned:
            return None
        name, own = split_group_id(gid)
        provider = name or self._catalog.current_provider_name()
        section = self._model.section
        menu = QMenu(self._screen)
        menu.addAction(translate(HIDE_TEXT)).triggered.connect(
            lambda _=False: hide(section, provider, own or gid, genre.title)
        )
        return menu

    def refresh_lists(self) -> None:
        """The films and series of the providers shown load again (2.8: ↻, the source menu,
        Ctrl/⌘+Shift+R): an Xtream panel's and a server's behind the saved lists, which show
        meanwhile (a server's asks only what changed); a portal's genres anew. Says so when
        any loads (none while a provider sets up)."""
        if self._closed:
            return
        if self._catalog.refresh_sections():
            self.notice.emit(translate(REFRESHING_TEXT))

    def _shown_providers(self) -> list[str]:
        """The providers the Library shows: those it mixes, else its own."""
        if self._catalog.all_providers():
            return list(self._catalog.mixed_names())
        return [self._catalog.current_provider_name()]

    def _tick_source(self, name: str) -> None:
        """A provider ticked joins what the Library shows, one unticked leaves it; the last one
        stays (something is shown)."""
        shown = self._shown_providers()
        if name in shown:
            if len(shown) == 1:
                return
            self._mix_sources([n for n in shown if n != name])
        else:
            self._mix_sources(shown + [name])

    def _mix_sources(self, names: list[str]) -> None:
        """Show these providers (one alone, several mixed; none: every one). The TV page and
        what plays are not touched; the model re-opens its section when they are ready."""
        if not self._closed:
            self._catalog.select_mix(names)

    def _choose_source(self, name: str) -> None:
        if self._closed:
            return
        if name == self._catalog.current_provider_name():  # already there, or on the way
            return
        # The Library's own source: the TV page and what plays are not touched, and a title
        # waiting for its provider still waits. The model re-opens its section when ready.
        self._catalog.select_provider(name)

    # ---- model --------------------------------------------------------------------------------

    def _section_available(self) -> bool:
        return self._model.available  # the provider's section, or (Movies) recordings

    def _on_model_changed(self) -> None:
        if self._closed:
            return
        self._screen.nav.set_tabs(self._tabs())  # a first recording brings Movies (2.8)
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
        items = model.items() if available else None
        shown = self._grid_signature
        if (
            items is None
            and available
            and shown is not None
            and shown[:3] == signature[:3]
            and grid.current_index() >= 0
        ):
            # The genre shown loads again (its provider set up again, a refresh): its titles
            # stay meanwhile, and the title and the place are found again in the new list.
            # (Its default sort, which needs the titles, is not the one shown until then.)
            return
        same = signature == shown
        self._grid_signature = signature
        if not same:
            self._failed = False
        if not available:
            grid.set_header("", "", [], "")
            grid.set_sections([])
            grid.set_letter_index(None)
            grid.set_message(_no_section_text(self._source_title(), model.section))
            return
        sorts: list[tuple[str, str]] = []
        if model.genre_id not in PINNED_TITLES:
            sorts = [(s.value, translate(SORT_LABELS[s])) for s in model.available_sorts()]
        count = "" if items is None else _titles(len(items))
        grid.set_header(self._genre_title(), count, sorts, model.sort.value)
        if not items:
            grid.set_sections([])
            grid.set_letter_index(None)
            if items is None:
                grid.set_message(translate(RETRY_TEXT if self._failed else LOADING_TEXT))
            else:
                grid.set_message(translate(EMPTY_TEXT))
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
        if message == PROVIDER_LOADING_MESSAGE:
            return  # the model asks again once the provider is ready: "Loading…" stays
        if self._model.items() is not None:
            return  # about titles not on screen (another genre)
        logger.info("Library content failed to load (%s)", message)
        self._failed = True
        if self._grid_content == "browse":
            grid = self._screen.grid
            grid.set_sections([])
            grid.set_message(translate(RETRY_TEXT))

    def _retry(self) -> None:
        self._failed = False
        if self._model.genre_id is None:
            self._model.set_section(self._model.section)
        else:
            self._model.select_genre(self._model.genre_id)

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
            self._screen.grid.set_details_provider(self._search.details)

    # ---- activation ---------------------------------------------------------------------------

    def _request(self, provider: str, run: Callable[[], None], section: Optional[str] = None):
        """Run `run` now when `provider` (the title's own) is ready, else once it is.

        One action waits. A provider that is not ready starts its setup beside what is shown
        (`ensure`): nothing is selected and nothing is announced. A title of the provider the
        Library shows needs no section loaded first; a title of another waits for `section`.
        """
        catalogs = self._catalog.catalogs
        if provider not in catalogs.names():
            self.notice.emit(translate(UNKNOWN_PROVIDER_NOTICE))
            return
        ready = catalogs.is_ready(provider)
        loaded = (
            section is None
            or provider == self._catalog.current_provider_name()
            or (ready and catalogs.catalog(provider).has_content(section))
        )
        step = self._pending.activate(Pending(provider, run, section), ready=ready, loaded=loaded)
        if step == RUN:
            run()
            return
        catalogs.ensure(provider)
        if step == LOAD and section is not None:
            self._load_section(provider, section)

    def _load_section(self, provider: str, section: str) -> None:
        """Ask `provider` for `section`; the pending action runs when it arrives."""
        catalog = self._catalog.catalogs.catalog(provider)
        if self._watched_sections.get(provider) is not catalog:
            self._watched_sections[provider] = catalog
            catalog.sectionReady.connect(lambda s, p=provider: self._on_section_ready(p, s))
            catalog.sectionFailed.connect(
                lambda s, message, p=provider: self._on_section_failed(p, s, message)
            )
        catalog.request_section(section)

    def _browse_provider(self) -> str:
        """The provider of the titles the browse grid and the model show."""
        return self._opened[0] if self._opened is not None else ""

    def _activate_index(self, index: int, start_over: bool = False) -> None:
        if self._closed:
            return
        if self._grid_content == "search":
            hit = self._search.hit_at(index)
            if hit is not None:
                self._activate_hit(hit, start_over)
            return
        item = self._screen.grid.item(index)
        if item is None:
            return
        provider = provider_of(item) or self._browse_provider()
        kind = self._model.item_kind(item)
        self._activate_item(item, provider, kind, start_over, self._model.items())

    def _activate_item(
        self, item: dict, provider: str, kind: str, start_over: bool, genre_items
    ) -> None:
        if kind == "recording":  # a file of the disk: no provider to wait for (2.8)
            self._play(item, "recording", _context(start_over=start_over))
        elif kind == "scheduled":  # 2.8: still to come: when, and on which channel
            self.notice.emit(str(item.get("description") or ""))
        elif kind == "series":
            self._request(provider, lambda: self._open_series(item, provider, back="browse"))
        elif kind == "episode":  # Continue in Series: no series dict, so no autoplay
            context = _context(start_over=start_over)
            self._request(provider, lambda: self._play(item, "episode", context))
        else:
            context = _context(start_over=start_over, genre_items=genre_items)
            self._request(provider, lambda: self._play(item, "movie", context))

    def _activate_hit(self, hit: Hit, start_over: bool) -> None:
        item, provider = hit.item, hit.provider
        if hit.section == "live":
            self._request(provider, lambda: self._tune_live(item))
        elif hit.section == "series":
            # Its seasons are asked for once the provider's series section is loaded.
            self._request(provider, lambda: self._open_hit_series(item, provider), "series")
        else:
            context = _context(start_over=start_over)
            self._request(provider, lambda: self._play(item, "movie", context))

    # ---- right-click menus -------------------------------------------------------------------

    def _on_grid_context(self, index: int, global_pos: QPoint) -> None:
        if self._closed:
            return
        if self._grid_content == "search":
            hit = self._search.hit_at(index)
            if hit is None or hit.section != "movies":
                return  # a channel or a series: nothing to play from a menu
            self._menu(hit.item, "movies", hit.provider, global_pos)
            return
        item = self._screen.grid.item(index)
        if item is None:
            return
        kind = self._model.item_kind(item)
        if kind in ("recording", "scheduled"):  # its own menu, no provider's (2.8)
            if self._context_menu is not None:
                self._context_menu(item, kind, global_pos)
        elif kind != "series":  # a series plays nothing: its episodes have the menu
            section = "series" if kind == "episode" else "movies"
            self._menu(item, section, provider_of(item) or self._browse_provider(), global_pos)

    def _on_episode_context(self, _season: dict, episode: dict, global_pos: QPoint) -> None:
        if not self._closed:
            self._menu(episode, "series", self._series_owner(), global_pos)

    def _menu(self, item: dict, section: str, provider: str, global_pos: QPoint) -> None:
        """The shell's menu for `item`, when its links can be asked of the provider shown (with
        All providers, of any provider: a link goes to the title's own, and waits there for
        its setup, whichever provider sets up)."""
        if (
            self._context_menu is not None
            and (self._catalog.all_providers() or not self._catalog.is_loading())
            and (
                provider == self._catalog.current_provider_name()
                or (self._catalog.all_providers() and provider in self._catalog.provider_names())
            )
        ):
            self._context_menu(item, section, global_pos)

    def _open_hit_series(self, series: dict, provider: str) -> None:
        back = "search" if self._grid_content == "search" else "browse"
        self._open_series(series, provider, back=back)

    def _on_pending_ready(self, name: str) -> None:
        """A provider (any) became ready: the action waiting for it runs or loads its section."""
        if self._closed:
            return
        step, action = self._pending.on_ready(name)
        if action is None:
            return
        if step == RUN:
            if self._catalog.all_providers() or name == self._catalog.current_provider_name():
                self._ran_for = name  # the catalog shown reports this ready too
            action.payload()
        elif step == LOAD and action.section is not None:
            self._load_section(name, action.section)

    def _on_provider_ready(self, name: str) -> None:
        """The provider shown (with All providers: any provider, or the mix) is ready."""
        if self._closed:
            return
        self._search.drop_snapshot()
        mixed = self._catalog.all_providers()
        moved = True  # the Library went to another source (not the one shown, ready again)
        if self._opened is not None and (not mixed or name == ALL_PROVIDERS):
            moved = self._opened[0] != name
            # The model opens the new source's section, or keeps the genre of the one shown.
            self._opened = (name, self._model.section)
        ran, self._ran_for = self._ran_for == name, ""
        # The old provider's series goes, unless this ready just opened one (or providers mix,
        # or the provider shown is only ready again).
        if (
            self._mode == "series"
            and not mixed
            and moved
            and not (ran and self._series_provider == name)
        ):
            self._leave_series()
        self._sync_nav("search" if self._mode == "search" else self._model.section)
        if self._grid_content == "search":
            self._search.rerun()

    def _on_providers_changed(self) -> None:
        """The provider list changed: a title waiting for a provider that is gone is dropped,
        and the catalogs the hub dropped are let go (a rebuilt one is followed when asked)."""
        created = getattr(self._catalog.catalogs, "created", None)
        if created is not None:
            alive = set(created())
            for provider in [p for p in self._watched_sections if p not in alive]:
                del self._watched_sections[provider]
        action = self._pending.action
        if not self._closed and action and action.provider not in self._catalog.catalogs.names():
            self._pending.cancel()
            self.notice.emit(translate(UNKNOWN_PROVIDER_NOTICE))

    def _on_provider_failed(self, name: str, message: str) -> None:
        if self._closed:
            return
        if self._pending.on_failed(name) is not None:
            shown = shown_message(message)  # the catalog's message is English
            if name:
                # i18n: a toast: {provider} is a provider's name, {message} why it failed
                shown = tr("{provider}: {message}", provider=name, message=shown)
            self.notice.emit(shown)

    def _on_section_ready(self, provider: str, section: str) -> None:
        if self._closed:
            return
        action = self._pending.on_section_ready(provider, section)
        if action is not None:
            action.payload()

    def _on_section_failed(self, provider: str, section: str, message: str) -> None:
        if self._closed:
            return
        if self._pending.on_section_failed(provider, section) is not None:
            logger.info("A pending %s section failed to load (%s)", section, message)
            self.notice.emit(translate(SERIES_FAILED_NOTICE))

    # ---- series -------------------------------------------------------------------------------

    def _open_series(self, series: dict, provider: str, *, back: str) -> None:
        self._series = series
        self._series_provider = provider
        self._series_back = back
        self._set_mode("series")
        self._focus("series")
        view = self._screen.series
        details = self._search.details(series) if back == "search" else self._model.details(series)
        self._series_details = details
        own = poster_url(series)
        poster = self._poster(series, own, "series")
        view.set_series(series, details, poster if poster != own else "")
        if self._tmdb is not None:
            self._tmdb.focus(series, "series")
        view.set_progress_provider(self._episode_progress)
        self._history_episode = history_episode(
            self._config.watch_history, self._series_owner(), series
        )
        seasons = self._catalog.seasons(series)
        if seasons is None:
            self._catalog.request_seasons(series)
        else:
            self._show_seasons(seasons)

    def _is_current_series(self, series_id: str) -> bool:
        return self._series is not None and str(self._series.get("id")) == series_id

    def _on_seasons_ready(self, series_id: str) -> None:
        if self._series is not None and self._is_current_series(series_id):
            self._show_seasons(self._catalog.seasons(self._series) or [])

    def _on_seasons_failed(self, series_id: str, message: str) -> None:
        if self._is_current_series(series_id):
            self._screen.series.set_seasons(None, error=message)

    def _show_seasons(self, seasons: list[dict]) -> None:
        if self._series is None:
            return
        view = self._screen.series
        current = history_season(
            self._config.watch_history,
            self._series_owner(),
            self._series,
            seasons,
        )
        view.set_seasons(seasons, current=current)  # kept when the view already has a season
        season = view.current_season()
        if season is not None:
            self._load_episodes(season)

    def _load_episodes(self, season: dict) -> None:
        if self._series is None:
            return
        episodes = self._catalog.episodes(self._series, season)
        if episodes is None:
            self._catalog.request_episodes(self._series, season)
        else:
            self._show_episodes(season, episodes)

    def _show_episodes(self, season: dict, episodes: Optional[list[dict]]) -> None:
        start = self._start_episode(episodes or [])
        self._screen.series.set_episodes(season, episodes, current=start)

    def _start_episode(self, episodes: list[dict]) -> Optional[dict]:
        """The history episode when it is in `episodes`, or the next one once it was finished."""
        episode = self._history_episode
        if episode is None:
            return None
        key = episode_key(episode)
        row = next((k for k, e in enumerate(episodes) if episode_key(e) == key), -1)
        if row < 0:
            return None
        progress = self._episode_progress(episodes[row])
        if progress is not None and progress >= RESUME_MAX_RATIO and row + 1 < len(episodes):
            row += 1
        return episodes[row]

    def _season_by_id(self, season_id: str) -> Optional[dict]:
        season = self._screen.series.current_season()
        if season is not None and str(season.get("id")) == season_id:
            return season
        return None

    def _on_episodes_ready(self, series_id: str, season_id: str) -> None:
        season = self._season_by_id(season_id)
        if self._series is not None and self._is_current_series(series_id) and season:
            self._show_episodes(season, self._catalog.episodes(self._series, season))

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
            own = poster_url(self._series)
            poster = self._poster(self._series, own, "series")
            self._screen.series.set_series(self._series, details, poster if poster != own else "")
            self._catalog.request_seasons(self._series)
        else:
            self._screen.series.set_episodes(season, None)
            self._catalog.request_episodes(self._series, season)

    def _activate_episode(self, season: dict, episode: dict, start_over: bool = False) -> None:
        series = self._series
        episodes = self._catalog.episodes(series, season) if series is not None else None
        context = _context(start_over=start_over, series=series, season=season, episodes=episodes)
        provider = self._series_owner()
        self._request(provider, lambda: self._play(episode, "episode", context))

    def _episode_progress(self, episode: dict) -> Optional[float]:
        """Resume progress, or the watched share (≥ 90%) that shows the check mark."""
        cid = WatchState.content_id(self._series_owner(), episode, item_type="episode")
        saved = (self._config.playback_positions or {}).get(cid) or {}
        position, duration = int(saved.get("position") or 0), int(saved.get("duration") or 0)
        if position <= 0 or duration <= 0:
            return None
        share = min(1.0, position / duration)
        if share >= RESUME_MAX_RATIO or self._watch.resume_position(cid) is not None:
            return share
        return None

    def _leave_series(self) -> None:
        back = self._series_back
        self._series = None
        self._series_details = None
        self._history_episode = None
        self._set_mode(back)
        has_titles = self._screen.grid.current_index() >= 0
        self._focus("grid" if has_titles else ("search" if back == "search" else "genres"))

    # ---- search -------------------------------------------------------------------------------

    def _show_hits(self) -> None:
        if self._grid_content != "search":
            return
        grid = self._screen.grid
        grid.set_sections(self._search.sections())
        grid.set_message(self._search.message())
        self._screen.search_field.set_count_text(self._search.count_text())
