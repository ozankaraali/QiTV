"""Record at a time (2.8, owner): a channel from one time to another, once on a day or on chosen
weekdays, with no guide needed. 2.13.1: a card over the picture in QiTV's look, no window: the
keys (the phone's arrows, OK and Back) and the mouse reach all of it, and it answers through a
callback."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import date, datetime, timedelta
import time
import unittest

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.keymap import Action
from ui.overlays.settings_card import CANCEL
from ui.record_timer_dialog import (
    DAILY,
    DAYS,
    NEXT_DAY_TEXT,
    NO_CHANNEL_TEXT,
    NO_DAY_TEXT,
    NO_LENGTH_TEXT,
    ONCE,
    PASSED_TEXT,
    RECORD,
    WEEKDAYS,
    RecordTimerCard,
    TimerChoice,
)

NOW = datetime(2026, 10, 26, 20, 7)  # a Monday, this computer's clock
DAY_MINUTES = 24 * 60
app = QApplication.instance() or QApplication([])


class RecordTimerCardTests(unittest.TestCase):
    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.card = RecordTimerCard(self.host)
        self.answers = []

    def ask(self, name="News One", **kw):
        kw.setdefault("now", lambda: NOW)
        done = lambda choice, channel: self.answers.append((choice, channel))  # noqa: E731
        self.card.ask(name, done=done, **kw)
        return self.card

    def key(self, action, payload="", times=1):
        for _ in range(times):
            self.assertTrue(self.card.handle(action, payload))

    def click(self, point: QPointF):
        QTest.mouseClick(self.card, Qt.MouseButton.LeftButton, pos=point.toPoint())
        QTest.qWait(QApplication.doubleClickInterval() + 50)  # never a double-click

    def go(self, row):
        """The keys onto the row `row` (↑ to the top first, then ↓)."""
        self.key(Action.NAV_UP, times=10)
        for _ in range(10):
            if self.card.focus() == ("field", row):
                return
            self.key(Action.NAV_DOWN)
        self.fail(f"no row {row}: {self.card.focus()}")

    def step_to(self, row, minutes):
        """From or To stepped by ←/→ (5 minutes each; Page Up/Down an hour) to `minutes`."""
        self.go(row)
        value = self.card.begin if row == "from" else self.card.end
        for _ in range(400):
            if value() == minutes:
                return
            ahead = (minutes - value()) % DAY_MINUTES
            if 60 <= ahead <= DAY_MINUTES // 2:
                self.key(Action.NAV_PAGE_UP)
            elif ahead > DAY_MINUTES // 2 and DAY_MINUTES - ahead >= 60:
                self.key(Action.NAV_PAGE_DOWN)
            else:
                self.key(Action.NAV_RIGHT if ahead <= DAY_MINUTES // 2 else Action.NAV_LEFT)
        self.fail(f"{row} never reached {minutes}: {value()}")

    def type(self, text):
        for letter in text:
            self.key(Action.NAV_TEXT, letter)

    def test_it_opens_on_the_next_five_minutes_for_an_hour(self):
        card = self.ask()
        self.assertTrue(card.isVisible())
        self.assertEqual(card.title(), "Record at a time")
        self.assertEqual((card.begin(), card.end()), (20 * 60 + 10, 21 * 60 + 10))
        self.assertEqual(card.day(), date(2026, 10, 26))
        self.assertEqual(card.repeat(), ONCE)
        self.assertEqual(card.focus(), ("field", "name"))  # the channel is given: its name
        self.assertEqual(card.channel_text(), "News One")
        self.assertEqual(card.rows(), ["channel", "name", "repeat", "day", "from", "to"])
        self.assertEqual(
            [(b.key, b.text, b.accent) for b in card._buttons],
            [(CANCEL, "Cancel", False), (RECORD, "Record", True)],
        )

    def test_it_opens_on_a_programme_s_time_exactly(self):
        card = self.ask(start=datetime(2026, 10, 27, 21, 3), stop=datetime(2026, 10, 27, 22, 17))
        self.assertEqual((card.begin(), card.end()), (21 * 60 + 3, 22 * 60 + 17))
        self.assertEqual(card.day(), date(2026, 10, 27))
        choice = card.choice()  # untouched: as the programme's
        self.assertEqual((choice.begin, choice.minutes, choice.day), (1263, 74, date(2026, 10, 27)))
        self.go("from")
        self.key(Action.NAV_RIGHT)  # a step lands on the five minutes' grid
        self.assertEqual(card.begin(), 21 * 60 + 5)
        self.key(Action.NAV_LEFT, times=2)
        self.assertEqual(card.begin(), 20 * 60 + 55)

    def test_once_on_a_day_past_midnight(self):
        card = self.ask()
        self.type("  Late film ")
        self.step_to("from", 23 * 60 + 30)
        self.step_to("to", 60)  # past midnight: it ends the next day
        choice = card.choice()
        self.assertEqual(choice.title, "Late film")
        self.assertEqual((choice.begin, choice.minutes, choice.days), (23 * 60 + 30, 90, ()))
        self.assertEqual(choice.start(), datetime(2026, 10, 26, 23, 30).astimezone())
        self.assertEqual(choice.stop(), datetime(2026, 10, 27, 1, 0).astimezone())
        self.assertEqual(card.hint(), ("Ends the next day.", True))
        self.assertEqual(NEXT_DAY_TEXT, "Ends the next day.")

    def test_repeats(self):
        card = self.ask()
        self.step_to("from", 21 * 60)
        self.step_to("to", 22 * 60)
        self.go("repeat")
        self.key(Action.NAV_RIGHT)
        self.assertEqual((card.repeat(), card.choice().days), (DAILY, tuple(range(7))))
        self.assertNotIn("day", card.rows())  # a day is for once only
        self.key(Action.NAV_RIGHT)
        self.assertEqual((card.repeat(), card.choice().days), (WEEKDAYS, (0, 1, 2, 3, 4)))
        self.key(Action.NAV_RIGHT)
        self.assertEqual(card.repeat(), DAYS)
        self.assertIn("days", card.rows())
        self.assertIsNone(card.choice())  # no day chosen yet
        self.assertEqual(card.hint(), (NO_DAY_TEXT, False))
        self.assertFalse(card._enabled("button", RECORD))
        self.go("name")
        self.key(Action.NAV_ACTIVATE)  # Enter in a field presses Record: not while it can't
        self.assertEqual(self.answers, [])
        self.assertTrue(card.isVisible())
        self.go("days")
        self.key(Action.NAV_RIGHT, times=2)
        self.key(Action.NAV_ACTIVATE)  # Enter on a day ticks it (never Record)
        self.key(Action.NAV_RIGHT, times=3)
        self.key(Action.OK)  # the phone's OK too
        self.assertEqual(self.answers, [])
        self.assertEqual(card.choice().days, (2, 5))
        self.assertTrue(card._enabled("button", RECORD))
        self.key(Action.NAV_ACTIVATE)  # again: off
        self.assertEqual(card.choice().days, (2,))
        self.go("repeat")
        self.key(Action.NAV_RIGHT)
        self.assertEqual(card.repeat(), ONCE)  # round again
        self.assertIn("day", card.rows())

    def test_a_time_gone_by_or_of_no_length_cannot_be_scheduled(self):
        card = self.ask()
        self.step_to("from", 19 * 60)
        self.step_to("to", 20 * 60)  # today, over
        self.assertIsNone(card.choice())
        self.assertEqual(card.hint(), (PASSED_TEXT, False))
        self.assertFalse(card._enabled("button", RECORD))
        self.go("day")
        self.key(Action.NAV_RIGHT)  # tomorrow: fine
        self.assertEqual(card.day(), date(2026, 10, 27))
        self.assertIsNotNone(card.choice())
        self.step_to("to", 19 * 60)
        self.assertIsNone(card.choice())
        self.assertEqual(card.hint(), (NO_LENGTH_TEXT, False))
        self.go("repeat")
        self.key(Action.NAV_RIGHT)  # every day: today's being over is fine
        self.step_to("to", 20 * 60)
        self.assertIsNotNone(card.choice())

    def test_one_on_now_can_be_scheduled(self):
        card = self.ask()
        self.step_to("from", 20 * 60)
        self.step_to("to", 21 * 60)  # began 7 minutes ago
        self.assertIsNotNone(card.choice())

    def test_once_ends_at_its_clock_time_across_a_clock_change(self):  # 2.8 review 5
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            berlin = ZoneInfo("Europe/Berlin")
        except ZoneInfoNotFoundError:
            self.skipTest("no time zone database")
        # 01:00–04:00 the night the clocks go back
        choice = TimerChoice("", date(2026, 10, 25), 60, 180, (), zone=berlin)
        self.assertEqual(choice.stop().timestamp() - choice.start().timestamp(), 4 * 3600)
        self.assertEqual(choice.stop().astimezone(berlin).hour, 4)

    def test_the_day_cannot_be_before_today(self):
        card = self.ask()
        self.go("day")
        self.key(Action.NAV_LEFT, times=3)
        self.assertEqual(card.day(), date(2026, 10, 26))
        self.key(Action.NAV_PAGE_UP)  # a week on
        self.assertEqual(card.day(), date(2026, 11, 2))
        self.key(Action.NAV_PAGE_DOWN, times=2)  # back, never before today
        self.assertEqual(card.day(), date(2026, 10, 26))

    def test_record_answers_through_the_callback_and_hides(self):
        card = self.ask()
        self.go("to")
        self.key(Action.NAV_DOWN)  # the buttons, on Record
        self.assertEqual(card.focus(), ("button", RECORD))
        self.key(Action.NAV_ACTIVATE)
        self.assertTrue(card.isHidden())
        ((choice, channel),) = self.answers
        self.assertIsNone(channel)  # the channel was given: the caller knows it
        self.assertEqual(choice.start(), (NOW + timedelta(minutes=3)).astimezone())
        self.assertFalse(card.handle(Action.NAV_ACTIVATE))  # hidden: no key is its own

    def test_cancel_esc_back_and_the_picture_answer_nothing(self):
        for way in ("esc", "cancel", "back", "picture"):
            with self.subTest(way=way):
                card = self.ask()
                if way == "esc":
                    self.key(Action.ESCAPE)
                elif way == "cancel":
                    self.go("to")
                    self.key(Action.NAV_DOWN)
                    self.key(Action.NAV_LEFT)
                    self.assertEqual(card.focus(), ("button", CANCEL))
                    self.key(Action.NAV_ACTIVATE)
                elif way == "back":
                    self.go("repeat")
                    self.key(Action.NAV_BACKSPACE)  # the phone's Back, on a stepper
                else:
                    self.click(QPointF(5, 5))  # the dimmed picture
                self.assertTrue(card.isHidden())
                self.assertEqual(self.answers, [])
        card = self.ask()
        self.go("name")
        self.type("ab")
        self.key(Action.NAV_BACKSPACE)  # in the name, Backspace deletes
        self.assertTrue(card.isVisible())
        self.assertEqual(card.values()["name"], "a")

    def test_the_mouse_steps_ticks_and_records(self):
        card = self.ask()
        self.click(card.part_rect("more", "from").center())
        self.assertEqual(card.begin(), 20 * 60 + 15)
        self.assertEqual(card.focus(), ("field", "from"))
        self.click(card.part_rect("less", "to").center())
        self.assertEqual(card.end(), 21 * 60 + 5)
        for _ in range(3):  # Once → On these days
            self.click(card.part_rect("more", "repeat").center())
        self.assertEqual(card.repeat(), DAYS)
        self.click(card.part_rect("day", "4").center())
        self.assertEqual(card.choice().days, (4,))
        self.assertEqual(card.focus(), ("field", "days"))
        self.click(card.control_rect("button", RECORD).center())
        self.assertTrue(card.isHidden())
        self.assertEqual(self.answers[0][0].days, (4,))

    def test_no_text_is_cut_short_in_a_small_window(self):
        self.host.resize(640, 360)
        card = self.ask(start=datetime(2026, 10, 28, 21, 0), stop=datetime(2026, 10, 28, 22, 0))
        card.grab()
        for row in card.rows():
            with self.subTest(row=row):
                self.assertFalse(card.control_rect("field", row).isEmpty())
        self.assertFalse(card.control_rect("button", RECORD).isEmpty())
        self.assertTrue(card.card_rect().contains(card.control_rect("button", RECORD)))

    # ---- 2.9.7: the Recordings page's Edit… and Record at a time… ------------------------------

    def test_edit_opens_filled_in_with_the_timer_and_saves(self):
        start = datetime(2026, 10, 26, 21, 0)
        card = self.ask(
            start=start,
            stop=start + timedelta(minutes=90),
            title="Late show",
            days=(0, 2),
            save_text="Save",
        )
        self.assertEqual(card.values()["name"], "Late show")
        self.assertEqual(card.repeat(), DAYS)
        self.assertEqual(card.days(), (0, 2))
        self.assertEqual(card._buttons[-1].text, "Save")
        choice = card.choice()
        self.assertEqual((choice.begin, choice.minutes, choice.days), (21 * 60, 90, (0, 2)))
        card.hide()
        self.assertEqual(self.ask(days=tuple(range(5))).repeat(), WEEKDAYS)

    def test_a_channel_is_chosen_from_the_list_by_typing_its_name(self):
        channels = [{"name": "News One"}, {"name": "Films"}, {"name": "Music"}, {"name": "Music 2"}]
        card = self.ask("Films", channels=channels, chosen=channels[1])
        self.assertEqual(card.focus(), ("field", "channel"))  # its channel first
        self.assertIs(card.chosen_channel(), channels[1])  # the one playing first
        self.type("Nothing like it")
        self.assertIsNone(card.chosen_channel())
        self.assertIsNone(card.choice())
        self.assertEqual(card.hint(), (NO_CHANNEL_TEXT, False))
        self.assertEqual(NO_CHANNEL_TEXT, "Choose a channel from the list.")
        self.assertFalse(card._enabled("button", RECORD))
        self.key(Action.NAV_BACKSPACE, times=len("Nothing like it"))
        # nothing typed: the last one found ("N…": News One), every channel to step through
        self.assertIs(card.chosen_channel(), channels[0])
        self.assertTrue(card.isVisible())
        self.type("mus")  # anywhere in its name, in any case
        self.assertIs(card.chosen_channel(), channels[2])
        self.key(Action.NAV_RIGHT)  # the next of those found
        self.assertIs(card.chosen_channel(), channels[3])
        self.key(Action.NAV_RIGHT)  # round to the first
        self.assertIs(card.chosen_channel(), channels[2])
        self.assertTrue(card._enabled("button", RECORD))
        self.go("to")
        self.key(Action.NAV_DOWN)
        self.key(Action.NAV_ACTIVATE)  # Record
        ((_choice, channel),) = self.answers
        self.assertIs(channel, channels[2])

    def test_of_two_channels_with_one_name_the_one_chosen_is_scheduled(self):  # 2.9.7
        channels = [{"name": "Films HD", "id": 1}, {"name": "News"}, {"name": "Films HD", "id": 2}]
        card = self.ask(channels=channels, chosen=channels[2])
        self.assertIs(card.chosen_channel(), channels[2])  # the one playing, not its namesake
        self.key(Action.NAV_RIGHT)
        self.assertIs(card.chosen_channel(), channels[0])
        two = [dict(c, _provider=p) for c, p in zip(channels, ("A", "B", "A"))]
        card.hide()
        card = self.ask(channels=two, chosen=two[0])
        self.assertEqual(card.channel_text(), "Films HD · A")  # two providers: whose it is

    def test_every_providers_channels_open_at_once(self):  # 2.9.7: 200,000 across providers
        channels = [{"name": f"Channel {k}"} for k in range(200_000)]
        started = time.monotonic()
        card = self.ask(channels=channels, chosen=channels[-1])
        card.grab()  # painted as it shows
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIs(card.chosen_channel(), channels[-1])
        started = time.monotonic()
        self.type("Channel 1999")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIs(card.chosen_channel(), channels[1999])


if __name__ == "__main__":
    unittest.main()
