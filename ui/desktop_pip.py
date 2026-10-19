"""Desktop picture in picture (macOS, 2.4): leaving the fullscreen player's desktop for another
one (a swipe, Ctrl+→, another app's desktop) floats the picture in a small window on every
desktop; coming back to the fullscreen desktop puts it back. Like a browser's picture in
picture on a tab change.

While the TV window is fullscreen (and the setting is on, one picture plays, the multiview's
grid is away), `DesktopPip` asks every `POLL_MS` whether the window is on the desktop shown
(`on_active_space`: on macOS [NSWindow isOnActiveSpace]). When it is no longer, the large
picture's surface is lent by the player (`lend`, PipFrame.lend_large) and moved into the
`FloatingPlayer`: the surface keeps its native view, and libVLC, which draws into a view of its
own inside it, goes on drawing (measured: no picture lost, no reopen). The fullscreen window
stays where it was, showing what the player page shows without its picture. Back on that
desktop, the surface returns (`take_back`) and the floating window hides; a click on it brings
the fullscreen desktop back, which puts the picture back the same way. Leaving fullscreen or
closing QiTV puts it back first. The floating window can be dragged; it shows on every desktop
(`join_all_spaces`), above other windows, and never takes the keyboard.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPalette
from PySide6.QtWidgets import QApplication, QWidget

logger = logging.getLogger(__name__)

POLL_MS = 400  # how often the fullscreen window's desktop is asked about
WIDTH_FRACTION = 0.25  # the floating window: a quarter of the screen's width, 16:9
MARGIN = 24  # from the screen's corner
AWAY_CHECKS = 2  # answers "another desktop" in a row before the picture floats (the
# fullscreen animation may answer it once)
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
    fullscreen desktop again (`returnRequested`), a drag moves it. A panel that takes no focus
    (on macOS a non-activating one): a press on it leaves QiTV inactive and the desktop shown,
    so it can be dragged there."""

    returnRequested = Signal()

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

    def surface(self) -> Optional[QWidget]:
        return self._surface

    def hold(self, surface: QWidget) -> None:
        """`surface` moves into this window and fills it; its mouse is this window's now."""
        self._surface = surface
        surface.setParent(self)
        surface.setGeometry(self.rect())
        surface.show()
        surface.installEventFilter(self)

    def release(self) -> Optional[QWidget]:
        """The surface held, for its window to take back (None: none)."""
        surface, self._surface = self._surface, None
        if surface is not None:
            surface.removeEventFilter(self)
        self._press, self._dragged = None, False
        return surface

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
        if kind == QEvent.Type.MouseButtonPress and event.button() == left:
            self._press, self._origin, self._dragged = where, self.pos(), False
        elif kind == QEvent.Type.MouseMove and self._press is not None:
            moved = where - self._press
            if self._dragged or moved.manhattanLength() >= QApplication.startDragDistance():
                self._dragged = True
                self.move(self._origin + moved)
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == left:
            clicked = self._press is not None and not self._dragged
            self._press, self._dragged = None, False
            if clicked:
                self.returnRequested.emit()
        return True  # the shell's clicks on the picture are for the fullscreen window


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
        parent: Optional[QObject] = None,
    ) -> None:
        """`on_active_space(window)`: whether the window is on the desktop shown (None: can't
        tell); `may_float()`: whether a picture may float now; `lend()`/`take_back(surface)`:
        the large surface out of and back into the player; `place()`: where the floating
        window goes (screen coordinates); `join_all_spaces(widget)`: show it on every desktop."""
        super().__init__(parent)
        self._window = window
        self._on_active_space = on_active_space
        self._may_float = may_float
        self._lend = lend
        self._take_back = take_back
        self._place = place
        self._join_all_spaces = join_all_spaces
        self._floating: Optional[FloatingPlayer] = None
        self._shown = False
        self._away = 0  # "another desktop" answers in a row
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self.check)

    def floating(self) -> bool:
        return self._shown

    def watch(self, on: bool) -> None:
        """Ask about the desktop (the window is fullscreen) or not; not puts the picture back."""
        if on:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
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
        floating.setGeometry(self._place())
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
