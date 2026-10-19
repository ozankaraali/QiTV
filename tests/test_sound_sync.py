"""Each live channel's sound sync (2.2): kept per channel in sound_sync.json, written off the
GUI thread, never pruned for a channel not watched lately."""

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from services.live.tuning import FILE_NAME as TUNING_FILE, LiveTuning, Tuning, channel_key
from services.sound_sync import FILE_NAME, LIMIT_MS, STEP_MS, SoundSync, clamp

ONE = channel_key("Home", {"id": 1, "name": "One", "cmd": "ffrt http://tv/1"})
TWO = channel_key("Backup", {"id": 2, "name": "Two"})


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


class SoundSyncTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.path = self.dir / FILE_NAME
        self.clock = Clock()

    def store(self, **kwargs):
        store = SoundSync(str(self.path), now=self.clock, **kwargs)
        self.addCleanup(store.flush)  # before the directory goes
        return store

    def saved(self):
        return json.loads(self.path.read_text())

    def test_steps_and_limits(self):
        self.assertEqual((STEP_MS, LIMIT_MS), (50, 5000))
        self.assertEqual(clamp(5050), 5000)
        self.assertEqual(clamp(-9000), -5000)
        self.assertEqual(clamp(150), 150)

    def test_a_channels_sync_survives_a_restart(self):
        store = self.store()
        store.set_audio_ms(ONE, 150)
        store.set_audio_ms(TWO, -300)
        store.flush()
        self.assertEqual(
            self.saved(),
            {
                "version": 1,
                "channels": {
                    ONE: {"audio_ms": 150, "set": 1000.0},
                    TWO: {"audio_ms": -300, "set": 1000.0},
                },
            },
        )
        again = self.store()
        self.assertEqual((again.audio_ms(ONE), again.audio_ms(TWO)), (150, -300))
        self.assertEqual(again.audio_ms("unknown"), 0)

    def test_in_sync_forgets_the_channel(self):
        store = self.store()
        store.set_audio_ms(ONE, 150)
        store.set_audio_ms(ONE, 0)
        store.flush()
        self.assertEqual(self.saved()["channels"], {})
        self.assertEqual(self.store().audio_ms(ONE), 0)

    def test_a_value_beyond_the_limit_is_kept_at_the_limit(self):
        store = self.store()
        store.set_audio_ms(ONE, 7000)
        self.assertEqual(store.audio_ms(ONE), LIMIT_MS)

    def test_a_channel_not_watched_for_days_keeps_its_sync(self):
        # live_tuning.json drops the least recently learned beyond 500 channels; a sync the
        # viewer set stays however many channels play since, up to the (much larger) cap.
        store = self.store()
        store.set_audio_ms(ONE, 250)
        for day in range(1, 31):
            self.clock.now += 24 * 60 * 60
            store.set_audio_ms(f"channel-{day}", 50)
        store.flush()
        self.assertEqual(self.store().audio_ms(ONE), 250)

    def test_beyond_the_cap_the_oldest_go_first(self):
        store = self.store(max_entries=2)
        store.set_audio_ms(ONE, 100)
        self.clock.now += 1
        store.set_audio_ms(TWO, 200)
        self.clock.now += 1
        store.set_audio_ms("three", 300)
        self.assertEqual(
            (store.audio_ms(ONE), store.audio_ms(TWO), store.audio_ms("three")), (0, 200, 300)
        )

    def test_writes_happen_off_the_calling_thread(self):
        store = self.store()
        seen = []
        original = store._write

        def write(*args):
            seen.append(threading.current_thread() is threading.main_thread())
            original(*args)

        store._write = write
        store.set_audio_ms(ONE, 50)
        store.flush()
        self.assertEqual(seen, [False])

    def test_a_burst_of_nudges_writes_the_newest_state(self):
        store = self.store()
        for ms in range(50, 1050, 50):
            store.set_audio_ms(ONE, ms)
        store.flush()
        self.assertEqual(self.saved()["channels"][ONE]["audio_ms"], 1000)

    def test_without_a_path_nothing_is_written(self):
        store = SoundSync(None)
        store.set_audio_ms(ONE, 150)
        self.assertEqual(store.audio_ms(ONE), 150)
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_missing_corrupt_or_foreign_file_counts_as_empty(self):
        self.assertEqual(self.store().audio_ms(ONE), 0)  # missing
        for text in ("{nope", "[]", '{"version": 2, "channels": {}}', '{"version": 1}'):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertLogs("services.sound_sync", "INFO"):
                    self.assertEqual(self.store().audio_ms(ONE), 0)

    def test_bad_entries_are_left_out_and_later_fields_are_read_past(self):
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "channels": {
                        ONE: {"audio_ms": 150, "set": 5.0, "subtitle_ms": 40},  # a later field
                        TWO: {"audio_ms": "150", "set": 5.0},
                        "big": {"audio_ms": 9000, "set": 5.0},
                        "zero": {"audio_ms": 0, "set": 5.0},
                        "flag": {"audio_ms": True, "set": 5.0},
                        "when": {"audio_ms": 50},
                    },
                }
            )
        )
        store = self.store()
        self.assertEqual(store.audio_ms(ONE), 150)
        for key in (TWO, "big", "zero", "flag", "when"):
            self.assertEqual(store.audio_ms(key), 0, key)

    def test_live_tuning_json_stays_as_older_builds_read_it(self):
        # The sync has a file of its own: live_tuning.json keeps its version and fields.
        tuning_path = self.dir / TUNING_FILE
        tuning = LiveTuning(str(tuning_path))
        tuning.remember(ONE, Tuning(rate_locked=True))
        tuning.flush()
        before = tuning_path.read_text()
        store = self.store()
        store.set_audio_ms(ONE, 150)
        store.flush()
        self.assertEqual(tuning_path.read_text(), before)
        self.assertEqual(json.loads(before)["version"], 1)
        self.assertNotIn("audio_ms", before)
        self.assertTrue(LiveTuning(str(tuning_path)).get(ONE).rate_locked)


if __name__ == "__main__":
    unittest.main()
