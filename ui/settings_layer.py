"""Settings in the drawer, wired into the TV's controller (moved out of ui/tv_controller.py).

While Settings shows it takes the channel list's keys (those it does not use reach nothing,
never the list under it), the mouse's Back and Forward, and a click on the picture closes it.
Opening it makes way: what would keep the keys hidden under it closes, each as it closes by
itself; the welcome card waits under it (ProviderFlow). Backed out of from the channel list,
the list comes back where it was. The channel list or the welcome card opened while it shows
(asked for: a menu, a click) shows on top and takes the keys: Settings goes.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject

from services.navigation import SETTINGS_ROW
from ui.keymap import Action
from ui.settings_controller import SettingsController

# The channel list's own keys (the drawer's context): while Settings shows, those it does not
# use reach nothing, never the list under it (its find, Delete's Remove from Recents).
_LIST_KEYS = frozenset(
    {
        Action.NAV_UP,
        Action.NAV_DOWN,
        Action.NAV_PAGE_UP,
        Action.NAV_PAGE_DOWN,
        Action.NAV_LEFT,
        Action.NAV_RIGHT,
        Action.NAV_ACTIVATE,
        Action.NAV_TEXT,
        Action.NAV_BACKSPACE,
        Action.NAV_HOME,
        Action.NAV_END,
        Action.NAV_DELETE,
        Action.FORGET,
        Action.SELECT_ALL,
        Action.COPY,
        Action.CUT,
        Action.PASTE,
    }
)


class SettingsLayer(QObject):
    """The TV controller's side of Settings in the drawer: what gives way to it, what it gives
    back, and its keys before the channel list's. `overlays` is the controller's (the channel
    list, the welcome card, the phone remote's pairing screen); `flow` its ProviderFlow;
    `guide` its GuideController and `quality` its ⚙ quality menu (None: none); the callables
    are late-bound, so that what the controller changes later is what they reach."""

    def __init__(
        self,
        *,
        overlays: Any,
        nav: Any,
        catalog: Any,
        flow: Any,
        guide: Any,
        quality: Any,
        closed: Callable[[], bool],
        close_online: Callable[[], None],
        update_guides: Callable[[], None],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._overlays = overlays
        self._nav = nav
        self._catalog = catalog
        self._flow = flow
        self._guide = guide
        self._quality = quality
        self._closed = closed
        self._close_online = close_online
        self._update_guides = update_guides
        # Settings in the drawer (the window's, set_settings): None until it is wired.
        self._settings: Optional[SettingsController] = None
        # Where the list was when its ⚙ opened Settings (its keys' side and highlighted row,
        # Drawer.resume_point): it comes back there once Settings is backed out of.
        self._place: Optional[tuple[str, Any]] = None

    def set_settings(self, settings: SettingsController) -> None:
        """Settings in the drawer: while it shows it takes the drawer's keys, the mouse's Back
        and Forward, and a click on the picture closes it."""
        self._settings = settings
        settings.hidden.connect(self._on_settings_hidden)
        # The channel list or the welcome card opened while it shows (asked for: a menu, a
        # click; what opens them by itself waits, ProviderFlow) raises itself over it and
        # takes the keys: Settings goes (eventFilter).
        self._overlays.drawer.installEventFilter(self)
        self._overlays.welcome.installEventFilter(self)

    def shown(self) -> bool:
        return self._settings is not None and self._settings.visible()

    def phone_field(self) -> Any:
        """2.15: the text box Settings' keys type into, if any (a phone's typing sheet)."""
        return self._settings.phone_field() if self.shown() and self._settings else None

    def handle(self, action: Action, payload: object = "") -> bool:
        """A key while Settings shows: True when it is Settings' or one of the channel list's
        keys (those reach nothing under it); False for one that goes on (the sound's)."""
        settings = self._settings
        if settings is None or not settings.visible():
            return False
        return settings.handle(action, payload) or action in _LIST_KEYS

    def make_way_for_settings(self, keep_place: bool = False) -> None:
        """Settings is opening over the player: what would keep the keys hidden under it
        closes, each as it closes by itself (the channel list, the guide, the OpenSubtitles
        search, the ⚙ quality menu, the phone remote's pairing screen), so the keys always go
        to what shows. The welcome card gives way until Settings goes (ProviderFlow, its
        owner). A question (stop a recording? convert? download?) keeps the keys, and stays
        above Settings. `keep_place` (the list's ⚙): where the list is, its path and its keys'
        side and row, is kept for when Settings is backed out of (reopen_drawer_on_settings);
        closing it leaves the path as it is."""
        if self._closed():
            return
        overlays = self._overlays
        self._place = None
        if overlays.drawer.isVisible():
            if keep_place:
                self._place = overlays.drawer.resume_point()
            overlays.drawer.close_drawer()
        if self._guide.is_open():
            self._guide.close()
        self._close_online()
        if self._quality is not None and self._quality.visible():
            self._quality.close()
        pairing = overlays.pairing
        if pairing is not None and pairing.isVisible():
            pairing.dismiss()
        self._flow.welcome_give_way()

    def close_settings(self) -> None:
        """Hide Settings, the list not given back (a click on the picture, the Library, the
        phone's TV button)."""
        self._place = None
        if self._settings is not None and self._settings.visible():
            self._settings.close()

    def click_elsewhere(self) -> None:
        """A click on the picture: a text being edited is kept (it is a click elsewhere), and
        Settings closes as the list does: it does not come back."""
        if self._settings is not None:
            self._settings.keep_edit()
        self.close_settings()

    def reopen_drawer_on_settings(self) -> None:
        """Backed out of Settings opened from the drawer: the list again. From its ⚙, where
        it was (the path closing left, the keys' side and row: make_way_for_settings); from
        its Settings row, on the sources with Settings highlighted (Enter opens it again).
        Without a provider, the welcome card, as the list asked for shows it."""
        if self._closed():
            return
        place, self._place = self._place, None
        if not self._catalog.provider_names():
            self._flow.welcome(chosen=True)
            return
        drawer = self._overlays.drawer
        if place is not None:
            side, key = place
            drawer.open_drawer(focus_key=key, focus_side=side)
        else:
            self._nav.focus_source(self._catalog.current_provider_name())
            drawer.open_drawer(focus_key=SETTINGS_ROW, focus_side="left")
        self._update_guides()  # the drawer is visible now: its group's providers are in use

    def _on_settings_hidden(self) -> None:
        """Settings went: what waited for it (the welcome card, a list due) as it is now."""
        if not self._closed():
            self._flow.settings_closed()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        overlays = self._overlays
        if watched is overlays.drawer or watched is overlays.welcome:
            if event.type() == QEvent.Type.Show and self.shown():
                self.close_settings()
        return False
