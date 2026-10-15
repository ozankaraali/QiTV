"""Native child widget libVLC draws into; reports mouse input to the shell."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget


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
        self._click = QTimer(self, singleShot=True)
        self._click.timeout.connect(self.clicked)
        self._wheel_accumulator = 0

    def native_handle(self) -> int:
        return int(self.winId())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._click.start(QApplication.doubleClickInterval())
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._click.stop()
        self.doubleClicked.emit()

    def mouseMoveEvent(self, event):
        self.mouseMoved.emit()
        super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        self._wheel_accumulator += event.angleDelta().y()
        steps = int(self._wheel_accumulator / 120)
        if steps:
            self._wheel_accumulator -= steps * 120
            self.wheelSteps.emit(-steps, event.modifiers())  # scroll down = next
        event.accept()
