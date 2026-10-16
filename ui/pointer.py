"""Shared click/double-click/wheel interpretation for the video surface and overlay host, and
a guard for the second click of a double-click whose first click showed another page."""

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent, QWindow
from PySide6.QtWidgets import QApplication

NAV_BUTTONS = (Qt.MouseButton.BackButton, Qt.MouseButton.ForwardButton)
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
