"""The Recordings page's content (2.9.7): airings, clashes, problems, files and places."""

from datetime import datetime, timedelta
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services import recording, recording_desk as desk, recording_schedule as rs
from services.epg_view import Programme
from services.recordings_list import RecordingsList
from tests.timer_fixtures import wait_until

app = QApplication.instance() or QApplication([])
NEWS = {"id": "7", "name": "News One", "_provider": "P"}
FILMS = {"id": "8", "name": "Films", "_provider": "P"}


def local(day, hour, minute=0):
    """A moment of this computer's clock in October 2026 (the 26th is a Monday)."""
    return datetime(2026, 10, day, hour, minute).astimezone().timestamp()


def once(item_id, channel, start, stop, title="Show"):
    return {
        "id": item_id,
        "kind": rs.ONCE,
        "provider": channel["_provider"],
        "channel": channel,
        "title": title,
        "start": start,
        "stop": stop,
    }


def keys(entries):
    return [e.key for e in entries]


class AiringTests(unittest.TestCase):
    def test_a_once_recording_airs_until_its_window_ends(self):
        item = once("a", NEWS, local(26, 20), local(26, 21))
        self.assertEqual(desk.airings(item, local(26, 12)), [(local(26, 20), local(26, 21))])
        self.assertEqual(desk.airings(item, local(26, 22)), [])

    def test_a_timer_airs_on_its_days_for_a_week(self):
        item = {"id": "t", "kind": rs.TIMER, "begin": 21 * 60, "minutes": 30, "days": [0, 2]}
        times = desk.airings(item, local(26, 22))  # Monday's is over
        self.assertEqual(times[0], (local(28, 21), local(28, 21, 30)))  # Wednesday
        self.assertEqual(len(times), 2)  # and next Monday (Wednesday's after a week is past)

    def test_a_series_airs_as_its_channel_s_guide_says(self):
        start = datetime.fromtimestamp(local(26, 20)).astimezone()
        listing = [
            Programme("The News", start, start + timedelta(minutes=30), "", None),
            Programme("Sport", start + timedelta(hours=1), start + timedelta(hours=2), "", None),
        ]
        item = {"id": "s", "kind": rs.SERIES, "channel": NEWS, "title": "the  news"}
        times = desk.airings(item, local(26, 12), guide=lambda channel, since: listing)
        self.assertEqual(times, [(local(26, 20), local(26, 20, 30))])
        self.assertEqual(desk.airings(item, local(26, 12)), [])  # no guide: none known


class ClashTests(unittest.TestCase):
    def plans(self, *items):
        return [plan for item in items for plan in desk.plans(item, desk.airings(item, 0))]

    def test_two_channels_at_once_clash_beyond_the_provider_s_streams(self):
        a = once("a", NEWS, local(26, 20), local(26, 21))
        b = once("b", FILMS, local(26, 20, 30), local(26, 22))
        found = desk.clashes(self.plans(a, b), {"P": 1})
        self.assertEqual(set(found), {"a", "b"})
        self.assertEqual((found["b"].count, found["b"].limit), (2, 1))
        self.assertEqual(desk.clashes(self.plans(a, b), {"P": 2}), {})
        self.assertEqual(desk.clashes(self.plans(a, b), {"P": None}), {})  # not known

    def test_back_to_back_or_within_on_one_channel_share_a_stream(self):
        a = once("a", NEWS, local(26, 20), local(26, 21))
        b = once("b", NEWS, local(26, 21), local(26, 22))  # windows overlap by the margins
        c = once("c", NEWS, local(26, 20, 15), local(26, 20, 45))  # within a
        self.assertEqual(desk.clashes(self.plans(a, b, c), {"P": 1}), {})

    def test_the_schedule_says_a_clash_and_what_is_next(self):
        a = once("a", NEWS, local(26, 20), local(26, 21), "Late news")
        b = once("b", FILMS, local(26, 20, 30), local(26, 22), "Film")
        timer = {
            "id": "t",
            "kind": rs.TIMER,
            "provider": "P",
            "channel": FILMS,
            "title": "",
            "begin": 9 * 60,
            "minutes": 60,
            "days": [1],
        }
        entries = desk.coming_entries([b, timer, a], local(26, 12), limits={"P": 1})
        self.assertEqual(
            keys(entries),
            [
                f"airing:a@{int(local(26, 20))}",
                f"airing:b@{int(local(26, 20, 30))}",
                f"airing:t@{int(local(27, 9))}",
                "add:coming",
            ],
        )  # 2.9.7: one row per airing, soonest first
        self.assertIn("Clashes at", entries[0].note)
        self.assertIn("P allows 1 at once", entries[0].note)
        self.assertEqual(entries[0].tone, desk.WARN)
        self.assertEqual(entries[2].title, "09:00–10:00")  # a timer without a name: its time
        self.assertEqual(entries[2].tone, desk.OK)
        self.assertEqual([b.name for b in entries[3].buttons], [desk.ADD_TIMER, desk.OPEN_GUIDE])

    def test_one_recording_now_is_not_edited_under_it(self):
        a = once("a", NEWS, local(26, 11, 50), local(26, 13))
        entry, _add = desk.coming_entries([a], local(26, 12), limits={}, recording_ids=["a"])
        edit = next(b for b in entry.buttons if b.name == desk.EDIT)
        self.assertFalse(edit.enabled)
        self.assertTrue(entry.note.startswith("Recording now, until"))


class AiringRowTests(unittest.TestCase):  # 2.9.7: Coming up, one row per airing
    TIMER = {
        "id": "t",
        "kind": rs.TIMER,
        "provider": "P",
        "channel": FILMS,
        "title": "Late film",
        "begin": 21 * 60,
        "minutes": 60,
        "days": [0, 2],
    }

    def test_a_series_lists_each_airing_with_skip(self):
        series = {"id": "s", "kind": rs.SERIES, "provider": "P", "channel": NEWS, "title": "News"}
        shows = [
            Programme(
                "News",
                datetime(2026, 10, 26, 20).astimezone(),
                datetime(2026, 10, 26, 21).astimezone(),
                "",
                None,
            ),
            Programme(
                "News",
                datetime(2026, 10, 27, 20).astimezone(),
                datetime(2026, 10, 27, 21).astimezone(),
                "",
                None,
            ),
        ]
        entries = desk.coming_entries(
            [series], local(26, 12), limits={}, guide=lambda channel, since: shows
        )
        rows = [e for e in entries if e.key.startswith("airing:")]
        self.assertEqual(
            [e.key for e in rows],
            [f"airing:s@{int(local(26, 20))}", f"airing:s@{int(local(27, 20))}"],
        )
        self.assertEqual([[b.name for b in e.buttons] for e in rows], [[desk.SKIP], [desk.SKIP]])
        self.assertEqual(rows[0].status, desk.status("series"))

    def test_a_timer_lists_each_day_with_edit_and_skip(self):
        entries = desk.coming_entries([self.TIMER], local(26, 12), limits={})
        rows = [e for e in entries if e.key.startswith("airing:")]
        self.assertEqual([e.sort for e in rows], [local(26, 21), local(28, 21)])
        self.assertEqual([b.name for b in rows[0].buttons], [desk.EDIT, desk.SKIP])
        self.assertEqual(rows[0].title, "Late film")

    def test_a_once_lists_edit_and_cancel(self):
        a = once("a", NEWS, local(26, 20), local(26, 21))
        row, _add = desk.coming_entries([a], local(26, 12), limits={})
        self.assertEqual([b.name for b in row.buttons], [desk.EDIT, desk.CANCEL])

    def test_a_skipped_airing_is_not_listed(self):
        skipped = {("t", int(local(26, 21)))}
        entries = desk.coming_entries(
            [self.TIMER],
            local(26, 12),
            limits={},
            skipped=lambda item_id, start: (item_id, int(start)) in skipped,
        )
        rows = [e for e in entries if e.key.startswith("airing:")]
        self.assertEqual([e.sort for e in rows], [local(28, 21)])

    def test_rules_list_series_and_timers_with_their_next_airing(self):
        series = {"id": "s", "kind": rs.SERIES, "provider": "P", "channel": NEWS, "title": "News"}
        a = once("a", NEWS, local(26, 20), local(26, 21))
        rules = desk.rule_entries([series, self.TIMER, a], local(26, 12), guide=lambda c, s: [])
        rows = [e for e in rules if e.key.startswith("rule:")]
        # the soonest next airing first, one with none in the guide last; not the one-time
        self.assertEqual([e.key for e in rows], ["rule:t", "rule:s"])
        self.assertEqual([e.section for e in rows], [desk.RULES, desk.RULES])
        self.assertEqual([b.name for b in rows[0].buttons], [desk.EDIT, desk.REMOVE_RULE])
        self.assertEqual([b.name for b in rows[1].buttons], [desk.REMOVE_RULE])
        self.assertTrue(rows[0].note.startswith("Next: "))
        self.assertEqual(rows[1].note, "No airing in the guide yet")


class SectionTests(unittest.TestCase):
    def test_what_records_now_the_big_picture_s_only_stops(self):
        entries = desk.now_entries(
            [
                desk.Recording("pip", "Sport", 20.0, "background"),
                desk.Recording("main", "News One", 10.0, "large"),
                desk.Recording("sched:x@1", "Films", 30.0, "scheduled", "The Film", local(26, 22)),
            ],
            [desk.Waiting("y@2", "y", "Late", "Music", "P", local(26, 23))],
        )
        self.assertEqual(
            keys(entries), ["rec:main", "rec:pip", "rec:sched:x@1", "wait:y@2"]
        )  # the oldest first, then what waits
        self.assertEqual([b.name for b in entries[0].buttons], [desk.STOP])
        self.assertEqual([b.name for b in entries[1].buttons], [desk.WATCH, desk.STOP])
        self.assertEqual(entries[2].title, "The Film")
        self.assertIn("until 22:00", entries[2].detail)
        self.assertIn("No stream of P is free", entries[3].note)
        self.assertEqual([b.name for b in entries[3].buttons], [desk.TRY_NOW, desk.SKIP])

    def test_problems_say_why_and_what_helps_until_dismissed(self):
        outcomes = [
            {
                "title": "A",
                "channel": "News One",
                "start": 1.0,
                "state": rs.MISSED,
                "why": "asleep",
                "at": 4.0,
            },
            {
                "title": "B",
                "channel": "Films",
                "start": 2.0,
                "state": rs.FAILED,
                "why": "link",
                "at": 3.0,
                "path": "/r/b.ts",
            },
            {
                "title": "C",
                "channel": "Films",
                "start": 2.0,
                "state": rs.MISSED,
                "why": "stream",
                "at": 2.0,
                "seen": True,
            },
            {"title": "D", "channel": "Films", "start": 2.0, "state": rs.RECORDED, "at": 1.0},
        ]
        entries = desk.attention_entries(
            outcomes, lambda why: "the provider gave no address", lambda path: path == "/r/b.ts"
        )
        self.assertEqual(keys(entries), ["out:4.0", "out:3.0", "add:attention"])
        self.assertIn("asleep", entries[0].note)
        self.assertEqual(
            entries[1].note, "The provider gave no address. What was recorded before is kept."
        )
        self.assertEqual([b.name for b in entries[1].buttons], [desk.PLAY, desk.DISMISS])
        self.assertEqual([b.name for b in entries[2].buttons], [desk.DISMISS_ALL])
        one = desk.attention_entries(outcomes[:1], str, lambda path: False)
        self.assertEqual(keys(one), ["out:4.0"])  # one alone: no Dismiss all

    def test_a_missed_one_for_want_of_a_stream_points_at_the_setting(self):
        reason = desk.problem_reason({"state": rs.MISSED, "why": "stream"}, str)
        self.assertIn("Max simultaneous streams", reason)
        self.assertIn("QiTV was closed", desk.problem_reason({"state": rs.MISSED}, str))

    def test_files_play_and_one_still_written_can_t_be_deleted(self):
        files = [
            {"_recording": True, "path": "/r/a.ts", "name": "A", "description": "x", "added": "2"},
            {"_recording": True, "path": "/q/b.ts", "name": "B", "_writing": True, "added": "1"},
            {"_scheduled": True, "name": "C"},
        ]
        entries = desk.recorded_entries(files, many_places=True)
        self.assertEqual(keys(entries), ["file:/r/a.ts", "file:/q/b.ts"])
        self.assertTrue(entries[0].detail.endswith(os.path.dirname("/r/a.ts")))
        self.assertFalse(entries[1].buttons[-1].enabled)
        self.assertEqual(entries[1].status, "Writing")

    def test_places_where_new_ones_go_first_then_the_others_then_add(self):
        home = os.path.expanduser("~")
        places = [
            desk.Place("/mnt/disk", False, free=10**12, count=2, size=5 * 10**9),
            desk.Place(os.path.join(home, "Movies", "QiTV"), True, builtin=True, free=10**9),
            desk.Place("/mnt/gone", False, reachable=False),
        ]
        entries = desk.place_entries(places, home)
        self.assertEqual(entries[0].title, os.path.join("~", "Movies", "QiTV"))
        self.assertIn("Almost full", entries[0].note)  # where new ones go, nearly full
        self.assertEqual([b.name for b in entries[0].buttons], [desk.OPEN])
        self.assertEqual(
            [b.name for b in entries[1].buttons], [desk.OPEN, desk.DEFAULT, desk.REMOVE]
        )
        self.assertIn("2 recordings", entries[1].detail)
        self.assertIn("Is its drive connected", entries[2].note)
        self.assertFalse(entries[2].buttons[1].enabled)
        self.assertEqual(entries[-1].key, "add:places")

    def test_what_holds_a_stream_is_said(self):
        self.assertEqual(desk.use_of("main", "large", "News", False).label, "Watching: News")
        self.assertTrue(desk.use_of("sched:a@1", None, "Film (Films)", True).recording)
        self.assertEqual(desk.use_of("probe", None, "", False).kind, "probe")
        streams = desk.Streams("P", 2, (desk.use_of("main", "large", "News", False),))
        self.assertEqual(desk.streams_text(streams), "1 of 2 streams")
        self.assertEqual(desk.streams_text(desk.Streams("P", None, ())), "0 streams in use")


class PlaceTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="qitv-places-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.config = SimpleNamespace(recordings_dir=str(self.folder / "main"), recording_places=[])

    def test_places_are_where_they_go_then_the_others_once_each(self):
        other = str(self.folder / "other")
        self.assertTrue(recording.add_place(self.config, other))
        self.assertFalse(recording.add_place(self.config, other + os.sep))
        self.assertFalse(recording.add_place(self.config, self.config.recordings_dir))
        self.assertEqual(recording.places(self.config), [self.config.recordings_dir, other])
        recording.remove_place(self.config, other)
        self.assertEqual(recording.places(self.config), [self.config.recordings_dir])

    def test_using_a_place_keeps_the_old_one_listed(self):
        before, other = self.config.recordings_dir, str(self.folder / "other")
        recording.add_place(self.config, other)
        recording.use_place(self.config, other)
        self.assertEqual(recording.folder(self.config), other)
        self.assertEqual(recording.places(self.config), [other, before])

    def test_the_list_reads_every_place_and_their_state(self):
        main, other = self.folder / "main", self.folder / "other"
        main.mkdir()
        other.mkdir()
        (main / "News 2026-10-26 20.00.ts").write_bytes(b"x" * 10)
        (other / "Films 2026-10-25 21.00.ts").write_bytes(b"y" * 20)
        recording.add_place(self.config, str(other))
        recording.add_place(self.config, str(self.folder / "missing"))
        listing = RecordingsList(self.config)
        listing.refresh()
        self.assertTrue(wait_until(lambda: bool(listing.places()), 5000))
        self.assertEqual([f["name"] for f in listing.files()], ["News", "Films"])
        self.assertEqual(listing.files()[0]["_size"], 10)
        states = listing.places()
        self.assertEqual([s.reachable for s in states], [True, True, False])
        self.assertIsNotNone(states[0].free)


if __name__ == "__main__":
    unittest.main()
