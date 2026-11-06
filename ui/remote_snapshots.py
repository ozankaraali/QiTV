"""What the phone remote shows of QiTV, as plain dicts: what's on, the large picture's sound and
subtitles (2.9) and the search's results (the Guide's and the Library's pages:
ui/remote_guide.py, ui/remote_library.py). GUI thread only.

Nothing a phone receives names a stream, a link or a credential: titles, numbers, times and
opaque handles only. A handle is a random id for something the phone may tap later (a channel, a
search hit, a title); QiTV keeps what it stands for, the most recent 2000.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
import hashlib
import secrets
import time
from typing import Any, Optional

from services.epg_view import Programme
from services.remote.protocol import b64e
from ui.overlays.painting import badge_color

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


def tint(name: str) -> str:
    """2.9.7: the colour the TV draws `name`'s generated logo tile in ("#rrggbb"): the phone's
    tile for it is the same (`ui.overlays.painting.badge_color`)."""
    return str(badge_color(name).name())


def _stamp(when: Optional[datetime]) -> Optional[float]:
    return when.timestamp() if when is not None else None


TITLE_CHARS = 80  # a name or title as much as a phone's row shows: messages stay frame-sized


def stable_key(list_id: str) -> str:
    """A group's or genre's key for the phone: the same in every session, so a phone's keys
    hold across reconnects (and QiTV restarts), and naming nothing of the id."""
    return "g" + hashlib.sha256(list_id.encode("utf-8")).hexdigest()[:12]


def find_page(
    page: int, groups: int, found: int, group_size: int, found_size: int
) -> tuple[slice, slice, int]:
    """A find's `page`: the groups it matched `group_size` a page, then what it found
    `found_size` a page, the page where the groups end beginning them (the first, with a page
    of groups or fewer). The groups' slice, the found's slice, and how many pages."""
    group_pages = -(-groups // group_size)
    first = max(0, group_pages - 1)  # the page the found begin on
    pages = max(1, group_pages, first + -(-found // found_size))
    at = page - first
    hits = slice(at * found_size, (at + 1) * found_size) if at >= 0 else slice(0, 0)
    return slice(page * group_size, (page + 1) * group_size), hits, pages


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
            "tint": tint(str(current.title or "")),
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
            "tint": tint(str(channel.get("name") or "")),  # 2.9.7: its tile's, as the TV's
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
        # 2.6: when the channel come back to was left ("20:10") while Resume is offered
        "back": controller.back_label(),
        "pip": controller.pip_shown(),
        "multiview": controller.multiview_shown(),
        # 2.6: what records in the background (the big picture's own is `recording`): since
        # when, in Unix seconds, so the phone counts the time itself
        "rec": _rec(controller),
    }


MAX_TRACKS = 40  # a sheet's tracks at most: messages stay frame-sized


def tracks(window: Any, of: int) -> dict:
    """The large picture's sound and subtitle tracks and its AI captions, as the desktop's
    Sound and Subtitles panels name them (ui/track_controls.py), for the phone's sheets (2.9).
    `of` is the number QiTV gave what plays large: a pick carries it back, so one made in the
    list of what played before is not for what plays now."""
    controls = window.controller.track_controls()
    message: dict = {"t": "tracks", "of": of, "playing": False, "sound": [], "subs": []}
    if controls is None or not controls.offers():
        return message
    message["playing"] = True
    for key, choices in (
        ("sound", controls.sound_choices()),
        ("subs", controls.subtitle_choices()),
    ):
        message[key] = [
            {"id": track, "name": str(name)[:TITLE_CHARS], "on": bool(on)}
            for track, name, on in choices[:MAX_TRACKS]
        ]
    captions = controls.captions_choice()
    if captions is not None:  # AI captions can be offered for what plays
        ai: dict = {
            "on": captions.on,
            "going": captions.going,  # the model downloads: they are on their way
            "note": captions.status[:TITLE_CHARS],  # "Downloading 40%", "Catching up…"
        }
        if captions.on:  # their Language row, and its list
            ai["lang"] = captions.language
            ai["lang_note"] = captions.language_text[:TITLE_CHARS]  # "Türkçe · automatic"
            ai["langs"] = [{"id": name, "name": own} for name, own in captions.languages]
        if captions.models:  # their row's ‹ speech model ›, and its steps in order
            ai["model"] = captions.model
            ai["models"] = [
                {"id": name, "name": text[:TITLE_CHARS]} for name, text in captions.models
            ]
        message["ai"] = ai
    return message


def _rec(controller: Any) -> list[dict]:
    started = time.time() - time.monotonic()  # LiveSession's clock is the monotonic one
    return [
        {"id": slot, "name": name[:TITLE_CHARS], "since": round(started + since)}
        for slot, name, since in controller.rec_list()
    ]


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
