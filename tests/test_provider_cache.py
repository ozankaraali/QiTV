"""A provider's cache in pieces (2.12): the file, and how ProviderManager reads, writes and
moves an older build's one over."""

import os
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import orjson

from services import provider_cache
from services.provider_manager import ProviderManager


def films(count: int, start: int = 0) -> list[dict]:
    return [
        {
            "id": n,
            "name": f"Film {n}",
            "cmd": f"http://127.0.0.1:9/movie/{n}.mkv",
            "plot": "A quiet evening by the harbour. " * 4,
        }
        for n in range(start, start + count)
    ]


def a_cache() -> dict:
    """A cache with every kind of value a provider's has."""
    return {
        "_cache": {"identity": "abc", "refreshed_at": {"itv": 1000.5, "vod": 1001.0}},
        "itv": {
            "categories": [{"id": "*", "title": "All"}, {"id": "News", "title": "News"}],
            "contents": films(40),
            "sorted_channels": {"News": [0, 1, 2], "Kids": []},
            "timezone": "Nowhere/Here",
            "tz_offset": -90,
        },
        "vod": {  # a portal's: its groups' pages, loaded one by one
            "categories": [{"id": "7", "title": "Drama"}],
            "contents": {"7": films(5, 100), "8": []},
        },
        "series": {
            "categories": [],
            "contents": [],
            "seasons": {"42": [{"id": "1", "name": "Show.Season 1"}]},
            "episodes": {"42": {"1": films(2, 200)}},
            "resolved_base": "http://127.0.0.1:9",
        },
        "old": [{"name": "A"}, {"name": "B"}],  # a section that is a list (an older build's)
    }


class FileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "x.v2.jsonl")

    def test_what_is_written_reads_back_the_same(self):
        cache = a_cache()
        provider_cache.write_cache(self.path, cache)
        self.assertEqual(provider_cache.read_cache(self.path), cache)

    def test_each_line_is_one_document_of_about_a_piece(self):
        cache = {"_cache": {"identity": "abc"}, "vod": {"categories": [], "contents": films(3000)}}
        with mock.patch.object(provider_cache, "PIECE_BYTES", 16 << 10):
            provider_cache.write_cache(self.path, cache)
        with open(self.path, "rb") as f:
            lines = f.read().splitlines()
        self.assertEqual(orjson.loads(lines[0]), {"qitv": "provider-cache", "version": 2})
        self.assertEqual(orjson.loads(lines[1]), {"key": "_cache", "value": {"identity": "abc"}})
        self.assertEqual(orjson.loads(lines[-1]), {"end": True})
        pieces = [orjson.loads(line) for line in lines[3:-1]]
        self.assertGreater(len(pieces), 20)
        self.assertTrue(all(p["key"] == "vod" and p["part"] == "contents" for p in pieces))
        self.assertLess(max(map(len, lines)), 2 * (16 << 10))  # one piece, and one film more
        self.assertEqual(provider_cache.read_cache(self.path), cache)

    def test_reading_and_writing_never_parse_or_dump_more_than_a_piece_at_once(self):
        # orjson holds the GIL for a whole call: a large cache stalled the GUI half a second.
        cache = {
            "vod": {"contents": films(30000), "sorted_channels": {str(n): [n] for n in range(5000)}}
        }
        sizes = []

        def dumps(value, *args, _dumps=orjson.dumps):
            blob = _dumps(value, *args)
            sizes.append(len(blob))
            return blob

        def loads(blob, _loads=orjson.loads):
            sizes.append(len(blob))
            return _loads(blob)

        with mock.patch.object(provider_cache.orjson, "dumps", dumps):
            provider_cache.write_cache(self.path, cache)
        self.assertGreater(os.path.getsize(self.path), 5 * provider_cache.PIECE_BYTES)
        self.assertLess(max(sizes), 1.5 * provider_cache.PIECE_BYTES)  # about one piece
        sizes.clear()
        with mock.patch.object(provider_cache.orjson, "loads", loads):
            self.assertEqual(provider_cache.read_cache(self.path), cache)
        self.assertGreater(len(sizes), 4)
        self.assertLess(max(sizes), 1.5 * provider_cache.PIECE_BYTES)  # about one piece

    def test_a_file_it_did_not_write_or_cut_short_is_refused(self):
        provider_cache.write_cache(self.path, a_cache())
        with open(self.path, "rb") as f:
            whole = f.read()
        for label, data in (
            ("empty", b""),
            ("an older build's", orjson.dumps(a_cache())),
            ("cut short", whole[: whole.rindex(b'{"end"')]),
            ("cut mid-line", whole[: len(whole) // 2]),
        ):
            with self.subTest(label):
                with open(self.path, "wb") as f:
                    f.write(data)
                with self.assertRaises(ValueError):
                    provider_cache.read_cache(self.path)
        with self.assertRaises(FileNotFoundError):
            provider_cache.read_cache(self.path + ".none")

    def test_a_value_json_cannot_hold_fails_and_leaves_the_file(self):
        provider_cache.write_cache(self.path, a_cache())
        for cache in ({"itv": {"contents": [object()]}}, {"itv": {"sorted_channels": {1: [0]}}}):
            with self.subTest(cache=cache), self.assertRaises(TypeError):
                provider_cache.write_cache(self.path, cache)
        self.assertEqual(provider_cache.read_cache(self.path), a_cache())
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["x.v2.jsonl"])  # no temporary


class ManagerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = SimpleNamespace(
            get_config_dir=lambda: directory.name,
            selected_provider_name="Fixture",
            ssl_verify=True,
        )
        self.manager = self.make()
        self.progress = SimpleNamespace(emit=lambda message: None)

    def make(self) -> ProviderManager:
        manager = ProviderManager(self.config)
        self.addCleanup(manager._cache_writer.shutdown, wait=True)
        manager.providers = [
            {"name": "Fixture", "type": "XTREAM", "url": "http://127.0.0.1:9", "username": "u"},
            {"name": "Other", "type": "XTREAM", "url": "http://127.0.0.1:9", "username": "o"},
        ]
        manager.current_provider = manager.providers[0]
        return manager

    def written(self, manager: ProviderManager) -> None:
        manager._cache_writer.submit(lambda: None).result(timeout=5)

    def legacy(self, name: str, cache: dict) -> str:
        """`cache` saved as an older build saved it: "<hash>.json", one document."""
        path = self.manager._legacy_cache_name(self.manager._provider_cache_name(name))
        with open(path, "wb") as f:
            f.write(orjson.dumps(cache))
        return path

    def fresh(self, manager: ProviderManager, sections: dict) -> dict:
        identity = manager.provider_cache_identity(manager.providers[0])
        return {**sections, "_cache": {"identity": identity, "refreshed_at": {"vod": time.time()}}}

    def test_its_cache_file_is_the_new_one(self):
        name = self.manager._provider_cache_name("Fixture")
        self.assertTrue(name.endswith(".v2.jsonl"), name)
        self.manager.current_provider_content = {"vod": {"categories": [], "contents": films(3)}}
        self.manager.save_provider()
        self.written(self.manager)
        self.assertEqual(provider_cache.read_cache(name)["vod"]["contents"], films(3))

    def test_an_older_builds_cache_is_read_once_and_written_anew(self):
        old = self.fresh(self.manager, {"vod": {"categories": [], "contents": films(3)}})
        legacy = self.legacy("Fixture", old)
        self.manager.set_current_provider(self.progress)
        self.assertEqual(self.manager.current_provider_content, old)  # no download
        self.assertFalse(self.manager.is_content_stale("vod"))
        self.written(self.manager)  # no save asked: it is written in pieces at once
        self.assertFalse(os.path.exists(legacy))
        self.assertEqual(
            provider_cache.read_cache(self.manager._provider_cache_name("Fixture")), old
        )
        again = self.make()
        again.set_current_provider(self.progress)
        self.assertEqual(again.current_provider_content, old)  # from the new file now

    def test_an_older_builds_cache_of_another_provider_is_only_read(self):
        # A search reads the providers not loaded yet: their own setup writes theirs anew.
        legacy = self.legacy("Other", {"vod": {"categories": [], "contents": films(2)}})
        self.manager.current_provider_content = {"vod": {"categories": []}}
        found = dict(self.manager.get_all_providers_cached_content())
        self.assertEqual(found["Other"]["vod"]["contents"], films(2))
        self.written(self.manager)
        self.assertTrue(os.path.exists(legacy))
        self.assertFalse(os.path.exists(self.manager._provider_cache_name("Other")))

    def test_the_new_file_wins_over_an_older_builds_left_beside_it(self):
        self.manager.current_provider_content = {"vod": {"categories": [], "contents": films(2)}}
        self.manager.save_provider()
        self.written(self.manager)
        self.legacy("Fixture", {"vod": {"categories": [], "contents": films(9)}})
        again = self.make()
        again.set_current_provider(self.progress)
        self.assertEqual(again.current_provider_content["vod"]["contents"], films(2))

    def test_a_damaged_cache_loads_nothing(self):
        with open(self.manager._provider_cache_name("Fixture"), "wb") as f:
            f.write(b'{"qitv": "provider-cache", "version": 2}\n{"key": "vod", "val')
        self.legacy("Fixture", {"vod": {"categories": [], "contents": films(9)}})
        self.manager.set_current_provider(self.progress)
        self.assertEqual(self.manager.current_provider_content, {})

    def test_an_unlisted_providers_files_go_and_a_listed_ones_older_file_stays(self):
        self.manager.current_provider_content = {"vod": {"categories": []}}
        self.manager.save_provider()
        self.written(self.manager)
        listed = self.legacy("Other", {"vod": {}})
        gone = self.manager._provider_cache_name("Fixture")
        gone_legacy = self.legacy("Fixture", {"vod": {}})
        self.manager.providers = [self.manager.providers[1]]
        self.manager.save_providers()
        self.assertFalse(os.path.exists(gone))
        self.assertFalse(os.path.exists(gone_legacy))
        self.assertTrue(os.path.exists(listed))  # read once, when it is set up

    def test_invalidating_removes_both_files(self):
        self.manager.current_provider_content = {"vod": {"categories": []}}
        self.manager.save_provider()
        self.written(self.manager)
        legacy = self.legacy("Fixture", {"vod": {}})
        self.manager.invalidate_provider_cache("Fixture")
        self.assertFalse(os.path.exists(self.manager._provider_cache_name("Fixture")))
        self.assertFalse(os.path.exists(legacy))


if __name__ == "__main__":
    unittest.main()
