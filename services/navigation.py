"""Layered navigation for the drawer: Source → Section → Group → Item → Season → Episode.

The sources column ends with three action rows, Settings, Add provider and Open file: rows to
choose, not providers or layers to open (the drawer hands them to the shell, as it does the
Library's sections).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
import itertools
import logging
import threading
from typing import Any, Callable, Optional, cast

from PySide6.QtCore import QObject, QTimer, Signal

from services import favorites
from services.catalog import ALL_GROUP_ID, PROVIDER_LOADING_MESSAGE
from services.provider_ids import (
    ALL_PROVIDERS,
    ALL_PROVIDERS_TITLE,
    provider_of,
    split_group_id,
    with_provider,
)
from services.text_fold import fold_all

logger = logging.getLogger(__name__)

SECTION_TITLES = {"live": "Live TV", "movies": "Movies", "series": "Series"}
LIBRARY_SECTIONS = frozenset({"movies", "series"})  # chosen in the drawer, shown by the Library
SETTINGS_ROW = "settings"
ADD_PROVIDER_ROW = "add_provider"
FREE_CHANNELS_ROW = "free_channels"  # iptv-org's free lists, in a menu at the row
OPEN_FILE_ROW = "open_file"  # File → Open File… without the menu bar (fullscreen hides it)
FAVORITES_GROUP = "@favorites"
RECENT_GROUP = "@recent"
_RECENT_LIMIT = 20
_MEMO_LIMIT = 64  # memoized child lists kept (least recently used are dropped)
FOLD_AHEAD_MS = 400  # a long list folds its titles this long after it is shown
LAZY_ROWS = 2000  # a list of items this long gets its nodes made on demand (see NodeSeq)


@dataclass(frozen=True)
class NavNode:
    layer: str
    key: str
    title: str
    payload: Any = None
    count: Optional[int] = None

    def _identity(self) -> tuple:
        # Payload dicts are unhashable: identity is layer + key, and for a channel or title
        # its provider (the same id at two providers is two rows in a mixed list).
        return (
            self.layer,
            self.key,
            provider_of(self.payload) if isinstance(self.payload, dict) else "",
        )

    def __hash__(self):
        return hash(self._identity())

    def __eq__(self, other):
        return isinstance(other, NavNode) and self._identity() == other._identity()


ACTION_ROWS = (  # the drawer paints their icons (a gear, a plus, a file)
    NavNode("action", SETTINGS_ROW, "Settings"),
    NavNode("action", ADD_PROVIDER_ROW, "Add provider"),
    NavNode("action", FREE_CHANNELS_ROW, "Free channels…"),
    NavNode("action", OPEN_FILE_ROW, "Open file"),
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


class _Folded(QObject):
    folded = Signal()


class NodeSeq(Sequence):
    """The item nodes of a long list (one provider's 57,000 channels, or all four's 200,000),
    made when a row is asked for: the drawer shows a screenful at a time, and making every
    node up front cost seconds. It reads like a list of NavNodes and is not changed once made.

    `items` are the dicts behind the rows (the models take them as they are); `matching()` is
    the same list narrowed by the drawer's filter.
    """

    _tokens = itertools.count(1)

    def __init__(
        self,
        items: list[dict],
        origin: Optional[list[int]] = None,
        folded: Optional[list[str]] = None,
    ):
        self.items = items
        self.token = next(self._tokens)  # tells one list from another without comparing rows
        self._origin = origin  # each item's row in the list this one was narrowed from
        self._folded = folded
        self._folding: Optional[threading.Thread] = None
        self._fold_due = False
        self._notifier: Optional[_Folded] = None
        self.heard_by: set[int] = set()  # the views connected to `folded`
        self._lock = threading.Lock()
        self._made: dict[int, NavNode] = {}
        self._rows: dict[NavNode, int] = {}  # the nodes made so far -> their rows
        self._absent: set[NavNode] = set()  # nodes looked for here and not found
        self._narrowed: Optional[tuple[str, NodeSeq]] = None
        self._digits: Optional[int] = None
        self._numbered: Optional[bool] = None

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, row):  # type: ignore[override]
        if isinstance(row, slice):
            return [self[i] for i in range(*row.indices(len(self.items)))]
        if row < 0:
            row += len(self.items)
        node = self._made.get(row)
        if node is None:
            if not 0 <= row < len(self.items):
                raise IndexError(row)
            node = self._made[row] = _item_node(self.items[row])
            self._rows[node] = row
        return node

    def __iter__(self):
        for row, item in enumerate(self.items):
            yield self._made.get(row) or _item_node(item)

    def source_row(self, row: int) -> int:
        """Row `row`'s position in the list this one was narrowed from (its own if none)."""
        return self._origin[row] if self._origin is not None else row

    def row_of(self, node: NavNode) -> Optional[int]:
        """The row holding `node` (any node of the same item), None if it is not listed."""
        row = self._rows.get(node)
        if row is not None or node.layer != "item" or node in self._absent:
            return row
        # A node from an older list: find it by its id and provider, and keep it as made.
        provider = provider_of(node.payload) if isinstance(node.payload, dict) else ""
        for at, item in enumerate(self.items):
            if item_key(item) == node.key and provider_of(item) == provider:
                return self._rows.setdefault(self[at], at)
        self._absent.add(node)
        return None

    def find_focus(self, wanted: "str | tuple[str, str]") -> Optional[NavNode]:
        """The first row an id, or a (provider, id) pair, names (without making every node)."""
        provider, key = wanted if isinstance(wanted, tuple) else ("", wanted)
        for row, item in enumerate(self.items):
            if item_key(item) == key and (not provider or provider_of(item) == provider):
                return self[row]
        return None

    def matching(self, needle: str) -> "NodeSeq":
        """The rows whose folded title contains `needle` (already folded); typing on narrows
        the rows of the filter before."""
        if not needle:
            return self
        if self._narrowed is not None:
            before, seq = self._narrowed
            if before == needle:
                return seq
            if needle.startswith(before):
                narrowed = seq.matching(needle)
                self._narrowed = (needle, narrowed)
                return narrowed
        if self._folded is None:
            # Never folded on the GUI thread (200,000 titles): start it in the background if
            # it has not, and show the list as it is until it ends.
            if self._folding is None:
                self._fold_in_background()
            if self._folded is None:
                return self
        folded = self._folded
        rows = [row for row, title in enumerate(folded) if needle in title]
        narrowed = NodeSeq(
            [self.items[row] for row in rows],
            [self.source_row(row) for row in rows],
            [folded[row] for row in rows],
        )
        self._narrowed = (needle, narrowed)
        return narrowed

    def fold_ahead(self, after_ms: int = FOLD_AHEAD_MS) -> None:
        """Fold the titles in the background a moment from now (not while the list is still
        opening), so the first letter typed to filter finds them done: a quarter of a second
        for 200,000 channels. Asking again starts nothing."""
        if self._folded is None and self._folding is None and not self._fold_due:
            self._fold_due = True
            QTimer.singleShot(after_ms, self._fold_in_background)

    def _fold_in_background(self) -> None:
        if self._folded is None and self._folding is None:
            self._folding = threading.Thread(target=self._fold_and_tell, daemon=True)
            self._folding.start()

    @property
    def folded(self) -> "_Folded":
        """Emits `folded` (on the GUI thread) when the titles are folded. Made on first use,
        and that must be on the GUI thread: the signal is queued to it from the fold's."""
        if self._notifier is None:
            self._notifier = _Folded()
        return self._notifier

    def _fold_and_tell(self) -> None:
        try:
            self._folded_titles()
        except Exception:
            logger.exception("Folding the titles failed")  # a later filter tries again
            return
        finally:
            self._folding = None
        if self._notifier is not None:
            self._notifier.folded.emit()

    def _folded_titles(self, wait: bool = True) -> Optional[list[str]]:
        if not self._lock.acquire(blocking=wait):
            return None
        try:
            if self._folded is None:
                titles = [i.get("name") or i.get("title") or "Untitled" for i in self.items]
                try:
                    self._folded = fold_all(titles)
                except TypeError:  # a title that is not text
                    self._folded = fold_all([str(title) for title in titles])
            return self._folded
        finally:
            self._lock.release()

    def numbered(self) -> bool:
        """Whether any channel has a number (else the position stands in for it)."""
        if self._numbered is None:
            self._numbered = any(_number(item) for item in self.items)
        return self._numbered

    def number_digits(self) -> int:
        """The most digits any channel's number has."""
        if self._digits is None:
            numbers = {item.get("number") for item in self.items}
            self._digits = max((len(text) for text in map(_text, numbers) if text), default=0)
        return self._digits


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(item: dict) -> str:
    return _text(item.get("number"))


def payloads(nodes: Sequence) -> list[dict]:
    """The dicts behind a column's nodes (the channels of a group)."""
    if isinstance(nodes, NodeSeq):
        return list(nodes.items)
    return [n.payload for n in nodes if isinstance(n.payload, dict)]


def _item_nodes(items: list[dict]) -> "Sequence[NavNode]":
    """Nodes for a group's items: a long list makes them on demand."""
    if len(items) >= LAZY_ROWS:
        return NodeSeq(items)
    return [_item_node(i) for i in items]


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
        self._revealed: Optional[dict] = None  # the row the list opened on (see reveal)
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
        return NavNode("source", name, ALL_PROVIDERS_TITLE if name == ALL_PROVIDERS else name, name)

    def all_mode(self) -> bool:
        """Whether the channel list shows every provider (the "All providers" source)."""
        return self.catalog.current_provider_name() == ALL_PROVIDERS

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
            names = self.catalog.provider_names()
            sources = [NavNode("source", n, n, n) for n in names]
            if len(names) >= 2:  # one provider has nothing to mix
                sources.insert(
                    0, NavNode("source", ALL_PROVIDERS, ALL_PROVIDERS_TITLE, ALL_PROVIDERS)
                )
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
            (
                NavNode("header", g.id, g.title, g)  # a provider's heading: never opened
                if g.header
                else NavNode("group", g.id, g.title, g, g.count)
            )
            for g in self.catalog.groups(parent.key)
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
        return _item_nodes(items)

    # --- memoization -------------------------------------------------------------------
    def _data_version(self) -> object:
        """The catalog's content version, or this model's event count when it has none."""
        version = getattr(self.catalog, "content_version", None)
        return version() if callable(version) else self._events

    def _favorites_fingerprint(self) -> tuple:
        return favorites.fingerprint(self.config)

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
            return entry[1] if isinstance(entry[1], NodeSeq) else list(entry[1])
        value = compute()
        if value is None:
            self._memo.pop(key, None)
            return None
        self._memo[key] = (deps, value)
        self._memo.move_to_end(key)
        while len(self._memo) > _MEMO_LIMIT:
            self._memo.popitem(last=False)
        return value if isinstance(value, NodeSeq) else list(value)

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
            if self._group_is_fetched_alone(node.key):
                return ("group", section, node.key)
            return ("section", section)
        if node.layer == "item":
            return ("seasons", node.key)
        if node.layer == "season":
            series = next((n for n in reversed(self._path) if n.layer == "item"), None)
            return ("episodes", series.key, node.key) if series else None
        return None

    def _group_is_fetched_alone(self, group_id: str) -> bool:
        """Whether a group's items are fetched on their own (an STB provider's), not with
        the whole section; in "All providers" the group's id names its provider."""
        if self.catalog.provider_type() != "ALL":
            return self.catalog.provider_type() == "STB"
        name, own_id = split_group_id(group_id)
        if not name or not own_id:
            return False  # the mixed All, or a provider's heading: the sections as a whole
        try:
            return self.catalog.provider_entry(name).get("type") == "STB"
        except KeyError:
            return False

    def load_error(self, node: NavNode) -> Optional[str]:
        """Why `node`'s children failed to load; None while they may still come."""
        key = self._node_key(node)
        message = self._errors.get(key) if key else None
        return None if message == PROVIDER_LOADING_MESSAGE else message

    def is_loading(self, node: NavNode) -> bool:
        """True while `node`'s children are on their way, or while a provider sets up."""
        # In "All providers" one provider's setup must not hold the others' lists back: a
        # section is loading until some provider has answered for it.
        if not self.all_mode() and self.catalog.is_loading():
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
        """Opening this source starts a provider setup again: its last one failed."""
        if node.layer != "source":
            return False
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
        """Every live channel once; the same id at two providers is two channels."""
        seen, result = set(), []
        for group in self.catalog.groups("live"):
            if group.header:
                continue
            for item in self.catalog.items("live", group.id) or []:
                key = (provider_of(item), item_key(item))
                if key not in seen:
                    seen.add(key)
                    result.append(item)
        return result

    def _favorite_items(self) -> list[dict]:
        # Scanning every live channel is the expensive part: a zap (history) must not redo it.
        deps = (self._data_version(), self._favorites_fingerprint())
        return cast(list[dict], self._cached(("@favorites",), deps, self._compute_favorites))

    def _recent_items(self) -> list[dict]:
        deps = (self._history_fingerprint(), tuple(self.catalog.provider_names()))
        return cast(list[dict], self._cached(("@recent",), deps, self._compute_recent))

    def _compute_favorites(self) -> list[dict]:
        is_kept = favorites.matcher(self.config)
        if is_kept is None:
            return []
        return [i for i in self._all_live_items() if is_kept(i)]

    def _compute_recent(self) -> list[dict]:
        # What was watched at any provider in the list, newest first (a removed provider's
        # entries can no longer play).
        known = set(self.catalog.provider_names())
        seen, result = set(), []
        for entry in self.config.watch_history or []:
            provider = entry.get("provider_name")
            if provider not in known or entry.get("item_type") != "channel":
                continue
            item = entry.get("item_data") or {}
            key = (provider, item_key(item))
            if key not in seen:
                seen.add(key)
                result.append(with_provider(item, provider) if len(known) > 1 else item)
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

    def shows_provider(self, parent: Optional[NavNode]) -> bool:
        """Whether the channels under `parent` name their provider: ★ Favorites and
        ⟲ Recent when there are several providers, and All in "All providers"."""
        if parent is None or parent.layer != "group" or len(self.catalog.provider_names()) < 2:
            return False
        if parent.key in (FAVORITES_GROUP, RECENT_GROUP):
            return True
        return parent.key == ALL_GROUP_ID and self.all_mode()

    # --- movement ----------------------------------------------------------------------
    def is_playable(self, node: NavNode) -> bool:
        if node.layer == "episode":
            return True
        return node.layer == "item" and self.section_of(node) in ("live", "movies")

    def open(self, node: NavNode) -> None:
        if node.layer == "header":
            return  # a provider's heading in the mixed lists only labels its groups
        if node.layer == "source":
            # Another provider is only shown (its setup runs once, if it has not); the
            # current one is set up again when its last setup failed. A ready provider
            # answers at once and reopens its list, so the path is set first.
            self._path = [node]
            if self.needs_setup(node) or node.key != self.catalog.current_provider_name():
                self._provider_errors.pop(node.key, None)
                self.providerRequested.emit(node.title)
                self.catalog.select_provider(node.key)
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
        self._revealed = None
        source = self._source()
        section_node = NavNode("section", section, SECTION_TITLES.get(section, section), section)
        self._path = [source, section_node]
        self._pending_group = None
        self._request_section(section)
        if group_id is not None and not self._resolve_group(section, group_id):
            self._pending_group = (section, group_id)
        self.changed.emit()

    def reveal(self, source: str, section: str, group_key: str, item: dict) -> None:
        """Open the list where `item` was picked: its source, section and group (the channel
        list opening at the big picture's channel). A source other than the page's is only
        shown (navigation: nothing that plays stops); `revealed()` is the row to highlight."""
        names = self.catalog.provider_names()
        if source and source != self.catalog.current_provider_name():
            if source == ALL_PROVIDERS and len(names) < 2:
                pass  # the providers changed since: keep what the list shows
            elif source == ALL_PROVIDERS or source in names:
                self._provider_errors.pop(source, None)
                self.catalog.select_provider(source)
        self.reset_to(section, group_key or ALL_GROUP_ID)
        self._revealed = item

    def revealed(self) -> Optional[dict]:
        """The item the list was last opened on by reveal(); None after any other reset."""
        return self._revealed

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
        key = ("section", section)
        # Asked again (the drawer opening) and fresh: when this changes nothing children()
        # reads, its memoized nodes stay (the catalog's content version covers new content).
        unchanged = (
            callable(getattr(self.catalog, "content_version", None))
            and section in self._ready_sections
            and key not in self._pending
            and key not in self._errors
            and not (self._pending_group and self._pending_group[0] == section)
        )
        if not unchanged:
            self._events += 1
        self._ready_sections.add(section)
        self._requested_sections.discard(section)
        self._pending.discard(key)
        self._errors.pop(key, None)
        if self._pending_group and self._pending_group[0] == section:
            if not self._resolve_group(*self._pending_group):
                logger.debug("Group %s not found in section %s", self._pending_group[1], section)
            self._pending_group = None
        self.changed.emit()

    def _on_section_failed(self, section: str, message: str) -> None:
        # While a provider sets up, the catalog still holds the previous provider's groups:
        # that is not a failed refresh. It stays an error until providerReady clears it.
        loading = message == PROVIDER_LOADING_MESSAGE or (
            not self.all_mode() and self.catalog.is_loading()
        )
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
        self._provider_errors.pop(name, None)
        self._ready_sections.clear()
        self._pending_group = None
        if not self._path or self._path[0].key != name:
            self._path = [self._source()]
        self.changed.emit()
