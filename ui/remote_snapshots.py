"""What the phone remote shows of QiTV, as plain dicts: what's on and the search's results (the
Guide's and the Library's pages: ui/remote_guide.py, ui/remote_library.py). GUI thread only.

Nothing a phone receives names a stream, a link or a credential: titles, numbers, times and
opaque handles only. A handle is a random id for something the phone may tap later (a channel, a
search hit, a title); QiTV keeps what it stands for, the most recent 2000.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
import json
import secrets
from typing import Any, Optional

from services.epg_view import Programme
from services.remote.protocol import b64e

MAX_BYTES = 48_000  # a message stays well inside one frame (65535 bytes) with its seal
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


def programme_dict(programme: Optional[Programme]) -> Optional[dict]:
    if programme is None:
        return None
    return {
        "title": programme.title,
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
            "name": current.title,
            "sub": current.subtitle or "",
            "pos_ms": _int(session, "time_ms"),
            "len_ms": _int(session, "length_ms"),
            "playing": _playing(session),  # the phone runs the clock itself while it plays
        }
    elif channel is not None:
        on, upcoming = controller.now_next(channel)
        lag = controller._live_lag()
        now = {
            "kind": "live",
            "num": controller.channel_number(channel),
            "name": str(channel.get("name") or ""),
            "now": programme_dict(on),
            "next": programme_dict(upcoming),
            "behind_s": round(lag) if lag is not None and lag >= 3 else 0,
        }
    live = getattr(controller, "live", None)
    sessions = getattr(window, "sessions", None)
    return {
        "t": "state",
        "context": window._key_context(),
        "section": (window.library_controller.section or "movies") if library else "live",
        "now": now,
        "volume": controller.main_level(),
        "muted": controller.main_muted(),
        "recording": bool(live is not None and live.recording()),
        "pip": bool(sessions is not None and sessions.pip_open()),
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
    """The search's hits, grouped by section, 30 a group."""
    library = window.library_controller
    grouped: dict[str, list[dict]] = {section: [] for section in _SECTIONS}
    for hit in library.search_hits():
        group = grouped.get(hit.section)
        if group is None or len(group) >= RESULTS_PER_GROUP:
            continue
        group.append({"id": handles.issue("hit", hit), "title": hit.title, "sub": ""})
    return {
        "t": "results",
        "q": library.query,
        "groups": [{"kind": s, "items": grouped[s]} for s in _SECTIONS if grouped[s]],
    }


def fit_entries(message: dict, entries: list[dict], current: str) -> list[dict]:
    """As many of a picker's `entries` (a guide's groups, a Library's genres) as fit `message`,
    the one shown (`current`) always among them."""
    budget = MAX_BYTES - _size({**message, "entries": []})
    kept: list[dict] = []
    used = sum(_size(e) + 2 for e in entries if current and e.get("id") == current)
    for entry in entries:  # each one costs itself and its ", "
        mine = current and entry.get("id") == current
        size = 0 if mine else _size(entry) + 2
        if used + size > budget:
            continue
        used += size
        kept.append(entry)
    return kept


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
