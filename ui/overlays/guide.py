"""EPG guide overlay (spec §5.1): channels as rows, time as columns, with a now-line.

The grid paints only the rows that fit on screen, and looks up programmes only for those
rows, so groups with thousands of channels stay cheap.

Its header has a button for each of its keys (every key has a control on screen): ‹ and ›
beside the day move the time window (← →, dimmed at its ends), and ⌕ (Search), ⛶ (Fullscreen)
and × (close, as G or Esc) sit at its right end. Each names its key when hovered.

Catch-up (2.2): when a listed channel's provider keeps an archive (`archive_days(channel)`:
its days, 0 for none or one that did not answer lately), the window also moves back, as far
as the deepest archive listed (at most `MAX_BACK`). On the highlighted channel, a past
programme within its archive can be watched from its start (`programmeRequested`): the one
clicked on (a programme that has ended), else, with the window moved back from now, the one
at the window's left edge. It is outlined, the footer describes it, and its "▶ Watch from the
start" button and Enter (a double-click on its box) play it; the footer's button also shows
for the programme on now (from its start). Otherwise Enter tunes the channel, as before. On
an STB portal's channel only a programme its guide does not mark as not recorded can play.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
from typing import NamedTuple

from PySide6.QtCore import QDate, QEvent, QLocale, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QTextLayout,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from services import i18n
from services.catalog import shown_label
from services.catchup import ARCHIVE_KEY, programme_in_archive
from services.epg_view import Programme, now_next, timeline
from services.i18n import N_, tr, translate
from services.navigation import item_key
from ui.keymap import key_hint
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_icon, draw_logo, elided, fill_round, fmt_clock
from ui.pointer import PointerInput
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

logger = logging.getLogger(__name__)

SLOT = timedelta(minutes=30)
SLOTS = 5
WINDOW = SLOTS * SLOT  # 2.5 h on screen
MAX_AHEAD = timedelta(hours=24)
MAX_BACK = timedelta(days=7)  # the furthest back the window goes (a channel's archive)
LOOKUP_LIMIT = 12
REPAINT_MS = 30_000  # keeps the now-line and the on-now boxes current while open
TOP_U = 1.0
HEADER_U = 4.0
ROW_U = 5.0
FOOTER_U = 5.0
CHANNEL_U = 22.0
SIDE_U = 1.6
MIN_BOX_U = 1.5  # narrower boxes (slivers at the window's edges) are not drawn
TIME_DETAIL_U = 8.0  # boxes at least this wide show their time under the title
NO_INFO_TEXT = N_("No programme information")  # i18n: a channel's guide is empty
# i18n: the guide's button: a past programme from the provider's archive, from its beginning
WATCH_TEXT = N_("Watch from the start")
BUTTON_U = 2.2  # the header's buttons: 28 px at the default 1280 px window
BUTTON_GAP_U = 0.6
# The header's buttons, left to right: what they do and their key (ui.keymap.SHORTCUTS);
# what they do is shown with translate().
HEADER_BUTTONS = {
    "earlier": (N_("Earlier"), "Left"),  # i18n: the guide's ‹ (tooltip): an earlier time
    "later": (N_("Later"), "Right"),  # i18n: the guide's › (tooltip): a later time
    "search": (N_("Search"), "Ctrl+F"),
    "fullscreen": (N_("Fullscreen"), "F"),
    "close": (N_("Close the guide"), "G"),
}
_BUTTON_ICONS = {"earlier": "chevron_left", "later": "chevron_right"}

# (channel key, row when the channel has no key, window start)
_Key = tuple[str, int | None, datetime]
# A box to label after the now-line is drawn: its rect, programme (None: no information)
# and whether it is on now.
_Box = tuple[QRectF, Programme | None, bool]


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


class Span(NamedTuple):
    programme: Programme  # as the channel's timeline has it (its stop cut by an overlap)
    x: float
    width: float


def programme_spans(
    programmes: list[Programme],
    window_start: datetime,
    left: float,
    width: float,
    min_width: float,
) -> list[Span]:
    """A row's boxes, on the channel's timeline (overlaps end where the next programme starts,
    as the details bar and the info line see them); slivers are dropped."""
    spans = []
    for programme in timeline(programmes):
        span = box_span(programme.start, programme.stop, window_start, left, width)
        if span is not None and span[1] >= min_width:
            spans.append(Span(programme, *span))
    return spans


def fmt_span(programme: Programme) -> str:
    return f"{fmt_clock(programme.start.astimezone())} – {fmt_clock(programme.stop.astimezone())}"


def day_label(window_start: datetime, now: datetime) -> str:
    """The window's local day: "Today", "Tomorrow" or its weekday."""
    start = window_start.astimezone()
    days = (start.date() - now.astimezone().date()).days
    if days == 0:
        return tr("Today")  # i18n: the guide's day, top left
    if days == 1:
        return tr("Tomorrow")  # i18n: the guide's day, top left
    day = QDate(start.year, start.month, start.day).dayOfWeek()
    return str(i18n.app_locale().dayName(day, QLocale.FormatType.LongFormat))


def footer_programme(programmes: list[Programme], now: datetime) -> Programme | None:
    """The programme on now (the grid's box on now), else the first one still to come."""
    on_now, upcoming = now_next(programmes, now)
    return on_now or upcoming


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


def _archive_depth(channel: dict) -> int:
    """The days of archive the loaders kept on a channel (0: none); read cheaply, for every
    listed channel (`services.catchup.archive_of` reads the rest)."""
    days = channel.get(ARCHIVE_KEY)
    return days if isinstance(days, int) and not isinstance(days, bool) and days > 0 else 0


class Guide(Overlay):
    tuneRequested = Signal(object)  # channel dict
    programmeRequested = Signal(object, object)  # channel dict, Programme: from its start
    closed = Signal()
    searchRequested = Signal()  # the header's ⌕
    fullscreenRequested = Signal()  # the header's ⛶
    rowsMoved = Signal()  # other rows are on screen now (opened, scrolled)

    def __init__(
        self,
        logos,
        programmes: Callable[[dict, datetime, int], list[Programme]],
        now: Callable[[], datetime],
        parent: QWidget | None = None,
        archive_days: Callable[[dict], int] = lambda channel: 0,
        archive_reach: Callable[[dict], timedelta] | None = None,
    ) -> None:
        """`archive_days(channel)`: the days of the channel's archive that may play now (0:
        none); `archive_reach(channel)` (2.6.2): how far back its past programmes play, its
        own server window's included (`archive_days` when not given)."""
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._logos = logos
        self._programmes = programmes
        self._now = now
        self._channels: list[dict] = []
        self._digits = 0  # the longest channel number listed
        self._current = -1
        self._top = 0
        self._archive_days = archive_days
        self._archive_reach = archive_reach or (
            lambda channel: timedelta(days=archive_days(channel))
        )
        self._back = timedelta(0)  # how far back the window goes (the deepest archive listed)
        self._picked: tuple[int, Programme] | None = None  # a past programme clicked on
        self._window_start = slot_start(now())
        self._cache: dict[_Key, list[Programme]] = {}
        self._failed: set[_Key] = set()  # lookups that raised: logged once, retried
        self._requested: set[str] = set()
        self._repaint = QTimer(self)
        self._repaint.setInterval(REPAINT_MS)
        self._repaint.timeout.connect(self.update)
        self._row_wheel = PointerInput(self)
        self._row_wheel.wheelSteps.connect(self._on_row_wheel)
        self._time_wheel = PointerInput(self)
        self._time_wheel.wheelSteps.connect(self._on_time_wheel)
        if logos is not None:
            logos.logoReady.connect(self._on_logo_ready)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API -------------------------------------------------------------------------

    def open_guide(self, channels: list[dict], current: int) -> None:
        self._channels = list(channels)
        # The number column fits the longest number listed (a provider's own run past 9999).
        self._digits = max(
            (len(str(c.get("number") or "").strip()) for c in self._channels), default=0
        )
        self._current = min(max(current, 0), len(self._channels) - 1)
        self._window_start = slot_start(self._now())
        self._picked = None
        deepest = max((_archive_depth(c) for c in self._channels), default=0)
        windows = max((self._archive_reach(c) for c in self._channels), default=timedelta(0))
        self._back = min(MAX_BACK, max(timedelta(days=deepest), windows))
        self._forget_lookups()
        self._requested.clear()  # a reopened guide retries logos that failed
        self._relayout()
        full = self._layout().full_rows()
        self._top = self._clamped_top(self._current - full // 2, full)
        self._repaint.start()
        self.show()
        self.raise_()
        self.update()
        self.rowsMoved.emit()

    def close_guide(self) -> None:
        self._repaint.stop()
        was_open = not self.isHidden()
        self.hide()
        if was_open:
            self.closed.emit()

    def invalidate(self) -> None:
        """Look the visible rows up again, e.g. after the EPG was refreshed."""
        self._forget_lookups()
        self.update()

    def move_by(self, delta: int) -> None:
        if not self._channels:
            return
        self._set_current(min(max(self._current + delta, 0), len(self._channels) - 1))

    def page(self, delta: int) -> None:
        self.move_by(delta * max(1, self._layout().full_rows() - 1))

    def shift(self, delta: int) -> None:
        """Move the window by `delta` half-hours, between the current one and a day ahead."""
        target = self._shifted(delta)
        if target != self._window_start:
            self._window_start = target
            self._picked = None
            self._forget_lookups()
            self.update()

    def _shifted(self, delta: int) -> datetime:
        now = slot_start(self._now())
        earliest = now - self._back
        return min(max(self._window_start + delta * SLOT, earliest), now + MAX_AHEAD)

    def header_buttons(self) -> dict[str, QRectF]:
        """The header's buttons, left to right: ‹ › at the right of the channels' column, then
        ⌕ ⛶ × at the header's right end."""
        layout = self._layout()
        u = layout.units.u
        size, gap = BUTTON_U * u, BUTTON_GAP_U * u
        top = layout.header.top() + 1.3 * u - size / 2  # centred on the day's line
        later = QRectF(layout.times.left() - SIDE_U * u - size, top, size, size)
        earlier = QRectF(later.left() - gap - size, top, size, size)
        close = QRectF(layout.times.right() - size, top, size, size)
        fullscreen = QRectF(close.left() - gap - size, top, size, size)
        search = QRectF(fullscreen.left() - gap - size, top, size, size)
        return {
            "earlier": earlier,
            "later": later,
            "search": search,
            "fullscreen": fullscreen,
            "close": close,
        }

    def tooltip_at(self, point: QPointF) -> str:
        """What hovering `point` says: a header button's name and key, or nothing."""
        for name, rect in self.header_buttons().items():
            if rect.contains(point):
                what, keys = HEADER_BUTTONS[name]
                return key_hint(translate(what), keys)
        if self.watch_button().contains(point):
            what = translate(WATCH_TEXT)
            return key_hint(what, "Return") if self.chosen() is not None else what
        return ""

    def activate(self) -> None:
        """Enter: the chosen past programme from its start (see `chosen`), else the channel."""
        channel = self.current_channel()
        if channel is None:
            return
        programme = self.chosen()
        if programme is not None:
            self.programmeRequested.emit(channel, programme)
        else:
            self.tuneRequested.emit(channel)

    # ---- catch-up (2.2) ---------------------------------------------------------------------

    def _playable(self, programme: Programme | None, channel: dict | None) -> bool:
        """`programme` began in the past, within the channel's archive (an STB portal's: one
        its guide lists as recorded, or says nothing of)."""
        if programme is None or channel is None:
            return False
        reach = self._archive_reach(channel)
        now = self._now().astimezone()
        return (
            reach > timedelta(0)
            and now - reach <= programme.start < now
            and programme_in_archive(channel, programme)
        )

    def chosen(self) -> Programme | None:
        """The past programme Enter plays from its start: the one clicked on in the highlighted
        row, else, with the window moved back from now, the one at its left edge; None when
        there is none, or the channel's archive doesn't reach it."""
        channel = self.current_channel()
        if channel is None:
            return None
        programme: Programme | None = None
        if self._picked is not None and self._picked[0] == self._current:
            programme = self._picked[1]
        elif self._window_start < slot_start(self._now()):
            programme = self._programme_at(self._current, self._window_start)
        return programme if self._playable(programme, channel) else None

    def _programme_at(self, row: int, moment: datetime) -> Programme | None:
        for programme in timeline(self._programmes_for(row)):
            if programme.is_on(moment):
                return programme
        return None

    def _footer_choice(self) -> tuple[Programme | None, bool]:
        """The footer's programme and whether its "Watch from the start" button shows."""
        chosen = self.chosen()
        if chosen is not None:
            return chosen, True
        if self.current_channel() is None:
            return None, False
        now = self._now().astimezone()
        programme = footer_programme(self._programmes_for(self._current), now)
        on_now = programme is not None and programme.is_on(now)
        return programme, on_now and self._playable(programme, self.current_channel())

    def watch_button(self) -> QRectF:
        """The footer's "▶ Watch from the start" (empty when it doesn't show)."""
        if self.isHidden() or not self._footer_choice()[1]:
            return QRectF()
        return self._watch_rect(self._layout())

    def _watch_rect(self, layout: _Layout) -> QRectF:
        units, footer = layout.units, layout.footer
        u = units.u
        fm = QFontMetrics(units.font(1.05, QFont.Weight.DemiBold))
        width = fm.horizontalAdvance("▶  " + translate(WATCH_TEXT)) + 2.4 * u
        height = max(2.6 * u, fm.height() + 0.8 * u)
        right = footer.right() - SIDE_U * u
        return QRectF(right - width, footer.center().y() - height / 2, width, height)

    def _watch_chosen(self) -> None:
        """The footer's button: its programme from the start."""
        channel = self.current_channel()
        programme, shown = self._footer_choice()
        if channel is not None and programme is not None and shown:
            self.programmeRequested.emit(channel, programme)

    def _pick_at(self, row: int, x: float) -> None:
        """A click on `row` at `x`: a past programme there (within the archive) is chosen."""
        layout = self._layout()
        picked = None
        if layout.times.left() <= x <= layout.times.right():
            spans = programme_spans(
                self._programmes_for(row),
                self._window_start,
                layout.times.left(),
                layout.times.width(),
                MIN_BOX_U * layout.units.u,
            )
            for span in spans:
                ended = span.programme.stop <= self._now().astimezone()
                if span.x <= x < span.x + span.width and ended:
                    if self._playable(span.programme, self._channels[row]):
                        picked = (row, span.programme)
                    break
        if picked != self._picked:
            self._picked = picked
            self.update()

    def escape(self) -> bool:
        self.close_guide()
        return True

    def current_channel(self) -> dict | None:
        if 0 <= self._current < len(self._channels):
            return self._channels[self._current]
        return None

    def window_start(self) -> datetime:
        return self._window_start

    def visible_channels(self) -> list[dict]:
        """The channels of the rows on screen (their programmes are looked up)."""
        rows = self._visible_rows(self._layout())
        return self._channels[rows.start : rows.stop]

    # ---- state ------------------------------------------------------------------------------

    def _set_current(self, row: int) -> None:
        if row != self._current:
            old, top = self._current, self._top
            self._current = row
            self._picked = None
            self._ensure_visible()
            if self._top != top:
                self.update()  # the rows scrolled
                self.rowsMoved.emit()
                return
            # Only the two rows' highlight and the footer change: repaint just them.
            layout = self._layout()
            visible = self._visible_rows(layout)
            for changed in (old, row):
                if changed in visible:
                    self.update(self._row_rect(layout, changed).toAlignedRect())
            self.update(layout.footer.toAlignedRect())

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

    def _row_rect(self, layout: _Layout, row: int) -> QRectF:
        top = layout.rows.top() + (row - self._top) * layout.row_h
        return QRectF(0, top, layout.rows.width(), layout.row_h)

    def _forget_lookups(self) -> None:
        self._cache.clear()
        self._failed.clear()

    def _programmes_for(self, row: int) -> list[Programme]:
        """The row's programmes for the current window; looked up once per channel and window.

        A lookup that raises is not cached, so the next paint tries again.
        """
        channel = self._channels[row]
        ident = item_key(channel)
        key: _Key = (ident, None if ident else row, self._window_start)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            result = list(self._programmes(channel, self._window_start, LOOKUP_LIMIT))
        except Exception:
            if key in self._failed:
                logger.debug("Guide lookup failed again for %s", channel.get("name"))
            else:
                self._failed.add(key)
                logger.exception("Guide lookup failed for %s", channel.get("name"))
            return []
        self._cache[key] = result
        return result

    def _on_logo_ready(self, url: str) -> None:
        if self.isHidden():
            return
        layout = self._layout()
        for row in self._visible_rows(layout):
            if self._channels[row].get("logo") == url:
                self.update(self._row_rect(layout, row).toAlignedRect())

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

    # ---- mouse: consumed here, so it never reaches the video underneath -----------------

    def _row_at(self, y: float) -> int | None:
        layout = self._layout()
        if not layout.rows.top() <= y < layout.rows.bottom():
            return None
        row = self._top + int((y - layout.rows.top()) // layout.row_h)
        return row if row < len(self._channels) else None

    def _select_at(self, event: QMouseEvent) -> bool:
        row = self._row_at(event.position().y())
        if row is None:
            return False
        self._set_current(row)
        self._pick_at(row, event.position().x())
        return True

    def _button_at(self, event: QMouseEvent) -> str | None:
        pos = event.position()
        return next((n for n, r in self.header_buttons().items() if r.contains(pos)), None)

    def _press_button(self, name: str) -> None:
        if name in ("earlier", "later"):
            self.shift(-1 if name == "earlier" else 1)
        elif name == "search":
            self.searchRequested.emit()
        elif name == "fullscreen":
            self.fullscreenRequested.emit()
        else:
            self.close_guide()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            button = self._button_at(event)
            if button is not None:
                self._press_button(button)
            elif self.watch_button().contains(event.position()):
                self._watch_chosen()
            else:
                self._select_at(event)
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            button = self._button_at(event)
            if button in ("earlier", "later"):
                self._press_button(button)  # a quick second click moves the window again
            elif self.watch_button().contains(event.position()):
                pass  # the first press played it
            elif button is None and self._select_at(event):
                self.activate()
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta()
        if abs(delta.x()) > abs(delta.y()):
            self._time_wheel.wheel(delta.x(), event.modifiers())
        elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._time_wheel.wheel(delta.y(), event.modifiers())
        else:
            self._row_wheel.wheel(delta.y(), event.modifiers())
        event.accept()

    def _on_row_wheel(self, steps: int, _modifiers) -> None:
        self.move_by(steps)

    def _on_time_wheel(self, steps: int, _modifiers) -> None:
        self.shift(steps)

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
        # Only what the update region covers is painted (a move repaints two rows).
        dirty = event.region()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(self.rect(), qcolor(PALETTE.panel, 0.94))
        if dirty.intersects(layout.header.toAlignedRect()):
            self._paint_header(p, layout, now)
        rows = self._visible_rows(layout)
        self._request_logos(rows)
        p.save()
        p.setClipRect(layout.rows)
        y = layout.rows.top()
        boxes: list[_Box] = []
        for row in rows:
            rect = QRectF(0, y, layout.rows.width(), layout.row_h)
            if dirty.intersects(rect.toAlignedRect()):
                boxes += self._paint_row(p, layout, row, rect, now)
            y += layout.row_h
        # The line runs over the boxes but under their text, so titles stay readable.
        self._paint_now_line(p, layout, now, min(y, layout.rows.bottom()))
        for box, programme, on_now in boxes:
            self._paint_box_text(p, layout.units, box, programme, on_now)
        p.restore()
        if dirty.intersects(layout.footer.toAlignedRect()):
            self._paint_footer(p, layout, now)
        p.end()

    def _now_x(self, layout: _Layout, now: datetime) -> float | None:
        offset = (now - self._window_start).total_seconds()
        if not 0 <= offset < WINDOW.total_seconds():
            return None
        return layout.times.left() + offset * layout.times.width() / WINDOW.total_seconds()

    def _paint_header(self, p: QPainter, layout: _Layout, now: datetime) -> None:
        units, header, times = layout.units, layout.header, layout.times
        u = units.u
        buttons = self.header_buttons()
        label_top, label_h = header.top() + 0.4 * u, 1.8 * u
        p.setFont(units.font(1.1, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.text, 0.85))
        day_w = buttons["earlier"].left() - BUTTON_GAP_U * u - SIDE_U * u
        day = QRectF(SIDE_U * u, label_top, max(0.0, day_w), label_h)
        label = day_label(self._window_start, now)
        p.drawText(day, Qt.AlignLeft | Qt.AlignVCenter, elided(p, label, day.width()))
        slot_w = times.width() / SLOTS
        p.setFont(units.font(1.0))
        labels_end = buttons["search"].left() - BUTTON_GAP_U * u  # never under ⌕ ⛶ ×
        for i in range(SLOTS):
            x = times.left() + i * slot_w
            p.fillRect(
                QRectF(x, header.top() + 0.5 * u, 1, header.height() - 0.5 * u),
                qcolor(PALETTE.text, 0.18),
            )
            p.setPen(qcolor(PALETTE.text, 0.6))
            width = min(slot_w - 0.8 * u, labels_end - x - 0.6 * u)
            if width > 0:
                p.drawText(
                    QRectF(x + 0.6 * u, label_top, width, label_h),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    elided(p, fmt_clock((self._window_start + i * SLOT).astimezone()), width),
                )
        for name, rect in buttons.items():
            movable = {"earlier": -1, "later": 1}.get(name)
            dim = movable is not None and self._shifted(movable) == self._window_start
            draw_icon(
                p, _BUTTON_ICONS.get(name, name), rect, qcolor(PALETTE.text, 0.3 if dim else 0.85)
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
                "▼ " + tr("now"),  # i18n: the guide's now-line marker; short (~5 letters)
            )

    def _paint_row(
        self, p: QPainter, layout: _Layout, row: int, rect: QRectF, now: datetime
    ) -> list[_Box]:
        """Paint a row's backgrounds; return its boxes for labelling after the now-line."""
        units, times = layout.units, layout.times
        u = units.u
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
        self._paint_channel(
            p, layout, self._channels[row], QRectF(0, rect.top(), times.left(), rect.height())
        )
        programmes = self._programmes_for(row)
        start, stop = self._window_start, self._window_start + WINDOW
        if not any(prog.stop > start and prog.start < stop for prog in programmes):
            box = QRectF(times.left(), rect.top(), times.width(), rect.height()).adjusted(
                0.2 * u, 0.2 * u, -0.2 * u, -0.2 * u
            )
            fill_round(p, box, qcolor(PALETTE.text, 0.03), 0.6 * u)
            return [(box, None, False)]
        boxes: list[_Box] = []
        chosen = self.chosen() if row == self._current else None
        for span in programme_spans(programmes, start, times.left(), times.width(), MIN_BOX_U * u):
            box = QRectF(span.x, rect.top(), span.width, rect.height()).adjusted(
                0.2 * u, 0.2 * u, -0.2 * u, -0.2 * u
            )
            on_now = span.programme.is_on(now)
            self._paint_box_fill(p, units, box, on_now)
            if chosen is not None and span.programme == chosen:
                p.save()  # Enter plays it from its start: outlined
                p.setPen(QPen(qcolor(PALETTE.accent), max(1.5, 0.2 * u)))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(box.adjusted(1, 1, -1, -1), 0.6 * u, 0.6 * u)
                p.restore()
            boxes.append((box, span.programme, on_now))
        return boxes

    def _number_width(self, units: Units) -> float:
        """The channel number column: as wide as the longest number listed, at least 3.4u."""
        font = units.font(1.1, QFont.Weight.DemiBold)
        widest = QFontMetrics(font).horizontalAdvance("0" * self._digits) + 0.2 * units.u
        return max(3.4 * units.u, widest)

    def _paint_channel(self, p: QPainter, layout: _Layout, channel: dict, rect: QRectF) -> None:
        units = layout.units
        u = units.u
        x = rect.left() + SIDE_U * u
        number = channel.get("number")
        num_w = self._number_width(units)
        if number is not None and str(number).strip():
            p.setFont(units.font(1.1, QFont.Weight.DemiBold))
            p.setPen(qcolor(PALETTE.accent))
            p.drawText(
                QRectF(x, rect.top(), num_w, rect.height()),
                Qt.AlignRight | Qt.AlignVCenter,
                elided(p, str(number).strip(), num_w),
            )
        x += num_w + 0.9 * u
        name = shown_label(str(channel.get("name") or ""))  # "Stream 2" in the active language
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

    def _paint_box_fill(self, p: QPainter, units: Units, box: QRectF, on_now: bool) -> None:
        u = units.u
        radius = 0.6 * u
        fill_round(p, box, qcolor(PALETTE.text, 0.1 if on_now else 0.06), radius)
        if on_now:
            p.save()
            path = QPainterPath()
            path.addRoundedRect(box, radius, radius)
            p.setClipPath(path, Qt.IntersectClip)
            p.fillRect(QRectF(box.left(), box.top(), 0.2 * u, box.height()), qcolor(PALETTE.accent))
            p.restore()

    def _paint_box_text(
        self, p: QPainter, units: Units, box: QRectF, programme: Programme | None, on_now: bool
    ) -> None:
        u = units.u
        if programme is None:
            p.setFont(units.font(1.0))
            p.setPen(qcolor(PALETTE.text, 0.45))
            inner = box.adjusted(0.9 * u, 0, -0.9 * u, 0)
            p.drawText(
                inner,
                Qt.AlignLeft | Qt.AlignVCenter,
                elided(p, translate(NO_INFO_TEXT), inner.width()),
            )
            return
        left = box.left() + (0.2 * u if on_now else 0.0)
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

    def _paint_watch(self, p: QPainter, units: Units, button: QRectF) -> None:
        """The footer's "▶ Watch from the start" chip."""
        fill_round(p, button, qcolor(PALETTE.accent), button.height() / 2)
        p.setFont(units.font(1.05, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.canvas))
        p.drawText(button, Qt.AlignCenter, "▶  " + translate(WATCH_TEXT))

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
        if self.current_channel() is None:
            return
        programme, watch = self._footer_choice()
        left, width = SIDE_U * u, footer.width() - 2 * SIDE_U * u
        if watch:
            button = self._watch_rect(layout)
            width = max(0.0, button.left() - 1.2 * u - left)
            self._paint_watch(p, units, button)
        if programme is None:
            p.setFont(units.font(1.0))
            p.setPen(qcolor(PALETTE.text, 0.45))
            p.drawText(
                QRectF(left, footer.top(), width, footer.height()),
                Qt.AlignLeft | Qt.AlignVCenter,
                translate(NO_INFO_TEXT),
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
