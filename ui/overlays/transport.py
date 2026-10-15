"""Transport overlay: title row on top, seek line and buttons at the bottom (VOD and plain live)."""

from datetime import datetime
import logging

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QLinearGradient, QMouseEvent, QPainter, QPaintEvent, QRegion
from PySide6.QtWidgets import QWidget

from ui.overlays.base import Overlay
from ui.overlays.painting import (
    Units,
    draw_icon,
    draw_progress,
    elided,
    fill_round,
    fmt_clock,
    fmt_duration,
)
from ui.theme import PALETTE, qcolor

logger = logging.getLogger(__name__)

SPEEDS = (1.0, 1.25, 1.5, 2.0)
TOP_FRACTION = 0.22
BOTTOM_FRACTION = 0.42


def _speed_label(rate: float) -> str:
    return f"{rate:g}x"


class Transport(Overlay):
    playPauseRequested = Signal()
    seekRequested = Signal(int)
    seekRelativeRequested = Signal(int)
    speedRequested = Signal(float)
    fullscreenRequested = Signal()
    audioRequested = Signal()
    subtitleRequested = Signal()

    def __init__(self, parent: QWidget | None = None, idle_ms: int = 3000) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._live = False
        self._title = ""
        self._subtitle = ""
        self._position = 0
        self._length = 0
        self._paused = False
        self._speed = 1.0
        self._drag_ms: int | None = None
        self._last_move = QPointF(-1, -1)
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(idle_ms)
        self._idle.timeout.connect(self.dismiss)
        self.hide()

    # ---- public API ---------------------------------------------------------------------------

    def show_vod(self, title: str, subtitle: str) -> None:
        self._set_mode(False, title, subtitle)

    def show_live(self, title: str, subtitle: str) -> None:
        self._set_mode(True, title, subtitle)

    def update_position(self, position_ms: int, length_ms: int) -> None:
        self._position = max(0, position_ms)
        self._length = max(0, length_ms)
        self.update()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.update()

    def set_speed(self, rate: float) -> None:
        self._speed = rate
        self.update()

    def poke(self) -> None:
        self.show()
        self.update()
        self._idle.start()

    def dismiss(self) -> None:
        self._idle.stop()
        self._drag_ms = None
        self.hide()

    def texts(self) -> tuple[str, str]:
        if self._live:
            return ("LIVE", "")
        shown = self._position if self._drag_ms is None else self._drag_ms
        return (fmt_duration(shown), "-" + fmt_duration(max(0, self._length - shown)))

    def mask_region(self) -> QRegion:
        top_h = round(self.height() * TOP_FRACTION)
        bottom_h = round(self.height() * BOTTOM_FRACTION)
        region = QRegion(QRect(0, 0, self.width(), top_h))
        region += QRegion(QRect(0, self.height() - bottom_h, self.width(), bottom_h))
        return region.translated(self.geometry().topLeft())

    # ---- geometry -----------------------------------------------------------------------------

    def _layout(self) -> dict[str, QRectF]:
        un = Units(self.width())
        left, right = un.px(3), self.width() - un.px(3)
        bottom = self.height() - un.px(3)
        icon = un.px(2.2)
        big = un.px(4.6)
        row_top = bottom - big
        rects: dict[str, QRectF] = {}
        if not self._live:
            seek_h = un.px(1.9)
            seek_y = row_top - un.px(1.2) - un.px(1.6) - seek_h
            rects["seek"] = QRectF(left, seek_y, right - left, seek_h)
            rects["times"] = QRectF(left, seek_y + seek_h, right - left, un.px(1.6))
        cy = row_top + big / 2
        x = float(left)

        def put(name: str, size: float, gap: float) -> None:
            nonlocal x
            rects[name] = QRectF(x, cy - size / 2, size, size)
            x += size + gap

        if self._live:
            put("play", big, un.px(1.5))
            x = float(right)
            for name in ("fullscreen", "subtitles", "audio"):
                x -= icon
                rects[name] = QRectF(x, cy - icon / 2, icon, icon)
                x -= un.px(1.8)
            pill_w = un.px(5.2)
            rects["live"] = QRectF(x - pill_w - un.px(0.4), cy - un.px(1.1), pill_w, un.px(2.2))
        else:
            put("back10", icon + un.px(1.6), un.px(0.6))
            put("play", big, un.px(0.6))
            put("fwd10", icon + un.px(1.6), un.px(1.0))
            chip_w = un.px(3.6)
            rects["speed"] = QRectF(x, cy - un.px(1.1), chip_w, un.px(2.2))
            x = float(right)
            for name in ("fullscreen", "audio", "subtitles"):
                x -= icon
                rects[name] = QRectF(x, cy - icon / 2, icon, icon)
                x -= un.px(1.8)
        return rects

    def seek_rect(self) -> QRectF:
        return self._layout()["seek"]

    def button_rect(self, name: str) -> QRectF:
        return self._layout()[name]

    # ---- painting -----------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        un = Units(self.width())
        self._paint_scrims(p)
        self._paint_title(p, un)
        rects = self._layout()
        text = qcolor(PALETTE.text)
        if self._live:
            self._paint_live(p, un, rects, text)
        else:
            self._paint_vod(p, un, rects, text)
        for name in ("subtitles", "audio", "fullscreen"):
            draw_icon(p, name, rects[name], text)
        p.end()

    def _paint_scrims(self, p: QPainter) -> None:
        top_h = self.height() * TOP_FRACTION
        grad = QLinearGradient(0, 0, 0, top_h)
        grad.setColorAt(0, qcolor("#000000", 0.62))
        grad.setColorAt(1, qcolor("#000000", 0.0))
        p.fillRect(QRectF(0, 0, self.width(), top_h), grad)
        bottom_h = self.height() * BOTTOM_FRACTION
        y = self.height() - bottom_h
        grad = QLinearGradient(0, self.height(), 0, y)
        grad.setColorAt(0, qcolor("#000000", 0.78))
        grad.setColorAt(1, qcolor("#000000", 0.0))
        p.fillRect(QRectF(0, y, self.width(), bottom_h), grad)

    def _paint_title(self, p: QPainter, un: Units) -> None:
        left = un.px(3)
        right = self.width() - un.px(3)
        y = un.px(2.4)
        row_h = un.px(2.6)
        clock = fmt_clock(datetime.now())
        p.setFont(un.font(1.8, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.text))
        clock_w = p.fontMetrics().horizontalAdvance(clock)
        p.drawText(
            QRectF(right - clock_w, y, clock_w, row_h), Qt.AlignVCenter | Qt.AlignRight, clock
        )
        p.setFont(un.font(1.8, QFont.Weight.Bold))
        title = elided(p, self._title, right - left - clock_w - un.px(4))
        title_w = p.fontMetrics().horizontalAdvance(title)
        p.drawText(QRectF(left, y, title_w + 2, row_h), Qt.AlignVCenter | Qt.AlignLeft, title)
        if self._subtitle:
            p.setFont(un.font(1.5))
            p.setPen(qcolor(PALETTE.text, 0.75))
            sx = left + title_w + un.px(1.2)
            sub = elided(p, self._subtitle, right - clock_w - un.px(2) - sx)
            p.drawText(QRectF(sx, y, right - sx, row_h), Qt.AlignVCenter | Qt.AlignLeft, sub)

    def _paint_vod(self, p: QPainter, un: Units, rects: dict[str, QRectF], text) -> None:
        seek = rects["seek"]
        shown = self._position if self._drag_ms is None else self._drag_ms
        frac = shown / self._length if self._length else 0.0
        frac = min(1.0, max(0.0, frac))
        track = QRectF(seek.x(), seek.center().y() - un.px(0.55) / 2, seek.width(), un.px(0.55))
        draw_progress(p, track, frac, track_alpha=0.14)
        knob_d = un.px(1.7)
        kc = QPointF(seek.x() + seek.width() * frac, seek.center().y())
        ring_d = knob_d + 2 * un.px(0.35)
        fill_round(
            p,
            QRectF(kc.x() - ring_d / 2, kc.y() - ring_d / 2, ring_d, ring_d),
            qcolor(PALETTE.accent, 0.45),
            ring_d / 2,
        )
        fill_round(
            p,
            QRectF(kc.x() - knob_d / 2, kc.y() - knob_d / 2, knob_d, knob_d),
            qcolor("#ffffff"),
            knob_d / 2,
        )
        elapsed, remaining = self.texts()
        p.setFont(un.font(1.25))
        p.setPen(qcolor(PALETTE.text, 0.8))
        times = rects["times"]
        p.drawText(times, Qt.AlignVCenter | Qt.AlignLeft, elapsed)
        p.drawText(times, Qt.AlignVCenter | Qt.AlignRight, remaining)
        dark = qcolor(PALETTE.canvas)
        for name in ("back10", "fwd10"):
            r = rects[name]
            side = un.px(2.2)
            draw_icon(
                p,
                name,
                QRectF(r.center().x() - side / 2, r.center().y() - side / 2, side, side),
                text,
            )
        self._paint_play(p, rects["play"], dark)
        chip = rects["speed"]
        fill_round(p, chip, qcolor(PALETTE.text, 0.14), chip.height() / 2)
        p.setFont(un.font(1.2, QFont.Weight.DemiBold))
        p.setPen(text)
        p.drawText(chip, Qt.AlignCenter, _speed_label(self._speed))

    def _paint_play(self, p: QPainter, rect: QRectF, dark) -> None:
        fill_round(p, rect, qcolor(PALETTE.text), rect.width() / 2)
        inner = rect.width() * 0.5
        draw_icon(
            p,
            "play" if self._paused else "pause",
            QRectF(
                rect.center().x() - inner / 2 + (rect.width() * 0.03 if self._paused else 0),
                rect.center().y() - inner / 2,
                inner,
                inner,
            ),
            dark,
        )

    def _paint_live(self, p: QPainter, un: Units, rects: dict[str, QRectF], text) -> None:
        self._paint_play(p, rects["play"], qcolor(PALETTE.canvas))
        pill = rects["live"]
        fill_round(p, pill, qcolor(PALETTE.live), pill.height() / 2)
        p.setFont(un.font(1.15, QFont.Weight.Bold))
        p.setPen(qcolor("#ffffff"))
        p.drawText(pill, Qt.AlignCenter, "LIVE")

    # ---- input --------------------------------------------------------------------------------

    def _ms_at(self, x: float) -> int:
        seek = self.seek_rect()
        frac = min(1.0, max(0.0, (x - seek.x()) / seek.width()))
        return round(frac * self._length)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.poke()
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        rects = self._layout()
        seek = rects.get("seek")
        if seek is not None and self._length > 0:
            hit = seek.adjusted(0, -seek.height(), 0, seek.height())
            if hit.contains(pos):
                self._drag_ms = self._ms_at(pos.x())
                self.update()
                return
        self._click(rects, pos)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if pos != self._last_move:  # ignore synthetic moves from layout changes
            self._last_move = pos
            self.poke()
        if self._drag_ms is not None:
            self._drag_ms = self._ms_at(event.position().x())
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._drag_ms is not None:
            target = self._ms_at(event.position().x())
            self._drag_ms = None
            self.update()
            self.seekRequested.emit(target)

    def _click(self, rects: dict[str, QRectF], pos: QPointF) -> None:
        for name, rect in rects.items():
            if not rect.contains(pos):
                continue
            if name == "play":
                self.playPauseRequested.emit()
            elif name == "back10":
                self.seekRelativeRequested.emit(-10_000)
            elif name == "fwd10":
                self.seekRelativeRequested.emit(10_000)
            elif name == "speed":
                nxt = SPEEDS[(self._nearest_speed() + 1) % len(SPEEDS)]
                self._speed = nxt
                self.update()
                self.speedRequested.emit(nxt)
            elif name == "fullscreen":
                self.fullscreenRequested.emit()
            elif name == "audio":
                self.audioRequested.emit()
            elif name == "subtitles":
                self.subtitleRequested.emit()
            return

    def _nearest_speed(self) -> int:
        return min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - self._speed))

    def _set_mode(self, live: bool, title: str, subtitle: str) -> None:
        self._live = live
        self._title = title
        self._subtitle = subtitle
        self._drag_ms = None
        if live:
            self._position = self._length = 0
        self.poke()
