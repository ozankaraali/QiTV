"""What QiTV learned per live channel (2.0.2): Auto's extra and "can't change speed"."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from services.live import tuning
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
                "services.live.tuning.os.replace", side_effect=PermissionError(13, "No", "/x")
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

    def test_without_a_file_it_remembers_for_the_run(self):
        store = LiveTuning(None)
        store.remember("a", Tuning(rate_locked=True))
        store.flush()
        self.assertEqual(store.get("a"), Tuning(rate_locked=True))


if __name__ == "__main__":
    unittest.main()
