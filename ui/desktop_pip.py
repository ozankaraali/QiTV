"""Desktop picture in picture (macOS, 2.4): leaving the fullscreen player's desktop for another
one (a swipe, Ctrl+→, another app's desktop) floats the picture in a small window on every
desktop; coming back to the fullscreen desktop puts it back. Like a browser's picture in
picture on a tab change.

While the TV window is fullscreen (and the setting is on, one picture plays, the multiview's
grid is away), `DesktopPip` asks every `POLL_MS` whether the window is on the desktop shown
(`on_active_space`: on macOS [NSWindow isOnActiveSpace]), and at once when the window loses or
gets the focus (`nudge`), as it does on a desktop change. When it is no longer, the large
picture's surface is lent by the player (`lend`, PipFrame.lend_large) and moved into the
`FloatingPlayer`: the surface keeps its native view, and libVLC, which draws into a view of its
own inside it, goes on drawing (measured: no picture lost, no reopen). The fullscreen window
stays where it was, showing what the player page shows without its picture. Back on that
desktop, the surface returns (`take_back`) and the floating window hides; a double-click on it
brings the fullscreen desktop back, which puts the picture back the same way. Leaving fullscreen or
closing QiTV puts it back first. The floating window can be dragged; it shows on every desktop
(`join_all_spaces`), above other windows, and never takes the keyboard.

The floating window has its picture's shape, so it shows no bars: `shape()` (the lent player's
`picture_ratio`) is asked as it floats and at each check while it does, and a new shape (the
stream's picture becomes known, a zap, another aspect ratio) reshapes it, as large as it was,
its corner nearest the screen's corner staying where it is. Its edges and corners resize it in
that shape; while the shape is not known (or fits any: Fill) it keeps the one it has.
"""

from __future__ import annotations

import logging
import math
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from services.picture import box_height, box_ratio, box_size, same_shape

logger = logging.getLogger(__name__)

POLL_MS = 400  # how often the fullscreen window's desktop is asked about
WIDTH_FRACTION = 0.25  # the floating window: a quarter of the screen's width, 16:9
MARGIN = 24  # from the screen's corner
EDGE_PX = 10  # a press this near an edge (or two: a corner) resizes the floating window
MIN_WIDTH = 240  # the smallest it gets in 16:9 (another shape: as large as that)
AWAY_CHECKS = 2  # answers "another desktop" in a row before the picture floats (the
# fullscreen animation may answer it once)
CONFIRM_MS = 150  # the second answer is asked this soon after the first, not a poll later
_MOUSE_EVENTS = (
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseMove,
    QEvent.Type.Wheel,  # never the shell's: a scroll drifting over it would zap
)


def corner_rect(available: QRect, screen_width: int) -> QRect:
    """The floating window in the bottom-right corner of the screen's `available` area."""
    width = round(screen_width * WIDTH_FRACTION)
    height = round(width * 9 / 16)
    x = available.x() + available.width() - width - MARGIN
    y = available.y() + available.height() - height - MARGIN
    return QRect(x, y, width, height)


class FloatingPlayer(QWidget):
    """A frameless panel on top that holds the large picture's surface; a click asks for the
    fullscreen desktop again on a double-click (`returnRequested`; a click alone does nothing, so
    a press to grab it never does), a drag moves it, a drag of an edge or a corner
    resizes it in its picture's shape (`set_ratio`; the pointer says so near them); where it
    ended up is told (`placed`). A panel that takes no focus
    (on macOS a non-activating one): a press on it leaves QiTV inactive and the desktop shown,
    so it can be dragged there."""

    returnRequested = Signal()
    placed = Signal(QRect)  # moved or resized by the user: where it is now

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("QiTV")
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("black"))
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self._surface: Optional[QWidget] = None
        self._press: Optional[QPoint] = None  # where a press began (global)
        self._origin = QPoint()  # the window's place when it did
        self._dragged = False
        self._edges: tuple[bool, bool, bool, bool] = (False, False, False, False)  # l, t, r, b
        self._start = QRect()  # the window's geometry when a resize began
        self._ratio: Optional[float] = None  # its picture's shape (None: it keeps its own)
        self._reshape_due = False  # a new shape came during a press: once it ends

    def surface(self) -> Optional[QWidget]:
        return self._surface

    def hold(self, surface: QWidget) -> None:
        """`surface` moves into this window and fills it; its mouse is this window's now."""
        self._surface = surface
        surface.setParent(self)
        surface.setGeometry(self.rect())
        surface.show()
        surface.setMouseTracking(True)  # moves without a button: the edges' pointer
        surface.installEventFilter(self)

    def release(self) -> Optional[QWidget]:
        """The surface held, for its window to take back (None: none)."""
        surface, self._surface = self._surface, None
        if surface is not None:
            surface.removeEventFilter(self)
        self._press, self._dragged = None, False
        self._reshape_due = False
        if surface is not None:
            surface.unsetCursor()
        return surface

    def set_ratio(self, ratio: Optional[float]) -> None:
        """Its picture shows in `ratio`'s shape (width / height; None: not known, or it fits
        any): the window takes it, as large as it was, the corner nearest its screen's corner
        staying where it is, inside the screen. During a press, once the press ends."""
        self._ratio = ratio
        if self._press is not None:
            self._reshape_due = True
        else:
            self._reshape()

    def _reshape(self) -> None:
        self._reshape_due = False
        rect = self.geometry()
        if self._ratio is None or rect.isEmpty():
            return
        shape = box_ratio(self._ratio)
        if same_shape(shape, rect.width() / rect.height()):
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
        self.setGeometry(QRect(x, y, width, height))

    def _available(self, rect: QRect) -> Optional[QRect]:
        """The available area of the screen under `rect` (None: no screen there)."""
        screen = QGuiApplication.screenAt(rect.center()) or self.screen()
        return screen.availableGeometry() if screen is not None else None

    def _shape(self) -> float:
        """The shape a resize keeps: its picture's, else the one the window has."""
        if self._ratio is not None:
            return box_ratio(self._ratio)
        start = self._start if not self._start.isEmpty() else self.geometry()
        return box_ratio(start.width() / start.height() if not start.isEmpty() else None)

    def _clamped(self, width: int, shape: float) -> int:
        """`width` between the smallest window (as large as a 16:9 one MIN_WIDTH wide) and the
        widest one of `shape` the screen has room for."""
        smallest = box_size(MIN_WIDTH, shape)[0]
        available = self._available(self.geometry())
        if available is None:
            return max(smallest, min(width, 4096))
        widest = min(available.width(), math.floor(available.height() * shape))
        return max(smallest, min(width, max(smallest, widest)))

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

    def _resized(self, moved: QPoint) -> QRect:
        """The geometry a resize by `moved` (global) gives: in its picture's shape (`_shape`),
        the edges opposite those dragged staying where they were, between the smallest window
        and the largest the screen has room for."""
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
        return QRect(x, y, width, height)

    def resizeEvent(self, event) -> None:
        if self._surface is not None:
            self._surface.setGeometry(self.rect())
        super().resizeEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self._surface or event.type() not in _MOUSE_EVENTS:
            return False
        if not isinstance(event, QMouseEvent):
            return True  # the wheel
        left = Qt.MouseButton.LeftButton
        where = event.globalPosition().toPoint()
        kind = event.type()
        if kind == QEvent.Type.MouseButtonDblClick and event.button() == left:
            self._press, self._dragged = None, False
            self.returnRequested.emit()  # a double-click: the fullscreen desktop again
        elif kind == QEvent.Type.MouseButtonPress and event.button() == left:
            self._press, self._origin, self._dragged = where, self.pos(), False
            self._edges = self._edges_at(self.mapFromGlobal(where))
            self._start = self.geometry()
        elif kind == QEvent.Type.MouseMove and self._press is not None:
            moved = where - self._press
            if self._dragged or moved.manhattanLength() >= QApplication.startDragDistance():
                self._dragged = True
                if any(self._edges):
                    self.setGeometry(self._resized(moved))
                else:
                    self.move(self._origin + moved)
        elif kind == QEvent.Type.MouseMove:
            shape = self._cursor_for(self._edges_at(self.mapFromGlobal(where)))
            if shape is None:
                watched.unsetCursor()  # (a QWidget: the surface)
            else:
                watched.setCursor(shape)
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == left:
            self._end_press()
        return True  # the shell's clicks on the picture are for the fullscreen window

    def _end_press(self) -> None:
        """The left button came up: a move or a resize is told; a click alone does nothing (a
        press to grab it must never send the user back). A shape that came meanwhile applies."""
        dragged = self._press is not None and self._dragged
        self._press, self._dragged = None, False
        if dragged:
            self.placed.emit(self.geometry())
        if self._reshape_due:
            self._reshape()


class DesktopPip(QObject):
    """Floats the large picture while the fullscreen TV window's desktop is not shown."""

    floatingChanged = Signal(bool)

    def __init__(
        self,
        window: QWidget,
        *,
        on_active_space: Callable[[QWidget], Optional[bool]],
        may_float: Callable[[], bool],
        lend: Callable[[], Optional[QWidget]],
        take_back: Callable[[QWidget], None],
        place: Callable[[], QRect],
        join_all_spaces: Callable[[QWidget], object],
        remember: Optional[Callable[[QRect], None]] = None,
        shape: Optional[Callable[[], Optional[float]]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """`on_active_space(window)`: whether the window is on the desktop shown (None: can't
        tell); `may_float()`: whether a picture may float now; `lend()`/`take_back(surface)`:
        the large surface out of and back into the player; `place()`: where the floating
        window goes (screen coordinates); `join_all_spaces(widget)`: show it on every desktop;
        `remember(rect)`: where the user moved or resized it (`place` gives it back next time);
        `shape()`: the shape the lent picture shows in (None: not known; it keeps its own)."""
        super().__init__(parent)
        self._window = window
        self._on_active_space = on_active_space
        self._may_float = may_float
        self._lend = lend
        self._take_back = take_back
        self._place = place
        self._join_all_spaces = join_all_spaces
        self._remember = remember
        self._shape = shape
        self._floating: Optional[FloatingPlayer] = None
        self._shown = False
        self._away = 0  # "another desktop" answers in a row
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self.check)

    def floating(self) -> bool:
        return self._shown

    def floating_window(self) -> Optional[FloatingPlayer]:
        """The floating window, once the picture has floated (None before)."""
        return self._floating

    def nudge(self) -> None:
        """Something suggests the desktop changed (the window lost or got the focus): ask
        now, not at the next poll."""
        if self._timer.isActive():
            self.check()

    def watch(self, on: bool) -> None:
        """Ask about the desktop (the window is fullscreen) or not; not puts the picture back."""
        if on:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._away = 0
            self.dock()

    def check(self) -> None:
        """The window's desktop now: float the picture when it is away, put it back when not."""
        if self._shown and not self._may_float():
            self.dock()  # nothing to float any more (it stopped, the grid or the Library came)
            return
        shown = self._on_active_space(self._window)
        if shown is None:
            return
        self._away = 0 if shown else self._away + 1
        if self._away >= AWAY_CHECKS and not self._shown and self._may_float():
            self._float()
        elif shown and self._shown:
            self.dock()
        elif self._away == 1 and not self._shown and self._timer.isActive():
            QTimer.singleShot(CONFIRM_MS, self, self.nudge)  # (the poll would take longer)
        elif self._shown:
            self._follow_shape()  # it floats on: its picture's shape may have changed

    def _follow_shape(self) -> None:
        if self._floating is not None and self._shape is not None:
            self._floating.set_ratio(self._shape())

    def dock(self) -> None:
        """The picture back in the fullscreen window (nothing when it is there)."""
        if not self._shown or self._floating is None:
            return
        self._shown = False
        surface = self._floating.release()
        self._floating.hide()
        if surface is not None:
            self._take_back(surface)
        logger.info("Desktop picture in picture: back in the window")
        self.floatingChanged.emit(False)

    def _float(self) -> None:
        surface = self._lend()
        if surface is None:
            return
        floating = self._floating
        if floating is None:
            floating = self._floating = FloatingPlayer()
            floating.returnRequested.connect(self._return)
            if self._remember is not None:
                floating.placed.connect(self._remember)
        floating.setGeometry(self._place())
        self._follow_shape()  # in its picture's shape, before it shows
        floating.hold(surface)
        floating.winId()  # its native window, to be told before it shows
        self._join_all_spaces(floating)
        floating.show()
        self._join_all_spaces(floating)  # again: showing may set its own
        self._shown = True
        logger.info("Desktop picture in picture: floating")
        self.floatingChanged.emit(True)

    def _return(self) -> None:
        """A click on the floating window: the fullscreen window's desktop again (macOS shows
        it as the window is activated); the next check puts the picture back."""
        self.dock()  # back at once, while macOS brings that desktop
        window = self._window
        window.raise_()
        window.activateWindow()

    def close(self) -> None:
        """QiTV closes: the picture back first, then the floating window goes."""
        self._timer.stop()
        self.dock()
        if self._floating is not None:
            self._floating.close()
            self._floating.deleteLater()
            self._floating = None
