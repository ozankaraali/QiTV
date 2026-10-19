"""Picture-in-picture frame: the second video surface, small in a corner of the player (spec §7).

The surfaces are named as their players' slots ("main", "pip", and a multiview's tiles once
added). Every surface is a native child of the player page and never changes parent, so the
windows libVLC draws into never change: a swap exchanges two surfaces' geometry and stacking
only. The large surface fills the page, the PiP's sits in a corner while it shows, and any
other is hidden.

The small surface takes the left button (the shell's click would open the drawer): a click
asks for a swap, and a drag moves it and, on release, snaps it to the nearest corner. These
are the Qt mouse events the shell reads on the main surface (libVLC's own mouse handling is
off, so they reach Qt on macOS too). Other buttons, the wheel and moves go on to the shell.
The PiP label's pill, drawn over the small surface, forwards its own presses the same way
(pill_press, pill_move, pill_release).

While the transport (or the mixer) shows, a bottom corner's PiP rises above the transport's
controls (`set_lift`, animated over LIFT_MS) and settles back once they hide; a top corner's
never moves. A drag in progress keeps the surface under the cursor: the lift applies when
it ends.
"""

from dataclasses import dataclass
import logging

from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from services.redact import safe_message
from ui.macos_window import reclaim_first_responder
from ui.overlays.painting import Units

logger = logging.getLogger(__name__)

CORNERS = ("br", "bl", "tr", "tl")
DEFAULT_CORNER = "br"
WIDTH_FRACTION = 0.25
HEIGHT_PER_WIDTH = 9 / 16
MARGIN_UNITS = 2.0  # from the nearest edges; u = 1% of the player width
DRAG_START_PX = 8
LIFT_MS = 150  # a bottom corner's PiP rises over the transport band, or settles, this fast
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


def lifted_rect(size: QSize, corner: str, lift: int) -> QRect:
    """`corner_rect`, raised by `lift` pixels for a bottom corner (never above the margin)."""
    rect = corner_rect(size, corner)
    if corner.startswith("b") and lift > 0:
        margin = Units(size.width()).px(MARGIN_UNITS)
        rect.moveTop(max(margin, rect.top() - lift))
    return rect


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
        self._surfaces: dict[str, QWidget] = {"main": main_surface, "pip": pip_surface}
        self._config = config
        self._large = "main"
        self._small_name = "pip"  # the PiP's surface (in the corner while shown)
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
        self._interactive = True  # False: the player is a thumbnail over the Library
        self._lift = 0  # pixels a bottom corner's PiP rises (the transport shows)
        self._lifting = QVariantAnimation(self)
        self._lifting.setDuration(LIFT_MS)
        self._lifting.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._lifting.valueChanged.connect(self._on_lift_step)
        self._lifting.finished.connect(self._on_lifted)
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
        """Hide the small surface; the large one fills the player (as SessionManager.close_pip,
        which stops the small picture: after a swap the large surface is the "pip" one)."""
        self._shown = False
        self._layout()

    def add_surface(self, name: str, surface: QWidget) -> None:
        """Another player's surface (a multiview tile's): hidden until it is large or small."""
        if name in self._surfaces:
            raise ValueError(f"There is a {name!r} surface already")
        self._surfaces[name] = surface
        if not self._closed:
            surface.installEventFilter(self)
        self._layout()

    def set_large(self, name: str) -> None:
        """Make surface `name` fill the player. When it was the PiP's, the one that was large
        becomes the PiP's (a swap: it goes to the corner, on top)."""
        if name not in self._surfaces:
            raise ValueError(f"Unknown surface: {name!r}")
        if name == self._small_name:
            self._small_name = self._large
        self._large = name
        self._layout()

    def set_roles(self, large: str, small: str) -> None:
        """Surface `large` fills the player and `small` is the PiP's (shown in the corner
        while picture in picture shows)."""
        for name in (large, small):
            if name not in self._surfaces:
                raise ValueError(f"Unknown surface: {name!r}")
        if large == small:
            raise ValueError("The large and the small surface are one")
        self._large, self._small_name = large, small
        self._layout()

    def small_rect(self) -> QRect:
        """Where the small surface is, in container coordinates; empty while PiP is hidden."""
        if not self._shown:
            return QRect()
        return QRect(self._small().geometry())

    def corner(self) -> str:
        return self._corner

    def set_lift(self, lift: int) -> None:
        """Raise a bottom corner's PiP by `lift` pixels (0: back in its corner), animated."""
        lift = max(0, int(lift))
        if lift == self._lift:
            return
        self._lift = lift
        if not self._shown or self._press is not None or self._closed:
            return  # shown, or once the drag ends, it lays out with the new lift
        small = self._small()
        target = self._small_target()
        self._lifting.stop()  # a lift on its way the other way ends where it is
        if small.geometry() == target:
            return
        self._lifting.setStartValue(small.geometry())
        self._lifting.setEndValue(target)
        self._lifting.start()

    def set_interactive(self, interactive: bool) -> None:
        """Whether the small surface takes the mouse (a click swaps, a drag moves it). Off
        while the whole player sits in a corner of the Library: its presses are the corner
        player's."""
        self._interactive = interactive
        if not interactive:
            self._press = None
            self._swap_at = None

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
        if self._shown and not self._closed and self._interactive:
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
        return self._surfaces[self._small_name]

    def _saved_corner(self) -> str:
        return _valid_corner(getattr(self._config, "pip_corner", DEFAULT_CORNER))

    def lifting(self) -> bool:
        """The small surface is on its way up or down (set_lift)."""
        return self._lifting.state() == QVariantAnimation.State.Running

    def _small_target(self) -> QRect:
        return lifted_rect(self._container.size(), self._corner, self._lift)

    def _on_lift_step(self, rect) -> None:
        if isinstance(rect, QRect) and self._shown and self._press is None:
            self._small().setGeometry(rect)
            self._report()

    def _on_lifted(self) -> None:
        if self._shown:
            self._small().raise_()  # macOS: kept above the large one after the moves

    def _layout(self) -> None:
        self._lifting.stop()
        if self._press is not None:
            self._press.void = True  # its release neither swaps nor snaps
        large = self._surfaces[self._large]
        large.setGeometry(self._container.rect())
        large.show()  # it may have been the small one, hidden with PiP closed
        small = self._small()
        for name, surface in self._surfaces.items():
            if name not in (self._large, self._small_name):
                surface.hide()  # a tile's, outside the multiview
        if self._shown:
            small.setGeometry(self._small_target())
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
        # Raised over the transport, the bottom half starts that much higher.
        area = self._container.size()
        corner = nearest_corner(
            self._small().geometry(), QSize(area.width(), max(1, area.height() - self._lift))
        )
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
            logger.warning("Couldn't save the picture-in-picture corner: %s", safe_message(e))

    # ---- mouse --------------------------------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # The event type first: a page being deleted still sends others (hide, deactivate)
        # through this filter, possibly after the garbage collector cleared this wrapper.
        kind = event.type()
        if kind in _LAYOUT_EVENTS:
            if watched is self._container:
                self._layout()
        elif kind in _MOUSE_EVENTS and isinstance(event, QMouseEvent) and self._interactive:
            return self._mouse(watched, event)
        return False

    def _mouse(self, watched: QObject, event: QMouseEvent) -> bool:
        """True when the event is the frame's: the shell's surface handling never sees it."""
        if not isinstance(watched, QWidget):
            return False
        kind, left = event.type(), Qt.MouseButton.LeftButton
        if kind != QEvent.Type.MouseMove and watched in self._surfaces.values():
            # macOS: libVLC's view in the clicked surface took the keys. The window's filter
            # never sees the presses this frame takes.
            reclaim_first_responder(self._container.window())
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
        self._lifting.stop()  # the surface stays where the press found it
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
            self._settle()
            return
        if press.dragging:
            self._snap()
        elif position is not None:
            self._swap_at = QPoint(position)
            self._swap_clock.start()
            self.swapRequested.emit()
        self._settle()

    def _settle(self) -> None:
        """A lift asked for during the press applies now (the swap may have laid it out)."""
        if self._shown and not self._closed and self._small().geometry() != self._small_target():
            self._layout()
