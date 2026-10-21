"""A phone's Guide: the channel list's groups as the TV lists them, a page of channels at a time
with what's on and next, and a channel's timetable. GUI thread only.

Each phone reads the groups through its own NavModel (★ Favorites, ⟲ Recent, then the
provider's groups), so its browsing leaves the TV's channel list where it is. As in the Library,
groups travel as short keys, channels and programmes as handles.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Optional, cast

from PySide6.QtCore import QObject, QTimer

from services.catchup import programme_in_archive
from services.navigation import NavModel, NavNode, section_title
from services.text_fold import fold
from services.zapping import ZapContext
from ui.remote_snapshots import Handles, fit_entries, programme_dict

GUIDE_PAGE = 50
TIMETABLE_LIMIT = 60
TIMETABLE_BACK = timedelta(hours=12)  # how far back a timetable lists a channel's archive
TITLE_CHARS = 80
ABOUT_CHARS = 160  # a programme's description, as much as two lines on a phone show
SETTLE_MS = 250  # lists arriving together answer once


class PhoneGuide(QObject):
    """`send(message)` reaches this phone; `ask` and `timetable` answer at once, a list or a
    programme guide that lands later pushes the first page again when what it shows changed."""

    def __init__(self, window: Any, send: Callable[[dict], None], parent=None) -> None:
        super().__init__(parent)
        self._window = window
        self._send = send
        self.nav = NavModel(window.catalog, window.config_manager, self)
        self.nav.reset_to("live")
        self.handles = Handles()
        self._keys: dict[str, str] = {}  # group key → the phone's key
        self._ids: dict[str, str] = {}  # the phone's key → group key
        self._group: Optional[str] = None
        self._find = ""
        self._asked = False
        self._shown: Optional[tuple] = None  # what the first page showed, as last sent
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self._push)
        self.nav.changed.connect(self._changed)
        window.catalog.epgReady.connect(self._changed)

    def ask(self, key: str, page: int, find: str) -> None:
        self._asked = True
        self._find = find
        self._choose(self._ids.get(key), key == "")
        group = self._group_node()
        if page == 0 and group is not None and self.nav.load_error(group):
            self.nav.retry(group)  # asked again: a failed list is fetched again
        view = self._view(page)
        if page == 0:
            self._shown = _signature(view, self._window.controller.now_next)
        self._send(self._message(view))

    def timetable(self, handle: str) -> None:
        """`handle`'s channel: its programmes from the one on now (with an archive, from earlier,
        each a tap away), the one on now a tap from live."""
        found = self.handles.get(handle)
        if found is None or found[0] != "channel":
            return
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
        self._send(
            {
                "t": "timetable",
                "id": handle,
                "name": str(channel.get("name") or "")[:TITLE_CHARS],
                "num": controller.channel_number(channel),
                "rows": rows,
            }
        )

    # ---- the groups ------------------------------------------------------------------------

    def _section(self) -> NavNode:
        return NavNode("section", "live", section_title("live"), "live")

    def _group_nodes(self) -> list[NavNode]:
        """The section's groups and, with providers mixed, their providers' headings."""
        nodes = self.nav.children(self._section()) or []
        return [n for n in nodes if n.layer in ("group", "header")]

    def _group_node(self) -> Optional[NavNode]:
        nodes = self._group_nodes()
        return next((n for n in nodes if n.layer == "group" and n.key == self._group), None)

    def _choose(self, wanted: Optional[str], fresh: bool) -> None:
        """The group asked for when it is listed; else (or asked afresh) the one the TV zaps
        through, else the one shown, else the first."""
        listed = [n.key for n in self._group_nodes() if n.layer == "group"]
        if wanted in listed:
            self._group = wanted
            return
        zapping = self._window.controller.zap.context.group_key
        if (fresh or self._group not in listed) and zapping in listed:
            self._group = zapping
        elif self._group not in listed:
            self._group = listed[0] if listed else None

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

    def _matching(self, channels: list[dict]) -> list[dict]:
        needle = fold(self._find.strip())
        if not needle:
            return channels
        return [
            c
            for c in channels
            if needle in fold(str(c.get("name") or ""))
            or str(ZapContext.number_of(c) or "").startswith(needle)
        ]

    # ---- what the phone sees ---------------------------------------------------------------

    def _view(self, page: int) -> dict:
        group = self._group_node()
        channels = self._channels(group) if group is not None else None
        error = self.nav.load_error(group if group is not None else self._section())
        if group is None and not error and not self.nav.is_loading(self._section()):
            channels = []  # no groups at all: nothing to list
        matches = self._matching(channels) if channels is not None else []
        return {
            "group": self._group if group is not None else None,
            "groups": self._group_nodes() if page == 0 else None,
            "page": page,
            "pages": max(1, -(-len(matches) // GUIDE_PAGE)),
            "rows": matches[page * GUIDE_PAGE : (page + 1) * GUIDE_PAGE],
            "channels": channels or [],
            "loading": channels is None and not error,
            "failed": channels is None and bool(error),
        }

    def _message(self, view: dict) -> dict:
        controller = self._window.controller
        playing = controller._channel
        rows = []
        for channel in view["rows"]:
            on, upcoming = controller.now_next(channel)
            payload = (channel, view["group"], view["channels"])
            rows.append(
                {
                    "id": self.handles.issue("channel", payload),
                    "num": controller.channel_number(channel),
                    "name": str(channel.get("name") or "")[:TITLE_CHARS],
                    "now": programme_dict(on),
                    "next": programme_dict(upcoming),
                    "playing": playing is not None and ZapContext.same(channel, playing),
                }
            )
        message: dict = {
            "t": "guide",
            "list": self._key(view["group"]),
            "find": self._find,
            "page": view["page"],
            "pages": view["pages"],
            "rows": rows,
            "loading": view["loading"],
        }
        if view["failed"]:
            message["failed"] = True
        if view["groups"] is not None:
            groups = [self._entry(node) for node in view["groups"]]
            message["groups"] = fit_entries(message, groups, message["list"])
        return message

    def _entry(self, node: NavNode) -> dict:
        title = node.title[:TITLE_CHARS]
        if node.layer == "header":
            return {"title": title, "header": True}
        entry: dict = {"id": self._key(node.key), "title": title}
        if node.count is not None:
            entry["count"] = node.count
        return entry

    # ---- lists and programme guides landing later ------------------------------------------

    def _changed(self) -> None:
        if self._asked:
            self._settle.start()

    def _push(self) -> None:
        self._choose(None, False)
        view = self._view(0)
        signature = _signature(view, self._window.controller.now_next)
        if signature != self._shown:
            self._shown = signature
            self._send(self._message(view))


def _signature(view: dict, now_next: Callable[[dict], tuple]) -> tuple:
    """What the phone sees of a first page: a change of it is pushed (a programme guide arriving
    included), the clock moving on is not."""
    groups = tuple((n.layer, n.key, n.title, n.count) for n in view["groups"] or ())
    flags = (view["group"], view["pages"], view["loading"], view["failed"])
    shows = tuple(
        (id(channel), tuple(p.title if p else None for p in now_next(channel)))
        for channel in view["rows"]
    )
    return flags + (groups, shows)
