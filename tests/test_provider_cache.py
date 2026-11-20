"""A provider's cache in pieces (2.12): the file, its pages beside it (what opened a series or
a portal's group at a time), and how ProviderManager reads, writes and moves an older build's
one over."""

import os
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from job_fixtures import ImmediateRunner
import orjson

from services import provider_cache, provider_manager
from services.catalog import CatalogService
from services.provider_manager import ProviderManager
from tests.test_catalog_service import FakeFactory


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


def lists_only(cache: dict) -> dict:
    """`cache` without its pages, as the lists' file holds it."""
    cache = {key: dict(value) if isinstance(value, dict) else value for key, value in cache.items()}
    cache["vod"]["contents"] = {}  # a portal's groups: none opened
    for part in ("seasons", "episodes"):
        del cache["series"][part]
    return cache


class FileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "x.v2.jsonl")
        self.pages = os.path.join(directory.name, "x.v2.pages.jsonl")

    def test_what_is_written_reads_back_the_same(self):
        cache = a_cache()
        provider_cache.write_cache(self.path, cache)
        provider_cache.write_pages(self.pages, cache)
        read = provider_cache.read_cache(self.path)
        self.assertEqual(read, lists_only(cache))  # the lists alone
        provider_cache.read_pages(self.pages, read)
        self.assertEqual(read, cache)

    def test_the_pages_hold_what_opened_and_the_lists_none_of_it(self):
        cache = a_cache()
        provider_cache.write_pages(self.pages, cache)
        with open(self.pages, "rb") as f:
            lines = [orjson.loads(line) for line in f.read().splitlines()]
        self.assertEqual(lines[0], {"qitv": "provider-pages", "version": 2})
        self.assertEqual(lines[-1], {"end": True})
        self.assertEqual(
            {(line["key"], line["part"], line["token"]) for line in lines[1:-1]},
            {
                ("vod", "contents", 1001.0),  # a portal's group pages
                ("series", "seasons", None),
                ("series", "episodes", None),
                ("series", "resolved_base", None),  # where its episodes play from
            },
        )
        self.assertTrue(provider_cache.has_pages(cache))
        self.assertFalse(provider_cache.has_pages(lists_only(cache)))

    def test_pages_saved_with_another_load_of_their_section_are_left_out(self):
        # The lists were written (a refresh) and the pages not yet: the old ones' pages stay
        # out of the new lists (a portal's groups may have changed).
        cache = a_cache()
        provider_cache.write_pages(self.pages, cache)
        cache["_cache"]["refreshed_at"]["vod"] = 2002.0  # loaded again since
        cache["_cache"]["refreshed_at"]["series"] = 2003.0
        read = lists_only(cache)
        provider_cache.read_pages(self.pages, read)
        self.assertEqual(read, lists_only(cache))

    def test_a_damaged_pages_file_puts_nothing_in(self):
        cache = a_cache()
        provider_cache.write_pages(self.pages, cache)
        with open(self.pages, "rb") as f:
            whole = f.read()
        for label, data in (("cut short", whole[: whole.rindex(b'{"end"')]), ("empty", b"")):
            with self.subTest(label):
                with open(self.pages, "wb") as f:
                    f.write(data)
                read = lists_only(cache)
                with self.assertRaises(ValueError):
                    provider_cache.read_pages(self.pages, read)
                self.assertEqual(read, lists_only(cache))

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
        self.assertEqual(provider_cache.read_cache(self.path), lists_only(a_cache()))
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["x.v2.jsonl"])  # no temporary


class ManagerCase(unittest.TestCase):
    """A ProviderManager over a fresh folder: "Fixture" (current) and "Other"."""

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


class ManagerTests(ManagerCase):
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

    @staticmethod
    def age(path: str, seconds: float) -> None:
        """`path` as written `seconds` ago."""
        then = time.time() - seconds
        os.utime(path, (then, then))

    def test_an_older_builds_file_written_since_wins_over_the_new_one(self):
        # 2.12, then 2.11 gone back to (it writes only "<hash>.json"), then 2.12 again: the
        # newer lists are 2.11's. They are read, and written anew in pieces at once.
        self.manager.current_provider_content = {"vod": {"categories": [], "contents": films(2)}}
        self.manager.save_provider()
        self.written(self.manager)
        lists = self.manager._provider_cache_name("Fixture")
        self.age(lists, 3600)
        newer = self.fresh(self.manager, {"vod": {"categories": [], "contents": films(9)}})
        legacy = self.legacy("Fixture", newer)
        again = self.make()
        again.set_current_provider(self.progress)
        self.assertEqual(again.current_provider_content, newer)
        self.written(again)
        self.assertFalse(os.path.exists(legacy))
        self.assertEqual(provider_cache.read_cache(lists), newer)

    def test_the_new_file_wins_over_an_older_builds_older_one(self):
        legacy = self.legacy("Fixture", {"vod": {"categories": [], "contents": films(9)}})
        self.age(legacy, 3600)  # (left behind: never a newer one)
        self.manager.current_provider_content = {"vod": {"categories": [], "contents": films(2)}}
        self.manager.save_provider()
        self.written(self.manager)
        self.assertFalse(os.path.exists(legacy))  # the lists' file written: it goes
        self.legacy("Fixture", {"vod": {"categories": [], "contents": films(9)}})
        self.age(legacy, 3600)
        again = self.make()
        again.set_current_provider(self.progress)
        self.assertEqual(again.current_provider_content["vod"]["contents"], films(2))

    def test_the_files_it_removes_go_in_their_turn(self):
        # Windows refuses to remove a file being read: a search reading the older build's
        # file or the pages made the removal fail.
        removed = []
        remove = os.remove

        def removing(path):
            if path.endswith((".json", ".pages.jsonl")):
                removed.append((os.path.basename(path), provider_manager._CACHE_FILE_TURN.locked()))
            remove(path)

        manager = self.manager
        self.age(self.legacy("Fixture", {"vod": {}}), 3600)
        manager.current_provider_content = {"series": {"seasons": {"42": [{"id": "1"}]}}}
        with mock.patch("services.provider_manager.os.remove", removing):
            manager.save_provider()  # the lists' file: the older build's goes
            self.written(manager)
            manager.current_provider_content = {"series": {"categories": []}}
            manager.save_provider()  # no pages left: the pages' file goes
            self.written(manager)
        self.assertEqual({name.split(".", 1)[1] for name, _ in removed}, {"json", "v2.pages.jsonl"})
        self.assertTrue(all(turn for _, turn in removed))

    def test_a_damaged_cache_loads_nothing(self):
        lists = self.manager._provider_cache_name("Fixture")
        for label, data in (
            ("cut mid-line", b'{"key": "vod", "val'),
            ("a part that is no key", b'{"key": "vod", "part": ["contents"], "items": []}'),
            ("a part that is a map", b'{"key": "vod", "part": {"a": 1}, "entries": {}}'),
        ):
            with self.subTest(label):
                header = b'{"qitv": "provider-cache", "version": 2}\n'
                head = b'{"key": "vod", "value": {"contents": []}}\n'
                with open(lists, "wb") as f:
                    f.write(header + head + data + b'\n{"end": true}\n')
                legacy = self.legacy("Fixture", {"vod": {"categories": [], "contents": films(9)}})
                self.age(legacy, 3600)
                self.manager.set_current_provider(self.progress)  # (raises nothing)
                self.assertEqual(self.manager.current_provider_content, {})

    def test_pages_in_no_place_of_theirs_are_dropped_and_the_lists_load(self):
        manager = self.manager
        manager.current_provider_content = {"series": {"categories": [], "contents": films(2)}}
        manager.save_provider()
        self.written(manager)
        pages = manager._pages_name(manager._provider_cache_name("Fixture"))
        page = {"key": "series", "token": None}
        for label, line in (
            ("a value no page has", {**page, "part": "contents", "value": 1}),
            ("entries no page has", {**page, "part": "categories", "entries": {}}),
            ("a part that is a list", {**page, "part": [1], "entries": {}}),
        ):
            with self.subTest(label):
                lines = [{"qitv": "provider-pages", "version": 2}, line, {"end": True}]
                with open(pages, "wb") as f:
                    f.write(b"".join(orjson.dumps(doc) + b"\n" for doc in lines))
                again = self.make()
                again.set_current_provider(self.progress)
                series = again.current_provider_content["series"]
                self.assertEqual(series, {"categories": [], "contents": films(2)})

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

    def test_invalidating_removes_every_file(self):
        self.manager.current_provider_content = {"series": {"seasons": {"42": [{"id": "1"}]}}}
        self.manager.save_provider()
        self.written(self.manager)
        cache = self.manager._provider_cache_name("Fixture")
        pages = self.manager._pages_name(cache)
        self.assertTrue(os.path.exists(pages))
        legacy = self.legacy("Fixture", {"vod": {}})
        self.manager.invalidate_provider_cache("Fixture")
        for path in (cache, pages, legacy):
            self.assertFalse(os.path.exists(path), path)


class PagesTests(ManagerCase):
    """2.12: a series, a season or a portal's group opened writes its pages beside the lists,
    never the lists again."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def lists(self, manager=None):
        return (manager or self.manager)._provider_cache_name("Fixture")

    def stamp(self, path):
        """What a rewrite would change: the file's inode and time (it is replaced whole)."""
        status = os.stat(path)
        return status.st_ino, status.st_mtime_ns

    def saved_series(self):
        manager = self.manager
        manager.current_provider_content = {
            "series": {"categories": [], "contents": films(500), "stream_base": "http://b"}
        }
        manager.mark_content_fresh("series")
        manager.save_provider()
        self.written(manager)
        return self.stamp(self.lists())

    def service(self, factory):
        config = SimpleNamespace(
            selected_provider_name="Fixture", ssl_verify=True, save_config=lambda: None
        )
        return CatalogService(config, self.manager, None, runner=ImmediateRunner(), factory=factory)

    def test_opening_a_series_writes_its_seasons_and_not_the_lists(self):
        before = self.saved_series()
        factory = FakeFactory()
        factory.results["xtream_series"] = {
            "result": {
                "resolved_base": "http://b",
                "seasons": [{"id": "1", "name": "Season 1", "episodes": [{"id": "5"}]}],
            }
        }
        written = mock.patch.object(
            provider_cache, "write_cache", wraps=provider_cache.write_cache
        ).start()
        self.addCleanup(mock.patch.stopall)
        service = self.service(factory)
        service.request_seasons({"id": 42, "name": "Show"})
        self.written(self.manager)
        written.assert_not_called()
        self.assertEqual(self.stamp(self.lists()), before)  # the 500 series were not written
        again = self.make()
        again.set_current_provider(self.progress)
        [season] = again.current_provider_content["series"]["seasons"]["42"]
        self.assertEqual(season["name"], "Show.Season 1")
        self.assertEqual(len(again.current_provider_content["series"]["contents"]), 500)

    def test_opening_a_portals_group_writes_its_page_and_not_the_lists(self):
        manager = self.manager
        manager.providers[0] = manager.current_provider = {
            "name": "Fixture",
            "type": "STB",
            "url": "http://127.0.0.1:9/c/",
            "mac": "00:1A:79",
        }
        manager.current_provider_content = {
            "vod": {"categories": [{"id": "7", "title": "Drama"}], "contents": {}}
        }
        manager.mark_content_fresh("vod")
        manager.save_provider()
        self.written(manager)
        before = self.stamp(self.lists())
        factory = FakeFactory()
        factory.results["stb_list"] = {"result": {"items": films(3)}}
        self.service(factory).request_group("movies", "7")
        self.written(manager)
        self.assertEqual(self.stamp(self.lists()), before)
        again = self.make()
        again.providers[0] = again.current_provider = manager.current_provider
        loaded = again._load_provider_cache(manager.current_provider)
        self.assertEqual(
            [f["name"] for f in loaded["vod"]["contents"]["7"]], ["Film 0", "Film 1", "Film 2"]
        )

    def test_a_list_loaded_again_drops_its_old_pages(self):
        self.saved_series()
        manager = self.manager
        manager.current_provider_content["series"]["seasons"] = {"42": [{"id": "1"}]}
        manager.save_provider_pages()
        self.written(manager)
        pages = manager._pages_name(self.lists())
        self.assertTrue(os.path.exists(pages))
        manager.current_provider_content["series"] = {"categories": [], "contents": films(2)}
        manager.mark_content_fresh("series")
        manager.save_provider()  # what _store_section does
        self.written(manager)
        self.assertFalse(os.path.exists(pages))  # nothing opened in the new list yet
        again = self.make()
        again.set_current_provider(self.progress)
        self.assertNotIn("seasons", again.current_provider_content["series"])

    def test_a_queued_save_of_the_lists_is_not_lost_to_a_pages_save_after_it(self):
        before = self.saved_series()
        manager = self.manager
        gate = threading.Event()
        manager._cache_writer.submit(gate.wait, 5)
        try:
            manager.current_provider_content["series"] = {"categories": [], "contents": films(3)}
            manager.save_provider()  # a refresh stored the series list ...
            manager.current_provider_content["series"]["seasons"] = {"9": [{"id": "1"}]}
            manager.save_provider_pages()  # ... and a series opened before it was written
            loaded = manager._load_provider_cache(manager.current_provider)  # (meanwhile)
            self.assertEqual(loaded["series"]["seasons"], {"9": [{"id": "1"}]})
        finally:
            gate.set()
        self.written(manager)
        self.assertNotEqual(self.stamp(self.lists()), before)
        again = self.make()
        again.set_current_provider(self.progress)
        self.assertEqual(again.current_provider_content["series"]["contents"], films(3))
        self.assertEqual(again.current_provider_content["series"]["seasons"], {"9": [{"id": "1"}]})

    def test_an_unlisted_providers_pages_go_too(self):
        self.manager.current_provider_content = {"series": {"seasons": {"42": [{"id": "1"}]}}}
        self.manager.save_provider()
        self.written(self.manager)
        pages = self.manager._pages_name(self.lists())
        self.manager.providers = [self.manager.providers[1]]
        self.manager.save_providers()
        self.assertFalse(os.path.exists(pages))


if __name__ == "__main__":
    unittest.main()
