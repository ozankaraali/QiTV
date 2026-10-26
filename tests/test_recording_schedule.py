"""Scheduled recordings (2.8): what is to be recorded, when each is due, and what became of it."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services import recording_schedule as rs
from services.epg_view import Programme

T0 = datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)
CHANNEL = {
    "id": "7",
    "name": "News One",
    "cmd": "http://panel.test/live/u/p/7.ts",
    "_provider": "P",
}


def local(day, hour, minute=0, second=0):
    """A moment of this computer's clock (timers keep its times), in Unix seconds."""
    return datetime(2026, 10, day, hour, minute, second).astimezone().timestamp()


def at(when):
    """`when` (local Unix seconds) as a clock that returns it."""
    return lambda: when


def programme(title, start_min, length_min=30):
    start = T0 + timedelta(minutes=start_min)
    return Programme(title, start, start + timedelta(minutes=length_min), "", None)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="qitv-schedule-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.schedule = rs.Schedule(
            str(self.folder / rs.FILE_NAME), write=rs.write_now, clock=lambda: T0.timestamp()
        )

    def test_a_programme_is_recorded_from_a_minute_before_to_three_after(self):
        item = self.schedule.add_once(CHANNEL, programme("The News", 30))
        start, stop = rs.window(item)
        self.assertEqual(start, (T0 + timedelta(minutes=29)).timestamp())
        self.assertEqual(stop, (T0 + timedelta(minutes=63)).timestamp())
        self.assertEqual(item["provider"], "P")
        self.assertEqual(item["channel"]["name"], "News One")

    def test_it_is_due_in_its_window_only(self):
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        guide = lambda channel, since: []  # noqa: E731
        self.assertEqual(self.schedule.due((T0 + timedelta(minutes=28)).timestamp(), guide), [])
        (job,) = self.schedule.due((T0 + timedelta(minutes=29)).timestamp(), guide)
        self.assertEqual(job.title, "The News")
        self.assertEqual(self.schedule.due((T0 + timedelta(minutes=64)).timestamp(), guide), [])

    def test_a_started_job_is_not_due_again(self):
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        now = (T0 + timedelta(minutes=30)).timestamp()
        (job,) = self.schedule.due(now, lambda c, s: [])
        self.schedule.started(job)
        self.assertEqual(self.schedule.due(now, lambda c, s: []), [])

    def test_a_series_records_every_airing_of_its_title_on_its_channel(self):
        self.schedule.add_series(CHANNEL, "The News")
        airings = [programme("The News", 30), programme("Weather", 60), programme("the news", 120)]
        guide = lambda channel, since: airings  # noqa: E731
        (first,) = self.schedule.due((T0 + timedelta(minutes=30)).timestamp(), guide)
        self.assertEqual((first.title, first.start), ("The News", airings[0].start.timestamp()))
        self.schedule.started(first)
        self.assertEqual(self.schedule.due((T0 + timedelta(minutes=31)).timestamp(), guide), [])
        (second,) = self.schedule.due((T0 + timedelta(minutes=121)).timestamp(), guide)
        self.assertEqual(second.start, airings[2].start.timestamp())

    def test_a_series_is_found_among_short_programmes(self):  # 2.8 review M6
        # Five-minute programmes all day: the guide lists 48 at a time from the moment asked;
        # asked six hours back, the airing now was never among them.
        self.schedule.add_series(CHANNEL, "Bulletin")
        day = [programme("Filler", k * 5, 5) for k in range(-12 * 12, 12 * 12)]
        day[12 * 12] = programme("Bulletin", 0, 5)  # at T0

        def guide(channel, since):
            return [p for p in day if p.stop.timestamp() > since][:48]

        (job,) = self.schedule.due(T0.timestamp() + 30, guide)
        self.assertEqual(job.title, "Bulletin")

    def test_a_once_recording_leaves_the_list_when_it_ended_and_its_outcome_stays(self):
        item = self.schedule.add_once(CHANNEL, programme("The News", 30))
        (job,) = self.schedule.due((T0 + timedelta(minutes=30)).timestamp(), lambda c, s: [])
        self.schedule.started(job)
        self.schedule.finished(job, rs.RECORDED, path="/rec/News One 2026-10-26 20.29.ts")
        self.assertEqual(self.schedule.items(), [])
        (outcome,) = self.schedule.outcomes()
        self.assertEqual((outcome["state"], outcome["title"]), (rs.RECORDED, "The News"))
        self.assertEqual(self.schedule.title_of("/rec/News One 2026-10-26 20.29.ts"), "The News")
        self.assertNotIn(item["id"], [i["id"] for i in self.schedule.items()])

    def test_what_was_missed_while_qitv_was_closed_is_noted_once(self):
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        missed = self.schedule.missed_before((T0 + timedelta(hours=3)).timestamp())
        self.assertEqual([m["title"] for m in missed], ["The News"])
        self.assertEqual(self.schedule.items(), [])
        self.assertEqual(self.schedule.outcomes()[0]["state"], rs.MISSED)
        self.assertEqual(self.schedule.missed_before((T0 + timedelta(hours=4)).timestamp()), [])

    def test_the_schedule_is_kept_on_disk_and_read_back(self):
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.schedule.add_series(CHANNEL, "Weather")
        again = rs.Schedule(str(self.folder / rs.FILE_NAME), write=rs.write_now)
        self.assertEqual([i["title"] for i in again.items()], ["The News", "Weather"])

    def test_a_file_of_another_version_or_broken_reads_as_empty(self):
        path = self.folder / rs.FILE_NAME
        path.write_text('{"version": 99, "items": [{"title": "x"}]}')
        self.assertEqual(rs.Schedule(str(path), write=rs.write_now).items(), [])
        path.write_text("not json")
        self.assertEqual(rs.Schedule(str(path), write=rs.write_now).items(), [])

    def test_saves_land_in_order_the_newest_last(self):  # 2.8 review M2
        written = []
        slow = [0.2]  # the first write takes longest

        def write_now(path, data):
            time.sleep(slow.pop() if slow else 0)
            written.append(len(data["items"]))

        with mock.patch.object(rs, "write_now", write_now):
            schedule = rs.Schedule(
                str(self.folder / "other.json"),
                write=rs.write_later,
                clock=lambda: T0.timestamp(),  # the shows have not ended, whatever today is
            )
            for minutes in range(5):
                schedule.add_once(CHANNEL, programme(f"Show {minutes}", 60 * minutes))
            rs.flush_writes(5)
        self.assertEqual(written[-1], 5)

    def test_a_programme_that_ended_is_not_scheduled(self):  # 2.8 review M7
        ended = programme("Old News", -60)  # an hour before T0, half an hour long
        self.assertIsNone(self.schedule.add_once(CHANNEL, ended))
        self.assertEqual(self.schedule.items(), [])

    def test_the_file_keeps_no_xtream_account(self):  # 2.8 review M15
        account = {"username": "u", "password": "p"}
        self.schedule.add_once(CHANNEL, programme("The News", 30), account=account)
        self.schedule.add_series(CHANNEL, "Weather", account=account)
        text = (self.folder / rs.FILE_NAME).read_text()
        self.assertNotIn("/u/p/", text)
        for item in rs.Schedule(str(self.folder / rs.FILE_NAME)).items():
            self.assertEqual(rs.with_account(item["channel"], account)["cmd"], CHANNEL["cmd"])
        # a new password since: the address uses the one the provider has now
        changed = rs.with_account(item["channel"], {"username": "u", "password": "q"})
        self.assertEqual(changed["cmd"], "http://panel.test/live/u/q/7.ts")

    def test_hand_edited_items_of_a_wrong_shape_are_left_out(self):  # 2.8 review M8
        path = self.folder / rs.FILE_NAME
        good = {
            "id": "a1",
            "kind": "once",
            "channel": CHANNEL,
            "title": "T",
            "start": 1.0,
            "stop": 2.0,
        }
        bad = [
            {"id": "b1", "kind": "once", "channel": CHANNEL, "title": "T", "start": "x"},
            {"id": "b2", "kind": "series", "channel": "not a channel", "title": "T"},
            {"id": "b3", "kind": "sometimes", "channel": CHANNEL, "title": "T"},
            {"kind": "series", "channel": CHANNEL, "title": "T"},
        ]
        import json

        path.write_text(json.dumps({"version": 1, "items": [good, *bad], "done": 5, "outcomes": 3}))
        schedule = rs.Schedule(str(path))
        self.assertEqual([i["id"] for i in schedule.items()], ["a1"])
        self.assertEqual(schedule.outcomes(), [])

    def test_a_longer_slot_at_the_same_start_extends_the_one_scheduled(self):  # review 2
        # "Record at a time" opened on a programme, its end made later: one recording, to the
        # later end (a shorter one asked again leaves it as long).
        news = programme("The News", 30)
        first = self.schedule.add_once(CHANNEL, news)
        longer = Programme("20:30–22:00", news.start, news.stop + timedelta(minutes=60))
        again = self.schedule.add_once(CHANNEL, longer)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["stop"], longer.stop.timestamp())
        self.schedule.add_once(CHANNEL, news)
        (kept,) = self.schedule.items()
        self.assertEqual(kept["stop"], longer.stop.timestamp())

    def test_cancelling_removes_it(self):
        item = self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.schedule.cancel(item["id"])
        self.assertEqual(self.schedule.items(), [])

    def test_a_programme_is_known_as_scheduled(self):
        news = programme("The News", 30)
        self.schedule.add_once(CHANNEL, news)
        self.assertTrue(self.schedule.is_scheduled(CHANNEL, news))
        self.assertFalse(self.schedule.is_scheduled(CHANNEL, programme("Weather", 60)))
        self.schedule.add_series(CHANNEL, "Weather")
        self.assertTrue(self.schedule.is_scheduled(CHANNEL, programme("weather", 600)))


class TimerTests(unittest.TestCase):  # 2.8 (owner): a time on chosen days, no guide needed
    # 2026-10-26 is a Monday (weekday 0).
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="qitv-timers-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.path = str(self.folder / rs.FILE_NAME)
        self.schedule = rs.Schedule(self.path, write=rs.write_now, clock=at(local(26, 12)))

    def due(self, when):
        return self.schedule.due(when, lambda channel, since: [])  # no guide at all

    def test_a_timer_records_its_time_on_its_days(self):
        self.schedule.add_timer(CHANNEL, begin=21 * 60, minutes=90, days=[0, 2])  # Mon, Wed
        (job,) = self.due(local(26, 21, 30))
        self.assertEqual((job.start, job.stop), (local(26, 21), local(26, 22, 30)))
        self.assertEqual(job.title, "21:00–22:30")  # no name: its time
        self.assertEqual(job.channel["name"], "News One")
        self.assertEqual(self.due(local(27, 21, 30)), [])  # Tuesday
        (wednesday,) = self.due(local(28, 21, 0))
        self.assertEqual(wednesday.start, local(28, 21))
        self.assertEqual(self.due(local(28, 20, 50)), [])  # not yet (a minute early only)

    def test_a_timer_past_midnight_is_its_first_day_s(self):
        self.schedule.add_timer(CHANNEL, begin=23 * 60 + 30, minutes=90, days=[0])
        (job,) = self.due(local(27, 0, 30))  # Tuesday night, the Monday slot
        self.assertEqual((job.start, job.stop), (local(26, 23, 30), local(27, 1)))

    def test_a_timer_stays_after_it_records_and_records_each_time_once(self):
        item = self.schedule.add_timer(
            CHANNEL, begin=21 * 60, minutes=60, days=range(7), title="Late film"
        )
        (job,) = self.due(local(26, 21, 5))
        self.assertEqual(job.title, "Late film")
        self.schedule.started(job)
        self.assertEqual(self.due(local(26, 21, 6)), [])
        self.schedule.finished(job, rs.RECORDED, path="/r/a.ts")
        self.assertEqual([i["id"] for i in self.schedule.items()], [item["id"]])
        (tomorrow,) = self.due(local(27, 21, 5))
        self.assertEqual(tomorrow.start, local(27, 21))

    def test_the_same_timer_twice_is_one(self):
        first = self.schedule.add_timer(CHANNEL, begin=60, minutes=30, days=[4, 1])
        again = self.schedule.add_timer(CHANNEL, begin=60, minutes=30, days=[1, 4, 4])
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["days"], [1, 4])

    def test_a_timer_is_kept_in_the_file_and_a_wrong_one_left_out(self):
        self.schedule.add_timer(CHANNEL, begin=21 * 60, minutes=90, days=[0])
        good = rs.Schedule(self.path).items()
        self.assertEqual(
            [(i["kind"], i["begin"], i["minutes"], i["days"]) for i in good],
            [("timer", 1260, 90, [0])],
        )
        import json

        base = {"kind": "timer", "channel": CHANNEL, "title": "", "begin": 60, "minutes": 30}
        bad = [
            dict(base, id="b1", days=[]),
            dict(base, id="b2", days=[7]),
            dict(base, id="b3", days=[0], begin=24 * 60),
            dict(base, id="b4", days=[0], minutes=0),
            dict(base, id="b5", days=[0], minutes=24 * 60 + 1),
            dict(base, id="b6", days=[True]),
            dict(base, id="b7", days="0"),
        ]
        data = json.loads(Path(self.path).read_text())
        data["items"] += bad
        Path(self.path).write_text(json.dumps(data))
        self.assertEqual(len(rs.Schedule(self.path).items()), 1)

    def test_a_timer_is_never_noted_missed(self):
        # QiTV was closed through Monday's slot: the next one records as usual.
        self.schedule.add_timer(CHANNEL, begin=21 * 60, minutes=60, days=[0])
        self.assertEqual(self.schedule.missed_before(local(27, 9)), [])
        self.assertEqual(len(self.schedule.items()), 1)

    def test_a_midnight_slot_starts_a_minute_early(self):  # review 6
        self.schedule.add_timer(CHANNEL, begin=0, minutes=60, days=[1])  # Tuesday 00:00
        (job,) = self.due(local(26, 23, 59, 30))  # Monday night
        self.assertEqual(job.start, local(27, 0))

    def test_a_timer_ends_at_its_clock_time_across_a_clock_change(self):  # review 5
        # The clocks go back an hour on 2026-10-25 (a Sunday) at 03:00 in Berlin: 01:00–04:00
        # is four hours that night, and ends at 04:00 by the clock.
        try:
            berlin = ZoneInfo("Europe/Berlin")
        except ZoneInfoNotFoundError:
            self.skipTest("no time zone database")
        schedule = rs.Schedule(
            str(self.folder / "berlin.json"), write=rs.write_now, clock=at(0), zone=berlin
        )
        schedule.add_timer(CHANNEL, begin=60, minutes=180, days=[6])
        now = datetime(2026, 10, 25, 1, 30, tzinfo=berlin).timestamp()
        (job,) = schedule.due(now, lambda channel, since: [])
        self.assertEqual(job.stop - job.start, 4 * 3600)
        self.assertEqual(datetime.fromtimestamp(job.stop, berlin).hour, 4)

    def test_the_time_of_a_slot(self):
        self.assertEqual(rs.slot_text(21 * 60, 90), "21:00–22:30")
        self.assertEqual(rs.slot_text(23 * 60 + 30, 90), "23:30–01:00")
        self.assertEqual(rs.slot_text(5, 24 * 60), "00:05–00:05")


if __name__ == "__main__":
    unittest.main()
