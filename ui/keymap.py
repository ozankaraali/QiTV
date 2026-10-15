"""Key presses to TV actions (spec §5.3). Pure: no widgets, no state."""

from __future__ import annotations

from enum import Enum
import sys
from typing import Any

from PySide6.QtCore import Qt


class Action(str, Enum):
    CH_NEXT = "ch_next"
    CH_PREV = "ch_prev"
    DIGIT = "digit"
    PREVIOUS = "previous"
    OK = "ok"  # Enter/Return: the column's highlight while it is open, else the drawer
    OPEN_DRAWER = "open_drawer"  # the drawer itself (menu, click)
    SEEK_BACK = "seek_back"
    SEEK_FWD = "seek_fwd"
    PLAY_PAUSE = "play_pause"
    GO_LIVE = "go_live"
    GUIDE = "guide"
    INFO = "info"
    FULLSCREEN = "fullscreen"
    MUTE = "mute"
    VOL_UP = "vol_up"
    VOL_DOWN = "vol_down"
    ESCAPE = "escape"
    MINI_PLAYER = "mini_player"
    MEDIA_NEXT = "media_next"  # next channel when live, +10 s on demand
    MEDIA_PREV = "media_prev"  # previous channel when live, -10 s on demand
    NAV_UP = "nav_up"
    NAV_DOWN = "nav_down"
    NAV_PAGE_UP = "nav_page_up"
    NAV_PAGE_DOWN = "nav_page_down"
    NAV_LEFT = "nav_left"
    NAV_RIGHT = "nav_right"
    NAV_ACTIVATE = "nav_activate"
    NAV_TEXT = "nav_text"
    NAV_BACKSPACE = "nav_backspace"
    NAV_HOME = "nav_home"
    NAV_END = "nav_end"
    SEARCH = "search"
    FAVORITE = "favorite"
    LIBRARY = "library"
    OPEN_FILE = "open_file"


CONTEXTS = ("player", "drawer", "library", "guide")


_K = Qt.Key
_SHIFT = Qt.KeyboardModifier.ShiftModifier.value
_CTRL = Qt.KeyboardModifier.ControlModifier.value  # ⌘ on macOS
_ALT = Qt.KeyboardModifier.AltModifier.value  # Option on macOS
_META = Qt.KeyboardModifier.MetaModifier.value  # Control on macOS

# Keys that act the same whatever modifier is held (macOS adds Keypad to arrows).
_DRAWER_KEYS = {
    _K.Key_Up.value: Action.NAV_UP,
    _K.Key_Down.value: Action.NAV_DOWN,
    _K.Key_PageUp.value: Action.NAV_PAGE_UP,
    _K.Key_PageDown.value: Action.NAV_PAGE_DOWN,
    _K.Key_Left.value: Action.NAV_LEFT,
    _K.Key_Right.value: Action.NAV_RIGHT,
    _K.Key_Return.value: Action.NAV_ACTIVATE,
    _K.Key_Enter.value: Action.NAV_ACTIVATE,
    _K.Key_Backspace.value: Action.NAV_BACKSPACE,
    _K.Key_Escape.value: Action.ESCAPE,
}
_PLAYER_KEYS = {
    _K.Key_Up.value: Action.CH_PREV,
    _K.Key_PageUp.value: Action.CH_PREV,
    _K.Key_Down.value: Action.CH_NEXT,
    _K.Key_PageDown.value: Action.CH_NEXT,
    _K.Key_Backspace.value: Action.PREVIOUS,
    _K.Key_Return.value: Action.OK,
    _K.Key_Enter.value: Action.OK,
    _K.Key_Left.value: Action.SEEK_BACK,
    _K.Key_Right.value: Action.SEEK_FWD,
    _K.Key_Escape.value: Action.ESCAPE,
}
# Character keys: only plain or with Shift (Ctrl/Alt/Meta combinations are not ours).
_PLAYER_CHARS = {
    _K.Key_Space.value: Action.PLAY_PAUSE,
    _K.Key_L.value: Action.GO_LIVE,
    _K.Key_G.value: Action.GUIDE,
    _K.Key_I.value: Action.INFO,
    _K.Key_F.value: Action.FULLSCREEN,
    _K.Key_M.value: Action.MUTE,
    _K.Key_Plus.value: Action.VOL_UP,
    _K.Key_Equal.value: Action.VOL_UP,  # the unshifted "+" key on US layouts
    _K.Key_Minus.value: Action.VOL_DOWN,
}
# Remote channel keys (drawer closed, like the arrows they stand in for).
_CHANNEL_KEYS = {
    _K.Key_ChannelUp.value: Action.CH_NEXT,
    _K.Key_ChannelDown.value: Action.CH_PREV,
}
# Media keys never type, so they work with the drawer open or closed.
_MEDIA_KEYS = {
    _K.Key_MediaTogglePlayPause.value: Action.PLAY_PAUSE,
    _K.Key_MediaPlay.value: Action.PLAY_PAUSE,
    _K.Key_MediaPause.value: Action.PLAY_PAUSE,
    _K.Key_MediaNext.value: Action.MEDIA_NEXT,
    _K.Key_MediaPrevious.value: Action.MEDIA_PREV,
}
# Shortcuts that work with the drawer open or closed (and in the guide).
_CTRL_KEYS = {
    _K.Key_Space.value: Action.PLAY_PAUSE,
    _K.Key_F.value: Action.FULLSCREEN,
    _K.Key_D.value: Action.FAVORITE,
}
# The guide (spec §5.1): the drawer's keys but Backspace, digits (a channel number to jump
# to) and Ch± (one row), G closes it, and the playback keys keep working; nothing types.
_GUIDE_KEYS = {k: a for k, a in _DRAWER_KEYS.items() if a is not Action.NAV_BACKSPACE}
_GUIDE_CHARS = {
    key: _PLAYER_CHARS[key]
    for key in (
        _K.Key_Space.value,
        _K.Key_G.value,
        _K.Key_F.value,
        _K.Key_M.value,
        _K.Key_Plus.value,
        _K.Key_Equal.value,
        _K.Key_Minus.value,
    )
}
# The Library (spec §5.4): the drawer's keys plus Home/End. It types like the drawer, so "/"
# and Ctrl+F search and Ctrl+D toggles My list; fullscreen moves to Ctrl+Meta+F (macOS ⌃⌘F)
# and F11 because Ctrl+F searches here.
_LIBRARY_KEYS = {
    **_DRAWER_KEYS,
    _K.Key_Home.value: Action.NAV_HOME,
    _K.Key_End.value: Action.NAV_END,
    _K.Key_F11.value: Action.FULLSCREEN,
}
_LIBRARY_CTRL_KEYS = {_K.Key_F.value: Action.SEARCH, _K.Key_D.value: Action.FAVORITE}
_DIGITS = "0123456789"


def as_int(value: Any) -> int:
    """A Qt key or modifier flag (enum or int, None for none) as a plain int."""
    return int(value.value) if hasattr(value, "value") else int(value or 0)


def resolve(
    key: int,
    modifiers: Qt.KeyboardModifier,
    text: str,
    *,
    context: str | None = None,
    drawer_open: bool | None = None,
    platform: str = sys.platform,
) -> tuple[Action, str] | None:
    """Map a key press to (action, payload); the payload is the digit or the typed text.

    `context` is "player", "drawer", "library" or "guide". `drawer_open` is the Phase 1B
    spelling (True means "drawer", False "player") and is used only when `context` is not
    given.
    """
    if context is None:
        context = "drawer" if drawer_open else "player"
    if context not in CONTEXTS:
        raise ValueError(f"Unknown key context: {context!r}")
    key, mods = as_int(key), as_int(modifiers)
    held = mods & (_CTRL | _ALT | _META)
    if key in _MEDIA_KEYS:
        return _MEDIA_KEYS[key], ""
    if held == _ALT and key == _K.Key_P.value:
        return Action.MINI_PLAYER, ""
    if context == "library":
        if held == _CTRL and key in _LIBRARY_CTRL_KEYS:
            return _LIBRARY_CTRL_KEYS[key], ""
        if held == _CTRL | _META and key == _K.Key_F.value:
            return Action.FULLSCREEN, ""
    if held == _CTRL and key in _CTRL_KEYS:
        return _CTRL_KEYS[key], ""
    if context == "library":
        if key in _LIBRARY_KEYS:
            return _LIBRARY_KEYS[key], ""
        if (key == _K.Key_Slash.value or text == "/") and not mods & (_CTRL | _META):
            return Action.SEARCH, "/"  # the Library types it where text goes
        return _text(text, mods, windows=platform.startswith("win"))
    if context == "drawer":
        if key in _DRAWER_KEYS:
            return _DRAWER_KEYS[key], ""
        return _text(text, mods, windows=platform.startswith("win"))
    keys, chars = (
        (_GUIDE_KEYS, _GUIDE_CHARS) if context == "guide" else (_PLAYER_KEYS, _PLAYER_CHARS)
    )
    if key in keys:
        return keys[key], ""
    if key in _CHANNEL_KEYS:
        return _CHANNEL_KEYS[key], ""
    if held:
        return None
    if _K.Key_0.value <= key <= _K.Key_9.value:
        return Action.DIGIT, chr(key)
    if len(text) == 1 and text in _DIGITS:  # layouts where digits need Shift
        return Action.DIGIT, text
    if key in chars:
        return chars[key], ""
    return None


def _text(text: str, mods: int, *, windows: bool) -> tuple[Action, str] | None:
    """Printable text for the drawer and Library filters.

    Ctrl/⌘ and Meta never type, except Ctrl+Alt on Windows, which is how it reports AltGr
    (ł, @, €); on macOS ⌘⌥ is a shortcut. Alt (macOS Option) composes letters such as ç, so
    it may type, but not digits, which it only modifies on other platforms.
    """
    if not text or not text.isprintable():
        return None
    altgr = windows and mods & _CTRL and mods & _ALT and not mods & _META
    if mods & (_CTRL | _META) and not altgr:
        return None
    if mods & _ALT and all(c in _DIGITS for c in text):
        return None
    return Action.NAV_TEXT, text


# ---- the shortcuts table (Help → Keyboard Shortcuts, the README) -----------------------------
# Per context, (keys, what they do): keys are QKeySequence texts; ", " separates alternatives,
# and "0…9" / "A…Z" stand for every key in between. The "menu" rows are the menu bar's own
# shortcuts. Tests resolve every key in its context, so the table cannot drift from resolve().
SHORTCUT_TITLES = {
    "player": "Watching TV",
    "drawer": "Channel list",
    "guide": "Guide",
    "library": "Movies and Series",
    "menu": "Anywhere",
}
SHORTCUTS: dict[str, list[tuple[str, str]]] = {
    "player": [
        ("Up, Down", "Previous / next channel (volume during a movie)"),
        ("0…9", "Type a channel number"),
        ("Backspace", "Previous channel"),
        ("Return", "Channel list"),
        ("Left, Right", "Back / forward 10 seconds"),
        ("Space", "Play / pause"),
        ("L", "Go live"),
        ("G", "Guide"),
        ("I", "Channel info"),
        ("+, -", "Volume up / down"),
        ("M", "Mute"),
        ("F", "Fullscreen"),
        ("Ctrl+D", "Add or remove a favorite channel"),
        ("Alt+P", "Mini player"),
        ("Esc", "Close what is open, leave fullscreen; after a movie, back to the Library"),
    ],
    "drawer": [
        ("Up, Down", "Move"),
        ("PgUp, PgDown", "Page"),
        ("Left, Right", "Layer up / down"),
        ("Return", "Open or play"),
        ("A…Z", "Type to filter"),
        ("Backspace", "Delete a letter of the filter"),
        ("Ctrl+D", "Add or remove a favorite channel"),
        ("Esc", "Clear the filter, then close"),
    ],
    "guide": [
        ("Up, Down", "Move between channels"),
        ("Left, Right", "Earlier / later"),
        ("0…9", "Jump to a channel number"),
        ("Return", "Watch the channel"),
        ("G, Esc", "Close the guide"),
    ],
    "library": [
        ("Up, Down, Left, Right", "Move"),
        ("Return", "Play or open"),
        ("Home, End", "First / last title"),
        ("A…Z", "Jump to a letter, or filter the genres"),
        ("/, Ctrl+F", "Search"),
        ("Ctrl+D", "Add to or remove from My list"),
        ("F11, Ctrl+Meta+F", "Fullscreen"),
        ("Esc", "Back; from the genres, back to live TV"),
    ],
    "menu": [
        ("Ctrl+1", "Live TV"),
        ("Ctrl+2", "Movies"),
        ("Ctrl+3", "Series"),
        ("Ctrl+O", "Open a file"),
        ("Ctrl+,", "Settings"),
        ("Ctrl+Q", "Quit"),
    ],
}


def shortcut_keys(keys: str) -> list[str]:
    """The single keys a SHORTCUTS entry names, e.g. "0…9" → ["0", "1", …, "9"]."""
    result: list[str] = []
    for part in keys.split(", "):
        first, ellipsis, last = part.partition("…")
        if ellipsis and len(first) == 1 and len(last) == 1:
            result += [chr(c) for c in range(ord(first), ord(last) + 1)]
        else:
            result.append(part)
    return result
