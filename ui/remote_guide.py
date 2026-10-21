"""A phone's Guide: the channel list's groups as the TV lists them, a group's channels a page at
a time with what's on and next, and a channel's timetable. GUI thread only.

Each phone reads the groups through its own NavModel (★ Favorites, ⟲ Recent, then the
provider's groups), so its browsing leaves the TV's channel list where it is; it opens where the
TV's list is, on the page of the channel playing. The find box searches the level shown, the
whole of it, and every channel besides, as the desktop search does: after the groups it
matches, or after the group's own channels. Groups travel as short keys, channels and
programmes as handles.

With two providers or more, the providers are a level above the groups: All providers, then
each. The phone's lists browse through a catalog of their own (`catalog`), whose choice moves
them and never the TV's.

The Remote tab's channel list is a bare one: the same groups and channels, without programmes
(its messages "chgroups", "channels" and "chsources"), so it loads no programme guide.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, ClassVar, Optional, cast

from PySide6.QtCore import QObject, QTimer

from services.catchup import programme_in_archive
from services.i18n import translate
from services.navigation import NavModel, NavNode, item_key, section_title
from services.provider_ids import ALL_PROVIDERS, ALL_PROVIDERS_TITLE, provider_of
from services.search import SearchIndex, search_index
from services.text_fold import fold, fold_all
from services.zapping import ZapContext
from ui.remote_snapshots import Handles, programme_dict, stable_key

GROUPS_PAGE = 100
GUIDE_PAGE = 50
FOUND_PAGE = 25  # the channels a find finds below the groups: with 100 groups, a frame's worth
TIMETABLE_LIMIT = 60
TIMETABLE_BACK = timedelta(hours=12)  # how far back a timetable lists a channel's archive
TITLE_CHARS = 80
ABOUT_CHARS = 160  # a programme's description, as much as two lines on a phone show
SETTLE_MS = 250  # lists arriving together answer once


def _pages(count: int, size: int) -> int:
    return max(1, -(-count // size))


class PhoneGuide(QObject):
    """`send(message)` reaches this phone. Each ask answers at once; a list or a programme guide
    that lands later sends the view's first page again when what it shows changed."""

    # Every live channel for the find, its name folded: one per catalog and version, for every
    # list browsing through that catalog (a phone's two lists share theirs).
    _indexes: ClassVar[dict[int, tuple[object, SearchIndex]]] = {}

    def __init__(
        self,
        window: Any,
        send: Callable[[dict], None],
        parent=None,
        bare: bool = False,
        catalog: Any = None,
    ) -> None:
        super().__init__(parent)
        self._window = window
        self._send = send
        self._bare = bare
        self._kinds = (
            ("chgroups", "channels", "chsources") if bare else ("groups", "guide", "sources")
        )
        self._catalog = catalog if catalog is not None else window.catalog
        self._sources: dict[str, str] = {}  # the phone's key → a provider (or ALL_PROVIDERS)
        self.nav = NavModel(self._catalog, window.config_manager, self)
        self.nav.reset_to("live")
        self.handles = Handles()
        self.providers: set[str] = set()  # those of the channels shown: their guides load
        self._ids: dict[str, str] = {}  # the phone's key → group key, the groups seen so far
        self._view: Optional[tuple] = None  # ("groups", find) or ("guide", group, find)
        # A group asked for before the lists listed it: its key, or "" for where the TV is.
        self._wanted: Optional[str] = None
        self._anchor = 0  # the page the view opened on: the one sent again
        self._shown: Optional[tuple] = None  # what that page showed, as last sent
        self._timetable: Optional[str] = None  # the channel whose timetable was asked last
        self._folded: tuple[int, int, list[str]] = (0, -1, [])  # a list's names, folded
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self._push)
        self.nav.changed.connect(self._changed)
        if not bare:
            window.catalog.epgReady.connect(self._on_guide_loaded)

    # ---- the phone's asks ------------------------------------------------------------------

    def groups(self, page: int, find: str, n: int = -1, source: str = "") -> None:
        """A page of the groups; `source` (a key `sources` gave) shows another provider's, or
        all of them, from now on."""
        name = self._sources.get(source) if source else None
        if name is not None and name != self._catalog.source():
            self._catalog.select_provider(name)
        self._wanted = None
        self._ask(("groups", find), page, n)

    def sources(self, n: int = -1) -> None:
        """The providers above the groups: All providers (two or more), then each one, the one
        shown marked."""
        names = list(self._catalog.provider_names())
        shown = self._catalog.source()
        rows = []
        for name in ([ALL_PROVIDERS] if len(names) >= 2 else []) + names:
            key = stable_key(f"source:{name}")
            self._sources[key] = name
            row: dict[str, Any] = {"id": key, "title": _source_title(name)[:TITLE_CHARS]}
            if name == shown:
                row["on"] = True
            rows.append(row)
        message: dict = {"t": self._kinds[2], "rows": rows}
        if n >= 0:
            message["n"] = n
        self._send(message)

    def channels(self, key: str, page: int, find: str, n: int = -1) -> None:
        """A group's channels; `key` "" is the group the TV's channel list is in, on the page of
        the channel playing. A group the lists don't hold yet opens once they do."""
        group = self._group_of(key) if key else self._tv_group()
        if not key:
            page = self._playing_page(group)
        self._wanted = key if group is None else None
        self._ask(("guide", group, find), page, n)

    def timetable(self, handle: str) -> None:
        """`handle`'s channel: its programmes from the one on now (with an archive, from earlier,
        each a tap away), the one on now a tap from live."""
        found = self.handles.get(handle)
        if found is None or found[0] != "channel":
            return
        self._timetable = handle
        channel, group_key, channels = cast(tuple, found[1])
        controller = self._window.controller
        now, listed = controller.timetable(channel, TIMETABLE_LIMIT, TIMETABLE_BACK)
        days = controller.archive_days(channel)
        rows = []
        for programme in listed:
            on = programme.is_on(now)
            row: dict[str, Any] = {
                "title": programme.title[:TITLE_CHARS],
                "start": programme.start.timestamp(),
                "stop": programme.stop.timestamp(),
                "on": on,
            }
            if programme.description:
                row["about"] = programme.description[:ABOUT_CHARS]
            if on:
                row["id"] = handle  # watching what's on: the channel live
            elif (
                days > 0
                and now - timedelta(days=days) <= programme.start < now
                and programme_in_archive(channel, programme)
            ):
                payload = (channel, programme, group_key, channels)
                row["id"] = self.handles.issue("catchup", payload)
            rows.append(row)
        self.providers = self.providers | ({provider_of(channel)} - {""})
        message: dict = {
            "t": "timetable",
            "id": handle,
            "name": str(channel.get("name") or "")[:TITLE_CHARS],
            "num": controller.channel_number(channel),
            "rows": rows,
        }
        if not rows and controller.guide_loading(channel):
            message["loading"] = True  # sent again once the guide is in
        self._send(message)

    def _ask(self, view: tuple, page: int, n: int = -1) -> None:
        if view != self._view:
            self._view, self._anchor = view, page
        if page == self._anchor:
            self._retry_failed()
        message, signature = self._answer(page)
        if page == self._anchor:
            self._shown = signature
        if n >= 0:
            message["n"] = n  # the ask it answers; a later push carries none
        self._send(message)

    # ---- the groups ------------------------------------------------------------------------

    def _section(self) -> NavNode:
        return NavNode("section", "live", section_title("live"), "live")

    def _group_nodes(self) -> list[NavNode]:
        """The section's groups and, with providers mixed, their providers' headings."""
        nodes = self.nav.children(self._section()) or []
        return [n for n in nodes if n.layer in ("group", "header")]

    def _group_node(self, group: Optional[str]) -> Optional[NavNode]:
        return next((n for n in self._group_nodes() if n.layer == "group" and n.key == group), None)

    def _tv_group(self) -> Optional[str]:
        """Where the TV's channel list is, when it is listed; else the first group."""
        listed = [n.key for n in self._group_nodes() if n.layer == "group"]
        group = str(self._window.controller.channel_list_group())
        if group in listed:
            return group
        return listed[0] if listed else None

    def _playing_page(self, group: Optional[str]) -> int:
        playing = self._window.controller._channel
        node = self._group_node(group)
        channels = self._channels(node) if node is not None and playing is not None else None
        for index, channel in enumerate(channels or []):
            if ZapContext.same(channel, playing):
                return index // GUIDE_PAGE
        return 0

    def _key(self, group: Optional[str]) -> str:
        if group is None:
            return ""
        key = stable_key(group)
        self._ids[key] = group
        return key

    def _group_of(self, key: str) -> Optional[str]:
        listed = [n.key for n in self._group_nodes() if n.layer == "group"]
        if key not in self._ids:
            for each in listed:
                self._ids[stable_key(each)] = each
        found = self._ids.get(key)
        return found if found in listed else None

    def _channels(self, node: NavNode) -> Optional[list[dict]]:
        nodes = self.nav.children(node)
        if nodes is None:
            return None
        items = getattr(nodes, "items", None)  # a long list's dicts, without making its rows
        if isinstance(items, list):
            return items
        return [n.payload for n in nodes if isinstance(n.payload, dict)]

    def _found_channels(self, channels: list[dict], find: str) -> list[dict]:
        words = fold(find).split()  # every word, in any order: as the desktop search
        if not words:
            return channels
        if self._folded[:2] != (id(channels), len(channels)):
            names = [fold(str(c.get("name") or "")) for c in channels]
            self._folded = (id(channels), len(channels), names)
        names = self._folded[2]
        return [
            c
            for c, name in zip(channels, names)
            if all(w in name for w in words)
            or str(ZapContext.number_of(c) or "").startswith(words[0])
        ]

    def _retry_failed(self) -> None:
        """A list that failed is fetched again when the phone opens it again."""
        node: Optional[NavNode] = self._section()
        if self._view is not None and self._view[0] == "guide":
            node = self._group_node(self._view[1]) or node
        if node is not None and self.nav.load_error(node):
            self.nav.retry(node)

    # ---- what the phone sees ---------------------------------------------------------------

    def _answer(self, page: int) -> tuple[dict, tuple]:
        if self._view is not None and self._view[0] == "groups":
            return self._groups_page(page)
        return self._channels_page(page)

    def _groups_page(self, page: int) -> tuple[dict, tuple]:
        """The groups; with a find, the groups it matches on the first page and the channels it
        finds (the desktop search's rules) a page at a time after them."""
        find = self._view[1] if self._view is not None else ""
        nodes = _matching(self._group_nodes(), fold(find.strip()))
        hits = search_index(self._live_index(), find) if find.strip() else []
        if hits:
            shown = nodes[:GROUPS_PAGE] if page == 0 else []
            pages = _pages(len(hits), FOUND_PAGE)
        else:
            shown = nodes[page * GROUPS_PAGE : (page + 1) * GROUPS_PAGE]
            pages = _pages(len(nodes), GROUPS_PAGE)
        found = [hit.item for hit in hits[page * FOUND_PAGE : (page + 1) * FOUND_PAGE]]
        section = self._section()
        error = self.nav.load_error(section)
        loading = not nodes and not error and self.nav.is_loading(section)
        playing = self._window.controller._channel
        rows = [self._channel_row(c, None, None, playing) for c in found]
        if found and not self._bare:
            self.providers = {provider_of(c) for c in found} - {""}
        message: dict = {
            "t": self._kinds[0],
            "find": find,
            "page": page,
            "pages": pages,
            "rows": [self._entry(node) for node in shown],
            "channels": rows,
            "found": len(hits),
            "loading": loading,
            # The providers' level above, with two or more; and which one these groups are.
            "pick": len(self._catalog.provider_names()) >= 2,
            "source": _source_title(self._catalog.source())[:TITLE_CHARS],
        }
        if not nodes and error:
            message["failed"] = True
        signature = (
            tuple((n.layer, n.key, n.title, n.count) for n in shown),
            tuple((r["name"], r.get("now")) for r in rows),
            pages,
            loading,
            bool(error),
        )
        return message, signature

    def _elsewhere(self, channels: list[dict], find: str) -> list[dict]:
        """The channels outside `channels` that the find finds among every channel."""
        listed = {(provider_of(c), item_key(c)) for c in channels}
        hits = search_index(self._live_index(), find)
        return [h.item for h in hits if (provider_of(h.item), item_key(h.item)) not in listed]

    def _live_index(self) -> SearchIndex:
        catalog = self._catalog
        version = catalog.content_version()
        kept = PhoneGuide._indexes.get(id(catalog))
        if kept is None or kept[0] != version:
            items = self.nav.all_live_items()
            names = [str(item.get("name") or "") for item in items]
            index = [
                (provider_of(item), "live", folded, name, item)
                for item, name, folded in zip(items, names, fold_all(names))
            ]
            kept = (version, index)
            if len(PhoneGuide._indexes) >= 4:  # catalogs gone (phones that left) give way
                PhoneGuide._indexes.pop(next(iter(PhoneGuide._indexes)))
            PhoneGuide._indexes[id(catalog)] = kept
        return kept[1]

    def _entry(self, node: NavNode) -> dict:
        title = node.title[:TITLE_CHARS]
        if node.layer == "header":
            return {"title": title, "header": True}
        entry: dict = {"id": self._key(node.key), "title": title}
        if node.count is not None:
            entry["count"] = node.count
        return entry

    def _channels_page(self, page: int) -> tuple[dict, tuple]:
        _kind, group, find = self._view if self._view is not None else ("guide", None, "")
        node = self._group_node(group)
        channels = self._channels(node) if node is not None else None
        error = self.nav.load_error(node if node is not None else self._section())
        if node is None and not error and not self.nav.is_loading(self._section()):
            channels = []  # no such group: nothing to list
        found = self._found_channels(channels, find) if channels is not None else []
        own = len(found)  # the group's own; any other channel the find finds follows them
        if channels is not None and find.strip():
            found = found + self._elsewhere(channels, find)
        start = page * GUIDE_PAGE
        shown = found[start : start + GUIDE_PAGE]
        playing = self._window.controller._channel
        rows = []
        for at, channel in enumerate(shown, start):
            if at < own:  # tuned within the group
                rows.append(self._channel_row(channel, group, channels, playing))
            else:  # found elsewhere: tuned on its own, as the desktop search's results
                rows.append(self._channel_row(channel, None, None, playing))
        shows = [(id(c), (r.get("now") or {}).get("title")) for c, r in zip(shown, rows)]
        if not self._bare:
            self.providers = {provider_of(c) for c in shown} - {""}
        message: dict = {
            "t": self._kinds[1],
            "list": self._key(group),
            "title": node.title[:TITLE_CHARS] if node is not None else "",
            "count": len(found),
            "find": find,
            "page": page,
            "pages": _pages(len(found), GUIDE_PAGE),
            "rows": rows,
            "loading": channels is None and not error,
        }
        if channels is None and error:
            message["failed"] = True
        signature = (group, message["pages"], message["loading"], bool(error), tuple(shows))
        return message, signature

    def _channel_row(
        self, channel: dict, group: Optional[str], channels: Optional[list], playing: Any
    ) -> dict:
        """A channel's row; a tap tunes it within `group` (its `channels`), or found by the
        find, as the desktop search's results do."""
        controller = self._window.controller
        row: dict[str, Any] = {
            "id": self.handles.issue("channel", (channel, group or "", channels or [])),
            "num": controller.channel_number(channel),
            "name": str(channel.get("name") or "")[:TITLE_CHARS],
            "playing": playing is not None and ZapContext.same(channel, playing),
        }
        if not self._bare:
            on, upcoming = controller.now_next(channel)
            row.update(now=programme_dict(on), next=programme_dict(upcoming))
        return row

    # ---- lists and programme guides landing later ------------------------------------------

    def _changed(self) -> None:
        if self._view is not None:
            self._settle.start()

    def _on_guide_loaded(self) -> None:
        self._changed()
        if self._timetable is not None:
            self.timetable(self._timetable)

    def _push(self) -> None:
        if self._view is None:
            return
        if self._view[0] == "guide" and self._view[1] is None and self._wanted is not None:
            # The lists arrived: the group asked for (or where the TV is) is listed now.
            group = self._group_of(self._wanted) if self._wanted else self._tv_group()
            if group is not None:
                by_position = not self._wanted
                self._wanted = None
                self._view = ("guide", group, self._view[2])
                self._anchor = self._playing_page(group) if by_position else 0
        message, signature = self._answer(self._anchor)
        if signature != self._shown:
            self._shown = signature
            self._send(message)


def _source_title(name: str) -> str:
    return translate(ALL_PROVIDERS_TITLE) if name == ALL_PROVIDERS else name


def _matching(nodes: list[NavNode], needle: str) -> list[NavNode]:
    """The groups whose title has `needle`, each provider's heading only above a match."""
    if not needle:
        return nodes
    found: list[NavNode] = []
    heading: Optional[NavNode] = None
    for node in nodes:
        if node.layer == "header":
            heading = node
        elif needle in fold(node.title):
            if heading is not None:
                found.append(heading)
                heading = None
            found.append(node)
    return found
