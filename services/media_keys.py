"""The system's media keys: ⏯ ⏭ ⏮ on the keyboard, headphones and AirPods, the Touch Bar and
the system's media panel (2.8.13).

Each system gives them to the player it lists as playing, not as key presses to the window in
front: macOS only so (Qt never saw them), Windows and Linux to a focused window too, else to the
listed player. QiTV lists itself, says what the large picture plays, and turns each command into
the key a keyboard gives (`pressed`); the window acts on it where the TV is, as on the
keyboard's key (TVWindow.remote_key).

- macOS: MediaPlayer's Now Playing (services.media_keys_mac).
- Windows: the System Media Transport Controls of the window (services.media_keys_windows).
- Linux: MPRIS on the session bus (services.media_keys_linux), which GNOME, KDE and the other
  desktops send their media keys to.

All of it through ctypes or Qt: no native build.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Optional, Protocol

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication

logger = logging.getLogger(__name__)

_K = Qt.Key

# A command's key, as a keyboard's media key gives it ("play" and "pause" act only when they
# change something: see MediaKeys.command).
_KEYS = {
    "toggle": _K.Key_MediaTogglePlayPause,
    "play": _K.Key_MediaTogglePlayPause,
    "pause": _K.Key_MediaTogglePlayPause,
    "next": _K.Key_MediaNext,
    "previous": _K.Key_MediaPrevious,
    "stop": _K.Key_MediaStop,
}
# The keyboard's media keys, as a focused window gets them (Windows, Linux): the system may
# send the same press as a command too, which is then the same press.
_KEYBOARD = {
    _K.Key_MediaTogglePlayPause.value: _K.Key_MediaTogglePlayPause.value,
    _K.Key_MediaPlay.value: _K.Key_MediaTogglePlayPause.value,
    _K.Key_MediaPause.value: _K.Key_MediaTogglePlayPause.value,
    _K.Key_MediaNext.value: _K.Key_MediaNext.value,
    _K.Key_MediaPrevious.value: _K.Key_MediaPrevious.value,
    _K.Key_MediaStop.value: _K.Key_MediaStop.value,
}
SAME_PRESS_S = 0.5

# The large picture's states (services.playback.PlaybackState) as the system hears them: a
# stream opening or waiting for data still plays; idle, ended and failed play nothing.
_PLAYING_STATES = frozenset({"opening", "buffering", "playing"})


class System(Protocol):
    """Where the commands come from and what plays is told."""

    def listen(self) -> None: ...

    def now_playing(self, state: str, title: str, live: bool) -> None: ...

    def close(self) -> None: ...

    def report(self) -> dict:
        """What the system holds now: {"state": "playing" | "paused" | "", "title": …}."""
        ...

    def simulate(self, name: str) -> bool:
        """A command as the system delivers it (the packaged smoke test's)."""
        ...


_receiver: Optional["MediaKeys"] = None  # the one listening (a process has one entry)


def on_command(name: str) -> bool:
    """A system's command, from whichever thread it calls on: True when it was QiTV's."""
    receiver = _receiver
    if receiver is None:
        return False
    try:
        return receiver.command(name)
    except Exception as e:  # a system callback must never raise into the system
        logger.warning("The media key %s failed: %s", name, e)
        return False


def _system_for(window_id: int) -> Optional[System]:
    """This system's media keys for the window, None where there are none to hear (tests'
    offscreen windows, a Linux without a session bus)."""
    platform = QGuiApplication.platformName()
    if sys.platform == "darwin":
        if platform != "cocoa":
            return None
        from services.media_keys_mac import MacNowPlaying

        return MacNowPlaying()
    if sys.platform.startswith("win"):
        if platform != "windows" or not window_id:
            return None
        from services.media_keys_windows import WindowsMediaControls

        return WindowsMediaControls(window_id)
    if platform not in ("xcb", "wayland"):
        return None
    from services.media_keys_linux import Mpris

    return Mpris()


class MediaKeys(QObject):
    """The system's media commands for QiTV, and what QiTV plays for the system."""

    pressed = Signal(int)  # the Qt key a keyboard's media key would give
    raiseRequested = Signal()  # Linux: the desktop asks for QiTV's window

    def __init__(self, parent: Optional[QObject] = None, *, system: Optional[System] = None):
        super().__init__(parent)
        self._system = system
        self._state = ""  # "playing", "paused" or "" (nothing plays)
        self._shown: Optional[tuple[str, str, bool]] = None
        self._keyboard: tuple[int, float] = (0, 0.0)  # the keyboard's last media key, when

    @property
    def system(self) -> Optional[System]:
        return self._system

    def start(self, window_id: int = 0) -> bool:
        """Listen for the commands; False where there is nothing to listen to.
        `window_id`: the window's native id (Windows lists the window)."""
        global _receiver
        try:
            if self._system is None:
                self._system = _system_for(window_id)
            if self._system is None:
                return False
            self._system.listen()
        except Exception as e:  # an older or a stripped system: the keys stay as they were
            logger.info("The system's media keys aren't available: %s", e)
            self._system = None
            return False
        _receiver = self
        if self._shown is not None:  # what plays, told before the system listened
            shown, self._shown = self._shown, None
            self.show(*shown)
        return True

    def listening(self) -> bool:
        return _receiver is self and self._system is not None

    def show(self, state: str, title: str, live: bool) -> None:
        """The large picture's state (a PlaybackState value) and what it plays."""
        if state in _PLAYING_STATES:
            state = "playing"
        elif state != "paused":
            state, title, live = "", "", False
        self._state = state
        shown = (state, title, live)
        if shown == self._shown:
            return
        self._shown = shown
        if self._system is None:
            return
        try:
            self._system.now_playing(state, title, live)
        except Exception as e:
            logger.warning("Couldn't tell the system what plays: %s", e)

    def keyboard(self, key: int) -> None:
        """The window acted on a keyboard's media key (a focused window's)."""
        same = _KEYBOARD.get(key)
        if same is not None:
            self._keyboard = (same, time.monotonic())

    def command(self, name: str) -> bool:
        """A command from the system: True when it was QiTV's to do. Play while playing and
        pause while paused (headphones taken off) change nothing, nor the system's copy of a
        key the window just had from the keyboard."""
        if name == "raise":
            self.raiseRequested.emit()
            return True
        key = _KEYS.get(name)
        if key is None:
            return False
        if name == "play" and self._state == "playing":
            return True
        if name == "pause" and self._state != "playing":
            return True
        last, at = self._keyboard
        if last == key.value and time.monotonic() - at < SAME_PRESS_S:
            return True
        self.pressed.emit(key.value)
        return True

    def stop(self) -> None:
        """QiTV closes: no longer listed as playing."""
        global _receiver
        if _receiver is self:
            _receiver = None
        system, self._system = self._system, None
        if system is not None:
            try:
                system.close()
            except Exception as e:
                logger.warning("Couldn't leave the system's media panel: %s", e)
