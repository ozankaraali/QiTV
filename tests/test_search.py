import os
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from job_fixtures import ImmediateRunner

from services import thread_cleanup
from services.jobs import JobRunner
from services.provider_manager import ProviderManager
from services.search import SearchWorker, build_index, search_index

APP = QApplication.instance() or QApplication([])


def titles(hits, section=None):
    return [h.title for h in hits if section is None or h.section == section]


def search(caches, text, **kwargs):
    return search_index(build_index(caches), text, **kwargs)


class SearchTests(unittest.TestCase):
    def test_multi_term_matching(self):
        caches = [
            ("P", {"vod": [{"id": "1", "name": "Salt Roads (2024)"}, {"id": "2", "name": "Salt"}]})
        ]
        self.assertEqual(titles(search(caches, "salt road")), ["Salt Roads (2024)"])
        self.assertEqual(titles(search(caches, "road salt")), ["Salt Roads (2024)"])

    def test_accent_and_turkish_folding(self):
        caches = [("P", {"itv": {"contents": [{"id": "1", "name": "İstanbul Haber"}]}})]
        hits = search(caches, "istanbul")
        self.assertEqual(titles(hits), ["İstanbul Haber"])
        self.assertEqual(hits[0].section, "live")
        self.assertEqual(hits[0].provider, "P")

    def test_prefix_hits_come_first(self):
        caches = [
            (
                "P",
                {
                    "vod": [
                        {"id": "1", "name": "The Night Road"},
                        {"id": "2", "name": "Road Night"},
                        {"id": "3", "name": "Night Road"},
                    ]
                },
            )
        ]
        self.assertEqual(
            titles(search(caches, "night")), ["Night Road", "The Night Road", "Road Night"]
        )

    def test_dedupes_across_stb_groups(self):
        item = {"id": "7", "name": "Dup Movie"}
        caches = [("P", {"vod": {"contents": {"g1": [item], "g2": [dict(item)]}}})]
        self.assertEqual(titles(search(caches, "dup")), ["Dup Movie"])

    def test_cache_shapes(self):
        caches = [
            (
                "P",
                {
                    "itv": [{"id": "1", "name": "Alpha live"}],
                    "vod": {"contents": [{"id": "2", "name": "Alpha film"}]},
                    "series": {"contents": {"a": [{"id": "3", "name": "Alpha show"}], "b": "x"}},
                },
            )
        ]
        hits = search(caches, "alpha")
        self.assertEqual(
            [(h.section, h.title) for h in hits],
            [("live", "Alpha live"), ("movies", "Alpha film"), ("series", "Alpha show")],
        )

    def test_categories_are_never_items(self):
        caches = [
            (
                "P",
                {
                    "itv": {
                        "categories": [{"id": "9", "title": "Alpha category"}],
                        "contents": [{"id": "1", "name": "Alpha one"}],
                    }
                },
            )
        ]
        self.assertEqual(titles(search(caches, "alpha")), ["Alpha one"])

    def test_per_section_limit(self):
        items = [{"id": str(i), "name": f"Movie {i}"} for i in range(10)]
        hits = search([("P", {"vod": items})], "movie", limit_per_section=3)
        self.assertEqual(len(hits), 3)

    def test_short_query_gives_nothing(self):
        caches = [("P", {"vod": [{"id": "1", "name": "A film"}]})]
        self.assertEqual(search(caches, "a"), [])
        self.assertEqual(search(caches, "  "), [])


class IndexTests(unittest.TestCase):
    def test_non_list_groups_and_contents_are_ignored(self):
        caches = [
            ("P", {"vod": {"contents": {"a": 5, "b": None, "c": "x"}}, "itv": {"contents": 7}})
        ]
        self.assertEqual(search(caches, "foo"), [])
        self.assertEqual(build_index(caches), [])

    def test_index_folds_titles_once_and_drops_duplicates(self):
        caches = [
            ("P", {"vod": [{"id": "1", "name": "İstanbul Film"}, {"id": "1", "name": "dup"}]})
        ]
        index = build_index(caches)
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0][2], "istanbul film")
        self.assertEqual(titles(search_index(index, "istanbul")), ["İstanbul Film"])

    def test_large_index_no_match_is_fast(self):
        items = [{"id": str(i), "name": f"Night Road Salt Film {i}"} for i in range(75000)]
        caches = [(f"P{k}", {"vod": items}) for k in range(3)]
        index = build_index(caches)
        started = time.perf_counter()
        hits = search_index(index, "zzz")
        elapsed = time.perf_counter() - started
        self.assertEqual(hits, [])
        if not os.environ.get("CI"):
            self.assertLess(elapsed, 0.25)


class CachedContentTests(unittest.TestCase):
    def test_playlists_offer_only_live_channels_to_search(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = SimpleNamespace(
            get_config_dir=lambda: directory.name,
            selected_provider_name="List",
            prefer_https=False,
            ssl_verify=True,
        )
        manager = ProviderManager(config)
        self.addCleanup(manager._cache_writer.shutdown, wait=True)
        manager.providers = [
            {"name": "List", "type": "M3UPLAYLIST", "url": "https://example.test/a"},
            {"name": "Portal", "type": "XTREAM", "url": "https://example.test/b"},
        ]
        content = {
            "List": {"itv": [{"name": "News"}], "vod": [{"name": "News"}], "series": {}},
            "Portal": {"itv": [], "vod": [{"name": "Film"}], "series": [{"name": "Show"}]},
        }
        with mock.patch.object(
            manager, "_load_provider_cache", side_effect=lambda p: dict(content[p["name"]])
        ):
            cached = dict(manager.get_all_providers_cached_content())
        self.assertEqual(set(cached["List"]), {"itv"})
        self.assertEqual(set(cached["Portal"]), {"itv", "vod", "series"})


class LoadedContentTests(unittest.TestCase):
    def test_a_group_added_while_the_worker_reads_does_not_break_it(self):
        from services.search import iter_items

        content = {"contents": {"g1": [{"name": "A"}], "g2": [{"name": "B"}]}}
        seen = []
        for item in iter_items(content):
            seen.append(item["name"])
            content["contents"]["g3"] = [{"name": "C"}]  # the GUI thread loads a group
        self.assertEqual(seen, ["A", "B"])

    def test_a_section_added_while_the_loader_reads_does_not_break_it(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = SimpleNamespace(
            get_config_dir=lambda: directory.name,
            selected_provider_name="P",
            prefer_https=False,
            ssl_verify=True,
        )
        manager = ProviderManager(config)
        self.addCleanup(manager._cache_writer.shutdown, wait=True)
        manager.providers = [{"name": "P", "type": "XTREAM", "url": "https://example.test/b"}]

        class Meanwhile(str):
            """A key that adds a section to its dict when the loader compares it (as the GUI
            thread could between two of the loader's steps)."""

            target: dict = {}

            def __ne__(self, other):
                self.target.setdefault("series", {})
                return str.__ne__(self, other)

            __hash__ = str.__hash__

        class Growing(dict):
            pass

        grown = Growing({Meanwhile("itv"): [{"name": "A"}], "vod": [{"name": "B"}]})
        Meanwhile.target = grown
        held = {"P": grown}
        found = dict(manager.get_all_providers_cached_content(held))
        self.assertEqual(set(found["P"]), {"itv", "vod"})

    def test_a_providers_content_in_memory_is_not_read_from_disk_again(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = SimpleNamespace(
            get_config_dir=lambda: directory.name,
            selected_provider_name="Portal",
            prefer_https=False,
            ssl_verify=True,
        )
        manager = ProviderManager(config)
        self.addCleanup(manager._cache_writer.shutdown, wait=True)
        manager.providers = [
            {"name": "List", "type": "M3UPLAYLIST", "url": "https://example.test/a"},
            {"name": "Portal", "type": "XTREAM", "url": "https://example.test/b"},
        ]
        disk = {"List": {"itv": [{"name": "On disk"}]}, "Portal": {"itv": [{"name": "Old"}]}}
        held = {"Portal": {"itv": [{"name": "Held"}], "vod": [{"name": "Film"}], "_cache": {}}}
        with mock.patch.object(
            manager, "_load_provider_cache", side_effect=lambda p: dict(disk[p["name"]])
        ) as load:
            found = dict(manager.get_all_providers_cached_content(held))
        self.assertEqual([c["name"] for c in found["Portal"]["itv"]], ["Held"])
        self.assertEqual(set(found["Portal"]), {"itv", "vod"})
        self.assertEqual(found["List"]["itv"][0]["name"], "On disk")
        self.assertEqual([c.args[0]["name"] for c in load.call_args_list], ["List"])


class FakeProviders:
    def __init__(self, caches):
        self.caches = caches
        self.calls = 0
        self.loaded = None

    def get_all_providers_cached_content(self, loaded=None):
        self.calls += 1
        self.loaded = loaded
        return self.caches


class SearchWorkerTests(unittest.TestCase):
    def setUp(self):
        self.caches = [("P", {"vod": [{"id": "1", "name": "Salt Roads"}]})]

    def test_loads_caches_and_searches(self):
        manager = FakeProviders(self.caches)
        results = []
        ImmediateRunner().run_worker(
            SearchWorker(manager, "salt"), lambda ctx, payload: results.append(payload)
        )
        caches, index, hits = results[0]
        self.assertEqual(len(index), 1)
        self.assertEqual(caches, self.caches)
        self.assertEqual(titles(hits), ["Salt Roads"])
        self.assertEqual(manager.calls, 1)

    def test_the_content_catalogs_hold_goes_to_the_loader(self):
        manager = FakeProviders(self.caches)
        held = {"P": {"vod": [{"id": "1", "name": "Salt Roads"}]}}
        ImmediateRunner().run_worker(
            SearchWorker(manager, "salt", loaded=held), lambda ctx, payload: None
        )
        self.assertIs(manager.loaded, held)

    def test_given_caches_skip_the_loader(self):
        manager = FakeProviders([])
        results = []
        ImmediateRunner().run_worker(
            SearchWorker(manager, "salt", self.caches), lambda ctx, payload: results.append(payload)
        )
        self.assertEqual(titles(results[0][2]), ["Salt Roads"])
        self.assertEqual(manager.calls, 0)

    def test_given_index_skips_loader_and_build(self):
        manager = FakeProviders([])
        index = build_index(self.caches)
        results = []
        ImmediateRunner().run_worker(
            SearchWorker(manager, "salt", index=index), lambda ctx, payload: results.append(payload)
        )
        self.assertIs(results[0][1], index)
        self.assertEqual(titles(results[0][2]), ["Salt Roads"])
        self.assertEqual(manager.calls, 0)

    def test_real_runner_leaves_no_pending_thread(self):
        runner = JobRunner()
        results = []
        runner.run_worker(
            SearchWorker(FakeProviders(self.caches), "salt"),
            lambda ctx, payload: results.append(payload),
        )
        for _ in range(300):
            if results and not thread_cleanup.has_pending_threads():
                break
            QTest.qWait(10)
        self.assertTrue(results, "SearchWorker delivered no result within 3 s")
        self.assertEqual(titles(results[0][2]), ["Salt Roads"])
        self.assertFalse(thread_cleanup.has_pending_threads())


if __name__ == "__main__":
    unittest.main()
