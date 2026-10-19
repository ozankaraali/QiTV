"""The TV window's two pages, the player and the Library (spec §5.4, R5), and the moves between.

Opening the Library (View → Movies, Series or Search, Ctrl/⌘+F, Movies or Series chosen in
the drawer) shows the Library page; what plays goes on in a corner of it (the PageStack's
corner mode: ⤢, a click on it, Esc at the Library's top level or ‹ bring the player back,
× stops it), and with nothing playing the player page is hidden. A title played from the Library
switches to the player; Esc on it, with nothing open on the player, or "‹ Library" on the player
controls shows the Library again as it was (its genre, title and scroll, or the series view on
its season and episode). Live TV (the menu, the Library's tab, Esc at its top
level) tunes the last channel again. The Library's LibraryController is made here, after the
TVController was wired (see TVWindow.__init__). Messages show on both pages' toasts: the
overlay host, and its toast, hide with the player page.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from typing import Iterator, Optional

from PySide6.QtCore import QObject, QPoint
from PySide6.QtWidgets import QFileDialog, QWidget

from services.exporter import ExportKind
from services.library import LibraryModel
from ui.keymap import Action
from ui.library_controller import LibraryController
from ui.page_stack import PageStack
from ui.pointer import SecondClickGuard
from ui.tv_controller import ERROR_MS, INFO_MS, scrub

logger = logging.getLogger(__name__)

MEDIA_FILTER = (
    "Media files (*.mp4 *.m4v *.mkv *.avi *.mov *.webm *.ts *.m2ts *.mts *.mpg *.mpeg *.flv"
    " *.wmv *.ogv *.mp3 *.m4a *.aac *.flac *.ogg *.opus *.wav *.m3u8);;All files (*)"
)


class TVPages(QObject):
    """`window` is the TVWindow: its pages, controller, exports, menus and window state."""

    def __init__(self, window, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._window = window
        self._return_to_library = False  # the movie playing came from the Library
        # After ‹ Library, ⌕ and the Movies/Series rows: see _guard_second_click.
        self._second_click = SecondClickGuard(self)
        self._keyed = False  # a key or a menu item moves the pages: see from_keys
        self.library_model = LibraryModel(
            window.library_catalog, window.config_manager, window.watch, self
        )
        self.library_controller = LibraryController(
            config=window.config_manager,
            catalog=window.library_catalog,
            model=self.library_model,
            watch=window.watch,
            screen=window.library_page,
            provider_manager=window.provider_manager,
            jobs=window.jobs,
            play=self.play,
            tune_live=self.tune_live,
            back_to_tv=self.back_to_tv,
            context_menu=self.context_menu,
            parent=self,
        )
        self.library_controller.notice.connect(self.notify)
        window.library_catalog.providerFailed.connect(self._on_provider_failed)
        window.overlays.drawer.libraryRequested.connect(self.open_library)

    # ---- pages --------------------------------------------------------------------------------

    def library_shown(self) -> bool:
        """The Library is the page in front (also while the player sits in its corner)."""
        return self._window.pages.currentWidget() is self._window.library_page

    def _show_page(self, page: QWidget, *, corner: bool = False) -> None:
        """Show the player page, or the Library with the player in its corner or hidden."""
        window = self._window
        stack = window.pages
        library = page is window.library_page
        wanted = (
            (PageStack.CORNER if corner else PageStack.LIBRARY) if library else PageStack.PLAYER
        )
        if stack.mode() == wanted:
            return
        if self.library_shown() and not library:
            self.library_controller.leave()  # a play waiting for its provider is dropped
        self._second_click.disarm()  # it kept a click off the page shown before
        if library:
            stack.show_library(corner=corner)
        else:
            if stack.mode() == PageStack.CORNER:
                window.controller.leave_library_playing()
            stack.show_player()
        window.menus.set_player_enabled(not library)

    def _guard_second_click(self) -> None:
        """A click just showed another page: a double-click's second click would land on
        whatever that page shows under the pointer, so for the double-click interval it reaches
        nothing (SecondClickGuard). A key or a menu item makes no double-click (from_keys):
        the next click is the user's own and reaches the page."""
        if not self._keyed:
            self._second_click.arm()

    @contextmanager
    def from_keys(self) -> Iterator[None]:
        """The page moves made inside come from a key or a menu item (TVWindow's keys, the
        menus' entries and their shortcuts): no second click follows them."""
        keyed, self._keyed = self._keyed, True
        try:
            yield
        finally:
            self._keyed = keyed

    def shutdown(self) -> None:
        """The window closes: the guard, which filters the whole application, lets go now
        (another window's next click is not this one's second click)."""
        self._second_click.disarm()

    def back_to_player(self) -> None:
        """⤢, a click on the corner player: the player page again, what plays as it was.

        A double-click's second click would land on the player page under the pointer (a
        double-click there toggles fullscreen): it reaches nothing (_guard_second_click).
        """
        if not self._window.closing() and self._window.pages.mode() == PageStack.CORNER:
            self._show_page(self._window.player_page)
            self._guard_second_click()

    def stop_corner(self) -> None:
        """× on the corner player: what plays stops (today's stop for the Library) and the
        Library stays, full size. A double-click's second click would land on the Library
        under the pointer (a press on a poster or ▶ plays it): it reaches nothing."""
        window = self._window
        if window.closing() or window.pages.mode() != PageStack.CORNER:
            return
        self._set_return_to_library(False)
        window.controller.stop_for_library()
        self._show_page(window.library_page)
        self._guard_second_click()

    def open_library(self, section: str) -> None:
        """Movies or Series: what plays goes on in a corner of the Library (nothing playing: the
        Library has the whole window).

        From the channel list's Movies and Series rows the Library shows at once, so a
        double-click's second click reaches nothing (_guard_second_click); after a key or
        the menu the next click reaches the Library.
        """
        if self._window.closing():
            return
        self._enter_library()
        self.library_controller.open(section)
        self._guard_second_click()

    def open_search(self, query: str = "") -> None:
        """The quick search: from ⌕ (the player controls, the guide, the channel list) a
        double-click's second click reaches nothing, as in open_library. `query` is what the
        channel list's find had typed (its films-and-series row)."""
        if self._window.closing():
            return
        self._enter_library()
        self.library_controller.open_search(query)
        self._guard_second_click()

    def _enter_library(self) -> None:
        window = self._window
        window.leave_mini_player()  # the Library is a full window: never inside the corner
        if window.controller.playing():
            window.controller.enter_library_playing()  # it plays on, in a corner of the Library
            self._show_page(window.library_page, corner=True)
            return
        self._set_return_to_library(False)
        window.controller.stop_for_library()
        self._show_page(window.library_page)

    def _set_return_to_library(self, value: bool) -> None:
        """Whether the title playing came from the Library: Esc and ‹ Library go back to it."""
        self._return_to_library = value
        self._window.overlays.transport.set_library_available(value)

    def play(self, item: dict, kind: str, context: dict) -> None:
        """A movie or an episode from the Library: Esc brings the Library back."""
        window = self._window
        if window.closing():
            return
        from_library = self.library_shown()  # the page switch ends the controller's Library state
        self._show_page(window.player_page)
        self._set_return_to_library(True)
        window.controller.play_vod(item, kind, context, from_library=from_library)

    def tune_live(self, channel: dict) -> None:
        """A live channel from the Library's search."""
        window = self._window
        if window.closing():
            return
        self._show_page(window.player_page)
        self._set_return_to_library(False)
        window.controller.tune_live(channel)

    def back_to_tv(self) -> None:
        """Live TV: the last channel again (R5), or the drawer."""
        window = self._window
        if window.closing():
            return
        if window.pages.mode() == PageStack.CORNER:
            self.back_to_player()  # the live channel plays on: Esc and ‹ bring it back
            return
        self._show_page(window.player_page)
        self._set_return_to_library(False)
        window.controller.resume_live()

    def toggle_multiview(self) -> None:
        """View → Multiview: the grid over live TV (from the Library, live TV first: what
        plays in its corner, or the last channel), or back to one picture."""
        window = self._window
        if window.closing():
            return
        if self.library_shown():
            self.back_to_tv()
        window.controller.handle(Action.MULTIVIEW)

    def escape_to_library(self) -> bool:
        """Esc on a movie from the Library, with nothing open on the player: back to it.

        Also while its link is on its way (it is dropped) or after it failed: never a black
        page without a way back. Not once a live channel was chosen since, not in fullscreen
        (Esc leaves it first) nor in the mini player. The Library keeps its genre, title and
        scroll: it is shown again, not opened again.
        """
        window = self._window
        if (
            not self._can_return()
            or window.mini_player_active()
            or window.fullscreen_heading()
            or window.controller.overlay_open()
        ):
            return False
        self._enter_library()
        return True

    def back_to_library(self) -> None:
        """The player controls' "‹ Library": what Esc does after a movie from the Library.

        Esc first closes what is open on the player and leaves fullscreen; a click on the
        button goes back at once, also from fullscreen or the mini player (which the Library
        leaves). A double-click's second click would land on the Library under the pointer
        (a click on an episode plays it): it reaches nothing.
        """
        if not self._window.closing() and self._can_return():
            self._enter_library()
            self._guard_second_click()

    def _can_return(self) -> bool:
        """The title playing, on its way or failed came from the Library, and no live channel
        was chosen since."""
        return (
            self._return_to_library
            and not self.library_shown()
            and self._window.controller.vod_chosen()
        )

    def context_menu(self, item: dict, section: str, global_pos: QPoint) -> None:
        """A right-click on a Library film or episode: Play in VLC/MPV or Copy stream link."""
        window = self._window
        if not window.closing():
            window.controller.show_library_menu(item, section, global_pos, window.library_page)

    def open_file(self) -> None:
        """File → Open File…: a local media file plays as a movie."""
        window = self._window
        if window.closing():
            return
        path, _filter = QFileDialog.getOpenFileName(window, "Open File", "", MEDIA_FILTER)
        if not path or window.closing():
            return
        self._show_page(window.player_page)
        self._set_return_to_library(False)
        window.controller.play_file(path)

    def export(self, kind: ExportKind) -> None:
        window = self._window
        if window.closing():
            return
        shown = self.library_shown()
        section = self.library_controller.section if shown else None
        catalog = window.library_catalog if shown else window.catalog
        window.exports.export(kind, section or "live", catalog)

    # ---- messages -----------------------------------------------------------------------------

    def notify(self, text: str, kind: str = "info", ms: int = INFO_MS) -> None:
        """A message on whichever page shows (each page has its toast)."""
        window = self._window
        for toast in (window.overlays.toast, window.library_toast):
            toast.show_message(text, kind, ms)

    def _on_provider_failed(self, name: str, message: str) -> None:
        if self.library_shown():  # the player's toast says it too, but its page is hidden
            self._window.library_toast.show_message(
                scrub(f"{name}: {message}" if name else message), "error", ERROR_MS
            )
