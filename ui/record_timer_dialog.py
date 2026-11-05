"""Record at a time (2.8): a channel recorded from one time to another, once on a day or on
chosen weekdays, with no guide needed (`services.recording_schedule`'s timers).

Opened from a channel's right-click and the guide's footer (on its channel; with the footer's
programme's time when there is one). Times are this computer's clock's; a time whose end comes
before its start ends the next day. Once on a day that is over cannot be scheduled.

2.9.7, the Recordings page: its Edit… opens it filled in with a scheduled recording (its name,
its days; Save), and its Record at a time… with the channels to choose from (`channels`: typed
to find one, the one playing first).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Callable, Optional

from PySide6.QtCore import QDate, QLocale, Qt, QTime
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTimeEdit,
    QWidget,
)

from services.i18n import N_, app_locale, tr, translate
from services.recording_schedule import local_time

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
STEP_MIN = 5  # it opens on the next five minutes


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


def _minutes(value: QTime) -> int:
    return value.hour() * 60 + value.minute()


class RecordTimerDialog(QDialog):
    def __init__(
        self,
        channel_name: str,
        *,
        start: Optional[datetime] = None,
        stop: Optional[datetime] = None,
        now: Callable[[], datetime] = datetime.now,
        parent: Optional[QWidget] = None,
        title: str = "",
        days: tuple[int, ...] = (),
        save_text: str = "",
        channels: Optional[list[dict]] = None,
        chosen: Optional[dict] = None,
    ) -> None:
        """`start`/`stop` (naive local, or aware): the time it opens on (a programme's), else
        the next five minutes for an hour. `now()` is this computer's clock (naive local).
        2.9.7: `title` and `days` (Monday 0) fill it in (Edit…), `save_text` names its button;
        `channels` makes the channel a choice, on `chosen` (`chosen_channel`)."""
        super().__init__(parent)
        self._now = now
        self.setWindowTitle(translate(TITLE_TEXT))
        if start is None or stop is None:
            base = now().replace(second=0, microsecond=0)
            start = base + timedelta(minutes=-base.minute % STEP_MIN)
            stop = start + timedelta(hours=1)
        start, stop = (
            moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment
            for moment in (start, stop)
        )
        self._channels = list(channels) if channels is not None else None
        self.channel_choice: Optional[QComboBox] = None
        if self._channels is not None:
            self.channel_choice = self._channel_choice(self._channels, chosen)
            self.channel: QWidget = self.channel_choice
        else:
            label = QLabel(channel_name, self)
            label.setTextFormat(Qt.TextFormat.PlainText)  # a provider's name, as it is
            self.channel = label
        self.name = QLineEdit(self)
        self.name.setText(title)
        # i18n: the placeholder of "Record at a time"'s Name field
        self.name.setPlaceholderText(tr("Optional"))
        self.repeat = QComboBox(self)
        for kind, text in REPEATS:
            self.repeat.addItem(translate(text), kind)
        locale = app_locale()
        self.days_row = QWidget(self)
        days_layout = QHBoxLayout(self.days_row)
        days_layout.setContentsMargins(0, 0, 0, 0)
        self.day_buttons: list[QPushButton] = []
        for weekday in range(7):
            button = QPushButton(locale.dayName(weekday + 1, QLocale.FormatType.ShortFormat))
            button.setCheckable(True)
            button.toggled.connect(self._check)
            days_layout.addWidget(button)
            self.day_buttons.append(button)
        self.day = QDateEdit(QDate(start.date()), self)
        self.day.setCalendarPopup(True)
        self.day.setMinimumDate(QDate(now().date()))
        self.begin = QTimeEdit(QTime(start.hour, start.minute), self)
        self.end = QTimeEdit(QTime(stop.hour, stop.minute), self)
        for edit in (self.begin, self.end):
            edit.setDisplayFormat("HH:mm")
        self.hint = QLabel(self)
        # i18n: the button of "Record at a time" that adds the recording to the schedule
        self.schedule_button = QPushButton(save_text or tr("Schedule"), self)
        self.schedule_button.setDefault(True)
        self.schedule_button.clicked.connect(self.accept)
        cancel = QPushButton(tr("Cancel"), self)  # i18n: a button
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.schedule_button)
        form = self._form = QFormLayout(self)
        form.addRow(tr("Channel"), self.channel)  # i18n: a field's label
        form.addRow(tr("Name"), self.name)  # i18n: a field's label: what it is called
        form.addRow(tr("Repeat"), self.repeat)  # i18n: a field's label: how often it records
        form.addRow("", self.days_row)
        form.addRow(tr("Day"), self.day)  # i18n: a field's label: the date it records on
        form.addRow(tr("From"), self.begin)  # i18n: a field's label: the time it starts
        form.addRow(tr("To"), self.end)  # i18n: a field's label: the time it stops
        form.addRow("", self.hint)
        form.addRow(buttons)
        if days:  # 2.9.7: a timer's days, as its repeat says them
            chosen_days = tuple(sorted(set(days)))
            kind = {tuple(range(7)): DAILY, tuple(range(5)): WEEKDAYS}.get(chosen_days, DAYS)
            self.repeat.setCurrentIndex(self.repeat.findData(kind))
            for weekday in chosen_days:
                if 0 <= weekday < 7:
                    self.day_buttons[weekday].setChecked(True)
        self.repeat.currentIndexChanged.connect(self._check)
        self.day.dateChanged.connect(self._check)
        self.begin.timeChanged.connect(self._check)
        self.end.timeChanged.connect(self._check)
        self._check()

    def _days(self) -> tuple[int, ...]:
        kind = self.repeat.currentData()
        if kind == DAILY:
            return tuple(range(7))
        if kind == WEEKDAYS:
            return tuple(range(5))
        if kind == DAYS:
            return tuple(k for k, button in enumerate(self.day_buttons) if button.isChecked())
        return ()

    def _problem(self) -> tuple[Optional[TimerChoice], str]:
        """The choice made (None when it cannot be scheduled) and what the hint says."""
        begin, end = _minutes(self.begin.time()), _minutes(self.end.time())
        minutes = (end - begin) % (24 * 60)
        if minutes == 0:
            return None, translate(NO_LENGTH_TEXT)
        once = self.repeat.currentData() == ONCE
        days = self._days()
        if not once and not days:
            return None, translate(NO_DAY_TEXT)
        day = self.day.date()
        on = date(day.year(), day.month(), day.day())
        choice = TimerChoice(self.name.text().strip(), on, begin, minutes, days)
        if once and choice.stop() <= self._now().astimezone():
            return None, translate(PASSED_TEXT)
        return choice, translate(NEXT_DAY_TEXT) if end <= begin else ""

    def _check(self, *_args: object) -> None:
        once = self.repeat.currentData() == ONCE
        self.day.setEnabled(once)
        self._form.setRowVisible(self.day, once)  # a day is for once only
        self.days_row.setVisible(self.repeat.currentData() == DAYS)
        choice, hint = self._problem()
        if choice is not None and self.channel_choice is not None and self.chosen_channel() is None:
            # i18n: a hint in "Record at a time": what was typed names no channel
            choice, hint = None, tr("Choose a channel from the list.")
        self.hint.setText(hint)
        self.schedule_button.setEnabled(choice is not None)

    def choice(self) -> Optional[TimerChoice]:
        """What to schedule; None when it cannot be (the hint says why)."""
        return self._problem()[0]

    # ---- 2.9.7: a channel to choose -------------------------------------------------------------

    def _channel_choice(self, channels: list[dict], chosen: Optional[dict]) -> QComboBox:
        """The channels by name (with their provider's when they come from several), typed
        to find one anywhere in its name."""
        providers = {str(c.get("_provider") or "") for c in channels}
        box = QComboBox(self)
        box.setEditable(True)
        box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        box.setMaxVisibleItems(15)
        for channel in channels:
            name = str(channel.get("name") or "")
            if len(providers) > 1 and channel.get("_provider"):
                name = f"{name} · {channel.get('_provider')}"
            box.addItem(name)
        completer = QCompleter(box.model(), box)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        box.setCompleter(completer)
        found = next((k for k, c in enumerate(channels) if chosen is not None and c is chosen), -1)
        if found < 0 and chosen is not None:
            found = next(
                (k for k, c in enumerate(channels) if c.get("name") == chosen.get("name")), -1
            )
        box.setCurrentIndex(max(0, found))
        box.currentTextChanged.connect(self._check)
        return box

    def chosen_channel(self) -> Optional[dict]:
        """The channel chosen (`channels` given; None while what is typed names none)."""
        box = self.channel_choice
        if box is None or self._channels is None:
            return None
        index = box.findText(box.currentText(), Qt.MatchFlag.MatchExactly)
        return self._channels[index] if 0 <= index < len(self._channels) else None
