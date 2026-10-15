"""Layered navigation for the drawer: Source → Section → Group → Item → Season → Episode."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Optional, cast

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

SECTION_TITLES = {"live": "Live TV", "movies": "Movies", "series": "Series"}
FAVORITES_GROUP = "@favorites"
RECENT_GROUP = "@recent"
_RECENT_LIMIT = 20


@dataclass(frozen=True)
class NavNode:
    layer: str
    key: str
    title: str
    payload: Any = None
    count: Optional[int] = None

    def __hash__(self):  # payload dicts are unhashable; identity is layer + key
        return hash((self.layer, self.key))

    def __eq__(self, other):
        return isinstance(other, NavNode) and (self.layer, self.key) == (other.layer, other.key)


def item_key(item: dict) -> str:
    """The drawer key of a channel, title, series or season: its id, else cmd, else name."""
    return str(item.get("id", item.get("cmd", item.get("name", ""))))


_item_key = item_key  # the earlier private name


def episode_key(item: dict) -> str:
    """The drawer key of an episode: id, season and episode number.

    STB episodes copy the series dict, so their id and name belong to the series.
    """
    return ":".join(
        str(v)
        for v in (
            item.get("id", ""),
            item.get("season_id") or item.get("season") or "",
            item.get("series") or item.get("episode_num") or item.get("number") or "",
        )
    )


def _item_node(item: dict, layer: str = "item") -> NavNode:
    key = item_key(item)
    title = item.get("name") or item.get("title")
    if layer == "episode":
        key = episode_key(item)
        title = item.get("ename") or title
    return NavNode(layer, key, str(title or "Untitled"), item)


class NavModel(QObject):
    changed = Signal()
    # A message for the shell to toast, e.g. that a saved catalog is shown because its
    # refresh failed.
    notice = Signal(str)

    def __init__(self, catalog, config, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.catalog = catalog
        self.config = config
        self._path: list[NavNode] = []
        self._pending: set[tuple] = set()
        self._errors: dict[tuple, str] = {}
        self._ready_sections: set[str] = set()
        self._pending_group: Optional[tuple[str, str]] = None
        self._memo: dict[str, Any] = {}
        self._fingerprint: Optional[tuple] = None
        catalog.sectionReady.connect(self._on_section_ready)
        catalog.groupReady.connect(self._on_group_ready)
        catalog.seasonsReady.connect(self._on_seasons_ready)
        catalog.episodesReady.connect(self._on_episodes_ready)
        catalog.sectionFailed.connect(self._on_section_failed)
        catalog.groupFailed.connect(self._on_group_failed)
        catalog.seasonsFailed.connect(self._on_seasons_failed)
        catalog.episodesFailed.connect(self._on_episodes_failed)
        catalog.providerReady.connect(self._on_provider_ready)

    @property
    def path(self) -> list[NavNode]:
        return list(self._path)

    # --- structure ---------------------------------------------------------------------
    def _source(self) -> NavNode:
        name = self.catalog.current_provider_name()
        return NavNode("source", name, name, name)

    def section_of(self, node: Optional[NavNode] = None) -> Optional[str]:
        chain = self._path if node is None else self._path + [node]
        for entry in chain:
            if entry.layer == "section":
                return entry.key
        return None

    def children(  # type: ignore[override]  # signature fixed by the drawer interface
        self, parent: Optional[NavNode]
    ) -> Optional[list[NavNode]]:
        if parent is None:
            return [NavNode("source", name, name, name) for name in self.catalog.provider_names()]
        if parent.layer == "source":
            return [
                NavNode("section", s, SECTION_TITLES.get(s, s.title()), s)
                for s in self.catalog.sections()
            ]
        if parent.layer == "section":
            if ("section", parent.key) in self._errors:
                return []
            pinned = self._pinned_groups() if parent.key == "live" else []
            groups = [
                NavNode("group", g.id, g.title, g, g.count) for g in self.catalog.groups(parent.key)
            ]
            return pinned + groups
        if parent.layer == "group":
            return self._group_items(parent)
        if parent.layer == "item" and self.section_of(parent) == "series":
            seasons = self.catalog.seasons(parent.payload)
            if seasons is None:
                sid = item_key(parent.payload)
                return self._load(
                    ("seasons", sid), lambda: self.catalog.request_seasons(parent.payload)
                )
            return [_item_node(s, "season") for s in seasons]
        if parent.layer == "season":
            series = next((n for n in reversed(self._path) if n.layer == "item"), None)
            if series is None:
                return []
            episodes = self.catalog.episodes(series.payload, parent.payload)
            if episodes is None:
                key = ("episodes", item_key(series.payload), item_key(parent.payload))
                return self._load(
                    key, lambda: self.catalog.request_episodes(series.payload, parent.payload)
                )
            return [_item_node(e, "episode") for e in episodes]
        return []

    def _group_items(self, group: NavNode) -> Optional[list[NavNode]]:
        if group.key == FAVORITES_GROUP:
            return [_item_node(i) for i in self._favorite_items()]
        if group.key == RECENT_GROUP:
            return [_item_node(i) for i in self._recent_items()]
        section = self.section_of(group) or "live"
        items = self.catalog.items(section, group.key)
        if items is None:
            key = self._node_key(group)
            if key and key[0] == "section" and section in self._ready_sections:
                return []  # the section loaded and still has no such group items
            if key and key[0] == "group":
                return self._load(key, lambda: self.catalog.request_group(section, group.key))
            return self._load(("section", section), lambda: self.catalog.request_section(section))
        return [_item_node(i) for i in items]

    def _load(self, key: tuple, request) -> Optional[list[NavNode]]:
        if key in self._errors:
            return []
        if key not in self._pending:
            self._pending.add(key)
            request()
        return None

    def _node_key(self, node: NavNode) -> Optional[tuple]:
        if node.layer == "section":
            return ("section", node.key)
        if node.layer == "group":
            section = self.section_of(node) or "live"
            if self.catalog.provider_type() == "STB":
                return ("group", section, node.key)
            return ("section", section)
        if node.layer == "item":
            return ("seasons", node.key)
        if node.layer == "season":
            series = next((n for n in reversed(self._path) if n.layer == "item"), None)
            return ("episodes", series.key, node.key) if series else None
        return None

    def load_error(self, node: NavNode) -> Optional[str]:
        key = self._node_key(node)
        return self._errors.get(key) if key else None

    def retry(self, node: NavNode) -> None:
        key = self._node_key(node)
        if key is None:
            return
        self._errors.pop(key, None)
        self._pending.discard(key)
        if key[0] == "section":
            self._ready_sections.discard(key[1])
            self._pending.add(key)
            self.catalog.request_section(key[1])
        self.changed.emit()

    def _all_live_items(self) -> list[dict]:
        seen, result = set(), []
        for group in self.catalog.groups("live"):
            for item in self.catalog.items("live", group.id) or []:
                key = item_key(item)
                if key not in seen:
                    seen.add(key)
                    result.append(item)
        return result

    def _check_memo(self) -> None:
        history = self.config.watch_history or []
        fingerprint = (
            tuple(self.config.favorites or []),
            len(history),
            repr(history[0]) if history else None,
        )
        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self._memo.clear()

    def _favorite_items(self) -> list[dict]:
        self._check_memo()
        if "fav" not in self._memo:
            self._memo["fav"] = self._compute_favorites()
        return cast(list[dict], self._memo["fav"])

    def _recent_items(self) -> list[dict]:
        self._check_memo()
        if "recent" not in self._memo:
            self._memo["recent"] = self._compute_recent()
        return cast(list[dict], self._memo["recent"])

    def _compute_favorites(self) -> list[dict]:
        names = set(self.config.favorites or [])
        return [i for i in self._all_live_items() if i.get("name") in names] if names else []

    def _compute_recent(self) -> list[dict]:
        provider = self.catalog.current_provider_name()
        seen, result = set(), []
        for entry in self.config.watch_history or []:
            if entry.get("provider_name") != provider or entry.get("item_type") != "channel":
                continue
            item = entry.get("item_data") or {}
            key = item_key(item)
            if key not in seen:
                seen.add(key)
                result.append(item)
            if len(result) >= _RECENT_LIMIT:
                break
        return result

    def _pinned_groups(self) -> list[NavNode]:
        self._check_memo()
        if "pinned" not in self._memo:
            self._memo["pinned"] = self._compute_pinned()
        return list(self._memo["pinned"])

    def _compute_pinned(self) -> list[NavNode]:
        pinned = []
        favorites = self._favorite_items()
        if favorites:
            pinned.append(NavNode("group", FAVORITES_GROUP, "★ Favorites", None, len(favorites)))
        recent = self._recent_items()
        if recent:
            pinned.append(NavNode("group", RECENT_GROUP, "⟲ Recent", None, len(recent)))
        return pinned

    # --- movement ----------------------------------------------------------------------
    def is_playable(self, node: NavNode) -> bool:
        if node.layer == "episode":
            return True
        return node.layer == "item" and self.section_of(node) in ("live", "movies")

    def open(self, node: NavNode) -> None:
        if node.layer == "source":
            if node.key != self.catalog.current_provider_name():
                self.catalog.select_provider(node.key)
            self._path = [node]
        else:
            depth = {"section": 1, "group": 2, "item": 3, "season": 4, "episode": 5}[node.layer]
            self._path = self._path[:depth] + [node]
            if node.layer == "section":
                self.catalog.request_section(node.key)
        self.changed.emit()

    def up(self) -> bool:
        if len(self._path) <= 1:
            return False
        self._path.pop()
        self.changed.emit()
        return True

    def reset_to(self, section: str = "live", group_id: Optional[str] = None) -> None:
        source = self._source()
        section_node = NavNode("section", section, SECTION_TITLES.get(section, section), section)
        self._path = [source, section_node]
        self._pending_group = None
        self.catalog.request_section(section)
        if group_id is not None and not self._resolve_group(section, group_id):
            self._pending_group = (section, group_id)
        self.changed.emit()

    def _resolve_group(self, section: str, group_id: str) -> bool:
        if len(self._path) != 2 or self._path[1].key != section:
            return False
        group = next((g for g in self.children(self._path[1]) or [] if g.key == group_id), None)
        if group is None:
            return False
        self._path.append(group)
        return True

    def columns(
        self,
    ) -> tuple[
        tuple[Optional[NavNode], Optional[list[NavNode]]],
        tuple[Optional[NavNode], Optional[list[NavNode]]],
    ]:
        if not self._path:
            return (None, self.children(None)), (None, None)
        left_parent = self._path[-2] if len(self._path) >= 2 else None
        right_parent = self._path[-1]
        return (left_parent, self.children(left_parent)), (
            right_parent,
            self.children(right_parent),
        )

    # --- catalog events ------------------------------------------------------------------
    def _settled(self, key: tuple) -> None:
        self._memo.clear()
        self._pending.discard(key)
        self._errors.pop(key, None)
        self.changed.emit()

    def _on_section_ready(self, section: str) -> None:
        self._memo.clear()
        self._ready_sections.add(section)
        self._pending.discard(("section", section))
        self._errors.pop(("section", section), None)
        if self._pending_group and self._pending_group[0] == section:
            if not self._resolve_group(*self._pending_group):
                logger.debug("Group %s not found in section %s", self._pending_group[1], section)
            self._pending_group = None
        self.changed.emit()

    def _on_section_failed(self, section: str, message: str) -> None:
        if self.catalog.groups(section):
            # A saved catalog could not be refreshed: keep showing it as a loaded
            # section, and say so instead of hiding it behind an error row.
            logger.info("Refreshing section %s failed; showing the saved catalog", section)
            self._on_section_ready(section)
            title = SECTION_TITLES.get(section, section.title())
            self.notice.emit(f"Couldn't refresh {title}; showing saved channels")
            return
        if self._pending_group and self._pending_group[0] == section:
            self._pending_group = None
        self._fail(("section", section), message)

    def _on_group_ready(self, section: str, group_id: str) -> None:
        self._settled(("group", section, group_id))

    def _on_seasons_ready(self, series_id: str) -> None:
        self._settled(("seasons", series_id))

    def _on_episodes_ready(self, series_id: str, season_id: str) -> None:
        self._settled(("episodes", series_id, season_id))

    def _fail(self, key: tuple, message: str) -> None:
        logger.info("Load failed for %s", key)  # the message is for the user, not the log
        if key[0] == "section":
            self._ready_sections.discard(key[1])
        self._pending.discard(key)
        self._errors[key] = message or "Load failed"
        self.changed.emit()

    def _on_group_failed(self, section: str, group_id: str, message: str) -> None:
        self._fail(("group", section, group_id), message)

    def _on_seasons_failed(self, series_id: str, message: str) -> None:
        self._fail(("seasons", series_id), message)

    def _on_episodes_failed(self, series_id: str, season_id: str, message: str) -> None:
        self._fail(("episodes", series_id, season_id), message)

    def _on_provider_ready(self, name: str) -> None:
        self._memo.clear()
        self._pending.clear()
        self._errors.clear()
        self._ready_sections.clear()
        self._pending_group = None
        if not self._path or self._path[0].key != name:
            self._path = [self._source()]
        self.changed.emit()
