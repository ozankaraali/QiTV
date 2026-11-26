"""The small window a picture floats in on macOS (2.4; 2.13.5): frameless, above other windows,
in its picture's shape. Two kinds share it: the desktop picture in picture's (ui/desktop_pip.py:
the large surface, when the fullscreen window's desktop is left or the window is covered) and
the mini player's (ui/mini_player.py: the whole player page, under the window's controls).

It has its picture's shape, so it shows no bars (`set_ratio`: as large as it was, its corner
nearest the screen's corner staying where it is). Its edges and corners resize it in that shape
(a press within their band, `band`; the pointer says so near them), whatever is under the press
there: the window of its controls over it too (`guard`: the owner found the edges hard to
catch while they showed, as they took the presses and the moves); while the shape is not known
(or fits any: Fill) it keeps the one it has. Qt makes a frameless window resizable by macOS
itself too, at its very edges, without a press QiTV sees (2.13.5: the owner's black bars after
a resize): macOS keeps the shape as they are dragged, between the smallest window and the
screen (`lock_shape`: its contentAspectRatio, contentMinSize and contentMaxSize, told again by
`relock` once it shows), and a resize QiTV did not make ends in the shape anyway, SNAP_MS after it
stops. A window as good as its picture's shape already (within 1%) is left as it is, and its
player fills it to the last pixel (the TV window tells it the box, services.picture.box_aspect).
Where the user resized it is told (`placed`).

Both show the player controls over them as the pointer moves over them, and while QiTV is not
the active app (another app clicked, a desktop change) Qt says nothing of the pointer (qtbase's
qnsview_mouse.mm drops the moves without a button then): `HoverPoll` asks its place instead,
every HOVER_MS, and tells a move over the small window as its hover (2.13.5).
"""

from __future__ import annotations

import math
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QMouseEvent, QPalette, QWindow
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from services.picture import box_height, box_ratio, box_size, same_shape
from ui.macos_window import reclaim_first_responder

WIDTH_FRACTION = 0.25  # a quarter of the screen's width, 16:9
MARGIN = 24  # from the screen's corner
# Its edges' grab band (`band`): a press this near an edge resizes it, and one along an edge
# this near the next edge resizes it by their corner. A thirtieth of its width, EDGE_PX at most
# (from 420 px wide): its controls are drawn in units of a hundredth of the width
# (ui.overlays.painting), the nearest about 3 units in (the star's middle 3.75 from the top), so
# each keeps its middle out of the band at every size and shape. A corner runs along its edges,
# not into the picture as a square: ▶ and ⛶ sit there (a 24 px square covers ⛶ whole 360 px
# wide). Neither is more than a fifth of its smaller side: a small window is dragged by its middle.
EDGE_PX = 14
EDGE_SHARE = 1 / 30
CORNER_PX = 24
BAND_SHARE = 1 / 5
MIN_WIDTH = 240  # the smallest it gets in 16:9 (another shape: as large as that)
SNAP_MS = 150  # a resize it did not make (macOS's own) ends in its picture's shape this soon
HOVER_MS = 150  # how often the pointer's place is asked while QiTV is not the active app

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


def _no_keep_up(_on: bool) -> None:
    return None


def _qitv_active() -> bool:
    return QGuiApplication.applicationState() == Qt.ApplicationState.ApplicationActive


class _Guard(QObject):
    """A window over a small window, its controls' (`SmallWindow.guard`): each mouse event on
    its QWindow is the small window's to take first, before any widget of it sees one."""

    def __init__(self, small: SmallWindow, host: QWidget, keep_up: Callable[[bool], object]):
        super().__init__(small)
        self._small, self._host, self.keep_up = small, host, keep_up
        self._handle: Optional[QWindow] = None
        self._pointing = False  # the resize pointer shows on its window
        host.installEventFilter(self)  # (its QWindow made anew, it hides)
        self._watch()

    def _watch(self) -> None:
        if self._host.windowHandle() is None:
            self._host.winId()  # (its QWindow, where the mouse comes)
        handle = self._host.windowHandle()
        if handle is not self._handle:
            if self._handle is not None and isValid(self._handle):
                self._handle.removeEventFilter(self)
            self._handle = handle
            if handle is not None:
                handle.installEventFilter(self)

    def point(self, shape: Optional[Qt.CursorShape], local: QPoint) -> None:
        """The resize pointer `shape` on its window; None: the widget's under `local` again."""
        handle = self._handle
        if handle is None or not isValid(handle):
            return
        if shape is not None:
            handle.setCursor(shape)
            self._pointing = True
        elif self._pointing:
            self._pointing = False
            handle.setCursor((self._host.childAt(local) or self._host).cursor())

    def unpoint(self) -> None:
        if self._pointing and self._handle is not None and isValid(self._handle):
            self._handle.unsetCursor()
        self._pointing = False

    def drop(self) -> None:
        self.unpoint()
        if isValid(self._host):
            self._host.removeEventFilter(self)
        if self._handle is not None and isValid(self._handle):
            self._handle.removeEventFilter(self)
        self._handle = None
        self.deleteLater()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._host:
            if event.type() == QEvent.Type.WinIdChange:
                self._watch()
            elif event.type() == QEvent.Type.Hide:
                self.unpoint()
            return False
        if watched is self._handle and isinstance(event, QMouseEvent):
            return self._small._host_mouse(self, event)
        return False


class HoverPoll(QObject):
    """The pointer over a small window while QiTV is not the active app (the module docstring):
    while `window()` shows (None: none does) and QiTV is inactive, the pointer's place
    (`cursor_pos()`, global) is asked every HOVER_MS; a move over the window is told
    (`moved(where)`), and the first one off it (`moved(None)`). It only reads where the
    pointer is: nothing is focused, activated or raised. Asked to `update` as the window shows
    and as QiTV becomes the active app or another does; with QiTV active Qt's own moves
    arrive, and nothing is asked."""

    def __init__(
        self,
        window: Callable[[], Optional[QWidget]],
        moved: Callable[[Optional[QPoint]], None],
        *,
        app_active: Callable[[], bool] = _qitv_active,
        cursor_pos: Callable[[], QPoint] = QCursor.pos,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._window = window
        self._moved = moved
        self._app_active = app_active
        self._cursor_pos = cursor_pos
        self._last: Optional[QPoint] = None  # the pointer's place at the last ask
        self._over = False  # it was over the window then
        self._timer = QTimer(self, interval=HOVER_MS)
        self._timer.timeout.connect(self.poll)

    def active(self) -> bool:
        return self._timer.isActive()

    def update(self) -> None:
        """Ask from now on while the window shows and QiTV is inactive; else not."""
        window = self._window()
        if window is not None and window.isVisible() and not self._app_active():
            if not self._timer.isActive():
                self._last = self._cursor_pos()  # (resting there: no hover until it moves)
                self._over = False
                self._timer.start()
        else:
            self._timer.stop()

    def stop(self) -> None:
        self._timer.stop()

    def poll(self) -> None:
        window = self._window()
        if window is None or not window.isVisible() or self._app_active():
            self._timer.stop()  # (QiTV active: asked again as it goes inactive)
            return
        where = self._cursor_pos()
        if where == self._last:
            return
        self._last = where
        if window.frameGeometry().contains(where):
            self._over = True
            self._moved(where)
        elif self._over:
            self._over = False
            self._moved(None)


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
        self._guard: Optional[_Guard] = None  # the window of its controls over it
        self._pressed_on: Optional[_Guard] = None  # the press began there (on its band)
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

    def band(self) -> tuple[int, int]:
        """Its edges' grab band now: how near an edge a press resizes it, and how far along an
        edge its corner reaches (EDGE_PX and the rest above)."""
        width, height = self.width(), self.height()
        most = math.floor(min(width, height) * BAND_SHARE)
        return min(EDGE_PX, math.floor(width * EDGE_SHARE), most), min(CORNER_PX, most)

    def _edges_at(self, point: QPoint) -> tuple[bool, bool, bool, bool]:
        """The edges (left, top, right, bottom) `point` (window coordinates) is near: within the
        band of one, and along it near the next one, both (their corner)."""
        if not self.rect().contains(point):
            return (False, False, False, False)
        edge, corner = self.band()
        x, y, width, height = point.x(), point.y(), self.width(), self.height()
        left, top, right, bottom = x < edge, y < edge, x >= width - edge, y >= height - edge
        if left or right:
            top, bottom = top or y < corner, bottom or y >= height - corner
        if top or bottom:
            left, right = left or x < corner, right or x >= width - corner
        return left, top, right, bottom

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
        if self._guard is not None:
            self._guard.unpoint()

    # ---- the window over it (its controls') ---------------------------------------------------

    def guard(
        self, host: Optional[QWidget], keep_up: Callable[[bool], object] = _no_keep_up
    ) -> None:
        """`host`, a window over this one (its controls' overlay host), leaves it the mouse
        within its edges' band, whatever lies under it there (`_host_mouse`, on the host's
        window before any widget of it): a left press resizes it, a move shows the resize
        pointer. `keep_up(True)` keeps the controls up through a resize begun there,
        `keep_up(False)` once it ends. None: no window over it any more."""
        self._release_controls()
        if self._guard is not None:
            self._guard.drop()
        self._guard = _Guard(self, host, keep_up) if host is not None else None

    def _host_mouse(self, guard: _Guard, event: QMouseEvent) -> bool:
        """A mouse event on `guard`'s window, over this one: the edges' (True) when it is a left
        press within the band, a move or the release of a press begun there, or a move without
        a button within the band (the resize pointer, on that window); else that window's."""
        if not self.isVisible():
            return False
        kind, left = event.type(), Qt.MouseButton.LeftButton
        where = event.globalPosition().toPoint()
        press = kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
        if self._press is not None:  # a press goes on: one begun there is all the edges'
            if self._pressed_on is not guard:
                return False  # (its picture's)
            if press and event.button() == left:
                self._edge_press(event)  # (a double-click's second press)
                self._hold_controls(guard)
            elif kind == QEvent.Type.MouseMove and event.buttons() & left:
                self._resize_to(where)
            elif kind == QEvent.Type.MouseMove or (
                kind == QEvent.Type.MouseButtonRelease and event.button() == left
            ):
                self._end_press()  # released (a move without the button: the release was lost)
            return True
        edges = self._edges_at(self.mapFromGlobal(where))
        if press and event.button() == left and any(edges):
            self._edge_press(event)
            self._hold_controls(guard)
            return True
        if kind == QEvent.Type.MouseMove and not event.buttons():
            guard.point(self._cursor_for(edges), event.position().toPoint())
            return any(edges)
        return False  # (a control's press, its drag, a right-click)

    def _edge_press(self, event: QMouseEvent) -> None:
        """A left press (or a double-click) within the band on the window over it: as one on
        its picture there (a resize begins)."""
        self._begin_press(event.globalPosition().toPoint())

    def _hold_controls(self, guard: _Guard) -> None:
        if self._press is not None:
            self._pressed_on = guard
            guard.keep_up(True)

    def _release_controls(self) -> None:
        guard, self._pressed_on = self._pressed_on, None
        if guard is not None:
            guard.keep_up(False)

    # ---- a press ------------------------------------------------------------------------------

    def _drop_press(self) -> None:
        """No press any more (released, or the picture went back): its controls may fade."""
        self._press, self._dragged = None, False
        self._release_controls()

    def _begin_press(self, where: QPoint) -> bool:
        """A left press at `where` (global): True when it is on an edge (a resize begins)."""
        self._release_controls()
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
        self._drop_press()
        if dragged:
            self._expected = self.geometry()
            self.placed.emit(self.geometry())
        if self._reshape_due:
            self._reshape()
        self._lock()
