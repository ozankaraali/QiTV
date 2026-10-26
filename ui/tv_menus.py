"""The TV window's menu bar, About, the update check, Keyboard Shortcuts and Report an Issue.

Single-key entries only show their key (a hint, active inside the open menu): the window's
keyPressEvent handles those keys through ui.keymap. Modifier combinations are application
shortcuts, which the keymap leaves to the menus (a test checks that no screen claims them).
The player's keys are disabled while the Library shows. The Providers menu is an exclusive
group of the configured providers, rebuilt when a provider becomes ready or fails and after
Settings closes, then Add provider and the free-channels menu (the welcome card that offers
both shows only without providers, and the last one stays). The drawer's Settings and Add
provider rows open the same dialogs, its Free channels row the free-channels menu, its Open
file row the same file dialog, and its header's ⌕ the same search. File → Open Recordings
Folder opens where recordings go (2.2), as Settings' Open folder does.
"""

from __future__ import annotations

from functools import partial
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QMenu, QVBoxLayout, QWidget

from services import recording
from services.config_manager import get_app_version
from services.exporter import ExportKind
from services.i18n import N_, tr, translate
from services.navigation import ADD_PROVIDER_ROW, FREE_CHANNELS_ROW, OPEN_FILE_ROW, SETTINGS_ROW
from services.provider_ids import ALL_PROVIDERS, ALL_PROVIDERS_TITLE
from ui.brand import logo_pixmap, scaled_asset
from ui.free_channels_menu import (
    PICKER_TITLE as FREE_CHANNELS_TITLE,
    build_menu as build_free_channels_menu,
)
from ui.keymap import Action
from ui.overlays.drawer import LIBRARY_ACTION, REFRESH_ACTION, SEARCH_ACTION
from ui.shortcuts_dialog import ShortcutsDialog
from ui.update_dialogs import check_for_updates

logger = logging.getLogger(__name__)

HOME_URL = "https://github.com/ozankaraali/QiTV"
ISSUES_URL = f"{HOME_URL}/issues"
ABOUT_LOGO_PX = 64
ABOUT_TEXT_MIN_W = 340  # wide enough for the links on one line each
NOTICES_URL = f"{HOME_URL}/blob/main/THIRD-PARTY-NOTICES.md"
ABOUT_TEXT = (
    "<p><b>QiTV {version}</b></p>"
    "<p>{tagline}</p>"
    '<p><a href="{home}">{home}</a><br><a href="{issues}">{report}</a></p>'
    '<p><small>{credits} <a href="{notices}">{notices_label}</a></small></p>'
    "<p><small>{tmdb}</small></p>"
)
CREDITS_TEXT = N_(
    # i18n: About QiTV's credits; the names stay as they are
    "Plays and casts with VLC, built with Qt and Python. Subtitles come from OpenSubtitles,"
    " free channels from iptv-org, and film and series details from TMDB. Works with Jellyfin"
    " and Emby servers and with Chromecast; Chromecast is a trademark of Google LLC, and QiTV"
    " is not affiliated with Jellyfin, Emby or Google."
)
NOTICES_LABEL = N_("Credits and licenses")  # i18n: a link in About QiTV, after its credits
# 2.8: TMDB's terms ask for this notice (their template, with the product's name) and their
# logo (assets/tmdb.png) in About, the logo less prominent than QiTV's own; they set no size.
# The notice is in the credits' small text, the logo on its own line under it, flush left.
# i18n: TMDB's terms ask for this sentence: translate it faithfully
TMDB_NOTICE = N_(
    "QiTV uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by"
    " TMDB."
)
TMDB_LOGO_FILE = "tmdb.png"
ABOUT_TMDB_LOGO_PX = 9
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
    return QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def about_text() -> str:
    """About's text: the version (pyproject.toml, as packaged), the project's links, and what
    QiTV uses, with every credit and license a link away."""
    return ABOUT_TEXT.format(
        version=get_app_version(),
        tagline=tr("A TV-first IPTV player for STB portals, Xtream and M3U playlists."),
        home=HOME_URL,
        issues=ISSUES_URL,
        report=tr("Report an issue"),  # i18n: a link in About QiTV, to the project's issues
        credits=translate(CREDITS_TEXT),
        notices=NOTICES_URL,
        notices_label=translate(NOTICES_LABEL),
        tmdb=translate(TMDB_NOTICE),
    )


class AboutDialog(QDialog):
    """About QiTV: the logo (the same cropped one as the Library's top bar and the welcome
    card, at this window's pixel ratio) above the name, the version and the links."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("About QiTV"))
        self.logo = QLabel(self)
        ratio = (parent if parent is not None else self).devicePixelRatioF()
        pixmap = logo_pixmap(ABOUT_LOGO_PX, ratio)
        if pixmap is not None:
            self.logo.setPixmap(pixmap)
        else:
            self.logo.hide()  # the asset is missing: the name says it
        self.text = QLabel(about_text(), self)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.text.setOpenExternalLinks(True)
        self.text.setMinimumWidth(ABOUT_TEXT_MIN_W)
        self.text.setWordWrap(True)
        # TMDB's logo under its notice (the text's last line), flush left.
        self.tmdb_logo = QLabel(self)
        tmdb_logo = scaled_asset(TMDB_LOGO_FILE, ABOUT_TMDB_LOGO_PX, ratio)
        if tmdb_logo is not None:
            self.tmdb_logo.setPixmap(tmdb_logo)
        else:
            self.tmdb_logo.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, self)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(self.logo)
        layout.addWidget(self.text)
        layout.addWidget(self.tmdb_logo, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(buttons)


class TVMenus(QObject):
    """Builds and runs the window's menus. `window` is the TVWindow (see TVWindow.__init__)."""

    def __init__(self, window, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._window = window
        self._player_actions: list[QAction] = []  # entries of the player's keys
        self._provider_group: Optional[QActionGroup] = None
        self._free_menu: Optional[QMenu] = None  # the Providers menu's Free channels
        self.providers_menu: QMenu
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
        # i18n: a menu entry (opens the Settings window)
        add(app_menu, tr("Settings…"), "Ctrl+,", window.options_dialog, role.PreferencesRole)
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
        add(view, tr("Search"), "Ctrl+F", screens.open_search)  # i18n: a menu entry (verb)
        add(view, tr("Refresh Channels and Guide"), "Ctrl+R", self.refresh_content)
        # i18n: a menu entry: the Library's films and series load again (2.8)
        add(view, tr("Refresh Films and Series"), "Ctrl+Shift+R", self.refresh_library)
        player = self._player_actions  # the player's keys: off while the Library shows
        player.append(add(view, tr("Guide"), "G", self._handler(Action.GUIDE)))
        player.append(add(view, tr("Channel List"), "Return", self._handler(Action.OPEN_DRAWER)))
        add(view, tr("Fullscreen"), "F", window.toggle_fullscreen)
        player.append(add(view, tr("Mini Player"), "Alt+P", self._handler(Action.MINI_PLAYER)))
        playback = bar.addMenu(tr("Playback"))  # i18n: the menu bar's Playback menu
        player.append(add(playback, tr("Play/Pause"), "Space", self._handler(Action.PLAY_PAUSE)))
        # i18n: a menu entry (2.6): every picture stops, nothing streams (verb)
        player.append(add(playback, tr("Stop"), "X", self._handler(Action.STOP)))
        # i18n: a menu entry (verb)
        player.append(add(playback, tr("Mute"), "M", self._handler(Action.MUTE)))
        player.append(
            add(playback, tr("Previous Channel"), "Backspace", self._handler(Action.PREVIOUS))
        )
        player.append(add(playback, tr("Go Live"), "L", self._handler(Action.GO_LIVE)))
        # i18n: a menu entry: record the channel being watched, or stop recording (verb)
        player.append(add(playback, tr("Record"), "R", self._handler(Action.RECORD)))
        self.providers_menu = bar.addMenu(tr("Providers"))  # i18n: the menu bar's Providers menu
        self.rebuild_providers()
        help_menu = bar.addMenu(tr("Help"))  # i18n: the menu bar's Help menu
        add(help_menu, tr("Keyboard Shortcuts…"), "", self.show_shortcuts)
        add(help_menu, tr("Report an Issue…"), "", self.report_issue)
        catalog = window.catalog
        catalog.providerReady.connect(lambda _name: self.rebuild_providers())
        catalog.providerFailed.connect(lambda _name, _message: self.rebuild_providers())

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

    def rebuild_providers(self) -> None:
        """One checkable entry per provider, after "All providers" when there are two or more
        (as in the channel list's sources), the source the TV page browses checked; then Add
        provider and Free channels."""
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
        """Add provider and the free-channels menu, under the providers."""
        window = self._window
        menu.addSeparator()
        add = menu.addAction(translate(ADD_PROVIDER_TEXT))
        add.triggered.connect(lambda _checked=False: window.add_provider())
        if self._free_menu is not None:
            self._free_menu.deleteLater()  # clear() leaves a submenu it held
        free = self._free_menu = build_free_channels_menu(
            menu, lambda code: window.add_free_channels(code), styled=False
        )
        free.setTitle(translate(FREE_CHANNELS_TITLE))
        menu.addMenu(free)

    def _choose_provider(self, source: str) -> None:
        """The exclusive group moved the check; providerReady rebuilds the menu (never here:
        that would delete the action whose signal runs). `source` is a provider's name or
        ALL_PROVIDERS."""
        window = self._window
        if not window.closing() and source != window.catalog.source():
            window.controller.select_provider(source)

    # ---- dialogs and links --------------------------------------------------------------------

    def show_about(self) -> None:
        window = self._window
        if not window.closing():
            AboutDialog(window).exec()

    def check_updates(self) -> None:
        if not self._window.closing():
            check_for_updates(self._window.config_manager, manual=True)

    def show_shortcuts(self) -> None:
        if not self._window.closing():
            ShortcutsDialog(self._window).exec()

    def report_issue(self) -> None:
        QDesktopServices.openUrl(QUrl(ISSUES_URL))

    def on_drawer_action(self, row: str) -> None:
        """The drawer's Settings, Add provider, Free channels and Open file rows, and its
        header's Library, ⌕ and refresh buttons."""
        if row == SETTINGS_ROW:
            self._window.options_dialog()
        elif row == ADD_PROVIDER_ROW:
            self._window.add_provider()
        elif row == FREE_CHANNELS_ROW:
            drawer = self._window.overlays.drawer
            self._window.show_free_channels(drawer.row_anchor(row))
        elif row == OPEN_FILE_ROW:
            self._window.screens.open_file()
        elif row == SEARCH_ACTION:
            self._window.screens.open_search()
        elif row == LIBRARY_ACTION:  # 2.8: the section opened last, Movies the first time
            screens = self._window.screens
            screens.open_library(screens.library_controller.section or "movies")
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
