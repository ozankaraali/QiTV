"""Settings over the Library (2.14; the owner: "in the lib, towards right side, you can put
settings, so instead of going back to live channels i can add plex/etc movie vendor from the
library").

Settings in the drawer and its cards are overlays of the TV window's player page. While the
Library is in front they show over the Library's page instead, on this layer: a transparent
widget over the whole page, shown while one of them shows (`overlay_changed`, as the overlay
host shows and hides). It takes the place of the picture beside Settings: a press on the Library
there closes Settings (`clicked`), as a click on the picture does, and reaches nothing under it
(a poster, a channel); the wheel scrolls nothing under it either. The mouse's Back and Forward,
over Settings, its cards or beside them, go to Settings (`backPressed`, `forwardPressed`), as
over the picture.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from ui.overlays.base import Overlay


class LibraryLayer(QWidget):
    clicked = Signal()  # a press beside Settings, on the Library under it
    backPressed = Signal()  # the mouse's Back button
    forwardPressed = Signal()

    def __init__(self, page: QWidget) -> None:
        super().__init__(page)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setGeometry(page.rect())
        page.installEventFilter(self)
        self.hide()

    def overlay_changed(self) -> None:
        """An overlay on it showed or hid (Overlay.setVisible): it shows while one does. It is
        stacked where it was made: over the page's own widgets, under what QiTV says, asks or
        offers there (made after it, and raised as they show: a message, a question, a
        menu)."""
        wanted = any(
            isinstance(child, Overlay) and not child.isHidden() for child in self.children()
        )
        if wanted == self.isHidden():  # (its own state: the page may be away meanwhile)
            self.setVisible(wanted)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.parentWidget().rect())
        return False

    def _press(self, event: QMouseEvent) -> None:
        event.accept()  # never the Library's under it
        button = event.button()
        if button == Qt.MouseButton.BackButton:
            self.backPressed.emit()
        elif button == Qt.MouseButton.ForwardButton:
            self.forwardPressed.emit()
        elif self.childAt(event.position().toPoint()) is None:
            self.clicked.emit()  # (one an overlay let through is no press beside it)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._press(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.BackButton, Qt.MouseButton.ForwardButton):
            self._press(event)  # a quick second Back or Forward is one more press
        else:
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.accept()
