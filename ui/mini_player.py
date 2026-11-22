"""The macOS mini player (2.13.5): the whole player page in a small window on every desktop, under
the TV window's own controls (its overlay host follows the page there), while the TV window
gives way to it (hidden). On Windows and Linux the mini player is the TV window itself, small.

The owner (2.13.5): "fullscreen switch pip and regular pip are not the same, one has hud the
other does not have". Asked for (Alt+P, the controls' button, View > Mini Player), the small
window should be the window: its picture's clicks, drags (they move it), double-clicks,
right-clicks and keys are the TV window's (`MiniWindow`). Its own, as the desktop picture in
picture's (ui/small_window.py): its edges resize it in its picture's shape, and the wheel over
the picture does nothing (a scroll drifting over it from another app). It shows without taking
the focus; a press on it brings QiTV forward (the TV window's to arrange). What is dropped on it
goes to the TV window, and so does a close (the system's Quit reaches only the windows shown).

`MiniPlayer` lends the page into it (`lend`: PageStack.lend_player; `take_back`), places it
where the floating picture was left (`place`, `remember`), follows its picture's shape every
POLL_MS (`shape`), and, while QiTV is not the active app (another app clicked meanwhile), asks
the pointer's place every HOVER_MS: Qt drops the moves without a button then (qnsview_mouse.mm),
and a move over the small window is its hover (`hover`: the controls come).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from ui.small_window import LockShape, SmallWindow, _no_lock

logger = logging.getLogger(__name__)

POLL_MS = 400  # how often its picture's shape is asked
HOVER_MS = 150  # how often the pointer's place is asked while QiTV is not the active app
# What it hands the TV window as the window's own: its keys, and what is dropped on it.
_WINDOW_EVENTS = (
    QEvent.Type.ShortcutOverride,
    QEvent.Type.KeyPress,
    QEvent.Type.KeyRelease,
    QEvent.Type.DragEnter,
    QEvent.Type.DragMove,
    QEvent.Type.DragLeave,
    QEvent.Type.Drop,
)
_MOUSE_EVENTS = (
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseMove,
)


def _qitv_active() -> bool:
    return QGuiApplication.applicationState() == Qt.ApplicationState.ApplicationActive


class MiniWindow(SmallWindow):
    """The mini player's small window: it holds the player page, its edges resize it in its
    picture's shape, the wheel over the picture does nothing, and the rest of the mouse, its
    keys, drops and a close are the TV window's (`keys`)."""

    def __init__(self, *, keys: Optional[QWidget] = None, lock_shape: LockShape = _no_lock):
        super().__init__(Qt.WindowType.Widget, lock_shape)  # (it takes the focus: its keys)
        self.setAcceptDrops(True)  # (handed to the TV window)
        self._keys = keys
        self._held: Optional[QWidget] = None
        # The page and its children (their mouse), each with the tracking it had before.
        self._watched: dict[QWidget, bool] = {}

    def held(self) -> Optional[QWidget]:
        return self._held

    def hold(self, widget: QWidget) -> None:
        """`widget` (the player page) moves into this window and fills it."""
        self._held = widget
        widget.setParent(self)
        widget.setGeometry(self.rect())
        widget.show()
        self._watched = {w: w.hasMouseTracking() for w in (widget, *widget.findChildren(QWidget))}
        for watched in self._watched:
            watched.setMouseTracking(True)  # moves without a button: the edges' pointer
            watched.installEventFilter(self)

    def release(self) -> Optional[QWidget]:
        """The page held, for the TV window to take back (None: none)."""
        held, self._held = self._held, None
        for watched, tracking in self._watched.items():
            if isValid(watched):
                watched.removeEventFilter(self)
                watched.setMouseTracking(tracking)  # as it was in the window
        self._watched = {}
        self._forget_pointer()
        self._press, self._dragged = None, False
        self._reshape_due = False
        return held

    def resizeEvent(self, event) -> None:
        if self._held is not None:
            self._held.setGeometry(self.rect())
        super().resizeEvent(event)

    def closeEvent(self, event) -> None:
        """Asked to close while it shows (the system's Quit closes only the windows shown, and
        the TV window is hidden): the TV window closes instead, its own way (its shutdown, a
        recording's question), and puts the page back first."""
        keys = self._keys
        if self.isVisible() and keys is not None and isValid(keys):
            event.ignore()
            keys.close()
            return
        super().closeEvent(event)

    def event(self, event: QEvent) -> bool:
        """Its keys, and what is dropped on it, are the TV window's: they reach it as its own
        would."""
        keys = getattr(self, "_keys", None)  # (None while QWidget's own construction runs)
        if event.type() in _WINDOW_EVENTS and keys is not None and isValid(keys):
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
                self._resize_to(where)
            elif kind == QEvent.Type.MouseMove or (
                kind == QEvent.Type.MouseButtonRelease and event.button() == left
            ):
                self._end_press()  # released (a move without the button: the release was lost)
            return True
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            if event.button() != left or not any(self._edges_at(self.mapFromGlobal(where))):
                return False  # the window's (a click, a drag that moves it, its menu)
            self._begin_press(where)
            return True
        if kind == QEvent.Type.MouseMove and isinstance(watched, QWidget):
            self._point_at(watched, where)
        return False  # the hover is the window's too: its controls come


class MiniPlayer(QObject):
    """The macOS mini player's small window and the page in it (the module docstring)."""

    shownChanged = Signal(bool)

    def __init__(
        self,
        *,
        lend: Callable[[], Optional[QWidget]],
        take_back: Callable[[QWidget], None],
        place: Callable[[], QRect],
        join_all_spaces: Callable[[QWidget], object],
        remember: Optional[Callable[[QRect], None]] = None,
        shape: Optional[Callable[[], Optional[float]]] = None,
        lock_shape: Optional[LockShape] = None,
        keys: Optional[QWidget] = None,
        hover: Optional[Callable[[], None]] = None,
        app_active: Callable[[], bool] = _qitv_active,
        cursor_pos: Callable[[], QPoint] = QCursor.pos,
        parent: Optional[QObject] = None,
    ) -> None:
        """`lend()`/`take_back(page)`: the player page out of and back into the TV window (None:
        not free to go); `place()`: where the small window goes; `join_all_spaces(widget)`: on
        every desktop; `remember(rect)`: where the user moved or resized it; `shape()`: its
        picture's shape (None: not known); `lock_shape`: macOS keeps it in that shape as its
        edges are dragged; `keys`: the TV window (its keys, drops and close); `hover()`: the
        pointer moved over it while QiTV is not the active app (`app_active()`, `cursor_pos()`)."""
        super().__init__(parent)
        self._lend = lend
        self._take_back = take_back
        self._place = place
        self._join_all_spaces = join_all_spaces
        self._remember = remember
        self._shape = shape
        self._lock_shape = lock_shape
        self._keys = keys
        self._hover = hover
        self._app_active = app_active
        self._cursor_pos = cursor_pos
        self._window: Optional[MiniWindow] = None
        self._shown = False
        self._last_pos: Optional[QPoint] = None  # the pointer's, at the last poll
        self._shape_poll = QTimer(self, interval=POLL_MS)
        self._shape_poll.timeout.connect(self._follow_shape)
        self._hover_poll = QTimer(self, interval=HOVER_MS)
        self._hover_poll.timeout.connect(self._poll_hover)

    def shown(self) -> bool:
        return self._shown

    def window(self) -> Optional[MiniWindow]:
        """The small window, once it showed (None before)."""
        return self._window

    def start(self) -> bool:
        """The page into the small window, shown on every desktop. False: the page is not free
        to go (the Library has it, or the multiview's grid)."""
        if self._shown:
            return True
        page = self._lend()
        if page is None:
            return False
        window = self._window
        if window is None:
            window = self._window = MiniWindow(keys=self._keys, lock_shape=self._locks)
            if self._remember is not None:
                window.placed.connect(self._remember)
        window.put(self._place())
        window.winId()  # its native window first: the page's native views move into it
        self._follow_shape()  # in its picture's shape, before it shows
        window.hold(page)
        self._join_all_spaces(window)
        window.show()
        self._join_all_spaces(window)  # again: showing may set its own
        window.relock()  # (Qt clears the shape kept as it makes the native window)
        self._shown = True
        self._shape_poll.start()
        self.activity_changed()
        logger.info("The mini player floats")
        self.shownChanged.emit(True)
        return True

    def end(self, why: str) -> None:
        """The page back in the TV window (nothing when it is there), `why` in the log."""
        if not self._shown or self._window is None:
            return
        self._shown = False
        self._shape_poll.stop()
        self._hover_poll.stop()
        page = self._window.release()
        self._window.hide()
        if page is not None:
            self._take_back(page)
        logger.info("The mini player ends: %s", why)
        self.shownChanged.emit(False)

    def close(self) -> None:
        """QiTV closes: the page back first, then the small window goes."""
        self.end("QiTV closes")
        if self._window is not None:
            self._window.close()
            self._window.deleteLater()
            self._window = None

    def activity_changed(self) -> None:
        """QiTV became the active app, or another did: while it shows and QiTV is not active,
        the pointer's place is asked every HOVER_MS (`_poll_hover`); while QiTV is active Qt's
        own moves arrive, and nothing is asked."""
        if self._shown and self._hover is not None and not self._app_active():
            if not self._hover_poll.isActive():
                self._last_pos = self._cursor_pos()  # (resting there: no hover until it moves)
                self._hover_poll.start()
        else:
            self._hover_poll.stop()

    def _poll_hover(self) -> None:
        window = self._window
        if not self._shown or window is None or self._hover is None or self._app_active():
            self._hover_poll.stop()  # (QiTV active: started again as it goes inactive)
            return
        where = self._cursor_pos()
        moved = where != self._last_pos
        self._last_pos = where
        if moved and window.frameGeometry().contains(where):
            self._hover()

    def _follow_shape(self) -> None:
        if self._window is not None and self._shape is not None:
            self._window.set_ratio(self._shape())

    def _locks(self, widget: QWidget, shape: float, smallest, largest) -> None:
        if self._lock_shape is not None:
            self._lock_shape(widget, shape, smallest, largest)
