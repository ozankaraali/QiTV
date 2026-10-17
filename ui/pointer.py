"""Shared click/double-click/wheel interpretation for the video surface and overlay host, a
guard for the second click of a double-click whose first click showed another page, and the
pointer that hides over the video with the player controls."""

from typing import Callable, Sequence

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent, QWindow
from PySide6.QtWidgets import QApplication, QWidget

from ui.macos_window import hide_pointer_until_moved

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
            return True
        if kind == QEvent.Type.MouseButtonRelease and self._held:
            self.disarm()
            return True
        return False


class PointerHider(QObject):
    """The pointer over the video goes with the player controls, as in VLC and mpv.

    `moved()` (a mouse move over the video, the PiP or the overlays) shows it and starts the
    wait. Once `idle_ms` have passed without a move, and `controls` (the transport) are gone,
    it hides, as long as `may_hide()` holds: something plays and no list or menu is open.
    The controls hiding later (they stay while hovered) hides it then. macOS hides it until
    the mouse moves, whatever view is under it (libVLC's own takes no Qt cursor); elsewhere
    `widgets` get a blank cursor.
    """

    def __init__(
        self,
        widgets: Sequence[QWidget],
        controls: QWidget,
        may_hide: Callable[[], bool],
        idle_ms: int = POINTER_IDLE_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._widgets = tuple(widgets)
        self._controls = controls
        self._may_hide = may_hide
        self._hidden = False
        self._idle = QTimer(self, singleShot=True, interval=idle_ms)
        self._idle.timeout.connect(self._try_hide)
        controls.installEventFilter(self)

    def hidden(self) -> bool:
        return self._hidden

    def moved(self) -> None:
        self._idle.start()
        if self._hidden:
            self._hidden = False
            for widget in self._widgets:
                widget.unsetCursor()  # macOS shows it again on the move itself

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._controls and event.type() == QEvent.Type.Hide:
            self._try_hide()
        return False

    def _try_hide(self) -> None:
        if self._hidden or self._idle.isActive() or self._controls.isVisible():
            return
        if not self._may_hide():
            return
        self._hidden = True
        if not hide_pointer_until_moved():
            for widget in self._widgets:
                widget.setCursor(Qt.CursorShape.BlankCursor)
