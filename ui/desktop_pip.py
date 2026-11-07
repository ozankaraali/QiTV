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

A covered window (macOS, a setting of its own, off by default): while the TV window is not
fullscreen, the same floating window takes the picture once the window has been out of sight
for `COVERED_SETTLE_MS`: minimized, or covered whole by other apps' windows while QiTV is not
the active app (`seen`: on macOS its NSWindow occlusionState; a sliver showing counts as seen).
QiTV's own dialogs and menus never float it: QiTV is the active app then. The picture goes back
when any of the window shows again, when QiTV becomes the active app, or on a double-click on
the floating window (which brings QiTV to the front). The floating window can't hide the window
by itself: while it lies over the whole window, what is seen says nothing (`_unseen`).
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent, QPalette
from PySide6.QtWidgets import QApplication, QWidget

logger = logging.getLogger(__name__)

# Why the picture floats (DesktopPip.arm): the fullscreen window's desktop is not the one shown
# (2.4), or the window (not fullscreen) is covered whole or minimized.
DESKTOP = "desktop"
COVERED = "covered"
POLL_MS = 400  # how often the fullscreen window's desktop (or a window's sight) is asked about
WIDTH_FRACTION = 0.25  # the floating window: a quarter of the screen's width, 16:9
MARGIN = 24  # from the screen's corner
EDGE_PX = 10  # a press this near an edge (or two: a corner) resizes the floating window
MIN_WIDTH = 240  # the smallest it gets
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
    resizes it in 16:9 (the pointer says so near them); where it ended up is told
    (`placed`). A panel that takes no focus
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
        if surface is not None:
            surface.unsetCursor()
        return surface

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
        """The geometry a resize by `moved` (global) gives: 16:9, the edges opposite those
        dragged staying where they were, between MIN_WIDTH and the screen's width."""
        left, top, right, bottom = self._edges
        start = self._start
        widths = []
        if left or right:
            widths.append(start.width() + (moved.x() if right else -moved.x()))
        if top or bottom:
            height = start.height() + (moved.y() if bottom else -moved.y())
            widths.append(round(height * 16 / 9))
        width = max(widths, key=lambda w: abs(w - start.width()))
        screen = self.screen()
        widest = screen.availableGeometry().width() if screen is not None else 4096
        width = max(MIN_WIDTH, min(int(width), widest))
        height = round(width * 9 / 16)
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
            # A click alone does nothing: a press to grab it must never send the user back.
            dragged = self._press is not None and self._dragged
            self._press, self._dragged = None, False
            if dragged:
                self.placed.emit(self.geometry())
        return True  # the shell's clicks on the picture are for the fullscreen window


def _qitv_active() -> bool:
    return QGuiApplication.applicationState() == Qt.ApplicationState.ApplicationActive


class DesktopPip(QObject):
    """Floats the large picture while the fullscreen TV window's desktop is not shown, or (the
    covered reason) while the TV window is out of sight."""

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
        parent: Optional[QObject] = None,
    ) -> None:
        """`on_active_space(window)`: whether the window is on the desktop shown (None: can't
        tell); `may_float()`: whether a picture may float now; `lend()`/`take_back(surface)`:
        the large surface out of and back into the player; `place()`: where the floating
        window goes (screen coordinates); `join_all_spaces(widget)`: show it on every desktop;
        `remember(rect)`: where the user moved or resized it (`place` gives it back next time).

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
        """What is asked about now (DESKTOP, COVERED); None while nothing is."""
        return self._reason if self._timer.isActive() else None

    def watch(self, on: bool) -> None:
        """Ask about the desktop (the window is fullscreen) or not; not puts the picture back."""
        self.arm(DESKTOP if on else None)

    def arm(self, reason: Optional[str]) -> None:
        """Ask about `reason` from now on: DESKTOP (the window is fullscreen: is its desktop
        shown?), COVERED (a window: can it be seen?) or nothing (None). The reason asked about
        already goes on as it was; another one, or nothing, puts the picture back first."""
        if reason == COVERED and (self._seen is None or self._may_float_covered is None):
            reason = None  # (nothing to ask with)
        if reason is not None and reason == self._reason:
            if not self._timer.isActive():
                self._timer.start()
            return
        self._timer.stop()
        self._away, self._since, self._held = 0, None, False
        self.dock()
        self._reason = reason or DESKTOP
        if reason is not None:
            self._timer.start()

    def check(self) -> None:
        """The window's desktop now: float the picture when it is away, put it back when not
        (covered: whether the window can be seen, `_check_covered`)."""
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
        if self._shown or self._held:
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
        floating.hold(surface)
        floating.winId()  # its native window, to be told before it shows
        self._join_all_spaces(floating)
        floating.show()
        self._join_all_spaces(floating)  # again: showing may set its own
        self._shown = True
        if self._reason == COVERED:
            logger.info("Desktop picture in picture: floating (the window is out of sight)")
        else:
            logger.info("Desktop picture in picture: floating")
        self.floatingChanged.emit(True)

    def _return(self) -> None:
        """A click on the floating window: the fullscreen window's desktop again (macOS shows
        it as the window is activated); the next check puts the picture back. Covered: the
        window back from the Dock and QiTV in front of the app that covered it; nothing floats
        again before the window was seen (should macOS not bring QiTV, the picture stays)."""
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
