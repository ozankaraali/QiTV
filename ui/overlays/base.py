"""Base classes for painted widgets: their tooltips, and the overlays hosted by OverlayHost.

Assistive technology (a screen reader) sees each painted overlay as one element: its
accessible name says what it is ("Player controls"). A message that shows (a toast, a
question) is named after its text and announced (`announce`).
"""

from typing import Optional

from PySide6.QtCore import QEvent, QPointF
from PySide6.QtGui import (
    QAccessible,
    QAccessibleAnnouncementEvent,
    QAccessibleEvent,
    QHelpEvent,
    QRegion,
    Qt,
)
from PySide6.QtWidgets import QToolTip, QWidget

from services.i18n import translate


def announce(widget: QWidget, text: str, assertive: bool = False, *, speak: bool = True) -> None:
    """`widget` is named `text` for assistive technology, which says it now (a screen reader
    reads it out): politely, after what it is saying, or at once (`assertive`: an error).
    Only a widget the user can see speaks (the other page's toast stays quiet); `speak=False`
    only renames it (a hint coming back after a message)."""
    if widget.accessibleName() != text:
        widget.setAccessibleName(text)
        QAccessible.updateAccessibility(QAccessibleEvent(widget, QAccessible.Event.NameChanged))
    if not speak or not text or not widget.isVisible():
        return
    event = QAccessibleAnnouncementEvent(widget, text)
    event.setPoliteness(
        QAccessible.AnnouncementPoliteness.Assertive
        if assertive
        else QAccessible.AnnouncementPoliteness.Polite
    )
    QAccessible.updateAccessibility(event)


def plain_tooltip(text: str) -> str:
    """`text` as a tooltip shows it as it is: Qt shows a text that looks like HTML as HTML,
    and a tooltip may name a provider's channel or programme ("<h1>…", an <img> of a local
    file). Such a text is escaped (as rich text that reads as the text); any other stays."""
    if Qt.mightBeRichText(text):
        return Qt.convertFromPlainText(text, Qt.WhiteSpaceMode.WhiteSpaceNormal)
    return text


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
                    QToolTip.showText(event.globalPos(), plain_tooltip(text), self)
                else:
                    QToolTip.hideText()
                    if self.pass_empty_tooltip:
                        event.ignore()
                return True
        return super().event(event)


class Overlay(TooltipWidget):
    # What assistive technology calls it (an English text marked with N_, shown translated);
    # "": nothing, or a name set as it shows (a toast is named after its message).
    accessible_name = ""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        if self.accessible_name:
            self.setAccessibleName(translate(self.accessible_name))

    def setVisible(self, visible: bool) -> None:  # show()/hide() route through here
        super().setVisible(visible)
        host = self.parentWidget()
        if host is not None and hasattr(host, "overlay_changed"):
            host.overlay_changed()

    def mask_region(self) -> QRegion:
        return QRegion(self.geometry())
