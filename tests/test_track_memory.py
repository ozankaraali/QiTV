"""The tracks chosen for each film, series and channel (services.track_memory, 2.9.1)."""

import json
import os
import shutil
import tempfile
import unittest

from services.track_memory import FILE_NAME, TrackMemory, title_key


class TitleKeyTests(unittest.TestCase):
    def test_a_film_by_its_tmdb_id_when_it_has_one(self):
        renumbered = {"stream_id": 99, "tmdb": "98"}
        self.assertEqual(
            title_key("P", {"stream_id": 7, "tmdb": "98"}, episode=False),
            title_key("P", renumbered, episode=False),
        )  # a provider that numbers it anew keeps it
        self.assertNotEqual(
            title_key("P", {"stream_id": 7}, episode=False),
            title_key("P", {"stream_id": 8}, episode=False),
        )
        self.assertNotEqual(  # another provider's is another key
            title_key("P", {"stream_id": 7}, episode=False),
            title_key("Q", {"stream_id": 7}, episode=False),
        )
        self.assertEqual(title_key("P", {}, episode=False), "")

    def test_every_episode_shares_its_series_key(self):
        series = {"context": {"series": {"series_id": 5, "name": "A series"}}}
        one = title_key("P", {"id": 1}, episode=True, context=series["context"])
        two = title_key("P", {"id": 2}, episode=True, context=series["context"])
        self.assertEqual(one, two)
        self.assertNotEqual(one, title_key("P", {"stream_id": 5}, episode=False))
        # Without the series in the context, the episode's own series number names it.
        self.assertNotEqual(title_key("P", {"series_id": 5}, episode=True), "")


class TrackMemoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.path = os.path.join(self.folder, FILE_NAME)

    def test_a_choice_is_kept_in_its_file_for_the_next_run(self):
        memory = TrackMemory(self.path)
        memory.choose("k", "audio", 13, "Track 13 - [Turkish]")
        memory.choose("k", "spu", -1, "Disable")
        memory.flush()
        again = TrackMemory(self.path)
        self.assertEqual(
            again.chosen("k"), {"audio": (13, "Track 13 - [Turkish]"), "spu": (-1, "Disable")}
        )
        self.assertEqual(again.chosen("other"), {})
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("Gladiator", f.read())  # keys are hashes, made by title_key

    def test_a_bad_entry_or_another_version_is_left_out(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": 1,
                    "channels": {
                        "good": {"audio": [2, "Deutsch"], "set": 1.0},
                        "bad": {"audio": ["2", "Deutsch"], "set": 1.0},
                        "empty": {"set": 1.0},
                    },
                },
                f,
            )
        memory = TrackMemory(self.path)
        self.assertEqual(memory.keys(), {"good"})
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "channels": {"good": {"audio": [2, "x"], "set": 1}}}, f)
        self.assertEqual(TrackMemory(self.path).keys(), set())

    def test_beyond_the_cap_the_oldest_choice_goes(self):
        clock = iter(range(100))
        memory = TrackMemory(None, max_entries=2, now=lambda: float(next(clock)))
        for key in ("a", "b", "c"):
            memory.choose(key, "audio", 1, "English")
        self.assertEqual(memory.keys(), {"b", "c"})


if __name__ == "__main__":
    unittest.main()
