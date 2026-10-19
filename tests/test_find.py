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
    GENRE_LIMIT,
    FindWorker,
    FoundGenre,
    Preferences,
    build_find_index,
    find_channels,
    find_genres,
    live_genres,
    preferences_for,
    words,
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

    def test_genres_and_channels_whole_words_first_then_word_starts_then_the_rest(self):
        index = build_find_index(caches())
        found = find_genres(index, "spor")
        self.assertEqual(
            [(g.provider, g.title) for g in found],
            [
                ("Harbor IPTV", "TR: Spor"),  # the word itself, though its provider comes second
                ("Demo TV", "Sports"),
                ("Harbor IPTV", "UK: Sports | 4K"),
            ],
        )
        self.assertEqual([g.title for g in find_genres(index, "SPÖR 4k")], ["UK: Sports | 4K"])
        self.assertEqual(find_genres(index, "  "), [])
        hits = find_channels(index, "spor")
        self.assertEqual(
            [(h.provider, h.title) for h in hits],
            [
                ("Harbor IPTV", "Harbor Spor 2"),
                ("Demo TV", "Northwind Sport 1"),
                ("Demo TV", "Northwind Sport 2"),
            ],
        )
        self.assertEqual([h.title for h in find_channels(index, "harbor 4k")], ["Harbor Arena 4K"])

    def test_each_provider_keeps_its_best_channels_up_to_the_limit(self):
        index = build_find_index(caches())
        self.assertEqual(
            [h.title for h in find_channels(index, "spor", limit=1)],
            ["Harbor Spor 2", "Northwind Sport 1"],
        )

    def test_words_are_runs_of_letters_and_digits_folded(self):
        self.assertEqual(words("TR| SPOR-Kanalı (1440p)"), ["tr", "spor", "kanali", "1440p"])


def free_list() -> dict:
    """A free world list, listed first in Settings, as long lists often are."""
    names = [
        "Mossy Fernery (Bulgaria)",
        "Moss 2 (1440p)",
        "Sports Free 1",
        "Sports Free 2",
        "MossFern Wild Free",
    ]
    genres = {"Sports": [2, 3], "Documentary": [0, 4], "Turkey": [1]}
    genres.update({f"Sports {n}": [2] for n in range(1, 151)})  # many weaker genre matches
    return {"itv": live_content(names, genres)}


def home_tv() -> dict:
    """The user's own provider, listed after it."""
    names = [
        "TR: MOSS FERN 4K",
        "TR: MOSS 1 4K",
        "Moss 1 (1440p)",
        "TR| SPOR DEMO",
        "UK Sports A",
        "UK Sports B",
    ]
    genres = {"TR| SPOR": [3], "UK | Sports": [4, 5], "TR | Belgesel": [0, 1, 2]}
    return {"itv": live_content(names, genres)}


class RankingTests(unittest.TestCase):
    """The owner's real lists buried their own provider's channels under a free list's: what
    matches better comes first, then the user's provider, then what they starred or watched."""

    def setUp(self):
        self.index = build_find_index([("World Free", free_list()), ("Home TV", home_tv())])

    def channels(self, text, preferences=Preferences()):
        return [h.title for h in find_channels(self.index, text, preferences)]

    def test_whole_words_rank_before_word_starts_whatever_the_provider(self):
        self.assertEqual(self.channels("spor")[0], "TR| SPOR DEMO")
        self.assertEqual(
            self.channels("moss fern"),
            ["TR: MOSS FERN 4K", "Mossy Fernery (Bulgaria)", "MossFern Wild Free"],
        )  # whole words, word starts, then only somewhere in a word
        # "1" only starts "1440p": a weaker match than the word "1" itself
        self.assertEqual(self.channels("moss 1"), ["TR: MOSS 1 4K", "Moss 1 (1440p)", "Moss 2 (1440p)"])

    def test_the_genres_are_ranked_before_their_limit_is_applied(self):
        found = find_genres(self.index, "spor")
        self.assertEqual(len(found), GENRE_LIMIT)  # 152 genres have it: the weakest go
        self.assertEqual((found[0].provider, found[0].title), ("Home TV", "TR| SPOR"))

    def test_at_one_quality_the_users_provider_comes_first(self):
        listed = ["Sports Free 1", "Sports Free 2", "UK Sports A", "UK Sports B"]
        self.assertEqual(self.channels("sports")[:4], listed)  # as listed in Settings
        browsing = Preferences(providers=("Home TV",))
        self.assertEqual(
            self.channels("sports", browsing)[:4],
            ["UK Sports A", "UK Sports B", "Sports Free 1", "Sports Free 2"],
        )
        genres = find_genres(self.index, "sports", browsing)
        self.assertEqual(genres[0].title, "UK | Sports")

    def test_at_one_quality_and_provider_starred_and_watched_channels_come_first(self):
        starred = Preferences(favorite=lambda item: item["name"] == "UK Sports B")
        self.assertEqual(self.channels("uk sports", starred), ["UK Sports B", "UK Sports A"])
        watched_b = home_tv()["itv"]["contents"][5]
        self.assertEqual(watched_b["name"], "UK Sports B")
        watched = Preferences(watched=frozenset({("Home TV", str(watched_b["id"]))}))
        self.assertEqual(self.channels("uk sports", watched), ["UK Sports B", "UK Sports A"])
        self.assertEqual(self.channels("uk sports"), ["UK Sports A", "UK Sports B"])  # as listed

    def test_the_preferences_put_the_browsed_provider_then_the_recently_watched_ones(self):
        names = ["World Free", "Other", "Home TV"]
        history = [
            {"provider_name": "Home TV", "item_type": "channel", "item_data": {"id": 7}},
            {"provider_name": "Gone", "item_type": "channel", "item_data": {"id": 8}},
            {"provider_name": "Other", "item_type": "movie", "item_data": {"id": 9}},
            {"provider_name": "Other", "item_type": "channel", "item_data": {"id": 3}},
        ]
        mine = preferences_for(names, "World Free", history)
        self.assertEqual(mine.providers, ("World Free", "Home TV", "Other"))
        self.assertEqual(mine.watched, frozenset({("Home TV", "7"), ("Other", "3")}))
        every = preferences_for(names, "@all", history)  # "All providers": none is browsed
        self.assertEqual(every.providers, ("Home TV", "Other"))
        self.assertEqual(preferences_for(names, "@all", []).providers, ())


class WorkerTests(unittest.TestCase):
    def test_the_worker_makes_the_index_once_and_finds(self):
        providers = FakeProviders(caches())
        worker = FindWorker(providers, "news", loaded={"Demo TV": demo()})
        got = []
        worker.finished.connect(got.append)
        worker.run()
        index, genres, hits = got[0]
        self.assertEqual(providers.reads, [{"Demo TV": demo()}])
        self.assertEqual([g.title for g in genres], ["News"])
        self.assertEqual([h.title for h in hits], ["Evening News", "Morning News"])
        again = FindWorker(providers, "kids", index=index)
        again.finished.connect(got.append)
        again.run()
        self.assertEqual(len(providers.reads), 1)  # the index was given: nothing read again
        self.assertEqual([h.title for h in got[1][2]], ["Harbor Kids"])


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
            [g.title for g in self.session.genres], ["TR: Spor", "Sports", "UK: Sports | 4K"]
        )
        hits = self.session.channels
        self.assertEqual([provider_of(h.item) for h in hits], ["Harbor IPTV", "Demo TV", "Demo TV"])

    def test_typing_on_narrows_what_was_found_at_once_and_the_index_is_kept(self):
        self.session.set_query("n")
        elapse(WAIT)
        self.session.set_query("ne")  # narrowed here, before the pause
        self.assertEqual([g.title for g in self.session.genres], ["News"])
        self.assertEqual([h.title for h in self.session.channels], ["Evening News", "Morning News"])
        self.assertEqual(len(self.providers.reads), 1)  # the index is kept

    def test_the_preferences_are_read_for_each_find(self):
        browsed = ["Demo TV"]
        session = FindSession(
            self.providers,
            ImmediateRunner(),
            preferences=lambda: Preferences(providers=tuple(browsed)),
        )
        session.set_query("sports")
        elapse(WAIT)
        self.assertEqual([g.provider for g in session.genres], ["Demo TV", "Harbor IPTV"])
        browsed[0] = "Harbor IPTV"
        session.set_query("sport")
        elapse(WAIT)
        self.assertEqual([g.provider for g in session.genres], ["Harbor IPTV", "Demo TV"])

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
