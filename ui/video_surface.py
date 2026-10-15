"""Native child widget libVLC draws into; reports mouse input to the shell.

It paints nothing itself: its parent paints the black behind the video (the TV window's player
page). macOS never repaints under an opaque native view, so an opaque surface would keep
showing whatever the window drew there last (the Library page) until libVLC draws.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget

from ui.pointer import PointerInput


class VideoSurface(QWidget):
    clicked = Signal()
    doubleClicked = Signal()
    mouseMoved = Signal()
    wheelSteps = Signal(int, object)  # steps, Qt.KeyboardModifier
    backPressed = Signal()  # the mouse's Back and Forward buttons
    forwardPressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setMouseTracking(True)
        self._pointer = PointerInput(self)
        self._pointer.clicked.connect(self.clicked)
        self._pointer.doubleClicked.connect(self.doubleClicked)
        self._pointer.wheelSteps.connect(self.wheelSteps)
        self._pointer.backPressed.connect(self.backPressed)
        self._pointer.forwardPressed.connect(self.forwardPressed)

    def native_handle(self) -> int:
        return int(self.winId())

    def mousePressEvent(self, event):
        self._pointer.press(event.button())
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._pointer.double_click(event.button())

    def mouseMoveEvent(self, event):
        self.mouseMoved.emit()
        super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        self._pointer.wheel(event.angleDelta().y(), event.modifiers())
        event.accept()
