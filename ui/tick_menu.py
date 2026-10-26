"""A menu whose ticks change while it stays open (2.8.1: the Library's providers)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QMenu

_TICK_KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)


class TickMenu(QMenu):
    """A click (pressed and released on it), Return or Space on a tickable entry ticks or
    unticks it and the menu stays open; any other entry acts and closes it, as in any menu."""

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self._pressed: Optional[QAction] = None

    def _tickable(self, action: Optional[QAction]) -> bool:
        return action is not None and action.isEnabled() and action.isCheckable()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._pressed = self.actionAt(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        pressed, self._pressed = self._pressed, None
        action = self.actionAt(event.position().toPoint())
        if event.button() != Qt.MouseButton.LeftButton or not self._tickable(action):
            super().mouseReleaseEvent(event)
        elif action is pressed:  # else the click that opened the menu: not a choice
            action.trigger()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        action = self.activeAction()
        if event.key() in _TICK_KEYS and self._tickable(action):
            action.trigger()
            return
        super().keyPressEvent(event)
