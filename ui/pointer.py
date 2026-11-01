"""Shared click/double-click/wheel interpretation for the video surface and overlay host, a
guard for the second click of a double-click whose first click showed another page, and the
pointer that hides over the video with the player controls."""

from typing import Callable, Sequence

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QMouseEvent, QWindow
from PySide6.QtWidgets import QApplication, QWidget

from ui.macos_window import hide_pointer_until_moved, reclaim_first_responder

NAV_BUTTONS = (Qt.MouseButton.BackButton, Qt.MouseButton.ForwardButton)
POINTER_IDLE_MS = 3000  # the player controls' own wait (Transport's idle_ms)
_SECOND_PRESSES = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)


class PointerInput(QObject):
    clicked = Signal()
    doubleClicked = Signal()
    wheelSteps = Signal(int, object)  # steps, Qt.KeyboardModifier; +1 means next / down
    backPressed = Signal()  # the mouse's Back button
    forwardPressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._click = QTimer(self, singleShot=True)
        self._click.timeout.connect(self.clicked)
        self._wheel_accumulator = 0

    def press(self, button) -> None:
        if button == Qt.MouseButton.LeftButton:
            self._click.start(QApplication.doubleClickInterval())
        elif button == Qt.MouseButton.BackButton:
            self.backPressed.emit()
        elif button == Qt.MouseButton.ForwardButton:
            self.forwardPressed.emit()

    def double_click(self, button=Qt.MouseButton.LeftButton) -> None:
        if button != Qt.MouseButton.LeftButton:
            self.press(button)  # a quick second Back or Forward is one more press
            return
        self._click.stop()
        self.doubleClicked.emit()

    def wheel(self, delta_y: int, modifiers) -> None:
        self._wheel_accumulator += delta_y
        steps = int(self._wheel_accumulator / 120)
        if steps:
            self._wheel_accumulator -= steps * 120
            self.wheelSteps.emit(-steps, modifiers)  # scroll down = next


class SecondClickGuard(QObject):
    """Swallows the second click of a double-click whose first click showed another page.

    "‹ Library", ⌕ and the channel list's Movies and Series rows show the Library at once, so a
    quick second click would land on whatever the Library shows under the pointer, and a click
    on an episode plays it. `arm()` right after such a click: for the double-click interval,
    the next left press (Qt may report it as a press or as a double-click) and its release
    reach no window. Only that one click, and only on that page: `disarm()` when it changes.

    macOS: a click on the video made libVLC's view the first responder before Qt saw it, and
    the window's own filter, which gives the keys back, never sees a click swallowed here:
    the guard gives them back to that window itself.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._open = QTimer(self, singleShot=True)  # the double-click interval
        self._open.timeout.connect(self.disarm)
        self._held = False  # its press was swallowed: its release goes too
        self._installed = False

    def arm(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        if not self._installed:
            app.installEventFilter(self)
            self._installed = True
        self._open.start(QApplication.doubleClickInterval())

    def disarm(self) -> None:
        """Done: its release came, the interval passed (a release after that, of a press held
        that long, goes on: a release alone does nothing), or the page changed again."""
        self._open.stop()
        self._held = False
        if self._installed:
            self._installed = False
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # A window's events, before it hands them to a widget.
        if not isinstance(watched, QWindow) or not isinstance(event, QMouseEvent):
            return False
        if event.button() != Qt.MouseButton.LeftButton or not self._open.isActive():
            return False
        kind = event.type()
        if kind in _SECOND_PRESSES:
            self._held = True
            reclaim_first_responder(_top_level(watched))
            return True
        if kind == QEvent.Type.MouseButtonRelease and self._held:
            self.disarm()
            reclaim_first_responder(_top_level(watched))
            return True
        return False


def _top_level(window: QWindow) -> QWindow:
    """The top-level window of `window` (a video surface's own native window has a parent)."""
    while window.parent() is not None:
        window = window.parent()
    return window


class PointerHider(QObject):
    """The pointer over the video goes with the player controls, as in VLC and mpv.

    `moved()` (a mouse move over the video, the PiP or the overlay host) shows it and starts
    the wait; a real move also says so (`realMoved`: the player controls come). Once `idle_ms`
    have passed without a move, and `controls` (the transport) are gone, it hides, as long as
    `may_hide()` holds (something plays and no list or menu is open) and no popup menu or
    dialog is up. The controls hiding later (they stay while hovered) hides it then. macOS
    hides it until the mouse moves, whatever view is under it (libVLC's own takes no Qt
    cursor); elsewhere `widgets` get a blank cursor.

    2.8.12 ("sometimes the cursor forgets to vanish"):
    - A wait that ended while hiding was not allowed (the drawer, a menu, a channel still
      opening) tried no more: nothing hid the pointer once that passed, until the mouse moved.
      It waits again and tries once more, until it may hide.
    - The overlays' own moves (the player controls take theirs) never reached `moved()`; on
      macOS any move shows the pointer, so it showed while still counted hidden, and never
      hid again. Their moves count too (`widgets`' children), and on macOS a hide is asked
      for again each time, whatever it last did.
    - Jitter: a move of less than `JITTER_PX` from where the pointer last rested neither
      shows it nor starts the wait again (on macOS, where such a move shows it, it hides it
      again at once).
    - The window coming back (activated, out of or into fullscreen or the mini player)
      starts the wait again: the system may show the pointer then.
    """

    JITTER_PX = 3  # a move shorter than this (Manhattan, from the last rest) is no move
    realMoved = Signal()  # a move over the video or the host, past the jitter

    def __init__(
        self,
        widgets: Sequence[QWidget],
        controls: QWidget,
        may_hide: Callable[[], bool],
        idle_ms: int = POINTER_IDLE_MS,
        parent: QObject | None = None,
        cursor_pos: Callable[[], QPoint] = QCursor.pos,
    ) -> None:
        super().__init__(parent)
        self._widgets: tuple[QWidget, ...] = ()
        self._watched: set[int] = set()  # the children and windows filtered (by id)
        self._controls = controls
        self._may_hide = may_hide
        self._cursor_pos = cursor_pos
        self._rest: QPoint | None = None  # where the last real move left the pointer
        self._signalled: QPoint | None = None  # where the last `realMoved` was
        self._hidden = False
        self._idle = QTimer(self, singleShot=True, interval=idle_ms)
        self._idle.timeout.connect(self._try_hide)
        controls.installEventFilter(self)
        for widget in widgets:
            self.add_widget(widget)

    def hidden(self) -> bool:
        return self._hidden

    def add_widget(self, widget: QWidget) -> None:
        """Another widget the pointer hides over (a multiview tile's surface)."""
        if widget in self._widgets:
            return
        self._widgets = (*self._widgets, widget)
        self._watch(widget)  # its children (ChildAdded) and their moves
        for child in widget.findChildren(QWidget):
            self._watch(child)
        self._watch(widget.window())  # activation and state changes
        if self._hidden and not hide_pointer_until_moved():
            widget.setCursor(Qt.CursorShape.BlankCursor)

    def moved(self) -> None:
        """A move over the video or the host (their mouseMoved): `realMoved` past the jitter
        (counted apart: an overlay that lets a move through has counted it already)."""
        self._move()
        where, last = self._cursor_pos(), self._signalled
        if last is None or (where - last).manhattanLength() >= self.JITTER_PX:
            self._signalled = where
            self.realMoved.emit()

    def _move(self) -> bool:
        """A move: True for a real one (the pointer shows, the wait starts again); False for
        jitter (on macOS, where it showed the pointer, it hides it again)."""
        where = self._cursor_pos()
        rest = self._rest
        if rest is not None and (where - rest).manhattanLength() < self.JITTER_PX:
            if self._hidden:
                hide_pointer_until_moved()  # macOS showed it on the move; elsewhere a no-op
            elif not self._idle.isActive():
                self._idle.start()  # a wait that ended (controls up) is not restarted by it
            return False
        self._rest = where
        self._idle.start()
        if self._hidden:
            self._hidden = False
            for widget in self._widgets:
                widget.unsetCursor()  # macOS shows it again on the move itself
        return True

    def _watch(self, obj: QObject) -> None:
        if id(obj) not in self._watched:
            self._watched.add(id(obj))
            obj.installEventFilter(self)
            obj.destroyed.connect(lambda _=None, key=id(obj): self._watched.discard(key))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if watched is self._controls and kind == QEvent.Type.Hide:
            self._try_hide()
        elif kind == QEvent.Type.MouseMove and watched not in self._widgets:
            if isinstance(watched, QWidget) and not watched.isWindow():
                self._move()  # over an overlay (the controls, a list): it shows the pointer
        elif kind == QEvent.Type.ChildAdded and watched in self._widgets:
            child = getattr(event, "child", lambda: None)()
            if isinstance(child, QWidget):
                self._watch(child)  # an overlay added later
        elif kind in _WINDOW_CHANGES and isinstance(watched, QWidget) and watched.isWindow():
            self._idle.start()  # the system may have shown the pointer: hide it again later
        return False

    def _try_hide(self) -> None:
        if self._idle.isActive() or self._controls.isVisible():
            return  # a move came since, or the controls show: their hiding tries again
        if not self._may_hide() or _popup_open():
            self._idle.start()  # a list, menu or dialog: try again once the wait is over
            return
        self._hidden = True
        if not hide_pointer_until_moved():  # macOS: asked again, whatever it did last
            for widget in self._widgets:
                widget.setCursor(Qt.CursorShape.BlankCursor)


# The window was activated, shown, or went into or out of fullscreen or the mini player.
_WINDOW_CHANGES = (
    QEvent.Type.WindowActivate,
    QEvent.Type.WindowStateChange,
    QEvent.Type.Show,
    QEvent.Type.Resize,
)


def _popup_open() -> bool:
    """A popup menu or a modal dialog is up: the pointer is for it."""
    return (
        QApplication.activePopupWidget() is not None or QApplication.activeModalWidget() is not None
    )
