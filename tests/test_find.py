"""The channel list's find: genres and channels at every provider (services.find, FindSession).

Fictional providers and channels only.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from mixed_fixtures import live_content
from timer_fixtures import elapse

from services.find import (
    FindWorker,
    FoundGenre,
    build_find_index,
    find_channels,
    find_genres,
    live_genres,
)
from services.jobs import ImmediateRunner
from services.provider_ids import provider_of
from ui.find_session import DEBOUNCE_MS, FindSession

APP = QApplication.instance() or QApplication([])
WAIT = DEBOUNCE_MS + 100


def demo() -> dict:
    """Demo TV: "Sports" (2 channels) and "News" (2)."""
    names = ["Northwind Sport 1", "Northwind Sport 2", "Evening News", "Morning News"]
    return {"itv": live_content(names, {"Sports": [0, 1], "News": [2, 3]})}


def harbor() -> dict:
    """Harbor IPTV: "TR: Spor" (1 channel), "UK: Sports | 4K" (1), "Kids" (1)."""
    names = ["Harbor Spor 2", "Harbor Arena 4K", "Harbor Kids"]
    content = live_content(names, {"TR: Spor": [0], "UK: Sports | 4K": [1], "Kids": [2]})
    return {"itv": content}


def caches():
    return [("Demo TV", demo()), ("Harbor IPTV", harbor())]


class FakeProviders:
    """What FindWorker reads: every provider's cached content."""

    def __init__(self, contents):
        self.contents = contents
        self.reads = []

    def get_all_providers_cached_content(self, loaded=None):
        self.reads.append(loaded)
        return list(self.contents)


class IndexTests(unittest.TestCase):
    def test_a_providers_genres_and_their_counts_as_its_list_has_them(self):
        genres = live_genres("Demo TV", demo()["itv"])
        self.assertEqual(
            genres,
            [
                FoundGenre("Demo TV", "Sports", "Sports", 2),
                FoundGenre("Demo TV", "News", "News", 2),
            ],
        )  # not the "All" group
        stb = {
            "categories": [{"id": "7", "title": "Films"}, {"id": "8", "title": "Kids"}],
            "contents": {"7": [{"id": 1, "name": "A"}], "8": []},
        }
        self.assertEqual(live_genres("Box", stb), [FoundGenre("Box", "7", "Films", 1)])
        unfetched = {"categories": [{"id": "9", "title": "Docs"}], "contents": {}}
        self.assertEqual(live_genres("Box", unfetched), [FoundGenre("Box", "9", "Docs", None)])
        self.assertEqual(live_genres("List", [{"id": 1, "name": "A"}]), [])  # a bare playlist

    def test_genres_start_with_the_text_first_then_contain_it(self):
        index = build_find_index(caches())
        found = find_genres(index, "spor")
        self.assertEqual(
            [(g.provider, g.title) for g in found],
            [
                ("Demo TV", "Sports"),
                ("Harbor IPTV", "TR: Spor"),
                ("Harbor IPTV", "UK: Sports | 4K"),
            ],
        )
        self.assertEqual([g.title for g in find_genres(index, "SPÖR 4k")], ["UK: Sports | 4K"])
        self.assertEqual(find_genres(index, "  "), [])

    def test_channels_of_every_provider_each_up_to_its_limit(self):
        index = build_find_index(caches())
        hits = find_channels(index, "spor")
        self.assertEqual(
            [(h.provider, h.title) for h in hits],
            [
                ("Demo TV", "Northwind Sport 1"),
                ("Demo TV", "Northwind Sport 2"),
                ("Harbor IPTV", "Harbor Spor 2"),
            ],
        )
        self.assertEqual(
            [h.title for h in find_channels(index, "spor", limit=1)],
            ["Northwind Sport 1", "Harbor Spor 2"],
        )
        self.assertEqual([h.title for h in find_channels(index, "harbor 4k")], ["Harbor Arena 4K"])
        starts = find_channels(index, "harbor")  # the one starting with the word first
        self.assertEqual(starts[0].title, "Harbor Spor 2")

    def test_the_worker_makes_the_index_once_and_finds(self):
        providers = FakeProviders(caches())
        worker = FindWorker(providers, "news", loaded={"Demo TV": demo()})
        got = []
        worker.finished.connect(got.append)
        worker.run()
        index, hits = got[0]
        self.assertEqual(providers.reads, [{"Demo TV": demo()}])
        self.assertEqual([h.title for h in hits], ["Evening News", "Morning News"])
        again = FindWorker(providers, "kids", index=index)
        again.finished.connect(got.append)
        again.run()
        self.assertEqual(len(providers.reads), 1)  # the index was given: nothing read again
        self.assertEqual([h.title for h in got[1][1]], ["Harbor Kids"])


class HeldRunner(ImmediateRunner):
    """Runs a worker when released: a find still running while the user types on."""

    def __init__(self):
        self.held = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.held.append((worker, on_finished, on_error, context))
        return worker

    def release(self):
        worker, on_finished, on_error, context = self.held.pop(0)
        super().run_worker(worker, on_finished, on_error, context)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.providers = FakeProviders(caches())
        self.version = [1]
        self.session = FindSession(
            self.providers, ImmediateRunner(), signature=lambda: tuple(self.version)
        )
        self.changes = []
        self.session.changed.connect(lambda: self.changes.append(True))

    def test_genres_and_channels_come_with_their_providers(self):
        self.session.set_query("spor")
        self.assertTrue(self.session.searching)  # the channels wait for a pause in the typing
        elapse(WAIT)
        self.assertFalse(self.session.searching)
        self.assertEqual(
            [g.title for g in self.session.genres], ["Sports", "TR: Spor", "UK: Sports | 4K"]
        )
        hits = self.session.channels
        self.assertEqual([provider_of(h.item) for h in hits], ["Demo TV", "Demo TV", "Harbor IPTV"])

    def test_with_an_index_the_genres_show_at_once_and_the_channels_narrow(self):
        self.session.set_query("n")
        elapse(WAIT)
        self.session.set_query("ne")  # matched here, before the pause
        self.assertEqual([g.title for g in self.session.genres], ["News"])
        self.assertEqual([h.title for h in self.session.channels], ["Evening News", "Morning News"])
        self.assertEqual(len(self.providers.reads), 1)  # the index is kept

    def test_the_index_is_made_again_when_the_catalogs_changed(self):
        self.session.set_query("news")
        elapse(WAIT)
        self.version[0] = 2
        self.session.set_query("kids")
        elapse(WAIT)
        self.assertEqual(len(self.providers.reads), 2)
        self.assertEqual([g.title for g in self.session.genres], ["Kids"])

    def test_clear_forgets_the_text_and_what_was_found(self):
        self.session.set_query("news")
        elapse(WAIT)
        self.session.clear()
        self.assertEqual(
            (self.session.query, self.session.genres, self.session.channels), ("", [], [])
        )
        self.session.set_query("   ")  # nothing to find
        self.assertEqual(self.session.genres, [])

    def test_a_text_typed_while_a_find_runs_waits_and_only_the_latest_runs(self):
        runner = HeldRunner()
        session = FindSession(self.providers, runner)
        session.set_query("news")
        elapse(WAIT)
        session.set_query("sport")
        elapse(WAIT)
        session.set_query("kids")
        elapse(WAIT)
        self.assertEqual(len(runner.held), 1)  # one at a time
        runner.release()  # "news" ends: its channels would only flash
        self.assertEqual(session.channels, [])
        self.assertEqual(len(runner.held), 1)
        runner.release()  # "kids", the latest
        self.assertEqual([h.title for h in session.channels], ["Harbor Kids"])
        self.assertFalse(session.searching)


if __name__ == "__main__":
    unittest.main()
