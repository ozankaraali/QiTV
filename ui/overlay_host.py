"""Translucent window glued over the video surface; parent of every overlay (spec §5.7)."""

import logging

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QRegion
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from ui.macos_window import attach_child_window
from ui.pointer import PointerInput

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
    backPressed = Signal()  # the mouse's Back and Forward buttons, over the video or overlays
    forwardPressed = Signal()

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
        self._attached_id = 0
        self._pointer = PointerInput(self)
        self._pointer.clicked.connect(self.clicked)
        self._pointer.doubleClicked.connect(self.doubleClicked)
        self._pointer.wheelSteps.connect(self.wheelSteps)
        self._pointer.backPressed.connect(self.backPressed)
        self._pointer.forwardPressed.connect(self.forwardPressed)
        window.installEventFilter(self)
        surface.installEventFilter(self)

    def add_overlay(self, widget: QWidget) -> None:
        """Host an overlay; one with `stays_on_top` (the toast) stays above the others."""
        self._overlays.append(widget)
        widget.setParent(self)
        widget.hide()
        widget.installEventFilter(self)
        widget.destroyed.connect(lambda _=None, w=widget: self._forget(w))
        self._raise_on_top()

    def remove_overlay(self, widget: QWidget) -> None:
        self._forget(widget)
        if isValid(widget):
            widget.removeEventFilter(self)
        self.overlay_changed()

    def _forget(self, widget) -> None:
        if widget in self._overlays:
            self._overlays.remove(widget)

    def _raise_on_top(self) -> None:
        for widget in self._overlays:
            if isValid(widget) and getattr(widget, "stays_on_top", False):
                widget.raise_()

    def overlay_changed(self) -> None:
        self._update_mask()
        self._update_visibility()

    def visible_overlays(self) -> list[QWidget]:
        return [w for w in self._overlays if isValid(w) and w.isVisibleTo(self)]

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
        return isValid(self._window) and self._window.isVisible() and not self._window.isMinimized()

    def _update_visibility(self) -> None:
        wanted = bool(self.visible_overlays()) and self._window_shown()
        if wanted:
            self.sync_geometry()
            if not self.isVisible():
                self.show()
                # On every show: macOS drops a child window from its parent when the child
                # is ordered out. A child is ordered above its parent: no raise there, since
                # on macOS raise_() activates QiTV, and an overlay shown over the inactive
                # window (a wheel zap's banner) must not take the focus from another app.
                native_id = int(self.winId())
                if attach_child_window(self._window, self):
                    self._attached_id = native_id
                else:
                    self.raise_()
        elif self.isVisible():
            self.hide()

    def eventFilter(self, obj, event):
        if not (isValid(self) and isValid(self._window) and isValid(self._surface)):
            return False
        kind = event.type()
        if obj in (self._window, self._surface) and kind in _TRACKED:
            if self.isVisible():
                self.sync_geometry()
            self._update_visibility()
        elif obj in self._overlays and kind in (QEvent.Type.Resize, QEvent.Type.Move):
            self._update_mask()
        elif (
            obj in self._overlays
            and kind == QEvent.Type.ZOrderChange
            and not getattr(obj, "stays_on_top", False)
        ):
            self._raise_on_top()  # an overlay raised itself over the toast
        return False

    # Clicks that reach the host itself (on transparent parts of overlays that let them through,
    # and the Back/Forward buttons, which the overlays pass on).
    def mousePressEvent(self, event):
        self._pointer.press(event.button())

    def mouseDoubleClickEvent(self, event):
        self._pointer.double_click(event.button())

    def mouseMoveEvent(self, event):
        self.mouseMoved.emit()

    def wheelEvent(self, event):
        self._pointer.wheel(event.angleDelta().y(), event.modifiers())
        event.accept()
