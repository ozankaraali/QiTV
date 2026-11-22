"""The small window a picture floats in on macOS (2.4; 2.13.5): frameless, above other windows,
in its picture's shape. Two kinds share it: the desktop picture in picture's (ui/desktop_pip.py:
the large surface, when the fullscreen window's desktop is left or the window is covered) and
the mini player's (ui/mini_player.py: the whole player page, under the window's controls).

It has its picture's shape, so it shows no bars (`set_ratio`: as large as it was, its corner
nearest the screen's corner staying where it is). Its edges and corners resize it in that shape
(a press within EDGE_PX of them; the pointer says so near them); while the shape is not known
(or fits any: Fill) it keeps the one it has. Qt makes a frameless window resizable by macOS
itself too, at its very edges, without a press QiTV sees (2.13.5: the owner's black bars after
a resize): macOS keeps the shape as they are dragged, between the smallest window and the
screen (`lock_shape`: its contentAspectRatio, contentMinSize and contentMaxSize, told again by
`relock` once it shows), and a resize QiTV did not make ends in the shape anyway, SNAP_MS after it
stops. A window as good as its picture's shape already (within 1%) is left as it is, and its
player fills it to the last pixel (the TV window tells it the box, services.picture.box_aspect).
Where the user resized it is told (`placed`).
"""

from __future__ import annotations

import math
from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from services.picture import box_height, box_ratio, box_size, same_shape
from ui.macos_window import reclaim_first_responder

WIDTH_FRACTION = 0.25  # a quarter of the screen's width, 16:9
MARGIN = 24  # from the screen's corner
EDGE_PX = 10  # a press this near an edge (or two: a corner) resizes the small window
MIN_WIDTH = 240  # the smallest it gets in 16:9 (another shape: as large as that)
SNAP_MS = 150  # a resize it did not make (macOS's own) ends in its picture's shape this soon

LockShape = Callable[[QWidget, float, QSize, QSize], object]


def corner_rect(available: QRect, screen_width: int) -> QRect:
    """The small window in the bottom-right corner of the screen's `available` area."""
    width = round(screen_width * WIDTH_FRACTION)
    height = round(width * 9 / 16)
    x = available.x() + available.width() - width - MARGIN
    y = available.y() + available.height() - height - MARGIN
    return QRect(x, y, width, height)


def _no_lock(_widget: QWidget, _shape: float, _smallest: QSize, _largest: QSize) -> None:
    return None


class SmallWindow(QWidget):
    """A frameless window on top, in its picture's shape (the module docstring). A subclass
    holds the picture and hands it a left press (`_begin_press`: True on an edge, a resize
    begins), the moves that follow (`_resize_to`) and the release (`_end_press`)."""

    placed = Signal(QRect)  # moved or resized by the user: where it is now

    def __init__(self, flags: Qt.WindowType, lock_shape: LockShape = _no_lock) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | flags,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("QiTV")
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("black"))
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self._lock_shape = lock_shape
        self._cursor_on: Optional[QWidget] = None  # where it set a resize pointer
        self._press: Optional[QPoint] = None  # where a press began (global)
        self._dragged = False
        self._edges: tuple[bool, bool, bool, bool] = (False, False, False, False)  # l, t, r, b
        self._start = QRect()  # the window's geometry when a resize began
        self._ratio: Optional[float] = None  # its picture's shape (None: it keeps its own)
        self._reshape_due = False  # a new shape came during a press or macOS's resize
        self._locked: Optional[tuple[float, QSize]] = None  # what macOS was told to keep
        self._expected = QRect()  # where QiTV put it last (else the user resized it)
        self._snap = QTimer(self, singleShot=True, interval=SNAP_MS)
        self._snap.timeout.connect(self._settle)

    # ---- where it is ------------------------------------------------------------------------

    def put(self, rect: QRect) -> None:
        """Where it goes, QiTV's choice (not the user's: never told as `placed`)."""
        self._expected = QRect(rect)
        self.setGeometry(rect)

    def set_ratio(self, ratio: Optional[float]) -> None:
        """Its picture shows in `ratio`'s shape (width / height; None: not known, or it fits
        any): the window takes it, as large as it was, the corner nearest its screen's corner
        staying where it is, inside the screen. During a press or macOS's own resize, once it
        ends. macOS keeps the shape it has then as its edges are dragged."""
        self._ratio = ratio
        if self._press is not None or self._snap.isActive():
            self._reshape_due = True
        else:
            self._reshape()
        self._lock()

    def relock(self) -> None:
        """Tell macOS the shape to keep again (Qt clears it as it makes the native window)."""
        self._lock(again=True)

    def _lock(self, again: bool = False) -> None:
        shape = self._shape()
        available = self._available(self.geometry())
        largest = available.size() if available is not None else QSize(4096, 4096)
        if again or (shape, largest) != self._locked:
            self._locked = (shape, largest)
            self._lock_shape(self, shape, QSize(*box_size(MIN_WIDTH, shape)), largest)

    def _reshape(self) -> None:
        """In its shape (`_shape`) and never under the smallest window, as large as it was."""
        self._reshape_due = False
        rect = self.geometry()
        if rect.isEmpty():
            return
        shape = self._shape()
        smallest = box_size(MIN_WIDTH, shape)[0]
        if same_shape(shape, rect.width() / rect.height()) and rect.width() >= smallest:
            return  # (as good as that shape already: no pixel of jitter)
        available = self._available(rect)
        width = self._clamped(round(math.sqrt(rect.width() * rect.height() * shape)), shape)
        height = box_height(width, shape)
        x, y = rect.x(), rect.y()
        if available is not None and rect.center().x() > available.center().x():
            x = rect.x() + rect.width() - width  # (its right edge stays)
        if available is not None and rect.center().y() > available.center().y():
            y = rect.y() + rect.height() - height
        if available is not None:
            x = max(available.x(), min(x, available.x() + available.width() - width))
            y = max(available.y(), min(y, available.y() + available.height() - height))
        self.put(QRect(x, y, width, height))

    def _settle(self) -> None:
        """A resize stopped. One it did not make (macOS's own, at its edges) ends in its shape,
        and is the user's: told (`placed`), as one from its own edges is."""
        if self._press is not None:
            return
        users = self.geometry() != self._expected
        self._reshape()
        if users:
            self._expected = self.geometry()
            self.placed.emit(self.geometry())
        self._lock()

    def _available(self, rect: QRect) -> Optional[QRect]:
        """The available area of the screen under `rect` (None: no screen there)."""
        screen = QGuiApplication.screenAt(rect.center()) or self.screen()
        return screen.availableGeometry() if screen is not None else None

    def _shape(self) -> float:
        """The shape it keeps: its picture's, else the one the window has (when a resize began,
        during one)."""
        if self._ratio is not None:
            return box_ratio(self._ratio)
        base = self._start if self._press is not None else self.geometry()
        return box_ratio(base.width() / base.height() if not base.isEmpty() else None)

    def _clamped(self, width: int, shape: float) -> int:
        """`width` between the smallest window (as large as a 16:9 one MIN_WIDTH wide) and the
        widest one of `shape` the screen has room for."""
        smallest = box_size(MIN_WIDTH, shape)[0]
        available = self._available(self.geometry())
        if available is None:
            return max(smallest, min(width, 4096))
        widest = min(available.width(), math.floor(available.height() * shape))
        return max(smallest, min(width, max(smallest, widest)))

    def resizeEvent(self, event) -> None:
        if self._press is None and self.geometry() != self._expected:
            self._snap.start()  # one it did not make (macOS's own, at its edges): once it stops
        super().resizeEvent(event)

    # ---- its edges ----------------------------------------------------------------------------

    def _edges_at(self, point: QPoint) -> tuple[bool, bool, bool, bool]:
        """The edges (left, top, right, bottom) `point` (window coordinates) is near."""
        rect = self.rect()
        return (
            point.x() < EDGE_PX,
            point.y() < EDGE_PX,
            point.x() >= rect.width() - EDGE_PX,
            point.y() >= rect.height() - EDGE_PX,
        )

    def _cursor_for(self, edges: tuple[bool, bool, bool, bool]) -> Optional[Qt.CursorShape]:
        left, top, right, bottom = edges
        if (left and top) or (right and bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if (right and top) or (left and bottom):
            return Qt.CursorShape.SizeBDiagCursor
        if left or right:
            return Qt.CursorShape.SizeHorCursor
        if top or bottom:
            return Qt.CursorShape.SizeVerCursor
        return None

    def _point_at(self, watched: QWidget, where: QPoint) -> None:
        """The pointer over `watched` at `where` (global): a resize one near an edge, else its
        own again."""
        shape = self._cursor_for(self._edges_at(self.mapFromGlobal(where)))
        if self._cursor_on is not None and self._cursor_on is not watched:
            if isValid(self._cursor_on):
                self._cursor_on.unsetCursor()
            self._cursor_on = None
        if shape is not None:
            watched.setCursor(shape)
            self._cursor_on = watched
        elif self._cursor_on is watched:
            watched.unsetCursor()
            self._cursor_on = None

    def _forget_pointer(self) -> None:
        if self._cursor_on is not None and isValid(self._cursor_on):
            self._cursor_on.unsetCursor()
        self._cursor_on = None

    def _begin_press(self, where: QPoint) -> bool:
        """A left press at `where` (global): True when it is on an edge (a resize begins)."""
        self._press, self._dragged = where, False
        self._edges = self._edges_at(self.mapFromGlobal(where))
        self._start = self.geometry()
        if any(self._edges):
            reclaim_first_responder(self)  # macOS: libVLC's view took the keys (a beep)
            return True
        return False

    def _drag_far_enough(self, where: QPoint) -> bool:
        """The press moved far enough to be a drag (and stays one)."""
        if self._press is None:
            return False
        moved = where - self._press
        if self._dragged or moved.manhattanLength() >= QApplication.startDragDistance():
            self._dragged = True
        return self._dragged

    def _resize_to(self, where: QPoint) -> None:
        """An edge press moved to `where`: in its picture's shape, the edges opposite those
        dragged staying where they were, between the smallest window and the largest the
        screen has room for."""
        if self._press is None or not self._drag_far_enough(where):
            return
        moved = where - self._press
        left, top, right, bottom = self._edges
        start = self._start
        shape = self._shape()
        widths = []
        if left or right:
            widths.append(start.width() + (moved.x() if right else -moved.x()))
        if top or bottom:
            height = start.height() + (moved.y() if bottom else -moved.y())
            widths.append(round(height * shape))
        width = max(widths, key=lambda w: abs(w - start.width()))
        width = self._clamped(int(width), shape)
        height = box_height(width, shape)
        x = start.x() + start.width() - width if left else start.x()
        y = start.y() + start.height() - height if top else start.y()
        self.put(QRect(x, y, width, height))

    def _end_press(self) -> None:
        """The left button came up: a move or a resize is told (a click alone does nothing
        here). A shape that came meanwhile applies."""
        dragged = self._press is not None and self._dragged
        self._press, self._dragged = None, False
        if dragged:
            self._expected = self.geometry()
            self.placed.emit(self.geometry())
        if self._reshape_due:
            self._reshape()
        self._lock()
