"""EPG guide overlay (spec §5.1, R4): channels as rows, time as columns, with a now-line.

The grid paints only the rows that fit on screen, and looks up programmes only for those
rows, so groups with thousands of channels stay cheap.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, Signal
from PySide6.QtGui import (
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QTextLayout,
)
from PySide6.QtWidgets import QWidget

from services.epg_view import Programme
from services.navigation import item_key
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_logo, elided, fill_round, fmt_clock
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

logger = logging.getLogger(__name__)

SLOT = timedelta(minutes=30)
SLOTS = 5
WINDOW = SLOTS * SLOT  # 2.5 h on screen (R4)
MAX_AHEAD = timedelta(hours=24)
LOOKUP_LIMIT = 12
TOP_U = 1.0
HEADER_U = 4.0
ROW_U = 5.0
FOOTER_U = 5.0
CHANNEL_U = 22.0
SIDE_U = 1.6
TIME_DETAIL_U = 8.0  # boxes at least this wide show their time under the title
NO_INFO_TEXT = "No programme information"


def slot_start(moment: datetime) -> datetime:
    """The start of the local half-hour containing `moment` (aware in, aware out)."""
    local = moment.astimezone()
    return local.replace(minute=local.minute - local.minute % 30, second=0, microsecond=0)


def box_span(
    start: datetime, stop: datetime, window_start: datetime, left: float, width: float
) -> tuple[float, float] | None:
    """The x and width of a programme in a time area `width` px wide showing WINDOW.

    The span is clipped to the window; None when the programme is entirely outside it.
    """
    window_stop = window_start + WINDOW
    first, last = max(start, window_start), min(stop, window_stop)
    if last <= first:
        return None
    scale = width / WINDOW.total_seconds()
    return (
        left + (first - window_start).total_seconds() * scale,
        (last - first).total_seconds() * scale,
    )


def fmt_span(programme: Programme) -> str:
    return f"{fmt_clock(programme.start.astimezone())} – {fmt_clock(programme.stop.astimezone())}"


def wrap_lines(text: str, font: QFont, width: float, max_lines: int) -> list[str]:
    """Word-wrap `text` into at most `max_lines` lines; the last one is elided."""
    text = " ".join(text.split())
    if not text or width <= 0 or max_lines <= 0:
        return []
    layout = QTextLayout(text, font)
    layout.beginLayout()
    lines: list[str] = []
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        start = line.textStart()
        if len(lines) == max_lines - 1:
            rest = text[start:]
            lines.append(QFontMetrics(font).elidedText(rest, Qt.ElideRight, int(width)))
            break
        lines.append(text[start : start + line.textLength()].rstrip())
    layout.endLayout()
    return lines


@dataclass(frozen=True)
class _Layout:
    units: Units
    header: QRectF
    rows: QRectF  # the area the rows scroll in (full width)
    times: QRectF  # the time columns: x and width of the 2.5 h window
    footer: QRectF
    row_h: float

    def full_rows(self) -> int:
        return max(1, int(self.rows.height() // self.row_h))

    def painted_rows(self) -> int:
        return max(1, math.ceil(self.rows.height() / self.row_h))


class Guide(Overlay):
    tuneRequested = Signal(object)  # channel dict
    closed = Signal()

    def __init__(
        self,
        logos,
        programmes: Callable[[dict, datetime, int], list[Programme]],
        now: Callable[[], datetime],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._logos = logos
        self._programmes = programmes
        self._now = now
        self._channels: list[dict] = []
        self._current = -1
        self._top = 0
        self._window_start = slot_start(now())
        self._cache: dict[tuple[str, datetime], list[Programme]] = {}
        self._requested: set[str] = set()
        if logos is not None:
            logos.logoReady.connect(self._on_logo_ready)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API -------------------------------------------------------------------------

    def open_guide(self, channels: list[dict], current: int) -> None:
        self._channels = list(channels)
        self._current = min(max(current, 0), len(self._channels) - 1)
        self._window_start = slot_start(self._now())
        self._cache.clear()
        self._requested.clear()  # a reopened guide retries logos that failed
        self._relayout()
        full = self._layout().full_rows()
        self._top = self._clamped_top(self._current - full // 2, full)
        self.show()
        self.raise_()
        self.update()

    def close_guide(self) -> None:
        was_open = not self.isHidden()
        self.hide()
        if was_open:
            self.closed.emit()

    def move_by(self, delta: int) -> None:
        if not self._channels:
            return
        row = min(max(self._current + delta, 0), len(self._channels) - 1)
        if row != self._current:
            self._current = row
            self._ensure_visible()
            self.update()

    def page(self, delta: int) -> None:
        self.move_by(delta * max(1, self._layout().full_rows() - 1))

    def shift(self, delta: int) -> None:
        """Move the window by `delta` half-hours, between the current one and a day ahead."""
        earliest = slot_start(self._now())
        target = min(max(self._window_start + delta * SLOT, earliest), earliest + MAX_AHEAD)
        if target != self._window_start:
            self._window_start = target
            self._cache.clear()
            self.update()

    def activate(self) -> None:
        channel = self.current_channel()
        if channel is not None:
            self.tuneRequested.emit(channel)

    def escape(self) -> bool:
        self.close_guide()
        return True

    def current_channel(self) -> dict | None:
        if 0 <= self._current < len(self._channels):
            return self._channels[self._current]
        return None

    def window_start(self) -> datetime:
        return self._window_start

    # ---- state ------------------------------------------------------------------------------

    def _clamped_top(self, top: int, full: int) -> int:
        return min(max(top, 0), max(0, len(self._channels) - full))

    def _ensure_visible(self) -> None:
        full = self._layout().full_rows()
        top = self._top
        if self._current < top:
            top = self._current
        elif self._current >= top + full:
            top = self._current - full + 1
        self._top = self._clamped_top(top, full)

    def _visible_rows(self, layout: _Layout) -> range:
        return range(self._top, min(len(self._channels), self._top + layout.painted_rows()))

    def _programmes_for(self, row: int) -> list[Programme]:
        """The row's programmes for the current window; looked up once per channel and window."""
        channel = self._channels[row]
        key = (item_key(channel), self._window_start)
        cached = self._cache.get(key)
        if cached is None:
            try:
                cached = list(self._programmes(channel, self._window_start, LOOKUP_LIMIT))
            except Exception:
                logger.exception("Guide lookup failed for %s", channel.get("name"))
                cached = []
            self._cache[key] = cached
        return cached

    def _footer_programme(self, now: datetime) -> Programme | None:
        if self.current_channel() is None:
            return None
        programmes = self._programmes_for(self._current)
        on_now = next((p for p in programmes if p.is_on(now)), None)
        return on_now or next((p for p in programmes if p.start >= now), None)

    def _on_logo_ready(self, url: str) -> None:
        if self.isHidden():
            return
        rows = self._visible_rows(self._layout())
        if any(self._channels[row].get("logo") == url for row in rows):
            self.update()

    def _request_logos(self, rows: range) -> None:
        if self._logos is None:
            return
        urls = []
        for row in rows:
            url = self._channels[row].get("logo") or ""
            if url and url not in self._requested:
                self._requested.add(url)
                urls.append(url)
        if urls:
            self._logos.request(urls)

    # ---- layout -----------------------------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._relayout()
        return False

    def _relayout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.setGeometry(parent.rect())
        if self._channels:
            self._ensure_visible()
        self.update()

    def _layout(self) -> _Layout:
        units = Units(self.width())
        u = units.u
        width, height = float(self.width()), float(self.height())
        header = QRectF(0, TOP_U * u, width, HEADER_U * u)
        footer = QRectF(0, height - FOOTER_U * u, width, FOOTER_U * u)
        rows = QRectF(0, header.bottom(), width, max(0.0, footer.top() - header.bottom()))
        left = CHANNEL_U * u
        times = QRectF(left, rows.top(), max(0.0, width - SIDE_U * u - left), rows.height())
        return _Layout(units, header, rows, times, footer, ROW_U * u)

    # ---- painting ---------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        layout = self._layout()
        now = self._now().astimezone()  # aware, like the programme and window times
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(self.rect(), qcolor(PALETTE.panel, 0.94))
        self._paint_header(p, layout, now)
        rows = self._visible_rows(layout)
        self._request_logos(rows)
        p.save()
        p.setClipRect(layout.rows)
        y = layout.rows.top()
        for row in rows:
            self._paint_row(p, layout, row, QRectF(0, y, layout.rows.width(), layout.row_h), now)
            y += layout.row_h
        self._paint_now_line(p, layout, now, min(y, layout.rows.bottom()))
        p.restore()
        self._paint_footer(p, layout, now)
        p.end()

    def _now_x(self, layout: _Layout, now: datetime) -> float | None:
        offset = (now - self._window_start).total_seconds()
        if not 0 <= offset < WINDOW.total_seconds():
            return None
        return layout.times.left() + offset * layout.times.width() / WINDOW.total_seconds()

    def _day_label(self, now: datetime) -> str:
        start = self._window_start.astimezone()
        days = (start.date() - now.astimezone().date()).days
        return {0: "Today", 1: "Tomorrow"}.get(days, start.strftime("%A"))

    def _paint_header(self, p: QPainter, layout: _Layout, now: datetime) -> None:
        units, header, times = layout.units, layout.header, layout.times
        u = units.u
        label_top, label_h = header.top() + 0.4 * u, 1.8 * u
        p.setFont(units.font(1.1, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.text, 0.85))
        day = QRectF(SIDE_U * u, label_top, times.left() - 2 * SIDE_U * u, label_h)
        p.drawText(
            day, Qt.AlignLeft | Qt.AlignVCenter, elided(p, self._day_label(now), day.width())
        )
        slot_w = times.width() / SLOTS
        p.setFont(units.font(1.0))
        for i in range(SLOTS):
            x = times.left() + i * slot_w
            p.fillRect(
                QRectF(x, header.top() + 0.5 * u, 1, header.height() - 0.5 * u),
                qcolor(PALETTE.text, 0.18),
            )
            p.setPen(qcolor(PALETTE.text, 0.6))
            label = fmt_clock((self._window_start + i * SLOT).astimezone())
            p.drawText(
                QRectF(x + 0.6 * u, label_top, slot_w - 0.8 * u, label_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )
        now_x = self._now_x(layout, now)
        if now_x is not None:
            font = units.font(0.85, QFont.Weight.Bold)
            p.setFont(font)
            p.setPen(qcolor(PALETTE.accent))
            tip = QFontMetrics(font).horizontalAdvance("▼")
            p.drawText(
                QRectF(now_x - tip / 2, header.bottom() - 1.6 * u, 8 * u, 1.4 * u),
                Qt.AlignLeft | Qt.AlignVCenter,
                "▼ now",
            )

    def _paint_row(
        self, p: QPainter, layout: _Layout, row: int, rect: QRectF, now: datetime
    ) -> None:
        units, times = layout.units, layout.times
        u = units.u
        channel = self._channels[row]
        p.fillRect(
            QRectF(rect.left(), rect.bottom() - 1, rect.width(), 1), qcolor(PALETTE.text, 0.06)
        )
        if row == self._current:
            box = rect.adjusted(0.6 * u, 0.1 * u, -0.6 * u, -0.1 * u)
            fill_round(p, box, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), 1.0 * u)
            p.save()
            p.setPen(QPen(qcolor(PALETTE.accent, 0.8), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box.adjusted(0.75, 0.75, -0.75, -0.75), 1.0 * u, 1.0 * u)
            p.restore()
        self._paint_channel(p, layout, channel, QRectF(0, rect.top(), times.left(), rect.height()))
        drawn = False
        for programme in self._programmes_for(row):
            span = box_span(
                programme.start, programme.stop, self._window_start, times.left(), times.width()
            )
            if span is None:
                continue
            box = QRectF(span[0], rect.top(), span[1], rect.height()).adjusted(
                0.2 * u, 0.2 * u, -0.2 * u, -0.2 * u
            )
            if box.width() >= 1:
                self._paint_box(p, units, programme, box, programme.is_on(now))
                drawn = True
        if not drawn:
            box = QRectF(times.left(), rect.top(), times.width(), rect.height()).adjusted(
                0.2 * u, 0.2 * u, -0.2 * u, -0.2 * u
            )
            fill_round(p, box, qcolor(PALETTE.text, 0.03), 0.6 * u)
            p.setFont(units.font(1.0))
            p.setPen(qcolor(PALETTE.text, 0.45))
            inner = box.adjusted(0.9 * u, 0, -0.9 * u, 0)
            p.drawText(
                inner, Qt.AlignLeft | Qt.AlignVCenter, elided(p, NO_INFO_TEXT, inner.width())
            )

    def _paint_channel(self, p: QPainter, layout: _Layout, channel: dict, rect: QRectF) -> None:
        units = layout.units
        u = units.u
        x = rect.left() + SIDE_U * u
        number = channel.get("number")
        num_w = 3.4 * u
        if number is not None and str(number).strip():
            p.setFont(units.font(1.1, QFont.Weight.DemiBold))
            p.setPen(qcolor(PALETTE.accent))
            p.drawText(
                QRectF(x, rect.top(), num_w, rect.height()),
                Qt.AlignRight | Qt.AlignVCenter,
                elided(p, str(number).strip(), num_w),
            )
        x += num_w + 0.9 * u
        name = str(channel.get("name") or "")
        size = 3.0 * u
        url = channel.get("logo") or ""
        pixmap = self._logos.pixmap(url) if url and self._logos is not None else None
        draw_logo(
            p, QRectF(x, rect.top() + (rect.height() - size) / 2, size, size), name, pixmap, 0.7 * u
        )
        x += size + 0.9 * u
        width = rect.right() - 0.8 * u - x
        p.setFont(units.font(1.1, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.text))
        p.drawText(
            QRectF(x, rect.top(), max(0.0, width), rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            elided(p, name, width),
        )

    def _paint_box(
        self, p: QPainter, units: Units, programme: Programme, box: QRectF, on_now: bool
    ) -> None:
        u = units.u
        radius = 0.6 * u
        fill_round(p, box, qcolor(PALETTE.text, 0.1 if on_now else 0.06), radius)
        left = box.left()
        if on_now:
            p.save()
            path = QPainterPath()
            path.addRoundedRect(box, radius, radius)
            p.setClipPath(path, Qt.IntersectClip)
            p.fillRect(QRectF(box.left(), box.top(), 0.2 * u, box.height()), qcolor(PALETTE.accent))
            p.restore()
            left += 0.2 * u
        inner = QRectF(left + 0.7 * u, box.top(), box.right() - left - 1.4 * u, box.height())
        if inner.width() < 1.0 * u:
            return
        title_font = units.font(1.05, QFont.Weight.DemiBold if on_now else QFont.Weight.Normal)
        title_h = QFontMetrics(title_font).height()
        show_time = box.width() >= TIME_DETAIL_U * u
        time_font = units.font(0.85)
        time_h = QFontMetrics(time_font).height() if show_time else 0
        y = inner.top() + (inner.height() - title_h - time_h) / 2
        p.setFont(title_font)
        p.setPen(qcolor(PALETTE.text))
        p.drawText(
            QRectF(inner.left(), y, inner.width(), title_h),
            Qt.AlignLeft | Qt.AlignVCenter,
            elided(p, programme.title, inner.width()),
        )
        if show_time:
            p.setFont(time_font)
            p.setPen(qcolor(PALETTE.text, 0.55))
            p.drawText(
                QRectF(inner.left(), y + title_h, inner.width(), time_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                elided(p, fmt_span(programme), inner.width()),
            )

    def _paint_now_line(self, p: QPainter, layout: _Layout, now: datetime, bottom: float) -> None:
        now_x = self._now_x(layout, now)
        if now_x is None or bottom <= layout.rows.top():
            return
        width = 0.2 * layout.units.u
        p.fillRect(
            QRectF(now_x - width / 2, layout.rows.top(), width, bottom - layout.rows.top()),
            qcolor(PALETTE.accent),
        )

    def _paint_footer(self, p: QPainter, layout: _Layout, now: datetime) -> None:
        units, footer = layout.units, layout.footer
        u = units.u
        p.fillRect(footer, qcolor(PALETTE.panel_raised, 0.96))
        p.fillRect(
            QRectF(footer.left(), footer.top(), footer.width(), 1), qcolor(PALETTE.text, 0.08)
        )
        programme = self._footer_programme(now)
        left, width = SIDE_U * u, footer.width() - 2 * SIDE_U * u
        if programme is None:
            if self.current_channel() is not None:
                p.setFont(units.font(1.0))
                p.setPen(qcolor(PALETTE.text, 0.45))
                p.drawText(
                    QRectF(left, footer.top(), width, footer.height()),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    NO_INFO_TEXT,
                )
            return
        title_font = units.font(1.2, QFont.Weight.Bold)
        body_font = units.font(1.0)  # the time range and the description
        title_h = QFontMetrics(title_font).height()
        body_h = QFontMetrics(body_font).height()
        lines = wrap_lines(programme.description, body_font, width, 2)
        gap = 0.2 * u
        total = title_h + (gap + body_h * len(lines) if lines else 0)
        y = footer.top() + (footer.height() - total) / 2
        span = "  " + fmt_span(programme)
        span_w = QFontMetrics(body_font).horizontalAdvance(span)
        p.setFont(title_font)
        p.setPen(qcolor(PALETTE.text))
        title = elided(p, programme.title, width - span_w)
        title_w = QFontMetrics(title_font).horizontalAdvance(title)
        p.drawText(QRectF(left, y, title_w + 2, title_h), Qt.AlignLeft | Qt.AlignVCenter, title)
        p.setFont(body_font)
        p.setPen(qcolor(PALETTE.text, 0.7))
        p.drawText(
            QRectF(left + title_w, y, span_w + 2, title_h), Qt.AlignLeft | Qt.AlignVCenter, span
        )
        y += title_h + gap
        for line in lines:
            p.drawText(QRectF(left, y, width, body_h), Qt.AlignLeft | Qt.AlignVCenter, line)
            y += body_h
