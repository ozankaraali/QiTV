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


def host_windows(host: QWidget) -> set[QWidget]:
    """The windows a press counts as on the host's page in (a panel closes on one beside it):
    the host's own, its parent's (the TV window), and the one it follows meanwhile (the macOS
    mini player's small window, which holds the player page: OverlayHost.followed)."""
    ours = {host}
    outer = host.parentWidget()
    if outer is not None:
        ours.add(outer.window())
    followed = getattr(host, "followed", None)
    window = followed() if callable(followed) else None
    if isinstance(window, QWidget):
        ours.add(window.window())
    return ours


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
    # It has the keys while it shows (a card, a panel, a list, Settings): a painted menu that
    # shows as it comes closes (2.13.1, ui/overlays/popup_menu.py), so that the keys always go
    # to the topmost thing; a toast, a notice or the player's bar leave it open, and so does
    # the mixer, the sound keys' level too (they go past an open menu, which stays over it).
    takes_keys = False

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        if self.accessible_name:
            self.setAccessibleName(translate(self.accessible_name))

    def setVisible(self, visible: bool) -> None:  # show()/hide() route through here
        super().setVisible(visible)
        host = self.parentWidget()
        if host is not None and hasattr(host, "overlay_changed"):
            host.overlay_changed()

    def move_to(self, parent: QWidget) -> None:
        """Show over `parent` from now on (2.14: Settings and its cards over the Library's page,
        and back over the picture), its resizes followed there (an overlay that fits itself to
        its parent watches it); shown there if it was, the old parent told it went."""
        old = self.parentWidget()
        if old is parent:
            return
        shown = not self.isHidden()
        if old is not None:
            old.removeEventFilter(self)
        self.setParent(parent)  # (Qt hides it as it moves)
        parent.installEventFilter(self)
        self._moved()
        if old is not None and hasattr(old, "overlay_changed"):
            old.overlay_changed()
        if shown:
            self.show()

    def _moved(self) -> None:
        """It is over another parent now (`move_to`), not shown yet: what lays itself out
        against its parent does so here."""

    def mask_region(self) -> QRegion:
        return QRegion(self.geometry())
