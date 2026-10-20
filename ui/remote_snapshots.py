"""What the phone remote shows of QiTV, as plain dicts: what's on, a guide page, the search's
results, the Library's shelves. GUI thread only (they read the controller and the catalog).

Nothing a phone receives names a stream, a link or a credential: titles, numbers, times and
opaque handles only. A handle is a random id for something the phone may tap later (a channel, a
search hit, a title); QiTV keeps what it stands for, the most recent 2000.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
import math
import secrets
from typing import Any, Optional

from services.epg_view import Programme
from services.library import continue_items, title_of
from services.navigation import FAVORITES_GROUP, RECENT_GROUP, NavNode
from services.remote.protocol import b64e
from services.watch_state import WatchState
from services.zapping import ZapContext

GUIDE_PAGE = 50
RESULTS_PER_GROUP = 30
SHELF = 30
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


def _programme(programme: Optional[Programme]) -> Optional[dict]:
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
            "now": _programme(on),
            "next": _programme(upcoming),
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


def _guide_channels(window: Any, list_name: str) -> list[dict]:
    if list_name == "all":
        return list(window.controller.zap.context.channels or [])
    key = FAVORITES_GROUP if list_name == "favorites" else RECENT_GROUP
    nodes = window.nav.children(NavNode("group", key, "")) or []
    return [node.payload for node in nodes if isinstance(node.payload, dict)]


def guide_page(window: Any, list_name: str, page: int, handles: Handles) -> dict:
    """A page of the channel list the TV is on (or Favorites, or Recent) with now and next."""
    controller = window.controller
    channels = _guide_channels(window, list_name)
    playing = controller._channel
    rows = []
    for channel in channels[page * GUIDE_PAGE : (page + 1) * GUIDE_PAGE]:
        on, upcoming = controller.now_next(channel)
        rows.append(
            {
                "id": handles.issue("channel", channel),
                "num": controller.channel_number(channel),
                "name": str(channel.get("name") or ""),
                "now": _programme(on),
                "next": _programme(upcoming),
                "playing": playing is not None and ZapContext.same(channel, playing),
            }
        )
    return {
        "t": "guide",
        "list": list_name,
        "page": page,
        "pages": max(1, math.ceil(len(channels) / GUIDE_PAGE)),
        "rows": rows,
    }


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


def library_lists(window: Any, handles: Handles) -> dict:
    """Continue watching (films and episodes) and My list of the Library's section shown."""
    config = window.config_manager
    watch = window.controller.watch
    providers = window.catalog.provider_names()
    positions = config.playback_positions or {}
    shelf = []
    for kind in ("movie", "episode"):
        for item in continue_items(config, watch, providers, kind):
            cid = WatchState.content_id(_provider(item), item, item_type=kind)
            place = positions.get(cid) or {}
            length = place.get("duration") or 0
            shelf.append(
                {
                    "id": handles.issue(kind, item),
                    "title": title_of(item),
                    "sub": str(item.get("year") or item.get("series_name") or ""),
                    "progress": round(place.get("position", 0) / length, 3) if length else 0.0,
                }
            )
    model = window.screens.library_model
    section = model.section if model.section in ("movies", "series") else ""
    mine = []
    if section:
        kind = "series" if section == "series" else "movie"
        for item in model.my_list()[:SHELF]:
            mine.append(
                {
                    "id": handles.issue(kind, item),
                    "title": title_of(item),
                    "sub": str(item.get("year") or ""),
                }
            )
    return {"t": "library", "continue": shelf[:SHELF], "mylist": mine, "mylist_section": section}


def _provider(item: dict) -> str:
    from services.provider_ids import provider_of

    return provider_of(item) or ""
