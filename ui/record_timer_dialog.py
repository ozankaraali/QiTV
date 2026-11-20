"""Record at a time (2.8): a channel recorded from one time to another, once on a day or on
chosen weekdays, with no guide needed (`services.recording_schedule`'s timers).

Opened from a channel's right-click and the guide's footer (on its channel; with the footer's
programme's time when there is one). Times are this computer's clock's; a time whose end comes
before its start ends the next day. Once on a day that is over cannot be scheduled.

2.9.7, the Recordings page: its Edit… opens it filled in with a scheduled recording (its name,
its days; Save), and its Record at a time… with the channels to choose from (`channels`: typed
to find one, the one playing first).

2.13.1: a card over the picture, as Settings' cards are (ui/overlays/settings_card.py), no
window: Channel (the one given, as it is; or a choice, typed to find), Name, Repeat, the days
(On these days), Day (Once), From and To, then Cancel and Record (Save for an Edit…), the accent
one. ↑/↓ go between the rows and the buttons. On a stepper ←/→ (‹ and › with the mouse) step:
the repeat round, the day by one (Page Up/Down: a week; never before today), From and To by
five minutes onto the five minutes' grid (Page Up/Down: an hour), a time given (a programme's,
21:03) staying as it is until it is stepped; the channel to the next of the channels the typed
text finds anywhere in their names (all of them while nothing is typed: Backspace deletes a
letter). On the days ←/→ go between them and Enter (OK) ticks one. Enter elsewhere is Record,
which waits (greyed out) while what is set cannot be scheduled, the line under the rows saying
why (as the window's did). Esc, Cancel, Back (Backspace off the name), the mouse's Back and a
click on the dimmed picture cancel. It answers through `done(choice, channel)` (`channel`: the
one chosen from `channels`, else None) and hides; nothing waits for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Callable, Optional

from PySide6.QtCore import QDate, QLocale, QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from services.i18n import N_, app_locale, translate
from services.recording_schedule import local_time
from ui.keymap import Action
from ui.overlays.base import announce
from ui.overlays.painting import Units, draw_icon, fill_round, paint_value_steps
from ui.overlays.settings_card import (
    CANCEL,
    CANCEL_TEXT,
    MIDDLE,
    PANEL,
    TITLE,
    Button,
    Field,
    FormCard,
)
from ui.text_edit import clipboard_line
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

ONCE, DAILY, WEEKDAYS, DAYS = "once", "daily", "weekdays", "days"
# i18n: the window that records a channel at a time of the user's choosing (no guide needed)
TITLE_TEXT = N_("Record at a time")
REPEATS = (
    (ONCE, N_("Once")),  # i18n: a timer recording's repeat: one time, on the day chosen
    (DAILY, N_("Every day")),  # i18n: a timer recording's repeat
    (WEEKDAYS, N_("Monday to Friday")),  # i18n: a timer recording's repeat
    (DAYS, N_("On these days")),  # i18n: a timer recording's repeat: the days ticked below
)
# i18n: a hint in "Record at a time": the end time is earlier than the start
NEXT_DAY_TEXT = N_("Ends the next day.")
# i18n: a hint in "Record at a time": a one-time recording whose time is over
PASSED_TEXT = N_("That time has passed.")
# i18n: a hint in "Record at a time": "On these days" with no day ticked
NO_DAY_TEXT = N_("Choose at least one day.")
# i18n: a hint in "Record at a time": the start and end times are the same
NO_LENGTH_TEXT = N_("From and To are the same.")
# i18n: a hint in "Record at a time": what was typed names no channel
NO_CHANNEL_TEXT = N_("Choose a channel from the list.")
# i18n: the placeholder of "Record at a time"'s Name field
OPTIONAL_TEXT = N_("Optional")
# i18n: the button of "Record at a time" that adds the recording to the schedule (verb)
RECORD_TEXT = N_("Record")
CHANNEL_TEXT = N_("Channel")  # i18n: a field's label
NAME_TEXT = N_("Name")  # i18n: a field's label: what it is called
REPEAT_TEXT = N_("Repeat")  # i18n: a field's label: how often it records
DAY_TEXT = N_("Day")  # i18n: a field's label: the date it records on
FROM_TEXT = N_("From")  # i18n: a field's label: the time it starts
TO_TEXT = N_("To")  # i18n: a field's label: the time it stops
STEP_MIN = 5  # it opens on the next five minutes, and steps by five
PAGE_MIN = 60  # Page Up/Down: an hour
PAGE_DAYS = 7  # a week
PAGE_CHANNELS = 10
RECORD = "record"  # the key of its Record (Save) button
CHANNEL, NAME, REPEAT, WEEK, DAY = "channel", "name", "repeat", "days", "day"
FROM, TO = "from", "to"
_STEPPERS = (CHANNEL, REPEAT, DAY, FROM, TO)
_LABELS = {
    CHANNEL: CHANNEL_TEXT,
    NAME: NAME_TEXT,
    REPEAT: REPEAT_TEXT,
    WEEK: "",
    DAY: DAY_TEXT,
    FROM: FROM_TEXT,
    TO: TO_TEXT,
}
_DAY_MINUTES = 24 * 60
_RADIUS_U = 0.6  # a field's corners (the cards')
_DAY_GAP_U = 0.4  # between the days' buttons
_HINT_GAP_U = 0.6  # above the line that says why it can't record
_SEARCH_U = 1.1  # the ⌕ before what is typed to find a channel
_HINT = ("hint", "")


@dataclass(frozen=True)
class TimerChoice:
    """What "Record at a time" was given: `begin` minutes after midnight for `minutes`, on
    `day` once (`days` empty), else on the weekdays `days` (Monday 0)."""

    title: str
    day: date
    begin: int
    minutes: int
    days: tuple[int, ...]
    zone: Optional[tzinfo] = field(default=None, compare=False)  # None: this computer's

    def start(self) -> datetime:
        """Once: when it starts (by the clock, aware)."""
        return local_time(self.day, self.begin, self.zone)

    def stop(self) -> datetime:
        """Once: when it stops, by the clock (across a clock change too)."""
        return local_time(self.day, self.begin + self.minutes, self.zone)


def _stepped(minutes: int, delta: int, step: int) -> int:
    """`minutes` a step on (or back) onto the grid of `step`: 21:03 → 21:05 (or 21:00)."""
    if delta > 0:
        return (minutes // step + 1) * step % _DAY_MINUTES
    return (-(-minutes // step) - 1) * step % _DAY_MINUTES


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class RecordTimerCard(FormCard):
    """Record at a time, a card over the picture (the TV's, or the Library's page)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._now: Callable[[], datetime] = datetime.now
        self._done: Optional[Callable[[TimerChoice, Optional[dict]], None]] = None
        self._channel_name = ""
        self._channels: Optional[list[dict]] = None
        self._names: list[str] = []
        self._lower: Optional[list[str]] = None  # the names to find in (made at the first find)
        self._find = ""
        self._matches: Optional[list[int]] = None  # the channels the find found (None: all)
        self._chosen = 0
        self._repeat = ONCE
        self._weekdays: set[int] = set()
        self._week_focus = 0  # the day the keys are on, among the days
        self._day = date.today()
        self._first_day = self._day
        self._begin = self._end = 0
        self._save_text = ""

    # ---- what its caller does -----------------------------------------------------------------

    def ask(
        self,
        channel_name: str,
        *,
        done: Callable[[TimerChoice, Optional[dict]], None],
        start: Optional[datetime] = None,
        stop: Optional[datetime] = None,
        now: Callable[[], datetime] = datetime.now,
        title: str = "",
        days: tuple[int, ...] = (),
        save_text: str = "",
        channels: Optional[list[dict]] = None,
        chosen: Optional[dict] = None,
    ) -> None:
        """Show it afresh (one still shown is replaced, unanswered). `start`/`stop` (naive
        local, or aware): the time it opens on (a programme's), else the next five minutes for
        an hour; `now()` is this computer's clock (naive local). `title` and `days` (Monday 0)
        fill it in (Edit…), `save_text` names its button; `channels` makes the channel a
        choice, on `chosen` (by itself, else by its name). `done(choice, channel)` once Record
        is pressed."""
        self.hide()
        self._now, self._done = now, done
        if start is None or stop is None:
            base = now().replace(second=0, microsecond=0)
            start = base + timedelta(minutes=-base.minute % STEP_MIN)
            stop = start + timedelta(hours=1)
        start, stop = (
            moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment
            for moment in (start, stop)
        )
        self._channel_name = channel_name
        self._set_channels(channels, chosen)
        self._day = start.date()
        # Today at the earliest; an item still recording past midnight keeps its own day.
        self._first_day = min(now().date(), start.date())
        self._begin = start.hour * 60 + start.minute
        self._end = stop.hour * 60 + stop.minute
        self._repeat, self._weekdays, self._week_focus = ONCE, set(), 0
        if days:  # 2.9.7: a timer's days, as its repeat says them
            chosen_days = tuple(sorted(set(days)))
            kinds = {tuple(range(7)): DAILY, tuple(range(5)): WEEKDAYS}
            self._repeat = kinds.get(chosen_days, DAYS)
            self._weekdays = {d for d in chosen_days if 0 <= d < 7}
        self._save_text = save_text
        self._set_rows(title)
        self._set_focus(("field", CHANNEL if self._channels is not None else NAME))

    def choice(self) -> Optional[TimerChoice]:
        """What to schedule; None when it cannot be (the hint says why)."""
        return self._problem()[0]

    def chosen_channel(self) -> Optional[dict]:
        """The channel chosen (`channels` given; None while what is typed names none)."""
        if self._channels is None or (self._matches is not None and not self._matches):
            return None
        return self._channels[self._chosen] if 0 <= self._chosen < len(self._channels) else None

    def channel_text(self) -> str:
        """What the Channel row shows: the channel given, as it is, or the one chosen (with its
        provider's name while the channels come from several), else what was typed."""
        if self._channels is None:
            return self._channel_name
        if self.chosen_channel() is None:
            return self._find
        return self._names[self._chosen]

    def rows(self) -> list[str]:
        """Its rows' keys, from the top, as they show now (the days for On these days, the day
        for Once)."""
        rows = [CHANNEL, NAME, REPEAT]
        if self._repeat == DAYS:
            rows.append(WEEK)
        if self._repeat == ONCE:
            rows.append(DAY)
        return rows + [FROM, TO]

    def begin(self) -> int:
        return self._begin

    def end(self) -> int:
        return self._end

    def day(self) -> date:
        return self._day

    def repeat(self) -> str:
        return self._repeat

    def days(self) -> tuple[int, ...]:
        """The days ticked (On these days)."""
        return tuple(sorted(self._weekdays))

    def hint(self) -> tuple[str, bool]:
        """The line under the rows, and whether it is only a note (False: why it can't
        record)."""
        choice, text = self._problem()
        return text, choice is not None

    def part_rect(self, part: str, key: str) -> QRectF:
        """Where a stepper's ‹ ("less") or › ("more"), or a day ("day", "0"…"6") is."""
        return QRectF(self._parts().get((part, key), QRectF()))

    # ---- what is set --------------------------------------------------------------------------

    def _set_channels(self, channels: Optional[list[dict]], chosen: Optional[dict]) -> None:
        self._channels = list(channels) if channels is not None else None
        self._lower, self._find, self._matches, self._chosen = None, "", None, 0
        if self._channels is None:
            self._names = []
            return
        providers = {str(c.get("_provider") or "") for c in self._channels}
        several = len(providers) > 1
        self._names = [
            (
                f"{c.get('name') or ''} · {c.get('_provider')}"
                if several and c.get("_provider")
                else str(c.get("name") or "")
            )
            for c in self._channels
        ]
        found = next(
            (k for k, c in enumerate(self._channels) if chosen is not None and c is chosen), -1
        )
        if found < 0 and chosen is not None:
            name = chosen.get("name")
            found = next((k for k, c in enumerate(self._channels) if c.get("name") == name), -1)
        self._chosen = max(0, found)

    def _weekday_choice(self) -> tuple[int, ...]:
        if self._repeat == DAILY:
            return tuple(range(7))
        if self._repeat == WEEKDAYS:
            return tuple(range(5))
        if self._repeat == DAYS:
            return self.days()
        return ()

    def _problem(self) -> tuple[Optional[TimerChoice], str]:
        """The choice made (None when it cannot be scheduled) and what the hint says."""
        minutes = (self._end - self._begin) % _DAY_MINUTES
        if minutes == 0:
            return None, translate(NO_LENGTH_TEXT)
        once = self._repeat == ONCE
        days = self._weekday_choice()
        if not once and not days:
            return None, translate(NO_DAY_TEXT)
        name = self._edits[NAME].text.strip() if NAME in self._edits else ""
        choice = TimerChoice(name, self._day, self._begin, minutes, days)
        if once and choice.stop() <= self._now().astimezone():
            return None, translate(PASSED_TEXT)
        if self._channels is not None and self.chosen_channel() is None:
            return None, translate(NO_CHANNEL_TEXT)
        return choice, translate(NEXT_DAY_TEXT) if self._end <= self._begin else ""

    def _set_rows(self, title: str = "") -> None:
        """The form as the repeat has it now (a row comes or goes), its name as typed."""
        name = self._edits[NAME].text if NAME in self._edits and not self.isHidden() else title
        fields = [
            Field(
                key,
                translate(_LABELS[key]) if _LABELS[key] else "",
                text=name if key == NAME else "",
                placeholder=translate(OPTIONAL_TEXT) if key == NAME else "",
            )
            for key in self.rows()
        ]
        buttons = [
            Button(CANCEL, translate(CANCEL_TEXT)),
            Button(RECORD, self._save_text or translate(RECORD_TEXT), accent=True),
        ]
        self.set_form(translate(TITLE_TEXT), fields, "", buttons, [])

    def _step(self, key: str, delta: int, page: bool = False) -> None:
        """The stepper `key` a step on (+) or back (-); `page`: a big one."""
        if key == REPEAT:
            kinds = [kind for kind, _text in REPEATS]
            at = kinds.index(self._repeat) + (1 if delta > 0 else -1)
            self._repeat = kinds[at % len(kinds)]
            self._set_rows()
        elif key == DAY:
            moved = self._day + timedelta(days=(PAGE_DAYS if page else 1) * delta)
            self._day = max(self._first_day, moved)
        elif key == FROM:
            self._begin = _stepped(self._begin, delta, PAGE_MIN if page else STEP_MIN)
        elif key == TO:
            self._end = _stepped(self._end, delta, PAGE_MIN if page else STEP_MIN)
        elif key == CHANNEL and self._channels:
            self._step_channel(delta * (PAGE_CHANNELS if page else 1))
        self.update()
        self._say()

    def _step_channel(self, delta: int) -> None:
        """The next (or an earlier) channel of those the find found (all of them while nothing
        is typed), round."""
        matches = self._matches
        if matches is None:
            self._chosen = (self._chosen + delta) % len(self._channels or (None,))
            return
        if not matches:
            return
        try:
            at = matches.index(self._chosen)
        except ValueError:
            at = -1 if delta > 0 else 0
        self._chosen = matches[(at + delta) % len(matches)]

    def _set_find(self, text: str) -> None:
        """What is typed to find a channel: those with it anywhere in their name, in any case;
        one named so exactly first, else the one chosen while it is among them, else the
        first. Typed on, the find looks only among those it found."""
        if self._lower is None:
            self._lower = [name.casefold() for name in self._names]
        lower = self._lower
        needle, before = text.casefold(), self._find.casefold()
        if not needle.strip():
            self._find, self._matches = text, None
            return
        narrower = self._matches is not None and before.strip() and needle.startswith(before)
        pool = self._matches if narrower and self._matches is not None else range(len(lower))
        matches = [k for k in pool if needle in lower[k]]
        self._find, self._matches = text, matches
        if not matches:
            return
        exact = next((k for k in matches if lower[k] == needle), None)
        if exact is not None:
            self._chosen = exact
        elif not (0 <= self._chosen < len(lower) and needle in lower[self._chosen]):
            self._chosen = matches[0]

    # ---- the keys -----------------------------------------------------------------------------

    def handle(self, action: Action, payload: object = "") -> bool:
        zone, key = self.focus()
        paging = action in (Action.NAV_PAGE_UP, Action.NAV_PAGE_DOWN)
        if paging and not self.isHidden() and zone == "field" and key in _STEPPERS:
            self._step(key, 1 if action is Action.NAV_PAGE_UP else -1, page=True)
            return True
        return super().handle(action, payload)

    def _horizontal(self, action: Action, payload: str) -> None:
        zone, key = self.focus()
        delta = -1 if action is Action.NAV_LEFT else 1
        if zone == "field" and key in _STEPPERS:
            self._step(key, delta)
        elif zone == "field" and key == WEEK:
            self._week_focus = max(0, min(6, self._week_focus + delta))
            self.update()
            self._say()
        else:
            super()._horizontal(action, payload)

    def _rows(self) -> list[tuple[str, str]]:
        rows = super()._rows()
        if self._channels is None:  # the channel given: only shown
            rows = [row for row in rows if row != ("field", CHANNEL)]
        return rows

    def _activate(self) -> None:
        if self.focus() == ("field", WEEK):
            self._tick(self._week_focus)
            return
        super()._activate()

    def _tick(self, weekday: int) -> None:
        self._weekdays ^= {weekday}
        self._week_focus = weekday
        self.update()
        self._say()

    def _field_key(self, key: str, action: Action, text: str) -> None:
        if key == NAME:
            super()._field_key(key, action, text)
            return
        if key == CHANNEL and self._channels is not None:
            if action is Action.NAV_TEXT and text:
                self._set_find(self._find + text)
            elif action is Action.PASTE:
                line = clipboard_line()
                if not line:
                    return
                self._set_find(self._find + line)
            elif action is Action.NAV_BACKSPACE:
                if not self._find:
                    self._cancel()  # nothing typed: Back
                    return
                self._set_find(self._find[:-1])
            elif action in (Action.NAV_DELETE, Action.FORGET):
                self._set_find("")
            else:
                return
            self.update()
            self._say()
            return
        if action is Action.NAV_BACKSPACE:
            self._cancel()  # on a stepper or the days, Backspace is Back

    def _back(self) -> None:
        """The player's Back (or Backspace off a field) cancels, but in the name."""
        if self.focus() != ("field", NAME):
            self._cancel()

    def _paste_target(self) -> str:
        return NAME

    def _enabled(self, zone: str, key: str) -> bool:
        if zone == "button" and key == RECORD and self.choice() is None:
            return False  # nothing to schedule yet: the line under the rows says why
        return super()._enabled(zone, key)

    def _pressed(self, key: str) -> None:
        if key != RECORD or self.isHidden():
            return
        choice = self.choice()
        if choice is None:
            return
        channel = self.chosen_channel()
        done, self._done = self._done, None
        self._drag.release()
        self.hide()
        if done is not None:
            done(choice, channel)

    def _value_text(self, key: str) -> str:
        if key == CHANNEL:
            return self.channel_text()
        if key == REPEAT:
            return translate(dict(REPEATS)[self._repeat])
        if key == DAY:
            return str(app_locale().toString(QDate(self._day), QLocale.FormatType.LongFormat))
        if key == FROM:
            return _clock(self._begin)
        if key == TO:
            return _clock(self._end)
        return ""

    @staticmethod
    def _day_name(weekday: int) -> str:
        return str(app_locale().dayName(weekday + 1, QLocale.FormatType.ShortFormat))

    def _say(self) -> None:
        """A stepper's label and value ("From, 21:00"), a day and whether it is ticked."""
        zone, key = self.focus()
        if zone == "field" and key in _STEPPERS:
            label = next((f.label for f in self._fields if f.key == key), "")
            announce(self, ", ".join(part for part in (label, self._value_text(key)) if part))
        elif zone == "field" and key == WEEK:
            ticked = "✓" if self._week_focus in self._weekdays else ""
            announce(self, " ".join(p for p in (self._day_name(self._week_focus), ticked) if p))
        else:
            super()._say()

    # ---- the layout and the painting ----------------------------------------------------------

    def _place(
        self, un: Units, fonts: dict[str, QFont], width: float, squeeze: float
    ) -> tuple[dict[tuple[str, str], QRectF], float]:
        """The form's places, the line that says why it can't record (or that it ends the next
        day) under the last row, everything under it moved down by its height."""
        rects, footer = super()._place(un, fonts, width, squeeze)
        text = self._problem()[1]
        if not text or not self._fields:
            return rects, footer
        under = rects[("field", self._fields[-1].key)]
        left, right = under.left(), rects[TITLE].right()
        top = under.bottom() + un.px(_HINT_GAP_U) * squeeze
        height = self._wrapped_height(fonts["found"], text, max(1.0, right - left))
        edge, shift = under.bottom(), top + height - under.bottom()
        for name, rect in list(rects.items()):
            if name not in (MIDDLE, PANEL) and rect.top() >= edge - 0.5:
                rects[name] = rect.translated(0, shift)
        rects[_HINT] = QRectF(left, top, max(1.0, right - left), height)
        for name in (MIDDLE, PANEL):
            rect = rects[name]
            rects[name] = QRectF(rect.left(), rect.top(), rect.width(), rect.height() + shift)
        return rects, footer + shift

    def _parts(self) -> dict[tuple[str, str], QRectF]:
        """Each stepper's ‹, value and ›, and each day's button, in the card."""
        rects = self._layout()
        parts: dict[tuple[str, str], QRectF] = {}
        for key in self.rows():
            box = rects.get(("field", key))
            if box is None or (key == CHANNEL and self._channels is None):
                continue
            if key in _STEPPERS:
                side = min(box.height(), box.width() / 4)
                parts[("less", key)] = QRectF(box.left(), box.top(), side, box.height())
                parts[("more", key)] = QRectF(box.right() - side, box.top(), side, box.height())
                parts[("value", key)] = box.adjusted(side, 0, -side, 0)
            elif key == WEEK:
                gap = float(self._units().px(_DAY_GAP_U))
                each = max(1.0, (box.width() - 6 * gap) / 7)
                for k in range(7):
                    x = box.left() + k * (each + gap)
                    parts[("day", str(k))] = QRectF(x, box.top(), each, box.height())
        return parts

    def _paint_field(
        self, p: QPainter, un: Units, fonts: dict[str, QFont], box: QRectF, f: Field
    ) -> None:
        if f.key == NAME:
            super()._paint_field(p, un, fonts, box, f)
            return
        focused = self.focus() == ("field", f.key)
        if f.key == WEEK:
            self._paint_days(p, un, fonts, focused)
            return
        if f.key == CHANNEL and self._channels is None:  # the channel given, as it is
            p.setFont(fonts["field"])
            p.setPen(qcolor(PALETTE.text))
            shown = QFontMetricsF(fonts["field"]).elidedText(
                self._channel_name, Qt.TextElideMode.ElideRight, box.width()
            )
            p.drawText(box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, shown)
            return
        radius = un.u * _RADIUS_U
        fill_round(p, box, qcolor(PALETTE.text, 0.08), radius)
        p.save()
        p.setPen(QPen(qcolor(PALETTE.accent, 0.8) if focused else qcolor(PALETTE.text, 0.18), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        p.restore()
        parts = self._parts()
        less, value, more = (parts[(name, f.key)] for name in ("less", "value", "more"))
        p.setFont(fonts["field"])
        if f.key == CHANNEL and self._find:
            value = self._paint_find(p, un, fonts, value)
            p.setFont(fonts["field"])
        missing = f.key == CHANNEL and self.chosen_channel() is None
        paint_value_steps(p, un, less, value, more, self._value_text(f.key), not missing)

    def _paint_find(self, p: QPainter, un: Units, fonts: dict[str, QFont], room: QRectF) -> QRectF:
        """What is typed to find a channel, muted after a ⌕ at the left of the value's room;
        the room left for the value."""
        size = float(un.px(_SEARCH_U))
        icon = QRectF(room.left(), room.center().y() - size / 2, size, size)
        muted = qcolor(PALETTE.text, PALETTE.text_muted_alpha)
        draw_icon(p, "search", icon, muted)
        fm = QFontMetricsF(fonts["field"])
        typed_w = min(fm.horizontalAdvance(self._find) + 2, room.width() / 3)
        typed = QRectF(icon.right() + un.px(0.3), room.top(), typed_w, room.height())
        p.setPen(muted)
        shown = fm.elidedText(self._find, Qt.TextElideMode.ElideLeft, typed.width())
        p.drawText(typed, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, shown)
        left = typed.right() + un.px(0.6)
        return QRectF(left, room.top(), max(0.0, room.right() - left), room.height())

    def _paint_days(self, p: QPainter, un: Units, fonts: dict[str, QFont], focused: bool) -> None:
        """The seven days, as buttons: a ticked one filled with the accent, the one the keys are
        on ringed while the row has them."""
        radius = un.u * _RADIUS_U
        p.setFont(fonts["button"])
        fm = QFontMetricsF(fonts["button"])
        for (part, key), rect in self._parts().items():
            if part != "day":
                continue
            weekday = int(key)
            p.save()
            if weekday in self._weekdays:
                fill_round(p, rect, qcolor(PALETTE.accent, 0.92), radius)
                tone = qcolor(PALETTE.canvas)
            else:
                if focused:
                    fill_round(p, rect, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA / 2), radius)
                p.setPen(QPen(qcolor(PALETTE.text, 0.25), 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
                tone = qcolor(PALETTE.text)
            if focused and weekday == self._week_focus:
                p.setPen(QPen(qcolor(PALETTE.text), 2.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), radius + 2, radius + 2)
            p.setPen(tone)
            name = fm.elidedText(self._day_name(weekday), Qt.TextElideMode.ElideRight, rect.width())
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, name)
            p.restore()

    def _paint_middle(
        self, p: QPainter, un: Units, fonts: dict[str, QFont], rects: dict[tuple[str, str], QRectF]
    ) -> None:
        super()._paint_middle(p, un, fonts, rects)
        hint = rects.get(_HINT)
        choice, text = self._problem()
        if hint is not None and text:
            p.setFont(fonts["found"])
            note = qcolor(PALETTE.text, PALETTE.text_muted_alpha)
            p.setPen(note if choice is not None else qcolor(PALETTE.warn))
            wrap = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap
            p.drawText(hint, int(wrap), text)

    # ---- the mouse ----------------------------------------------------------------------------

    def _part_at(self, pos: QPointF) -> Optional[tuple[str, str]]:
        """A stepper's ‹ or ›, or a day, under `pos` (None elsewhere)."""
        if not self._layout()[MIDDLE].contains(pos):
            return None
        for name, rect in self._parts().items():
            if name[0] != "value" and rect.contains(pos):
                return name
        return None

    def _mouse_press(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            part = self._part_at(pos)
            if part is not None:
                name, key = part
                if name == "day":
                    self._set_focus(("field", WEEK))
                    self._tick(int(key))
                else:
                    self._set_focus(("field", key))
                    self._step(key, -1 if name == "less" else 1)
                return
            hit = self._hit(pos)
            if hit is not None and hit[0] in ("field", "label") and hit[1] != NAME:
                if hit[1] != CHANNEL or self._channels is not None:
                    self._set_focus(("field", hit[1]))  # a stepper's value: the keys on it
                return
        super()._mouse_press(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._part_at(event.position()):
            self.mousePressEvent(event)  # quick clicks on ‹, › or a day are clicks
            return
        super().mouseDoubleClickEvent(event)
