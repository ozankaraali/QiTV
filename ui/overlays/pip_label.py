"""PiP label: the small surface's channel and state, a speaker for the mixer and a close button.

It covers the small surface (follow()), but only its pill and buttons take the mouse and the
host's mask: a click anywhere else on the small video reaches the surface below. The pill
stands for that video: its press, moves and release go to the PiP frame (pillPressed …), which
swaps on a click and moves the PiP on a drag, as it does for the surface itself.

The state is what the pill is for, so it is never cut: the channel name gives way first, down
to nothing, then the state's font shrinks. Only "Connecting…" may still be cut once its font
shrank a little (a mini player's 90 px PiP); LIVE and the time behind live always show whole.
"""

from dataclasses import dataclass
import re

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QFont,
    QFontMetrics,
    QHelpEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QRegion,
)
from PySide6.QtWidgets import QToolTip, QWidget

from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_icon, fill_round
from ui.pointer import NAV_BUTTONS
from ui.theme import PALETTE, qcolor

LIVE = "LIVE"
CONNECTING = "Connecting…"
TOOLTIPS = {
    "pill": "Swap (S) · drag to move",
    "mixer": "Volume",
    "close": "Close picture-in-picture (P)",
}
_SHIFTED_PREFIXES = ("−", "-")  # "−MM:SS" behind live
_BEHIND = re.compile(r"^[−-]\d+(:\d\d){1,2}$")  # "−MM:SS" or "−H:MM:SS"
_FALLBACK_HOST_WIDTH = 1280
_SHRINK = 0.8  # the smallest state font a cut state gets, of its normal size
_SMALLEST_STATE_PX = 6  # LIVE and the time behind live shrink down to this, never cut


@dataclass(frozen=True)
class _Layout:
    """Label coordinates."""

    pill: QRectF
    close: QRectF
    mixer: QRectF
    name: str  # elided to fit
    state: str  # elided to fit, unless "LIVE"
    name_font: QFont
    state_font: QFont
    pad: float
    gap: float
    dot: float  # the LIVE dot's diameter
    dot_gap: float  # between the dot and "LIVE"


class PipLabel(Overlay):
    closeRequested = Signal()
    mixerRequested = Signal()  # the speaker: the mixer opens with the PiP row selected
    # The left button on the pill, in global coordinates: for PipFrame.pill_press/_move/_release.
    pillPressed = Signal(QPoint)
    pillMoved = Signal(QPoint)
    pillReleased = Signal(QPoint)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._name = ""
        self._state = CONNECTING
        self._audible = False  # the small slot starts muted
        self._pill_held = False
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API ---------------------------------------------------------------------

    def set_info(self, name: str, state: str) -> None:
        """`state` is "LIVE", "−MM:SS" (behind live) or "Connecting…"."""
        if (name, state) != (self._name, self._state):
            self._name, self._state = name, state
            self._refresh()

    def set_audible(self, audible: bool) -> None:
        if audible != self._audible:
            self._audible = audible
            self.update()

    def follow(self, rect: QRect) -> None:
        """Cover the small surface: `rect` in host coordinates.

        An empty rect (PiP closed) also hides the label: shown, it would leave the host with
        nothing to mask, and the host would take every click.
        """
        if rect != self.geometry():
            self.setGeometry(rect)
        if rect.isEmpty():
            self.hide()
        else:
            self._refresh()

    def info(self) -> tuple[str, str]:
        return self._name, self._state

    def shown_text(self) -> tuple[str, str]:
        """The name and state as painted: elided ("…") to fit the pill."""
        layout = self._layout()
        return layout.name, layout.state

    def is_audible(self) -> bool:
        return self._audible

    def pill_rect(self) -> QRectF:
        return self._layout().pill

    def state_pixel_size(self) -> int:
        """The state's font size as painted (smaller than normal when it had to shrink)."""
        return self._layout().state_font.pixelSize()

    def button_rect(self, name: str) -> QRectF:
        """The "close" or "mixer" (speaker) button, in label coordinates."""
        layout = self._layout()
        return {"close": layout.close, "mixer": layout.mixer}[name]

    def mask_region(self) -> QRegion:
        return self._region().translated(self.geometry().topLeft())

    def tooltip_at(self, pos: QPointF) -> str:
        """What hovering `pos` says (label coordinates): the part and its key, or nothing."""
        layout = self._layout()
        for name, rect in (("close", layout.close), ("mixer", layout.mixer), ("pill", layout.pill)):
            if rect.contains(pos):
                return TOOLTIPS[name]
        return ""

    # ---- layout -------------------------------------------------------------------------

    def _units(self) -> Units:
        parent = self.parentWidget()
        return Units(parent.width() if parent is not None else _FALLBACK_HOST_WIDTH)

    def _layout(self) -> _Layout:
        un = self._units()
        name_font = un.font(1.0, QFont.Weight.DemiBold)
        state_font = un.font(1.0, QFont.Weight.Bold)
        names, states = QFontMetrics(name_font), QFontMetrics(state_font)
        inset, pad, gap = un.px(1), un.px(0.8), un.px(0.6)
        height = max(names.height(), states.height()) + 2 * un.px(0.35)
        close = QRectF(self.width() - inset - height, inset, height, height)
        mixer = close.translated(-(height + un.px(0.4)), 0)
        dot_gap = un.px(0.4)
        room = max(0.0, mixer.left() - un.px(0.6) - inset)  # the pill ends before the speaker
        # The name gives way first, then the state's font shrinks (the module docstring).
        state_room = room - 2 * pad
        whole = self._state == LIVE or bool(_BEHIND.match(self._state))
        normal = state_font.pixelSize()
        smallest = _SMALLEST_STATE_PX if whole else max(_SMALLEST_STATE_PX, round(normal * _SHRINK))
        while self._state_width(state_font, dot_gap) > state_room and (
            state_font.pixelSize() > smallest
        ):
            state_font.setPixelSize(state_font.pixelSize() - 1)
        states = QFontMetrics(state_font)
        dot = max(6, round(states.height() * 0.42))
        state = self._state
        if not whole and states.horizontalAdvance(state) > state_room:
            state = states.elidedText(state, Qt.TextElideMode.ElideRight, max(0, int(state_room)))
        state_w = self._state_width(state_font, dot_gap, state)
        elide = Qt.TextElideMode.ElideRight
        # The name may shrink to "…" or, with no room for that, to nothing.
        name_room = int(room - 2 * pad - gap - state_w)
        name = names.elidedText(self._name, elide, name_room) if name_room > 0 else ""
        width = 2 * pad + state_w + (gap + names.horizontalAdvance(name) if name else 0)
        pill = QRectF(inset, inset, min(width, room), height)
        return _Layout(
            pill, close, mixer, name, state, name_font, state_font, pad, gap, dot, dot_gap
        )

    def _state_width(self, font: QFont, dot_gap: float, text: str | None = None) -> float:
        metrics = QFontMetrics(font)
        if self._state == LIVE:
            return (
                max(6, round(metrics.height() * 0.42)) + dot_gap + metrics.horizontalAdvance(LIVE)
            )
        return metrics.horizontalAdvance(self._state if text is None else text)

    def _region(self) -> QRegion:
        """The pill and buttons, in label coordinates (empty with nothing to cover)."""
        if self.width() <= 0 or self.height() <= 0:
            return QRegion()
        layout = self._layout()
        region = QRegion()
        for part in (layout.pill, layout.mixer, layout.close):
            region = region.united(QRegion(part.toAlignedRect()))
        return region.intersected(QRegion(self.rect()))

    def _refresh(self) -> None:
        """The pill's size changed: clicks elsewhere go through, and the host masks anew."""
        region = self._region()
        if region.isEmpty():
            self.clearMask()
        else:
            self.setMask(region)  # within the host, a click beside the pill is not the label's
        host = self.parentWidget()
        if not self.isHidden() and host is not None and hasattr(host, "overlay_changed"):
            host.overlay_changed()
        self.update()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        # u follows the host's width, whichever of the host and the frame resizes first.
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._refresh()
        return False

    # ---- painting -----------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        if self.width() <= 0 or self.height() <= 0:
            return
        layout = self._layout()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self._paint_pill(p, layout)
        self._paint_backdrop(p, layout.mixer)
        inset = layout.mixer.width() * 0.13
        icon = layout.mixer.adjusted(inset, inset, -inset, -inset)
        if self._audible:
            draw_icon(p, "volume", icon, qcolor(PALETTE.accent))
        else:
            draw_icon(p, "mute", icon, qcolor(PALETTE.text, 0.85))
        self._paint_backdrop(p, layout.close)
        self._paint_cross(p, layout.close)
        p.end()

    def _paint_backdrop(self, p: QPainter, rect: QRectF) -> None:
        radius = rect.height() / 2
        fill_round(p, rect, qcolor(PALETTE.panel), radius)
        p.save()
        p.setPen(QPen(qcolor(PALETTE.text, 0.12), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        p.restore()

    def _paint_pill(self, p: QPainter, layout: _Layout) -> None:
        pill = layout.pill
        self._paint_backdrop(p, pill)
        p.save()
        p.setClipRect(pill)
        x = pill.left() + layout.pad
        if layout.name:
            p.setFont(layout.name_font)
            p.setPen(qcolor(PALETTE.text))
            name_w = QFontMetrics(layout.name_font).horizontalAdvance(layout.name)
            p.drawText(
                QRectF(x, pill.top(), name_w + 1, pill.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                layout.name,
            )
            x += name_w + layout.gap
        p.setFont(layout.state_font)
        rest = QRectF(x, pill.top(), max(0.0, pill.right() - x), pill.height())
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        if self._state == LIVE:
            d = layout.dot
            fill_round(p, QRectF(x, pill.center().y() - d / 2, d, d), qcolor(PALETTE.live), d / 2)
            p.setPen(qcolor(PALETTE.text, 0.9))
            p.drawText(rest.adjusted(d + layout.dot_gap, 0, 0, 0), align, LIVE)
        else:
            shifted = self._state.startswith(_SHIFTED_PREFIXES)
            color = (
                qcolor(PALETTE.accent)
                if shifted
                else qcolor(PALETTE.text, PALETTE.text_muted_alpha)
            )
            p.setPen(color)
            p.drawText(rest, align, layout.state)
        p.restore()

    def _paint_cross(self, p: QPainter, rect: QRectF) -> None:
        arm = rect.width() * 0.17
        centre = rect.center()
        p.save()
        pen = QPen(qcolor(PALETTE.text), max(1.5, rect.width() * 0.075))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(centre + QPointF(-arm, -arm), centre + QPointF(arm, arm))
        p.drawLine(centre + QPointF(arm, -arm), centre + QPointF(-arm, arm))
        p.restore()

    # ---- input: the buttons act on a press; the pill is the small video's ------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host: the shell's drawer layers
            return
        event.accept()
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        layout = self._layout()
        if layout.close.contains(pos):
            self.closeRequested.emit()
        elif layout.mixer.contains(pos):
            self.mixerRequested.emit()
        elif layout.pill.contains(pos):
            self._pill_held = True
            self.pillPressed.emit(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._pill_held:
            event.ignore()  # the host shows the transport
            return
        event.accept()
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.pillMoved.emit(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()
        if self._pill_held and event.button() == Qt.MouseButton.LeftButton:
            self._pill_held = False
            self.pillReleased.emit(event.globalPosition().toPoint())

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # the host counts a quick second Back as another press
            return
        event.accept()
        on_pill = self._layout().pill.contains(event.position())
        if event.button() == Qt.MouseButton.LeftButton and on_pill:
            # A press like any other: the frame knows whether it ends a swapping click.
            self._pill_held = True
            self.pillPressed.emit(event.globalPosition().toPoint())

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            text = self.tooltip_at(QPointF(event.pos()))
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    def hideEvent(self, event) -> None:
        self._pill_held = False
        super().hideEvent(event)
