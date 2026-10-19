"""Key presses to TV actions (spec §5.3). Pure: no widgets, no state."""

from __future__ import annotations

from enum import Enum
import sys
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from services.i18n import N_, tr


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
    PIP = "pip"  # picture in picture: open (the previous or highlighted channel) or close
    SWAP = "swap"  # the large and the small picture trade places
    MULTIVIEW = "multiview"  # the grid of channels, or back to one picture
    TAB = "tab"  # the mixer's other row
    SOUND_EARLIER = "sound_earlier"  # J: the sound 50 ms earlier against the picture
    SOUND_LATER = "sound_later"  # K: 50 ms later (VLC's keys)
    SUBTITLES_EARLIER = "subtitles_earlier"  # Shift+J: the subtitles 50 ms earlier
    SUBTITLES_LATER = "subtitles_later"  # Shift+K (VLC's G and H: G is the guide here)
    PASTE = "paste"  # Ctrl/⌘+V: the clipboard into the subtitles search's field
    RECORD = "record"  # R: record the channel being watched, or stop (2.2)
    FORGET = "forget"  # Delete in the channel list: the highlighted channel leaves ⟲ Recent


# "subtitles": the OpenSubtitles search over the player (2.2): it types like the drawer.
CONTEXTS = ("player", "drawer", "library", "guide", "subtitles")


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
    _K.Key_Tab.value: Action.TAB,
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
    _K.Key_P.value: Action.PIP,
    _K.Key_S.value: Action.SWAP,
    _K.Key_V.value: Action.MULTIVIEW,
    _K.Key_J.value: Action.SOUND_EARLIER,
    _K.Key_K.value: Action.SOUND_LATER,
    _K.Key_R.value: Action.RECORD,
}
# With Shift, the sound's sync keys move the subtitles (the player only).
_PLAYER_SHIFT_CHARS = {
    _K.Key_J.value: Action.SUBTITLES_EARLIER,
    _K.Key_K.value: Action.SUBTITLES_LATER,
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
    _K.Key_VolumeUp.value: Action.VOL_UP,
    _K.Key_VolumeDown.value: Action.VOL_DOWN,
    _K.Key_VolumeMute.value: Action.MUTE,
}
# Shortcuts that work in every context: Ctrl/⌘+F is the quick search (the owner's ruling),
# and fullscreen is F11 or ⌃⌘F (Ctrl+Meta+F) anywhere, plain F on the player and in the guide.
_CTRL_KEYS = {
    _K.Key_Space.value: Action.PLAY_PAUSE,
    _K.Key_F.value: Action.SEARCH,
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
# searches too, and Ctrl+D toggles My list.
_LIBRARY_KEYS = {
    **_DRAWER_KEYS,
    _K.Key_Home.value: Action.NAV_HOME,
    _K.Key_End.value: Action.NAV_END,
}
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

    `context` is "player", "drawer", "library", "guide" or "subtitles". `drawer_open` is the
    Phase 1B spelling (True means "drawer", False "player") and is used only when `context`
    is not given.
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
    if key == _K.Key_F11.value or (held == _CTRL | _META and key == _K.Key_F.value):
        return Action.FULLSCREEN, ""
    if held == _CTRL and key in _CTRL_KEYS:
        return _CTRL_KEYS[key], ""
    if held == _CTRL and key == _K.Key_P.value and context in ("player", "drawer"):
        # The drawer types P into its find: Ctrl/⌘+P opens the highlighted channel small.
        return Action.PIP, ""
    if context == "library":
        if key in _LIBRARY_KEYS:
            return _LIBRARY_KEYS[key], ""
        if (key == _K.Key_Slash.value or text == "/") and not mods & (_CTRL | _META):
            return Action.SEARCH, "/"  # the Library types it where text goes
        return _text(text, mods, windows=platform.startswith("win"))
    if context == "drawer":
        if key in _DRAWER_KEYS:
            return _DRAWER_KEYS[key], ""
        if key == _K.Key_Delete.value and not held:  # the row's "Remove from Recents"
            return Action.FORGET, ""
        return _text(text, mods, windows=platform.startswith("win"))
    if context == "subtitles":
        if key in _DRAWER_KEYS:
            return _DRAWER_KEYS[key], ""
        if held == _CTRL and key == _K.Key_V.value:
            return Action.PASTE, ""
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
    if mods & _SHIFT and context == "player" and key in _PLAYER_SHIFT_CHARS:
        return _PLAYER_SHIFT_CHARS[key], ""
    if key in chars:
        return chars[key], ""
    return None


def _text(text: str, mods: int, *, windows: bool) -> tuple[Action, str] | None:
    """Printable text for the drawer's find and the Library's filters.

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
# The titles and what the keys do stay English here (the README is made from them) and are
# shown with `translate` (ui/shortcuts_dialog.py); the keys' names are Qt's.
SHORTCUT_TITLES = {
    "player": N_("Watching TV"),  # i18n: a heading of Help → Keyboard Shortcuts
    "drawer": N_("Channel list"),
    "guide": N_("Guide"),  # i18n: the TV guide (the programme grid)
    "library": N_("Movies and Series"),
    "subtitles": N_("Finding subtitles"),
    "menu": N_("Anywhere"),  # i18n: a heading of Help → Keyboard Shortcuts: keys for anywhere
}
SHORTCUTS: dict[str, list[tuple[str, str]]] = {
    "player": [
        ("Up, Down", N_("Previous / next channel (volume during a movie)")),
        ("0…9", N_("Type a channel number")),
        ("Backspace", N_("Previous channel")),
        ("Return", N_("Channel list")),
        ("Left, Right", N_("Back / forward 10 seconds")),
        ("Space", N_("Play / pause")),
        ("L", N_("Go live")),
        ("R", N_("Record the channel, or stop recording")),
        ("G", N_("Guide")),
        ("I", N_("Channel info")),
        ("+, -", N_("Volume up / down (the mixer)")),
        ("M", N_("Mute")),  # i18n: what a key does (verb)
        ("Tab", N_("The mixer's other row (while it shows)")),
        ("J, K", N_("Sound 50 ms earlier / later (sync with the picture)")),
        ("Shift+J, Shift+K", N_("Subtitles 50 ms earlier / later")),
        ("F, F11, Ctrl+Meta+F", N_("Fullscreen")),
        ("Ctrl+F", N_("Search")),  # i18n: what a key does (verb)
        ("Ctrl+D", N_("Add or remove a favorite channel")),
        ("P, Ctrl+P", N_("Picture-in-picture with the previous channel, or close it")),
        ("S", N_("Swap the large and the small picture")),
        ("V", N_("Multiview: several channels at once, or back to one")),
        ("Alt+P", N_("Mini player")),
        (
            "Esc",
            N_(
                "Close what is open, leave the multiview or fullscreen; after a movie, back to "
                "the Library"
            ),
        ),
    ],
    "drawer": [
        ("Up, Down", N_("Move")),  # i18n: what the arrow keys do in a list (verb)
        ("PgUp, PgDown", N_("Page")),  # i18n: what Page Up / Page Down do: a page up or down
        ("Left, Right", N_("Layer up / down")),
        ("Return", N_("Open or play")),
        ("A…Z", N_("Find a genre or channel (every provider)")),
        ("Backspace", N_("Delete a letter of the find")),
        ("Ctrl+D", N_("Add or remove a favorite channel")),
        ("Ctrl+P", N_("Open the channel in picture-in-picture")),
        ("Delete", N_("Remove the channel from Recents")),
        ("Ctrl+F", N_("Search")),
        ("F11, Ctrl+Meta+F", N_("Fullscreen")),
        ("Esc", N_("End the find, then close")),
    ],
    "guide": [
        ("Up, Down", N_("Move between channels")),
        ("Left, Right", N_("Earlier / later")),
        ("0…9", N_("Jump to a channel number")),
        ("Return", N_("Watch the channel; a past programme from its start")),
        ("Ctrl+F", N_("Search")),
        ("F, F11, Ctrl+Meta+F", N_("Fullscreen")),
        ("G, Esc", N_("Close the guide")),
    ],
    "library": [
        ("Up, Down, Left, Right", N_("Move")),
        ("Return", N_("Play or open")),
        ("Home, End", N_("First / last title")),
        ("A…Z", N_("Jump to a letter, or filter the genres")),
        ("Ctrl+F", N_("Search")),
        ("/", N_("Search, or type it in the search")),
        ("Ctrl+D", N_("Add to or remove from My list")),
        ("F11, Ctrl+Meta+F", N_("Fullscreen")),
        ("Esc", N_("Back; from the genres, back to live TV")),
    ],
    "subtitles": [
        ("Up, Down", N_("Move in a list; up from the top to the search field")),
        ("PgUp, PgDown", N_("Page")),
        ("Left, Right", N_("Between the languages and the subtitles")),
        ("Return", N_("Download and show the subtitles; in the field, search (or save the key)")),
        ("A…Z", N_("Type what to search for (or the API key)")),
        ("Ctrl+V", N_("Paste")),  # i18n: what a key does (verb)
        ("Backspace", N_("Delete a letter")),
        ("Esc", N_("Close")),  # i18n: what a key does (verb)
    ],
    "menu": [
        ("Ctrl+1", N_("Live TV")),
        ("Ctrl+2", N_("Movies")),
        ("Ctrl+3", N_("Series")),
        ("Ctrl+4", N_("Multiview")),
        ("Ctrl+O", N_("Open a file")),
        ("Ctrl+,", N_("Settings")),
        ("Ctrl+R", N_("Refresh channels and guide")),
        ("Ctrl+Q", N_("Quit")),  # i18n: what a key does (verb)
    ],
}


_ARROWS = {"Left": "←", "Right": "→", "Up": "↑", "Down": "↓"}


def key_hint(what: str, keys: str) -> str:
    """An on-screen control's tooltip naming its key, as this computer names it.

    "Guide (G)", "Search (⌘F)" on macOS and "Search (Ctrl+F)" elsewhere; arrows read as
    arrows everywhere. `what` is already in the active language.
    """
    name = _ARROWS.get(keys) or QKeySequence(keys).toString(QKeySequence.SequenceFormat.NativeText)
    # i18n: a control's tooltip: what it does, then its key in brackets: "Guide (G)"
    return tr("{action} ({key})", action=what, key=name)


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
