"""Key presses to TV actions (spec §5.3). Pure: no widgets, no state."""

from __future__ import annotations

from enum import Enum
from typing import Any

from PySide6.QtCore import Qt


class Action(str, Enum):
    CH_NEXT = "ch_next"
    CH_PREV = "ch_prev"
    DIGIT = "digit"
    PREVIOUS = "previous"
    OPEN_DRAWER = "open_drawer"
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
    NAV_UP = "nav_up"
    NAV_DOWN = "nav_down"
    NAV_PAGE_UP = "nav_page_up"
    NAV_PAGE_DOWN = "nav_page_down"
    NAV_LEFT = "nav_left"
    NAV_RIGHT = "nav_right"
    NAV_ACTIVATE = "nav_activate"
    NAV_TEXT = "nav_text"
    NAV_BACKSPACE = "nav_backspace"


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
    _K.Key_Return.value: Action.OPEN_DRAWER,
    _K.Key_Enter.value: Action.OPEN_DRAWER,
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
# Shortcuts that work with the drawer open or closed.
_CTRL_KEYS = {_K.Key_Space.value: Action.PLAY_PAUSE, _K.Key_F.value: Action.FULLSCREEN}
_DIGITS = "0123456789"


def _value(flag: Any) -> int:
    return int(flag.value) if hasattr(flag, "value") else int(flag or 0)


def resolve(
    key: int, modifiers: Qt.KeyboardModifier, text: str, *, drawer_open: bool
) -> tuple[Action, str] | None:
    """Map a key press to (action, payload); the payload is the digit or the typed text."""
    key, mods = _value(key), _value(modifiers)
    held = mods & (_CTRL | _ALT | _META)
    if held == _ALT and key == _K.Key_P.value:
        return Action.MINI_PLAYER, ""
    if held == _CTRL and key in _CTRL_KEYS:
        return _CTRL_KEYS[key], ""
    if drawer_open:
        if key in _DRAWER_KEYS:
            return _DRAWER_KEYS[key], ""
        return _text(text, mods)
    if key in _PLAYER_KEYS:
        return _PLAYER_KEYS[key], ""
    if held:
        return None
    if _K.Key_0.value <= key <= _K.Key_9.value:
        return Action.DIGIT, chr(key)
    if len(text) == 1 and text in _DIGITS:  # layouts where digits need Shift
        return Action.DIGIT, text
    if key in _PLAYER_CHARS:
        return _PLAYER_CHARS[key], ""
    return None


def _text(text: str, mods: int) -> tuple[Action, str] | None:
    """Printable text for the drawer filter.

    Ctrl/⌘ and Meta never type. Alt (macOS Option) composes letters such as ç, so it may
    type, but not digits, which it only modifies on other platforms.
    """
    if not text or not text.isprintable() or mods & (_CTRL | _META):
        return None
    if mods & _ALT and all(c in _DIGITS for c in text):
        return None
    return Action.NAV_TEXT, text
