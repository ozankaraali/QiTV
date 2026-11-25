"""The TV window's menu bar, the update check and Report an Issue.

Single-key entries only show their key (a hint, active inside the open menu): the window's
keyPressEvent handles those keys through ui.keymap. Modifier combinations are application
shortcuts, which the keymap leaves to the menus (a test checks that no screen claims them).
The player's keys are disabled while the Library shows. The Providers menu is an exclusive
group of the configured providers, rebuilt when a provider becomes ready or fails and after
a provider changes in Settings, then Add provider and the free-channels and free-radio (2.13)
submenus: All countries, the system's region, and Another country…, which opens their
countries in Settings (2.13.1; the welcome card that offers them shows only without providers,
and the last one stays). Settings… opens Settings in the drawer, as the drawer's Settings row
and ⚙ do (backing out of it from there gives the drawer back). The drawer's Add provider row
opens the same Add card in Settings as Add provider, its Free channels and Free radio rows
their countries there, its Open file row the same file dialog, and its header's ⌕ the same
search. File → Open Recordings Folder opens where recordings go (2.2), as Settings' Open
folder does; View → Recordings (2.9.7) the Library's Recordings page, as the drawer's
Recordings row. About QiTV and Help → Keyboard Shortcuts… open Settings at About QiTV and at
Keys (2.13.1: no windows of their own).
"""

from __future__ import annotations

from functools import partial
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import QMenu

from services import free_radio, log_file, recording, system_open
from services.exporter import ExportKind
from services.i18n import N_, tr, translate
from services.navigation import (
    ADD_PROVIDER_ROW,
    FREE_CHANNELS_ROW,
    FREE_RADIO_ROW,
    OPEN_FILE_ROW,
    OPEN_FOLDER_ROW,
    OPEN_LINK_ROW,
    RECORDINGS_ROW,
    SETTINGS_ROW,
)
from services.provider_ids import ALL_PROVIDERS, ALL_PROVIDERS_TITLE
from services.settings_model import ABOUT, KEYS
from ui.free_channels_menu import (
    OTHER_TEXT as OTHER_COUNTRY_TEXT,
    PICKER_TITLE as FREE_CHANNELS_TITLE,
    RADIO_TITLE as FREE_RADIO_TITLE,
    quick_choices,
)
from ui.keymap import Action
from ui.overlays.drawer import LIBRARY_ACTION, REFRESH_ACTION, SEARCH_ACTION, SETTINGS_BUTTON
from ui.settings_about import ISSUES_URL
from ui.update_dialogs import check_for_updates

logger = logging.getLogger(__name__)

NO_PROVIDERS_TEXT = N_("No providers")  # i18n: the Providers menu's only entry, when it is empty
ADD_PROVIDER_TEXT = N_("Add provider")  # i18n: the Providers menu's entry; opens the provider form
_SHORTCUT_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.MetaModifier
)


def open_folder(path: str) -> bool:
    """`path` in the system's file manager; False when it could not be opened (a folder
    that does not exist yet). Nothing is read here: the system opens it."""
    return system_open.open_folder(path)


class TVMenus(QObject):
    """Builds and runs the window's menus. `window` is the TVWindow (see TVWindow.__init__)."""

    def __init__(self, window, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._window = window
        self._player_actions: list[QAction] = []  # entries of the player's keys
        self._provider_group: Optional[QActionGroup] = None
        self._free_menu: Optional[QMenu] = None  # the Providers menu's Free channels
        self._radio_menu: Optional[QMenu] = None  # and its Free radio (2.13)
        self.providers_menu: QMenu
        self.keep_on_top: QAction  # View → Keep on Top, checkable (2.8.5)
        # A mouse button was let go on an open menu: the entry it triggers now was clicked
        # (until the event is handled), not chosen by its shortcut or the menu's keys.
        self._clicked = False

    # ---- building -----------------------------------------------------------------------------

    def build(self) -> None:
        window, screens = self._window, self._window.screens
        bar, add, role = window.menuBar(), self._add_action, QAction.MenuRole
        app_menu = bar.addMenu("QiTV")  # the app's name (macOS's application menu)
        add(app_menu, tr("About QiTV"), "", self.show_about, role.AboutRole)
        add(
            app_menu,
            tr("Check for Updates…"),  # i18n: a menu entry
            "",
            self.check_updates,
            role.ApplicationSpecificRole,
        )
        # i18n: a menu entry (opens Settings)
        add(app_menu, tr("Settings…"), "Ctrl+,", window.open_settings, role.PreferencesRole)
        # i18n: a menu entry: the phone remote's pairing code (turning the remote on if it's off)
        add(
            app_menu,
            tr("Phone Remote…"),
            "",
            window.open_phone_remote,
            role.ApplicationSpecificRole,  # a Mac's app menu, beside Settings
        )
        add(app_menu, tr("Quit"), "Ctrl+Q", window.quit_chosen, role.QuitRole)  # i18n: a menu entry
        file_menu = bar.addMenu(tr("File"))  # i18n: the menu bar's File menu
        add(file_menu, tr("Open File…"), "Ctrl+O", screens.open_file)
        # i18n: a menu entry (plan 4): a stream's link plays, a provider's goes to Add provider
        add(file_menu, tr("Open Link…"), "Ctrl+N", window.open_link)
        # i18n: a menu entry (Plan 4): a folder chosen plays, and goes on the Library's Folders tab
        add(file_menu, tr("Open Folder…"), "Ctrl+Shift+O", screens.open_folder)
        export = file_menu.addMenu(tr("Export"))  # i18n: a submenu of File (verb)
        for text, kind in (
            (tr("Cached Content…"), ExportKind.CACHED),  # i18n: File → Export's entries
            (tr("All Live Channels…"), ExportKind.ALL_LIVE),
            (tr("Complete Series (Fetch All)…"), ExportKind.COMPLETE_SERIES),
        ):
            add(export, text, "", partial(screens.export, kind))
        # i18n: a menu entry: the folder recordings go to (Settings → Time-shift)
        add(file_menu, tr("Open Recordings Folder"), "", self.open_recordings)
        view = bar.addMenu(tr("View"))  # i18n: the menu bar's View menu
        add(view, tr("Live TV"), "Ctrl+1", screens.back_to_tv)
        add(view, tr("Movies"), "Ctrl+2", lambda: screens.open_library("movies"))
        add(view, tr("Series"), "Ctrl+3", lambda: screens.open_library("series"))
        # The multiview's grid over live TV (V on the player), or back to one picture.
        add(view, tr("Multiview"), "Ctrl+4", screens.toggle_multiview)
        add(view, tr("Restore Last Multiview"), "", screens.restore_multiview)
        # i18n: a View menu entry (2.9.7): the Library's Recordings page
        add(view, tr("Recordings"), "Ctrl+5", screens.open_recordings)
        add(view, tr("Search"), "Ctrl+F", screens.open_search)  # i18n: a menu entry (verb)
        add(view, tr("Refresh Channels and Guide"), "Ctrl+R", self.refresh_content)
        # i18n: a menu entry: the Library's films and series load again (2.8)
        add(view, tr("Refresh Films and Series"), "Ctrl+Shift+R", self.refresh_library)
        player = self._player_actions  # the player's keys: off while the Library shows
        player.append(add(view, tr("Guide"), "G", self._handler(Action.GUIDE)))
        # i18n: a View menu entry: opens the channel list
        player.append(add(view, tr("Channel List"), "Return", self._handler(Action.OPEN_DRAWER)))
        add(view, tr("Fullscreen"), "F", window.toggle_fullscreen)
        # i18n: a View menu entry: the picture in a small window of its own
        player.append(add(view, tr("Mini Player"), "Alt+P", self._handler(Action.MINI_PLAYER)))
        # i18n: a View menu entry, checked while the window stays above other apps' windows
        self.keep_on_top = add(view, tr("Keep on Top"), "", self._toggle_keep_on_top)
        self.keep_on_top.setCheckable(True)
        self.keep_on_top.setChecked(bool(getattr(window.config_manager, "keep_on_top", False)))
        playback = bar.addMenu(tr("Playback"))  # i18n: the menu bar's Playback menu
        # i18n: a Playback menu entry: plays, or pauses what plays
        player.append(add(playback, tr("Play/Pause"), "Space", self._handler(Action.PLAY_PAUSE)))
        # i18n: a menu entry (2.6): every picture stops, nothing streams (verb)
        player.append(add(playback, tr("Stop"), "X", self._handler(Action.STOP)))
        # i18n: a menu entry (verb)
        player.append(add(playback, tr("Mute"), "M", self._handler(Action.MUTE)))
        player.append(
            add(playback, tr("Previous Channel"), "Backspace", self._handler(Action.PREVIOUS))
        )
        # i18n: a Playback menu entry: a time-shifted channel back to live (verb)
        player.append(add(playback, tr("Go Live"), "L", self._handler(Action.GO_LIVE)))
        # i18n: a menu entry: record the channel being watched, or stop recording (verb)
        player.append(add(playback, tr("Record"), "R", self._handler(Action.RECORD)))
        self.providers_menu = bar.addMenu(tr("Providers"))  # i18n: the menu bar's Providers menu
        self.rebuild_providers()
        help_menu = bar.addMenu(tr("Help"))  # i18n: the menu bar's Help menu
        add(help_menu, tr("Keyboard Shortcuts…"), "", self.show_shortcuts)
        add(help_menu, tr("Report an Issue…"), "", self.report_issue)
        # i18n: a Help menu entry: shows QiTV's log file in Finder or Explorer (2.8.2)
        add(help_menu, tr("Show the Log File…"), "", self.show_log)
        catalog = window.catalog
        # Bound slots: Qt lets go of them with this object (a lambda would outlive it).
        catalog.providerReady.connect(self._on_providers_changed)
        catalog.providerFailed.connect(self._on_providers_changed)

    def _add_action(
        self,
        menu: QMenu,
        text: str,
        shortcut: str,
        slot: Callable[[], Any],
        role: QAction.MenuRole = QAction.MenuRole.NoRole,
    ) -> QAction:
        window = self._window
        action = QAction(text, window)
        action.setMenuRole(role)
        action.triggered.connect(lambda _checked=False: self._run(slot))
        menu.addAction(action)
        menu.installEventFilter(self)  # its clicks (once per menu: Qt keeps no duplicates)
        if not shortcut:
            return action
        sequence = QKeySequence(shortcut)
        action.setShortcut(sequence)
        first = sequence[0]  # type: ignore[index]  # QKeyCombination; missing from the stubs
        if first.keyboardModifiers() & _SHORTCUT_MODIFIERS:
            action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            window.addAction(action)  # stays active while a non-native menu bar is hidden
        else:
            # A hint only: active solely inside the open menu, so the key reaches
            # keyPressEvent (the drawer types "f", "m" and " ", and Return activates).
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        return action

    def _run(self, slot: Callable[[], Any]) -> None:
        """A menu entry or its shortcut. A page a shortcut (or the menu's keys) shows gets no
        second click (TVPages.from_keys); a click on the entry is a click: on Windows and
        Linux the menu drops down over the page, and a double-click's second click would land
        on the page it shows (macOS's menu bar is the system's: no click reaches the menus)."""
        if self._clicked:
            slot()
            return
        with self._window.screens.from_keys():
            slot()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseButtonRelease and isinstance(obj, QMenu):
            # The menu triggers the entry under the pointer while it handles this release.
            self._clicked = True
            QTimer.singleShot(0, self._click_handled)
        return False

    def _click_handled(self) -> None:
        self._clicked = False

    def _handler(self, action: Action) -> Callable[[], None]:
        return lambda: self._window.controller.handle(action)

    def _toggle_keep_on_top(self) -> None:
        self._window.set_keep_on_top(self.keep_on_top.isChecked())

    def open_recordings(self) -> None:
        """The Recordings folder in the system's file manager; a toast when there is none."""
        window = self._window
        if not open_folder(recording.folder(window.config_manager)):
            window.overlays.toast.show_message(translate(recording.NO_RECORDINGS))

    def set_player_enabled(self, enabled: bool) -> None:
        """The player's keys in the menus: off while the Library shows."""
        for action in self._player_actions:
            action.setEnabled(enabled)

    # ---- providers ----------------------------------------------------------------------------

    def _on_providers_changed(self, *_args: object) -> None:
        self.rebuild_providers()

    def rebuild_providers(self) -> None:
        """One checkable entry per provider, after "All providers" when there are two or more
        (as in the channel list's sources), the source the TV page browses checked; then Add
        provider, Free channels and Free radio."""
        menu = self.providers_menu
        menu.clear()
        catalog = self._window.catalog
        names = catalog.provider_names()
        if not names:
            menu.addAction(translate(NO_PROVIDERS_TEXT)).setEnabled(False)
            self._add_entries(menu)
            return
        current = catalog.source()
        entries = [(name, name) for name in names]
        if len(names) >= 2:
            entries.insert(0, (ALL_PROVIDERS, translate(ALL_PROVIDERS_TITLE)))
        if self._provider_group is not None:
            self._provider_group.deleteLater()
        self._provider_group = group = QActionGroup(self)
        group.setExclusive(True)
        for source, title in entries:
            action = QAction(title, menu)
            action.setCheckable(True)
            action.setChecked(source == current)
            action.triggered.connect(lambda _checked=False, s=source: self._choose_provider(s))
            group.addAction(action)
            menu.addAction(action)
        self._add_entries(menu)

    def _add_entries(self, menu: QMenu) -> None:
        """Add provider and the free-channels and free-radio submenus, under the providers."""
        window = self._window
        menu.addSeparator()
        add = menu.addAction(translate(ADD_PROVIDER_TEXT))
        add.triggered.connect(lambda _checked=False: window.add_provider())
        for submenu in (self._free_menu, self._radio_menu):
            if submenu is not None:
                submenu.deleteLater()  # clear() leaves a submenu it held
        self._free_menu = self._free_lists_menu(
            menu,
            FREE_CHANNELS_TITLE,
            lambda code: window.add_free_channels(code),
            lambda: window.show_free_channels(),
        )
        self._radio_menu = self._free_lists_menu(
            menu,
            FREE_RADIO_TITLE,
            lambda code: window.add_free_radio(code),
            lambda: window.show_free_radio(),
            free_radio.COUNTRY_CODES,
            translate(free_radio.HELP_TEXT),
        )

    @staticmethod
    def _free_lists_menu(
        menu: QMenu,
        title: str,
        add: Callable[[Optional[str]], None],
        other: Callable[[], None],
        codes: Optional[tuple[str, ...]] = None,
        tooltip: str = "",
    ) -> QMenu:
        """A submenu (the menu bar's own look): All countries and the system's region add
        their list at once (`add`), Another country… opens the countries (`other`)."""
        submenu = menu.addMenu(translate(title))
        for text, code in quick_choices(codes):
            submenu.addAction(text).triggered.connect(lambda _checked=False, c=code: add(c))
        submenu.addAction(translate(OTHER_COUNTRY_TEXT)).triggered.connect(
            lambda _checked=False: other()
        )
        if tooltip:  # where the lists come from
            submenu.setToolTipsVisible(True)
            for action in submenu.actions():
                action.setToolTip(tooltip)
        return submenu

    def _choose_provider(self, source: str) -> None:
        """The exclusive group moved the check; providerReady rebuilds the menu (never here:
        that would delete the action whose signal runs). `source` is a provider's name or
        ALL_PROVIDERS."""
        window = self._window
        if not window.closing() and source != window.catalog.source():
            window.controller.select_provider(source)

    # ---- About, Keys and links ----------------------------------------------------------------

    def show_about(self) -> None:
        """Settings at About QiTV (2.13.1: a section of Settings, not a window)."""
        if not self._window.closing():
            self._window.open_settings(section_key=ABOUT)

    def check_updates(self) -> None:
        if not self._window.closing():
            check_for_updates(self._window.config_manager, manual=True)

    def show_shortcuts(self) -> None:
        """Settings at Keys: each screen's keys (2.13.1: a section of Settings, not a window)."""
        if not self._window.closing():
            self._window.open_settings(section_key=KEYS)

    def report_issue(self) -> None:
        system_open.open_url(ISSUES_URL)

    def show_log(self) -> None:
        """The folder of QiTV's log file (qitv.log), in the system's file manager (2.8.2)."""
        open_folder(str(log_file.log_dir()))

    def on_drawer_action(self, row: str) -> None:
        """The drawer's Settings, Add provider, Free channels, Free radio, Open file, Open a link,
        Open a folder and Recordings rows, and its header's ⚙, Library, ⌕ and refresh buttons.
        Backed out of, Settings gives the list back: on its Settings row from there, where it
        was from ⚙, from Add provider (Settings at Providers, its Add card over it) and from
        Free channels and Free radio (their countries in Settings)."""
        if row == SETTINGS_ROW:
            self._window.open_settings(from_drawer=True)
        elif row == SETTINGS_BUTTON:
            self._window.open_settings(from_drawer=True, keep_place=True)
        elif row == ADD_PROVIDER_ROW:
            self._window.add_provider(from_drawer=True)
        elif row == FREE_CHANNELS_ROW:  # Settings › Providers › Free channels (2.13.1)
            self._window.show_free_channels(from_drawer=True)
        elif row == FREE_RADIO_ROW:  # 2.13
            self._window.show_free_radio(from_drawer=True)
        elif row == OPEN_FILE_ROW:
            self._window.screens.open_file()
        elif row == OPEN_LINK_ROW:  # plan 4: File → Open Link…
            self._window.open_link()
        elif row == OPEN_FOLDER_ROW:  # Plan 4: File → Open Folder… (P4-25)
            self._window.screens.open_folder()
        elif row == RECORDINGS_ROW:  # 2.9.7
            self._window.screens.open_recordings()
        elif row == SEARCH_ACTION:
            self._window.screens.open_search()
        elif row == LIBRARY_ACTION:  # 2.8: the section opened last, Movies the first time
            self._window.screens.open_last_library()
        elif row == REFRESH_ACTION:
            self.refresh_content()

    def refresh_library(self) -> None:
        """The Library's films and series load again (2.8), as its ↻."""
        self._window.screens.library_controller.refresh_lists()

    def refresh_content(self) -> None:
        """Load the provider's channels and guide again (the drawer's ↻, Ctrl/⌘+R).

        The forced provider setup loads the guide with force once it is ready.
        """
        self._window.set_provider(force_update=True)
        self._window.screens.notify(tr("Refreshing channels and guide…"))
