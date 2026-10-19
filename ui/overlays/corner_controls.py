"""Corner controls: the buttons over the player while it sits in a corner of the Library.

Three round buttons in the corner player's top-right: a speaker that mutes or unmutes what
plays (crossed out while muted, else its waves show the volume), ⤢ that gives the player the
whole window back, and × that stops it (the Library stays). They are the only overlay shown in
that mode (the window hides the others). The overlay fills the host, which is glued over the
player page, but only the buttons take the mouse and the host's mask: a click anywhere else on
the small video reaches the page below, which brings the player back as ⤢ does.

The host's width is the corner player's, so u would make these tiny: each button is
MIN_HIT_PX wide whatever the window size.

What reaches the buttons stays there: a wheel, the mouse's Back and Forward and moves would go
on to the host, whose wheel zaps and whose Back opens the channel list of the hidden player
(the corner player is a thumbnail, as the PageStack keeps it over the video). A double-click
acts once.
"""

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QRegion,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from services.i18n import N_, translate
from ui.overlays.base import Overlay
from ui.overlays.painting import draw_icon, fill_round, volume_icon
from ui.theme import PALETTE, qcolor

BUTTONS = ("sound", "back", "stop")  # left to right
MIN_HIT_PX = 28  # each button is this square at least, at the default window and any other
GAP_PX = 6
INSET_PX = 6
# No key: M types into the Library's search, which has the keys while the player is here.
# Shown with translate().
MUTE_TOOLTIP = N_("Mute")  # i18n: tooltip of the corner player's speaker (verb)
UNMUTE_TOOLTIP = N_("Unmute")  # i18n: tooltip of the corner player's speaker (verb)
TOOLTIPS = {
    "back": N_("Back to the player"),  # i18n: tooltip: the corner player takes the window again
    "stop": N_("Stop"),  # i18n: tooltip of the corner player's × (verb: stop playing)
}


class CornerControls(Overlay):
    pass_empty_tooltip = True  # off its controls, the parent's tooltip shows

    soundRequested = Signal()
    backRequested = Signal()  # ⤢
    stopRequested = Signal()  # ×

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)  # never the host's
        self._muted = False
        self._level = 100  # the volume of what plays (libVLC's default until set_level)
        if parent is not None:
            parent.installEventFilter(self)
            self.setGeometry(parent.rect())
        self._refresh()
        self.hide()

    # ---- public API ---------------------------------------------------------------------

    def set_muted(self, muted: bool) -> None:
        if muted != self._muted:
            self._muted = muted
            self.update()

    def set_level(self, level: int) -> None:
        """The volume of what plays (0-100): the speaker's waves show it."""
        if level != self._level:
            self._level = level
            self.update()

    def button_rect(self, name: str) -> QRectF:
        """Where the "sound", "back" or "stop" button is and takes clicks (own coordinates)."""
        if name not in BUTTONS:
            raise KeyError(name)
        return self._rects()[name]

    def mask_region(self) -> QRegion:
        return self._region().translated(self.geometry().topLeft())

    def tooltip_at(self, pos: QPointF) -> str:
        name = self._part_at(pos)
        if name == "sound":
            return translate(UNMUTE_TOOLTIP if self._muted else MUTE_TOOLTIP)
        text = TOOLTIPS.get(name or "", "")
        return translate(text) if text else ""

    # ---- layout -------------------------------------------------------------------------

    def _rects(self) -> dict[str, QRectF]:
        """The buttons in a row at the top-right, `stop` at the corner."""
        size = float(MIN_HIT_PX)
        bounds = QRectF(self.rect())
        rects: dict[str, QRectF] = {}
        x = self.width() - INSET_PX - size
        for name in reversed(BUTTONS):
            rects[name] = QRectF(x, INSET_PX, size, size).intersected(bounds)
            x -= size + GAP_PX
        return {name: rects[name] for name in BUTTONS}

    def _part_at(self, pos: QPointF) -> str | None:
        for name, rect in self._rects().items():
            if rect.contains(pos):
                return name
        return None

    def _region(self) -> QRegion:
        if self.width() <= 0 or self.height() <= 0:
            return QRegion()
        region = QRegion()
        for rect in self._rects().values():
            region = region.united(QRegion(rect.toAlignedRect()))
        return region.intersected(QRegion(self.rect()))

    def _refresh(self) -> None:
        region = self._region()
        if region.isEmpty():
            self.clearMask()
        else:
            self.setMask(region)  # a click beside the buttons is not this overlay's
        host = self.parentWidget()
        if not self.isHidden() and host is not None and hasattr(host, "overlay_changed"):
            host.overlay_changed()
        self.update()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.parentWidget().rect())  # it fills the host
            self._refresh()
        return False

    # ---- painting -----------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for name, rect in self._rects().items():
            fill_round(p, rect, qcolor(PALETTE.panel), rect.height() / 2)
            p.save()
            p.setPen(QPen(qcolor(PALETTE.text, 0.12), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))
            p.restore()
            inset = rect.width() * 0.22
            icon = rect.adjusted(inset, inset, -inset, -inset)
            if name == "stop":
                self._paint_cross(p, icon)
            elif name == "back":
                self._paint_expand(p, icon)
            else:
                speaker = volume_icon(self._level, self._muted)
                draw_icon(p, speaker, icon, qcolor(PALETTE.text, 0.85))
        p.end()

    @staticmethod
    def _pen(width: float) -> QPen:
        pen = QPen(qcolor(PALETTE.text), max(1.5, width * 0.12))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        return pen

    def _paint_cross(self, p: QPainter, rect: QRectF) -> None:
        p.save()
        p.setPen(self._pen(rect.width()))
        p.drawLine(rect.topLeft(), rect.bottomRight())
        p.drawLine(rect.topRight(), rect.bottomLeft())
        p.restore()

    def _paint_expand(self, p: QPainter, rect: QRectF) -> None:
        """⤢: a diagonal with an arrowhead at each end."""
        head = rect.width() * 0.38
        top_right, bottom_left = rect.topRight(), rect.bottomLeft()
        p.save()
        p.setPen(self._pen(rect.width()))
        p.drawLine(bottom_left, top_right)
        p.drawLine(top_right, top_right + QPointF(-head, 0))
        p.drawLine(top_right, top_right + QPointF(0, head))
        p.drawLine(bottom_left, bottom_left + QPointF(head, 0))
        p.drawLine(bottom_left, bottom_left + QPointF(0, -head))
        p.restore()

    # ---- input: the buttons act on a press ----------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()
        if event.button() != Qt.MouseButton.LeftButton:
            return  # Back and Forward too: the Library's page does nothing with them either
        part = self._part_at(event.position())
        if part == "sound":
            self.soundRequested.emit()
        elif part == "back":
            self.backRequested.emit()
        elif part == "stop":
            self.stopRequested.emit()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()  # its first click acted: a mute would be undone at once

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.accept()
