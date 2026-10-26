"""What QiTV learned per live channel: Auto's extra and "can't change speed" (2.0.2), the
learned target (2.1)."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from services.live import learned_target as lt, tuning
from services.live.learned_target import Learned, LearnedTarget
from services.live.tuning import LiveTuning, Tuning, channel_key

CHANNEL = {"id": 7, "name": "One", "cmd": "http://provider.example/live/user/secret/7.ts"}


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        self.now += 1.0
        return self.now


class ChannelKeyTests(unittest.TestCase):
    def test_a_channel_has_one_key_per_provider(self):
        key = channel_key("Home", CHANNEL)
        self.assertEqual(key, channel_key("Home", dict(CHANNEL, name="One HD")))
        self.assertNotEqual(key, channel_key("Away", CHANNEL))
        self.assertNotEqual(key, channel_key("Home", dict(CHANNEL, cmd="http://x.example/8")))

    def test_it_tells_channels_apart_as_the_zap_list_does(self):
        # The stream address first (M3U ids are list positions), else the id, else the name.
        moved = dict(CHANNEL, id=99)
        self.assertEqual(channel_key("Home", moved), channel_key("Home", CHANNEL))
        by_id = {"id": 3, "name": "Three"}
        self.assertEqual(channel_key("Home", by_id), channel_key("Home", {"id": "3"}))
        self.assertNotEqual(channel_key("Home", by_id), channel_key("Home", {"id": 4}))
        self.assertEqual(channel_key("Home", {"name": "N"}), channel_key("Home", {"name": "N"}))
        self.assertEqual(channel_key("Home", {}), "")

    def test_the_key_holds_neither_the_address_nor_the_provider(self):
        key = channel_key("Home", CHANNEL)
        for part in ("secret", "provider.example", "Home", "7.ts"):
            self.assertNotIn(part, key)


class LiveTuningTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / tuning.FILE_NAME
        self.clock = Clock()

    def store(self, **kwargs):
        store = LiveTuning(str(self.path), now=self.clock, **kwargs)
        self.addCleanup(store.flush)
        return store

    def test_nothing_learned_reads_as_nothing(self):
        self.assertEqual(self.store().get("k"), Tuning())
        self.assertEqual(self.store().get(""), Tuning())
        self.assertFalse(self.path.exists())

    def test_what_is_learned_round_trips_through_the_file(self):
        store = self.store()
        store.remember("a", Tuning(extra=5.5))
        store.remember("b", Tuning(rate_locked=True))
        store.remember("c", Tuning(extra=7.0, rate_locked=True))
        self.assertEqual(store.get("a"), Tuning(extra=5.5))  # at once, from memory
        store.flush()
        again = self.store()
        self.assertEqual(again.get("a"), Tuning(extra=5.5))
        self.assertEqual(again.get("b"), Tuning(rate_locked=True))
        self.assertEqual(again.get("c"), Tuning(extra=7.0, rate_locked=True))
        self.assertEqual(again.get("d"), Tuning())

    def test_nothing_left_to_remember_drops_the_channel(self):
        store = self.store()
        store.remember("a", Tuning(extra=5.5))
        store.remember("a", Tuning())  # Auto came back to where it starts
        store.flush()
        self.assertEqual(json.loads(self.path.read_text())["channels"], {})

    def test_at_most_the_newest_entries_are_kept(self):
        store = self.store(max_entries=3)
        for key in ("a", "b", "c", "d"):
            store.remember(key, Tuning(extra=4.0))
        store.remember("b", Tuning(extra=5.5))  # learned again: the newest now
        store.remember("e", Tuning(extra=4.0))
        self.assertEqual(store.get("a"), Tuning())  # the oldest went first
        self.assertEqual(store.get("c"), Tuning())
        store.flush()
        self.assertEqual(set(json.loads(self.path.read_text())["channels"]), {"b", "d", "e"})
        self.assertEqual(self.store(max_entries=3).get("b"), Tuning(extra=5.5))

    def test_the_default_cap_is_five_hundred(self):
        self.assertEqual(tuning.MAX_ENTRIES, 500)
        store = self.store()
        for i in range(501):
            store.remember(f"k{i}", Tuning(rate_locked=True))
        self.assertEqual(store.get("k0"), Tuning())
        self.assertEqual(store.get("k500"), Tuning(rate_locked=True))
        store.flush()
        self.assertEqual(len(json.loads(self.path.read_text())["channels"]), 500)

    def test_a_corrupt_file_is_ignored_and_replaced(self):
        for text in ("{not json", "[]", '{"channels": 3}', '{"channels": {"a": "x"}}'):
            with self.subTest(text=text):
                self.path.write_text(text)
                store = self.store()
                self.assertEqual(store.get("a"), Tuning())
                store.remember("b", Tuning(extra=4.0))
                store.flush()
                self.assertEqual(self.store().get("b"), Tuning(extra=4.0))

    def test_entries_that_make_no_sense_are_left_out(self):
        channels = {
            "ok": {"extra": 4.0, "rate_locked": True, "used": self.clock.now},
            "text": {"extra": "4", "used": 1.0},
            "flag": {"rate_locked": "yes", "used": 1.0},
            "bool": {"extra": True, "used": 1.0},
            "when": {"extra": 4.0, "used": "today"},
            "lock": {"rate_locked": True, "used": 1.0, "locked_at": "today"},
            "direct": {"direct": "yes", "used": self.clock.now},
            "list": [4.0, True],
        }
        self.path.write_text(json.dumps({"version": 1, "channels": channels}))
        store = self.store()
        self.assertEqual(store.get("ok"), Tuning(extra=4.0, rate_locked=True))
        for key in ("text", "flag", "bool", "when", "lock", "direct", "list"):
            self.assertEqual(store.get(key), Tuning(), key)

    def test_the_file_is_written_off_the_calling_thread(self):
        threads = []
        write = LiveTuning._write

        def spy(store, *args):
            threads.append(threading.get_ident())
            return write(store, *args)

        with mock.patch.object(LiveTuning, "_write", spy):
            store = self.store()
            store.remember("a", Tuning(extra=4.0))
            store.flush()
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)

    def test_a_failed_write_is_logged_without_the_path(self):
        store = self.store()
        with (
            mock.patch(
                "services.atomic_write.os.replace", side_effect=PermissionError(13, "No", "/x")
            ),
            self.assertLogs("services.live.tuning", "WARNING") as logs,
        ):
            store.remember("a", Tuning(extra=4.0))
            store.flush()
        self.assertNotIn(str(self.path.parent), "\n".join(logs.output))
        self.assertEqual(list(self.path.parent.iterdir()), [])  # no temporary file left
        self.assertEqual(store.get("a"), Tuning(extra=4.0))  # kept for this run

    def test_a_lock_is_tried_again_a_day_after_it_was_set(self):
        store = self.store()
        store.remember("a", Tuning(extra=5.5, rate_locked=True))
        locked_at = self.clock.now
        self.clock.now = locked_at + tuning.RATE_LOCK_S - 10
        self.assertEqual(store.get("a"), Tuning(extra=5.5, rate_locked=True))
        self.clock.now = locked_at + tuning.RATE_LOCK_S
        self.assertEqual(store.get("a"), Tuning(extra=5.5))  # the extra stays
        store.remember("a", Tuning(extra=5.5, rate_locked=True))  # it crawled again
        self.clock.now += tuning.RATE_LOCK_S - 10
        self.assertEqual(store.get("a"), Tuning(extra=5.5, rate_locked=True))

    def test_a_later_lesson_keeps_the_time_of_the_lock(self):
        store = self.store()
        store.remember("a", Tuning(rate_locked=True))
        locked_at = self.clock.now
        self.clock.now = locked_at + 20 * 3600
        store.remember("a", Tuning(extra=4.0, rate_locked=True))  # Auto moved meanwhile
        self.clock.now = locked_at + tuning.RATE_LOCK_S
        self.assertEqual(store.get("a"), Tuning(extra=4.0))

    def test_the_time_of_a_lock_round_trips_through_the_file(self):
        store = self.store()
        store.remember("a", Tuning(rate_locked=True))
        locked_at = self.clock.now
        store.flush()
        self.clock.now = locked_at + tuning.RATE_LOCK_S - 10
        self.assertEqual(self.store().get("a"), Tuning(rate_locked=True))
        self.clock.now = locked_at + tuning.RATE_LOCK_S
        self.assertEqual(self.store().get("a"), Tuning())

    def test_a_lock_without_its_time_counts_from_when_it_was_learned(self):
        channels = {"a": {"rate_locked": True, "used": self.clock.now}}
        self.path.write_text(json.dumps({"version": 1, "channels": channels}))
        self.assertEqual(self.store().get("a"), Tuning(rate_locked=True))
        self.clock.now += tuning.RATE_LOCK_S
        self.assertEqual(self.store().get("a"), Tuning())

    def test_a_channel_played_directly_is_tried_on_the_pipeline_again_after_a_day(self):
        store = self.store()
        store.remember("a", Tuning(extra=4.0, direct=True))
        set_at = self.clock.now
        self.clock.now = set_at + tuning.DIRECT_S - 10
        self.assertEqual(store.get("a"), Tuning(extra=4.0, direct=True))
        store.remember("a", Tuning(extra=5.5, direct=True))  # a later lesson keeps its time
        store.flush()
        self.clock.now = set_at + tuning.DIRECT_S - 10
        self.assertEqual(self.store().get("a"), Tuning(extra=5.5, direct=True))  # the file
        self.clock.now = set_at + tuning.DIRECT_S
        self.assertEqual(store.get("a"), Tuning(extra=5.5))
        store.remember("a", Tuning(direct=True))  # it crawled again
        self.clock.now += tuning.DIRECT_S - 10
        self.assertEqual(store.get("a"), Tuning(direct=True))

    def test_a_channel_decoded_in_software_tries_hardware_again_after_a_day(self):
        # 2.1: hardware decoding froze its picture (services.picture_watch).
        self.assertEqual(tuning.SOFTWARE_S, 24 * 60 * 60)
        store = self.store()
        store.remember("a", Tuning(extra=4.0, software=True))
        set_at = self.clock.now
        store.remember("a", Tuning(extra=5.5, software=True))  # a later lesson keeps its time
        store.flush()
        self.clock.now = set_at + tuning.SOFTWARE_S - 10
        self.assertEqual(self.store().get("a"), Tuning(extra=5.5, software=True))  # the file
        self.clock.now = set_at + tuning.SOFTWARE_S
        self.assertEqual(store.get("a"), Tuning(extra=5.5))
        store.remember("a", Tuning(software=True))  # it froze again
        self.clock.now += tuning.SOFTWARE_S - 10
        self.assertEqual(store.get("a"), Tuning(software=True))
        self.clock.now -= 2 * tuning.SOFTWARE_S  # a time in the future counts as expired
        self.assertEqual(store.get("a"), Tuning())

    def test_software_decoding_alone_is_kept_and_a_wrong_flag_is_left_out(self):
        channels = {
            "soft": {"software": True, "used": self.clock.now},
            "text": {"software": "yes", "used": self.clock.now},
            "when": {"software": True, "used": self.clock.now, "software_at": "today"},
        }
        self.path.write_text(json.dumps({"version": 1, "channels": channels}))
        store = self.store()
        self.assertEqual(store.get("soft"), Tuning(software=True))
        self.assertEqual(store.get("text"), Tuning())
        self.assertEqual(store.get("when"), Tuning())
        store.remember("plain", Tuning(extra=1.5))
        store.flush()
        written = json.loads(self.path.read_text())["channels"]
        self.assertNotIn("software", written["plain"])  # only when it is so
        self.assertEqual(written["soft"]["software_at"], written["soft"]["used"])

    def test_software_decoding_is_not_pushed_out_by_learned_entries(self):
        store = self.store(max_entries=2)
        store.remember("soft", Tuning(software=True))
        for key in ("a", "b"):
            store.remember(key, Tuning(learned=Learned(need=8.0)))
        self.assertEqual(store.get("soft"), Tuning(software=True))
        self.assertEqual(store.get("a"), Tuning())

    def test_a_time_in_the_future_counts_as_expired(self):
        # The computer's clock was set back: a lock or direct play set "later" is not kept
        # until the clock passes it again (2.0.2).
        store = self.store()
        store.remember("a", Tuning(rate_locked=True, direct=True))
        self.clock.now -= 3600
        self.assertEqual(store.get("a"), Tuning())
        store.remember("a", Tuning(rate_locked=True, direct=True))  # learned again: from now
        self.clock.now += tuning.RATE_LOCK_S - 10
        self.assertEqual(store.get("a"), Tuning(rate_locked=True, direct=True))

    def test_an_unexpected_write_error_is_logged_without_the_address(self):
        store = self.store()
        failure = ValueError("cannot serialise http://provider.example/user/secret/1.ts")
        with (
            mock.patch("services.channel_store.json.dumps", side_effect=failure),
            self.assertLogs("services.live.tuning", "WARNING") as logs,
        ):
            store.remember("a", Tuning(extra=1.5))
            store.flush()
        self.assertNotIn("secret", "\n".join(logs.output))
        self.assertIn("ValueError", "\n".join(logs.output))

    def test_a_file_of_another_version_is_ignored(self):
        channels = {"a": {"extra": 1.5, "used": self.clock.now}}
        for version in (2, None, "1"):
            with self.subTest(version=version):
                data = {"channels": channels}
                if version is not None:
                    data["version"] = version
                self.path.write_text(json.dumps(data))
                self.assertEqual(self.store().get("a"), Tuning())
        self.path.write_text(json.dumps({"version": 1, "channels": channels}))
        self.assertEqual(self.store().get("a"), Tuning(extra=1.5))

    def test_the_learned_target_round_trips_through_the_file(self):
        store = self.store()
        learned = Learned(need=8.25, events=1.5, hours=1.2)
        store.remember("a", Tuning(learned=learned))
        store.remember("b", Tuning(extra=3.0, rate_locked=True, learned=Learned(events=4.0)))
        self.assertEqual(store.get("a"), Tuning(learned=learned))  # at once, from memory
        store.flush()
        data = json.loads(self.path.read_text())
        self.assertEqual(data["version"], 1)  # an optional field: 2.0.x reads the file
        self.assertEqual(
            data["channels"]["a"]["learned"], {"need": 8.25, "events": 1.5, "hours": 1.2}
        )
        again = self.store()
        self.assertEqual(again.get("a"), Tuning(learned=learned))
        self.assertEqual(
            again.get("b"), Tuning(extra=3.0, rate_locked=True, learned=Learned(events=4.0))
        )

    def test_two_zero_eight_reads_what_two_one_writes(self):
        store = self.store()
        store.remember("locked", Tuning(rate_locked=True, learned=Learned(need=8.0)))
        store.remember("extra", Tuning(extra=3.0, learned=Learned(events=4.0)))
        store.remember("direct", Tuning(direct=True))
        store.remember("learned", Tuning(learned=Learned(need=13.0)))
        store.remember("software", Tuning(software=True))
        store.remember("both", Tuning(extra=2.0, software=True))
        store.flush()
        old = load_two_zero_eight()
        reader = old.LiveTuning(str(self.path), now=self.clock)
        self.addCleanup(reader.flush)
        self.assertEqual(reader.get("locked"), old.Tuning(rate_locked=True))
        self.assertEqual(reader.get("extra"), old.Tuning(extra=3.0))
        self.assertEqual(reader.get("direct"), old.Tuning(direct=True))
        self.assertEqual(reader.get("learned"), old.Tuning())  # what it can't hold: nothing
        self.assertEqual(reader.get("software"), old.Tuning())
        self.assertEqual(reader.get("both"), old.Tuning(extra=2.0))
        reader.remember("other", old.Tuning(extra=1.5))  # 2.0.8 writes back what it knows
        reader.flush()
        again = self.store()
        self.assertEqual(again.get("locked"), Tuning(rate_locked=True))
        self.assertEqual(again.get("other"), Tuning(extra=1.5))

    def test_the_next_tune_starts_at_the_learned_target(self):
        target = LearnedTarget()
        now = 1000.0
        for n in range(1, 120):  # 10 s batches every 12 s, a tick every 0.25 s
            for tick in range(48):
                now += 0.25
                target.arrivals.add(now, 10.0 * n if tick == 47 else 10.0 * (n - 1))
        target.near_miss(0.2)
        target.played(600.0)
        store = self.store()
        store.remember("a", Tuning(learned=target.learned()))
        store.flush()
        restored = self.store().get("a")
        again = LearnedTarget(restored.learned, restored.extra)
        self.assertGreater(again.target(10.0), 10.0)  # its burst and the near-miss's margin
        self.assertAlmostEqual(again.target(10.0), target.target(10.0), delta=0.01)

    def test_a_learned_only_segment_cover_round_trips_through_the_file(self):
        # The burst floor off (its A/B switch), a lost 30 s segment covered over a 6 s need:
        # a margin beyond today's 7.5 s, which the file keeps whole.
        target = LearnedTarget(Learned(need=6.0), segment_floor=False)
        target.starved(30.0)
        self.assertAlmostEqual(target.target(30.0), 31.0)  # the segment and 1 s
        store = self.store()
        store.remember("a", Tuning(learned=target.learned()))
        store.flush()
        restored = self.store().get("a")
        self.assertIsNotNone(restored.learned)
        again = LearnedTarget(restored.learned, segment_floor=False)
        self.assertAlmostEqual(again.target(30.0), target.target(30.0), delta=0.01)

    def test_a_two_zero_two_extra_reads_as_a_prior(self):
        channels = {"a": {"extra": 3.0, "used": self.clock.now}}
        self.path.write_text(json.dumps({"version": 1, "channels": channels}))
        store = self.store()
        tuned = store.get("a")
        self.assertEqual(tuned, Tuning(extra=3.0))  # as 2.0.2 wrote it
        target = LearnedTarget(tuned.learned, tuned.extra)
        self.assertAlmostEqual(target.target(), lt.FLOOR_S + 3.0)  # it needed that much
        store.remember("a", Tuning(learned=target.learned()))  # kept the new way from now
        store.flush()
        again = self.store().get("a")
        self.assertIsNone(again.extra)
        self.assertAlmostEqual(LearnedTarget(again.learned).target(), lt.FLOOR_S + 3.0)

    def test_a_learned_target_that_makes_no_sense_is_left_out_on_its_own(self):
        good = {"need": 8.0, "events": 1.5, "hours": 1.2}
        channels = {
            "ok": {"learned": good, "used": self.clock.now},
            "unknown": {"learned": dict(good, need=None), "used": self.clock.now},
            "text": {"learned": "8", "used": 1.0},
            "list": {"learned": [8.0, 1.5, 1.2], "used": 1.0},
            "need": {"learned": dict(good, need="8"), "used": 1.0},
            "negative": {"learned": dict(good, need=-1.0), "used": 1.0},
            "huge": {"learned": dict(good, need=1e9), "used": 1.0},
            "events": {"learned": dict(good, events=True), "used": 1.0},
            "fewer": {"learned": dict(good, events=-0.5), "used": 1.0},
            "hours": {"learned": dict(good, hours=0.0), "used": 1.0},
            "missing": {"learned": {"need": 8.0}, "used": 1.0},
            "nothing": {"learned": {"need": 2.0, "events": 0.5, "hours": 1.0}, "used": 1.0},
            "locked": {"learned": "x", "rate_locked": True, "used": self.clock.now},
            "extra": {"learned": dict(good, hours=-1), "extra": 3.0, "used": self.clock.now},
        }
        self.path.write_text(json.dumps({"version": 1, "channels": channels}))
        store = self.store()
        self.assertEqual(store.get("ok"), Tuning(learned=Learned(8.0, 1.5, 1.2)))
        self.assertEqual(store.get("unknown"), Tuning(learned=Learned(None, 1.5, 1.2)))
        self.assertEqual(store.get("locked"), Tuning(rate_locked=True))  # the lock stays
        self.assertEqual(store.get("extra"), Tuning(extra=3.0))
        for key in set(channels) - {"ok", "unknown", "locked", "extra"}:
            self.assertEqual(store.get(key), Tuning(), key)

    def test_a_learned_target_that_changes_nothing_is_not_kept(self):
        store = self.store()
        zapped = Learned(None, 0.5, 1.0001)  # a quarter second played through
        store.remember("zap", Tuning(learned=zapped))
        store.remember("steady", Tuning(learned=Learned(need=2.25)))  # under the floor
        store.remember("nonsense", Tuning(learned=Learned(need=-3.0)))
        store.remember("locked", Tuning(rate_locked=True, learned=zapped))
        store.flush()
        self.assertEqual(set(json.loads(self.path.read_text())["channels"]), {"locked"})
        self.assertEqual(store.get("locked"), Tuning(rate_locked=True))
        store.remember("learned", Tuning(learned=Learned(need=8.0)))
        store.remember("learned", Tuning(learned=zapped))  # it changes nothing now: forgotten
        self.assertEqual(store.get("learned"), Tuning())

    def test_learned_entries_do_not_push_out_a_locked_rate_or_direct_play(self):
        store = self.store(max_entries=3)
        store.remember("locked", Tuning(rate_locked=True))
        store.remember("direct", Tuning(direct=True))
        for key in ("a", "b", "c"):  # three zaps that learned something
            store.remember(key, Tuning(learned=Learned(need=8.0)))
        self.assertEqual(store.get("locked"), Tuning(rate_locked=True))
        self.assertEqual(store.get("direct"), Tuning(direct=True))
        self.assertEqual(store.get("a"), Tuning())  # the oldest of the rest went
        store.flush()
        self.assertEqual(
            set(json.loads(self.path.read_text())["channels"]), {"locked", "direct", "c"}
        )

    def test_a_lock_that_expired_does_not_outweigh_a_fresh_learned_target(self):
        store = self.store(max_entries=2)
        store.remember("expired", Tuning(rate_locked=True))
        store.remember("direct", Tuning(direct=True))
        self.clock.now += tuning.RATE_LOCK_S + 10  # both read as nothing now
        store.remember("a", Tuning(learned=Learned(need=8.0)))
        self.assertEqual(store.get("a"), Tuning(learned=Learned(need=8.0)))
        store.remember("b", Tuning(learned=Learned(need=9.0)))
        self.assertEqual(store.get("a"), Tuning(learned=Learned(need=8.0)))
        self.assertEqual(store.get("b"), Tuning(learned=Learned(need=9.0)))
        store.flush()
        self.assertEqual(set(json.loads(self.path.read_text())["channels"]), {"a", "b"})

    def test_a_learned_target_is_judged_under_its_own_floor(self):
        target = LearnedTarget(floor=1.0)
        now = 1000.0
        for n in range(1, 400):  # batches of 1 s every second: a need of 2 s, over 1.0 + 0.25
            for tick in range(4):
                now += 0.25
                target.arrivals.add(now, n - 1 + (1.0 if tick == 3 else 0.0))
        learned = target.learned()
        self.assertIsNotNone(learned)
        self.assertEqual(learned.floor, 1.0)
        store = self.store()
        store.remember("a", Tuning(learned=learned))
        store.remember("normal", Tuning(learned=Learned(need=2.0)))  # under Normal's floor
        store.flush()
        channels = json.loads(self.path.read_text())["channels"]
        self.assertEqual(set(channels), {"a"})
        self.assertEqual(channels["a"]["learned"]["floor"], 1.0)
        self.assertEqual(self.store().get("a").learned, learned)

    def test_an_old_extra_does_not_come_back_once_its_lesson_faded(self):
        # Part B's way: the next tune reads 2.0.2's extra as a prior; once that faded,
        # `learned()` is None and the channel is remembered with extra None, so the old extra
        # is gone, not read again.
        channels = {"a": {"extra": 3.0, "rate_locked": True, "used": self.clock.now}}
        self.path.write_text(json.dumps({"version": 1, "channels": channels}))
        store = self.store()
        tuned = store.get("a")
        target = LearnedTarget(tuned.learned, tuned.extra)
        self.assertAlmostEqual(target.target(), lt.FLOOR_S + 3.0)
        target.played(3 * 3600.0)
        self.assertIsNone(target.learned())
        store.remember("a", Tuning(None, tuned.rate_locked, tuned.direct, target.learned()))
        again = store.get("a")
        self.assertEqual(again, Tuning(rate_locked=True))
        self.assertEqual(LearnedTarget(again.learned, again.extra).target(), lt.FLOOR_S)
        store.remember("a", Tuning(None, False, False, None))  # nothing left: forgotten
        store.flush()
        self.assertEqual(json.loads(self.path.read_text())["channels"], {})

    def test_the_hold_round_trips_and_a_wrong_one_is_left_out(self):
        store = self.store()
        held = Learned(need=8.0, events=1.5, hours=1.0, hold=900.0)
        store.remember("a", Tuning(learned=held))
        store.flush()
        channels = json.loads(self.path.read_text())["channels"]
        self.assertEqual(channels["a"]["learned"]["hold"], 900.0)
        self.assertEqual(self.store().get("a").learned, held)
        for hold in (-1.0, lt.HOLD_S + 1.0, "900"):
            with self.subTest(hold=hold):
                wrong = {"need": 8.0, "events": 1.5, "hours": 1.0, "hold": hold}
                channels = {"a": {"learned": wrong, "rate_locked": True, "used": self.clock.now}}
                self.path.write_text(json.dumps({"version": 1, "channels": channels}))
                self.assertEqual(self.store().get("a"), Tuning(rate_locked=True))

    def test_the_cap_holds_with_learned_entries(self):
        store = self.store(max_entries=3)
        for key in ("a", "b", "c", "d"):
            store.remember(key, Tuning(learned=Learned(need=8.0)))
        self.assertEqual(store.get("a"), Tuning())
        store.flush()
        self.assertEqual(set(json.loads(self.path.read_text())["channels"]), {"b", "c", "d"})

    def test_without_a_file_it_remembers_for_the_run(self):
        store = LiveTuning(None)
        store.remember("a", Tuning(rate_locked=True))
        store.flush()
        self.assertEqual(store.get("a"), Tuning(rate_locked=True))


def load_two_zero_eight():
    """2.0.8's tuning module, loaded from its source (`TUNING_2_0_8`) beside this one."""
    module = types.ModuleType("tuning_2_0_8")
    sys.modules[module.__name__] = module  # its dataclass looks its module up
    try:
        exec(compile(TUNING_2_0_8, "tuning_2_0_8.py", "exec"), module.__dict__)
    finally:
        del sys.modules[module.__name__]
    return module


# services/live/tuning.py as 2.0.8 released it (git show 67154a0:services/live/tuning.py),
# verbatim: the reader that a downgrade meets.
TUNING_2_0_8 = r'''"""What QiTV learned about each live channel (2.0.2), for that channel's next tune.

Per (provider, channel): what Auto latency adds to its target (`AutoTarget`), whether VLC can play the
channel at a rate other than 1x (`RateHealth`), and whether it plays directly because VLC
crawled through the pipeline even at 1x (an HLS channel). `channel_key()` tells channels apart as the
zap list does (the stream address, else the id, else the name) and hashes that with the
provider's name: a stream address may carry credentials, and none reach the file.

A lock is kept with when it was set: from `RATE_LOCK_S` (a day) later it reads as unlocked,
so the channel's next tune tries other rates again, and rate health locks it again if VLC
still crawls (a new day from then). Playing directly is kept the same way (`DIRECT_S`): a
day later the next tune tries the pipeline again. A later lesson (Auto's extra moving)
keeps their times; a time in the future (the clock was set back) counts as expired.

`LiveTuning` keeps the entries in memory and in `live_tuning.json` beside the config, at
most `MAX_ENTRIES` (the least recently learned go first). The file is read once; each change
writes it on one background thread, the newest state only, through a temporary file swapped
in. A missing, unreadable or corrupt file, or one of another version, counts as empty, and
a bad entry is left out. A write that fails is logged (without its path or any address).
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import logging
import math
import os
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Optional

import orjson as json

from services.redact import safe_message

_LOG = logging.getLogger(__name__)

FILE_NAME = "live_tuning.json"
MAX_ENTRIES = 500
RATE_LOCK_S = 24 * 60 * 60  # a lock older than this is tried again
DIRECT_S = 24 * 60 * 60  # so is a channel played directly
_VERSION = 1


@dataclass(frozen=True)
class Tuning:
    """What one channel taught: nothing, by default."""

    extra: Optional[float] = None  # what Auto latency adds to the target in force (if any)
    rate_locked: bool = False  # VLC cannot play the channel at a rate other than 1x
    direct: bool = False  # it crawled through the pipeline even at 1x: play it directly


def channel_key(provider: str, item: Mapping[str, Any]) -> str:
    """The channel's key under `provider`; "" when the item has no address, id or name."""
    cmd = str(item.get("cmd") or "").strip()
    if cmd:
        identity = f"cmd:{cmd}"
    elif item.get("id") is not None:
        identity = f"id:{item['id']}"
    elif item.get("name"):
        identity = f"name:{item['name']}"
    else:
        return ""
    return hashlib.sha256(f"{provider}\0{identity}".encode("utf-8")).hexdigest()[:32]


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _entry(value: Any) -> Optional[dict[str, Any]]:
    """A stored entry as written by `remember()`; None when it is not one."""
    if not isinstance(value, dict):
        return None
    extra, locked, used = value.get("extra"), value.get("rate_locked", False), value.get("used")
    direct = value.get("direct", False)
    if extra is not None and not (_number(extra) and math.isfinite(extra)):
        return None
    if not isinstance(locked, bool) or not isinstance(direct, bool) or not _number(used):
        return None
    if extra is None and not locked and not direct:
        return None
    entry = {"extra": extra, "rate_locked": locked, "direct": direct, "used": used}
    for flag, at in (("rate_locked", "locked_at"), ("direct", "direct_at")):
        if entry[flag]:
            when = value.get(at, used)  # without its time: when it was learned
            if not _number(when):
                return None
            entry[at] = when
    return entry


class LiveTuning:
    def __init__(
        self,
        path: Optional[str],
        *,
        max_entries: int = MAX_ENTRIES,
        now: Callable[[], float] = time.time,
    ) -> None:
        """`path` None keeps what is learned for this run only."""
        self._path = path
        self._max = max_entries
        self._now = now
        self._lock = threading.Lock()  # the newest state's number, read by the writer
        self._generation = 0
        self._writer: Optional[ThreadPoolExecutor] = None
        self._written: Optional[Future] = None
        self._entries: dict[str, dict[str, Any]] = self._load()
        self._trim()

    def get(self, key: str) -> Tuning:
        """What the channel taught; a lock from `RATE_LOCK_S` ago or longer reads as none,
        and so does playing directly from `DIRECT_S` ago."""
        entry = self._entries.get(key) if key else None
        if entry is None:
            return Tuning()
        now = self._now()
        locked = entry["rate_locked"] and 0 <= now - entry["locked_at"] < RATE_LOCK_S
        direct = entry.get("direct", False) and 0 <= now - entry["direct_at"] < DIRECT_S
        return Tuning(entry["extra"], locked, direct)

    def remember(self, key: str, tuning: Tuning) -> None:
        """Keep `tuning` for the channel `key` (nothing learned: forget it); write it soon."""
        if not key:
            return
        if tuning == Tuning():
            if self._entries.pop(key, None) is None:
                return
        else:
            now, old = self._now(), self._entries.get(key)
            entry = {
                "extra": tuning.extra,
                "rate_locked": tuning.rate_locked,
                "direct": tuning.direct,
                "used": now,
            }
            # A lock (or direct play) still in force keeps its time; else it counts from now.
            for flag, at, lasts in (
                ("rate_locked", "locked_at", RATE_LOCK_S),
                ("direct", "direct_at", DIRECT_S),
            ):
                if entry[flag]:
                    entry[at] = now
                    if old is not None and old.get(flag) and 0 <= now - old[at] < lasts:
                        entry[at] = old[at]
            self._entries[key] = entry
            self._trim()
        self._save()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for the writes asked for so far (tests; at exit the pool waits by itself)."""
        written = self._written
        if written is not None:
            written.result(timeout=timeout)

    # ---- the file ------------------------------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any]]:
        if self._path is None:
            return {}
        try:
            with open(self._path, "rb") as f:
                data = json.loads(f.read())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            _LOG.info("Ignoring the saved live tuning: %s", safe_message(e))
            return {}
        channels = data.get("channels") if isinstance(data, dict) else None
        if not isinstance(channels, dict):
            _LOG.info("Ignoring the saved live tuning: it is not QiTV's")
            return {}
        if data.get("version") != _VERSION:
            _LOG.info("Ignoring the saved live tuning: it is of another version")
            return {}
        entries = {}
        for key, value in channels.items():
            entry = _entry(value)
            if isinstance(key, str) and key and entry is not None:
                entries[key] = entry
        return entries

    def _trim(self) -> None:
        """Drop the least recently learned entries beyond the cap."""
        excess = len(self._entries) - self._max
        if excess > 0:
            oldest = sorted(self._entries, key=lambda key: self._entries[key]["used"])
            for key in oldest[:excess]:
                del self._entries[key]

    def _save(self) -> None:
        if self._path is None:
            return
        snapshot = {"version": _VERSION, "channels": dict(self._entries)}
        with self._lock:
            self._generation += 1
            generation = self._generation
        if self._writer is None:
            self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-tuning")
        self._written = self._writer.submit(self._write, generation, snapshot)

    def _write(self, generation: int, snapshot: dict[str, Any]) -> None:
        """On the writer thread: write `snapshot` unless a newer one is queued."""
        with self._lock:
            if generation != self._generation:
                return
        path = self._path
        assert path is not None
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(
                dir=os.path.dirname(path) or ".", prefix=".live_tuning-", suffix=".tmp"
            )
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(snapshot))
            os.replace(temporary, path)
            temporary = None
        except Exception as e:  # anything else would stay in the Future, unseen
            _LOG.warning("Couldn't save the live tuning: %s", safe_message(e))
        finally:
            if temporary is not None:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
'''  # noqa: E501 (verbatim copy of the released file)


if __name__ == "__main__":
    unittest.main()
