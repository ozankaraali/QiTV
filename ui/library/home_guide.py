"""The Recordings home's guide (2.9.7): tonight's programmes to record, in the page.

Channels as rows, half hours as columns from half an hour before now (four hours shown), as the
TV's guide overlay draws them (its `programme_spans`), and a glowing NOW line. What records now
has a red outline, what is scheduled an accented one with ●, a clock timer a striped band over
its channel's row (`set_mark_provider`, `set_band_provider`); what has ended is dimmed and the
one on now lighter. Only the rows that show are asked for their programmes, once per window.

A click on a programme still to come (or the keys' Enter) opens a small card beside it: ● Record
(accented) and ↻ Every airing; on one scheduled, Skip and Cancel series (an airing of a series)
or Cancel (a one-time recording). R records the current programme. Above the rows: ‹ ›, the
days the window can show (Today, Tomorrow, then weekdays) and the channels' source chip (★
Favorites ▾), which asks the page for its menu (`sourceRequested`). A click on a channel's
name tunes it.

The window follows now (`follow_now`, and whenever the guide shows) until the viewer moves it
or picks another day. The keys reach everything the mouse does: ↑ from the first row goes to
the bar's chips (← → along them, Enter presses one), ← → move between an open card's buttons,
PgUp/PgDn move a screen of rows. A row with no programmes yet is asked again (a guide loading),
and a channel's programmes are its provider's (two providers' lists may share an id).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QWidget

from services.epg_view import Programme
from services.i18n import N_, translate
from services.provider_ids import provider_of
from ui.overlays.base import TooltipWidget
from ui.overlays.guide import day_label, programme_spans, slot_start
from ui.overlays.painting import Units, elided, fill_round, fmt_clock, ui_units
from ui.pointer import PointerInput
from ui.theme import PALETTE, qcolor

logger = logging.getLogger(__name__)

SLOTS = 8  # half hours shown
WINDOW = timedelta(minutes=30 * SLOTS)
DAYS = 4  # Today, Tomorrow and two more
LIMIT = 24  # programmes asked per row and window
CHANNEL_W_U = 14.0
ROW_H_U = 4.0
HEAD_H_U = 2.6  # the half hours' row
BAR_H_U = 3.2  # the days' and the source's chips
CARD_W_U = 22.0
BUTTON_H_U = 2.6
RADIUS_U = 0.7
PAST_ALPHA = 0.45

# i18n: the Recordings home's guide when it has no programmes for its channels
EMPTY_TEXT = N_(
    "No guide for these channels yet. Recording by the clock still works: ＋ At a time…"
)
# i18n: the Recordings home's guide card: record this programme (verb)
RECORD_TEXT = N_("● Record")
# i18n: the Recordings home's guide card: record every airing of this programme
SERIES_TEXT = N_("↻ Every airing")
# i18n: the Recordings home's guide card: this airing of a series isn't recorded (verb)
SKIP_TEXT = N_("Skip")
# i18n: the Recordings home's guide card: the series is taken off the schedule
CANCEL_SERIES_TEXT = N_("Cancel series")
# i18n: the Recordings home's guide card: a one-time recording is cancelled
CANCEL_TEXT = N_("Cancel recording")
_BUTTON_TEXTS = {
    "record": RECORD_TEXT,
    "series": SERIES_TEXT,
    "skip": SKIP_TEXT,
    "cancel_series": CANCEL_SERIES_TEXT,
    "cancel": CANCEL_TEXT,
}


class HomeGuide(TooltipWidget):
    recordRequested = Signal(object, object, bool)  # channel, programme, every airing
    skipRequested = Signal(object, object)  # channel, programme (an airing of a series)
    cancelRequested = Signal(object, object)  # channel, programme (a one-time recording)
    cancelSeriesRequested = Signal(object, object)  # channel, programme (its series goes)
    tuneRequested = Signal(object)  # a channel's name clicked
    sourceRequested = Signal(QPoint)  # the source chip: where its menu opens (global)
    upEdge = Signal()  # ↑ from the first row

    def __init__(
        self,
        programmes: Callable[[dict, datetime, int], list[Programme]],
        now: Callable[[], datetime],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)
        self._programmes = programmes
        self._now = now
        self._channels: list[dict] = []
        self._day = 0
        self._start = self._home_start()
        self._top = 0  # the first row shown
        self._row = 0
        self._current: Optional[Programme] = None  # the keys' programme in `_row`
        self._card: Optional[tuple[int, Programme]] = None
        self._cache: dict[tuple[str, str, float], list[Programme]] = {}
        self._mark: Callable[[dict, Programme], str] = lambda channel, programme: ""
        self._bands: Callable[[dict], list[tuple[float, float, str]]] = lambda channel: []
        self._source = ""
        self._focused = False
        self._shifted = False  # the viewer moved the window (else it follows now)
        self._bar = ""  # the keys' chip in the bar ("": the rows have them)
        self._card_button = 0  # the keys' button in the card
        self._row_wheel = PointerInput(self)  # a trackpad's scroll counts its distance
        self._row_wheel.wheelSteps.connect(self._on_row_wheel)
        self._time_wheel = PointerInput(self)
        self._time_wheel.wheelSteps.connect(self._on_time_wheel)

    # ---- what it shows ----------------------------------------------------------------------

    def set_channels(self, channels: list[dict]) -> None:
        self._channels = list(channels)
        self._top = self._row = 0
        self._card = None
        self._bar = ""
        self._cache.clear()
        self._current = self._on_now(0)
        self.update()

    def set_mark_provider(self, mark: Callable[[dict, Programme], str]) -> None:
        """`mark(channel, programme)`: "recording", "once", "series" (scheduled) or ""."""
        self._mark = mark
        self.update()

    def set_band_provider(self, bands: Callable[[dict], list[tuple[float, float, str]]]) -> None:
        """`bands(channel)`: its clock timers' windows (unix start, stop, name)."""
        self._bands = bands
        self.update()

    def set_source_label(self, text: str) -> None:
        self._source = text
        self.update()

    def set_focused(self, focused: bool) -> None:
        self._focused = focused
        self.update()

    def refresh(self) -> None:
        """The guide's data changed (a guide loaded): asked again."""
        self._cache.clear()
        self.update()

    def window_start(self) -> datetime:
        return self._start

    def shown_day(self) -> int:
        """The day chip lit: 0 today, 1 tomorrow…."""
        return self._day

    def _home_start(self) -> datetime:
        return slot_start(self._now()) - timedelta(minutes=30)

    def day_choices(self) -> list[tuple[str, int]]:
        now = self._now()
        return [(day_label(now + timedelta(days=k), now), k) for k in range(DAYS)]

    def set_day(self, day: int) -> None:
        """The window on day `day` (0: today, from half an hour before now; later days at the
        same time of day)."""
        self._day = max(0, min(DAYS - 1, day))
        self._start = self._home_start() + timedelta(days=self._day)
        self._shifted = False
        self._card = None
        self._current = self._on_now(self._row) or self._first(self._row)
        self.update()

    def shift(self, half_hours: int) -> None:
        earliest = self._home_start()
        latest = earliest + timedelta(days=DAYS) - WINDOW
        moved = self._start + timedelta(minutes=30 * half_hours)
        self._start = max(earliest, min(latest, moved))
        self._shifted = self._start != earliest
        # the day the window shows most of (just after midnight, earliest is yesterday's)
        middle = self._start + WINDOW / 2
        self._day = max(0, min(DAYS - 1, (middle.date() - self._now().date()).days))
        self._card = None
        shown = [span.programme for span in self._spans(self._row)]
        if self._current not in shown:
            self._current = shown[0] if shown else None
        self.update()

    def follow_now(self) -> None:
        """Today's window from half an hour before now, as time goes on; one the viewer moved
        (or another day) stays where they put it."""
        if self._shifted or self._day != 0:
            return
        start = self._home_start()
        if start == self._start:
            return
        self._start = start
        self._card = None
        self._cache.clear()
        self._current = self._on_now(self._row) or self._first(self._row)
        self.update()

    def showEvent(self, event) -> None:
        self.follow_now()
        super().showEvent(event)

    def empty_text(self) -> str:
        """The one line said instead of rows ("" when the rows have programmes)."""
        if not self._channels:
            return translate(EMPTY_TEXT)
        shown = range(self._top, min(len(self._channels), self._top + self._rows_fit()))
        if any(self._row_programmes(k) for k in shown):
            return ""
        return translate(EMPTY_TEXT)

    # ---- geometry ---------------------------------------------------------------------------

    def _units(self) -> Units:
        host = self.window()
        width = host.width() if host is not None and host.width() > 0 else self.width()
        return ui_units(max(1, width))

    def _bar_rect(self) -> QRectF:
        return QRectF(0, 0, self.width(), BAR_H_U * self._units().u)

    def _head_rect(self) -> QRectF:
        u = self._units().u
        return QRectF(0, self._bar_rect().bottom(), self.width(), HEAD_H_U * u)

    def time_area(self) -> QRectF:
        """The rows' time area: right of the channels' names, under the half hours."""
        u = self._units().u
        left = CHANNEL_W_U * u
        top = self._head_rect().bottom()
        return QRectF(left, top, max(1.0, self.width() - left), max(0.0, self.height() - top))

    def _row_h(self) -> float:
        return ROW_H_U * self._units().u

    def _rows_fit(self) -> int:
        return max(1, int(self.time_area().height() // max(1.0, self._row_h())))

    def rows_fit(self) -> int:
        return self._rows_fit()

    def top_row(self) -> int:
        return self._top

    def _row_top(self, row: int) -> float:
        return self.time_area().top() + (row - self._top) * self._row_h()

    def now_x(self) -> float:
        area = self.time_area()
        seconds = (self._now() - self._start).total_seconds()
        return area.left() + area.width() * seconds / WINDOW.total_seconds()

    def _x_at(self, moment: float) -> float:
        area = self.time_area()
        seconds = moment - self._start.timestamp()
        return area.left() + area.width() * seconds / WINDOW.total_seconds()

    def _spans(self, row: int) -> list[Any]:
        area = self.time_area()
        return programme_spans(
            self._row_programmes(row), self._start, area.left(), area.width(), 4.0, WINDOW
        )

    def box_rect(self, row: int, title: str) -> QRectF:
        """Where `title` is drawn in `row` (empty when it isn't)."""
        for span in self._spans(row):
            if span.programme.title == title:
                return self._box(row, span)
        return QRectF()

    def _box_of(self, row: int, programme: Programme) -> QRectF:
        for span in self._spans(row):
            if span.programme == programme:
                return self._box(row, span)
        return QRectF()

    def _box(self, row: int, span: Any) -> QRectF:
        u = self._units().u
        top = self._row_top(row)
        return QRectF(
            span.x + 0.15 * u, top + 0.35 * u, span.width - 0.3 * u, self._row_h() - 0.7 * u
        )

    def band_rects(self, row: int) -> list[QRectF]:
        if not 0 <= row < len(self._channels):
            return []
        u = self._units().u
        area = self.time_area()
        top = self._row_top(row)
        rects = []
        for start, stop, _name in self._bands(self._channels[row]):
            left = max(area.left(), self._x_at(start))
            right = min(area.right(), self._x_at(stop))
            if right > left:
                rects.append(QRectF(left, top + 0.2 * u, right - left, self._row_h() - 0.4 * u))
        return rects

    def mark_of(self, row: int, title: str) -> str:
        programme = next(
            (s.programme for s in self._spans(row) if s.programme.title == title), None
        )
        return self._mark(self._channels[row], programme) if programme is not None else ""

    def _chips(self) -> dict[str, QRectF]:
        """The bar's chips: ‹, each day, ›, and the source at the right end (its name shortened
        to the room the others leave)."""
        units = self._units()
        u = units.u
        bar = self._bar_rect()
        fm = QFontMetricsF(units.font(1.0, QFont.Weight.DemiBold))
        height = 2.4 * u
        top = bar.center().y() - height / 2
        chips: dict[str, QRectF] = {}
        x = bar.left()
        for name, text in (
            [("back", "‹")]
            + [(f"day{k}", label) for label, k in self.day_choices()]
            + [("forward", "›")]
        ):
            width = fm.horizontalAdvance(text) + 2.0 * u
            chips[name] = QRectF(x, top, width, height)
            x += width + 0.6 * u
        width = fm.horizontalAdvance(self._source_text(fm, bar.right() - x)) + 2.0 * u
        chips["source"] = QRectF(bar.right() - width, top, width, height)
        return chips

    def _source_text(self, fm: QFontMetricsF, room: float) -> str:
        """The source chip's text in `room` px (with its padding): "★ Favorites ▾"."""
        source = self._source or "★"
        room = max(0.0, room - 2.0 * self._units().u - fm.horizontalAdvance(" ▾"))
        return fm.elidedText(source, Qt.ElideRight, room) + " ▾"

    def chip_rects(self) -> dict[str, QRectF]:
        return self._chips()

    def source_rect(self) -> QRectF:
        return self._chips()["source"]

    # ---- data -------------------------------------------------------------------------------

    def _row_programmes(self, row: int) -> list[Programme]:
        if not 0 <= row < len(self._channels):
            return []
        channel = self._channels[row]
        name = str(channel.get("id") or channel.get("name") or row)
        key = (provider_of(channel), name, self._start.timestamp())
        found = self._cache.get(key)
        if found is None:
            try:
                found = list(self._programmes(channel, self._start, LIMIT))
            except Exception as e:  # a guide that fails shows nothing for the row
                logger.debug("A channel's programmes could not be read: %s", type(e).__name__)
                found = []
            if found:  # none yet (a guide still loading): asked again
                self._cache[key] = found
        return found

    def _on_now(self, row: int) -> Optional[Programme]:
        now = self._now()
        return next((p for p in self._row_programmes(row) if p.start <= now < p.stop), None)

    def _first(self, row: int) -> Optional[Programme]:
        spans = self._spans(row)
        return spans[0].programme if spans else None

    def _recordable(self, programme: Programme) -> bool:
        return programme.stop > self._now()

    # ---- the card ---------------------------------------------------------------------------

    def card_buttons(self) -> list[str]:
        if self._card is None:
            return []
        row, programme = self._card
        mark = self._mark(self._channels[row], programme)
        if mark == "series":
            return ["skip", "cancel_series"]
        if mark in ("once", "recording"):
            return ["cancel"]
        return ["record", "series"]

    def _card_rect(self) -> QRectF:
        if self._card is None:
            return QRectF()
        u = self._units().u
        row, programme = self._card
        box = self._box_of(row, programme)
        height = 3.0 * u + BUTTON_H_U * u + 1.6 * u
        width = CARD_W_U * u
        x = min(max(self.time_area().left(), box.left()), self.width() - width - 0.5 * u)
        y = box.bottom() + 0.4 * u
        if y + height > self.height():
            y = box.top() - height - 0.4 * u
        return QRectF(x, max(0.0, y), width, height)

    def card_rect(self) -> QRectF:
        return self._card_rect()

    def card_focus(self) -> str:
        buttons = self.card_buttons()
        return buttons[min(self._card_button, len(buttons) - 1)] if buttons else ""

    def bar_focus(self) -> str:
        return self._bar

    def card_button_rect(self, name: str) -> QRectF:
        units = self._units()
        u = units.u
        card = self._card_rect()
        fm = QFontMetricsF(units.font(0.95, QFont.Weight.DemiBold))
        x = card.left() + 1.0 * u
        y = card.bottom() - 0.8 * u - BUTTON_H_U * u
        for button in self.card_buttons():
            width = fm.horizontalAdvance(translate(_BUTTON_TEXTS[button])) + 2.0 * u
            if button == name:
                return QRectF(x, y, width, BUTTON_H_U * u)
            x += width + 0.6 * u
        return QRectF()

    def _press_card(self, name: str) -> None:
        if self._card is None:
            return
        row, programme = self._card
        channel = self._channels[row]
        self._card = None
        if name == "record":
            self.recordRequested.emit(channel, programme, False)
        elif name == "series":
            self.recordRequested.emit(channel, programme, True)
        elif name == "skip":
            self.skipRequested.emit(channel, programme)
        elif name == "cancel_series":
            self.cancelSeriesRequested.emit(channel, programme)
        elif name == "cancel":
            self.cancelRequested.emit(channel, programme)
        self.update()

    # ---- keys -------------------------------------------------------------------------------

    def channels(self) -> list[dict]:
        return list(self._channels)

    def has_rows(self) -> bool:
        """Rows to show: channels, and programmes for those that show."""
        return bool(self._channels) and not self.empty_text()

    def at_last_row(self) -> bool:
        return self._row >= len(self._channels) - 1

    def at_bottom(self) -> bool:
        """↓ leaves the guide: no rows (the bar has the keys), or the last row."""
        return not self.has_rows() or (not self._bar and self.at_last_row())

    def enter(self) -> None:
        """The keys come to the guide: its rows, or its bar when it has none."""
        self._bar = "" if self.has_rows() else f"day{self._day}"
        self.update()

    def set_programmes(self, programmes: Callable[[dict, datetime, int], list[Programme]]) -> None:
        """The guide's programmes (the window's catalog), once the page has them."""
        self._programmes = programmes
        self.refresh()

    def current_title(self) -> str:
        return self._current.title if self._current is not None else ""

    def move_by(self, dx: int, dy: int) -> None:
        if self._bar or not self.has_rows():
            self._move_in_bar(dx, dy)
            return
        if self._card is not None and dx and not dy:  # along the card's buttons
            last = max(0, len(self.card_buttons()) - 1)
            self._card_button = max(0, min(last, self._card_button + dx))
            self.update()
            return
        self._card = None
        if dy:
            if self._row + dy < 0:
                self._bar = f"day{self._day}"  # above the rows: the bar
                self.update()
                return
            self._go_row(self._row + dy)
        elif dx:
            spans = [s.programme for s in self._spans(self._row)]
            if self._current in spans:
                k = spans.index(self._current) + dx
                if 0 <= k < len(spans):
                    self._current = spans[k]
                elif dx > 0:
                    self.shift(1)
                elif self._start > self._home_start():
                    self.shift(-1)
            elif spans:
                self._current = spans[0]
        self.update()

    def _move_in_bar(self, dx: int, dy: int) -> None:
        if dy < 0:
            self._bar = ""
            self.update()
            self.upEdge.emit()
            return
        if dy > 0:
            if self.has_rows():
                self._bar = ""
                self.update()
            return  # without rows the page goes on below (`at_bottom`)
        order = list(self._chips())
        if not self._bar:
            self._bar = f"day{self._day}"
        else:
            k = order.index(self._bar) if self._bar in order else 0
            self._bar = order[max(0, min(len(order) - 1, k + dx))]
        self.update()

    def _go_row(self, row: int) -> None:
        """The keys to `row`, on its programme at the time the current one starts."""
        at = self._current.start if self._current is not None else self._now()
        self._row = max(0, min(len(self._channels) - 1, row))
        self._reveal()
        programmes = self._row_programmes(self._row)
        self._current = next((p for p in programmes if p.start <= at < p.stop), None) or (
            programmes[0] if programmes else None
        )

    def page(self, pages: int) -> None:
        """PgUp/PgDn: a screen of rows up or down."""
        if not self.has_rows():
            return
        self._bar = ""
        self._card = None
        self._go_row(self._row + pages * self._rows_fit())
        self.update()

    def _reveal(self) -> None:
        fit = self._rows_fit()
        if self._row < self._top:
            self._top = self._row
        elif self._row >= self._top + fit:
            self._top = self._row - fit + 1

    def activate(self) -> None:
        """Enter: the bar's chip presses; the card of the current programme opens (its
        accented button first: a second Enter presses the button the keys are on)."""
        if self._bar:
            chips = self._chips()
            if self._bar in chips:
                self._press_chip(self._bar, chips[self._bar])
            return
        if self._card is not None:
            focused = self.card_focus()
            if focused:
                self._press_card(focused)
            return
        if self._current is not None and self._recordable(self._current):
            self._card = (self._row, self._current)
            self._card_button = 0
            self.update()

    def record_key(self) -> bool:
        """R: the current programme records (False when it can't: none, or it ended)."""
        if self._current is None or not self._recordable(self._current) or not self._channels:
            return False
        self.recordRequested.emit(self._channels[self._row], self._current, False)
        return True

    def escape(self) -> bool:
        if self._card is not None:
            self._card = None
            self.update()
            return True
        return False

    # ---- the mouse --------------------------------------------------------------------------

    def _row_at(self, y: float) -> int:
        area = self.time_area()
        if y < area.top():
            return -1
        row = self._top + int((y - area.top()) // max(1.0, self._row_h()))
        return row if row < len(self._channels) else -1

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        if self._card is not None:
            for name in self.card_buttons():
                if self.card_button_rect(name).contains(pos):
                    self._press_card(name)
                    return
            if self._card_rect().contains(pos):
                return
            self._card = None
        for name, rect in self._chips().items():
            if rect.contains(pos):
                self._press_chip(name, rect)
                return
        row = self._row_at(pos.y())
        if row < 0 or self.empty_text():  # no rows drawn: nothing to click
            self.update()
            return
        if pos.x() < self.time_area().left():
            self.tuneRequested.emit(self._channels[row])
            return
        for span in self._spans(row):
            if self._box(row, span).contains(pos):
                self._row, self._current = row, span.programme
                self._bar = ""
                if self._recordable(span.programme):
                    self._card = (row, span.programme)
                    self._card_button = 0
                break
        self.update()

    def _press_chip(self, name: str, rect: QRectF) -> None:
        if name == "source":
            self.sourceRequested.emit(
                self.mapToGlobal(QPoint(int(rect.left()), int(rect.bottom())))
            )
        elif name == "back":
            self.shift(-1)
        elif name == "forward":
            self.shift(1)
        elif name.startswith("day"):
            self.set_day(int(name[3:]))

    def wheelEvent(self, event) -> None:
        """The rows scroll by the distance moved (a trackpad's small steps add up, its glide
        and a gesture's start or end move nothing); sideways, the window moves in time."""
        delta = event.angleDelta()
        if abs(delta.x()) > abs(delta.y()):
            self._time_wheel.wheel(delta.x(), event.modifiers())
        else:
            self._row_wheel.wheel_event(event)
        event.accept()

    def _on_row_wheel(self, steps: int, _modifiers: Any) -> None:
        last = max(0, len(self._channels) - self._rows_fit())
        self._top = max(0, min(last, self._top + steps))
        self._card = None
        self.update()

    def _on_time_wheel(self, steps: int, _modifiers: Any) -> None:
        self.shift(steps)

    def tooltip_at(self, pos: QPointF) -> Optional[str]:
        row = self._row_at(pos.y())
        if row < 0:
            return None
        for span in self._spans(row):
            if self._box(row, span).contains(pos):
                programme = span.programme
                start = fmt_clock(programme.start.astimezone())
                stop = fmt_clock(programme.stop.astimezone())
                return f"{programme.title} · {start}–{stop}"
        return None

    # ---- painting ---------------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        units = self._units()
        u = units.u
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), qcolor(PALETTE.canvas))
        self._paint_bar(p, units)
        self._paint_head(p, units)
        area = self.time_area()
        empty = self.empty_text()
        if empty:
            p.setFont(units.font(1.05))
            p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
            line = QRectF(1.0 * u, area.top() + 1.0 * u, self.width() - 2.0 * u, 3.0 * u)
            p.drawText(line, Qt.AlignLeft | Qt.AlignVCenter, elided(p, empty, line.width()))
            p.end()
            return
        p.save()
        p.setClipRect(QRectF(0, area.top(), self.width(), area.height()))
        for row in range(self._top, min(len(self._channels), self._top + self._rows_fit() + 1)):
            self._paint_row(p, units, row)
        self._paint_now(p, units)
        p.restore()
        if self._card is not None:
            self._paint_card(p, units)
        p.end()

    def _paint_bar(self, p: QPainter, units: Units) -> None:
        p.setFont(units.font(1.0, QFont.Weight.DemiBold))
        chips = self._chips()
        fm = QFontMetricsF(units.font(1.0, QFont.Weight.DemiBold))
        room = chips["source"].right() - chips["forward"].right() - 0.6 * units.u
        for name, rect in chips.items():
            on = name == f"day{self._day}"
            fill_round(p, rect, qcolor(PALETTE.text, 0.16 if on else 0.06), rect.height() / 2)
            if self._focused and name == self._bar:
                self._outline(p, rect, qcolor(PALETTE.text), rect.height() / 2)
            p.setPen(qcolor(PALETTE.text, 1.0 if on else 0.8))
            text = {"back": "‹", "forward": "›"}.get(name)
            if text is None and name == "source":
                text = self._source_text(fm, room)
            elif text is None:
                text = self.day_choices()[int(name[3:])][0]
            p.drawText(rect, Qt.AlignCenter, text)

    def _paint_head(self, p: QPainter, units: Units) -> None:
        head = self._head_rect()
        area = self.time_area()
        p.setFont(units.font(0.85))
        p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
        step = area.width() / SLOTS
        for k in range(SLOTS):
            moment = self._start + timedelta(minutes=30 * k)
            rect = QRectF(area.left() + k * step + 0.4 * units.u, head.top(), step, head.height())
            p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, fmt_clock(moment.astimezone()))

    def _paint_row(self, p: QPainter, units: Units, row: int) -> None:
        u = units.u
        channel = self._channels[row]
        top = self._row_top(row)
        area = self.time_area()
        p.setPen(qcolor(PALETTE.text, 0.06))
        p.drawLine(QPointF(0, top + self._row_h()), QPointF(self.width(), top + self._row_h()))
        name_rect = QRectF(0.8 * u, top, area.left() - 1.2 * u, self._row_h())
        fill_round(
            p,
            QRectF(0, top + 1, area.left() - 0.4 * u, self._row_h() - 2),
            qcolor(PALETTE.panel_raised),
            4,
        )
        p.setFont(units.font(1.0, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.text))
        name = str(channel.get("name") or "")
        p.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter, elided(p, name, name_rect.width()))
        for band in self.band_rects(row):
            self._paint_band(p, units, band)
        now = self._now()
        for span in self._spans(row):
            programme = span.programme
            box = self._box(row, span)
            mark = self._mark(channel, programme)
            past = programme.stop <= now
            on_now = programme.start <= now < programme.stop
            fill = qcolor(PALETTE.text, 0.10 if on_now else 0.06)
            fill_round(p, box, fill, RADIUS_U * u)
            if mark == "recording":
                self._outline(p, box, qcolor(PALETTE.live), RADIUS_U * u)
            elif mark in ("once", "series"):
                self._outline(p, box, qcolor(PALETTE.accent), RADIUS_U * u)
            if self._focused and not self._bar and row == self._row and programme == self._current:
                self._outline(p, box.adjusted(-1, -1, 1, 1), qcolor(PALETTE.text), RADIUS_U * u)
            text = programme.title
            if mark in ("recording", "once", "series"):
                text = "● " + text
            p.setFont(units.font(0.95))
            alpha = PAST_ALPHA if past else 1.0
            pen = qcolor(PALETTE.live) if mark == "recording" else qcolor(PALETTE.text, 0.9)
            if mark in ("once", "series"):
                pen = qcolor(PALETTE.accent)
            pen.setAlphaF(pen.alphaF() * alpha)
            p.setPen(pen)
            inner = box.adjusted(0.6 * u, 0, -0.4 * u, 0)
            p.drawText(inner, Qt.AlignLeft | Qt.AlignVCenter, elided(p, text, inner.width()))

    def _paint_band(self, p: QPainter, units: Units, band: QRectF) -> None:
        u = units.u
        p.save()
        brush = QBrush(QColor(qcolor(PALETTE.accent, 0.22)), Qt.BDiagPattern)
        p.setPen(Qt.NoPen)
        fill_round(p, band, qcolor(PALETTE.accent, 0.10), RADIUS_U * u)
        p.setBrush(brush)
        p.drawRoundedRect(band, RADIUS_U * u, RADIUS_U * u)
        p.restore()
        self._outline(p, band, qcolor(PALETTE.accent, 0.8), RADIUS_U * u)

    def _outline(self, p: QPainter, rect: QRectF, color: QColor, radius: float) -> None:
        p.save()
        p.setPen(QPen(color, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)
        p.restore()

    def _paint_now(self, p: QPainter, units: Units) -> None:
        area = self.time_area()
        x = self.now_x()
        if not area.left() <= x <= area.right():
            return
        p.save()
        glow = qcolor(PALETTE.accent, 0.25)
        p.fillRect(QRectF(x - 3, area.top(), 6, area.height()), glow)
        p.fillRect(QRectF(x - 1, area.top(), 2, area.height()), qcolor(PALETTE.accent))
        p.restore()

    def _paint_card(self, p: QPainter, units: Units) -> None:
        u = units.u
        card = self._card_rect()
        if self._card is None or card.isEmpty():
            return
        row, programme = self._card
        fill_round(p, card, qcolor(PALETTE.panel_raised), 1.2 * u)
        self._outline(p, card, qcolor(PALETTE.text, 0.18), 1.2 * u)
        p.setFont(units.font(1.05, QFont.Weight.Bold))
        p.setPen(qcolor(PALETTE.text))
        title = QRectF(card.left() + 1.0 * u, card.top() + 0.6 * u, card.width() - 2 * u, 1.6 * u)
        p.drawText(title, Qt.AlignLeft | Qt.AlignVCenter, elided(p, programme.title, title.width()))
        p.setFont(units.font(0.9))
        p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
        start = fmt_clock(programme.start.astimezone())
        stop = fmt_clock(programme.stop.astimezone())
        channel = str(self._channels[row].get("name") or "")
        sub = QRectF(title.left(), title.bottom(), title.width(), 1.4 * u)
        p.drawText(
            sub,
            Qt.AlignLeft | Qt.AlignVCenter,
            elided(p, f"{channel} · {start}–{stop}", sub.width()),
        )
        p.setFont(units.font(0.95, QFont.Weight.DemiBold))
        for k, name in enumerate(self.card_buttons()):
            rect = self.card_button_rect(name)
            if k == 0:
                fill_round(p, rect, qcolor(PALETTE.accent), 0.8 * u)
                p.setPen(qcolor(PALETTE.canvas))
            else:
                fill_round(p, rect, qcolor(PALETTE.text, 0.11), 0.8 * u)
                p.setPen(
                    qcolor(PALETTE.live) if name.startswith("cancel") else qcolor(PALETTE.text)
                )
            if self._focused and name == self.card_focus():
                self._outline(p, rect.adjusted(-2, -2, 2, 2), qcolor(PALETTE.text), 0.9 * u)
            p.drawText(rect, Qt.AlignCenter, translate(_BUTTON_TEXTS[name]))
