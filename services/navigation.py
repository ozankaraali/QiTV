"""Layered navigation for the drawer: Source → Section → Group → Item → Season → Episode.

The sources column ends with two action rows, Settings and Add provider: rows to choose, not
providers or layers to open (the drawer hands them to the shell, as it does the Library's
sections).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import logging
from typing import Any, Callable, Optional, cast

from PySide6.QtCore import QObject, Signal

from services import favorites
from services.catalog import PROVIDER_LOADING_MESSAGE

logger = logging.getLogger(__name__)

SECTION_TITLES = {"live": "Live TV", "movies": "Movies", "series": "Series"}
LIBRARY_SECTIONS = frozenset({"movies", "series"})  # chosen in the drawer, shown by the Library
SETTINGS_ROW = "settings"
ADD_PROVIDER_ROW = "add_provider"
FAVORITES_GROUP = "@favorites"
RECENT_GROUP = "@recent"
_RECENT_LIMIT = 20
_MEMO_LIMIT = 64  # memoized child lists kept (least recently used are dropped)


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


ACTION_ROWS = (  # the drawer paints their icons (a gear, a plus)
    NavNode("action", SETTINGS_ROW, "Settings"),
    NavNode("action", ADD_PROVIDER_ROW, "Add provider"),
)


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


def item_title(item: dict, *, episode: bool = False) -> str:
    """How the drawer names a channel, title or season (an episode by its own name)."""
    title = (item.get("ename") if episode else None) or item.get("name") or item.get("title")
    return str(title or "Untitled")


def _item_node(item: dict, layer: str = "item") -> NavNode:
    episode = layer == "episode"
    key = episode_key(item) if episode else item_key(item)
    return NavNode(layer, key, item_title(item, episode=episode), item)


class NavModel(QObject):
    changed = Signal()
    # A message for the shell to toast, e.g. that a saved catalog is shown because its
    # refresh failed.
    notice = Signal(str)
    # A source opened here started a provider setup (the shell shows that it loads).
    providerRequested = Signal(str)

    def __init__(self, catalog, config, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.catalog = catalog
        self.config = config
        self._path: list[NavNode] = []
        self._pending: set[tuple] = set()
        self._errors: dict[tuple, str] = {}
        self._provider_errors: dict[str, str] = {}  # providers whose last setup failed
        self._requested_sections: set[str] = set()  # asked for, neither ready nor failed yet
        self._ready_sections: set[str] = set()
        self._pending_group: Optional[tuple[str, str]] = None
        # children() results, keyed by parent and stamped with what they depend on.
        self._memo: OrderedDict[tuple, tuple[tuple, Any]] = OrderedDict()
        self._events = 0  # bumped by every catalog event handled here, and by retry()
        catalog.sectionReady.connect(self._on_section_ready)
        catalog.groupReady.connect(self._on_group_ready)
        catalog.seasonsReady.connect(self._on_seasons_ready)
        catalog.episodesReady.connect(self._on_episodes_ready)
        catalog.sectionFailed.connect(self._on_section_failed)
        catalog.groupFailed.connect(self._on_group_failed)
        catalog.seasonsFailed.connect(self._on_seasons_failed)
        catalog.episodesFailed.connect(self._on_episodes_failed)
        catalog.providerReady.connect(self._on_provider_ready)
        catalog.providerFailed.connect(self._on_provider_failed)

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
            sources = [NavNode("source", n, n, n) for n in self.catalog.provider_names()]
            return sources + list(ACTION_ROWS)
        if parent.layer == "source":
            return [
                NavNode("section", s, SECTION_TITLES.get(s, s.title()), s)
                for s in self.catalog.sections()
            ]
        section = self.section_of(parent)
        state = (self._data_version(), self._events)
        if parent.layer == "section":
            pinned = self._pinned_deps() if parent.key == "live" else ()
            return self._cached(
                ("section", parent.key), state + pinned, lambda: self._section_children(parent)
            )
        if parent.layer == "group":
            if parent.key == FAVORITES_GROUP:
                return [_item_node(i) for i in self._favorite_items()]
            if parent.key == RECENT_GROUP:
                return [_item_node(i) for i in self._recent_items()]
            return self._cached(
                ("group", section, parent.key), state, lambda: self._group_items(parent)
            )
        series = next((n for n in reversed(self._path) if n.layer == "item"), None)
        key = (parent.layer, section, series.key if series else None, parent.key)
        return self._cached(key, state, lambda: self._series_children(parent))

    def _section_children(self, parent: NavNode) -> list[NavNode]:
        if ("section", parent.key) in self._errors:
            return []
        pinned = self._pinned_groups() if parent.key == "live" else []
        groups = [
            NavNode("group", g.id, g.title, g, g.count) for g in self.catalog.groups(parent.key)
        ]
        return pinned + groups

    def _series_children(self, parent: NavNode) -> Optional[list[NavNode]]:
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

    # --- memoization -------------------------------------------------------------------
    def _data_version(self) -> object:
        """The catalog's content version, or this model's event count when it has none."""
        version = getattr(self.catalog, "content_version", None)
        return version() if callable(version) else self._events

    def _favorites_fingerprint(self) -> tuple:
        return tuple(favorites.names(self.config))

    def _history_fingerprint(self) -> tuple:
        history = self.config.watch_history or []
        return (len(history), repr(history[0]) if history else None)

    def _pinned_deps(self) -> tuple:
        return (
            self._favorites_fingerprint(),
            self._history_fingerprint(),
            self.catalog.current_provider_name(),
        )

    def _cached(
        self, key: tuple, deps: tuple, compute: Callable[[], Optional[list]]
    ) -> Optional[list]:
        """compute() once per key while deps stay equal; None (still loading) is not kept."""
        entry = self._memo.get(key)
        if entry is not None and entry[0] == deps:
            self._memo.move_to_end(key)
            return list(entry[1])
        value = compute()
        if value is None:
            self._memo.pop(key, None)
            return None
        self._memo[key] = (deps, value)
        self._memo.move_to_end(key)
        while len(self._memo) > _MEMO_LIMIT:
            self._memo.popitem(last=False)
        return list(value)

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
        """Why `node`'s children failed to load; None while they may still come."""
        key = self._node_key(node)
        message = self._errors.get(key) if key else None
        return None if message == PROVIDER_LOADING_MESSAGE else message

    def is_loading(self, node: NavNode) -> bool:
        """True while `node`'s children are on their way, or while a provider sets up."""
        if self.catalog.is_loading():
            return True
        key = self._node_key(node)
        if key is None:
            return False
        if key[0] == "section" and key[1] in self._requested_sections:
            return True
        return key in self._pending or self._errors.get(key) == PROVIDER_LOADING_MESSAGE

    def provider_error(self, node: NavNode) -> Optional[str]:
        """Why the setup of a source's provider failed, until it is set up again."""
        return self._provider_errors.get(node.key) if node.layer == "source" else None

    def needs_setup(self, node: NavNode) -> bool:
        """Opening this source starts a provider setup: another provider, or a failed one."""
        if node.layer != "source":
            return False
        if node.key != self.catalog.current_provider_name():
            return True
        return node.key in self._provider_errors and not self.catalog.is_loading()

    def retry(self, node: NavNode) -> None:
        key = self._node_key(node)
        if key is None:
            return
        self._errors.pop(key, None)
        self._pending.discard(key)
        if key[0] == "section":
            self._ready_sections.discard(key[1])
            self._pending.add(key)
            self._requested_sections.add(key[1])
            self.catalog.request_section(key[1])
        self._events += 1
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

    def _favorite_items(self) -> list[dict]:
        # Scanning every live channel is the expensive part: a zap (history) must not redo it.
        deps = (self._data_version(), self._favorites_fingerprint())
        return cast(list[dict], self._cached(("@favorites",), deps, self._compute_favorites))

    def _recent_items(self) -> list[dict]:
        deps = (self._history_fingerprint(), self.catalog.current_provider_name())
        return cast(list[dict], self._cached(("@recent",), deps, self._compute_recent))

    def _compute_favorites(self) -> list[dict]:
        kept = set(favorites.names(self.config))
        if not kept:
            return []
        return [i for i in self._all_live_items() if favorites.favorite_key(i) in kept]

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
        deps = (self._data_version(),) + self._pinned_deps()
        return cast(list[NavNode], self._cached(("@pinned",), deps, self._compute_pinned))

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
            # The current provider is set up again when its last setup failed.
            if self.needs_setup(node):
                self._provider_errors.pop(node.key, None)
                self.catalog.select_provider(node.key)
                self.providerRequested.emit(node.key)
            self._path = [node]
        else:
            depth = {"section": 1, "group": 2, "item": 3, "season": 4, "episode": 5}[node.layer]
            self._path = self._path[:depth] + [node]
            if node.layer == "section":
                self._request_section(node.key)
        self.changed.emit()

    def focus_source(self, name: str) -> None:
        """Show the sources with `name` at the end of the path (e.g. after its setup failed)."""
        self._path = [NavNode("source", name, name, name)]
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
        self._request_section(section)
        if group_id is not None and not self._resolve_group(section, group_id):
            self._pending_group = (section, group_id)
        self.changed.emit()

    def _request_section(self, section: str) -> None:
        """Ask for a section; it is loading until sectionReady or sectionFailed."""
        self._requested_sections.add(section)
        self.catalog.request_section(section)

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
        self._events += 1
        self._pending.discard(key)
        self._errors.pop(key, None)
        self.changed.emit()

    def _on_section_ready(self, section: str) -> None:
        self._events += 1
        self._ready_sections.add(section)
        self._requested_sections.discard(section)
        self._pending.discard(("section", section))
        self._errors.pop(("section", section), None)
        if self._pending_group and self._pending_group[0] == section:
            if not self._resolve_group(*self._pending_group):
                logger.debug("Group %s not found in section %s", self._pending_group[1], section)
            self._pending_group = None
        self.changed.emit()

    def _on_section_failed(self, section: str, message: str) -> None:
        # While a provider sets up, the catalog still holds the previous provider's groups:
        # that is not a failed refresh. It stays an error until providerReady clears it.
        loading = message == PROVIDER_LOADING_MESSAGE or self.catalog.is_loading()
        if not loading and self.catalog.groups(section):
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
        self._events += 1
        if key[0] == "section":
            self._ready_sections.discard(key[1])
            self._requested_sections.discard(key[1])
        self._pending.discard(key)
        self._errors[key] = message or "Load failed"
        self.changed.emit()

    def _on_group_failed(self, section: str, group_id: str, message: str) -> None:
        self._fail(("group", section, group_id), message)

    def _on_seasons_failed(self, series_id: str, message: str) -> None:
        self._fail(("seasons", series_id), message)

    def _on_episodes_failed(self, series_id: str, season_id: str, message: str) -> None:
        self._fail(("episodes", series_id, season_id), message)

    def _on_provider_failed(self, name: str, message: str) -> None:
        # Requests made for the setup's provider will never settle: none is loading now,
        # and a request refused because the setup ran is not loading either.
        self._events += 1
        self._pending.clear()
        self._requested_sections.clear()
        for key in [k for k, m in self._errors.items() if m == PROVIDER_LOADING_MESSAGE]:
            del self._errors[key]
        self._provider_errors[name] = message or "Couldn't connect"
        self.changed.emit()

    def _on_provider_ready(self, name: str) -> None:
        self._memo.clear()
        self._events += 1
        self._pending.clear()
        self._requested_sections.clear()
        self._errors.clear()
        self._provider_errors.clear()
        self._ready_sections.clear()
        self._pending_group = None
        if not self._path or self._path[0].key != name:
            self._path = [self._source()]
        self.changed.emit()
