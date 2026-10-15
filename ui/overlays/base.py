"""Base class for overlays hosted by OverlayHost."""

from PySide6.QtGui import QRegion
from PySide6.QtWidgets import QWidget


class Overlay(QWidget):
    def setVisible(self, visible: bool) -> None:  # show()/hide() route through here
        super().setVisible(visible)
        host = self.parentWidget()
        if host is not None and hasattr(host, "overlay_changed"):
            host.overlay_changed()

    def mask_region(self) -> QRegion:
        return QRegion(self.geometry())
