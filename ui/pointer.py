"""Shared click/double-click/wheel interpretation for the video surface and overlay host."""

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication


class PointerInput(QObject):
    clicked = Signal()
    doubleClicked = Signal()
    wheelSteps = Signal(int, object)  # steps, Qt.KeyboardModifier; +1 means next / down

    def __init__(self, parent=None):
        super().__init__(parent)
        self._click = QTimer(self, singleShot=True)
        self._click.timeout.connect(self.clicked)
        self._wheel_accumulator = 0

    def press(self, button) -> None:
        if button == Qt.MouseButton.LeftButton:
            self._click.start(QApplication.doubleClickInterval())

    def double_click(self) -> None:
        self._click.stop()
        self.doubleClicked.emit()

    def wheel(self, delta_y: int, modifiers) -> None:
        self._wheel_accumulator += delta_y
        steps = int(self._wheel_accumulator / 120)
        if steps:
            self._wheel_accumulator -= steps * 120
            self.wheelSteps.emit(-steps, modifiers)  # scroll down = next
