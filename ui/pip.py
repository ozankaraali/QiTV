"""Picture-in-picture frame: the second video surface, small in a corner of the player (spec §7).

Both surfaces are native children of the player page and never change parent, so the windows
libVLC draws into never change: a swap exchanges their geometry and stacking only.

The small surface takes the left button (the shell's click would open the drawer): a click
asks for a swap, and a drag moves it and, on release, snaps it to the nearest corner. These
are the Qt mouse events the shell reads on the main surface (libVLC's own mouse handling is
off, so they reach Qt on macOS too). Other buttons, the wheel and moves go on to the shell.
The PiP label's pill, drawn over the small surface, forwards its own presses the same way
(pill_press, pill_move, pill_release).
"""

from dataclasses import dataclass
import logging

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from ui.overlays.painting import Units

logger = logging.getLogger(__name__)

CORNERS = ("br", "bl", "tr", "tl")
DEFAULT_CORNER = "br"
SURFACES = ("main", "pip")
WIDTH_FRACTION = 0.25
HEIGHT_PER_WIDTH = 9 / 16
MARGIN_UNITS = 2.0  # from the nearest edges; u = 1% of the player width
DRAG_START_PX = 8
_LAYOUT_EVENTS = (QEvent.Type.Resize, QEvent.Type.Show)
_MOUSE_EVENTS = (
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseMove,
    QEvent.Type.MouseButtonRelease,
)


def corner_rect(size: QSize, corner: str) -> QRect:
    """The small surface's rect in a player of `size`: 25% wide, 16:9, 2u from the edges."""
    width = round(size.width() * WIDTH_FRACTION)
    height = round(width * HEIGHT_PER_WIDTH)
    margin = Units(size.width()).px(MARGIN_UNITS)
    x = margin if corner.endswith("l") else size.width() - margin - width
    y = margin if corner.startswith("t") else size.height() - margin - height
    return QRect(x, y, width, height)


def nearest_corner(rect: QRect, size: QSize) -> str:
    """The corner of the quadrant holding the centre of `rect`."""
    centre = rect.center()
    vertical = "t" if centre.y() < size.height() / 2 else "b"
    horizontal = "l" if centre.x() < size.width() / 2 else "r"
    return vertical + horizontal


def _valid_corner(value: object) -> str:
    return value if isinstance(value, str) and value in CORNERS else DEFAULT_CORNER


@dataclass
class _Press:
    """The left button went down on the small surface (globals: the surface moves)."""

    surface: QWidget
    start: QPoint  # global
    origin: QPoint  # the surface's position then, in the container
    dragging: bool = False
    void: bool = False  # the layout changed meanwhile, or the second press of a double-click
    surface_events: bool = True  # False: the label's pill forwards the rest of it


class PipFrame(QObject):
    swapRequested = Signal()  # a click on the small surface
    cornerChanged = Signal(str)  # a drag snapped it to another corner (saved as pip_corner)
    smallRectChanged = Signal(QRect)  # container coordinates, empty when hidden (the label)

    def __init__(
        self, container: QWidget, main_surface: QWidget, pip_surface: QWidget, config
    ) -> None:
        super().__init__(container)
        self._container = container
        self._surfaces = {"main": main_surface, "pip": pip_surface}
        self._config = config
        self._large = "main"
        self._shown = False
        self._corner = self._saved_corner()
        self._press: _Press | None = None
        # Where and when a click swapped. A second press there within the double-click
        # interval belongs to that click, whichever surface (now in the corner) it lands on:
        # Qt need not make a double-click of presses on two native windows.
        self._swap_at: QPoint | None = None
        self._swap_clock = QElapsedTimer()
        self._reported = QRect()
        self._closed = False
        # Removed by close(), or by Qt once this frame is deleted with the container.
        for watched in (container, main_surface, pip_surface):
            watched.installEventFilter(self)
        self._layout()

    # ---- public API ---------------------------------------------------------------------

    def show_pip(self) -> None:
        """Show the PiP surface small, at the saved corner."""
        self._corner = self._saved_corner()
        self._shown = True
        self._layout()

    def hide_pip(self) -> None:
        """Hide the PiP surface; the main one fills the player (as SessionManager.close_pip)."""
        self._shown = False
        self._large = "main"
        self._layout()

    def set_large(self, name: str) -> None:
        """Make "main" or "pip" fill the player; the other goes to the corner, on top."""
        if name not in SURFACES:
            raise ValueError(f"Unknown surface: {name!r}")
        self._large = name
        self._layout()

    def small_rect(self) -> QRect:
        """Where the small surface is, in container coordinates; empty while PiP is hidden."""
        if not self._shown:
            return QRect()
        return QRect(self._small().geometry())

    def corner(self) -> str:
        return self._corner

    def close(self) -> None:
        """Stop watching the player page and the surfaces (the window's shutdown)."""
        if self._closed:
            return
        self._closed = True
        self._press = None
        for watched in (self._container, *self._surfaces.values()):
            if isValid(watched):
                watched.removeEventFilter(self)

    # The label's pill covers part of the small surface; it forwards its presses (globals).
    def pill_press(self, position: QPoint) -> None:
        if self._shown and not self._closed:
            self._begin(self._small(), position, surface_events=False)

    def pill_move(self, position: QPoint) -> None:
        press = self._press
        if press is not None and not press.surface_events and not press.void:
            self._drag(press, position)

    def pill_release(self, position: QPoint) -> None:
        press = self._press
        if press is not None and not press.surface_events:
            self._release(press, position)

    # ---- layout -------------------------------------------------------------------------

    def _small(self) -> QWidget:
        return self._surfaces["pip" if self._large == "main" else "main"]

    def _saved_corner(self) -> str:
        return _valid_corner(getattr(self._config, "pip_corner", DEFAULT_CORNER))

    def _layout(self) -> None:
        if self._press is not None:
            self._press.void = True  # its release neither swaps nor snaps
        self._surfaces[self._large].setGeometry(self._container.rect())
        small = self._small()
        if self._shown:
            small.setGeometry(corner_rect(self._container.size(), self._corner))
            small.show()
            # macOS can stack a native view under its sibling after a geometry change.
            small.raise_()
        else:
            small.hide()
        self._report()

    def _report(self) -> None:
        rect = self.small_rect()
        if rect != self._reported:
            self._reported = QRect(rect)
            self.smallRectChanged.emit(rect)

    def _snap(self) -> None:
        corner = nearest_corner(self._small().geometry(), self._container.size())
        changed, self._corner = corner != self._corner, corner
        self._layout()
        if changed:
            self._save_corner()
            self.cornerChanged.emit(corner)

    def _save_corner(self) -> None:
        self._config.pip_corner = self._corner
        try:
            self._config.save_config()
        except Exception as e:
            logger.warning("Couldn't save the picture-in-picture corner (%s)", type(e).__name__)

    # ---- mouse --------------------------------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # The event type first: a page being deleted still sends others (hide, deactivate)
        # through this filter, possibly after the garbage collector cleared this wrapper.
        kind = event.type()
        if kind in _LAYOUT_EVENTS:
            if watched is self._container:
                self._layout()
        elif kind in _MOUSE_EVENTS and isinstance(event, QMouseEvent):
            return self._mouse(watched, event)
        return False

    def _mouse(self, watched: QObject, event: QMouseEvent) -> bool:
        """True when the event is the frame's: the shell's surface handling never sees it."""
        if not isinstance(watched, QWidget):
            return False
        kind, left = event.type(), Qt.MouseButton.LeftButton
        position = event.globalPosition().toPoint()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            if event.button() != left:
                return False
            return self._begin(watched, position, surface_events=True)
        press = self._press
        if press is None or not press.surface_events or watched is not press.surface:
            return False
        if kind == QEvent.Type.MouseMove:
            if not event.buttons() & left:  # the release was lost (left the window)
                self._release(press, None)
                return False
            if not press.void:
                self._drag(press, position)
            return True
        if kind == QEvent.Type.MouseButtonRelease and event.button() == left:
            self._release(press, position)
            return True
        return False

    def _begin(self, surface: QWidget, position: QPoint, *, surface_events: bool) -> bool:
        """A left press on `surface` (or the pill over it); False when it is not the frame's."""
        void = self._second_press(position)
        if not void and (not self._shown or surface is not self._small()):
            return False
        self._press = _Press(
            surface, position, surface.pos(), void=void, surface_events=surface_events
        )
        return True

    def _second_press(self, position: QPoint) -> bool:
        """The second press of a double-click whose first click swapped (any press ends it)."""
        at, self._swap_at = self._swap_at, None
        distance = QGuiApplication.styleHints().mouseDoubleClickDistance()
        return (
            at is not None
            and self._swap_clock.elapsed() <= QApplication.doubleClickInterval()
            and (position - at).manhattanLength() <= distance
        )

    def _drag(self, press: _Press, position: QPoint) -> None:
        delta = position - press.start
        if not press.dragging:
            if delta.manhattanLength() < DRAG_START_PX:
                return
            press.dragging = True
            press.surface.raise_()  # it moves: kept above the large one (macOS)
        area, size = self._container.size(), press.surface.size()
        target = press.origin + delta
        press.surface.move(
            max(0, min(target.x(), area.width() - size.width())),
            max(0, min(target.y(), area.height() - size.height())),
        )
        self._report()

    def _release(self, press: _Press, position: QPoint | None) -> None:
        """The button came up at `position` (None: the release was lost)."""
        self._press = None
        if press.void:
            return
        if press.dragging:
            self._snap()
        elif position is not None:
            self._swap_at = QPoint(position)
            self._swap_clock.start()
            self.swapRequested.emit()
