"""A phone's Guide: the channel list's groups as the TV lists them, a group's channels a page at
a time with what's on and next, and a channel's timetable. GUI thread only.

Each phone reads the groups through its own NavModel (★ Favorites, ⟲ Recent, then the
provider's groups), so its browsing leaves the TV's channel list where it is; it opens where the
TV's list is, on the page of the channel playing. The find box searches the level shown, the
whole of it. Groups travel as short keys, channels and programmes as handles.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Optional, cast

from PySide6.QtCore import QObject, QTimer

from services.catchup import programme_in_archive
from services.navigation import NavModel, NavNode, section_title
from services.provider_ids import provider_of
from services.text_fold import fold
from services.zapping import ZapContext
from ui.remote_snapshots import Handles, programme_dict

GROUPS_PAGE = 100
GUIDE_PAGE = 50
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

    def __init__(self, window: Any, send: Callable[[dict], None], parent=None) -> None:
        super().__init__(parent)
        self._window = window
        self._send = send
        self.nav = NavModel(window.catalog, window.config_manager, self)
        self.nav.reset_to("live")
        self.handles = Handles()
        self.providers: set[str] = set()  # those of the channels shown: their guides load
        self._keys: dict[str, str] = {}  # group key → the phone's key
        self._ids: dict[str, str] = {}  # the phone's key → group key
        self._view: Optional[tuple] = None  # ("groups", find) or ("guide", group, find)
        self._anchor = 0  # the page the view opened on: the one sent again
        self._shown: Optional[tuple] = None  # what that page showed, as last sent
        self._timetable: Optional[str] = None  # the channel whose timetable was asked last
        self._folded: tuple[int, int, list[str]] = (0, -1, [])  # a list's names, folded
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self._push)
        self.nav.changed.connect(self._changed)
        window.catalog.epgReady.connect(self._on_guide_loaded)

    # ---- the phone's asks ------------------------------------------------------------------

    def groups(self, page: int, find: str) -> None:
        self._ask(("groups", find), page)

    def channels(self, key: str, page: int, find: str) -> None:
        """A group's channels; `key` "" is the group the TV's channel list is in, on the page of
        the channel playing."""
        if key:
            group: Optional[str] = self._ids.get(key)
        else:
            group = self._tv_group()
            page = self._playing_page(group)
        self._ask(("guide", group, find), page)

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

    def _ask(self, view: tuple, page: int) -> None:
        if view != self._view:
            self._view, self._anchor = view, page
        if page == self._anchor:
            self._retry_failed()
        message, signature = self._answer(page)
        if page == self._anchor:
            self._shown = signature
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
        if group not in self._keys:
            key = f"g{len(self._keys) + 1}"
            self._keys[group], self._ids[key] = key, group
        return self._keys[group]

    def _channels(self, node: NavNode) -> Optional[list[dict]]:
        nodes = self.nav.children(node)
        if nodes is None:
            return None
        items = getattr(nodes, "items", None)  # a long list's dicts, without making its rows
        if isinstance(items, list):
            return items
        return [n.payload for n in nodes if isinstance(n.payload, dict)]

    def _found_channels(self, channels: list[dict], find: str) -> list[dict]:
        needle = fold(find.strip())
        if not needle:
            return channels
        if self._folded[:2] != (id(channels), len(channels)):
            names = [fold(str(c.get("name") or "")) for c in channels]
            self._folded = (id(channels), len(channels), names)
        names = self._folded[2]
        return [
            c
            for c, name in zip(channels, names)
            if needle in name or str(ZapContext.number_of(c) or "").startswith(needle)
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
        find = self._view[1] if self._view is not None else ""
        nodes = _matching(self._group_nodes(), fold(find.strip()))
        section = self._section()
        error = self.nav.load_error(section)
        loading = not nodes and not error and self.nav.is_loading(section)
        shown = nodes[page * GROUPS_PAGE : (page + 1) * GROUPS_PAGE]
        message: dict = {
            "t": "groups",
            "find": find,
            "page": page,
            "pages": _pages(len(nodes), GROUPS_PAGE),
            "rows": [self._entry(node) for node in shown],
            "loading": loading,
        }
        if not nodes and error:
            message["failed"] = True
        signature = (
            tuple((n.layer, n.key, n.title, n.count) for n in shown),
            message["pages"],
            loading,
            bool(error),
        )
        return message, signature

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
        shown = found[page * GUIDE_PAGE : (page + 1) * GUIDE_PAGE]
        controller = self._window.controller
        playing = controller._channel
        rows, shows = [], []
        for channel in shown:
            on, upcoming = controller.now_next(channel)
            rows.append(
                {
                    "id": self.handles.issue("channel", (channel, group or "", channels or [])),
                    "num": controller.channel_number(channel),
                    "name": str(channel.get("name") or "")[:TITLE_CHARS],
                    "now": programme_dict(on),
                    "next": programme_dict(upcoming),
                    "playing": playing is not None and ZapContext.same(channel, playing),
                }
            )
            shows.append((id(channel), on.title if on else None))
        self.providers = {provider_of(c) for c in shown} - {""}
        message: dict = {
            "t": "guide",
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
        message, signature = self._answer(self._anchor)
        if signature != self._shown:
            self._shown = signature
            self._send(message)


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
