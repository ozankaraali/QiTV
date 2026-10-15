"""Layered navigation for the drawer: Source → Section → Group → Item → Season → Episode."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Optional

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


def _item_key(item: dict) -> str:
    return str(item.get("id", item.get("cmd", item.get("name", ""))))


def _item_node(item: dict, layer: str = "item") -> NavNode:
    return NavNode(
        layer, _item_key(item), str(item.get("name") or item.get("title") or "Untitled"), item
    )


class NavModel(QObject):
    changed = Signal()

    def __init__(self, catalog, config, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.catalog = catalog
        self.config = config
        self._path: list[NavNode] = []
        for signal in (
            catalog.sectionReady,
            catalog.groupReady,
            catalog.seasonsReady,
            catalog.episodesReady,
        ):
            signal.connect(self._emit_changed)
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
                self.catalog.request_seasons(parent.payload)
                return None
            return [_item_node(s, "season") for s in seasons]
        if parent.layer == "season":
            series = next((n for n in reversed(self._path) if n.layer == "item"), None)
            if series is None:
                return []
            episodes = self.catalog.episodes(series.payload, parent.payload)
            if episodes is None:
                self.catalog.request_episodes(series.payload, parent.payload)
                return None
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
            self.catalog.request_group(section, group.key)
            return None
        return [_item_node(i) for i in items]

    def _all_live_items(self) -> list[dict]:
        seen, result = set(), []
        for group in self.catalog.groups("live"):
            for item in self.catalog.items("live", group.id) or []:
                key = _item_key(item)
                if key not in seen:
                    seen.add(key)
                    result.append(item)
        return result

    def _favorite_items(self) -> list[dict]:
        names = set(self.config.favorites or [])
        return [i for i in self._all_live_items() if i.get("name") in names] if names else []

    def _recent_items(self) -> list[dict]:
        provider = self.catalog.current_provider_name()
        seen, result = set(), []
        for entry in self.config.watch_history or []:
            if entry.get("provider_name") != provider or entry.get("item_type") != "channel":
                continue
            item = entry.get("item_data") or {}
            key = _item_key(item)
            if key not in seen:
                seen.add(key)
                result.append(item)
            if len(result) >= _RECENT_LIMIT:
                break
        return result

    def _pinned_groups(self) -> list[NavNode]:
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
        self.catalog.request_section(section)
        if group_id is not None:
            group = next((g for g in self.children(section_node) or [] if g.key == group_id), None)
            self._path.append(group or NavNode("group", group_id, group_id))
        self.changed.emit()

    def columns(self):
        if not self._path:
            return (None, self.children(None)), (None, None)
        left_parent = self._path[-2] if len(self._path) >= 2 else None
        right_parent = self._path[-1]
        return (left_parent, self.children(left_parent)), (
            right_parent,
            self.children(right_parent),
        )

    # --- catalog events ------------------------------------------------------------------
    def _emit_changed(self, *args) -> None:
        self.changed.emit()

    def _on_provider_ready(self, name: str) -> None:
        if not self._path or self._path[0].key != name:
            self._path = [self._source()]
        self.changed.emit()
