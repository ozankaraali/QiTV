"""A link opened with Open Link plays, is kept and resumes (plan 4, Task 4: ui/network_player.py).

A live link plays as a channel through the TV controller's own delivery (the time-shift
pipeline when it applies, a budget slot under its provider), and lands in ⟲ Recent; a film
link plays as a film and resumes by its stable id, whatever token its address carries. What
played is kept in Network streams (network_streams.json), read and written on a worker; a link
that fails is kept with a plain reason, tried again once at most. Fictional hosts; nothing is
fetched."""

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from timer_fixtures import pause, wait_until

from services import network_streams as ns
from services.jobs import JobRunner
from services.link_kind import FILM, LIVE, host_of, owner_of
from services.live.refusals import STREAM_REFUSED
from services.navigation import RECENT_GROUP, NavNode
from services.network_streams import NetworkStreams, stream_id
from services.provider_ids import NETWORK_PROVIDER
from tests import test_tv_controller as tvc
from tests.test_tv_window import WindowCase
from ui import live_controls
from ui.network_player import NetworkPlayer

URL = "https://cdn.example/live/harbour/index.m3u8?token=aaa"
FRESH = "https://cdn.example/live/harbour/index.m3u8?token=bbb"
FILM_URL = "https://vod.example/films/harbour.mkv?token=aaa"
FILM_FRESH = "https://vod.example/films/harbour.mkv?token=bbb"
HARBOR = {
    "name": "Harbor IPTV",
    "type": "XTREAM",
    "url": "http://harbor.example:8080",
    "username": "demo",
    "password": "secret",
}
NOW = 1_800_000_000.0


class PlayerCase(tvc.PipelineCase):
    """The TV controller with a fake time-shift session and a real budget, and the network
    player over a store in a temporary folder (its jobs run on real threads)."""

    def setUp(self):
        super().setUp()
        self.live.retry_now = lambda: False  # (LiveSession's Try now)
        self.catalog.specs["Harbor IPTV"] = dict(HARBOR)
        self.catalog.limits_of["Harbor IPTV"] = 1  # the panel reported one stream
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "network_streams.json"
        self.store = NetworkStreams(self.path)
        self.jobs = JobRunner()
        self.addCleanup(lambda: wait_until(lambda: self.jobs.pending() == 0))
        self.player = NetworkPlayer(
            self.store, self.jobs, self.controller, owner=self.owner, now=lambda: NOW
        )

    def owner(self, url):
        providers = [(spec, {host_of(spec.get("url"))}) for spec in self.catalog.specs.values()]
        return owner_of(url, providers)

    def opens(self, action):
        """Do `action`, which plays something new, and wait until it opens: a buffer playing
        is stopped first, and its upstream let go (as LiveSession says once it closed)."""
        stops = len(self.live.named("stop"))
        buffered = self.live.running and self.live.mode != "direct"
        opened = len(self.starts()), len(self.session.plays)
        action()
        if buffered:
            self.assertTrue(wait_until(lambda: len(self.live.named("stop")) > stops))
            self.live.released.emit()
        self.assertTrue(wait_until(lambda: (len(self.starts()), len(self.session.plays)) != opened))

    def link(self, url, kind=LIVE):
        """Play `url` and wait for what opens it (its link is answered after a turn)."""
        self.opens(lambda: self.player.play(url, kind))

    def plays_now(self):
        self.session.stateChanged.emit("playing")  # its first picture

    def loaded(self):
        self.player.start()
        self.assertTrue(wait_until(self.player.loaded))


class LivePlayTests(PlayerCase):
    def test_a_live_link_plays_as_a_channel_and_lands_in_recent(self):
        self.loaded()
        self.link(URL)
        self.assertEqual(self.catalog.links, [])  # no provider was asked for its link
        url, start = self.starts()[-1]
        self.assertEqual(url, URL)  # through the time-shift pipeline
        self.assertEqual((start["revive"], start["once"]), (False, True))  # tried again once
        self.assertEqual(self.budget.holders(NETWORK_PROVIDER), ["main"])
        provider, item, kind, link, group = self.watch.plays[-1]
        self.assertEqual(
            (provider, kind, link, group), (NETWORK_PROVIDER, "channel", URL, RECENT_GROUP)
        )
        self.assertEqual(
            item,
            {
                "id": stream_id(URL),
                "name": "cdn.example — harbour",
                "cmd": URL,
                "_provider": NETWORK_PROVIDER,
            },
        )
        self.plays_now()
        self.store.set_url(stream_id(URL), FRESH)  # its address was replaced since
        # ⟲ Recent lists it, as the watch history keeps it.
        self.config.watch_history = [
            {"item_type": "channel", "provider_name": provider, "item_data": dict(item)}
        ]
        recent = self.nav.children(NavNode("group", RECENT_GROUP, "Recent"))
        self.assertEqual([node.payload["cmd"] for node in recent], [URL])
        self.opens(lambda: self.play(1))  # another channel, then the link from ⟲ Recent
        self.assertEqual(self.starts()[-1][0], "http://stream/2")
        asked = len(self.catalog.links)
        channel = recent[0].payload
        self.opens(
            lambda: self.controller.tune(channel, group_key=RECENT_GROUP, channels=[channel])
        )
        self.assertEqual(self.starts()[-1][0], FRESH)  # its address now
        self.assertEqual(len(self.catalog.links), asked)  # the network player's, not a provider's

    def test_an_xtream_link_counts_against_its_provider(self):
        self.loaded()
        for url, owner in (
            ("http://harbor.example:8080/live/demo/secret/7.ts", "Harbor IPTV"),
            ("http://harbor.example:8080/demo/secret/8", "Harbor IPTV"),  # the short form
            ("http://harbor.example:8080/live/other/pw/9.ts", NETWORK_PROVIDER),  # another user
            ("http://elsewhere.example/live/demo/secret/7.ts", NETWORK_PROVIDER),
        ):
            with self.subTest(url):
                self.link(url)
                self.assertEqual(self.controller.now_playing().item["_provider"], owner)
                # Only a link no provider owns refuses patience (an account's keeps it).
                once = self.starts()[-1][1].get("once", False)
                self.assertEqual(once, owner == NETWORK_PROVIDER)
                self.assertTrue(wait_until(lambda: self.budget.holders(owner) == ["main"]))
                others = {"Harbor IPTV", NETWORK_PROVIDER} - {owner}
                self.assertEqual([self.budget.holders(o) for o in others], [[]])
        # A film of the account: its slot is the account's, its resume point Network streams'.
        film = "http://harbor.example:8080/movie/demo/secret/55.mkv"
        self.link(film, FILM)
        self.assertEqual(self.session.plays[-1][0], film)
        self.assertEqual(self.budget.holders("Harbor IPTV"), ["main"])
        self.session.positionChanged.emit(90_000, 6_000_000)
        cid = self.watch.positions[-1][0]
        self.assertEqual(cid, f"{NETWORK_PROVIDER}:movie:{stream_id(film)}")


class FilmTests(PlayerCase):
    def test_a_film_resumes_after_a_fresh_token(self):
        self.loaded()
        self.link(FILM_URL, FILM)
        url, options = self.session.plays[-1]
        self.assertEqual(url, FILM_URL)
        self.assertIsNone(options["start_ms"])  # from its start
        self.assertEqual(self.catalog.links, [])
        self.assertEqual(self.watch.plays[-1][2], "movie")
        self.plays_now()
        self.session.positionChanged.emit(125_000, 6_000_000)
        cid = f"{NETWORK_PROVIDER}:movie:{stream_id(FILM_URL)}"
        self.assertEqual(self.watch.positions[-1], (cid, 125_000, 6_000_000))
        self.watch.resume[cid] = 125_000  # as the watch state keeps it
        self.link(FILM_FRESH, FILM)  # the same film handed out with a fresh token
        url, options = self.session.plays[-1]
        self.assertEqual((url, options["start_ms"]), (FILM_FRESH, 125_000))
        self.assertEqual(self.controller.resume_key(self.controller.now_playing()), cid)
        self.assertEqual(len(self.store.entries()), 1)  # one entry, its address the newest
        self.plays_now()
        self.assertEqual(self.store.entries()[0].url, FILM_FRESH)
        self.assertEqual(self.store.entries()[0].kind, FILM)


class KeptTests(PlayerCase):
    def test_a_link_that_played_is_kept_on_a_worker(self):
        older = NetworkStreams(self.path)
        kept = older.keep(URL, "Harbour News", LIVE)  # renamed in an earlier run
        ns.write(self.path, older.save_bytes())
        threads = []

        def spy(real):
            def run(*args, **kwargs):
                threads.append((real.__name__, threading.current_thread()))
                return real(*args, **kwargs)

            return run

        with (
            mock.patch.object(ns, "read_bytes", spy(ns.read_bytes)),
            mock.patch.object(ns, "save", spy(ns.save)),
        ):
            self.link(FRESH)  # before the file is read: it plays as it is named here
            self.plays_now()
            pause(100)
            self.assertEqual(threads, [])  # nothing written before the file was read
            self.loaded()
            self.assertTrue(wait_until(lambda: [n for n, _ in threads] == ["read_bytes", "save"]))
            self.assertTrue(wait_until(lambda: self.jobs.pending() == 0))
        self.assertTrue(all(thread is not threading.main_thread() for _, thread in threads))
        rows = json.loads(self.path.read_text())["entries"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], kept.id)
        self.assertEqual(rows[0]["name"], "Harbour News")  # the name it was given stays
        self.assertEqual(rows[0]["url"], FRESH)  # the address that played
        self.assertEqual((rows[0]["opened"], rows[0]["failed"]), (NOW, ""))
        self.assertEqual(self.store.get(kept.id).opened, NOW)
        # Its name plays from now on (the item the controller plays carries it).
        self.opens(lambda: self.play(1))
        self.link(URL)
        self.assertEqual(self.controller.now_playing().item["name"], "Harbour News")

    def test_saves_follow_one_another_with_the_newest_state(self):
        self.loaded()
        real, lock, disk = ns.save, threading.Lock(), threading.Event()
        writes, running, most = [], [0], [0]

        def slow(*args, **kwargs):  # a slow disk: the changes meanwhile wait their turn
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            try:
                disk.wait(5.0)  # held until every link played
                writes.append(json.loads(args[1])["entries"])
                return real(*args, **kwargs)
            finally:
                with lock:
                    running[0] -= 1

        with mock.patch.object(ns, "save", slow):
            for url in (URL, "http://h.example/b.ts", "http://h.example/c.ts"):
                self.link(url)
                self.plays_now()
            disk.set()
            self.assertTrue(wait_until(lambda: len(writes) == 2 and self.jobs.pending() == 0))
        self.assertEqual(most[0], 1)  # never two writes at once
        self.assertEqual([len(rows) for rows in writes], [1, 3])  # the rest in one, the newest
        self.assertEqual(len(json.loads(self.path.read_text())["entries"]), 3)

    def test_a_file_with_a_time_too_large_is_still_read_and_saved(self):
        # Review: a huge number in `opened` raised out of the read: nothing was saved all run.
        row = '{"id": "x", "name": "X", "url": "http://h.example/x.ts", "opened": %s}' % ("9" * 400)
        self.path.write_text('{"v": 1, "entries": [%s]}' % row, encoding="utf-8")
        self.loaded()
        self.link(URL)
        self.plays_now()
        self.assertTrue(wait_until(lambda: len(self.rows()) == 2 and self.jobs.pending() == 0))
        self.assertEqual(self.store.get("x").opened, 0.0)

    def test_a_read_that_fails_keeps_what_plays_beside_the_file(self):
        self.path.write_text('{"v": 1, "entries": []}', encoding="utf-8")
        with mock.patch.object(self.store, "merge", side_effect=RuntimeError):
            self.loaded()  # read or not, the store is ready
        self.link(URL)
        self.plays_now()
        self.assertTrue(wait_until(lambda: len(self.rows()) == 1 and self.jobs.pending() == 0))
        self.assertEqual([p.read_text() for p in self.aside()], ['{"v": 1, "entries": []}'])

    def test_a_file_that_cannot_be_set_aside_is_never_written_over(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.loaded()
        with mock.patch.object(ns, "set_file_aside", return_value=None) as aside:
            self.link(URL)
            self.plays_now()
            self.assertTrue(wait_until(lambda: aside.called and self.jobs.pending() == 0))
        self.assertEqual(self.path.read_text(), "{not json")  # left as it was
        self.link("http://h.example/b.ts")  # the next change: set aside now, then written
        self.plays_now()
        self.assertTrue(wait_until(lambda: self.aside() and self.jobs.pending() == 0))
        self.assertEqual([p.read_text() for p in self.aside()], ["{not json"])
        self.assertEqual(len(self.rows()), 2)

    def rows(self):
        try:
            return json.loads(self.path.read_text())["entries"]
        except ValueError, KeyError:
            return []

    def aside(self):
        return [p for p in self.path.parent.iterdir() if ".broken-" in p.name]

    def test_an_unreadable_file_is_set_aside_before_it_is_written(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.loaded()
        self.link(URL)
        self.plays_now()
        self.assertTrue(wait_until(lambda: self.path.read_text() != "{not json"))
        self.assertTrue(wait_until(lambda: self.jobs.pending() == 0))
        aside = [p for p in self.path.parent.iterdir() if ".broken-" in p.name]
        self.assertEqual([p.read_text() for p in aside], ["{not json"])
        self.assertEqual(len(json.loads(self.path.read_text())["entries"]), 1)


class FailureTests(PlayerCase):
    def setUp(self):
        super().setUp()
        self.loaded()

    def entry(self, url):
        return self.store.get(stream_id(url))

    def errors(self):
        return [text for text, kind in self.toast.messages() if kind == "error"]

    def fails(self, message):
        """VLC's failure, as PlaybackSession reports it: its state, then why."""
        self.session.stateChanged.emit("error")
        self.session.errorOccurred.emit(message)

    def test_a_refused_link_is_marked_and_not_retried_forever(self):
        # Refused by its buffer (a plain 403): kept, marked, said plainly, not asked again.
        self.link(URL)
        self.live.failure = lambda: ("http", 403)
        self.live.failed.emit(STREAM_REFUSED)
        self.assertEqual(self.entry(URL).failed, "403: the link was refused")
        self.assertEqual(self.errors()[-1], "cdn.example — harbour: 403: the link was refused")
        self.assertEqual(len(self.starts()), 1)
        self.assertTrue(wait_until(lambda: self.jobs.pending() == 0))
        self.assertEqual(
            json.loads(self.path.read_text())["entries"][0]["failed"],
            "403: the link was refused",
        )
        # Its token ran out (401): its buffer's failure says so, after the direct try.
        self.live.released.emit()
        expired = "https://cdn.example/live/coast/index.m3u8?token=old"
        self.link(expired)
        self.live.failure = lambda: ("http", 401)
        self.live.failed.emit("The stream could not be opened")
        self.assertEqual(self.entry(expired).failed, "401: the link's access ran out")
        self.assertIn("401: the link's access ran out", self.errors()[-1])
        # One that played and keeps going down: tried again once, then it stops and says so.
        down = "https://cdn.example/live/bay/index.m3u8"
        self.link(down)
        self.live.failure = lambda: ("", None)
        self.live.play_pipeline()
        self.plays_now()
        self.assertEqual(self.entry(down).failed, "")
        self.live.retrying.emit(5.0)  # its stream went down: one try in 5 s
        self.assertNotIn(("stop",), self.live.calls[-1:])
        self.assertEqual(self.entry(down).failed, "")
        self.live.retrying.emit(30.0)  # that try failed too: no other
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.assertEqual(self.entry(down).failed, "The link stopped answering")
        self.assertIn("The link stopped answering", self.errors()[-1])

    def test_a_direct_link_is_opened_again_once(self):
        self.enterContext(mock.patch.object(live_controls, "RECONNECT_MS", (10, 20)))
        rtmp = "rtmp://media.example/live/harbour"
        self.link(rtmp)  # no pipeline: VLC plays it directly
        self.assertEqual(self.session.plays[-1][0], rtmp)
        self.plays_now()
        self.fails("The stream could not be played")
        self.assertTrue(wait_until(lambda: len(self.session.plays) == 2))  # its one try again
        self.fails("The stream could not be played")
        pause(200)
        self.assertEqual(len(self.session.plays), 2)
        self.assertEqual(self.entry(rtmp).failed, "The link could not be opened")
        self.link(rtmp)  # chosen again, it opens anew
        self.assertEqual(len(self.session.plays), 3)
        # A film that does not open is kept, marked; nothing tries it again.
        self.link(FILM_URL, FILM)
        self.fails("The stream could not be opened")
        pause(100)
        self.assertEqual(self.session.plays[-1][0], FILM_URL)
        self.assertEqual(self.entry(FILM_URL).failed, "The link could not be opened")
        self.assertEqual(self.entry(FILM_URL).kind, FILM)

    def test_the_same_link_opens_again_after_each_failure(self):
        # Chosen again (Open Link, ⟲ Recent), a link that failed opens anew, however it went.
        self.link(URL)  # refused: its stream stopped
        self.live.failure = lambda: ("http", 403)
        self.live.failed.emit(STREAM_REFUSED)
        self.live.released.emit()
        self.link(URL)
        self.assertEqual(len(self.starts()), 2)
        self.live.play_direct()  # its direct try failed: nothing stopped it
        self.live.failure = lambda: ("http", 401)
        self.session.stateChanged.emit("error")
        self.live.failed.emit("The stream could not be opened")
        self.link(URL)
        self.assertEqual(len(self.starts()), 3)
        self.live.failure = lambda: ("", None)  # it played, and went down after its try
        self.live.play_pipeline()
        self.plays_now()
        self.live.retrying.emit(5.0)
        self.live.retrying.emit(30.0)
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.live.released.emit()
        self.link(URL)
        self.assertEqual(len(self.starts()), 4)
        self.assertEqual(self.starts()[-1][0], URL)

    def test_a_link_that_plays_again_forgets_why_it_failed(self):
        self.link(URL)
        self.live.failure = lambda: ("http", 403)
        self.live.failed.emit(STREAM_REFUSED)
        self.live.released.emit()
        self.live.failure = lambda: ("", None)
        self.link(FRESH)
        self.plays_now()
        self.assertEqual(self.entry(URL).failed, "")
        self.assertEqual(self.entry(URL).opened, NOW)


class WindowTests(WindowCase):
    """The real window: its catalogs never see a link, and its store is the config folder's."""

    seed_fixture = True

    def provider_list(self):
        from tests.fixtures.local_provider import PROVIDER_NAME

        fixture = {
            "type": "M3UPLAYLIST",
            "name": PROVIDER_NAME,
            "url": str(Path(self.config_dir) / "fixture.m3u"),
        }
        return [
            fixture,
            {
                "type": "XTREAM",
                "name": "Panel",
                "url": "http://panel.example:8080",
                "username": "demo",
                "password": "secret",
                "max_streams": 1,
            },
        ]

    def test_a_link_plays_without_a_provider_and_is_kept_in_the_config_folder(self):
        window = self.window
        self.assertFalse(window.network_player.loaded())  # read once the window started
        window.network_player.start()  # (as its first show does)
        self.assertTrue(wait_until(window.network_player.loaded))
        asked = mock.patch.object(
            window.catalogs, "request_link", wraps=window.catalogs.request_link
        )
        with asked as requests:
            window.play_network_link(URL, LIVE)
            self.assertTrue(wait_until(lambda: window.controller.now_playing() is not None))
            self.assertTrue(wait_until(lambda: window.watch.config.watch_history))
            requests.assert_not_called()
        entry = window.config_manager.watch_history[0]
        self.assertEqual(
            (entry["provider_name"], entry["item_type"]), (NETWORK_PROVIDER, "channel")
        )
        self.assertTrue(window.nav.recent_count() >= 1)
        # Without libVLC nothing opens: that is QiTV's, not the link's, so it is not marked.
        self.assertTrue(wait_until(lambda: window.jobs.pending() == 0))
        self.assertIsNone(window.network_streams.get(stream_id(URL)))
        # The account's own short link is the account's stream (handed over, not played: it
        # would sign the account in for its guide).
        short = "http://panel.example:8080/demo/secret/12"
        with mock.patch.object(window.controller, "play_link") as play:
            window.play_network_link(short, LIVE)
        item, section = play.call_args.args
        self.assertEqual((item["cmd"], item["_provider"], section), (short, "Panel", "live"))
        store_path = Path(self.config_dir) / ns.FILE_NAME
        self.assertEqual(window.network_streams.path, store_path)


if __name__ == "__main__":
    unittest.main()
