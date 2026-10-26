"""Scheduled recordings (2.8): what is to be recorded, when each is due, and what became of it."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from services import recording_schedule as rs
from services.epg_view import Programme

T0 = datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)
CHANNEL = {
    "id": "7",
    "name": "News One",
    "cmd": "http://panel.test/live/u/p/7.ts",
    "_provider": "P",
}


def programme(title, start_min, length_min=30):
    start = T0 + timedelta(minutes=start_min)
    return Programme(title, start, start + timedelta(minutes=length_min), "", None)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="qitv-schedule-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.schedule = rs.Schedule(str(self.folder / rs.FILE_NAME), write=rs.write_now)

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


if __name__ == "__main__":
    unittest.main()
