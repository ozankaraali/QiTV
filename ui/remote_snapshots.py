"""What the phone remote shows of QiTV, as plain dicts: what's on and the search's results (the
Guide's and the Library's pages: ui/remote_guide.py, ui/remote_library.py). GUI thread only.

Nothing a phone receives names a stream, a link or a credential: titles, numbers, times and
opaque handles only. A handle is a random id for something the phone may tap later (a channel, a
search hit, a title); QiTV keeps what it stands for, the most recent 2000.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
import hashlib
import secrets
from typing import Any, Optional

from services.epg_view import Programme
from services.remote.protocol import b64e

RESULTS_PER_GROUP = 30
_SECTIONS = ("live", "movies", "series")


class Handles:
    def __init__(self, limit: int = 2000) -> None:
        self._limit = limit
        self._items: OrderedDict[str, tuple[str, object]] = OrderedDict()

    def issue(self, kind: str, payload: object) -> str:
        handle = b64e(secrets.token_bytes(8))
        self._items[handle] = (kind, payload)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)
        return handle

    def get(self, handle: str) -> Optional[tuple[str, object]]:
        return self._items.get(handle)


def _stamp(when: Optional[datetime]) -> Optional[float]:
    return when.timestamp() if when is not None else None


TITLE_CHARS = 80  # a name or title as much as a phone's row shows: messages stay frame-sized


def stable_key(list_id: str) -> str:
    """A group's or genre's key for the phone: the same in every session, so a phone's keys
    hold across reconnects (and QiTV restarts), and naming nothing of the id."""
    return "g" + hashlib.sha256(list_id.encode("utf-8")).hexdigest()[:12]


def programme_dict(programme: Optional[Programme]) -> Optional[dict]:
    if programme is None:
        return None
    return {
        "title": programme.title[:TITLE_CHARS],
        "start": _stamp(programme.start),
        "stop": _stamp(programme.stop),
    }


def state(window: Any) -> dict:
    """What's on and where the TV is, for the now-playing card and the section buttons."""
    controller = window.controller
    library = window.screens.library_shown()
    current = controller._current
    channel = controller._channel
    now: Optional[dict] = None
    if current is not None and not current.live:
        session = controller.session
        now = {
            "kind": current.section if current.section in ("movies", "series") else "file",
            "name": str(current.title or "")[:TITLE_CHARS],
            "sub": str(current.subtitle or "")[:TITLE_CHARS],
            "pos_ms": _int(session, "time_ms"),
            "len_ms": _int(session, "length_ms"),
            "playing": _playing(session),  # the phone runs the clock itself while it plays
        }
    elif channel is not None:
        on, upcoming = controller.now_next(channel)
        behind = controller.behind_live()
        now = {
            "kind": "live",
            "num": controller.channel_number(channel),
            "name": str(channel.get("name") or "")[:TITLE_CHARS],
            "now": programme_dict(on),
            "next": programme_dict(upcoming),
            "behind_s": round(behind),
        }
    live = getattr(controller, "live", None)
    return {
        "t": "state",
        "context": window._key_context(),
        "section": (window.library_controller.section or "movies") if library else "live",
        "now": now,
        "volume": controller.main_level(),
        "muted": controller.main_muted(),
        "recording": bool(live is not None and live.recording()),
        "pip": controller.pip_shown(),
        "multiview": controller.multiview_shown(),
    }


def _int(session: Any, name: str) -> Optional[int]:
    try:
        value = getattr(session, name)()
    except Exception:  # a session between streams answers nothing useful
        return None
    return int(value) if isinstance(value, (int, float)) and value >= 0 else None


def _playing(session: Any) -> bool:
    try:
        return str(getattr(session.state(), "value", session.state())) == "playing"
    except Exception:
        return False


def results(window: Any, handles: Handles) -> dict:
    """The TV's search's hits, grouped by section, 30 a group."""
    library = window.library_controller
    return results_of(library.search_hits(), library.query, handles)


def results_of(hits: list, query: str, handles: Handles) -> dict:
    """Search hits (services.search's), grouped by section, 30 a group."""
    grouped: dict[str, list[dict]] = {section: [] for section in _SECTIONS}
    for hit in hits:
        group = grouped.get(hit.section)
        if group is None or len(group) >= RESULTS_PER_GROUP:
            continue
        title = str(hit.title or "")[:TITLE_CHARS]
        group.append({"id": handles.issue("hit", hit), "title": title, "sub": ""})
    return {
        "t": "results",
        "q": query,
        "groups": [{"kind": s, "items": grouped[s]} for s in _SECTIONS if grouped[s]],
    }
