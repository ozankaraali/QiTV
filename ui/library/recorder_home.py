"""The Recordings home (2.9.7): where the Recordings tab opens.

Its top row has three cards. What records now, big: its REC badge, the programme's title, a
bar of how far it is ("1:12:30 of 2:58 · until 21:58"; one started with R, its elapsed time
alone), ■ Stop and the accented ▶ Watch; with more than one, each is a line of the card. What
is next: when, in big accented words ("in 48 min" within two hours, "at 21:00" later today, its
day beyond), the programme, its channel and times, its kind, Edit… where its kind has one.
Adding one: a line of help, the accented ＋ At a time…, and the streams of the providers with a
limit or a stream in use. Under them, tonight's guide (`HomeGuide`, its own child); at the
bottom, the recordings recorded last, as tiles, and All recordings ›.

The keys: the top row's buttons (Watch first), the guide, the shelf, in that order down; ← and
→ move within a row, Enter does what the button or tile says, ↑ from the top row goes up out of
the page, ← from a row's first goes to the tab's rail. Nothing here opens a stream: Watch and a
tile ask the page.
"""

from __future__ import annotations

from datetime import datetime
import math
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QLocale, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PySide6.QtWidgets import QWidget

from services import i18n, recording_desk as desk, recording_schedule as rs
from services.epg_view import Programme
from services.i18n import N_, tr, tr_n, translate
from ui.library.home_guide import HomeGuide
from ui.library.poster_grid import card_hue
from ui.overlays.base import TooltipWidget
from ui.overlays.painting import Units, draw_progress, elided, fill_round, fmt_clock, ui_units
from ui.theme import PALETTE, RADIUS_CARD, qcolor

PAD_U = 2.2
GAP_U = 1.2
HERO_H_U = 15.0
SHELF_H_U = 13.0
TILE_W_U = 15.0
BUTTON_H_U = 2.6
TICK_MS = 1000
SOON_S = 2 * 3600  # within this, the next one counts down

# i18n: the Recordings home's card of what records now, when nothing does
NOTHING_NOW = N_("Nothing is recording.")
# i18n: the Recordings home's card of the next recording, when nothing is scheduled
NOTHING_NEXT = N_("Nothing is scheduled yet.")
# i18n: the Recordings home: the caption over the next recording (in capitals)
NEXT_CAPTION = N_("Next")
# i18n: the Recordings home: the caption over adding one (in capitals)
ADD_CAPTION = N_("Add a recording")
# i18n: the Recordings home: how to add a recording
ADD_HELP = N_("Click a programme in the guide below, or record a channel by the clock.")
# i18n: the Recordings home's button: record a channel at a time chosen (Record at a time)
ADD_TEXT = N_("＋ At a time…")
# i18n: the Recordings home's button: the recording's channel plays
WATCH_TEXT = N_("▶ Watch")
# i18n: the Recordings home's button: the recording stops (its file kept)
STOP_TEXT = N_("■ Stop")
# i18n: the Recordings home's shelf of the recordings made last
SHELF_TEXT = N_("Recently recorded")
# i18n: the Recordings home's link to every recording
ALL_TEXT = N_("All recordings ›")
# i18n: a recording's progress on the Recordings home ({elapsed}, {length}: h:mm, {until}: a time)
PROGRESS_TEXT = N_("{elapsed} of {length} · until {until}")
# i18n: the next recording starts at a time today ({time})
AT_TEXT = N_("at {time}")
# i18n: the next recording starts in hours ({h}) and minutes ({mm}, two digits)
IN_HOURS_TEXT = N_("in {h} h {mm}")


def _clock_text(seconds: float, *, with_seconds: bool = True) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if with_seconds else f"{hours}:{minutes:02d}"


class RecorderHome(TooltipWidget):
    watchRequested = Signal(str)  # a recording's name (its REC sign name)
    stopRequested = Signal(str)
    editRequested = Signal(str)  # the next one's schedule item id
    addRequested = Signal()
    allRequested = Signal()
    playRequested = Signal(dict)  # a recording of the shelf
    upEdge = Signal()  # ↑ from the top row
    leftEdge = Signal()  # ← from a row's first button or tile

    def __init__(
        self,
        programmes: Callable[[dict, datetime, int], list[Programme]],
        now: Callable[[], datetime],
        parent: Optional[QWidget] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)
        self._now = now
        self._clock = clock
        self._hero = desk.Hero((), None, ())
        self._shelf: list[dict] = []
        self._region = "hero"
        self._button = 0
        self._tile = 0
        self._focused = False
        self.guide = HomeGuide(programmes, now, self)
        self.guide.upEdge.connect(self._from_guide_up)
        self._tick = QTimer(self)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self.update)

    # ---- content ----------------------------------------------------------------------------

    def set_hero(self, hero: desk.Hero) -> None:
        self._hero = hero
        self._button = min(self._button, max(0, len(self._hero_order()) - 1))
        self._sync_tick()
        self.update()

    def set_shelf(self, items: list[dict]) -> None:
        self._shelf = list(items)
        self._tile = min(self._tile, max(0, len(self._shelf_order()) - 1))
        self.update()

    def set_focused(self, focused: bool) -> None:
        self._focused = focused
        self.guide.set_focused(focused and self._region == "guide")
        self.update()

    def now_cards(self) -> list[tuple[str, str, Optional[float]]]:
        """(title, its progress in words, its share done or None) of each recording now."""
        cards = []
        for recording in self._hero.now:
            elapsed = max(0.0, self._clock() - recording.since)
            title = recording.title or recording.channel
            if recording.until > 0:
                left = max(0.0, recording.until - self._now().timestamp())
                total = elapsed + left
                text = translate(
                    PROGRESS_TEXT,
                    elapsed=_clock_text(elapsed),
                    length=_clock_text(total, with_seconds=False),
                    until=fmt_clock(datetime.fromtimestamp(recording.until).astimezone()),
                )
                cards.append((title, text, elapsed / total if total > 0 else None))
            else:
                cards.append((title, _clock_text(elapsed), None))
        return cards

    def next_when(self) -> str:
        """When the next recording starts, as its card says it ("" without one)."""
        upcoming = self._hero.next
        if upcoming is None:
            return ""
        now = self._now()
        start = datetime.fromtimestamp(upcoming.start).astimezone()
        seconds = (start - now).total_seconds()
        if seconds < SOON_S:
            minutes = max(1, math.ceil(seconds / 60))
            if minutes < 60:
                # i18n: the next recording starts in minutes
                return tr_n("in {n} min", "in {n} min", minutes, n=i18n.number(minutes))
            hours, rest = divmod(minutes, 60)
            return translate(IN_HOURS_TEXT, h=i18n.number(hours), mm=f"{rest:02d}")
        if start.date() == now.astimezone().date():
            return translate(AT_TEXT, time=fmt_clock(start))
        day = i18n.app_locale().dayName(start.isoweekday(), QLocale.FormatType.ShortFormat)
        return f"{day} {fmt_clock(start)}"

    def stream_lines(self) -> list[str]:
        return [f"{s.provider} · {desk.streams_text(s)}" for s in self._hero.streams]

    # ---- geometry ---------------------------------------------------------------------------

    def _units(self) -> Units:
        host = self.window()
        width = host.width() if host is not None and host.width() > 0 else self.width()
        return ui_units(max(1, width))

    def _hero_cards(self) -> tuple[QRectF, QRectF, QRectF]:
        u = self._units().u
        pad, gap = PAD_U * u, GAP_U * u
        width = max(1.0, self.width() - 2 * pad - 2 * gap)
        top, height = pad, HERO_H_U * u
        widths = (width * 1.35 / 3.35, width / 3.35, width / 3.35)
        x = pad
        cards = []
        for w in widths:
            cards.append(QRectF(x, top, w, height))
            x += w + gap
        return cards[0], cards[1], cards[2]

    def _shelf_area(self) -> QRectF:
        u = self._units().u
        pad = PAD_U * u
        height = SHELF_H_U * u
        return QRectF(pad, self.height() - pad - height, self.width() - 2 * pad, height)

    def guide_rect(self) -> QRectF:
        u = self._units().u
        top = self._hero_cards()[0].bottom() + GAP_U * u
        bottom = self._shelf_area().top() - GAP_U * u
        pad = PAD_U * u
        return QRectF(pad, top, self.width() - 2 * pad, max(0.0, bottom - top))

    def _button_rect(self, right: float, bottom: float, text: str, units: Units) -> QRectF:
        fm = QFontMetricsF(units.font(0.95, QFont.Weight.DemiBold))
        width = fm.horizontalAdvance(text) + 2.0 * units.u
        return QRectF(right - width, bottom - BUTTON_H_U * units.u, width, BUTTON_H_U * units.u)

    def hero_rects(self) -> dict[str, QRectF]:
        """The top row's buttons: "watch:<name>", "stop:<name>", "edit", "add"."""
        units = self._units()
        u = units.u
        now_card, next_card, add_card = self._hero_cards()
        rects: dict[str, QRectF] = {}
        rows = self._now_rows(now_card)
        for recording, row in zip(self._hero.now, rows):
            watch = self._button_rect(
                row.right() - 1.0 * u, row.bottom() - 0.6 * u, translate(WATCH_TEXT), units
            )
            stop = self._button_rect(
                watch.left() - 0.6 * u, watch.bottom(), translate(STOP_TEXT), units
            )
            rects[f"watch:{recording.name}"] = watch
            rects[f"stop:{recording.name}"] = stop
        if self._editable():
            rects["edit"] = self._button_rect(
                next_card.right() - 1.0 * u, next_card.bottom() - 1.0 * u, desk_edit(), units
            )
        add = self._button_rect(
            add_card.right() - 1.0 * u, add_card.bottom() - 2.8 * u, translate(ADD_TEXT), units
        )
        add.moveLeft(add_card.left() + 1.2 * u)
        rects["add"] = add
        return rects

    def _now_rows(self, card: QRectF) -> list[QRectF]:
        count = len(self._hero.now)
        if not count:
            return []
        height = card.height() / count
        return [
            QRectF(card.left(), card.top() + k * height, card.width(), height) for k in range(count)
        ]

    def _editable(self) -> bool:
        upcoming = self._hero.next
        return upcoming is not None and upcoming.kind in (rs.ONCE, rs.TIMER)

    def all_rect(self) -> QRectF:
        units = self._units()
        area = self._shelf_area()
        fm = QFontMetricsF(units.font(1.0, QFont.Weight.DemiBold))
        width = fm.horizontalAdvance(translate(ALL_TEXT)) + 1.0 * units.u
        return QRectF(area.right() - width, area.top(), width, 2.4 * units.u)

    def shelf_rects(self) -> list[tuple[dict, QRectF]]:
        u = self._units().u
        area = self._shelf_area()
        top = area.top() + 3.0 * u
        width = TILE_W_U * u
        height = min(area.bottom() - top, width * 9 / 16)
        out = []
        x = area.left()
        for item in self._shelf:
            if x + width > area.right():
                break
            out.append((item, QRectF(x, top, width, height)))
            x += width + GAP_U * u
        return out

    # ---- keys -------------------------------------------------------------------------------

    def focus_region(self) -> str:
        return self._region

    def _hero_order(self) -> list[str]:
        order = []
        for recording in self._hero.now:
            order += [f"watch:{recording.name}", f"stop:{recording.name}"]
        if self._editable():
            order.append("edit")
        order.append("add")
        return order

    def _shelf_order(self) -> list[Any]:
        return [item for item, _rect in self.shelf_rects()] + ["all"]

    def focused_button(self) -> str:
        order = self._hero_order()
        return order[self._button] if self._region == "hero" and order else ""

    def _set_region(self, region: str) -> None:
        self._region = region
        self.guide.set_focused(self._focused and region == "guide")
        self.update()

    def move_by(self, dx: int, dy: int) -> None:
        if self._region == "guide":
            if dy > 0 and (not self.guide.has_rows() or self.guide.at_last_row()):
                self._set_region("shelf")
                return
            self.guide.move_by(dx, dy)  # ↑ from its first row: `_from_guide_up`
            return
        if dy < 0:
            if self._region == "hero":
                self.upEdge.emit()
            else:
                self._set_region("guide")
            return
        if dy > 0:
            if self._region == "hero":
                self._set_region("guide")
            return
        if self._region == "hero":
            order = self._hero_order()
            if self._button + dx < 0:
                self.leftEdge.emit()
                return
            self._button = max(0, min(len(order) - 1, self._button + dx))
        else:
            order = self._shelf_order()
            if self._tile + dx < 0:
                self.leftEdge.emit()
                return
            self._tile = max(0, min(len(order) - 1, self._tile + dx))
        self.update()

    def _from_guide_up(self) -> None:
        self._set_region("hero")

    def activate(self) -> None:
        if self._region == "hero":
            order = self._hero_order()
            if order:
                self._press(order[self._button])
        elif self._region == "guide":
            self.guide.activate()
        else:
            order = self._shelf_order()
            if order:
                choice = order[self._tile]
                if choice == "all":
                    self.allRequested.emit()
                else:
                    self.playRequested.emit(choice)

    def record_key(self) -> bool:
        return self._region == "guide" and self.guide.record_key()

    def escape(self) -> bool:
        return self._region == "guide" and self.guide.escape()

    def _press(self, name: str) -> None:
        kind, _, value = name.partition(":")
        if kind == "watch":
            self.watchRequested.emit(value)
        elif kind == "stop":
            self.stopRequested.emit(value)
        elif kind == "edit" and self._hero.next is not None:
            self.editRequested.emit(self._hero.next.item_id)
        elif kind == "add":
            self.addRequested.emit()

    # ---- the mouse --------------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        for name, rect in self.hero_rects().items():
            if rect.contains(pos):
                self._press(name)
                return
        if self.all_rect().contains(pos):
            self.allRequested.emit()
            return
        for item, rect in self.shelf_rects():
            if rect.contains(pos):
                self.playRequested.emit(item)
                return

    # ---- life -------------------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.guide.setGeometry(self.guide_rect().toRect())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.guide.setGeometry(self.guide_rect().toRect())
        self._sync_tick()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._tick.stop()

    def _sync_tick(self) -> None:
        if self.isVisible() and self._hero.now:
            if not self._tick.isActive():
                self._tick.start()
        else:
            self._tick.stop()

    # ---- painting ---------------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        units = self._units()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), qcolor(PALETTE.canvas))
        now_card, next_card, add_card = self._hero_cards()
        self._paint_now(p, units, now_card)
        self._paint_next(p, units, next_card)
        self._paint_add(p, units, add_card)
        self._paint_shelf(p, units)
        p.end()

    def _caption(self, p: QPainter, units: Units, rect: QRectF, text: str) -> None:
        font = units.font(0.8, QFont.Weight.Bold)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 0.1 * font.pixelSize())
        p.setFont(font)
        p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
        p.drawText(rect, Qt.AlignLeft | Qt.AlignTop, i18n.upper(text))

    def _paint_button(self, p: QPainter, units: Units, name: str, rect: QRectF, text: str) -> None:
        u = units.u
        accent = name.startswith("watch") or name == "add"
        fill_round(
            p, rect, qcolor(PALETTE.accent) if accent else qcolor(PALETTE.text, 0.11), 0.8 * u
        )
        if self._focused and self.focused_button() == name:
            p.setPen(qcolor(PALETTE.text))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect.adjusted(-1.5, -1.5, 1.5, 1.5), 0.8 * u, 0.8 * u)
        p.setFont(units.font(0.95, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.canvas) if accent else qcolor(PALETTE.text))
        p.drawText(rect, Qt.AlignCenter, text)

    def _paint_now(self, p: QPainter, units: Units, card: QRectF) -> None:
        u = units.u
        recording_now = bool(self._hero.now)
        fill_round(
            p,
            card,
            qcolor(PALETTE.live, 0.12) if recording_now else qcolor(PALETTE.panel_raised),
            RADIUS_CARD,
        )
        if not recording_now:
            self._caption(
                p,
                units,
                card.adjusted(1.4 * u, 1.2 * u, 0, 0),
                translate(desk.SECTION_TITLES[desk.NOW]),
            )
            p.setFont(units.font(1.1))
            p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
            p.drawText(
                card.adjusted(1.4 * u, 0, -1.4 * u, 0),
                Qt.AlignLeft | Qt.AlignVCenter,
                translate(NOTHING_NOW),
            )
            return
        rects = self.hero_rects()
        rows = self._now_rows(card)
        big = len(rows) == 1
        for recording, row, (title, text, progress) in zip(self._hero.now, rows, self.now_cards()):
            inner = row.adjusted(1.4 * u, 0.9 * u, -1.4 * u, -0.6 * u)
            badge = QRectF(inner.left(), inner.top(), 4.6 * u, 1.7 * u)
            fill_round(p, badge, qcolor(PALETTE.live), 0.4 * u)
            p.setFont(units.font(0.8, QFont.Weight.Bold))
            p.setPen(qcolor(PALETTE.text))
            p.drawText(badge, Qt.AlignCenter, "● " + tr("REC"))
            p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
            p.setFont(units.font(0.9))
            channel = QRectF(
                badge.right() + 0.8 * u, badge.top(), inner.width() * 0.6, badge.height()
            )
            p.drawText(
                channel,
                Qt.AlignLeft | Qt.AlignVCenter,
                elided(p, recording.channel, channel.width()),
            )
            p.setFont(units.font(2.0 if big else 1.25, QFont.Weight.Bold))
            p.setPen(qcolor(PALETTE.text))
            title_h = QFontMetricsF(p.font()).height()
            title_rect = QRectF(inner.left(), badge.bottom() + 0.4 * u, inner.width(), title_h)
            p.drawText(
                title_rect, Qt.AlignLeft | Qt.AlignVCenter, elided(p, title, title_rect.width())
            )
            y = title_rect.bottom() + 0.4 * u
            if progress is not None:
                bar = QRectF(inner.left(), y, inner.width(), 0.45 * u)
                draw_progress(p, bar, progress, track_alpha=0.2)
                y = bar.bottom() + 0.4 * u
            p.setFont(units.font(0.9))
            p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
            watch = rects[f"watch:{recording.name}"]
            stop = rects[f"stop:{recording.name}"]
            line = QRectF(
                inner.left(), stop.top(), stop.left() - inner.left() - 0.6 * u, stop.height()
            )
            p.drawText(line, Qt.AlignLeft | Qt.AlignVCenter, elided(p, text, line.width()))
            self._paint_button(p, units, f"stop:{recording.name}", stop, translate(STOP_TEXT))
            self._paint_button(p, units, f"watch:{recording.name}", watch, translate(WATCH_TEXT))

    def _paint_next(self, p: QPainter, units: Units, card: QRectF) -> None:
        u = units.u
        fill_round(p, card, qcolor(PALETTE.panel_raised), RADIUS_CARD)
        inner = card.adjusted(1.4 * u, 1.2 * u, -1.4 * u, -1.0 * u)
        self._caption(p, units, inner, translate(NEXT_CAPTION))
        upcoming = self._hero.next
        if upcoming is None:
            p.setFont(units.font(1.05))
            p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
            p.drawText(
                inner,
                Qt.AlignLeft | Qt.AlignVCenter,
                elided(p, translate(NOTHING_NEXT), inner.width()),
            )
            return
        p.setFont(units.font(2.2, QFont.Weight.Bold))
        p.setPen(qcolor(PALETTE.accent))
        when_h = QFontMetricsF(p.font()).height()
        when = QRectF(inner.left(), inner.top() + 1.6 * u, inner.width(), when_h)
        p.drawText(when, Qt.AlignLeft | Qt.AlignVCenter, elided(p, self.next_when(), when.width()))
        p.setFont(units.font(1.1, QFont.Weight.Bold))
        p.setPen(qcolor(PALETTE.text))
        title = QRectF(inner.left(), when.bottom() + 0.2 * u, inner.width(), 1.8 * u)
        p.drawText(title, Qt.AlignLeft | Qt.AlignVCenter, elided(p, upcoming.title, title.width()))
        start = fmt_clock(datetime.fromtimestamp(upcoming.start).astimezone())
        stop = fmt_clock(datetime.fromtimestamp(upcoming.stop).astimezone())
        p.setFont(units.font(0.9))
        p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
        sub = QRectF(inner.left(), title.bottom(), inner.width(), 1.5 * u)
        p.drawText(
            sub,
            Qt.AlignLeft | Qt.AlignVCenter,
            elided(p, f"{upcoming.channel} · {start}–{stop}", sub.width()),
        )
        kind = {rs.ONCE: "once", rs.SERIES: "series"}.get(upcoming.kind, "repeats")
        p.drawText(
            QRectF(
                inner.left(), inner.bottom() - BUTTON_H_U * u, inner.width() / 2, BUTTON_H_U * u
            ),
            Qt.AlignLeft | Qt.AlignVCenter,
            desk.status(kind),
        )
        rect = self.hero_rects().get("edit")
        if rect is not None:
            self._paint_button(p, units, "edit", rect, desk_edit())

    def _paint_add(self, p: QPainter, units: Units, card: QRectF) -> None:
        u = units.u
        fill_round(p, card, qcolor(PALETTE.canvas), RADIUS_CARD)
        p.save()
        pen = p.pen()
        pen.setColor(qcolor(PALETTE.text, 0.18))
        pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS_CARD, RADIUS_CARD)
        p.restore()
        inner = card.adjusted(1.2 * u, 1.2 * u, -1.2 * u, -0.8 * u)
        self._caption(p, units, inner, translate(ADD_CAPTION))
        p.setFont(units.font(0.95))
        p.setPen(qcolor(PALETTE.text, 0.85))
        help_rect = QRectF(inner.left(), inner.top() + 1.6 * u, inner.width(), 3.4 * u)
        p.drawText(help_rect, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, translate(ADD_HELP))
        rect = self.hero_rects()["add"]
        self._paint_button(p, units, "add", rect, translate(ADD_TEXT))
        p.setFont(units.font(0.85))
        p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
        y = rect.bottom() + 0.5 * u
        for line in self.stream_lines()[:2]:
            box = QRectF(inner.left(), y, inner.width(), 1.4 * u)
            p.drawText(box, Qt.AlignLeft | Qt.AlignVCenter, elided(p, line, box.width()))
            y += 1.4 * u

    def _paint_shelf(self, p: QPainter, units: Units) -> None:
        u = units.u
        area = self._shelf_area()
        p.setFont(units.font(1.2, QFont.Weight.Bold))
        p.setPen(qcolor(PALETTE.text))
        p.drawText(
            QRectF(area.left(), area.top(), area.width() / 2, 2.4 * u),
            Qt.AlignLeft | Qt.AlignVCenter,
            translate(SHELF_TEXT),
        )
        all_rect = self.all_rect()
        focused_all = (
            self._focused and self._region == "shelf" and self._shelf_order()[self._tile] == "all"
        )
        p.setFont(units.font(1.0, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.accent) if focused_all else qcolor(PALETTE.text, 0.8))
        p.drawText(all_rect, Qt.AlignRight | Qt.AlignVCenter, translate(ALL_TEXT))
        order = self._shelf_order()
        for item, rect in self.shelf_rects():
            hue = card_hue(str(item.get("_channel") or ""))  # a channel keeps its colour
            fill_round(p, rect, _card_color(hue), RADIUS_CARD)
            if self._focused and self._region == "shelf" and order[self._tile] is item:
                p.setPen(qcolor(PALETTE.accent))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(rect.adjusted(-1.5, -1.5, 1.5, 1.5), RADIUS_CARD, RADIUS_CARD)
            p.setFont(units.font(1.0, QFont.Weight.Bold))
            p.setPen(qcolor(PALETTE.text))
            title = str(item.get("_title") or item.get("name") or "")
            text_rect = rect.adjusted(0.8 * u, 0, -0.8 * u, -0.6 * u)
            p.drawText(
                text_rect, Qt.AlignLeft | Qt.AlignBottom, elided(p, title, text_rect.width())
            )


def desk_edit() -> str:
    return translate(desk.BUTTON_TEXTS[desk.EDIT])


def _card_color(hue: float) -> QColor:
    return QColor.fromHsvF(hue, 0.45, 0.32)
