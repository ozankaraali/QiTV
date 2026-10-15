"""Native child widget libVLC draws into; reports mouse input to the shell."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget

from ui.pointer import PointerInput


class VideoSurface(QWidget):
    clicked = Signal()
    doubleClicked = Signal()
    mouseMoved = Signal()
    wheelSteps = Signal(int, object)  # steps, Qt.KeyboardModifier

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMouseTracking(True)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#000000"))
        self.setPalette(palette)
        self._pointer = PointerInput(self)
        self._pointer.clicked.connect(self.clicked)
        self._pointer.doubleClicked.connect(self.doubleClicked)
        self._pointer.wheelSteps.connect(self.wheelSteps)

    def native_handle(self) -> int:
        return int(self.winId())

    def mousePressEvent(self, event):
        self._pointer.press(event.button())
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._pointer.double_click()

    def mouseMoveEvent(self, event):
        self.mouseMoved.emit()
        super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        self._pointer.wheel(event.angleDelta().y(), event.modifiers())
        event.accept()
