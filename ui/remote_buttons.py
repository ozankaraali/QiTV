"""What each phone-remote button does on the TV: a key press (resolved in the TV's context, as
the keyboard's), an action, or a screen. Routes, repeats and reverses are in
services/remote/protocol.py (BUTTON_META)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from PySide6.QtCore import Qt

from ui.keymap import Action

_K = Qt.Key


@dataclass(frozen=True)
class Button:
    kind: Literal["key", "action", "screen"]
    key: int = 0
    text: str = ""
    action: Optional[Action] = None
    payload: str = ""


def _key(qt_key: Qt.Key, text: str = "") -> Button:
    return Button("key", key=qt_key.value, text=text)


def _action(action: Action) -> Button:
    return Button("action", action=action)


def _buttons() -> dict[str, Button]:
    table = {
        "up": _key(_K.Key_Up),
        "down": _key(_K.Key_Down),
        "left": _key(_K.Key_Left),
        "right": _key(_K.Key_Right),
        "ok": _key(_K.Key_Return),
        "back": _key(_K.Key_Escape),
        "previous": _key(_K.Key_Backspace),
        "search": _action(Action.SEARCH),
        "fullscreen": _action(Action.FULLSCREEN),
        "volup": _action(Action.VOL_UP),
        "voldown": _action(Action.VOL_DOWN),
        "mute": _action(Action.MUTE),
        # Ch ▲ is the keyboard's ↑: the channel above in the list (the previous one).
        "chup": _action(Action.CH_PREV),
        "chdown": _action(Action.CH_NEXT),
        "rew": _action(Action.SEEK_BACK),
        "fwd": _action(Action.SEEK_FWD),
        "pp": _action(Action.PLAY_PAUSE),
        "stop": _action(Action.STOP),  # 2.6: nothing plays, nothing streams
        "live": _action(Action.GO_LIVE),
        "info": _action(Action.INFO),
        "guide": _action(Action.GUIDE),
        "record": _action(Action.RECORD),
        "pip": _action(Action.PIP),
        "swap": _action(Action.SWAP),
        "multiview": _action(Action.MULTIVIEW),
        "picture": _action(Action.PICTURE),
        "back_to": _action(Action.BACK_TO),  # 2.6: Resume, while the TV offers it
        "tv": Button("screen", payload="tv"),
        "movies": Button("screen", payload="movies"),
        "series": Button("screen", payload="series"),
    }
    for digit in range(10):
        table[f"d{digit}"] = _key(getattr(_K, f"Key_{digit}"), str(digit))
    return table


BUTTONS = _buttons()
