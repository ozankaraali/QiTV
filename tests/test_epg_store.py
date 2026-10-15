import os
import pickle
import tempfile
import unittest
from unittest import mock

from services import epg_store
from services.epg_store import PackedGuide, read_guide, write_guide
from services.multikeydict import MultiKeyDict


def programmes(channel, count):
    return [
        {"@start": f"2026101{n % 10}000000 +0000", "@channel": channel, "title": {"__text": "x"}}
        for n in range(count)
    ]


def big_guide(channels=50, per_channel=30):
    guide = MultiKeyDict()
    for n in range(channels):
        guide[(f"ch{n}", f"Channel {n}", f"channel {n}")] = programmes(f"ch{n}", per_channel)
    return guide


class GuideStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "guide.guide")

    def round_trip(self, guide):
        write_guide(self.path, guide)
        return read_guide(self.path)

    def test_multikey_guide_round_trips_with_its_key_map(self):
        guide = big_guide()
        guide[("shared", "ch1")] = programmes("shared", 2)  # "ch1" now maps to this tuple
        del guide["Channel 7"]
        loaded = self.round_trip(guide)
        self.assertIsInstance(loaded, MultiKeyDict)
        self.assertEqual(len(loaded), len(guide))
        self.assertEqual(list(loaded.items()), list(guide.items()))
        self.assertEqual(loaded._keys_map, guide._keys_map)
        self.assertEqual(loaded["ch1"], guide["ch1"])
        self.assertNotIn("ch7", loaded)
        self.assertIsNone(loaded.get("ch7"))
        with self.assertRaises(KeyError):
            loaded["ch7"]

    def test_a_loaded_guide_unpickles_only_the_channels_looked_up(self):
        loaded = self.round_trip(big_guide(channels=10))
        self.assertIsInstance(loaded, PackedGuide)
        self.assertEqual(loaded.packed(), 10)
        first = loaded["Channel 3"]
        self.assertEqual(first, programmes("ch3", 30))
        self.assertIs(loaded.get("ch3"), first)  # kept: the same programme dicts again
        self.assertIs(loaded.setdefault(("ch3", "Channel 3", "channel 3")), first)
        self.assertEqual(loaded.packed(), 9)
        self.assertIn("channel 5", loaded)  # membership reads only the key map
        self.assertEqual(loaded.packed(), 9)

    def test_writing_returns_the_guide_as_it_loads(self):
        guide = big_guide(channels=4)
        written = write_guide(self.path, guide)
        self.assertIsInstance(written, PackedGuide)
        self.assertEqual(written.packed(), 4)
        self.assertEqual(list(written.items()), list(guide.items()))
        again = os.path.join(os.path.dirname(self.path), "again.guide")
        write_guide(again, read_guide(self.path))  # a packed guide saves without unpickling
        self.assertEqual(list(read_guide(again).items()), list(guide.items()))

    def test_plain_dict_guide_round_trips(self):  # STB portals' guides are dicts
        guide = {str(n): programmes(str(n), 3) for n in range(40)}
        loaded = self.round_trip(guide)
        self.assertIs(type(loaded), dict)
        self.assertEqual(loaded, guide)
        self.assertEqual(list(loaded), list(guide))

    def test_empty_guides_round_trip(self):
        self.assertEqual(len(self.round_trip(MultiKeyDict())), 0)
        self.assertEqual(self.round_trip({}), {})

    def test_a_large_guide_is_read_in_many_short_pieces(self):
        # Each pickle.load holds the GIL until it returns: the GUI thread can only run
        # between pieces, so a guide is never read in one go.
        guide = big_guide(channels=200, per_channel=40)
        loads = []
        real_load = pickle.load

        def counting(file):
            loads.append(1)
            return real_load(file)

        with (
            mock.patch.object(epg_store, "PIECE_BYTES", 16 * 1024),
            mock.patch.object(epg_store, "PIECE_ENTRIES", 100),
        ):
            write_guide(self.path, guide)
        with mock.patch.object(epg_store.pickle, "load", side_effect=counting):
            loaded = read_guide(self.path)
        self.assertEqual(list(loaded.items()), list(guide.items()))
        self.assertGreater(len(loads), 20)
        with open(self.path, "rb") as f:
            pickle.load(f)  # the header
            first = pickle.load(f)
        self.assertLess(len(first), 200)

    def test_a_truncated_file_raises(self):
        write_guide(self.path, big_guide())
        with open(self.path, "r+b") as f:
            f.truncate(os.path.getsize(self.path) // 2)
        with self.assertRaises((EOFError, pickle.UnpicklingError)):
            read_guide(self.path)

    def test_a_file_of_another_kind_raises(self):
        with open(self.path, "wb") as f:
            pickle.dump(big_guide(), f)  # the earlier single-pickle format
        with self.assertRaises(ValueError):
            read_guide(self.path)

    def test_writing_replaces_the_file_whole(self):
        write_guide(self.path, big_guide(channels=3))
        with mock.patch.object(epg_store.pickle, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_guide(self.path, big_guide(channels=9))
        self.assertEqual(len(read_guide(self.path)), 3)  # the old guide is intact
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["guide.guide"])


if __name__ == "__main__":
    unittest.main()
