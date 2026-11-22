"""The desktop picture in picture's player controls (macOS, 2.13.5).

The owner: "PIP CAN BE FULLSCREEN TO OTHER DESKTOP, THIS IS ALSO PIP AND THOSE SHOULD BE EQUAL".
The window a desktop change (or a covered window) floats the picture in shows the controls the
mini player shows, the same way: they come as the pointer moves over it and fade after the
same wait, kept up while it rests on them. The mini player holds the whole player page under
the window's own controls (ui/mini_player.py); the floating window holds only the picture's
surface (ui/desktop_pip.py: moving the page out of the fullscreen window brought its desktop
back every second or two), so its controls are a Transport of their own that mirrors the
window's (Transport.add_mirror: what they show; whether they show is their own), in the mini
player's look (its way back), in an overlay host of their own over it: libVLC's view covers any
widget inside the floating window.

Nothing here focuses, activates or raises a window. The host is a panel that takes no focus,
on every desktop beside the floating window (`join_all_spaces`: a press on it never activates
QiTV), told again each time it shows; the hover only reads where the pointer is (`hover_at`,
from DesktopPip's HoverPoll while QiTV is not the active app).

Its clicks:
- What acts on the player acts in place (`IN_PLACE`: play/pause, ■, the seeks and the steps,
  live, the programme's start, the speed, the next and previous, the star, record, the music
  player's tracks and its volume bar), and the speaker mutes or unmutes (as M does: the mixer
  lives in the window).
- Its way back (`WAY_BACK`: the window's button, fullscreen) brings the fullscreen desktop back
  (`come_back`), as a double-click on the picture does.
- Every other control opens what lives in the window (Sound, Subtitles, Picture, ⚙, the guide,
  Search, the Library, the multiview, picture in picture, cast, ⋯): the fullscreen desktop comes
  back first and it opens there, as the mini player's Library and Search do.
- A press on the shades beside a control is the picture's (`picture_mouse`): a drag moves the
  floating window, an edge's resizes it, a double-click comes back.
- The wheel does nothing (a scroll drifting over it from another app never zaps), nor do the
  keys: it never takes the keyboard.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from ui.overlay_host import OverlayHost
from ui.overlays.transport import Transport

logger = logging.getLogger(__name__)

# The controls' requests that act on the player where it is (the window's own handlers).
IN_PLACE = (
    "playPauseRequested",
    "stopRequested",
    "seekRequested",
    "seekRelativeRequested",
    "speedRequested",
    "nextRequested",
    "goLiveRequested",
    "programmeStartRequested",
    "seekToRequested",
    "previousRequested",
    "favoriteRequested",
    "recordRequested",
    "prevTrackRequested",
    "nextTrackRequested",
    "shuffleRequested",
    "repeatRequested",
    "volumeSetRequested",
)
WAY_BACK = ("fullscreenRequested", "miniPlayerRequested")  # the fullscreen desktop, no more
MUTE = "volumeRequested"  # the speaker
# Menus that open from their button (a global point): in the window, from the window's button.
AT_BUTTON = {"externalRequested": "external", "castRequested": "cast", "moreRequested": "more"}
_PICTURE_EVENTS = (QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease)


def requests() -> tuple[str, ...]:
    """Every request the player controls make (their signals' names)."""
    return tuple(
        name
        for name, value in vars(Transport).items()
        if name.endswith("Requested") and isinstance(value, Signal)
    )


class FloatHost(OverlayHost):
    """The overlay host over the floating window: glued over it (it is both the window it
    follows and the surface it covers), a panel on every desktop beside it that takes no
    focus, told so again each time it shows (showing may set its own)."""

    def __init__(self, window: QWidget, join_all_spaces: Callable[[QWidget], object]) -> None:
        super().__init__(window, window)
        self._join_all_spaces = join_all_spaces
        join_all_spaces(self)

    def setVisible(self, visible: bool) -> None:  # show()/hide() route through here
        super().setVisible(visible)
        join = getattr(self, "_join_all_spaces", None)  # (None while OverlayHost's init runs)
        if visible and join is not None:
            join(self)


class FloatControls(QObject):
    """The player controls over the floating window (the module docstring): `transport` is the
    window's, mirrored; `come_back()` brings the fullscreen desktop back; `mute()` mutes or
    unmutes the large picture; `picture_mouse(event)` is the floating window's own handling
    of a mouse event on its picture."""

    def __init__(
        self,
        window: QWidget,
        transport: Transport,
        *,
        come_back: Callable[[], None],
        mute: Callable[[], None],
        picture_mouse: Callable[[QMouseEvent], None],
        join_all_spaces: Callable[[QWidget], object],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._window = window
        self._transport = transport
        self._come_back = come_back
        self._picture_mouse = picture_mouse
        self._picture_press = False  # a press on the shades: its moves are the picture's
        self.host = FloatHost(window, join_all_spaces)
        mirror = self.mirror = Transport(self.host, idle_ms=transport.idle_ms())
        mirror.set_window_mode(mini=True, fullscreen=False)  # the mini player's way back
        transport.add_mirror(mirror)  # (before the host hosts it: it shows nothing meanwhile)
        self.host.add_overlay(mirror)  # (hidden: it shows on a hover)
        mirror.installEventFilter(self)
        for name in requests():
            signal = getattr(mirror, name)
            if name in IN_PLACE or name == MUTE:
                if name != "volumeSetRequested":  # (not each step of a drag of its bar)
                    signal.connect(lambda *_args, name=name: self._note(name))
                signal.connect(mute if name == MUTE else getattr(transport, name))
            elif name in WAY_BACK:
                signal.connect(self._back)
            else:
                signal.connect(lambda *args, name=name: self._in_the_window(name, args))

    @staticmethod
    def _note(name: str) -> None:
        """Each click that acts in place, in the log: should the window ever come forward after
        one, a live run says which (the focus line follows it)."""
        logger.info("Desktop picture in picture: %s, in place", name)

    def floated(self) -> None:
        """The picture floats (again): the host takes the window's place once it has one."""
        QTimer.singleShot(0, self.host, self.host.sync_geometry)  # (its Show came first)

    def docked(self) -> None:
        """The picture is back in the window: its controls go with the floating window."""
        self._picture_press = False
        if isValid(self.mirror):
            self.mirror.hover_at(None)
            self.mirror.dismiss()

    def hover_at(self, where: Optional[QPoint]) -> None:
        """The pointer moved to `where` over the floating window (global), or off it (None)."""
        if not isValid(self.mirror) or not isValid(self._window):
            return
        if where is None:
            self.mirror.hover_at(None)
        else:  # (the floating window's place: the host's may be from its last show)
            self.mirror.hover_at(QPointF(self._window.mapFromGlobal(where)))

    def _back(self) -> None:
        QTimer.singleShot(0, self, self._come_back)  # (once its click is through)

    def _in_the_window(self, name: str, args: tuple) -> None:
        QTimer.singleShot(0, self, lambda: self._open_in_the_window(name, args))

    def _open_in_the_window(self, name: str, args: tuple) -> None:
        """`name` asked of the window's controls, up as after a click on them, once the
        fullscreen desktop is back (a menu from the window's own button)."""
        self._come_back()
        transport = self._transport
        if not isValid(transport):
            return
        button = AT_BUTTON.get(name)
        if button is not None and args:
            try:
                rect = transport.button_rect(button)
            except KeyError:
                pass  # (not shown there: from where it was asked)
            else:
                args = (transport.mapToGlobal(rect.topLeft().toPoint()),)
        logger.info("Desktop picture in picture: %s, in the window", name)
        transport.poke()
        getattr(transport, name).emit(*args)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """A press on the shades beside a control, and its moves and release, are the
        picture's (a drag moves the floating window); the controls' own stay theirs."""
        if watched is not self.mirror or not isinstance(event, QMouseEvent):
            return False
        kind = event.type()
        left = Qt.MouseButton.LeftButton
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            if event.button() != left or self.mirror.control_at(event.position()) is not None:
                return False
            self._picture_press = kind == QEvent.Type.MouseButtonPress  # (a double-click: back)
        elif not (self._picture_press and kind in _PICTURE_EVENTS):
            return False
        elif kind == QEvent.Type.MouseButtonRelease or not event.buttons() & left:
            self._picture_press = False  # released (a move without the button: it was lost)
        self.mirror.poke()  # (up while it is dragged by them)
        self._picture_mouse(event)
        return True
