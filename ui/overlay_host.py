"""Translucent window glued over the video surface; parent of every overlay (spec §5.7)."""

import logging

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QRegion
from PySide6.QtWidgets import QWidget

from ui.macos_window import attach_child_window

logger = logging.getLogger(__name__)

_TRACKED = {
    QEvent.Type.Move,
    QEvent.Type.Resize,
    QEvent.Type.Show,
    QEvent.Type.Hide,
    QEvent.Type.WindowStateChange,
}


class OverlayHost(QWidget):
    clicked = Signal()
    doubleClicked = Signal()
    mouseMoved = Signal()
    wheelSteps = Signal(int, object)  # steps, Qt.KeyboardModifier

    def __init__(self, window: QWidget, surface: QWidget):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(window, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self._window = window
        self._surface = surface
        self._overlays: list[QWidget] = []
        self._attached = False
        window.installEventFilter(self)
        surface.installEventFilter(self)

    def add_overlay(self, widget: QWidget) -> None:
        self._overlays.append(widget)
        widget.setParent(self)
        widget.hide()
        widget.installEventFilter(self)

    def overlay_changed(self) -> None:
        self._update_mask()
        self._update_visibility()

    def visible_overlays(self) -> list[QWidget]:
        return [w for w in self._overlays if w.isVisibleTo(self)]

    def sync_geometry(self) -> None:
        rect = QRect(self._surface.mapToGlobal(QPoint(0, 0)), self._surface.size())
        if rect != self.geometry():
            self.setGeometry(rect)
        self._update_mask()

    def _update_mask(self) -> None:
        region = QRegion()
        for widget in self.visible_overlays():
            part = (
                widget.mask_region()
                if hasattr(widget, "mask_region")
                else QRegion(widget.geometry())
            )
            region = region.united(part)
        if region.isEmpty():
            self.clearMask()
        else:
            self.setMask(region)

    def _window_shown(self) -> bool:
        return self._window.isVisible() and not self._window.isMinimized()

    def _update_visibility(self) -> None:
        wanted = bool(self.visible_overlays()) and self._window_shown()
        if wanted:
            self.sync_geometry()
            if not self.isVisible():
                self.show()
                self.raise_()
                if not self._attached:
                    self._attached = attach_child_window(self._window, self)
        elif self.isVisible():
            self.hide()

    def eventFilter(self, obj, event):
        kind = event.type()
        if obj in (self._window, self._surface) and kind in _TRACKED:
            if self.isVisible():
                self.sync_geometry()
            self._update_visibility()
        elif obj in self._overlays and kind in (QEvent.Type.Resize, QEvent.Type.Move):
            self._update_mask()
        return False

    # Clicks that reach the host itself (on transparent parts of overlays that let them through).
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit()

    def mouseMoveEvent(self, event):
        self.mouseMoved.emit()

    def wheelEvent(self, event):
        steps = -int(event.angleDelta().y() / 120)
        if steps:
            self.wheelSteps.emit(steps, event.modifiers())
        event.accept()
