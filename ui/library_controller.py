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
TV page's source and never stops what plays. 2.9.3: with two providers or more, Movies or Series
asked of a source without them (an M3U playlist) shows the provider that has them (several: All
providers) instead of "… has no movies", and the tabs show while any provider has them; with one
provider it is as before.

Typing (2.9.3; the owner: "i kinda default to genre search instead of library search"): letters
typed anywhere in the Library (the grid, the top bar, a series, the genres' rows) go to the top
bar's search, which shows what they find; only once the genre filter is chosen (a click on it,
or ↑ from the first genre: it shows its ring and caret then) do they filter the genres. The A–Z
rail jumps to a letter.

Recordings (2.9.7): the top bar's last tab (once the shell gives it its page) opens the
Recordings page (`ui.library.recordings_view`, filled by `ui.recordings_desk`): the keys go to
it (zone "recordings"), ↑ from its chips to the top bar, Esc from its chips back to live TV.

A right-click on a film's poster or an episode row asks the shell for its menu (Play in VLC,
Play in MPV, Copy stream link: `context_menu(item, section, global position)`, section "movies"
or "series"). Links are asked of the provider QiTV is on, so a title of another provider, or
any title during a provider switch, has no menu; nor has a series or a channel.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtWidgets import QMenu

from services import i18n, recording_desk, recordings_library as rl
from services.catalog import PROVIDER_LOADING_MESSAGE
from services.failure_reasons import SIGN_IN_AGAIN_MESSAGE, shown_message
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
from services.media_server import server_length
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

# i18n: a Library genre's menu (2.9.1): out of the lists, no PIN; Settings → Providers shows it
TIDY_GENRE_TEXT = N_("Hide genre")
from ui.keymap import Action
from ui.library.library_screen import LibraryScreen
from ui.library.poster_grid import poster_url
from ui.library.search_session import DEBOUNCE_MS, SearchSession
from ui.library.top_nav import REFRESH_TEXT
from ui.text_edit import edit_key
from ui.tick_menu import TickMenu

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


# i18n: under "{source} has no movies" in the Library, beside an Add provider button
WHERE_FILMS_TEXT = N_("Films and series come from Xtream, STB, Jellyfin and Emby providers.")
ADD_PROVIDER_TEXT = N_("Add provider")


SEARCH_DEBOUNCE_MS = DEBOUNCE_MS
# What the grid or a toast shows: shown with translate().
LOADING_TEXT = N_("Loading…")
EMPTY_TEXT = N_("Nothing here")  # i18n: a Library genre without titles
RETRY_TEXT = N_("Couldn't load — click or press Enter to retry")
SERIES_FAILED_NOTICE = N_("Couldn't load that series")
UNKNOWN_PROVIDER_NOTICE = N_("That title's provider is no longer in your list")
REFRESHING_TEXT = N_("Refreshing films and series…")  # i18n: a toast (2.8: the Library's ↻)
_MOVES = {
    Action.NAV_LEFT: (-1, 0),
    Action.NAV_RIGHT: (1, 0),
    Action.NAV_UP: (0, -1),
    Action.NAV_DOWN: (0, 1),
}


# 2.9.7: the 2.9.7 page's sections (the REC sign's Manage Recordings…) as the rail's rows
_REC_ROWS = {
    recording_desk.NOW: rl.COMING,
    recording_desk.COMING: rl.COMING,
    recording_desk.ATTENTION: rl.ATTENTION,
    recording_desk.PLACES: rl.SETTINGS,
    recording_desk.RULES: rl.RULES,
    recording_desk.RECORDED: rl.ALL,  # a tile's right-click Manage Recordings…
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
    addProviderRequested = Signal()  # the Add provider under "… has no movies" (or Enter)
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
        cancel_scheduled: Optional[Callable[[str], None]] = None,
        parent: Optional[QObject] = None,
    ):
        """`tmdb` (2.8, a ui.tmdb_fill.TmdbFill) fills titles' gaps from TMDB; None: never.
        `cancel_scheduled(id)` cancels a scheduled recording (its strip's Cancel, 2.8.12)."""
        super().__init__(parent)
        self._cancel_scheduled = cancel_scheduled
        self._desk: Optional[Any] = None  # 2.9.7: the Recordings page's (`set_recordings_desk`)
        # 2.9.7: the Recordings tab's rail and tiles (`set_recordings_library`), its row shown,
        # and what its strip's Delete… and Show in Folder do (the pages')
        self._rec_library: Optional[Any] = None
        self._rec_row = rl.TONIGHT
        self.delete_recording: Callable[[dict], None] = lambda item: None
        self.show_recording_folder: Callable[[dict], object] = lambda item: None
        self._search_from = ""  # 2.9.7: "recordings" while the search was opened from that page
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
        self._tidy_genre: Optional[Callable[[str, str, str, str], None]] = None
        self._may_hide: Callable[[], bool] = lambda: False
        self._closed = False
        self._mode = "browse"
        self._zone = "grid"
        self._genre_field = False  # 2.9.3: the keys type in the genre filter (it was chosen)
        self._rec_field = False  # 2.9.7: the keys type in the Recordings rail's find
        self._nav_return = "grid"  # the zone ↓ returns to from the top nav
        self._section: Optional[str] = None  # the Movies/Series section last opened
        self._opened: Optional[tuple[str, str]] = None  # (provider, section) the model shows
        # the grid
        self._grid_content = ""  # what the grid holds: "browse", "search" or nothing yet
        self._grid_signature: Optional[tuple] = None  # (provider, section, genre, sort) shown
        self._failed = False
        self._failure = ""  # what the list that failed said (its catalog message)
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
            # The results of the section and the provider shown first.
            where=lambda: (self._section, str(self._catalog.current_provider_name())),
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
        grid.cancelRequested.connect(self._cancel_index)
        grid.sortRequested.connect(lambda value: model.set_sort(Sort(value)))
        grid.leftEdge.connect(self._on_left_edge)
        grid.upEdge.connect(self._on_up_edge)
        grid.contextRequested.connect(self._on_grid_context)
        grid.messageClicked.connect(self._on_grid_message_clicked)
        grid.messageAction.connect(self.addProviderRequested)
        series.seasonSelected.connect(self._load_episodes)
        series.episodeActivated.connect(self._activate_episode)
        series.startOverRequested.connect(
            lambda season, episode: self._activate_episode(season, episode, start_over=True)
        )
        series.retryRequested.connect(self._retry_series)
        series.backRequested.connect(self._leave_series)
        series.episodeContextRequested.connect(self._on_episode_context)
        nav.tabRequested.connect(self._on_tab)
        screen.recordings.upEdge.connect(lambda: self._focus("nav"))  # 2.9.7
        rec_rail, rec_grid = screen.rec_rail, screen.rec_grid  # 2.9.7: the Recordings tab
        rec_rail.genreSelected.connect(self._show_rec_row)
        rec_rail.fieldClicked.connect(lambda: self._focus("rec_rail", field=True))
        rec_rail.genreActivated.connect(lambda _id: self._focus("rec_content"))
        rec_rail.filterChanged.connect(lambda _text: self._refresh_recordings())
        rec_grid.activated.connect(lambda i: self._play_recording(i, start_over=False))
        rec_grid.startOverRequested.connect(lambda i: self._play_recording(i, start_over=True))
        rec_grid.deleteRequested.connect(lambda i: self._recording_action(i, self.delete_recording))
        rec_grid.folderRequested.connect(
            lambda i: self._recording_action(i, self.show_recording_folder)
        )
        rec_grid.leftEdge.connect(lambda: self._focus("rec_rail"))
        rec_grid.upEdge.connect(lambda: self._focus("nav"))
        rec_grid.contextRequested.connect(self._on_recording_context)
        home = screen.home  # 2.9.7: the Recordings tab's home
        home.upEdge.connect(lambda: self._focus("nav"))
        home.leftEdge.connect(lambda: self._focus("rec_rail"))
        home.allRequested.connect(self._open_all_recordings)
        home.playRequested.connect(
            lambda item: self._play(item, "recording", _context(start_over=False))
        )
        nav.searchRequested.connect(self.open_search)
        nav.sourceRequested.connect(self._show_source_menu)
        genres.genreMenuRequested.connect(self._show_genre_menu)
        nav.refreshRequested.connect(self.refresh_lists)
        screen.search_field.clicked.connect(lambda: self._focus("search"))
        screen.search_field.clearRequested.connect(self._clear_or_leave_search)  # 2.9.3: its ✕
        genres.fieldClicked.connect(lambda: self._focus("genres", field=True))  # 2.9.3
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
        if self._desk is not None:
            self._desk.hide()

    # ---- 2.9.7: the Recordings page ----------------------------------------------------------

    def set_recordings_desk(self, desk: Any) -> None:
        """The Recordings page's controller (`ui.recordings_desk`): its tab shows from now on."""
        self._desk = desk
        self._screen.nav.set_tabs(self._tabs())

    def set_recordings_library(self, library: Any) -> None:
        """2.9.7: the Recordings tab's rail and tiles (services.recordings_library)."""
        self._rec_library = library

    def open_recordings(self, row: Optional[str] = None) -> None:
        """The Recordings tab on its rail's `row` (its home, Tonight, by default). A section
        of the 2.9.7 page names its row ("now" and "coming": Coming up; "attention";
        "places": Folders & streams), as the REC sign's Manage Recordings… does."""
        if self._closed or self._desk is None:
            return
        self._series = None
        self._set_mode("recordings")
        self._sync_nav("recordings")
        self._desk.show()
        self._show_rec_row(_REC_ROWS.get(row or "", row or rl.TONIGHT))
        self._focus("rec_content")

    def recordings_changed(self) -> None:
        """2.9.7: the recordings or the schedule changed: the rail's counts and the tiles."""
        if self._mode == "recordings":
            self._refresh_recordings()

    def _refresh_recordings(self) -> None:
        library = self._rec_library
        if library is None:
            return
        rail = self._screen.rec_rail
        rows = library.rail(rail.filter_text)
        rail.set_genres(rows)
        if self._rec_row.startswith(rl.CHANNEL_PREFIX) and self._rec_row not in {
            g.id for g in rows
        }:
            self._show_rec_row(rl.ALL)  # its last recording went: All
            return
        rail.set_current(self._rec_row)
        if self._screen.recordings_content == "grid":
            self._fill_rec_grid()

    def _show_rec_row(self, row: str, *, rebuild: bool = True) -> None:
        """The view of the rail's `row`: the home, the tiles of All or a channel, or a list;
        `rebuild`: the rail's rows worked out again (not for a plain move along them)."""
        library = self._rec_library
        self._rec_row = row
        screen = self._screen
        if library is not None and rebuild:
            rail = screen.rec_rail
            rail.set_genres(library.rail(rail.filter_text))
            rail.set_current(row)
        if row == rl.TONIGHT:
            screen.set_recordings_content("home")
        elif row == rl.ALL or row.startswith(rl.CHANNEL_PREFIX):
            screen.set_recordings_content("grid")
            self._fill_rec_grid()
        else:
            screen.set_recordings_content("list")
            screen.recordings.set_section(rl.SECTIONS.get(row, recording_desk.COMING))

    def _open_all_recordings(self) -> None:
        self._show_rec_row(rl.ALL)
        self._focus("rec_content")

    def _fill_rec_grid(self) -> None:
        library = self._rec_library
        if library is None:
            return
        grid = self._screen.rec_grid
        grid.set_details_provider(library.details)
        grid.set_progress_provider(library.progress)
        grid.set_key_provider(library.identity)
        grid.set_badge_provider(library.badges)
        grid.set_poster_url_provider(lambda item: "")  # its title's card (no art of its own)
        items = library.items(self._rec_row, self._screen.rec_rail.filter_text)
        if self._rec_row.startswith(rl.CHANNEL_PREFIX):  # its channel, without asking the rail
            title = self._rec_row[len(rl.CHANNEL_PREFIX) :]
        else:
            title = translate(recording_desk.ALL_TEXT)
        grid.set_header(title, i18n.number(len(items)), [], "")
        grid.set_sections([("", items)], keep_current=True)

    def _rec_item(self, index: int) -> Optional[dict]:
        item = self._screen.rec_grid.item(index)
        return item if isinstance(item, dict) else None

    def _play_recording(self, index: int, *, start_over: bool) -> None:
        item = self._rec_item(index)
        if item is not None and not self._closed:
            self._play(item, "recording", _context(start_over=start_over))

    def _recording_action(self, index: int, action: Callable[[dict], object]) -> None:
        item = self._rec_item(index)
        if item is not None and not self._closed:
            action(item)

    def _on_recording_context(self, index: int, pos: Any) -> None:
        item = self._rec_item(index)
        if item is not None and self._context_menu is not None:
            self._context_menu(item, "recording", pos)

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

    def _focus(self, zone: str, *, field: bool = False) -> None:
        """The keys to `zone`; with `field`, the genres' filter (2.9.3: chosen, it alone
        takes typing, and shows its ring and caret)."""
        if zone == "nav" and self._zone != "nav":
            self._nav_return = self._zone
        self._zone = zone
        self._genre_field = field and zone == "genres"
        self._rec_field = field and zone == "rec_rail"  # 2.9.7: the Recordings rail's find
        self._screen.genres.set_focused(zone == "genres", field=self._genre_field)
        self._screen.nav.set_focused(zone == "nav")
        self._screen.grid.set_focused(zone == "grid")
        self._screen.recordings.set_focused(zone == "rec_content")
        self._screen.rec_rail.set_focused(zone == "rec_rail", field=self._rec_field)  # 2.9.7
        self._screen.rec_grid.set_focused(zone == "rec_content")
        self._screen.home.set_focused(zone == "rec_content")
        self._sync_search_focus()

    def _sync_search_focus(self) -> None:
        """The search field's ring: the keys type in it, or the top bar's focus is on it."""
        on_it = self._zone == "nav" and self._screen.nav.focus_item() == "search"
        self._screen.search_field.set_focused(
            self._zone == "search" or on_it, caret=self._zone == "search"
        )

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        self._screen.set_mode(mode)
        if mode != "recordings" and self._desk is not None:
            self._desk.hide()  # 2.9.7: the page stops looking while it is hidden

    def _tabs(self) -> list[str]:
        """The provider's sections (2.9.7: recordings have their own tab, not Movies);
        2.9.3: with two providers or more, Movies and Series while any of them has them (the
        tab shows the one that has: `_source_for`)."""
        tabs = list(self._catalog.sections())
        for section in ("movies", "series"):
            if section not in tabs and self._others_with(section):
                tabs.append(section)
        if self._desk is not None:
            tabs.append("recordings")  # 2.9.7: whatever the providers have
        return tabs

    def _others_with(self, section: str) -> list[str]:
        """The providers that have `section` (in Settings order), when there are two
        providers or more (one provider: none, as in 2.0)."""
        names = self._catalog.provider_names()
        sections_of = getattr(self._catalog, "sections_of", None)
        if len(names) < 2 or sections_of is None:
            return []
        return [name for name in names if section in sections_of(name)]

    def _source_for(self, section: str) -> None:
        """2.9.3: Movies or Series asked of a source without them (an M3U playlist): the
        provider that has them is shown instead, or All providers when several have them, not
        "… has no movies" (with one provider, that, as before)."""
        if section not in ("movies", "series") or section in self._catalog.sections():
            return
        having = self._others_with(section)
        if len(having) == 1:
            self._choose_source(having[0])
        elif having:
            self._mix_sources([])  # every provider: those without it add nothing

    def _sync_nav(self, active: str) -> None:
        nav = self._screen.nav
        nav.set_source_visible(active != "recordings")  # 2.9.7: it covers every provider
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
        """Show Movies or Series. Re-opening the section on screen keeps its genre and title.
        2.9.7: "recordings" is the Recordings page."""
        if self._closed:
            return
        if section == "recordings":
            self.open_recordings()
            return
        self._source_for(section)
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

    def close_search(self) -> None:
        """The phone remote's search is done: the search it showed here closes as Esc closes an
        empty one (back to the section it came from, or to the TV); any other view stays."""
        if self._closed or self._mode != "search":
            return
        self._search.clear()
        self._screen.search_field.set_query("")
        self._leave_search()

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
        if self._mode != "search":  # 2.9.7: Esc from the search goes back to that page
            self._search_from = "recordings" if self._mode == "recordings" else ""
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
            if payload and (self._zone == "search" or self._genre_field):
                return self.handle(Action.NAV_TEXT, payload)  # "/" types where text goes
            self.open_search()
            return True
        handler = {
            "nav": self._handle_nav,
            "genres": self._handle_genres,
            "grid": self._handle_grid,
            "series": self._handle_series,
            "search": self._handle_search,
            "rec_rail": self._handle_rec_rail,  # 2.9.7
            "rec_content": self._handle_rec_content,
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
            elif grid.message_button().isVisible():
                self.addProviderRequested.emit()  # "… has no movies": its Add provider
            else:
                grid.activate()
        elif action is Action.FAVORITE:
            if grid.current_index() >= 0:
                self._toggle_favorite(grid.current_index())
        elif action is Action.ESCAPE:
            self._focus("search" if self._mode == "search" else "genres")
        elif action in (Action.NAV_TEXT, Action.NAV_BACKSPACE, Action.PASTE):
            return self._type_in_search(action, payload)  # 2.9.3: never a hidden letter jump
        else:
            return False
        return True

    def _type_in_search(self, action: Action, payload: str) -> bool:
        """2.9.3: typing where no field is chosen: the top bar's search takes it (it opens
        with what was typed). Backspace with nothing typed yet does nothing."""
        if action is Action.NAV_BACKSPACE and self._mode != "search":
            return True
        if self._mode != "search":
            self.open_search()
        self._focus("search")
        return self._handle_search(action, payload)

    def _handle_genres(self, action: Action, payload: str) -> bool:
        genres = self._screen.genres
        if self._genre_field:
            if genres.field_key(action, payload):
                return True  # 2.9.3: the filter's caret, selection and clipboard
            if action in (Action.NAV_DOWN, Action.NAV_ACTIVATE):
                self._focus("genres")  # its rows, the filter kept
                return True
            if action is Action.NAV_UP:
                self._focus("nav")
                return True
        elif action in (Action.NAV_TEXT, Action.NAV_BACKSPACE, Action.PASTE):
            return self._type_in_search(action, payload)  # 2.9.3: the filter wasn't chosen
        if action in (Action.NAV_UP, Action.NAV_DOWN):
            before = genres.current_id()
            genres.move_by(-1 if action is Action.NAV_UP else 1)
            if action is Action.NAV_UP and genres.current_id() == before:
                self._focus("genres", field=True)  # ↑ on the first genre: its filter
        elif action in (Action.NAV_PAGE_UP, Action.NAV_PAGE_DOWN):
            genres.page(-1 if action is Action.NAV_PAGE_UP else 1)
        elif action in (Action.NAV_RIGHT, Action.NAV_ACTIVATE):
            if genres.current_id() is None:
                self._focus("grid")
            else:
                genres.activate()  # genreActivated focuses the grid
        elif action is Action.ESCAPE:
            if not genres.escape():
                self._go_back_to_tv()
        else:
            return False
        return True

    def _handle_nav(self, action: Action, payload: str) -> bool:
        nav = self._screen.nav
        if action in (Action.NAV_TEXT, Action.PASTE):  # 2.9.3: typing in the top bar searches
            self.open_search()
            return self._handle_search(action, payload)
        if action in (Action.NAV_LEFT, Action.NAV_RIGHT):
            nav.move_by(-1 if action is Action.NAV_LEFT else 1)
            self._sync_search_focus()
        elif action is Action.NAV_ACTIVATE:
            nav.activate()
        elif action is Action.NAV_DOWN:
            back = self._nav_return
            if back == "genres" and self._mode != "browse":
                back = "grid"
            if self._mode == "recordings":  # 2.9.7: its content (its rail if it was there)
                back = "rec_rail" if back == "rec_rail" else "rec_content"
            elif back in ("rec_rail", "rec_content"):
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
        elif action in (Action.NAV_TEXT, Action.PASTE):
            return self._type_in_search(action, payload)  # 2.9.3: typing searches
        else:
            return False
        return True

    def _handle_rec_rail(self, action: Action, payload: str) -> bool:
        """2.9.7: the Recordings tab's rail, as the genre column: ↑↓ choose a row (its view
        shows), → or Enter go to it, Esc back to the TV."""
        rail = self._screen.rec_rail
        if self._rec_field:
            if rail.field_key(action, payload):
                return True  # its caret, selection and clipboard
            if action in (Action.NAV_DOWN, Action.NAV_ACTIVATE):
                self._focus("rec_rail")  # its rows, the find kept
                return True
            if action is Action.NAV_UP:
                self._focus("nav")
                return True
        if action is Action.NAV_UP and rail.current_id() == rl.TONIGHT:
            self._focus("nav")
        elif action in (Action.NAV_UP, Action.NAV_DOWN):
            rail.move_by(-1 if action is Action.NAV_UP else 1)
            if rail.current_id():
                self._show_rec_row(str(rail.current_id()), rebuild=False)
        elif action in (Action.NAV_PAGE_UP, Action.NAV_PAGE_DOWN):
            rail.page(-1 if action is Action.NAV_PAGE_UP else 1)
        elif action in (Action.NAV_RIGHT, Action.NAV_ACTIVATE):
            self._focus("rec_content")
        elif action is Action.ESCAPE:
            if not rail.escape():
                self._go_back_to_tv()
        elif action in (Action.NAV_TEXT, Action.NAV_BACKSPACE, Action.PASTE):
            return self._type_in_search(action, payload)
        else:
            return False
        return True

    def _handle_rec_content(self, action: Action, payload: str) -> bool:
        """2.9.7: the Recordings tab's view: its tiles as the Library's grid, a list as the
        2.9.7 page; Esc goes to the rail."""
        kind = self._screen.recordings_content
        if kind == "grid":
            if action is Action.ESCAPE:
                self._focus("rec_rail")
                return True
            grid = self._screen.rec_grid
            if action in _MOVES:
                if grid.current_index() < 0:
                    if action is Action.NAV_LEFT:
                        self._focus("rec_rail")
                    elif action is Action.NAV_UP:
                        self._focus("nav")
                    return True
                grid.move_by(*_MOVES[action])
            elif action in (Action.NAV_PAGE_UP, Action.NAV_PAGE_DOWN):
                grid.page(-1 if action is Action.NAV_PAGE_UP else 1)
            elif action is Action.NAV_HOME:
                grid.home()
            elif action is Action.NAV_END:
                grid.end()
            elif action is Action.NAV_ACTIVATE:
                grid.activate()
            elif action is Action.NAV_DELETE:  # its strip's Delete… (asks first)
                self._recording_action(grid.current_index(), self.delete_recording)
            elif action in (Action.NAV_TEXT, Action.NAV_BACKSPACE, Action.PASTE):
                return self._type_in_search(action, payload)
            else:
                return False
            return True
        if kind == "home":
            home = self._screen.home
            if action in _MOVES:
                home.move_by(*_MOVES[action])  # its edges: upEdge, leftEdge
            elif action is Action.NAV_ACTIVATE:
                home.activate()
            elif action is Action.RECORD:
                return home.record_key()
            elif action is Action.ESCAPE:
                if not home.escape():
                    self._focus("rec_rail")
            elif action in (Action.NAV_TEXT, Action.NAV_BACKSPACE, Action.PASTE):
                return self._type_in_search(action, payload)
            else:
                return False
            return True
        return self._handle_recordings(action, payload)

    def _handle_recordings(self, action: Action, payload: str) -> bool:
        """2.9.7: a list of the Recordings tab: its entries' buttons; Esc goes to the rail."""
        page = self._screen.recordings
        if action is Action.NAV_LEFT and page.at_left_edge():
            self._focus("rec_rail")
            return True
        if action in _MOVES:
            page.move_by(*_MOVES[action])
        elif action in (Action.NAV_PAGE_UP, Action.NAV_PAGE_DOWN):
            page.page(-1 if action is Action.NAV_PAGE_UP else 1)
        elif action is Action.NAV_HOME:
            page.home()
        elif action is Action.NAV_END:
            page.end()
        elif action is Action.NAV_ACTIVATE:
            page.activate()
        elif action is Action.ESCAPE:
            if not page.escape():
                self._focus("rec_rail")
        elif action in (Action.NAV_TEXT, Action.NAV_BACKSPACE, Action.PASTE):
            return self._type_in_search(action, payload)  # typing searches, as elsewhere
        else:
            return False
        return True

    def _handle_search(self, action: Action, payload: str) -> bool:
        search, field = self._search, self._screen.search_field
        changed = edit_key(field.edit, action, payload, arrows=True)  # 2.9.3: a caret
        if changed is not None:
            if changed:
                search.set_query(field.edit.text)
                field.set_query(search.query)
            field.update()
            return True
        if action in (Action.NAV_TEXT, Action.NAV_BACKSPACE):
            pass  # nothing to type or to take out
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
            self._clear_or_leave_search()
        else:
            return False
        return True

    def _clear_or_leave_search(self) -> None:
        """Esc in the search, and its ✕ (2.9.3): what was typed goes; with nothing typed,
        the search closes, back to where it came from."""
        if self._closed:
            return
        search, field = self._search, self._screen.search_field
        if search.query:
            search.clear()
            field.set_query("")
            self._focus("search")
        else:
            self._leave_search()

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
        if self._search_from == "recordings" and self._desk is not None:
            self._search_from = ""
            self.open_recordings()  # 2.9.7: the page the search came from
        elif self._section is None:
            self._go_back_to_tv()
        else:
            self.open(self._section)

    def _show_source_menu(self, pos: QPoint) -> None:
        menu = self._source_menu()
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(pos)

    def _source_menu(self) -> QMenu:
        """A tick for each provider the Library shows (2.8: several mix), and All providers,
        which ticks every one or, with every one ticked, none (2.8.1: to pick one). The menu
        stays open while ticks change; what is ticked shows once it closes (nothing ticked:
        what showed stays)."""
        menu = TickMenu(self._screen)
        names = self._catalog.provider_names()
        shown = self._shown_providers()
        ticks = []
        for name in names:
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name in shown)
            ticks.append(action)
        if len(names) > 1:  # with one provider there is nothing to mix
            menu.addSeparator()
            every = menu.addAction(translate(ALL_PROVIDERS_TITLE))
            every.setCheckable(True)

            def tick_all(checked: bool) -> None:
                for action in ticks:
                    action.setChecked(checked)

            def follow() -> None:
                every.setChecked(all(action.isChecked() for action in ticks))

            follow()
            every.triggered.connect(tick_all)
            for action in ticks:
                action.triggered.connect(follow)

        def apply() -> None:
            chosen = [action.text() for action in ticks if action.isChecked()]
            if chosen and set(chosen) != set(shown):
                self._mix_sources([] if len(chosen) == len(names) else chosen)

        menu.aboutToHide.connect(apply)
        menu.addSeparator()
        refresh = menu.addAction(translate(REFRESH_TEXT))
        refresh.triggered.connect(lambda _checked=False: self.refresh_lists())
        return menu

    def set_hiding(
        self,
        hide: Callable[[str, str, str, str], None],
        may_hide: Callable[[], bool],
        tidy: Optional[Callable[[str, str, str, str], None]] = None,
    ) -> None:
        """Parental controls (2.8): `hide(section, provider, genre id, title)` hides a genre,
        offered on its right-click while `may_hide()` (a PIN is set); `tidy(…)` takes it out
        of the lists without a PIN (2.9.1, services.hidden_groups), offered always."""
        self._hide_genre, self._may_hide = hide, may_hide
        self._tidy_genre = tidy

    def _show_genre_menu(self, gid: str, pos: QPoint) -> None:
        menu = self.genre_menu(gid)
        if menu is not None:
            menu.aboutToHide.connect(menu.deleteLater)
            menu.popup(pos)

    def genre_menu(self, gid: str) -> Optional[QMenu]:
        """A genre's right-click menu: Hide (parental controls); None for a pinned row (My
        list, Continue …), a genre not shown, or without a PIN."""
        hide = self._hide_genre if self._may_hide() else None
        tidy = self._tidy_genre
        if (hide is None and tidy is None) or self._closed:
            return None
        genre = next((g for g in self._model.genres() if g.id == gid and not g.header), None)
        if genre is None or genre.pinned:
            return None
        name, own = split_group_id(gid)
        provider = name or self._catalog.current_provider_name()
        section = self._model.section
        menu = QMenu(self._screen)
        if tidy is not None:
            menu.addAction(translate(TIDY_GENRE_TEXT)).triggered.connect(
                lambda _=False: tidy(section, provider, own or gid, genre.title)
            )
        if hide is not None:
            menu.addAction(translate(HIDE_TEXT)).triggered.connect(
                lambda _=False: hide(section, provider, own or gid, genre.title)
            )
        return menu

    def refresh_lists(self) -> None:
        """The films and series of the providers shown load again (2.8: ↻, the source menu,
        Ctrl/⌘+Shift+R): an Xtream panel's and a server's behind the saved lists, which show
        meanwhile (a server's asks only what changed); a portal's genres anew (a provider
        setting up first finishes). Says so when any loads."""
        if self._closed:
            return
        if self._catalog.refresh_sections():
            self.notice.emit(translate(REFRESHING_TEXT))

    def _shown_providers(self) -> list[str]:
        """The providers the Library shows: those it mixes, else its own."""
        if self._catalog.all_providers():
            return list(self._catalog.mixed_names())
        return [self._catalog.current_provider_name()]

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
            text = _no_section_text(self._source_title(), model.section)
            if model.section in ("movies", "series"):  # where they come from, and the way
                text += "\n" + translate(WHERE_FILMS_TEXT)
                grid.set_message(text, translate(ADD_PROVIDER_TEXT))
            else:
                grid.set_message(text)
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
                grid.set_message(self._failed_text() if self._failed else translate(LOADING_TEXT))
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
        self._failed, self._failure = True, message
        if self._grid_content == "browse":
            grid = self._screen.grid
            grid.set_sections([])
            grid.set_message(self._failed_text())

    def _failed_text(self) -> str:
        """Why the list is not shown: a media server's ask to sign in again as it says it (2.8:
        where to sign in), else "Couldn't load — press Enter to retry"."""
        if self._failure == SIGN_IN_AGAIN_MESSAGE:
            return shown_message(self._failure)
        return translate(RETRY_TEXT)

    def _on_grid_message_clicked(self) -> None:
        """A click on "Couldn't load" retries, as Enter does."""
        if self._failed and self._grid_content == "browse":
            self._retry()

    def _retry(self) -> None:
        self._failed = False
        if self._model.genre_id is None:
            self._model.set_section(self._model.section)
        else:
            self._model.select_genre(self._model.genre_id)

    def _on_filter_changed(self, text: str) -> None:
        genres = self._screen.genres
        genres.set_genres(self._model.genres(text) if self._section_available() else [])
        genres.update()  # (no match: its line says so)

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

    def _cancel_index(self, index: int) -> None:
        """A scheduled recording's Cancel on the strip (2.8.12): as its right-click's."""
        if self._closed or self._grid_content == "search":
            return
        item = self._screen.grid.item(index)
        if item is None or self._model.item_kind(item) != "scheduled":
            return
        if self._cancel_scheduled is not None:
            self._cancel_scheduled(str(item.get("_item_id") or ""))

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
        active = {"search": "search", "recordings": "recordings"}.get(self._mode)
        self._sync_nav(active or self._model.section)  # 2.9.7: Recordings stays as it is
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
        """Resume progress, or the watched share (≥ 90%) that shows the check mark; without
        QiTV's own, where its media server says it stopped (2.8)."""
        cid = WatchState.content_id(self._series_owner(), episode, item_type="episode")
        saved = (self._config.playback_positions or {}).get(cid) or {}
        position, duration = int(saved.get("position") or 0), int(saved.get("duration") or 0)
        if position > 0 and duration > 0:
            share = min(1.0, position / duration)
            if share >= RESUME_MAX_RATIO or self._watch.resume_position(cid) is not None:
                return share
        resume, length = self._watch.resume_from(cid, episode), server_length(episode)
        return min(1.0, resume / length) if resume and length > 0 else None

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
        # 2.9.3: the field is in the top bar; the grid's header says what it found, as a
        # genre's says its titles.
        query = self._search.query.strip()
        grid.set_header(f"“{query}”" if query else "", self._search.count_text(), [], "")
