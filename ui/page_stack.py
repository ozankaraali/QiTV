"""The TV window's two pages in one of three arrangements (2.1: what plays keeps playing).

PLAYER: the player page fills the stack, the Library is hidden. LIBRARY: the Library fills it,
the player page is hidden. CORNER: the Library fills it and the player page, resized into a
corner above it, goes on playing ("we put your main screen here while you navigate").

Both pages are children of the stack in every mode and only change geometry and visibility: the
player page holds the native views libVLC draws into, and a native view is never re-parented
(on macOS libVLC's view would lose its picture).

In the corner a press on the player drags it and the release snaps it to the nearest corner,
as PipFrame does for the small picture (saved as `corner_player_corner`); a press released
without a drag is a click, `cornerClicked`. The stack takes every mouse event on the player
page and its views in that mode (also views added later): nothing reaches the shell's video
handling (pause, the drawer, zaps) and the PiP frame's own mouse handling is switched off by
the window. The mouse's Back and Forward go nowhere either, as over the Library's page.
"""

import logging
from typing import Optional

from PySide6.QtCore import QChildEvent, QEvent, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QWidget

from services.redact import safe_message
from ui.macos_window import reclaim_first_responder
from ui.overlays.painting import Units

logger = logging.getLogger(__name__)

CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
DEFAULT_CORNER = "bottom-right"
DRAG_START_PX = 8
_MOUSE_EVENTS = (
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseMove,
    QEvent.Type.MouseButtonRelease,
    QEvent.Type.Wheel,
)


def nearest_corner(rect: QRect, size: QSize) -> str:
    """The corner of the quadrant of `size` that holds the centre of `rect`."""
    centre = rect.center()
    vertical = "top" if centre.y() < size.height() / 2 else "bottom"
    horizontal = "left" if centre.x() < size.width() / 2 else "right"
    return f"{vertical}-{horizontal}"


class PageStack(QWidget):
    PLAYER = "player"
    LIBRARY = "library"
    CORNER = "corner"
    CORNER_WIDTH_FRACTION = 1 / 3  # of the stack's width, 16:9
    CORNER_MARGIN_U = 1.6  # from the nearest edges; u = 1% of the stack's width

    modeChanged = Signal(str)
    cornerClicked = Signal()  # a press on the corner player that was not a drag

    def __init__(self, player: QWidget, library: QWidget, config, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        self._library = library
        self._config = config
        self._mode = self.PLAYER
        self._corner = self._saved_corner()
        self._press: Optional[tuple[QPoint, QPoint]] = None  # (global press, player position)
        self._dragging = False
        for page in (player, library):
            page.setParent(self)
        # The player page and its native views: their mouse events are the stack's in the corner.
        self._watch(player)
        library.hide()
        self._layout()

    # ---- public API ---------------------------------------------------------------------

    def mode(self) -> str:
        return self._mode

    def currentWidget(self) -> QWidget:
        """The page in front: the Library in LIBRARY and CORNER (the player page is the small
        one over it), else the player page."""
        return self._player if self._mode == self.PLAYER else self._library

    def show_player(self) -> None:
        self._set_mode(self.PLAYER)

    def show_library(self, *, corner: bool) -> None:
        """The Library, with the player page in a corner (it plays on) or hidden."""
        self._set_mode(self.CORNER if corner else self.LIBRARY)

    def corner(self) -> str:
        return self._corner

    def corner_rect(self) -> QRect:
        """The corner player's rect in stack coordinates: a third of the width, 16:9."""
        size = self.size()
        width = round(size.width() * self.CORNER_WIDTH_FRACTION)
        height = round(width * 9 / 16)
        margin = Units(size.width()).px(self.CORNER_MARGIN_U)
        x = margin if self._corner.endswith("left") else size.width() - margin - width
        y = margin if self._corner.startswith("top") else size.height() - margin - height
        return QRect(x, y, width, height)

    def set_corner(self, name: str) -> None:
        """Where the corner player sits: one of CORNERS, saved when it changes."""
        if name not in CORNERS:
            raise ValueError(f"Unknown corner: {name!r}")
        changed, self._corner = name != self._corner, name
        self._layout()
        if changed:
            self._config.corner_player_corner = name
            try:
                self._config.save_config()
            except Exception as e:
                logger.warning("Couldn't save the corner player's corner: %s", safe_message(e))

    # ---- layout -------------------------------------------------------------------------

    def _saved_corner(self) -> str:
        value = getattr(self._config, "corner_player_corner", DEFAULT_CORNER)
        return value if isinstance(value, str) and value in CORNERS else DEFAULT_CORNER

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._press, self._dragging = None, False
        self._layout()
        self.modeChanged.emit(mode)

    def _layout(self) -> None:
        area = self.rect()
        if self._mode == self.PLAYER:
            self._player.setGeometry(area)
            self._player.show()
            self._library.hide()
            return
        self._library.setGeometry(area)
        self._library.show()
        if self._mode == self.LIBRARY:
            self._player.hide()
            return
        self._player.setGeometry(self.corner_rect())
        self._player.show()
        self._player.raise_()  # above the Library (the views too: macOS stacks natives itself)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout()

    # ---- mouse: dragging the corner player ----------------------------------------------

    def _watch(self, widget: QWidget) -> None:
        for watched in (widget, *widget.findChildren(QWidget)):
            watched.installEventFilter(self)  # once: Qt drops an earlier install of it

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        mode = getattr(self, "_mode", None)
        if mode is None:
            # Collected in a reference cycle: the collector cleared the attributes before Qt
            # deletes the stack and its views, whose last events still come here.
            return False
        if isinstance(event, QChildEvent) and event.type() == QEvent.Type.ChildAdded:
            child = event.child()
            if isinstance(child, QWidget):
                self._watch(child)  # a view added to the player page later
            return False
        if mode != self.CORNER or event.type() not in _MOUSE_EVENTS:
            return False
        if isinstance(event, QMouseEvent):
            self._mouse(event)
        return True  # the corner player is a thumbnail: no pause, drawer, zap or volume

    def _mouse(self, event: QMouseEvent) -> None:
        kind, left = event.type(), Qt.MouseButton.LeftButton
        if kind != QEvent.Type.MouseMove:
            # macOS: libVLC's view in the clicked surface took the keys, and the window's own
            # filter never sees the clicks this one takes (the Library's search would beep).
            reclaim_first_responder(self.window())
        position = event.globalPosition().toPoint()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            if event.button() == left:
                self._press, self._dragging = (position, self._player.pos()), False
            return
        press = self._press
        if press is None:
            return
        start, origin = press
        if kind == QEvent.Type.MouseMove:
            if not event.buttons() & left:  # the release was lost (left the window)
                self._press, self._dragging = None, False
            else:
                self._drag(position - start, origin)
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == left:
            dragged, self._press, self._dragging = self._dragging, None, False
            if dragged:
                self.set_corner(nearest_corner(self._player.geometry(), self.size()))
            else:
                self.cornerClicked.emit()

    def _drag(self, delta: QPoint, origin: QPoint) -> None:
        if not self._dragging:
            if delta.manhattanLength() < DRAG_START_PX:
                return
            self._dragging = True
        area, size = self.size(), self._player.size()
        target = origin + delta
        self._player.move(
            max(0, min(target.x(), area.width() - size.width())),
            max(0, min(target.y(), area.height() - size.height())),
        )
