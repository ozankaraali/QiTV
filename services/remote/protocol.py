"""The phone remote's wire vocabulary: constants, encodings, the buttons and the messages.

Pure: no Qt, no network. The buttons' meaning for the TV (a key or an action) is in
ui/remote_buttons.py; here each button id only says whether a hold repeats, which button undoes
an overshoot, and how it is routed. Phone messages are checked against a fixed allowlist before
anything else sees them.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import re
from typing import Literal, Optional

RELAY_URL = "https://remote.qitv.app"
DEFAULT_PORT = 5384
PORTS = range(DEFAULT_PORT, DEFAULT_PORT + 10)
PROTO = 1
STORE_VERSION = 1  # the config's `remote` object (services/remote/pairing.py)
CODE_ROTATE_S = 60.0  # the pairing code on the TV is replaced this often

DELAY_MS = 400  # a held button's first repeat
INTERVAL_MS = 40  # then 25 a second
KEEPALIVE_MS = 200  # the phone's "still held" while a button is down
SILENCE_MS = 600  # no word from a holding phone for this long ends the hold

MAX_FRAME = 65535
MAX_MESSAGE = MAX_FRAME - 16  # a message and its 16-byte seal fit one frame (the relay's)
MAX_OPENING = 256
MAX_SESSIONS = 8
MAX_PENDING = 16
HANDSHAKE_S = 5.0
MAX_RATE = 60  # phone messages in any one second
MAX_QUERY = 100
MAX_HANDLE = 32
MAX_STEPS = 100_000

_B64 = re.compile(r"[A-Za-z0-9_-]*")


def b64e(data: bytes) -> str:
    """base64url without padding, as every id, key and code travels."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    """The inverse of b64e; raises ValueError for anything that isn't base64url."""
    if not isinstance(text, str) or not _B64.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def steps_for(held_ms: int) -> int:
    """How many presses a hold of `held_ms` makes: one at once, then from 400 ms every 40 ms.

    The page counts the same way, so a release can say how many steps the phone saw."""
    if held_ms < DELAY_MS:
        return 1
    return 2 + (held_ms - DELAY_MS) // INTERVAL_MS


# ---- buttons ---------------------------------------------------------------------------------

Route = Literal["keys", "stay", "player", "screen"]


@dataclass(frozen=True)
class ButtonMeta:
    route: Route
    repeat: bool = False
    reverse: Optional[str] = None  # the button that undoes one overshooting step


def _meta() -> dict[str, ButtonMeta]:
    table: dict[str, ButtonMeta] = {
        "up": ButtonMeta("keys", True, "down"),
        "down": ButtonMeta("keys", True, "up"),
        "left": ButtonMeta("keys", True, "right"),
        "right": ButtonMeta("keys", True, "left"),
        "ok": ButtonMeta("keys"),
        "back": ButtonMeta("keys"),
        "previous": ButtonMeta("keys"),
        "search": ButtonMeta("stay"),
        "fullscreen": ButtonMeta("stay"),
        "volup": ButtonMeta("stay", True),
        "voldown": ButtonMeta("stay", True),
        "mute": ButtonMeta("stay"),
        "chup": ButtonMeta("player", True),
        "chdown": ButtonMeta("player", True),
        "rew": ButtonMeta("player", True),
        "fwd": ButtonMeta("player", True),
        "tv": ButtonMeta("screen"),
        "movies": ButtonMeta("screen"),
        "series": ButtonMeta("screen"),
    }
    for name in ("pp", "live", "info", "guide", "record", "pip", "swap", "multiview", "picture"):
        table[name] = ButtonMeta("player")
    for digit in range(10):
        table[f"d{digit}"] = ButtonMeta("keys")
    return table


BUTTON_META = _meta()


# ---- the opening frame -----------------------------------------------------------------------


@dataclass(frozen=True)
class Opening:
    mode: Literal["pair", "kk", "ticket"]
    key_id: str = ""
    ticket_id: str = ""


def parse_opening(text: str) -> Optional[Opening]:
    """The connection's first frame, `{"v":1,"mode":…}`; None when it isn't one."""
    if not isinstance(text, str) or len(text) > MAX_OPENING:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("v") != PROTO:
        return None
    mode = obj.get("mode")
    if mode == "pair":
        return Opening("pair")
    if mode == "kk" and _short_id(obj.get("key")):
        return Opening("kk", key_id=obj["key"])
    if mode == "ticket" and _short_id(obj.get("ticket")):
        return Opening("ticket", ticket_id=obj["ticket"])
    return None


def _short_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_HANDLE


# ---- phone messages --------------------------------------------------------------------------

LIBRARY_SECTIONS = ("movies", "series")


def _int(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_phone_message(obj: object) -> Optional[dict]:
    """A phone's message, normalised, or None when it isn't on the allowlist."""
    if not isinstance(obj, dict):
        return None
    t = obj.get("t")
    if t in ("held", "where", "forget", "totv"):
        return {"t": t}
    if t == "pos":  # how far the phone got in a film or an episode it plays
        ms, length = _int(obj.get("ms")), _int(obj.get("len"))
        if ms is not None and length is not None and 0 <= ms <= MAX_POS and 0 < length <= MAX_POS:
            return {"t": "pos", "ms": ms, "len": length}
        return None
    if t == "watch":  # what the TV plays, or a channel of the phone's list by its handle
        if "id" not in obj:
            return {"t": "watch"}
        return {"t": "watch", "id": obj["id"]} if _short_id(obj.get("id")) else None
    button = obj.get("b")
    known = isinstance(button, str) and button in BUTTON_META  # a list or dict isn't hashable
    if t == "down" and known:
        return {"t": "down", "b": obj["b"]}
    if t == "up" and known:
        n = _int(obj.get("n"))
        if n is not None and 1 <= n <= MAX_STEPS:
            return {"t": "up", "b": obj["b"], "n": n}
        return None
    if t == "query" and isinstance(obj.get("s"), str):  # "tv": the TV's search shows it too
        return {"t": "query", "s": obj["s"][:MAX_QUERY], "tv": obj.get("tv") is True}
    if t == "open" and _short_id(obj.get("id")):
        return {"t": "open", "id": obj["id"]}
    if t == "get":
        what = obj.get("what")
        if what in ("library", "genres"):
            return _library_ask(obj)
        if what in ("guide", "groups", "channels", "chgroups", "sources", "chsources"):
            return _guide_ask(obj)
        if what == "timetable" and _short_id(obj.get("id")):
            return {"t": "get", "what": "timetable", "id": obj["id"]}
    return None


MAX_ASK = 2**31 - 1  # an ask's number (see _numbered)
MAX_POS = 2 * 24 * 3600 * 1000  # a title's place or length, in ms: two days at most


def _page_and_find(obj: dict) -> Optional[tuple[int, str]]:
    page, find = _int(obj.get("page", 0)), obj.get("find", "")
    if page is None or page < 0 or not isinstance(find, str):
        return None
    return page, find[:MAX_QUERY]


def _numbered(obj: dict, ask: dict) -> Optional[dict]:
    """`ask` with the phone's number for it ("n"), which its answer carries back: the phone
    keeps only the answer to its latest ask. None when the number is malformed."""
    if "n" not in obj:
        return ask
    n = _int(obj["n"])
    return {**ask, "n": n} if n is not None and 0 <= n <= MAX_ASK else None


def _guide_ask(obj: dict) -> Optional[dict]:
    """A page of the Guide's groups, or of a group's channels (its key; "": where the TV's
    channel list is), with what the phone's find box holds; or the providers above the groups.
    The Remote tab's channel list asks the same as "chgroups", "channels" and "chsources"."""
    what = obj["what"]
    if what in ("sources", "chsources"):  # the providers, above the groups (two or more)
        return _numbered(obj, {"t": "get", "what": what})
    asked = _page_and_find(obj)
    if asked is None:
        return None
    page, find = asked
    if what in ("groups", "chgroups"):
        ask = {"t": "get", "what": what, "page": page, "find": find}
        if "source" in obj:  # the provider (or all of them) the phone's lists show from now
            if not _short_id(obj["source"]):
                return None
            ask["source"] = obj["source"]
        return _numbered(obj, ask)
    group = obj.get("list", "")
    if not (group == "" or _short_id(group)):
        return None
    ask = {"t": "get", "what": what, "list": group, "page": page, "find": find}
    return _numbered(obj, ask)


def _library_ask(obj: dict) -> Optional[dict]:
    """A page of a Library section's genres, or of a genre's titles (its key; "": the first
    shelf), with what the phone's find box holds."""
    asked, section = _page_and_find(obj), obj.get("section", "movies")
    if asked is None or not isinstance(section, str) or section not in LIBRARY_SECTIONS:
        return None
    page, find = asked
    if obj["what"] == "genres":
        ask = {"t": "get", "what": "genres", "section": section, "page": page, "find": find}
        return _numbered(obj, ask)
    genre = obj.get("genre", "")
    if not (genre == "" or _short_id(genre)):
        return None
    message = {"t": "get", "what": "library", "section": section, "genre": genre}
    return _numbered(obj, {**message, "page": page, "find": find})


# ---- events (the hub thread → the GUI thread) ------------------------------------------------


@dataclass(frozen=True)
class Paired:
    sid: str
    device_id: str
    name: str


@dataclass(frozen=True)
class Opened:
    sid: str
    device_id: str
    door: str


@dataclass(frozen=True)
class Closed:
    sid: str
    device_id: Optional[str]


@dataclass(frozen=True)
class Press:
    sid: str
    button: str
    down: bool
    steps: int


@dataclass(frozen=True)
class Held:
    sid: str


@dataclass(frozen=True)
class Query:
    sid: str
    text: str
    tv: bool = False  # Show on TV: the TV's own search shows it; else the phone's alone


@dataclass(frozen=True)
class WatchHere:
    """The phone asks to play in its own player what the TV plays, or the channel `handle`
    (one of its list's) when given."""

    sid: str
    handle: str = ""


@dataclass(frozen=True)
class Position:
    """How far the phone got in the film or episode it plays, of how long (ms)."""

    sid: str
    position_ms: int
    length_ms: int


@dataclass(frozen=True)
class ToTv:
    """The phone hands what it plays back to the TV."""

    sid: str


@dataclass(frozen=True)
class OpenItem:
    sid: str
    handle: str


@dataclass(frozen=True)
class Get:
    sid: str
    what: str
    list: str
    page: int
    section: str = ""  # the Library's: movies or series
    genre: str = ""  # the Library's: a genre's key, or "" for QiTV's first shelf
    find: str = ""  # the Guide's find box
    handle: str = ""  # the timetable's channel
    n: int = -1  # the phone's number for the ask, which the answer carries back (-1: none)
    source: str = ""  # the groups': the provider the phone's lists show from now on (its key)


@dataclass(frozen=True)
class Forgot:
    sid: str
    device_id: str


@dataclass(frozen=True)
class StoreSaved:
    data: dict


@dataclass(frozen=True)
class Code:
    url: str  # the pairing QR's address; "" when the pairing screen is closed


@dataclass(frozen=True)
class Status:
    home_url: str
    relay: str  # "off", "connecting", "online", "resting" or "error"
