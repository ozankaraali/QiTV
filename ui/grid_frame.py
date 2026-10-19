"""Multiview's grid: the players' surfaces as tiles of the player page.

The native views libVLC draws into never change parent (macOS loses the picture), so the
grid is a layout of the player page, as the PiP frame's corner is: `grid_rects` places 2
(1×2), 4 (2×2), 6 (3×2) or 9 (3×3) tiles, each 16:9, as large as the page allows and
centred. A tile whose player shows nothing (an empty "+" tile, a "No stream to spare" card)
hides its surface: the tiles overlay (ui/overlays/grid_tiles.py) draws it and takes its
clicks. The PiP frame stands aside meanwhile (`PipFrame.suspend`).

The mouse, while the grid shows:
- a left press on a tile that is not focused asks for its focus (`focusRequested`) and goes
  no further: the shell's click would open the channel list. The focused tile's press goes
  on to the shell, as on the big picture (a click opens the channel list, a double-click
  leaves the grid);
- the overlay's presses ask for the focus too; its clicks (after the double-click interval)
  come as `tileClicked` (a "+" tile opens the channel list, a card tries again) and its
  double-clicks as `leaveRequested`.
"""

from __future__ import annotations

import logging
from typing import Mapping, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from ui.macos_window import reclaim_first_responder
from ui.overlays.painting import Units

logger = logging.getLogger(__name__)

LAYOUTS = {2: (2, 1), 4: (2, 2), 6: (3, 2), 9: (3, 3)}  # tiles: (columns, rows)
GAP_UNITS = 0.4  # between tiles; u = 1% of the page width
ASPECT = 16 / 9
_LAYOUT_EVENTS = (QEvent.Type.Resize, QEvent.Type.Show)


def grid_rects(size: QSize, cells: int) -> list[QRect]:
    """The tiles' rects in a page of `size`, row by row: 16:9 each, `GAP_UNITS` apart, the
    grid as large as fits and centred. `cells` is one of LAYOUTS."""
    if cells not in LAYOUTS:
        raise ValueError(f"No layout for {cells} tiles")
    columns, rows = LAYOUTS[cells]
    if size.width() <= 0 or size.height() <= 0:
        return [QRect() for _ in range(cells)]
    gap = Units(size.width()).px(GAP_UNITS)
    room_w = size.width() - gap * (columns - 1)
    room_h = size.height() - gap * (rows - 1)
    width = max(1.0, min(room_w / columns, room_h / rows * ASPECT))
    height = width / ASPECT
    left = (size.width() - (width * columns + gap * (columns - 1))) / 2
    top = (size.height() - (height * rows + gap * (rows - 1))) / 2
    rects = []
    for row in range(rows):
        y0 = round(top + row * (height + gap))
        y1 = round(top + row * (height + gap) + height)
        for column in range(columns):
            x0 = round(left + column * (width + gap))
            x1 = round(left + column * (width + gap) + width)
            rects.append(QRect(x0, y0, x1 - x0, y1 - y0))
    return rects


class GridFrame(QObject):
    focusRequested = Signal(str)  # a left press on a tile that is not focused
    tileClicked = Signal(str)  # a click on what the overlay draws ("+", a card, a label)
    leaveRequested = Signal(str)  # a double-click on what the overlay draws
    tilesChanged = Signal()  # laid out anew (shown, resized, tiles or focus changed)

    def __init__(self, container: QWidget, surfaces: Mapping[str, QWidget], overlay=None) -> None:
        """`surfaces` are the players' by their slots' names; `overlay` the GridTiles (None
        in tests without one)."""
        super().__init__(container)
        self._container = container
        self._surfaces: dict[str, QWidget] = {}
        self._names: list[str] = []  # the tiles, in order
        self._focused = ""
        self._playing: set[str] = set()  # the tiles whose surface shows
        self._shown = False
        self._closed = False
        container.installEventFilter(self)
        for name, surface in surfaces.items():
            self.add_surface(name, surface)
        if overlay is not None:
            overlay.tilePressed.connect(self._on_overlay_pressed)
            overlay.tileClicked.connect(self.tileClicked)
            overlay.leaveRequested.connect(self.leaveRequested)

    # ---- public API ---------------------------------------------------------------------

    def add_surface(self, name: str, surface: QWidget) -> None:
        """Another player's surface (a tile's, made when the tile first takes the focus or a
        channel): laid out at once while its tile shows."""
        if name in self._surfaces:
            raise ValueError(f"There is a {name!r} surface already")
        self._surfaces[name] = surface
        if not self._closed:
            surface.installEventFilter(self)
        if name in self._names:
            self._layout()

    def show_grid(self, names: list[str], focused: str, playing: set[str]) -> None:
        """Lay `names` out as tiles, in order; `focused` has the focus, `playing` show (a tile
        without a surface yet is the overlay's alone)."""
        self._check(names)
        self._names, self._focused, self._playing = list(names), focused, set(playing)
        self._shown = True
        self._layout()

    def set_tiles(self, names: list[str], focused: str, playing: set[str]) -> None:
        """Other tiles (a layout change), focus or players; laid out at once while shown."""
        self._check(names)
        self._names, self._focused, self._playing = list(names), focused, set(playing)
        self._layout()

    def hide_grid(self) -> None:
        """Stop laying the surfaces out; the PiP frame takes them back."""
        self._shown = False
        self._names, self._focused, self._playing = [], "", set()

    def shown(self) -> bool:
        return self._shown

    def names(self) -> list[str]:
        return list(self._names)

    def focused(self) -> str:
        return self._focused

    def area(self) -> QRect:
        """The page the grid covers (its own coordinates: the overlay host's too)."""
        return self._container.rect()

    def map_from_global(self, point: QPoint) -> QPoint:
        """`point` (global) in the page's coordinates, as `tiles()` gives the rects."""
        return self._container.mapFromGlobal(point)

    def tiles(self) -> list[tuple[str, QRect]]:
        """Each tile's name and rect (container coordinates), in order; none while hidden."""
        if not self._shown:
            return []
        return list(zip(self._names, grid_rects(self._container.size(), len(self._names))))

    def rect_of(self, name: str) -> QRect:
        for tile, rect in self.tiles():
            if tile == name:
                return QRect(rect)
        return QRect()

    def close(self) -> None:
        """Stop watching the page and the surfaces (the window's shutdown)."""
        if self._closed:
            return
        self._closed = True
        self._shown = False
        for watched in (self._container, *self._surfaces.values()):
            if isValid(watched):
                watched.removeEventFilter(self)

    # ---- layout -------------------------------------------------------------------------

    def _check(self, names: list[str]) -> None:
        if len(names) not in LAYOUTS:
            raise ValueError(f"No layout for {len(names)} tiles")

    def _layout(self) -> None:
        if not self._shown or self._closed:
            return
        for name, rect in self.tiles():
            surface = self._surfaces.get(name)
            if surface is None:
                continue  # no player yet: the overlay draws its "+"
            surface.setGeometry(rect)
            surface.setVisible(name in self._playing)
        for name, surface in self._surfaces.items():
            if name not in self._names:
                surface.hide()
        self.tilesChanged.emit()

    # ---- mouse --------------------------------------------------------------------------

    def _on_overlay_pressed(self, name: str) -> None:
        if self._shown and name != self._focused:
            self.focusRequested.emit(name)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind in _LAYOUT_EVENTS:
            if watched is self._container:
                self._layout()
            return False
        if (
            not self._shown
            or kind not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
            or not isinstance(event, QMouseEvent)
            or event.button() != Qt.MouseButton.LeftButton
        ):
            return False
        name = self._name_of(watched)
        if name is None or name == self._focused:
            return False  # the focused tile's: the shell's, as on the big picture
        # macOS: libVLC's view in the clicked surface took the keys, and the window's own
        # filter never sees a press taken here.
        reclaim_first_responder(self._container.window())
        self.focusRequested.emit(name)
        return True

    def _name_of(self, watched: QObject) -> Optional[str]:
        for name in self._names:
            if self._surfaces.get(name) is watched:
                return name
        return None
