"""The drawer's context menu: a right-click on a channel, title or episode, or ⟲ Recent.

The player controls' "Open in external player" offers the two players for what plays now
(`show_players`): the same plays as the menu's, opening upwards from the button. A right-click
on a Library title or episode offers the players and its link (`show_target`). 2.9.3: a
right-click on a channel of the guide offers its row's menu as the channel list has it
(`show_channel`), and one on the picture the menu of what plays (`show_playing`: the channel's,
without picture-in-picture of itself; a film's or an episode's players and link).

- "Add to favorites" / "Remove from favorites" (channels only);
- "Open in picture-in-picture" (channels, when the shell offers picture in picture): the
  channel plays small beside the one playing (Ctrl/⌘+P on the highlighted row does the same);
- "Record at a time…" (channels, when QiTV records, 2.8): the channel recorded from one time to
  another, once or on chosen days, with no guide needed (`ui.record_timer_dialog`);
- "Remove from Recents" (a channel of ⟲ Recent, while the list shows it): it leaves the list
  (Delete on the highlighted row does the same); "Clear Recents ›" there, and on the ⟲ Recent
  row itself, opens one entry that clears the whole list: the submenu is the confirmation;
- "Play in VLC" / "Play in MPV": the shell plays the link externally once the in-app stream is
  released. A player that is not installed is said at once, and nothing stops. A local file
  (File → Open File…) is no provider's: it goes out as it is, with no link to ask for;
- "Copy stream link": the link goes to the clipboard.
Links are asked for on LinkLanes' side lane (one at a time, beside the tune's); a newer
choice supersedes an external play still waiting for its link, never a copy.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, QPoint
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu, QWidget

from services import external_players, favorites, i18n
from services.i18n import N_, tr, tr_n, translate
from services.navigation import (
    FAVORITES_GROUP,
    RECENT_GROUP,
    NavModel,
    NavNode,
    item_key,
    item_title,
)
from ui.link_lanes import LinkLanes, Target
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

logger = logging.getLogger(__name__)

Launcher = Callable[[str], tuple[bool, str]]
# English (a test finds the menu's entry by it); translate() shows them.
LINK_COPIED_TEXT = N_("Link copied")  # i18n: a toast: the stream's link is on the clipboard
OPEN_PIP_TEXT = N_("Open in picture-in-picture")  # i18n: a menu entry
# i18n: a menu entry: the channel leaves the list of channels watched lately ("Recent")
REMOVE_RECENT_TEXT = N_("Remove from Recents")
# i18n: a channel's or a group's right-click entry: parental controls hide it (2.8)
HIDE_TEXT = N_("Hide (parental controls)")
# i18n: a channel group's menu (2.9.1): out of the lists, no PIN; Settings → Providers shows it
TIDY_TEXT = N_("Hide group")
# i18n: a channel's right-click entry: record it from one time to another (no guide needed)
RECORD_AT_TEXT = N_("Record at a time…")
# i18n: a menu entry opening a submenu that confirms emptying the "Recent" list
CLEAR_RECENTS_TEXT = N_("Clear Recents")
PLAYERS = ((N_("Play in VLC"), "vlc"), (N_("Play in MPV"), "mpv"))


def _a_providers_group(node: NavNode) -> bool:
    """A group its provider lists (not Favorites, Recent, All, or a provider's heading)."""
    from services.catalog import ALL_GROUP_ID
    from services.provider_ids import split_group_id

    name, own = split_group_id(node.key)
    group_id = own if name else node.key
    return bool(group_id) and group_id != ALL_GROUP_ID and not str(group_id).startswith("@")


def is_channel(nav: NavModel, node: Optional[NavNode]) -> bool:
    """A live item: the only rows that become favorites from the player."""
    return (
        node is not None
        and node.layer == "item"
        and isinstance(node.payload, dict)
        and nav.section_of(node) == "live"
    )


def _css(value: str, alpha: Optional[float] = None) -> str:
    color = qcolor(value, alpha)
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alphaF():.2f})"


def menu_style() -> str:
    """The menu in the shell's colours, with the shared focus style."""
    line = _css(PALETTE.text, 0.12)
    return (
        f"QMenu {{ background: {_css(PALETTE.panel_raised)}; color: {_css(PALETTE.text)};"
        f" border: 1px solid {line}; padding: 4px; }}"
        " QMenu::item { padding: 6px 22px; border: 1.5px solid transparent; border-radius: 6px; }"
        f" QMenu::item:selected {{ background: {_css(PALETTE.accent, FOCUS_FILL_ALPHA)};"
        f" border-color: {_css(PALETTE.accent)}; }}"
        f" QMenu::separator {{ height: 1px; background: {line}; margin: 4px 8px; }}"
    )


class ContextActions(QObject):
    def __init__(
        self,
        *,
        nav: NavModel,
        config,
        lanes: LinkLanes,
        external,
        toggle_favorite: Callable[[dict], None],
        play_external: Callable[[Target, str, Launcher], None],
        notify: Callable[[str, str], None],
        open_pip: Optional[Callable[[NavNode], None]] = None,
        forget_recent: Optional[Callable[[dict], None]] = None,
        clear_recents: Optional[Callable[[], None]] = None,
        hide: Optional[Callable[[NavNode], None]] = None,
        may_hide: Callable[[], bool] = lambda: True,
        tidy: Optional[Callable[[NavNode], None]] = None,
        record_at: Optional[Callable[[], Optional[Callable[[dict], None]]]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """`external` is services.external_players or a stand-in; `notify(text, kind)` toasts.

        `open_pip(node)` opens a channel row in picture in picture, `forget_recent(item)` takes
        a channel out of ⟲ Recent and `clear_recents()` empties it (None: no such entry).
        `record_at()` is what records a channel at a time (2.8; None while QiTV cannot).
        """
        super().__init__(parent)
        self._open_pip = open_pip
        self._hide = hide  # 2.8: parental controls hide a channel or a group ...
        self._may_hide = may_hide  # ... once their PIN is set
        self._tidy = tidy  # 2.9.1: a group leaves the lists, no PIN (services.hidden_groups)
        self._record_at = record_at
        self._forget_recent = forget_recent
        self._clear_recents = clear_recents
        self.nav = nav
        self.config = config
        self.lanes = lanes
        self.external = external
        self._toggle_favorite = toggle_favorite
        self._play_external = play_external
        self._notify = notify

    def show(self, node: NavNode, global_pos: QPoint, parent: Optional[QObject] = None) -> None:
        """Pop `node`'s menu up at `global_pos`; it is built now and deleted once closed."""
        menu = self.menu(node, parent if isinstance(parent, QWidget) else None)
        if menu is not None:
            menu.aboutToHide.connect(menu.deleteLater)
            menu.popup(global_pos)

    def show_players(
        self, target: Target, anchor: QPoint, parent: Optional[QObject] = None
    ) -> None:
        """The two players for `target` (what plays now), opening upwards from `anchor`: the
        player controls' button top-left, in global coordinates."""
        menu = QMenu(parent if isinstance(parent, QWidget) else None)
        menu.setStyleSheet(menu_style())
        self._add_players(menu, target)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(QPoint(anchor.x(), anchor.y() - menu.sizeHint().height()))

    def show_target(
        self, target: Target, global_pos: QPoint, parent: Optional[QObject] = None
    ) -> None:
        """A Library title's or episode's menu: the players and its link (no favorites)."""
        menu = QMenu(parent if isinstance(parent, QWidget) else None)
        menu.setStyleSheet(menu_style())
        self._add_link_actions(menu, target)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(global_pos)

    def show_channel(
        self,
        item: dict,
        global_pos: QPoint,
        parent: Optional[QObject] = None,
        *,
        group_key: str = "",
    ) -> None:
        """2.9.3: a live channel's menu where the channel list is not (the guide's rows): its
        row's entries, whatever the channel list shows now."""
        menu = self.channel_menu(item, parent if isinstance(parent, QWidget) else None, group_key)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(global_pos)

    def show_playing(
        self, target: Target, global_pos: QPoint, parent: Optional[QObject] = None
    ) -> None:
        """2.9.3: a right-click on the picture: the menu of what plays. A live channel's as
        its row's (no picture-in-picture of the channel itself), a film's or an episode's the
        players and its link, a file's the players."""
        widget = parent if isinstance(parent, QWidget) else None
        if target.live and not target.local:
            menu = self.channel_menu(target.item, widget, target.group_key, playing=target)
        else:
            menu = QMenu(widget)
            menu.setStyleSheet(menu_style())
            if target.local:
                self._add_players(menu, target)
            else:
                self._add_link_actions(menu, target)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(global_pos)

    def channel_menu(
        self,
        item: dict,
        parent: Optional[QWidget] = None,
        group_key: str = "",
        *,
        playing: Optional[Target] = None,
    ) -> QMenu:
        """A live channel's menu wherever it is listed (2.9.3): favorites, picture-in-picture
        (not for `playing`: the channel playing itself), Record at a time…, Hide, then the
        players and its link (`playing`'s own, else the channel's live one)."""
        node = NavNode("item", item_key(item), item_title(item), item)
        menu = QMenu(parent)
        menu.setStyleSheet(menu_style())
        self._add_channel_actions(menu, node, pip=playing is None, recent=False)
        menu.addSeparator()
        target = playing or Target(item, "live", node.title, group_key=group_key)
        self._add_link_actions(menu, target)
        return menu

    def menu(self, node: NavNode, parent: Optional[QWidget] = None) -> Optional[QMenu]:
        """`node`'s menu; None when it has nothing to offer (a series)."""
        if node.layer == "group" and node.key == RECENT_GROUP:
            return self._recents_menu(parent)
        hide = self._hide if self._may_hide() else None
        tidy = self._tidy
        if (
            node.layer == "group"
            and (hide is not None or tidy is not None)
            and node.key != FAVORITES_GROUP
            and _a_providers_group(node)
        ):
            menu = QMenu(parent)  # a group's menu: hidden from the lists (2.9.1), parental (2.8)
            menu.setStyleSheet(menu_style())
            if tidy is not None:
                menu.addAction(translate(TIDY_TEXT)).triggered.connect(lambda _=False: tidy(node))
            if hide is not None:
                menu.addAction(translate(HIDE_TEXT)).triggered.connect(lambda _=False: hide(node))
            return menu
        channel = is_channel(self.nav, node)
        playable = isinstance(node.payload, dict) and self.nav.is_playable(node)
        if not channel and not playable:
            return None
        menu = QMenu(parent)
        menu.setStyleSheet(menu_style())
        if channel:
            self._add_channel_actions(menu, node)
        if playable:
            if channel:
                menu.addSeparator()
            self._add_link_actions(menu, self._target(node))
        return menu

    def _add_channel_actions(
        self, menu: QMenu, node: NavNode, *, pip: bool = True, recent: bool = True
    ) -> None:
        """A channel's own entries: favorites, picture-in-picture (with `pip`), Record at a
        time…, Remove from Recents (with `recent`, on a row of ⟲ Recent) and Hide."""
        item: dict = node.payload
        kept = favorites.is_favorite(self.config, item)
        text = tr("Remove from favorites") if kept else tr("Add to favorites")
        menu.addAction(text).triggered.connect(lambda _=False: self._toggle_favorite(item))
        open_pip = self._open_pip if pip else None
        if open_pip is not None:
            menu.addAction(translate(OPEN_PIP_TEXT)).triggered.connect(
                lambda _=False: open_pip(node)
            )
        record_at = self._record_at() if self._record_at is not None else None
        if record_at is not None:
            menu.addAction(translate(RECORD_AT_TEXT)).triggered.connect(
                lambda _=False: record_at(item)
            )
        forget = self._forget_recent
        if recent and forget is not None and self.nav.is_recent_row(node):
            menu.addSeparator()
            menu.addAction(translate(REMOVE_RECENT_TEXT)).triggered.connect(
                lambda _=False: forget(item)
            )
            self._add_clear_recents(menu)
        hide = self._hide if self._may_hide() else None
        if hide is not None:
            menu.addAction(translate(HIDE_TEXT)).triggered.connect(lambda _=False: hide(node))

    def _recents_menu(self, parent: Optional[QWidget]) -> Optional[QMenu]:
        """A right-click on the ⟲ Recent row: "Clear Recents ›"."""
        if self._clear_recents is None or not self.nav.recent_count():
            return None
        menu = QMenu(parent)
        menu.setStyleSheet(menu_style())
        self._add_clear_recents(menu)
        return menu

    def _add_clear_recents(self, menu: QMenu) -> None:
        """The "Clear Recents ›" submenu: its one entry, naming how many channels go, clears
        the list (a second, deliberate choice: never a dialog over the TV)."""
        clear = self._clear_recents
        if clear is None:
            return
        sub = menu.addMenu(translate(CLEAR_RECENTS_TEXT))
        sub.setStyleSheet(menu_style())
        count = self.nav.recent_count()
        # i18n: the one entry under "Clear Recents": it empties the list; {n} is how many go
        text = tr_n("Clear {n} channel", "Clear all {n} channels", count, n=i18n.number(count))
        sub.addAction(text).triggered.connect(lambda _=False: clear())

    def _target(self, node: NavNode) -> Target:
        """What the row plays: its section and group as the drawer shows them now."""
        group = next((n.key for n in reversed(self.nav.path) if n.layer == "group"), "")
        section = self.nav.section_of(node) or "live"
        return Target(node.payload, section, node.title, group_key=group)

    def _add_link_actions(self, menu: QMenu, target: Target) -> None:
        """The players, then "Copy stream link"."""
        self._add_players(menu, target)
        menu.addSeparator()
        menu.addAction(tr("Copy stream link")).triggered.connect(  # i18n: a menu entry
            lambda _=False: self.lanes.request_side(target, self._copy_link, play=False)
        )

    def _add_players(self, menu: QMenu, target: Target) -> None:
        for text, player in PLAYERS:
            menu.addAction(translate(text)).triggered.connect(
                lambda _=False, name=player: self._external_play(target, name)
            )

    def _external_play(self, target: Target, player: str) -> None:
        if player == "vlc":
            found, launch, missing = (
                self.external.find_vlc(),
                self.external.launch_vlc,
                external_players.VLC_MISSING,
            )
        else:
            found, launch, missing = (
                self.external.find_mpv(),
                self.external.launch_mpv,
                external_players.MPV_MISSING,
            )
        if not found:
            logger.info("Not playing %s externally: %s is not installed", target.title, player)
            self._notify(translate(missing.partition("\n")[0]), "error")  # its headline
            return
        if target.local:  # its address is the file's (play_file)
            self.lanes.supersede()  # a newer choice than a side play waiting for its link
            self._play_external(target, str(target.item.get("cmd") or ""), launch)
            return
        self.lanes.request_side(
            target, lambda url: self._play_external(target, url, launch), play=True
        )

    def _copy_link(self, url: str) -> None:
        QGuiApplication.clipboard().setText(url)
        self._notify(translate(LINK_COPIED_TEXT), "info")
