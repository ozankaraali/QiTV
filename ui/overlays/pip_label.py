"""PiP label: the small surface's channel and state, then a swap button (⇄), a speaker that
mutes or unmutes the small picture and a close button (×).

It covers the small surface (follow()), but only its pill and buttons take the mouse and the
host's mask: a click anywhere else on the small video reaches the surface below. The pill
stands for that video: its press, moves and release go to the PiP frame (pillPressed …), which
swaps on a click and moves the PiP on a drag, as it does for the surface itself.

Each button takes clicks over a square MIN_HIT_PX wide (or the button drawn and its spacing,
when larger), and the pill keeps room for LIVE or a time behind live at their normal size. A PiP
too small for that leaves the swap button out (the pill swaps too), then gives the other two
less room, down to the drawn size and its spacing.

The state is what the pill is for, so it is never cut: the channel name gives way first, down
to nothing, then the state's font shrinks. Only "Connecting…" may still be cut once its font
shrank a little (a mini player's 90 px PiP); LIVE and the time behind live always show whole,
and so does REC, which a channel that records shows instead (2.3), after a red dot as LIVE.
"""

from dataclasses import dataclass
import re

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QRegion,
)
from PySide6.QtWidgets import QWidget

from services.i18n import N_, translate
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_icon, fill_round
from ui.pointer import NAV_BUTTONS
from ui.theme import PALETTE, qcolor

# The states (`set_info`) are kept in English, and painted translated.
LIVE = N_("LIVE")  # i18n: badge on a small picture or a multiview tile (live TV); max ~6 letters
# i18n: a small picture's or a multiview tile's state until its channel plays
CONNECTING = N_("Connecting…")
# i18n: a small picture's or a multiview tile's state while its channel records; max ~6 letters
REC = N_("REC")
DOTTED = (LIVE, REC)  # states painted after a red dot, never cut
BUTTONS = ("swap", "speaker", "close")  # after the pill, left to right
MIN_HIT_PX = 28  # each button takes clicks over this square at least, room permitting
TOOLTIPS = {
    # i18n: tooltip of the small picture's label; S is the key
    "pill": N_("Swap (S) · drag to move"),
    "swap": N_("Swap (S)"),  # i18n: tooltip: swap the small and the large picture; S is the key
    "close": N_("Close picture-in-picture (P)"),  # i18n: tooltip; P is the key
}
MUTE_TOOLTIP = N_("Mute the small picture")  # i18n: tooltip of the small picture's speaker
UNMUTE_TOOLTIP = N_("Unmute the small picture")  # i18n: tooltip of the small picture's speaker
_SHIFTED_PREFIXES = ("−", "-")  # "−MM:SS" behind live
_BEHIND = re.compile(r"^[−-]\d+(:\d\d){1,2}$")  # "−MM:SS" or "−H:MM:SS"
_FALLBACK_HOST_WIDTH = 1280
_SHRINK = 0.8  # the smallest state font a cut state gets, of its normal size
_SMALLEST_STATE_PX = 6  # LIVE and the time behind live shrink down to this, never cut
_TYPICAL_BEHIND = "−00:00"  # the pill keeps room for this, or LIVE, before the buttons grow


def paint_backdrop(p: QPainter, rect: QRectF, radius: float | None = None) -> None:
    """The label's look behind a pill or a button (a multiview tile's card too): the panel
    colour, round, with a faint edge."""
    radius = rect.height() / 2 if radius is None else radius
    fill_round(p, rect, qcolor(PALETTE.panel), radius)
    p.save()
    p.setPen(QPen(qcolor(PALETTE.text, 0.12), 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    p.restore()


def paint_pill(
    p: QPainter,
    pill: QRectF,
    name: str,
    state: str,
    name_font: QFont,
    state_font: QFont,
    pad: float,
    gap: float,
    dot: float,
    dot_gap: float,
) -> None:
    """A channel's name and its state in a pill, as the PiP label shows them (a multiview
    tile's label too): LIVE after a red dot, a time behind live in the accent colour, any
    other state muted. `name` and `state` fit already (elided); `state` is LIVE or REC (in
    English, after a red dot) or the text to show."""
    paint_backdrop(p, pill)
    p.save()
    p.setClipRect(pill)
    x = pill.left() + pad
    if name:
        p.setFont(name_font)
        p.setPen(qcolor(PALETTE.text))
        name_w = QFontMetrics(name_font).horizontalAdvance(name)
        p.drawText(
            QRectF(x, pill.top(), name_w + 1, pill.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            name,
        )
        x += name_w + gap
    p.setFont(state_font)
    rest = QRectF(x, pill.top(), max(0.0, pill.right() - x), pill.height())
    align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    if state in DOTTED:
        fill_round(
            p, QRectF(x, pill.center().y() - dot / 2, dot, dot), qcolor(PALETTE.live), dot / 2
        )
        p.setPen(qcolor(PALETTE.text, 0.9))
        p.drawText(rest.adjusted(dot + dot_gap, 0, 0, 0), align, translate(state))
    else:
        shifted = state.startswith(_SHIFTED_PREFIXES)
        p.setPen(
            qcolor(PALETTE.accent) if shifted else qcolor(PALETTE.text, PALETTE.text_muted_alpha)
        )
        p.drawText(rest, align, state)
    p.restore()


@dataclass(frozen=True)
class _Layout:
    """Label coordinates."""

    pill: QRectF
    hits: dict[str, QRectF]  # the buttons shown (BUTTONS) and where each takes clicks
    glyphs: dict[str, QRectF]  # where each is drawn, centred in its hit area
    name: str  # elided to fit
    state: str  # elided to fit, unless "LIVE"
    name_font: QFont
    state_font: QFont
    pad: float
    gap: float
    dot: float  # the LIVE dot's diameter
    dot_gap: float  # between the dot and "LIVE"


class PipLabel(Overlay):
    pass_empty_tooltip = True  # off its controls, the parent's tooltip shows

    closeRequested = Signal()
    muteRequested = Signal()  # the speaker: mute or unmute the small picture
    swapRequested = Signal()  # ⇄: as a click on the small picture
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
        """`state` is "LIVE", "−MM:SS" (behind live), "Connecting…", "Finished", or "" (a
        movie or an episode: the name alone); in English: it is painted translated."""
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
        """Where the "swap", "speaker" or "close" button takes clicks (label coordinates);
        empty when it is left out."""
        if name not in BUTTONS:
            raise KeyError(name)
        return QRectF(self._layout().hits.get(name, QRectF()))

    def glyph_rect(self, name: str) -> QRectF:
        """Where that button is drawn, within its hit area; empty when it is left out."""
        if name not in BUTTONS:
            raise KeyError(name)
        return QRectF(self._layout().glyphs.get(name, QRectF()))

    def mask_region(self) -> QRegion:
        return self._region().translated(self.geometry().topLeft())

    def tooltip_at(self, pos: QPointF) -> str:
        """What hovering `pos` says (label coordinates): the part and its key, or nothing."""
        name = self._part_at(pos)
        if name == "speaker":
            return translate(MUTE_TOOLTIP if self._audible else UNMUTE_TOOLTIP)
        text = TOOLTIPS.get(name or "", "")
        return translate(text) if text else ""

    def _part_at(self, pos: QPointF) -> str | None:
        """The button or the pill at `pos` (label coordinates), else None."""
        layout = self._layout()
        for name, rect in layout.hits.items():
            if rect.contains(pos):
                return name
        return "pill" if layout.pill.contains(pos) else None

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
        dot_gap = un.px(0.4)
        least_state = max(
            self._live_width(state_font, dot_gap), states.horizontalAdvance(_TYPICAL_BEHIND)
        )
        hits, glyphs = self._buttons(un, height, 2 * pad + least_state)
        # The pill ends before the first button's hit area.
        room = max(0.0, min(r.left() for r in hits.values()) - un.px(0.6) - inset)
        # The name gives way first, then the state's font shrinks (the module docstring).
        state_room = room - 2 * pad
        whole = self._state in DOTTED or bool(_BEHIND.match(self._state))
        normal = state_font.pixelSize()
        smallest = _SMALLEST_STATE_PX if whole else max(_SMALLEST_STATE_PX, round(normal * _SHRINK))
        while self._state_width(state_font, dot_gap) > state_room and (
            state_font.pixelSize() > smallest
        ):
            state_font.setPixelSize(state_font.pixelSize() - 1)
        states = QFontMetrics(state_font)
        dot = max(6, round(states.height() * 0.42))
        state = translate(self._state)
        if not whole and states.horizontalAdvance(state) > state_room:
            state = states.elidedText(state, Qt.TextElideMode.ElideRight, max(0, int(state_room)))
        state_w = self._state_width(state_font, dot_gap, state)
        elide = Qt.TextElideMode.ElideRight
        between = gap if state else 0.0  # the name alone ends at the pad
        # The name may shrink to "…" or, with no room for that, to nothing.
        name_room = int(room - 2 * pad - between - state_w)
        name = names.elidedText(self._name, elide, name_room) if name_room > 0 else ""
        width = 2 * pad + state_w + (between + names.horizontalAdvance(name) if name else 0)
        pill = QRectF(inset, inset, min(width, room), height)
        return _Layout(
            pill, hits, glyphs, name, state, name_font, state_font, pad, gap, dot, dot_gap
        )

    def _buttons(
        self, un: Units, glyph: float, pill: float
    ) -> tuple[dict[str, QRectF], dict[str, QRectF]]:
        """The buttons' hit areas and drawn squares (`glyph` wide), close at the right inset.

        Each hit area is a square `slot` wide with the drawn button at its centre: MIN_HIT_PX,
        or the drawn button and its spacing when larger, while the pill keeps `pill` (the
        module docstring). The swap button is left out first, then the other two get less.
        """
        inset, least = un.px(1), glyph + un.px(0.4)
        # Room for the hit areas, from the close button's centre leftwards, the pill's aside.
        free = self.width() - 2 * inset - un.px(0.6) - glyph / 2 - pill
        three = free / (len(BUTTONS) - 0.5) >= max(least, MIN_HIT_PX)
        names = BUTTONS if three else BUTTONS[1:]
        slot = int(max(least, min(MIN_HIT_PX, free / (len(names) - 0.5))))  # whole pixels
        cy = inset + glyph / 2
        cx = self.width() - inset - glyph / 2
        bounds = QRectF(self.rect())
        hits: dict[str, QRectF] = {}
        glyphs: dict[str, QRectF] = {}
        for name in reversed(names):  # from the right
            hits[name] = QRectF(cx - slot / 2, cy - slot / 2, slot, slot).intersected(bounds)
            glyphs[name] = QRectF(cx - glyph / 2, cy - glyph / 2, glyph, glyph)
            cx -= slot
        order = [name for name in BUTTONS if name in hits]
        return {n: hits[n] for n in order}, {n: glyphs[n] for n in order}

    def _live_width(self, font: QFont, dot_gap: float) -> float:
        """LIVE and its dot in `font`, as the pill paints them."""
        metrics = QFontMetrics(font)
        live = metrics.horizontalAdvance(translate(LIVE))
        return max(6, round(metrics.height() * 0.42)) + dot_gap + live

    def _state_width(self, font: QFont, dot_gap: float, text: str | None = None) -> float:
        if self._state in DOTTED:
            metrics = QFontMetrics(font)
            dot = max(6, round(metrics.height() * 0.42))
            return dot + dot_gap + metrics.horizontalAdvance(translate(self._state))
        shown = translate(self._state) if text is None else text
        return QFontMetrics(font).horizontalAdvance(shown)

    def _region(self) -> QRegion:
        """The pill and buttons, in label coordinates (empty with nothing to cover)."""
        if self.width() <= 0 or self.height() <= 0:
            return QRegion()
        layout = self._layout()
        region = QRegion()
        for part in (layout.pill, *layout.hits.values()):
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
        for name, rect in layout.glyphs.items():
            self._paint_backdrop(p, rect)
            inset = rect.width() * 0.13
            icon = rect.adjusted(inset, inset, -inset, -inset)
            if name == "close":
                self._paint_cross(p, rect)
            elif name == "swap":
                draw_icon(p, "swap", icon, qcolor(PALETTE.text, 0.85))
            elif self._audible:
                draw_icon(p, "volume", icon, qcolor(PALETTE.accent))
            else:
                draw_icon(p, "mute", icon, qcolor(PALETTE.text, 0.85))
        p.end()

    def _paint_backdrop(self, p: QPainter, rect: QRectF) -> None:
        paint_backdrop(p, rect)

    def _paint_pill(self, p: QPainter, layout: _Layout) -> None:
        paint_pill(
            p,
            layout.pill,
            layout.name,
            LIVE if self._state == LIVE else layout.state,
            layout.name_font,
            layout.state_font,
            layout.pad,
            layout.gap,
            layout.dot,
            layout.dot_gap,
        )

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
        part = self._part_at(event.position())
        if part == "close":
            self.closeRequested.emit()
        elif part == "speaker":
            self.muteRequested.emit()
        elif part == "swap":
            self.swapRequested.emit()
        elif part == "pill":
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
        on_pill = self._part_at(event.position()) == "pill"
        if event.button() == Qt.MouseButton.LeftButton and on_pill:
            # A press like any other: the frame knows whether it ends a swapping click.
            self._pill_held = True
            self.pillPressed.emit(event.globalPosition().toPoint())

    def hideEvent(self, event) -> None:
        self._pill_held = False
        super().hideEvent(event)
