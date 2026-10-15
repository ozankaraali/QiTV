"""The drawer's context menu: a right-click on a channel, title or episode.

- "Add to favorites" / "Remove from favorites" (channels only);
- "Open in picture-in-picture" (channels, when the shell offers picture in picture): the
  channel plays small beside the one playing (Ctrl/⌘+P on the highlighted row does the same);
- "Play in VLC" / "Play in MPV": the shell plays the link externally once the in-app stream is
  released. A player that is not installed is said at once, and nothing stops;
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

from services import external_players, favorites
from services.navigation import NavModel, NavNode
from ui.link_lanes import LinkLanes, Target
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

logger = logging.getLogger(__name__)

Launcher = Callable[[str], tuple[bool, str]]
LINK_COPIED_TEXT = "Link copied"
OPEN_PIP_TEXT = "Open in picture-in-picture"


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
        parent: Optional[QObject] = None,
    ) -> None:
        """`external` is services.external_players or a stand-in; `notify(text, kind)` toasts.

        `open_pip(node)` opens a channel row in picture in picture (None: no such entry).
        """
        super().__init__(parent)
        self._open_pip = open_pip
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

    def menu(self, node: NavNode, parent: Optional[QWidget] = None) -> Optional[QMenu]:
        """`node`'s menu; None when it has nothing to offer (a series)."""
        channel = is_channel(self.nav, node)
        playable = isinstance(node.payload, dict) and self.nav.is_playable(node)
        if not channel and not playable:
            return None
        menu = QMenu(parent)
        menu.setStyleSheet(menu_style())
        item: dict = node.payload
        if channel:
            kept = favorites.is_favorite(self.config, item)
            text = "Remove from favorites" if kept else "Add to favorites"
            menu.addAction(text).triggered.connect(lambda _=False: self._toggle_favorite(item))
            open_pip = self._open_pip
            if open_pip is not None:
                menu.addAction(OPEN_PIP_TEXT).triggered.connect(lambda _=False: open_pip(node))
        if playable:
            if channel:
                menu.addSeparator()
            target = self._target(node)
            for text, player in (("Play in VLC", "vlc"), ("Play in MPV", "mpv")):
                menu.addAction(text).triggered.connect(
                    lambda _=False, name=player: self._external_play(target, name)
                )
            menu.addSeparator()
            menu.addAction("Copy stream link").triggered.connect(
                lambda _=False: self.lanes.request_side(target, self._copy_link, play=False)
            )
        return menu

    def _target(self, node: NavNode) -> Target:
        """What the row plays: its section and group as the drawer shows them now."""
        group = next((n.key for n in reversed(self.nav.path) if n.layer == "group"), "")
        section = self.nav.section_of(node) or "live"
        return Target(node.payload, section, node.title, group_key=group)

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
            self._notify(missing.partition("\n")[0], "error")
            return
        self.lanes.request_side(
            target, lambda url: self._play_external(target, url, launch), play=True
        )

    def _copy_link(self, url: str) -> None:
        QGuiApplication.clipboard().setText(url)
        self._notify(LINK_COPIED_TEXT, "info")
