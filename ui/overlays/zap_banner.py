"""Zapping banner (mockup A1): number, logo, name, now/next with progress over a bottom scrim."""

from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPaintEvent,
    QPixmap,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

from services.epg_view import Programme
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_logo, draw_progress, elided, fill_round, fmt_clock
from ui.theme import PALETTE, qcolor


@dataclass
class BannerInfo:
    number: str
    name: str
    logo: QPixmap | None
    now: Programme | None
    next: Programme | None
    upcoming: list[Programme] = field(default_factory=list)
    is_live: bool = True
    clock: datetime = field(default_factory=lambda: datetime.now().astimezone())


class ZapBanner(Overlay):
    def __init__(self, parent: QWidget | None = None, visible_ms: int = 4000) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._info: BannerInfo | None = None
        self._digits = ""
        self._visible_ms = visible_ms
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(1.0)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(250)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(QEasingCurve.Type.Linear)
        self._anim.finished.connect(self._faded)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    @property
    def info(self) -> BannerInfo | None:
        return self._info

    # ---- public API ---------------------------------------------------------------------

    def show_info(self, info: BannerInfo) -> None:
        self._info = info
        self._reveal()
        if not self._digits:
            self._timer.start(self._visible_ms)

    def show_digits(self, text: str) -> None:
        if not text and not self.isVisible():
            self._digits = ""
            return
        self._digits = text
        self._reveal()
        if text:
            self._timer.stop()
        else:
            self._timer.start(self._visible_ms)

    def dismiss(self) -> None:
        self._timer.stop()
        self._anim.stop()
        self._digits = ""
        self.hide()

    # ---- internals ----------------------------------------------------------------------

    def _reveal(self) -> None:
        self._anim.stop()
        self._effect.setOpacity(1.0)
        self._layout()
        self.show()
        self.raise_()
        self.update()

    def _fade_out(self) -> None:
        if self._digits:
            return
        self._anim.start()

    def _faded(self) -> None:
        self.hide()
        self._effect.setOpacity(1.0)

    def _layout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        height = round(parent.height() * 0.48)
        self.setGeometry(0, parent.height() - height, parent.width(), height)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize and self.isVisible():
            self._layout()
        return False

    # ---- painting -----------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        info = self._info
        if info is None and not self._digits:
            return
        units = Units(self.width())
        u = units.u
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        self._paint_scrim(p)

        text = qcolor(PALETTE.text)
        accent = qcolor(PALETTE.accent)
        num_font = units.font(5.4, QFont.Weight.Light)
        name_font = units.font(2.6, QFont.Weight.Bold)
        clock_font = units.font(2.1, QFont.Weight.DemiBold)
        now_font = units.font(1.9)
        now_bold = units.font(1.9, QFont.Weight.DemiBold)
        next_font = units.font(1.5)

        def lh(font: QFont) -> int:
            return QFontMetrics(font).height()

        has_epg = info is not None and info.now is not None
        gap1, gap2, gap3 = units.px(0.6), units.px(0.7), units.px(0.7)
        bar_h = max(2.0, 0.45 * u)
        body_h: float = lh(name_font) + gap1 + lh(now_font)
        if has_epg:
            body_h += gap2 + bar_h + gap3 + lh(next_font)
        logo_s = 8 * u
        row_h = max(body_h, logo_s)
        row_bottom = self.height() - 3 * u
        row_top = row_bottom - row_h
        left, gap = 3 * u, 2 * u
        right = self.width() - 3 * u

        # number
        number_text = self._digits or (info.number if info else "")
        while num_font.pixelSize() > 8 and (
            QFontMetrics(num_font).horizontalAdvance(number_text) > 9 * u - 2
        ):
            num_font.setPixelSize(num_font.pixelSize() - 1)
        p.setFont(num_font)
        p.setPen(accent if self._digits else qcolor(PALETTE.text, 0.95))
        p.drawText(QRectF(left, row_top, 9 * u, row_h), Qt.AlignLeft | Qt.AlignVCenter, number_text)
        if info is None:
            p.end()
            return
        x = left + 9 * u + gap
        draw_logo(
            p,
            QRectF(x, row_top + (row_h - logo_s) / 2, logo_s, logo_s),
            info.name,
            info.logo,
            0.9 * u,
        )
        bx = x + logo_s + gap
        bw = right - bx
        y = row_top + (row_h - body_h) / 2

        # name line + LIVE pill + clock
        p.setFont(clock_font)
        clock = fmt_clock(info.clock)
        clock_w = QFontMetrics(clock_font).horizontalAdvance(clock)
        p.setPen(qcolor(PALETTE.text, 0.9))
        p.drawText(
            QRectF(right - clock_w - 2, y, clock_w + 2, lh(name_font)),
            Qt.AlignRight | Qt.AlignVCenter,
            clock,
        )
        pill_font = units.font(1.25, QFont.Weight.Bold)
        pill_w = 0.0
        pill_text = "LIVE"
        if info.is_live:
            pill_w = QFontMetrics(pill_font).horizontalAdvance(pill_text) + 1.4 * u + 1 * u
        p.setFont(name_font)
        p.setPen(text)
        avail = bw - clock_w - 2 * u - pill_w
        name = elided(p, info.name, avail)
        name_w = QFontMetrics(name_font).horizontalAdvance(name)
        p.drawText(QRectF(bx, y, name_w + 2, lh(name_font)), Qt.AlignLeft | Qt.AlignVCenter, name)
        if info.is_live:
            pad_x, pad_y = 0.7 * u, 0.25 * u
            ph = lh(pill_font) + 2 * pad_y
            pw = QFontMetrics(pill_font).horizontalAdvance(pill_text) + 2 * pad_x
            py = y + (lh(name_font) - ph) / 2
            pr = QRectF(bx + name_w + 1 * u, py, pw, ph)
            fill_round(p, pr, qcolor(PALETTE.live), 0.5 * u)
            p.setFont(pill_font)
            p.setPen(QColor("white"))
            p.drawText(pr, Qt.AlignCenter, pill_text)
        y += lh(name_font) + gap1

        # now line
        if has_epg and info.now is not None:
            nw = QFontMetrics(now_font).horizontalAdvance("Now  ")
            p.setFont(now_font)
            p.setPen(accent)
            p.drawText(QRectF(bx, y, nw, lh(now_font)), Qt.AlignLeft | Qt.AlignVCenter, "Now")
            span = f" · {fmt_clock(info.now.start)} – {fmt_clock(info.now.stop)}"
            span_w = QFontMetrics(now_font).horizontalAdvance(span)
            p.setFont(now_bold)
            title = elided(p, info.now.title, bw - nw - span_w)
            title_w = QFontMetrics(now_bold).horizontalAdvance(title)
            p.setPen(text)
            p.drawText(
                QRectF(bx + nw, y, title_w + 2, lh(now_font)),
                Qt.AlignLeft | Qt.AlignVCenter,
                title,
            )
            p.setFont(now_font)
            p.drawText(
                QRectF(bx + nw + title_w, y, span_w + 2, lh(now_font)),
                Qt.AlignLeft | Qt.AlignVCenter,
                span,
            )
            y += lh(now_font) + gap2
            draw_progress(p, QRectF(bx, y, bw, bar_h), info.now.progress(info.clock))
            y += bar_h + gap3
            parts = [f"{fmt_clock(q.start)} {q.title}" for q in ([info.next] if info.next else [])]
            parts += [f"{fmt_clock(q.start)} {q.title}" for q in info.upcoming[:1]]
            if parts:
                p.setFont(next_font)
                p.setPen(qcolor(PALETTE.text, 0.7))
                line = "Next  " + "  ·  ".join(parts)
                p.drawText(
                    QRectF(bx, y, bw, lh(next_font)),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    elided(p, line, bw),
                )
        else:
            p.setFont(now_font)
            p.setPen(qcolor(PALETTE.text, 0.6))
            p.drawText(
                QRectF(bx, y, bw, lh(now_font)),
                Qt.AlignLeft | Qt.AlignVCenter,
                "No programme information",
            )
        p.end()

    def _paint_scrim(self, p: QPainter) -> None:
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0.0, QColor(5, 8, 12, 0))
        grad.setColorAt(0.45, QColor(5, 8, 12, round(0.55 * 255)))
        grad.setColorAt(1.0, QColor(5, 8, 12, round(0.92 * 255)))
        p.fillRect(self.rect(), grad)
