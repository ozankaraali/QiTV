import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from job_fixtures import ImmediateRunner
from timer_fixtures import wait_until

from services.library import LibraryModel
from services.provider_ids import provider_of
from services.watch_state import WatchState
from ui.library.search_session import (
    DEBOUNCE_MS,
    SEARCH_PROMPT,
    SEARCHING_TEXT,
    SearchSession,
)

app = QApplication.instance() or QApplication([])

WAIT_MS = DEBOUNCE_MS + 150


class Catalog(QObject):
    sectionReady = Signal(str)
    sectionFailed = Signal(str, str)
    groupReady = Signal(str, str)
    groupFailed = Signal(str, str, str)
    providerReady = Signal(str)

    def current_provider_name(self):
        return "P"

    def all_providers(self):
        return False

    def content_version(self):
        return 1

    def sections(self):
        return ["live", "movies", "series"]

    def groups(self, section):
        return []

    def items(self, section, gid):
        return None

    def request_section(self, section, *, force=False):
        pass

    def request_group(self, section, gid):
        pass


class Providers:
    def __init__(self, caches):
        self.caches = caches
        self.loads = 0

    def get_all_providers_cached_content(self, loaded=None):
        self.loads += 1
        return list(self.caches)


class DeferredRunner:
    def __init__(self):
        self.jobs = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.jobs.append((worker, on_finished, on_error, context))
        return worker

    def finish(self, k):
        worker, on_finished, on_error, context = self.jobs[k]
        ImmediateRunner().run_worker(worker, on_finished, on_error, context)


CACHES = [
    (
        "P",
        {
            "itv": [{"id": "c1", "name": "Salt TV", "logo": "http://logos.invalid/1"}],
            "vod": [{"id": "1", "name": "Salt Roads"}, {"id": "2", "name": "Sand Castle"}],
        },
    ),
    ("Q", {"vod": [{"id": "9", "name": "Salt Lake"}]}),
]


class SearchSessionTests(unittest.TestCase):
    def setUp(self):
        cfg = SimpleNamespace(favorites=[], watch_history=[], playback_positions={})
        self.watch = WatchState(cfg)
        self.model = LibraryModel(Catalog(), cfg, self.watch)
        self.providers = Providers(CACHES)
        self.runner = DeferredRunner()
        self.session = SearchSession(self.providers, self.runner, self.model, self.watch)
        self.addCleanup(self.session.shutdown)
        self.changes = 0

        def changed():
            self.changes += 1

        self.session.changed.connect(changed)

    def titles(self):
        return [title for title, _items in self.session.sections()]

    def names(self):
        return [item["name"] for _title, items in self.session.sections() for item in items]

    def test_one_search_in_flight_and_only_the_latest_query_waits(self):
        session = self.session
        for text in ("sa", "sal", "salt", "salt r"):
            session.set_query(text)
            QTest.qWait(WAIT_MS)
        self.assertEqual(len(self.runner.jobs), 1)  # the first, cold search is still running
        self.assertEqual(session.message(), SEARCHING_TEXT)
        self.runner.finish(0)
        self.assertEqual(len(self.runner.jobs), 2)  # the latest query, with the folded index
        self.assertIsNotNone(self.runner.jobs[1][0]._index)
        self.assertIsNone(session.hits)  # "sa" results never flash
        self.runner.finish(1)
        self.assertEqual(self.names(), ["Salt Roads"])
        self.assertEqual(self.providers.loads, 1)

    def test_the_first_search_gets_the_content_the_catalogs_hold_the_next_do_not(self):
        held = {"P": {"vod": []}}
        session = SearchSession(
            self.providers, self.runner, self.model, self.watch, loaded=lambda: held
        )
        self.addCleanup(session.shutdown)
        session.set_query("salt")
        session.run_now()
        self.assertIs(self.runner.jobs[-1][0]._loaded, held)
        self.runner.finish(len(self.runner.jobs) - 1)  # (a fake manager: no loaded argument)
        session.set_query("salt r")
        session.run_now()
        self.assertIsNone(self.runner.jobs[-1][0]._loaded)  # the folded index is kept

    def test_sections_are_titled_by_provider_and_section(self):
        self.session.set_query("salt")
        self.session.run_now()
        self.runner.finish(0)
        self.assertEqual(self.titles(), ["P · Live TV (1)", "P · Movies (1)", "Q · Movies (1)"])
        self.assertEqual(self.session.count_text(), "3 results")
        self.assertEqual(self.session.hit_at(2).provider, "Q")
        item = self.session.hit_at(0).item
        self.assertEqual(self.session.hit_for(item).section, "live")
        self.assertEqual(
            self.session.poster_url(item, lambda url: "logo:" + url), "logo:http://logos.invalid/1"
        )

    def test_the_section_and_provider_shown_come_first(self):
        where = ["movies", "Q"]
        session = SearchSession(
            self.providers, self.runner, self.model, self.watch, where=lambda: tuple(where)
        )
        self.addCleanup(session.shutdown)
        session.set_query("salt")
        session.run_now()
        self.runner.finish(0)
        titles = [title for title, _items in session.sections()]
        self.assertEqual(titles, ["Q · Movies (1)", "P · Movies (1)", "P · Live TV (1)"])
        self.assertEqual(session.hit_at(0).provider, "Q")  # the grid's order
        where[:] = ["live", "P"]
        session.rerun()
        self.runner.finish(1)
        titles = [title for title, _items in session.sections()]
        self.assertEqual(titles, ["P · Live TV (1)", "P · Movies (1)", "Q · Movies (1)"])

    def test_hits_are_stamped_with_their_provider(self):
        self.session.set_query("salt")
        self.session.run_now()
        self.runner.finish(0)
        hits = self.session.hits
        self.assertEqual([provider_of(h.item) for h in hits], ["P", "P", "Q"])
        self.assertEqual([h.provider for h in hits], ["P", "P", "Q"])
        shown = [item for _title, items in self.session.sections() for item in items]
        self.assertEqual([provider_of(i) for i in shown], ["P", "P", "Q"])
        self.assertIs(self.session.hit_for(shown[2]), hits[2])
        self.assertNotIn("_provider", CACHES[1][1]["vod"][0])  # the cache itself is untouched

    def test_one_character_shows_the_prompt_without_searching(self):
        self.session.set_query("s")
        self.session.run_now()
        self.assertEqual(self.runner.jobs, [])
        self.assertIsNone(self.session.hits)
        self.assertEqual(self.session.message(), SEARCH_PROMPT)
        self.assertEqual(self.session.count_text(), "")

    def test_nothing_found(self):
        self.session.set_query("zz")
        self.session.run_now()
        self.runner.finish(0)
        self.assertEqual(self.session.hits, [])
        self.assertEqual(self.session.message(), "Nothing matches “zz”")
        self.assertEqual(self.session.count_text(), "No results")

    def test_clearing_ignores_the_search_under_way(self):
        self.session.set_query("salt")
        self.session.run_now()
        self.session.clear()
        self.runner.finish(0)
        self.assertIsNone(self.session.hits)
        self.assertEqual(self.session.query, "")

    def test_a_dropped_snapshot_is_not_kept_and_rerun_reads_again(self):
        self.session.set_query("salt")
        self.session.run_now()
        self.session.drop_snapshot()  # a provider became ready meanwhile
        self.session.rerun()  # queued behind the running search
        self.runner.finish(0)
        self.assertEqual(len(self.runner.jobs), 2)
        self.assertIsNone(self.runner.jobs[1][0]._index)  # the old snapshot was not kept
        self.runner.finish(1)
        self.assertEqual(self.providers.loads, 2)
        self.assertEqual(len(self.session.hits), 3)

    def test_failure_message(self):
        self.session.set_query("salt")
        self.session.run_now()
        worker, _done, on_error, context = self.runner.jobs[0]
        with self.assertLogs("ui.library.search_session", level="WARNING"):
            on_error(context, "The background task failed")
        self.assertTrue(self.session.message().startswith("Couldn't search"))
        self.assertEqual(self.session.hits, [])

    def test_a_snapshot_no_search_used_for_a_while_goes(self):
        # Every provider's titles (400,000 rows) were kept for the rest of the run: each full
        # garbage collection walked them, long after the search was left.
        session = SearchSession(self.providers, self.runner, self.model, self.watch, idle_ms=200)
        self.addCleanup(session.shutdown)
        session.set_query("salt")
        session.run_now()
        QTest.qWait(300)  # still searching: nothing goes
        self.runner.finish(0)
        self.assertIsNotNone(session._index)
        session.set_query("salt r")
        session.run_now()
        QTest.qWait(150)
        self.runner.finish(1)  # (with the index kept: the catalogs were read once)
        QTest.qWait(100)
        self.assertIsNotNone(session._index)  # used 0.1 s ago
        self.assertTrue(wait_until(lambda: session._index is None))
        self.assertIsNone(session._caches)
        self.assertEqual(self.names_of(session), ["Salt Roads"])  # what was found stays shown
        session.set_query("sand")
        session.run_now()
        self.assertIsNone(self.runner.jobs[2][0]._index)  # made again
        self.runner.finish(2)
        self.assertEqual(self.providers.loads, 2)
        self.assertEqual(self.names_of(session), ["Sand Castle"])

    @staticmethod
    def names_of(session):
        return [item["name"] for _title, items in session.sections() for item in items]


if __name__ == "__main__":
    unittest.main()
