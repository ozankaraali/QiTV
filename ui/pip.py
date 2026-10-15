"""Picture-in-picture frame: the second video surface, small in a corner of the player (spec §7).

Both surfaces are native children of the player page and never change parent, so the windows
libVLC draws into never change: a swap exchanges their geometry and stacking only.

The small surface takes the left button (the shell's click would open the drawer): a click
asks for a swap, and a drag moves it and, on release, snaps it to the nearest corner. These
are the Qt mouse events the shell reads on the main surface (libVLC's own mouse handling is
off, so they reach Qt on macOS too). Other buttons, the wheel and moves go on to the shell.
"""

from dataclasses import dataclass
import logging

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QWidget
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
        # A click swapped: the double-click's second press belongs to it, wherever it lands.
        self._swallow_double = False
        self._reported = QRect()
        container.installEventFilter(self)
        for surface in self._surfaces.values():
            surface.installEventFilter(self)
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

    # ---- layout -------------------------------------------------------------------------

    def _small(self) -> QWidget:
        return self._surfaces["pip" if self._large == "main" else "main"]

    def _saved_corner(self) -> str:
        return _valid_corner(getattr(self._config, "pip_corner", DEFAULT_CORNER))

    def _alive(self) -> bool:
        # The garbage collector can clear this wrapper while Qt, deleting the container,
        # still sends events through the C++ object's filter.
        container, surfaces = getattr(self, "_container", None), getattr(self, "_surfaces", {})
        return (
            container is not None
            and isValid(container)
            and bool(surfaces)
            and all(isValid(s) for s in surfaces.values())
        )

    def _layout(self) -> None:
        if not self._alive():
            return
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
        if not self._alive():
            return False
        kind = event.type()
        if watched is self._container:
            if kind in _LAYOUT_EVENTS:
                self._layout()
            return False
        if kind in _MOUSE_EVENTS and isinstance(event, QMouseEvent):
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
            if kind == QEvent.Type.MouseButtonDblClick and self._swallow_double:
                self._swallow_double = False
                self._press = _Press(watched, position, watched.pos(), void=True)
                return True
            self._swallow_double = False
            small = self._small()
            if not self._shown or watched is not small:
                return False
            self._press = _Press(small, position, small.pos())
            return True
        press = self._press
        if press is None or watched is not press.surface:
            return False
        if kind == QEvent.Type.MouseMove:
            if not event.buttons() & left:  # the release was lost (left the window)
                self._release(press, clicked=False)
                return False
            if not press.void:
                self._drag(press, position)
            return True
        if kind == QEvent.Type.MouseButtonRelease and event.button() == left:
            self._release(press, clicked=True)
            return True
        return False

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

    def _release(self, press: _Press, *, clicked: bool) -> None:
        self._press = None
        if press.void:
            return
        if press.dragging:
            self._snap()
        elif clicked:
            self._swallow_double = True
            self.swapRequested.emit()
