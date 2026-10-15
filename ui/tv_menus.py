"""The TV window's menu bar, About, the update check, Keyboard Shortcuts and Report an Issue.

Single-key entries only show their key (a hint, active inside the open menu): the window's
keyPressEvent handles those keys through ui.keymap. Modifier combinations are application
shortcuts, which the keymap leaves to the menus (a test checks that no screen claims them).
The player's keys are disabled while the Library shows. The Providers menu is an exclusive
group of the configured providers, rebuilt when a provider becomes ready or fails and after
Settings closes. The drawer's Settings and Add provider rows open the same dialogs.
"""

from __future__ import annotations

from functools import partial
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QMenu, QVBoxLayout, QWidget

from config_manager import get_app_version
from services.exporter import ExportKind
from services.navigation import ADD_PROVIDER_ROW, SETTINGS_ROW
from ui.brand import logo_pixmap
from ui.keymap import Action
from ui.shortcuts_dialog import ShortcutsDialog
from update_checker import check_for_updates

logger = logging.getLogger(__name__)

HOME_URL = "https://github.com/ozankaraali/QiTV"
ISSUES_URL = f"{HOME_URL}/issues"
ABOUT_LOGO_PX = 64
ABOUT_TEXT_MIN_W = 340  # wide enough for the links on one line each
ABOUT_TEXT = (
    "<p><b>QiTV {version}</b></p>"
    "<p>A TV-first IPTV player for STB portals, Xtream and M3U playlists.</p>"
    '<p><a href="{home}">{home}</a><br><a href="{issues}">Report an issue</a></p>'
)
NO_PROVIDERS_TEXT = "No providers"
_SHORTCUT_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.MetaModifier
)


def about_text() -> str:
    """About's text: the version (pyproject.toml, as packaged) and the project's links."""
    return ABOUT_TEXT.format(version=get_app_version(), home=HOME_URL, issues=ISSUES_URL)


class AboutDialog(QDialog):
    """About QiTV: the logo (the same cropped one as the Library's top bar and the welcome
    card, at this window's pixel ratio) above the name, the version and the links."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About QiTV")
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
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, self)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(self.logo)
        layout.addWidget(self.text)
        layout.addWidget(buttons)


class TVMenus(QObject):
    """Builds and runs the window's menus. `window` is the TVWindow (see TVWindow.__init__)."""

    def __init__(self, window, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._window = window
        self._player_actions: list[QAction] = []  # entries of the player's keys
        self._provider_group: Optional[QActionGroup] = None
        self.providers_menu: QMenu

    # ---- building -----------------------------------------------------------------------------

    def build(self) -> None:
        window, screens = self._window, self._window.screens
        bar, add, role = window.menuBar(), self._add_action, QAction.MenuRole
        app_menu = bar.addMenu("QiTV")
        add(app_menu, "About QiTV", "", self.show_about, role.AboutRole)
        add(app_menu, "Check for Updates…", "", self.check_updates, role.ApplicationSpecificRole)
        add(app_menu, "Settings…", "Ctrl+,", window.options_dialog, role.PreferencesRole)
        add(app_menu, "Quit", "Ctrl+Q", window.close, role.QuitRole)
        file_menu = bar.addMenu("File")
        add(file_menu, "Open File…", "Ctrl+O", screens.open_file)
        export = file_menu.addMenu("Export")
        for text, kind in (
            ("Cached Content…", ExportKind.CACHED),
            ("All Live Channels…", ExportKind.ALL_LIVE),
            ("Complete Series (Fetch All)…", ExportKind.COMPLETE_SERIES),
        ):
            add(export, text, "", partial(screens.export, kind))
        view = bar.addMenu("View")
        add(view, "Live TV", "Ctrl+1", screens.back_to_tv)
        add(view, "Movies", "Ctrl+2", lambda: screens.open_library("movies"))
        add(view, "Series", "Ctrl+3", lambda: screens.open_library("series"))
        add(view, "Search", "Ctrl+F", screens.open_search)
        player = self._player_actions  # the player's keys: off while the Library shows
        player.append(add(view, "Guide", "G", self._handler(Action.GUIDE)))
        player.append(add(view, "Channel List", "Return", self._handler(Action.OPEN_DRAWER)))
        add(view, "Fullscreen", "F", window.toggle_fullscreen)
        player.append(add(view, "Mini Player", "Alt+P", self._handler(Action.MINI_PLAYER)))
        playback = bar.addMenu("Playback")
        player.append(add(playback, "Play/Pause", "Space", self._handler(Action.PLAY_PAUSE)))
        player.append(add(playback, "Mute", "M", self._handler(Action.MUTE)))
        player.append(
            add(playback, "Previous Channel", "Backspace", self._handler(Action.PREVIOUS))
        )
        player.append(add(playback, "Go Live", "L", self._handler(Action.GO_LIVE)))
        self.providers_menu = bar.addMenu("Providers")
        self.rebuild_providers()
        help_menu = bar.addMenu("Help")
        add(help_menu, "Keyboard Shortcuts…", "", self.show_shortcuts)
        add(help_menu, "Report an Issue…", "", self.report_issue)
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
        action.triggered.connect(lambda _checked=False: slot())
        menu.addAction(action)
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

    def _handler(self, action: Action) -> Callable[[], None]:
        return lambda: self._window.controller.handle(action)

    def set_player_enabled(self, enabled: bool) -> None:
        """The player's keys in the menus: off while the Library shows."""
        for action in self._player_actions:
            action.setEnabled(enabled)

    # ---- providers ----------------------------------------------------------------------------

    def rebuild_providers(self) -> None:
        """One checkable entry per provider, the current one (or the one loading) checked."""
        menu = self.providers_menu
        menu.clear()
        catalog = self._window.catalog
        names = catalog.provider_names()
        if not names:
            menu.addAction(NO_PROVIDERS_TEXT).setEnabled(False)
            return
        current = catalog.loading_provider() or catalog.current_provider_name()
        if self._provider_group is not None:
            self._provider_group.deleteLater()
        self._provider_group = group = QActionGroup(self)
        group.setExclusive(True)
        for name in names:
            action = QAction(name, menu)
            action.setCheckable(True)
            action.setChecked(name == current)
            action.triggered.connect(lambda _checked=False, n=name: self._choose_provider(n))
            group.addAction(action)
            menu.addAction(action)

    def _choose_provider(self, name: str) -> None:
        """The exclusive group moved the check; providerReady rebuilds the menu (never here:
        that would delete the action whose signal runs)."""
        window = self._window
        catalog = window.catalog
        heading_to = catalog.loading_provider() or catalog.current_provider_name()
        if not window.closing() and name != heading_to:
            window.controller.select_provider(name)

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
        """The drawer's Settings and Add provider rows."""
        if row == SETTINGS_ROW:
            self._window.options_dialog()
        elif row == ADD_PROVIDER_ROW:
            self._window.add_provider()
