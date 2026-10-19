"""The volume mixer: one row per picture, TV-style, bottom-centre (Phase 4, owner's amendment).

One row ("Main") without picture in picture; two while it is open ("Main" for the large
picture, "PiP" for the small one, each with its channel's name). Each row has a speaker
(a click mutes or unmutes it; crossed out when muted, else a wave per third of its level), a
slider (a click or a drag sets its level) and the level. The selected row is outlined; the keys
act on it (the shell routes them while the mixer shows). It hides itself 3 s after the last
change, and says so (`closed`).

Bottom-centre: 4u from the bottom, or just above the transport's controls while the
transport shows (`set_floor`), so both can show at once. Never over the PiP (`avoid`): over
it, it moves up above the PiP, or beside it when there is no room above.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetricsF, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_icon, draw_progress, fill_round, volume_icon
from ui.pointer import NAV_BUTTONS
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

HIDE_MS = 3000  # after the last change
BOTTOM_U = 4.0  # from the bottom, with the transport hidden
FLOOR_GAP_U = 1.0  # above the transport's controls while it shows
_FALLBACK_HOST_WIDTH = 1280
_PAD_U = 1.0
_ROW_U = 3.4
_ROW_GAP_U = 0.5
_ICON_U = 2.2
_GAP_U = 0.9
_SLIDER_U = 16.0
_NAME_MAX_U = 15.0
_VALUE_U = 3.4
_MARGIN_U = 1.0  # from the PiP it avoids


@dataclass(frozen=True)
class MixerRow:
    title: str  # "Main" or "PiP"
    channel: str  # the picture's channel ("" when it has none)
    level: int  # 0-100
    muted: bool


@dataclass(frozen=True)
class _RowRects:
    row: QRectF
    speaker: QRectF
    text: QRectF
    slider: QRectF  # the hit area; the bar is its middle
    value: QRectF


class Mixer(Overlay):
    levelRequested = Signal(int, int)  # row, level: a click or a drag on its slider
    muteRequested = Signal(int)  # row: a click on its speaker
    rowSelected = Signal(int)  # a click on a row selects it
    closed = Signal()  # it hid itself (or was dismissed)

    def __init__(self, parent: QWidget | None = None, idle_ms: int = HIDE_MS) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._rows: list[MixerRow] = []
        self._selected = 0
        self._drag: int | None = None  # the row whose slider the left button holds
        self._avoid = QRect()
        self._floor: int | None = None  # the transport's controls' top while it shows
        self._idle = QTimer(self, singleShot=True, interval=idle_ms)
        self._idle.timeout.connect(self.dismiss)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API ---------------------------------------------------------------------

    def show_rows(self, rows: list[MixerRow], selected: int) -> None:
        """Show these rows (or update them), `selected` outlined; it hides 3 s from now."""
        self._rows = list(rows)
        self._selected = max(0, min(selected, len(self._rows) - 1))
        self._layout()
        if self.isHidden():
            self.show()
        self.raise_()
        self.update()
        if self._drag is None:
            self._idle.start()

    def rows(self) -> list[MixerRow]:
        return list(self._rows)

    def selected(self) -> int:
        return self._selected

    def dismiss(self) -> None:
        self._idle.stop()
        self._drag = None
        if not self.isHidden():
            self.hide()
            self.closed.emit()

    def avoid(self, rect: QRect) -> None:
        """Keep clear of `rect` (host coordinates; empty: nothing to avoid), the PiP."""
        self._avoid = QRect(rect)
        if not self.isHidden():
            self._layout()

    def set_floor(self, top: int | None) -> None:
        """Sit just above `top` (host coordinates; the transport's controls), or, with None,
        4u from the bottom."""
        if top != self._floor:
            self._floor = top
            if not self.isHidden():
                self._layout()

    def speaker_rect(self, row: int) -> QRectF:
        return self._rects()[row].speaker

    def slider_rect(self, row: int) -> QRectF:
        return self._rects()[row].slider

    # ---- layout -------------------------------------------------------------------------

    def _units(self) -> Units:
        parent = self.parentWidget()
        return Units(parent.width() if parent is not None else _FALLBACK_HOST_WIDTH)

    def _fonts(self, un: Units) -> tuple[QFont, QFont, QFont]:
        return (
            un.font(1.25, QFont.Weight.Bold),  # the title
            un.font(1.15),  # the channel
            un.font(1.25, QFont.Weight.DemiBold),  # the level
        )

    def _text_width(self, un: Units) -> float:
        title, name, _value = (QFontMetricsF(f) for f in self._fonts(un))
        widest = 0.0
        for row in self._rows:
            width = title.horizontalAdvance(row.title)
            if row.channel:
                width += un.px(0.6) + min(name.horizontalAdvance(row.channel), un.px(_NAME_MAX_U))
            widest = max(widest, width)
        return widest

    def _row_height(self, un: Units) -> float:
        heights = [QFontMetricsF(f).height() for f in self._fonts(un)]
        return max(float(un.px(_ROW_U)), max(heights) + un.px(0.8))

    def _size(self) -> tuple[int, int]:
        un = self._units()
        rows = max(1, len(self._rows))
        value = max(
            float(un.px(_VALUE_U)), QFontMetricsF(self._fonts(un)[2]).horizontalAdvance("100")
        )
        width = (
            2 * un.px(_PAD_U)
            + un.px(_ICON_U)
            + un.px(_GAP_U)
            + self._text_width(un)
            + un.px(_GAP_U)
            + un.px(_SLIDER_U)
            + un.px(_GAP_U)
            + value
        )
        height = 2 * un.px(_PAD_U) + rows * self._row_height(un) + (rows - 1) * un.px(_ROW_GAP_U)
        return math.ceil(width) + 2, math.ceil(height)  # 2 px: a name that fits is never cut

    def _layout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        un = Units(parent.width())
        width, height = self._size()
        width = min(width, parent.width())
        x = (parent.width() - width) // 2
        bottom = (
            parent.height() - un.px(BOTTOM_U)
            if self._floor is None
            else self._floor - un.px(FLOOR_GAP_U)
        )
        y = max(0, bottom - height)
        rect = QRect(x, y, width, height)
        avoid = self._avoid
        if not avoid.isEmpty() and rect.intersects(avoid):
            above = avoid.top() - un.px(_MARGIN_U) - height
            if above >= 0:
                rect.moveTop(above)
            elif avoid.center().x() >= parent.width() // 2:  # beside it, away from its side
                rect.moveRight(max(width - 1, avoid.left() - un.px(_MARGIN_U)))
            else:
                rect.moveLeft(min(parent.width() - width, avoid.right() + un.px(_MARGIN_U)))
        if rect != self.geometry():
            self.setGeometry(rect)

    def _rects(self) -> list[_RowRects]:
        un = self._units()
        pad, gap = un.px(_PAD_U), un.px(_GAP_U)
        row_h = self._row_height(un)
        value_w = max(
            float(un.px(_VALUE_U)), QFontMetricsF(self._fonts(un)[2]).horizontalAdvance("100")
        )
        result = []
        y = float(pad)
        for _row in self._rows:
            row = QRectF(pad / 2, y - pad / 4, self.width() - pad, row_h + pad / 2)
            icon = un.px(_ICON_U)
            speaker = QRectF(pad, y + (row_h - icon) / 2, icon, icon)
            value = QRectF(self.width() - pad - value_w, y, value_w, row_h)
            slider = QRectF(value.left() - gap - un.px(_SLIDER_U), y, un.px(_SLIDER_U), row_h)
            text = QRectF(
                speaker.right() + gap, y, slider.left() - gap - speaker.right() - gap, row_h
            )
            result.append(_RowRects(row, speaker, text, slider, value))
            y += row_h + un.px(_ROW_GAP_U)
        return result

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if (
            obj is self.parentWidget()
            and event.type() == QEvent.Type.Resize
            and not self.isHidden()
        ):
            self._layout()
        return False

    # ---- painting -----------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._rows:
            return
        un = self._units()
        title_font, name_font, value_font = self._fonts(un)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        panel = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = un.u * 1.2
        p.setPen(QPen(qcolor(PALETTE.text, 0.12), 1.0))
        p.setBrush(qcolor(PALETTE.panel, 0.92))
        p.drawRoundedRect(panel, radius, radius)
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        for index, (row, rects) in enumerate(zip(self._rows, self._rects())):
            if index == self._selected and len(self._rows) > 1:
                fill_round(p, rects.row, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), un.u * 0.8)
                p.save()
                p.setPen(QPen(qcolor(PALETTE.accent), max(1.0, un.u * 0.12)))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(rects.row, un.u * 0.8, un.u * 0.8)
                p.restore()
            dim = row.muted or row.level == 0
            text = qcolor(PALETTE.text, PALETTE.text_muted_alpha if dim else 1.0)
            draw_icon(p, volume_icon(row.level, row.muted), rects.speaker, text)
            p.setFont(title_font)
            p.setPen(qcolor(PALETTE.text))
            title_w = QFontMetricsF(title_font).horizontalAdvance(row.title)
            p.drawText(rects.text, align, row.title)
            if row.channel:
                p.setFont(name_font)
                p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
                x = rects.text.left() + title_w + un.px(0.6)
                room = max(0.0, rects.text.right() - x)
                shown = QFontMetricsF(name_font).elidedText(
                    row.channel, Qt.TextElideMode.ElideRight, room
                )
                p.drawText(QRectF(x, rects.text.top(), room, rects.text.height()), align, shown)
            bar_h = max(3.0, un.u * 0.55)
            slider = rects.slider
            bar = QRectF(slider.left(), slider.center().y() - bar_h / 2, slider.width(), bar_h)
            fraction = row.level / 100
            accent = qcolor(PALETTE.text, 0.55) if dim else qcolor(PALETTE.accent)
            draw_progress(p, bar, fraction, track_alpha=0.18, color=accent)
            knob = max(8.0, un.u * 1.3)
            kx = bar.left() + bar.width() * fraction
            fill_round(
                p,
                QRectF(kx - knob / 2, bar.center().y() - knob / 2, knob, knob),
                qcolor("#ffffff", 0.6 if dim else 1.0),
                knob / 2,
            )
            p.setFont(value_font)
            p.setPen(text)
            p.drawText(
                rects.value,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                str(row.level),
            )
        p.end()

    # ---- input --------------------------------------------------------------------------

    def _row_at(self, pos: QPointF) -> int | None:
        for index, rects in enumerate(self._rects()):
            if rects.row.contains(pos):
                return index
        return None

    def _level_at(self, row: int, x: float) -> int:
        slider = self._rects()[row].slider
        if slider.width() <= 0:
            return 0
        return round(100 * min(1.0, max(0.0, (x - slider.left()) / slider.width())))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host: the shell's drawer layers
            return
        event.accept()
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        row = self._row_at(pos)
        if row is None:
            self._idle.start()
            return
        if row != self._selected:
            self._selected = row
            self.update()
            self.rowSelected.emit(row)
        rects = self._rects()[row]
        if rects.speaker.contains(pos):
            self._idle.start()
            self.muteRequested.emit(row)
        elif rects.slider.contains(pos):
            self._drag = row
            self._idle.stop()
            self.levelRequested.emit(row, self._level_at(row, pos.x()))
        else:
            self._idle.start()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        row = self._drag
        if row is None:
            event.ignore()  # the host shows the transport
            return
        event.accept()
        if not event.buttons() & Qt.MouseButton.LeftButton:  # the release went elsewhere
            self._drag = None
            self._idle.start()
            return
        self.levelRequested.emit(row, self._level_at(row, event.position().x()))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()
        if event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            self._drag = None
            self._idle.start()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()
            return
        self.mousePressEvent(event)  # a quick second click is another click, never fullscreen

    def hideEvent(self, event) -> None:
        self._drag = None
        super().hideEvent(event)
