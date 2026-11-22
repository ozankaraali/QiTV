"""Desktop picture in picture (macOS, 2.4): leaving the fullscreen player's desktop for another
one (a swipe, Ctrl+→, another app's desktop) floats the picture in a small window on every
desktop; coming back to the fullscreen desktop puts it back. Like a browser's picture in
picture on a tab change.

One small window, however it comes (2.13.5; the owner: "fullscreen switch pip and regular pip
are not the same, one has hud the other does not have"): the mini player (Alt+P, the controls'
button; `MINI`), a desktop change while the window is fullscreen (`DESKTOP`) and a covered window
(`COVERED`) all float the player page in the same `FloatingPlayer`, and the TV window's own
controls (its overlay host, which follows the page) show over it: the same hover, clicks and
keys either way. On Windows and Linux the mini player is the window itself, small (ui/tv_window).

While the TV window is fullscreen (and the setting is on, one picture plays, the multiview's
grid is away), `DesktopPip` asks every `POLL_MS` whether the window is on the desktop shown
(`on_active_space`: on macOS [NSWindow isOnActiveSpace]), and at once when the window loses or
gets the focus (`nudge`), as it does on a desktop change. When it is no longer, the player page
is lent by the window (`lend`: PageStack.lend_player) and moved into the `FloatingPlayer`: the
page's surfaces keep their native views, and libVLC, which draws into a view of its own inside
each, goes on drawing (measured with the large surface: no picture lost, no reopen). The
fullscreen window stays where it was, showing "Playing in the small window" in the page's place.
Back on that desktop, the page returns (`take_back`) and the floating window hides; a
double-click on it (or its controls' way back) brings the fullscreen desktop back
(`come_back`), which puts the page back the same way. Leaving fullscreen or closing QiTV puts it
back first. The floating window shows on every desktop (`join_all_spaces`), above other windows.

The mini player (`arm(MINI)`): the page floats at once, and the TV window hides until it comes
back (the window's to say: Alt+P again, the controls' button, a double-click, the Library).

A covered window (macOS, a setting of its own, off by default): while the TV window is not
fullscreen, the same floating window takes the picture once the window has been out of sight
for `COVERED_SETTLE_MS`: minimized, or covered whole by other apps' windows while QiTV is not
the active app (`seen`: on macOS its NSWindow occlusionState; a sliver showing counts as seen).
QiTV's own dialogs and menus never float it: QiTV is the active app then. The picture goes back
when any of the window shows again, when QiTV becomes the active app, or on a double-click on
the floating window (which brings QiTV to the front). The floating window can't hide the window
by itself: while it lies over the whole window, what is seen says nothing (`_unseen`).

The floating window has its picture's shape, so it shows no bars: `shape()` (the large
player's `picture_ratio`) is asked as it floats and at each check while it does, and a new shape
(the stream's picture becomes known, a zap, another aspect ratio) reshapes it, as large as it
was, its corner nearest the screen's corner staying where it is. Its edges and corners resize it
in that shape; while the shape is not known (or fits any: Fill) it keeps the one it has. Qt makes
a frameless window resizable by macOS itself, at its very edges, without a press QiTV sees
(2.13.5: the owner's black bars after a resize): macOS keeps the shape as they are dragged
(`lock_shape`: its contentAspectRatio), and a resize QiTV did not make ends in the shape anyway,
`SNAP_MS` after it stops. A window as good as its picture's shape already (within 1%) is left as
it is, and the large player fills it to the last pixel (the TV window tells it the window's
size, services.picture.box_aspect).
"""

from __future__ import annotations

import logging
import math
import time
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent, QPalette
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from services.picture import box_height, box_ratio, box_size, same_shape

logger = logging.getLogger(__name__)

# Why the picture floats (DesktopPip.arm): the fullscreen window's desktop is not the one shown
# (2.4), the window (not fullscreen) is covered whole or minimized, or the mini player (2.13.5).
DESKTOP = "desktop"
COVERED = "covered"
MINI = "mini"
POLL_MS = 400  # how often the fullscreen window's desktop (or a window's sight) is asked about
WIDTH_FRACTION = 0.25  # the floating window: a quarter of the screen's width, 16:9
MARGIN = 24  # from the screen's corner
EDGE_PX = 10  # a press this near an edge (or two: a corner) resizes the floating window
MIN_WIDTH = 240  # the smallest it gets in 16:9 (another shape: as large as that)
SNAP_MS = 150  # a resize it did not make (macOS's own) ends in its picture's shape this soon
AWAY_CHECKS = 2  # answers "another desktop" in a row before the picture floats (the
# fullscreen animation may answer it once)
CONFIRM_MS = 150  # the second answer is asked this soon after the first, not a poll later
# A covered window: out of sight this long before the picture floats (a menu, a notification or
# a window dragged across never floats it). Asked again a little after (a timer may be early).
COVERED_SETTLE_MS = 500
COVERED_CONFIRM_MS = COVERED_SETTLE_MS + 60
_MOUSE_EVENTS = (
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseMove,
)
_KEY_EVENTS = (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease)


def corner_rect(available: QRect, screen_width: int) -> QRect:
    """The floating window in the bottom-right corner of the screen's `available` area."""
    width = round(screen_width * WIDTH_FRACTION)
    height = round(width * 9 / 16)
    x = available.x() + available.width() - width - MARGIN
    y = available.y() + available.height() - height - MARGIN
    return QRect(x, y, width, height)


def _no_lock(_widget: QWidget, _shape: float) -> None:
    return None


class FloatingPlayer(QWidget):
    """A frameless panel on top that holds the player page under the TV window's own controls
    (their host follows the page here): a press, a click, a drag (it moves this window), a
    double-click and a right-click on the picture are the window's, as over its own picture.
    Its own: a drag of an edge or a corner resizes it in its picture's shape (`set_ratio`; the
    pointer says so near them), the wheel over the picture does nothing (a scroll drifting over
    it would zap), and a resize it did not make (macOS's own, at its very edges) ends in that
    shape, `SNAP_MS` after it stops; macOS keeps the shape while the edges are dragged
    (`lock_shape`, told again by `relock` once it shows). Where the user resized it is told
    (`placed`).

    It shows without taking the focus, and on macOS it is a non-activating panel
    (join_all_spaces): a press on it leaves QiTV inactive and the desktop shown, so it can be
    dragged there. The press gives it the keys all the same; they go to `keys` (the TV window),
    as the window's own would."""

    placed = Signal(QRect)  # resized by the user from its edges: where it is now

    def __init__(
        self,
        *,
        keys: Optional[QWidget] = None,
        lock_shape: Callable[[QWidget, float], object] = _no_lock,
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("QiTV")
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("black"))
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self._keys = keys
        self._lock_shape = lock_shape
        self._held: Optional[QWidget] = None
        self._watched: list[QWidget] = []  # the held widget and its children (their mouse)
        self._cursor_on: Optional[QWidget] = None  # where it set a resize pointer
        self._press: Optional[QPoint] = None  # where a press on an edge began (global)
        self._dragged = False
        self._edges: tuple[bool, bool, bool, bool] = (False, False, False, False)  # l, t, r, b
        self._start = QRect()  # the window's geometry when a resize began
        self._ratio: Optional[float] = None  # its picture's shape (None: it keeps its own)
        self._reshape_due = False  # a new shape came during a press: once it ends
        self._locked: Optional[float] = None  # the shape macOS was told to keep
        self._snap = QTimer(self, singleShot=True, interval=SNAP_MS)
        self._snap.timeout.connect(self._settle)

    def held(self) -> Optional[QWidget]:
        return self._held

    def hold(self, widget: QWidget) -> None:
        """`widget` (the player page) moves into this window and fills it; the edges of its
        pictures resize this window, and the wheel over them does nothing."""
        self._held = widget
        widget.setParent(self)
        widget.setGeometry(self.rect())
        widget.show()
        self._watched = [widget, *widget.findChildren(QWidget)]
        for watched in self._watched:
            watched.setMouseTracking(True)  # moves without a button: the edges' pointer
            watched.installEventFilter(self)

    def release(self) -> Optional[QWidget]:
        """The widget held, for its window to take back (None: none)."""
        held, self._held = self._held, None
        for watched in self._watched:
            if isValid(watched):
                watched.removeEventFilter(self)
        self._watched = []
        if self._cursor_on is not None and isValid(self._cursor_on):
            self._cursor_on.unsetCursor()
        self._cursor_on = None
        self._press, self._dragged = None, False
        self._reshape_due = False
        return held

    def set_ratio(self, ratio: Optional[float]) -> None:
        """Its picture shows in `ratio`'s shape (width / height; None: not known, or it fits
        any): the window takes it, as large as it was, the corner nearest its screen's corner
        staying where it is, inside the screen. During a press, once the press ends. macOS
        keeps the shape it has then as its edges are dragged."""
        self._ratio = ratio
        if self._press is not None:
            self._reshape_due = True
        else:
            self._reshape()
        self._lock()

    def relock(self) -> None:
        """Tell macOS the shape to keep again (Qt clears it as it makes the native window)."""
        self._lock(again=True)

    def _lock(self, again: bool = False) -> None:
        shape = self._shape()
        if again or shape != self._locked:
            self._locked = shape
            self._lock_shape(self, shape)

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
        self.setGeometry(QRect(x, y, width, height))

    def _settle(self) -> None:
        """A resize it did not make stopped (macOS's own, at its edges): in its shape now."""
        if self._press is None:
            self._reshape()

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

    def _point_at(self, watched: QWidget, shape: Optional[Qt.CursorShape]) -> None:
        """The pointer over `watched`: a resize one near an edge, else its own again."""
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
        if self._held is not None:
            self._held.setGeometry(self.rect())
        if self._press is None:
            self._snap.start()  # (one it did not make: macOS's own, at its edges)
        super().resizeEvent(event)

    def closeEvent(self, event) -> None:
        """Asked to close while it shows (the system's Quit closes only the windows shown, and
        the mini player hides the TV window): the TV window closes instead, its own way (its
        shutdown, a recording's question), and puts the picture back first."""
        keys = self._keys
        if self.isVisible() and keys is not None and isValid(keys):
            event.ignore()
            keys.close()
            return
        super().closeEvent(event)

    def event(self, event: QEvent) -> bool:
        """Its keys are the TV window's (`keys`): they reach it as its own would."""
        keys = getattr(self, "_keys", None)  # (None while QWidget's own construction runs)
        if event.type() in _KEY_EVENTS and keys is not None and isValid(keys):
            QApplication.sendEvent(keys, event)
            return event.isAccepted()
        return super().event(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._held is None or watched not in self._watched:
            return False
        kind = event.type()
        if kind == QEvent.Type.Wheel:
            return True  # never the window's: a scroll drifting over it would zap
        if kind not in _MOUSE_EVENTS or not isinstance(event, QMouseEvent):
            return False
        left = Qt.MouseButton.LeftButton
        where = event.globalPosition().toPoint()
        if self._press is not None:  # an edge or a corner is dragged: all of it is this one's
            if kind == QEvent.Type.MouseMove and event.buttons() & left:
                moved = where - self._press
                if self._dragged or moved.manhattanLength() >= QApplication.startDragDistance():
                    self._dragged = True
                    self.setGeometry(self._resized(moved))
            elif kind == QEvent.Type.MouseMove or (
                kind == QEvent.Type.MouseButtonRelease and event.button() == left
            ):
                self._end_press()  # released (a move without the button: the release was lost)
            return True
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            edges = self._edges_at(self.mapFromGlobal(where))
            if event.button() != left or not any(edges):
                return False  # the window's (a click, a drag that moves it, its menu)
            self._press, self._dragged, self._edges = where, False, edges
            self._start = self.geometry()
            return True
        if kind == QEvent.Type.MouseMove and isinstance(watched, QWidget):
            self._point_at(watched, self._cursor_for(self._edges_at(self.mapFromGlobal(where))))
        return False  # the hover is the window's too: its controls come

    def _end_press(self) -> None:
        """The left button came up after a press on an edge: a resize is told (a click there
        does nothing). A shape that came meanwhile applies."""
        dragged = self._press is not None and self._dragged
        self._press, self._dragged = None, False
        if dragged:
            self.placed.emit(self.geometry())
        if self._reshape_due:
            self._reshape()
        self._lock()


def _qitv_active() -> bool:
    return QGuiApplication.applicationState() == Qt.ApplicationState.ApplicationActive


class DesktopPip(QObject):
    """Floats the player page while the fullscreen TV window's desktop is not shown, (the
    covered reason) while the TV window is out of sight, or as the mini player."""

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
        seen: Optional[Callable[[QWidget], Optional[bool]]] = None,
        may_float_covered: Optional[Callable[[], bool]] = None,
        app_active: Callable[[], bool] = _qitv_active,
        activate: Optional[Callable[[], object]] = None,
        clock: Callable[[], float] = time.monotonic,
        shape: Optional[Callable[[], Optional[float]]] = None,
        lock_shape: Optional[Callable[[QWidget, float], object]] = None,
        keys: Optional[QWidget] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """`on_active_space(window)`: whether the window is on the desktop shown (None: can't
        tell); `may_float()`: whether a picture may float now; `lend()`/`take_back(page)`: the
        player page out of and back into the window; `place()`: where the floating window goes
        (screen coordinates); `join_all_spaces(widget)`: show it on every desktop;
        `remember(rect)`: where the user moved or resized it (`place` gives it back next time);
        `shape()`: the shape the large picture shows in (None: not known; it keeps its own);
        `lock_shape(widget, shape)`: the floating window's edges resize it in `shape` (macOS);
        `keys`: the widget its keys go to (the TV window).

        The covered reason (without `seen` and `may_float_covered` it is never asked about):
        `seen(window)`: whether any of the window can be seen (None: can't tell);
        `may_float_covered()`: whether a picture may float for it now; `app_active()`: whether
        QiTV is the active app; `activate()`: QiTV to the front as an app (a double-click on
        the floating window); `clock()`: seconds, for how long the window was out of sight."""
        super().__init__(parent)
        self._window = window
        self._on_active_space = on_active_space
        self._may_float = may_float
        self._lend = lend
        self._take_back = take_back
        self._place = place
        self._join_all_spaces = join_all_spaces
        self._remember = remember
        self._seen = seen
        self._may_float_covered = may_float_covered
        self._app_active = app_active
        self._activate = activate
        self._clock = clock
        self._shape = shape
        self._lock_shape = lock_shape
        self._keys = keys
        self._floating: Optional[FloatingPlayer] = None
        self._shown = False
        self._reason = DESKTOP  # what `check` asks about (the timer runs while it is armed)
        self._away = 0  # "another desktop" answers in a row
        self._since: Optional[float] = None  # covered: out of sight since (`clock`)
        self._held = False  # covered: after a double-click, nothing floats before it was seen
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

    def reason(self) -> Optional[str]:
        """What is asked about now (DESKTOP, COVERED, MINI); None while nothing is."""
        return self._reason if self._timer.isActive() else None

    def watch(self, on: bool) -> None:
        """Ask about the desktop (the window is fullscreen) or not; not puts the picture back."""
        self.arm(DESKTOP if on else None)

    def arm(self, reason: Optional[str]) -> None:
        """Ask about `reason` from now on: DESKTOP (the window is fullscreen: is its desktop
        shown?), COVERED (a window: can it be seen?), MINI (the mini player: it floats at once,
        until another reason or none is armed, or it is docked) or nothing (None). The reason
        asked about already goes on as it was; another one, or nothing, puts the picture back
        first."""
        if reason == COVERED and (self._seen is None or self._may_float_covered is None):
            reason = None  # (nothing to ask with)
        if reason is not None and reason == self._reason and self._timer.isActive():
            return
        self._timer.stop()
        self._away, self._since, self._held = 0, None, False
        self.dock()
        self._reason = reason or DESKTOP
        if reason is not None:
            self._timer.start()
        if reason == MINI:
            self._float()

    def check(self) -> None:
        """The window's desktop now: float the picture when it is away, put it back when not
        (covered: whether the window can be seen, `_check_covered`; the mini player: its
        picture's shape)."""
        if self._reason == MINI:
            if self._shown:
                self._follow_shape()
            return
        if self._reason == COVERED:
            self._check_covered()
            return
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

    def _check_covered(self) -> None:
        """The window out of sight for COVERED_SETTLE_MS floats the picture; any of it seen
        puts it back at once."""
        may_float = self._may_float_covered
        if may_float is None or not may_float():
            self._since = None
            self.dock()  # nothing to float any more (it stopped, the grid, Keep on Top came)
            return
        unseen = self._unseen()
        if unseen is None:
            return
        if not unseen:
            self._since, self._held = None, False
            self.dock()
            return
        if self._shown:
            self._follow_shape()  # it floats on: its picture's shape may have changed
            return
        if self._held:
            return
        now = self._clock()
        if self._since is None:
            self._since = now
            if self._timer.isActive():
                QTimer.singleShot(COVERED_CONFIRM_MS, self, self.nudge)  # (not a poll later)
        elif (now - self._since) * 1000 >= COVERED_SETTLE_MS:
            self._float()

    def _unseen(self) -> Optional[bool]:
        """Covered: whether the window is out of sight now (None: can't tell). Minimized, it
        is; with QiTV the active app it is not (its own dialog or menu over the window, or the
        user back in QiTV); else none of it can be seen. The floating window is QiTV's own:
        while it lies over the whole window, what is seen says nothing (it would hide the
        window it floats for, and never let the picture go back)."""
        window = self._window
        if window.isMinimized():
            return True
        if self._app_active():
            return False
        floating = self._floating
        if (
            self._shown
            and floating is not None
            and floating.frameGeometry().contains(window.frameGeometry())
        ):
            return None
        seen = self._seen(window) if self._seen is not None else None
        return None if seen is None else not seen

    def dock(self) -> None:
        """The picture back in the TV window (nothing when it is there). The mini player's
        window is the TV window's to show again (`floatingChanged`)."""
        if not self._shown or self._floating is None:
            return
        self._shown = False
        page = self._floating.release()
        self._floating.hide()
        if page is not None:
            self._take_back(page)
        logger.info("Desktop picture in picture: back in the window")
        self.floatingChanged.emit(False)

    def _float(self) -> None:
        page = self._lend()
        if page is None:
            return
        floating = self._floating
        if floating is None:
            floating = self._floating = FloatingPlayer(keys=self._keys, lock_shape=self._locks)
            if self._remember is not None:
                floating.placed.connect(self._remember)
        floating.setGeometry(self._place())
        floating.winId()  # its native window first: the page's native views move into it
        self._follow_shape()  # in its picture's shape, before it shows
        floating.hold(page)
        self._join_all_spaces(floating)
        floating.show()
        self._join_all_spaces(floating)  # again: showing may set its own
        floating.relock()  # (Qt clears the shape kept as it makes the native window)
        self._shown = True
        if self._reason == COVERED:
            logger.info("Desktop picture in picture: floating (the window is out of sight)")
        elif self._reason == MINI:
            logger.info("Desktop picture in picture: floating (the mini player)")
        else:
            logger.info("Desktop picture in picture: floating")
        self.floatingChanged.emit(True)

    def _locks(self, widget: QWidget, shape: float) -> None:
        if self._lock_shape is not None:
            self._lock_shape(widget, shape)

    def come_back(self) -> None:
        """A double-click on the floating window, or its controls' way back (DESKTOP, COVERED):
        the fullscreen window's desktop again (macOS shows it as the window is activated); the
        next check puts the picture back. Covered: the window back from the Dock and QiTV in
        front of the app that covered it; nothing floats again before the window was seen
        (should macOS not bring QiTV, the picture stays). The mini player's way back is the
        TV window's (it shows itself again)."""
        self.dock()  # back at once, while macOS brings that desktop
        window = self._window
        if self._reason == COVERED and self._timer.isActive():
            self._held, self._since = True, None
            if window.isMinimized():
                window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
                window.show()
            if self._activate is not None:
                self._activate()
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
