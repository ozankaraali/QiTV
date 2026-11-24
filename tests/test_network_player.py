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
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QWheelEvent
from PySide6.QtWidgets import QWidget
from timer_fixtures import elapse, pause, wait_until

from services import live_sites, network_streams as ns, parental
from services.jobs import JobRunner
from services.link_kind import FILM, LIVE, host_of, owner_of
from services.live.refusals import STREAM_REFUSED
from services.live_sites import KICK, Resolved, SiteError, SitePage
from services.navigation import LINKS_LAYER, RECENT_GROUP, NavNode
from services.network_streams import NetworkStreams, stream_id
from services.parental import Parental
from services.provider_ids import NETWORK_PROVIDER
from tests import test_tv_controller as tvc
from tests.test_tv_window import WindowCase
from ui import live_controls
from ui.keymap import Action
from ui.network_player import NetworkPlayer
from ui.open_link import LinkCard, OpenLink

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


class OwnerAfreshTests(PlayerCase):
    """Final review C1: an account's link played before that account was configured is kept
    as Network streams' (no limit). Once the account is added it books under the account on
    every path, with the account's patience: whose stream a link is is asked each time it
    plays, never read from the stamp it was kept with. ⟲ Recent lists it once, under whose
    stream it is now."""

    ACCOUNT = "http://harbor.example:8080/live/demo/secret/7.ts"
    ACCOUNT_FILM = "http://harbor.example:8080/movie/demo/secret/55.mkv"

    def setUp(self):
        super().setUp()
        self.harbor = self.catalog.specs.pop("Harbor IPTV")  # not configured yet
        self.loaded()

    def kept_before(self, url, kind=LIVE):
        """`url` played before its account was added (its item as it was kept), then the
        account added, and a channel of the browsed provider played meanwhile."""
        self.link(url, kind)
        self.plays_now()
        provider, item, *_ = self.watch.plays[-1]
        self.assertEqual((provider, item["_provider"]), (NETWORK_PROVIDER, NETWORK_PROVIDER))
        self.catalog.specs["Harbor IPTV"] = self.harbor
        self.catalog.names.append("Harbor IPTV")
        self.opens(lambda: self.play(1))
        self.assertEqual(self.budget.holders(NETWORK_PROVIDER), [])
        return item

    def recent(self):
        return self.nav.children(NavNode("group", RECENT_GROUP, "Recent"))

    def test_a_link_kept_as_no_providers_plays_as_its_accounts(self):
        item = self.kept_before(self.ACCOUNT)
        self.config.watch_history = [tvc.watched(NETWORK_PROVIDER, item)]
        self.assertEqual([r.payload["_provider"] for r in self.recent()], ["Harbor IPTV"])
        # Played as it was kept (a zap context, Play again, a drag made before): the same.
        self.opens(lambda: self.controller.tune(item, group_key=RECENT_GROUP, channels=[item]))
        self.assertEqual(self.controller.now_playing().item["_provider"], "Harbor IPTV")
        self.assertTrue(wait_until(lambda: self.budget.holders("Harbor IPTV") == ["main"]))
        self.assertEqual(self.budget.holders(NETWORK_PROVIDER), [])
        self.assertFalse(self.starts()[-1][1].get("once", False))  # the account's patience
        self.assertEqual(self.watch.plays[-1][0], "Harbor IPTV")

    def test_a_film_kept_as_no_providers_plays_as_its_accounts(self):
        item = self.kept_before(self.ACCOUNT_FILM, FILM)
        self.opens(lambda: self.controller.play_vod(item, "movie", tvc.play_context()))
        self.assertEqual(self.session.plays[-1][0], self.ACCOUNT_FILM)
        self.assertEqual(self.budget.holders("Harbor IPTV"), ["main"])
        self.assertEqual(self.budget.holders(NETWORK_PROVIDER), [])

    def test_the_phones_watch_counts_it_as_its_accounts(self):
        item = self.kept_before(self.ACCOUNT)
        launched = []
        target = tvc.Target(item, "live", "Seven", group_key=RECENT_GROUP)
        stops = len(self.live.named("stop"))
        self.assertTrue(
            self.controller.play_elsewhere(
                lambda t, url: launched.append(url) or (True, ""), target
            )
        )
        self.assertTrue(wait_until(lambda: len(self.live.named("stop")) > stops))
        self.live.released.emit()  # QiTV's own stream let go first
        self.assertTrue(wait_until(lambda: launched == [self.ACCOUNT]))
        self.assertEqual(self.budget.holders("Harbor IPTV"), ["main"])  # the phone's
        self.assertEqual(self.budget.holders(NETWORK_PROVIDER), [])

    def test_recent_lists_a_link_once_and_takes_it_out_whole(self):
        item = self.kept_before(self.ACCOUNT)
        owned = {**item, "_provider": "Harbor IPTV"}
        self.config.watch_history = [
            tvc.watched("Harbor IPTV", owned),
            tvc.watched(NETWORK_PROVIDER, item),
            tvc.watched("Home", tvc.CHANNELS[1]),
        ]
        rows = self.recent()
        self.assertEqual(
            [(r.payload["_provider"], r.payload["id"]) for r in rows],
            [("Harbor IPTV", item["id"]), ("Home", tvc.CHANNELS[1]["id"])],
        )
        # Its account removed, it is still listed: a link plays without it, as no provider's.
        self.catalog.names.remove("Harbor IPTV")
        del self.catalog.specs["Harbor IPTV"]
        rows = self.recent()
        self.assertEqual(rows[0].payload["_provider"], NETWORK_PROVIDER)
        self.assertEqual(len(rows), 2)
        # Remove from Recents takes it out whatever it was kept under.
        self.controller.watch = tvc.WatchState(self.config)
        self.controller._forget_recent(rows[0].payload)
        self.assertEqual([e["provider_name"] for e in self.config.watch_history], ["Home"])


class LastChannelTests(PlayerCase):
    def test_a_link_never_becomes_the_last_channel(self):
        # Final review M2: Start with the last channel would play nothing at the next start
        # (and a link's token may have run out by then): the provider's channel stays.
        self.loaded()
        self.opens(lambda: self.play(1))
        last = self.watch.last_channel
        self.assertEqual((last[0], last[1]["id"]), ("Home", 2))
        for url in (URL, "http://harbor.example:8080/live/demo/secret/7.ts"):  # an account's
            with self.subTest(url):
                self.link(url)
                self.plays_now()
                self.assertEqual(self.watch.plays[-1][1]["cmd"], url)  # in the history
                self.assertEqual(self.watch.last_channel, last)


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

    def test_another_film_in_a_links_place_starts_from_its_beginning(self):
        # Settings' Replace: the link keeps its id (its name, ⟲ Recent's row), but where the
        # old film stopped is forgotten; its own film with a fresh token keeps it.
        self.loaded()
        self.link(FILM_URL, FILM)
        self.plays_now()
        key = stream_id(FILM_URL)
        self.watch.resume[f"{NETWORK_PROVIDER}:movie:{key}"] = 125_000
        item = {"id": key, "cmd": FILM_URL, "_provider": NETWORK_PROVIDER}
        self.assertEqual(self.controller.stopped_at(item), 125_000)
        self.player.set_url(key, FILM_FRESH)
        self.assertEqual(self.controller.stopped_at(item), 125_000)
        cid = f"{NETWORK_PROVIDER}:movie:{key}"
        self.session.positionChanged.emit(130_000, 6_000_000)  # its own film, playing on
        self.assertEqual(self.watch.positions[-1], (cid, 130_000, 6_000_000))
        other = "https://vod.example/films/concert.mp4"
        self.player.set_url(key, other, FILM)
        self.assertIsNone(self.controller.stopped_at(item))
        recorded = len(self.watch.positions)
        self.session.positionChanged.emit(140_000, 6_000_000)  # the old film, still playing
        self.assertEqual(len(self.watch.positions), recorded)  # keeps no point under the link
        self.assertEqual((self.store.get(key).id, self.store.get(key).url), (key, other))
        self.opens(lambda: self.player.play_kept(key))
        url, options = self.session.plays[-1]
        self.assertEqual((url, options["start_ms"]), (other, None))  # from its beginning

    def test_going_back_to_a_links_first_film_forgets_the_film_between(self):
        # Review M1: "the same stream" is the one the link plays now, never its id's first.
        self.loaded()
        self.link(FILM_URL, FILM)
        self.plays_now()
        key = stream_id(FILM_URL)
        cid = f"{NETWORK_PROVIDER}:movie:{key}"
        other = "https://vod.example/films/concert.mp4"
        self.player.set_url(key, other, FILM)  # Replace: another film
        self.opens(lambda: self.player.play_kept(key))
        self.plays_now()
        self.watch.resume[cid] = 300_000  # where that film stopped, under the link's id
        self.player.set_url(key, FILM_FRESH, FILM)  # Settings: back to its first film
        item = {"id": key, "cmd": FILM_FRESH, "_provider": NETWORK_PROVIDER}
        self.assertIsNone(self.controller.stopped_at(item))
        # Open Link of the first film's address, after another Replace: the same.
        self.player.set_url(key, other, FILM)
        self.watch.resume[cid] = 300_000
        self.opens(lambda: self.player.play(FILM_URL, FILM))
        url, options = self.session.plays[-1]
        self.assertEqual((url, options["start_ms"]), (FILM_URL, None))
        self.assertEqual(self.store.get(key).url, FILM_URL)


class KeptTests(PlayerCase):
    def test_a_kept_film_played_as_a_channel_elsewhere_stays_a_film(self):
        # Task 5's review: a link played another way than by this player (a tile, ⟲ Recent)
        # keeps the kind it was kept with; this player playing it so changes it.
        self.loaded()
        self.link(FILM_URL, FILM)
        self.plays_now()
        key = stream_id(FILM_URL)
        self.assertEqual(self.store.get(key).kind, FILM)
        item = {"id": key, "name": "Harbour", "cmd": FILM_URL, "_provider": NETWORK_PROVIDER}
        self.opens(lambda: self.controller.tune(item, group_key=RECENT_GROUP, channels=[item]))
        self.plays_now()  # its first picture, heard as a live link's
        self.assertEqual(self.store.get(key).kind, FILM)
        self.assertEqual(len(self.store.entries()), 1)
        self.opens(lambda: self.play(1))  # another channel (the same one again would stay)
        self.link(FILM_URL, LIVE)  # Open Link said live this time: this player's own choice
        self.plays_now()
        self.assertEqual(self.store.get(key).kind, LIVE)

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
        self.enterContext(self.slow_writes())  # the file is away a moment meanwhile
        self.path.write_text('{"v": 1, "entries": []}', encoding="utf-8")
        with mock.patch.object(self.store, "merge", side_effect=RuntimeError):
            self.loaded()  # read or not, the store is ready
        self.link(URL)
        self.plays_now()
        self.assertTrue(wait_until(lambda: len(self.rows()) == 1 and self.jobs.pending() == 0))
        self.assertEqual([p.read_text() for p in self.aside()], ['{"v": 1, "entries": []}'])

    def test_a_file_that_cannot_be_set_aside_is_never_written_over(self):
        self.enterContext(self.slow_writes())  # the file is away a moment meanwhile
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
        """The file's links as it is now ([] while it is away: set aside, the new one not
        written yet)."""
        try:
            return json.loads(self.text())["entries"]
        except ValueError, KeyError, TypeError:
            return []

    def text(self):
        """The file's text, None while it is away (moved aside, the new one not written)."""
        try:
            return self.path.read_text()
        except FileNotFoundError:
            return None

    def aside(self):
        return [p for p in self.path.parent.iterdir() if ".broken-" in p.name]

    def slow_writes(self):
        """Each write a moment late, as on a slow disk: a file set aside is away meanwhile (it
        is moved aside, then the new one written)."""
        real = ns.write

        def slow(path, data):
            time.sleep(0.15)
            real(path, data)

        return mock.patch.object(ns, "write", slow)

    def test_an_unreadable_file_is_set_aside_before_it_is_written(self):
        self.enterContext(self.slow_writes())  # the file is away a moment meanwhile
        self.path.write_text("{not json", encoding="utf-8")
        self.loaded()
        self.link(URL)
        self.plays_now()
        self.assertTrue(wait_until(lambda: self.text() not in (None, "{not json")))
        self.assertTrue(wait_until(lambda: self.jobs.pending() == 0))
        aside = [p for p in self.path.parent.iterdir() if ".broken-" in p.name]
        self.assertEqual([p.read_text() for p in aside], ["{not json"])
        self.assertEqual(len(json.loads(self.path.read_text())["entries"]), 1)


class LinkStepTests(PlayerCase):
    """Channel up/down (and the media keys' next and previous) while a kept link plays: the
    next or previous kept link of Network streams plays, through the network player, round
    from the last to the first as channels do; a provider's channel zaps as ever."""

    OTHER = "https://cdn.example/live/lighthouse/index.m3u8?token=aaa"
    THIRD = "https://cdn.example/live/pier/index.m3u8?token=aaa"
    ACCOUNT = "http://harbor.example:8080/live/demo/secret/7.ts"

    def setUp(self):
        super().setUp()
        ticks = iter(range(1, 10_000))
        self.player._now = lambda: NOW + next(ticks)  # each link opened after the one before
        self.loaded()

    def keep(self, *urls, kind=LIVE):
        """Play each of `urls` (kept once its first picture shows), in that order."""
        for url in urls:
            self.link(url, kind)
            self.plays_now()

    def playing(self):
        return self.controller.now_playing().item["id"]

    def step(self, action):
        """`action` (a channel key), what it plays opened and showing its first picture."""
        self.opens(lambda: self.controller.handle(action))
        self.plays_now()

    def nothing_opens(self, action):
        opened = len(self.starts()), len(self.session.plays)
        self.controller.handle(action)
        pause(50)
        self.assertEqual((len(self.starts()), len(self.session.plays)), opened)

    def test_channel_up_and_down_step_through_the_kept_links_round(self):
        self.keep(self.OTHER, URL)  # URL opened last: first in Network streams
        order = [stream_id(URL), stream_id(self.OTHER)]
        self.assertEqual([e.id for e in self.store.entries()], order)
        self.assertEqual(self.playing(), stream_id(URL))
        with mock.patch.object(self.player, "play_kept", wraps=self.player.play_kept) as kept:
            self.step(Action.CH_NEXT)
            # The network player plays it, as a zap step (an outside player waits for the rest).
            kept.assert_called_once_with(stream_id(self.OTHER), zapped=True)
            self.assertEqual(self.playing(), stream_id(self.OTHER))
            self.assertEqual(self.starts()[-1][0], self.OTHER)
            self.step(Action.CH_NEXT)  # past the last: the first again
            self.assertEqual(self.playing(), stream_id(URL))
            self.step(Action.CH_PREV)  # before the first: the last
            self.assertEqual(self.playing(), stream_id(self.OTHER))
            self.step(Action.MEDIA_NEXT)  # the media keys' next too
            self.assertEqual(self.playing(), stream_id(URL))
            self.step(Action.MEDIA_PREV)
            self.assertEqual(self.playing(), stream_id(self.OTHER))
        self.assertEqual(self.catalog.links, [])  # no provider was asked for a link

    def test_the_steps_keep_the_order_they_began_in(self):
        # A link that plays moves to the top of Network streams: the steps go on in the order
        # they began with, never back and forth between the last two.
        self.keep(self.THIRD, self.OTHER, URL)
        self.assertEqual(
            [e.id for e in self.store.entries()],
            [stream_id(URL), stream_id(self.OTHER), stream_id(self.THIRD)],
        )
        played = []
        for _ in range(4):
            self.step(Action.CH_NEXT)
            played.append(self.playing())
        self.assertEqual(
            played,
            [stream_id(self.OTHER), stream_id(self.THIRD), stream_id(URL), stream_id(self.OTHER)],
        )

    def test_a_link_that_fails_is_stepped_past(self):
        # A kept link whose access ran out fails as it opens: the next step goes on to the link
        # after it, never back to it, nor to a provider's channel.
        self.keep(self.THIRD, self.OTHER, URL)  # Network streams: URL, OTHER, THIRD
        self.opens(lambda: self.controller.handle(Action.CH_NEXT))
        self.assertEqual(self.starts()[-1][0], self.OTHER)
        self.live.failure = lambda: ("http", 403)
        self.live.failed.emit(STREAM_REFUSED)
        self.assertEqual(self.store.get(stream_id(self.OTHER)).failed, "403: the link was refused")
        self.live.released.emit()
        self.opens(lambda: self.controller.handle(Action.CH_NEXT))
        self.assertEqual(self.starts()[-1][0], self.THIRD)
        self.plays_now()
        self.assertEqual(self.playing(), stream_id(self.THIRD))
        self.assertEqual(self.catalog.links, [])  # no provider's channel was asked for

    def test_one_link_alone_steps_nowhere(self):
        self.keep(URL)
        for action in (Action.CH_NEXT, Action.CH_PREV, Action.MEDIA_NEXT):
            with self.subTest(action=action):
                self.nothing_opens(action)
                self.assertEqual(self.playing(), stream_id(URL))
        self.assertEqual(self.catalog.links, [])  # nor a provider's channel asked for

    def test_a_link_plays_as_kept_an_account_s_and_a_film(self):
        self.keep(FILM_URL, kind=FILM)
        self.keep(self.ACCOUNT, URL)  # URL first, then the account's link, then the film
        self.step(Action.CH_NEXT)
        self.assertEqual(self.playing(), stream_id(self.ACCOUNT))
        self.assertEqual(self.controller.now_playing().item["_provider"], "Harbor IPTV")
        self.assertTrue(wait_until(lambda: self.budget.holders("Harbor IPTV") == ["main"]))
        self.step(Action.CH_NEXT)
        self.assertEqual(self.session.plays[-1][0], FILM_URL)  # as a film
        self.assertFalse(self.controller.now_playing().live)
        # A film plays: its channel keys are the volume's, as any film's.
        self.nothing_opens(Action.CH_NEXT)
        self.assertEqual(self.playing(), stream_id(FILM_URL))

    def test_the_wheel_over_the_channel_column_moves_in_the_column(self):
        # The fast-zap column lists the zap's channels: its wheel steps among them, never the
        # kept links, while a link plays.
        self.keep(self.OTHER, URL)
        with (
            mock.patch.object(self.player, "play_kept") as kept,
            mock.patch.object(self.controller.zap, "step") as zapped,
        ):
            self.controller._on_column_wheel(1, Qt.KeyboardModifier.NoModifier)
        zapped.assert_called_once_with(1)
        kept.assert_not_called()

    def test_a_providers_channel_zaps_as_ever(self):
        self.keep(self.OTHER, URL)
        with mock.patch.object(self.player, "play_kept", wraps=self.player.play_kept) as kept:
            self.opens(lambda: self.play(0))
            self.assertEqual(self.starts()[-1][0], "http://stream/1")
            self.opens(
                lambda: (self.controller.handle(Action.CH_NEXT), self.ready("http://stream/2"))
            )
            self.assertEqual(self.starts()[-1][0], "http://stream/2")  # the group's next
        kept.assert_not_called()


class _Kept:
    """Where parental controls keep their rules (the config's `parental`)."""

    parental = None

    def save_config(self):
        pass


class HiddenLinkTests(PlayerCase):
    """Final review (maintenance) I1: a link hidden with "Hide (parental controls)" is hidden
    as a hidden channel is while locked. Network streams doesn't list it, nothing plays it
    (its row, the find, Open Link of its address), channel up/down steps past it, and one
    playing at the lock stops. That holds whoever's stream it is: an account added after it
    was hidden too, and a kept film as well."""

    OTHER = LinkStepTests.OTHER
    ACCOUNT = "http://harbor.example:8080/live/demo/secret/7.ts"

    def setUp(self):
        super().setUp()
        ticks = iter(range(1, 10_000))
        self.player._now = lambda: NOW + next(ticks)  # each link opened after the one before
        self.harbor = self.catalog.specs.pop("Harbor IPTV")  # not configured yet
        self.nav.set_links(self.player.rows)  # as the window lists them
        self.loaded()
        self.guard = Parental(_Kept())
        self.guard.set_pin("1234")
        self.assertTrue(self.guard.unlock("1234"))
        self.controller.parental = self.guard
        parental.install(self.guard)  # as the window installs it, for the lists
        self.addCleanup(parental.install, None)

    def keep(self, *urls, kind=LIVE):
        for url in urls:
            self.link(url, kind)
            self.plays_now()

    def hide(self, url):
        """`url`'s ⟲ Recent row hidden with its menu's Hide (unlocked), as Network streams'
        link (its account not configured yet)."""
        item = {"id": stream_id(url), "name": "Hidden", "cmd": url, "_provider": NETWORK_PROVIDER}
        self.config.watch_history = [tvc.watched(NETWORK_PROVIDER, item)]
        rows = self.nav.children(NavNode("group", RECENT_GROUP, "Recent"))
        self.controller._hide_from_menu(next(n for n in rows if n.payload["id"] == item["id"]))
        self.assertEqual(len(self.guard.hidden()), 1)
        return item

    def add_account_and_lock(self):
        self.catalog.specs["Harbor IPTV"] = self.harbor
        self.catalog.names.append("Harbor IPTV")
        self.guard.lock()
        self.controller.parental_changed()  # as the window tells it

    def nothing_opens(self, action):
        opened = len(self.starts()), len(self.session.plays)
        action()
        pause(50)
        self.assertEqual((len(self.starts()), len(self.session.plays)), opened)

    def listed(self):
        layer = NavNode(LINKS_LAYER, NETWORK_PROVIDER, "Network streams", NETWORK_PROVIDER)
        return [node.key for node in self.nav.children(layer)]

    def test_a_hidden_link_is_not_listed_played_or_stepped_onto_while_locked(self):
        self.keep(self.ACCOUNT, self.OTHER, URL)  # Network streams: URL, OTHER, ACCOUNT
        hidden = self.hide(self.ACCOUNT)
        self.add_account_and_lock()  # it is the account's stream now
        self.assertEqual(self.listed(), [stream_id(URL), stream_id(self.OTHER)])
        self.assertEqual([row.id for row in self.player.rows()], self.listed())
        source = next(n for n in self.nav.children(None) if n.layer == LINKS_LAYER)
        self.assertEqual(source.count, 2)  # Network streams' count among the sources
        # Played again as the account's (⟲ Recent kept it so), it is still not listed there.
        owned = {**hidden, "_provider": "Harbor IPTV"}
        self.config.watch_history = [tvc.watched("Harbor IPTV", owned)]
        recent = self.nav.children(NavNode("group", RECENT_GROUP, "Recent"))
        self.assertEqual(recent, [])
        self.assertTrue(self.controller._hidden_now(owned))  # no tile plays it
        # Its row, the find and Open Link of its address play nothing.
        self.nothing_opens(lambda: self.player.play_kept(hidden["id"]))
        self.nothing_opens(lambda: self.player.play(self.ACCOUNT, LIVE))
        self.nothing_opens(
            lambda: self.controller.tune(owned, group_key=RECENT_GROUP, channels=[owned])
        )
        # Channel up/down steps past it, round.
        self.assertEqual(self.controller.now_playing().item["id"], stream_id(URL))
        played = []
        for action in (Action.CH_NEXT, Action.CH_NEXT, Action.CH_PREV):
            self.opens(lambda: self.controller.handle(action))
            self.plays_now()
            played.append(self.controller.now_playing().item["id"])
        self.assertEqual(played, [stream_id(self.OTHER), stream_id(URL), stream_id(self.OTHER)])
        self.assertEqual(self.budget.holders("Harbor IPTV"), [])

    def test_a_hidden_link_that_plays_stops_at_the_lock_and_never_enters_the_history(self):
        self.keep(self.OTHER, self.ACCOUNT)
        self.hide(self.ACCOUNT)
        self.catalog.specs["Harbor IPTV"] = self.harbor
        self.catalog.names.append("Harbor IPTV")
        plays = len(self.watch.plays)
        self.opens(lambda: self.player.play(self.ACCOUNT, LIVE))  # unlocked: it plays
        self.assertEqual(self.controller.now_playing().item["_provider"], "Harbor IPTV")
        self.assertEqual(len(self.watch.plays), plays)  # hidden: kept out of the history
        self.guard.lock()
        self.controller.parental_changed()
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.assertIsNone(self.controller.now_playing())

    def test_play_again_of_a_hidden_link_without_a_provider_holds_nothing(self):
        # Re-review R3: Play again held the TV for the link, then nothing played it: black,
        # and no welcome card.
        self.catalog.names = []
        self.controller.start()
        self.keep(URL)  # held: no provider
        self.hide(URL)
        self.controller.stop()
        self.assertTrue(self.welcome.visible)
        self.guard.lock()
        self.controller.parental_changed()
        self.nothing_opens(self.controller.play_again)
        self.assertTrue(self.welcome.visible)
        self.assertFalse(self.controller._link_hold)
        self.assertFalse(self.controller._flow.held())

    def test_a_hidden_film_link_is_not_listed_or_played_while_locked(self):
        self.keep(FILM_URL, kind=FILM)
        self.keep(URL)
        film = self.hide(FILM_URL)  # its row of ⟲ Recent (played there as a channel once)
        self.guard.lock()
        self.controller.parental_changed()
        self.assertEqual(self.listed(), [stream_id(URL)])
        self.nothing_opens(lambda: self.player.play_kept(film["id"]))
        self.nothing_opens(lambda: self.player.play(FILM_URL, FILM))


class ExternalLinkStepTests(PlayerCase):
    """In VLC or MPV: a pick of a kept link launches the player at once; channel up/down
    through the kept links launches only the link the steps settle on, as channels do."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("ui.tv_controller.EXTERNAL_SETTLE_MS", 60)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config.play_in_vlc = True
        self.loaded()

    def test_link_steps_launch_only_the_link_the_steps_settle_on(self):
        third = LinkStepTests.THIRD
        other = LinkStepTests.OTHER
        for url in (third, other, URL):
            self.player.keep_new(url, LIVE)  # Network streams: URL, other, third
        self.player.play_kept(stream_id(URL))  # a pick launches at once
        self.assertTrue(wait_until(lambda: self.external.calls == [("vlc", URL)]))
        self.controller.handle(Action.CH_NEXT)  # other
        self.controller.handle(Action.CH_NEXT)  # third, before other settled
        pause(20)
        self.assertEqual(self.external.calls, [("vlc", URL)])  # nothing yet
        elapse(120)
        self.assertEqual(self.external.calls, [("vlc", URL), ("vlc", third)])


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

    def test_a_direct_link_that_ends_for_good_keeps_why(self):
        # Final review M5: it only said "The stream ended", and Network streams kept no
        # reason for it (the pipeline's give-up keeps one).
        self.enterContext(mock.patch.object(live_controls, "RECONNECT_MS", (10, 20)))
        rtmp = "rtmp://media.example/live/harbour"
        self.link(rtmp)  # no pipeline: VLC plays it directly
        self.plays_now()
        self.session.stateChanged.emit("ended")
        self.session.ended.emit()
        self.assertTrue(wait_until(lambda: len(self.session.plays) == 2))  # its one try again
        self.plays_now()
        self.session.stateChanged.emit("ended")
        self.session.ended.emit()  # ended again: nothing more is tried
        self.assertEqual(self.entry(rtmp).failed, "The link stopped answering")
        self.assertIn("The link stopped answering", self.errors()[-1])
        self.assertNotIn(("The stream ended", "info"), self.toast.messages())
        pause(100)
        self.assertEqual(len(self.session.plays), 2)

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


KICK_PAGE = "https://kick.com/examplechannel"
TWITCH_PAGE = "https://www.twitch.tv/otherchannel"
# What the sites answer: a master playlist whose token is good for minutes (never kept).
MASTER = "https://edge.example/hls/examplechannel/master.m3u8?token=sentinel-one"
MASTER_FRESH = "https://edge.example/hls/examplechannel/master.m3u8?token=sentinel-two"
MASTER_OTHER = "https://edge.example/hls/otherchannel/master.m3u8?token=sentinel-three"


class SiteTests(PlayerCase):
    """A Kick or Twitch channel's page: kept by its page, named by its channel; each play
    asks the site afresh on the controller's runner (services.live_sites.resolve, faked here:
    nothing is fetched), and plays what it answered."""

    def setUp(self):
        super().setUp()
        self.loaded()
        self.asked = []  # (the page asked, whether on the GUI thread)
        self.answers = []  # each ask's answer in turn: an address, a SiteError, or a callable
        self.enterContext(mock.patch.object(live_sites, "resolve", self.resolve))
        self.addCleanup(lambda: wait_until(lambda: self.controller._jobs.pending() == 0))

    def resolve(self, page, stop=None, **_kwargs):
        self.asked.append((page, threading.current_thread() is threading.main_thread()))
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer(stop)
        if isinstance(answer, Exception):
            raise answer
        return Resolved(answer, f"{page.channel} · {live_sites.SITE_NAMES[page.site]}")

    def errors(self):
        return [text for text, kind in self.toast.messages() if kind == "error"]

    def test_each_play_of_a_site_page_asks_the_site_afresh(self):
        self.answers = [MASTER, MASTER_FRESH]
        self.link(KICK_PAGE)
        self.assertEqual(self.starts()[-1][0], MASTER)  # the site's answer plays
        self.assertEqual(self.asked, [(SitePage(KICK, "examplechannel"), False)])  # off the GUI
        self.assertEqual(self.catalog.links, [])  # no provider was asked
        self.plays_now()
        self.opens(lambda: self.play(1))  # another channel, then the page's row again
        self.opens(lambda: self.player.play_kept(stream_id(KICK_PAGE)))
        self.assertEqual(self.starts()[-1][0], MASTER_FRESH)
        self.assertEqual(len(self.asked), 2)

    def test_a_site_link_is_kept_by_its_page_and_named_by_its_channel(self):
        self.answers = [MASTER, MASTER_FRESH]
        self.link(KICK_PAGE)
        self.plays_now()
        entry = self.store.get(stream_id(KICK_PAGE))
        self.assertEqual(
            (entry.url, entry.name, entry.kind), (KICK_PAGE, "examplechannel · Kick", LIVE)
        )
        item = self.controller.now_playing().item
        self.assertEqual((item["cmd"], item["name"]), (KICK_PAGE, "examplechannel · Kick"))
        # Renamed, it keeps its name the next time it plays.
        self.player.rename(entry.id, "Evening show")
        self.opens(lambda: self.play(1))
        self.opens(lambda: self.player.play(KICK_PAGE, LIVE))  # Open Link of the page again
        self.plays_now()
        self.assertEqual(self.starts()[-1][0], MASTER_FRESH)
        kept = self.store.get(entry.id)
        self.assertEqual((kept.url, kept.name), (KICK_PAGE, "Evening show"))
        saved = self.store.save_bytes().decode()
        self.assertEqual(json.loads(saved)["v"], 1)  # older 2.x builds read it as ever
        self.assertNotIn("sentinel", saved)  # the token is never kept

    def test_a_stale_resolve_is_dropped(self):
        late = threading.Event()
        stopped = []

        def slow(stop):
            late.wait(5)
            stopped.append(stop.is_set())
            return MASTER  # it answers all the same, once the newer choice was made

        self.answers = [slow, MASTER_OTHER]
        with mock.patch.object(
            self.controller, "_link_dropped", wraps=self.controller._link_dropped
        ) as dropped:
            self.player.play(KICK_PAGE, LIVE)  # A: its site is slow to answer
            self.assertTrue(wait_until(lambda: len(self.asked) == 1))
            self.opens(lambda: (self.player.play(TWITCH_PAGE, LIVE), late.set()))  # then B
        self.assertEqual(stopped, [True])  # a newer tune stopped A's ask
        dropped.assert_called_once_with(MASTER)  # A's late answer goes nowhere
        self.assertEqual(self.starts()[-1][0], MASTER_OTHER)  # and B plays
        self.assertNotIn(MASTER, [url for url, _ in self.starts()])
        self.assertEqual(self.controller.now_playing().item["cmd"], TWITCH_PAGE)
        self.assertEqual(self.errors(), [])

    def test_a_newer_tune_ends_the_site_ask_on_its_way(self):
        def waits(stop):
            stop.wait(5)  # the real resolve stops between its requests, as "network"
            return SiteError("network")

        self.answers = [waits, MASTER_OTHER]
        began = time.monotonic()
        self.player.play(KICK_PAGE, LIVE)
        self.assertTrue(wait_until(lambda: len(self.asked) == 1))
        self.opens(lambda: self.player.play(TWITCH_PAGE, LIVE))
        self.assertLess(time.monotonic() - began, 4)  # not held for A's whole ask
        self.assertEqual(self.starts()[-1][0], MASTER_OTHER)
        self.assertEqual(self.errors(), [])  # A was given up for B: nothing to say
        a = self.store.get(stream_id(KICK_PAGE))
        self.assertTrue(a is None or a.failed == "")  # and it did not fail

    def test_a_failed_resolve_counts_and_says_why(self):
        refused = "Kick refused QiTV's request: try again later, or paste the stream link"
        timed_out = "The link didn't answer in time"
        cases = (  # (why, as Network streams keeps it; the toast)
            ("offline", "examplechannel isn't live now", "examplechannel isn't live now"),
            ("unknown", *["No channel named examplechannel on Kick"] * 2),
            ("refused", refused, refused),
            ("network", timed_out, f"examplechannel · Kick: {timed_out}"),  # after its name
        )
        for count, (reason, why, toast) in enumerate(cases, 1):
            with self.subTest(reason):
                before = self.store.get(stream_id(KICK_PAGE))
                self.assertEqual(before.failed if before is not None else "", "")
                self.answers.append(SiteError(reason))
                self.player.play(KICK_PAGE, LIVE)
                self.assertTrue(wait_until(lambda: len(self.errors()) == count))
                self.assertEqual(self.errors()[-1], toast)  # said once, plainly
                entry = self.store.get(stream_id(KICK_PAGE))
                self.assertEqual((entry.url, entry.failed), (KICK_PAGE, why))  # counted in its row
                self.assertEqual(len(self.asked), count)  # asked once: no retry
                self.assertEqual(self.starts(), [])
                self.store.failed(entry.id, "")  # (the next case starts afresh)
        pause(100)
        self.assertEqual(len(self.asked), len(cases))

    def test_channel_up_and_down_step_past_a_site_page_that_is_not_live(self):
        # Its site gave no stream: the link before it plays on, and the next step goes on
        # past it (never back onto it, asking the site again).
        ticks = iter(range(1, 10_000))
        self.player._now = lambda: NOW + next(ticks)
        third = LinkStepTests.THIRD
        for url in (third, URL):
            self.link(url)
            self.plays_now()
        self.player.keep_new(KICK_PAGE, LIVE)  # never opened: listed last
        self.assertEqual(
            [e.id for e in self.store.entries()],
            [stream_id(URL), stream_id(third), stream_id(KICK_PAGE)],
        )
        self.answers.append(SiteError("offline"))
        self.controller.handle(Action.CH_PREV)  # before the first: the last, the page
        self.assertTrue(wait_until(lambda: self.errors()))
        self.assertEqual(self.errors(), ["examplechannel isn't live now"])
        self.assertEqual(self.controller.now_playing().item["id"], stream_id(URL))  # plays on
        self.opens(lambda: self.controller.handle(Action.CH_PREV))
        self.assertEqual(self.starts()[-1][0], third)
        self.assertEqual(len(self.asked), 1)  # the page was not asked again

    def test_history_keeps_the_page_not_the_token(self):
        self.answers = [MASTER]
        with self.assertLogs(level="DEBUG") as logs:
            self.link(KICK_PAGE)
        self.assertNotIn("sentinel", "\n".join(logs.output))  # never logged either
        self.assertEqual(self.starts()[-1][0], MASTER)
        provider, item, kind, link, group = self.watch.plays[-1]
        self.assertEqual(
            (provider, kind, link, group), (NETWORK_PROVIDER, "channel", KICK_PAGE, RECENT_GROUP)
        )
        self.assertEqual(item["cmd"], KICK_PAGE)
        self.plays_now()
        self.assertNotIn("sentinel", json.dumps([list(p[:4]) for p in self.watch.plays]))

    def test_copy_link_copies_the_page(self):
        QGuiApplication.clipboard().setText("")
        self.answers = [MASTER, MASTER_FRESH]
        self.link(KICK_PAGE)
        self.plays_now()

        def menu():
            with tvc.shown_menus() as popup:
                self.controller.video_context(QPoint(10, 10))  # a right-click on the picture
            return tvc.menus_of(popup)[0][0]

        menu().entry("Copy stream link").trigger()
        self.assertEqual(QGuiApplication.clipboard().text(), KICK_PAGE)  # at once: the page
        self.assertEqual(len(self.asked), 1)  # the site was not asked for it
        # An outside player is handed what the site answers now (it plays it there).
        stops = len(self.live.named("stop"))
        menu().entry("Play in VLC").trigger()
        self.assertTrue(wait_until(lambda: len(self.live.named("stop")) > stops))
        self.live.released.emit()  # (the stream in QiTV gives way first)
        self.assertTrue(wait_until(lambda: self.external.calls))
        self.assertEqual(self.external.calls, [("vlc", MASTER_FRESH)])
        self.assertEqual(len(self.asked), 2)


class NoProviderHoldTests(PlayerCase):
    """With no provider configured a link holds the TV as a file opened in QiTV does: the
    welcome card, and what waits for QiTV to be idle, wait until it stops, ends or fails.
    With a provider nothing is held for it."""

    def setUp(self):
        super().setUp()
        self.loaded()
        self.catalog.names = []
        self.controller.start()
        self.assertTrue(self.welcome.visible)
        self.runs = []

    def waits(self):
        self.controller.when_idle(lambda: self.runs.append(True))

    def test_a_live_link_holds_until_it_stops(self):
        self.link(URL)
        self.assertFalse(self.welcome.visible)
        self.waits()
        self.plays_now()
        self.assertEqual(self.runs, [])
        coast = "https://cdn.example/live/coast/index.m3u8"
        self.link(coast)  # a link after a link: still held
        self.assertFalse(self.welcome.visible)
        self.assertEqual(self.runs, [])
        self.controller.stop()
        self.assertTrue(self.welcome.visible)
        self.assertEqual(self.runs, [True])

    def test_a_link_that_fails_for_good_lets_go(self):
        self.link(URL)
        self.waits()
        self.live.failure = lambda: ("http", 403)
        self.live.failed.emit(STREAM_REFUSED)
        self.assertEqual(self.store.get(stream_id(URL)).failed, "403: the link was refused")
        self.assertEqual(self.runs, [True])
        self.assertTrue(self.welcome.visible)

    def test_a_film_link_holds_until_it_ends(self):
        self.link(FILM_URL, FILM)
        self.assertFalse(self.welcome.visible)
        self.waits()
        self.plays_now()
        self.assertEqual(self.runs, [])
        self.session.stateChanged.emit("ended")
        self.session.ended.emit()
        self.assertEqual(self.runs, [True])
        self.assertTrue(self.welcome.visible)

    def test_a_link_that_never_comes_lets_go(self):
        # Review M2: held at play_link, before anything is current; its delivery raised.
        failing = mock.patch.object(
            self.controller, "_deliver", side_effect=RuntimeError("no player")
        )
        with failing, self.assertLogs("ui.tv_controller", "WARNING"):
            self.player.play(URL, LIVE)
            self.waits()
            self.assertTrue(wait_until(lambda: self.runs == [True]))
        self.assertTrue(self.welcome.visible)
        self.assertFalse(self.controller._link_hold)

    def test_the_first_provider_added_after_a_link_still_opens_its_list(self):
        # Review M4: a link spends nothing of the start, as a file does.
        self.link(URL)
        self.controller.stop()
        self.catalog.names = ["Home"]
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.drawer.named("open_drawer"), [(None,)])

    def test_once_a_provider_exists_the_next_link_lets_the_hold_go(self):
        self.link(URL)
        self.waits()
        self.catalog.names = ["Home"]  # added while the link plays
        self.link("https://cdn.example/live/coast/index.m3u8")
        self.assertEqual(self.runs, [True])
        self.assertFalse(self.controller._link_hold)

    def film(self):
        """A file opened in QiTV plays (TVWindow.open_paths holds for it first)."""
        self.controller.hold_for_file(True)
        self.controller.play_file("/videos/Harbour Night.mkv", size=1234)
        self.session.stateChanged.emit("playing")
        self.assertFalse(self.welcome.visible)

    def assert_the_film_holds_on(self):
        """The film plays on with its own hold; stopped, the welcome card and the waits come."""
        self.assertFalse(self.welcome.visible)
        self.assertEqual(self.runs, [])
        self.assertTrue(self.controller.now_playing().local)
        self.assertTrue(self.controller._flow.held())
        self.assertFalse(self.controller._link_hold)
        self.controller.stop()
        self.assertTrue(self.welcome.visible)
        self.assertEqual(self.runs, [True])

    def test_a_link_that_never_comes_over_a_film_leaves_the_films_hold(self):
        # Review R1: its delivery raised while a file plays.
        self.film()
        self.waits()
        failing = mock.patch.object(
            self.controller, "_deliver", side_effect=RuntimeError("no player")
        )
        with failing, self.assertLogs("ui.tv_controller", "WARNING"):
            self.player.play(URL, LIVE)
            self.assertTrue(wait_until(lambda: not self.controller._link_hold))
        self.assert_the_film_holds_on()

    def test_an_outside_player_failing_a_link_over_a_film_leaves_the_films_hold(self):
        self.film()
        self.waits()
        self.config.play_in_mpv = True
        self.external.result = (False, "MPV is not installed")
        self.player.play(URL, LIVE)
        self.assertTrue(wait_until(lambda: self.external.calls == [("mpv", URL)]))
        self.assertTrue(wait_until(lambda: not self.controller._link_hold))
        self.assertIn(("MPV is not installed", "error"), self.toast.messages())
        self.assert_the_film_holds_on()

    def test_a_link_an_outside_player_took_lets_go(self):
        # Final review M4: Settings' Play in VLC took the link, and QiTV stayed black with no
        # welcome card (and the update question waiting) for good.
        self.config.play_in_vlc = True
        self.player.play(URL, LIVE)
        self.waits()
        self.assertTrue(wait_until(lambda: self.external.calls == [("vlc", URL)]))
        self.assertTrue(wait_until(lambda: self.runs == [True]))
        self.assertTrue(self.welcome.visible)
        self.assertFalse(self.controller._link_hold)

    def test_with_a_provider_a_link_holds_nothing(self):
        self.catalog.names = ["Home"]
        self.link(URL)
        self.waits()
        self.assertEqual(self.runs, [True])  # at once, as with a channel


class CardOverTheTvTests(PlayerCase):
    """Final review M1: while File → Open Link…'s card shows (it has the keys), neither the
    list nor the welcome card opens over it by itself: they wait until it goes, cancelled
    or after its Play (the link held first)."""

    def setUp(self):
        super().setUp()
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.card = LinkCard(self.host)
        self.opener = OpenLink(
            self.card,
            self.jobs,
            play=self.player.play,
            add_provider=lambda link: None,
            verify_ssl=lambda url: True,
            may_probe=lambda url: False,  # never read: no network in tests
            parent=self.host,
        )
        self.controller.add_card(self.card)
        self.loaded()

    def test_the_list_due_meanwhile_opens_once_the_card_is_cancelled(self):
        self.opener.open()
        self.catalog.hub_ready("Home")  # the provider still loading at start comes
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.drawer.named("open_drawer"), [])  # not over the card
        self.card.handle(Action.ESCAPE)
        self.assertTrue(self.card.isHidden())
        self.assertTrue(wait_until(lambda: self.drawer.named("open_drawer") == [(None,)]))

    def test_without_a_provider_the_welcome_card_waits_for_the_card(self):
        self.catalog.names = []
        self.controller.start()
        self.link(URL)  # held: the welcome card waits for it
        self.assertFalse(self.welcome.visible)
        self.opener.open()  # a fresh link is pasted as the one playing runs out
        self.live.failure = lambda: ("http", 401)
        self.live.failed.emit(STREAM_REFUSED)
        pause(20)
        self.assertFalse(self.welcome.visible)  # not over the card, which has the keys
        self.card.handle(Action.ESCAPE)
        self.assertTrue(wait_until(lambda: self.welcome.visible))

    def test_the_wheel_over_the_card_is_the_cards(self):
        # The controller watches the card for its hiding only: a wheel over it never zaps,
        # even with the channel column open under it.
        self.opener.open()
        self.column.visible = True
        wheel = QWheelEvent(
            QPointF(20, 20),
            QPointF(20, 20),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        with mock.patch.object(self.controller._column_wheel, "wheel_event") as zapped:
            self.assertFalse(self.controller.eventFilter(self.card, wheel))
        zapped.assert_not_called()

    def test_a_link_played_from_the_card_holds_before_the_welcome_card_could_come(self):
        self.catalog.names = []
        self.controller.start()
        self.link(URL)
        self.opener.open()
        self.live.failure = lambda: ("http", 401)
        self.live.failed.emit(STREAM_REFUSED)
        self.live.released.emit()
        fresh = "https://cdn.example/live/coast/index.m3u8"
        self.card.handle(Action.NAV_TEXT, fresh)
        opened = len(self.starts())
        self.card.handle(Action.NAV_ACTIVATE)  # Play: classified live by its address
        self.assertTrue(self.card.isHidden())
        self.assertTrue(wait_until(lambda: len(self.starts()) > opened))
        pause(20)
        self.assertFalse(self.welcome.visible)  # the fresh link holds it
        self.assertTrue(self.controller._link_hold)


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
        # Kept as no provider's (played before the account was added), ⟲ Recent lists it as
        # the account's: the window's own owner (final review C1).
        kept = {**item, "_provider": NETWORK_PROVIDER}
        window.config_manager.watch_history = [tvc.watched(NETWORK_PROVIDER, kept)]
        recent = window.nav.children(NavNode("group", RECENT_GROUP, "Recent"))
        self.assertEqual([node.payload["_provider"] for node in recent], ["Panel"])
        store_path = Path(self.config_dir) / ns.FILE_NAME
        self.assertEqual(window.network_streams.path, store_path)


if __name__ == "__main__":
    unittest.main()
