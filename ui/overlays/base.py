"""Base classes for painted widgets: their tooltips, and the overlays hosted by OverlayHost."""

from typing import Optional

from PySide6.QtCore import QEvent, QPointF
from PySide6.QtGui import QHelpEvent, QRegion
from PySide6.QtWidgets import QToolTip, QWidget


class TooltipWidget(QWidget):
    """A painted widget whose tooltips come from `tooltip_at(pos)`.

    An empty text hides the tooltip; with `pass_empty_tooltip` the event then goes on to the
    parent, so the parent's own tooltip shows there.
    """

    pass_empty_tooltip = False

    def tooltip_at(self, pos: QPointF) -> Optional[str]:
        """What hovering `pos` says; None leaves the tooltip to QWidget."""
        return None

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            text = self.tooltip_at(QPointF(event.pos()))  # QHelpEvent has no position()
            if text is not None:
                if text:
                    QToolTip.showText(event.globalPos(), text, self)
                else:
                    QToolTip.hideText()
                    if self.pass_empty_tooltip:
                        event.ignore()
                return True
        return super().event(event)


class Overlay(TooltipWidget):
    def setVisible(self, visible: bool) -> None:  # show()/hide() route through here
        super().setVisible(visible)
        host = self.parentWidget()
        if host is not None and hasattr(host, "overlay_changed"):
            host.overlay_changed()

    def mask_region(self) -> QRegion:
        return QRegion(self.geometry())
