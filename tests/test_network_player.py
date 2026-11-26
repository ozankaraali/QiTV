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
from services.navigation import LINK_LAYER, LINKS_LAYER, RECENT_GROUP, NavNode
from services.network_streams import NetworkStreams, stream_id
from services.parental import Parental
from services.provider_ids import NETWORK_PROVIDER
from tests import test_tv_controller as tvc
from tests.test_tv_window import WindowCase
from ui import live_controls
from ui.keymap import Action
from ui.link_lanes import Target
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


class ItemOfTests(PlayerCase):
    """A kept link as `play_kept` hands it over, without playing it (`item_of`: what its row's
    menu and a tile it is dragged onto play): its kept address, whose stream it is now (an
    account's link its account's), and its kind."""

    ACCOUNT = "http://harbor.example:8080/live/demo/secret/7.ts"

    def test_item_of_is_what_play_kept_hands_over(self):
        live = self.store.keep(URL, "Harbour News", LIVE)
        film = self.store.keep(FILM_URL, "Harbour film", FILM)
        account = self.store.keep(self.ACCOUNT, "Seven", LIVE)
        item = {"id": live.id, "name": "Harbour News", "cmd": URL, "_provider": NETWORK_PROVIDER}
        self.assertEqual(self.player.item_of(live.id), (item, LIVE))
        self.assertEqual(self.player.item_of(account.id)[0]["_provider"], "Harbor IPTV")
        self.assertEqual(self.player.item_of(film.id)[1], FILM)
        self.assertIsNone(self.player.item_of(stream_id("https://cdn.example/gone.m3u8")))
        self.assertEqual(self.player._handed, {})  # (only what plays changes a link's kind)
        with mock.patch.object(self.controller, "play_link") as play:
            for entry in (live, film, account):
                self.assertTrue(self.player.play_kept(entry.id))
                item, kind = self.player.item_of(entry.id)
                section = "movies" if kind == FILM else "live"
                self.assertEqual(play.call_args.args, (item, section))


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

    def test_a_network_streams_rows_hide_hides_the_link(self):
        # Its row's menu (the find's too) hides it as ⟲ Recent's does: by its id, gone from
        # the list once locked, and no menu for it then.
        self.keep(URL, self.OTHER)

        def menu_of(key):
            with tvc.shown_menus() as popup:
                self.drawer.contextRequested.emit(NavNode(LINK_LAYER, key, "Row"), QPoint(5, 5))
            shown = tvc.menus_of(popup)
            return shown[0][0] if shown else None

        menu = menu_of(stream_id(URL))
        hide = "Hide (parental controls)"
        self.assertEqual(menu.texts(), [hide, "Play in VLC", "Play in MPV", "Copy stream link"])
        menu.entry(hide).trigger()
        self.assertEqual(len(self.guard.hidden()), 1)
        self.guard.lock()
        self.controller.parental_changed()  # as the window tells it
        self.assertEqual(self.listed(), [stream_id(self.OTHER)])
        self.assertIsNone(menu_of(stream_id(URL)))

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
MASTER_NEWER = "https://edge.example/hls/examplechannel/master.m3u8?token=sentinel-four"
OFFLINE = "examplechannel isn't live now"


class SiteTests(tvc.RetryPillCase, PlayerCase):
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
        self.install_pill()

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

    def drops_direct(self, url):
        """`url` plays directly (the buffer fell back), shows its picture, and its stream ends."""
        self.link(url)
        self.live.play_direct()
        self.plays_now()
        plays = len(self.session.plays)
        self.session.ended.emit()
        return plays

    def test_a_dropped_site_link_reopens_with_a_fresh_address(self):
        self.answers = [MASTER, MASTER_FRESH]
        plays = self.drops_direct(KICK_PAGE)
        self.assertEqual(self.session.plays[-1][0], MASTER)
        self.assertTrue(wait_until(lambda: len(self.session.plays) == plays + 1))
        self.assertEqual(self.session.plays[-1][0], MASTER_FRESH)  # not the first address again
        self.assertEqual(len(self.asked), 2)  # the site was asked a second time, off the GUI
        self.assertEqual([on_gui for _page, on_gui in self.asked], [False, False])
        self.assertEqual(self.asked[1][0], SitePage(KICK, "examplechannel"))
        self.assertEqual(self.errors(), [])

    def test_the_reopen_keeps_the_buffer_when_the_run_can_be_joined(self):
        # The reopen of a direct stream has no `after_seq` (the helper's resume-join belongs to
        # its own restarts): a plain restart in v1, which the task ledger carries.
        self.answers = [MASTER, MASTER_FRESH]
        plays = self.drops_direct(KICK_PAGE)
        starts = len(self.starts())
        self.assertTrue(wait_until(lambda: len(self.session.plays) == plays + 1))
        kwargs = self.session.plays[-1][1]
        self.assertNotIn("after_seq", kwargs)
        self.assertEqual(len(self.starts()), starts)  # the same player, no new helper run

    def test_a_direct_play_whose_session_says_no_break_reopens(self):
        self.enterContext(mock.patch("ui.live_controls.RECONNECT_MS", (50, 50)))
        self.answers = [MASTER, MASTER_FRESH]
        self.link(KICK_PAGE)
        self.live.play_direct()
        self.plays_now()
        self.live.in_ad_break = lambda: False  # (LiveSession's: a pipeline's break only)
        plays = len(self.session.plays)
        self.session.ended.emit()
        self.assertTrue(wait_until(lambda: len(self.session.plays) == plays + 1))
        self.assertEqual(self.session.plays[-1][0], MASTER_FRESH)

    def test_a_reopen_is_skipped_while_the_session_skips_an_ad_break(self):
        self.enterContext(mock.patch("ui.live_controls.RECONNECT_MS", (50, 50)))
        self.answers = [MASTER, MASTER_FRESH]
        self.link(KICK_PAGE)
        self.live.play_direct()
        self.plays_now()
        self.live.in_ad_break = lambda: True
        plays = len(self.session.plays)
        self.session.ended.emit()
        pause(400)
        self.assertEqual(len(self.session.plays), plays)
        self.assertEqual(len(self.asked), 1)  # the site was not asked again

    def pipeline_fails(self, message=STREAM_REFUSED):
        """The page plays through the buffer, shows its picture, and the buffer fails."""
        self.link(KICK_PAGE)
        self.live.play_pipeline()
        self.plays_now()
        starts = len(self.starts())
        self.live.failed.emit(message)
        return starts

    def test_a_site_link_whose_buffer_fails_asks_the_site_again(self):
        self.answers = [MASTER, MASTER_FRESH]
        starts = self.pipeline_fails()
        self.assertTrue(wait_until(lambda: len(self.starts()) == starts + 1))
        self.assertEqual(self.starts()[-1][0], MASTER_FRESH)
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(self.errors(), [])  # no refusal toast

    def test_a_site_that_is_off_the_air_after_a_buffer_failure_waits_for_it(self):
        # Owner 2026-11-25: the streamer ended: the channel waits on screen (no error), its
        # site asked again calmly, and plays its newest once it is live again.
        self.calm()
        self.answers = [MASTER, SiteError("offline"), SiteError("offline"), MASTER_FRESH]
        starts = self.pipeline_fails()
        self.assertTrue(wait_until(lambda: OFFLINE in (self.pill_text() or "")))
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(self.store.get(stream_id(KICK_PAGE)).failed, OFFLINE)
        self.assertTrue(wait_until(lambda: len(self.starts()) == starts + 1, timeout_ms=5000))
        self.assertEqual(self.starts()[-1][0], MASTER_FRESH)
        self.assertEqual(len(self.asked), 4)
        self.assertEqual(self.errors(), [])

    def test_a_zap_in_flight_is_not_lost_to_a_site_buffer_failure(self):
        late = threading.Event()

        def slow(stop):
            late.wait(5)
            return MASTER_OTHER

        self.answers = [MASTER, slow]
        self.link(KICK_PAGE)
        self.live.play_pipeline()
        self.plays_now()
        starts = len(self.starts())
        self.player.play(TWITCH_PAGE, LIVE)  # the zap: its site is slow to answer
        self.assertTrue(wait_until(lambda: len(self.asked) == 2))
        self.live.failed.emit(STREAM_REFUSED)  # the old channel's buffer fails meanwhile
        self.assertEqual(len(self.asked), 2)  # the old site was not asked again
        self.opens(late.set)
        self.assertEqual(self.starts()[-1][0], MASTER_OTHER)
        self.assertEqual(self.controller.now_playing().item["cmd"], TWITCH_PAGE)
        self.assertGreater(len(self.starts()), starts)

    def test_a_site_link_asks_again_each_time_after_a_growing_wait(self):
        # Owner 2026-11-25: a token that runs out again and again is asked for again each time,
        # at once first, then after a growing wait while each fresh address fails before it
        # played HEALTHY_S; nothing is said while the site answers (the pill counts down).
        self.enterContext(mock.patch.object(live_controls, "SITE_AGAIN_S", (0.0, 0.3, 0.6)))
        self.answers = [MASTER, MASTER_FRESH, MASTER_OTHER, MASTER_NEWER]
        starts = self.pipeline_fails()
        self.assertTrue(wait_until(lambda: len(self.starts()) == starts + 1))
        for count, (address, wait) in enumerate(((MASTER_OTHER, 0.3), (MASTER_NEWER, 0.6)), 2):
            self.live.play_pipeline()
            self.plays_now()
            began = time.monotonic()
            self.live.failed.emit(STREAM_REFUSED)  # again, at once
            self.assertTrue(wait_until(lambda: self.pill_text() is not None))
            self.assertTrue(
                wait_until(lambda: len(self.starts()) == starts + count, timeout_ms=5000)
            )
            self.assertGreaterEqual(time.monotonic() - began, wait - 0.05)
            self.assertEqual(self.starts()[-1][0], address)
        self.assertEqual(len(self.asked), 4)
        self.assertEqual(self.errors(), [])

    def test_the_helpers_second_wait_stops_no_site_link(self):
        # A link is tried again once (LINK_TRIES): a site's channel is the site's to give, and
        # the helper's waits for its stream never stop it.
        self.answers = [MASTER]
        self.link(KICK_PAGE)
        self.live.play_pipeline()
        self.plays_now()
        for _ in range(3):
            self.live.retrying.emit(5.0)  # the helper waits for its stream
            self.live.retrying.emit(-1.0)  # it came back
        pause(100)
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.live.named("stop"), [])

    def test_a_site_link_that_played_a_while_asks_again_when_it_fails_again(self):
        # Final review I1: the fresh address's token runs out in turn (the Mac slept twice).
        # HEALTHY_S after the re-asked tune's first picture, the site is asked again; a
        # picture again since (a hold let go, a reopen) does not count the time anew.
        self.enterContext(mock.patch.object(live_controls, "HEALTHY_S", 0.2))
        self.answers = [MASTER, MASTER_FRESH, MASTER_OTHER]
        starts = self.pipeline_fails()
        self.assertTrue(wait_until(lambda: len(self.starts()) == starts + 1))
        self.live.play_pipeline()
        self.plays_now()
        pause(300)  # HEALTHY_S since its first picture
        self.plays_now()  # its hold let go: "playing" again
        self.live.failed.emit(STREAM_REFUSED)
        self.assertTrue(wait_until(lambda: len(self.starts()) == starts + 2))
        self.assertEqual(self.starts()[-1][0], MASTER_OTHER)
        self.assertEqual(len(self.asked), 3)
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.store.get(stream_id(KICK_PAGE)).failed, "")

    def test_the_helpers_wait_spends_no_ask_of_the_site(self):
        # Final review I1 (b): on waking, the network comes back only after the helper's
        # quick reconnects. Its wait (a counted `retrying`) is no ask of the site: the token's
        # death after it still asks.
        self.answers = [MASTER, MASTER_FRESH]
        self.link(KICK_PAGE)
        self.live.play_pipeline()
        self.plays_now()
        starts = len(self.starts())
        self.live.retrying.emit(5.0)  # the helper waits for its stream
        self.live.failed.emit(STREAM_REFUSED)  # its one try met the token's death
        self.assertTrue(wait_until(lambda: len(self.starts()) == starts + 1))
        self.assertEqual(self.starts()[-1][0], MASTER_FRESH)
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(self.errors(), [])

    def test_another_link_whose_buffer_is_refused_is_not_tuned_again(self):
        self.link(URL)
        self.live.play_pipeline()
        self.plays_now()
        starts = len(self.starts())
        self.live.failed.emit(STREAM_REFUSED)
        pause(200)
        self.assertEqual(len(self.starts()), starts)
        self.assertTrue(self.errors())  # the refusal says so as ever
        self.assertEqual(self.asked, [])

    def test_a_site_that_is_off_the_air_when_it_reopens_waits_for_it(self):
        self.calm()
        self.answers = [MASTER, SiteError("offline"), MASTER_FRESH]
        self.drops_direct(KICK_PAGE)
        self.assertTrue(wait_until(lambda: OFFLINE in (self.pill_text() or "")))
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(self.store.get(stream_id(KICK_PAGE)).failed, OFFLINE)
        self.assertTrue(wait_until(lambda: len(self.asked) == 3, timeout_ms=5000))
        self.assertTrue(
            wait_until(lambda: self.starts() and self.starts()[-1][0] == MASTER_FRESH, 5000)
        )
        self.assertEqual(self.errors(), [])

    def calm(self, seconds=0.3):
        """The offline wait's calm asks, every `seconds` here (30 s and more in the app)."""
        self.enterContext(mock.patch.object(live_controls, "OFFLINE_ASK_S", seconds))
        self.enterContext(mock.patch.object(live_controls, "OFFLINE_JITTER_S", 0.0))

    def test_a_site_page_opened_while_offline_waits_and_plays_once_live(self):
        # Owner 2026-11-25: "if i give url, streamer is offline and comes online, just when you
        # sense resync to latest". What played stops (its stream goes back); the channel waits
        # on screen, said by the pill; its site is asked again calmly, one ask at a time, until
        # it is live: then it plays (from its newest: the tune's own start).
        self.calm()
        self.link(URL)
        self.plays_now()
        stops = len(self.live.named("stop"))
        self.answers = [SiteError("offline"), SiteError("offline"), MASTER]
        asked_at = []
        resolve = self.resolve

        def timed(page, stop=None, **kwargs):
            asked_at.append(time.monotonic())
            return resolve(page, stop, **kwargs)

        self.enterContext(mock.patch.object(live_sites, "resolve", timed))
        self.player.play(KICK_PAGE, LIVE)
        self.assertTrue(wait_until(lambda: self.pill_text() is not None))
        self.assertIn(OFFLINE, self.pill_text())
        self.assertEqual(self.controller.now_playing().item["cmd"], KICK_PAGE)
        self.assertGreater(len(self.live.named("stop")), stops)  # the link before it stopped
        self.assertEqual(self.store.get(stream_id(KICK_PAGE)).failed, OFFLINE)  # said in its row
        self.assertTrue(wait_until(lambda: self.starts() and self.starts()[-1][0] == MASTER, 5000))
        self.assertEqual(len(self.asked), 3)
        gaps = [b - a for a, b in zip(asked_at, asked_at[1:])]
        self.assertTrue(all(gap >= 0.25 for gap in gaps), gaps)  # never in a burst
        self.assertEqual(self.errors(), [])

    def test_a_film_left_for_a_channel_that_isnt_live_keeps_where_it_stopped(self):
        # The wait takes the large picture as a delivered link would: the film's last place is
        # saved and its transport goes.
        self.calm(30.0)
        self.link(FILM_URL, FILM)
        self.plays_now()
        self.session.positionChanged.emit(125_000, 6_000_000)
        cid = f"{NETWORK_PROVIDER}:movie:{stream_id(FILM_URL)}"
        flushes = self.watch.flushes
        self.answers = [SiteError("offline")]
        self.player.play(KICK_PAGE, LIVE)
        self.assertTrue(wait_until(lambda: self.pill_text() is not None))
        self.assertEqual(self.watch.positions[-1], (cid, 125_000, 6_000_000))
        self.assertGreater(self.watch.flushes, flushes)
        self.assertFalse(self.controller.overlays.transport.isVisible())

    def test_an_older_builds_row_plays_and_opens_as_one(self):
        # Ask 3 through the player: a Twitch row an older build kept without "www." (its id is
        # not its page's) plays from its row and by Open Link under another spelling, as one.
        page = "https://twitch.tv/otherchannel"
        with mock.patch.object(ns, "site_page", lambda url: None):
            old = stream_id(page)
        self.assertNotEqual(old, stream_id(page))
        row = {"id": old, "name": "otherchannel · Twitch", "url": page, "kind": "LIVE"}
        self.store.load(json.dumps({"v": 1, "entries": [row]}).encode())
        self.answers = [MASTER_OTHER, MASTER_OTHER]
        self.opens(lambda: self.player.play_kept(old))
        self.plays_now()
        self.opens(lambda: self.player.play("https://www.twitch.tv/OtherChannel", LIVE))
        self.plays_now()
        self.assertEqual([e.id for e in self.store.entries()], [old])

    def test_the_wait_ends_when_another_is_chosen(self):
        self.calm()
        self.answers = [SiteError("offline")]
        self.player.play(KICK_PAGE, LIVE)
        self.assertTrue(wait_until(lambda: self.pill_text() is not None))
        self.link(URL)  # the viewer chose another meanwhile
        pause(800)
        self.assertEqual(len(self.asked), 1)  # never asked again
        self.assertEqual(self.starts()[-1][0], URL)

    def test_a_wait_whose_site_then_refuses_says_so_and_ends(self):
        self.calm()
        self.answers = [SiteError("offline"), SiteError("refused")]
        self.player.play(KICK_PAGE, LIVE)
        self.assertTrue(wait_until(lambda: self.pill_text() is not None))
        self.assertTrue(wait_until(lambda: self.errors()))
        self.assertTrue(self.errors()[-1].startswith("Kick refused"))
        self.assertIsNone(self.pill_text())  # no longer waits
        pause(800)
        self.assertEqual(len(self.asked), 2)

    def test_the_wait_ends_with_qitv(self):
        self.calm()
        self.answers = [SiteError("offline")]
        self.player.play(KICK_PAGE, LIVE)
        self.assertTrue(wait_until(lambda: self.pill_text() is not None))
        self.controller.shutdown()
        pause(800)
        self.assertEqual(len(self.asked), 1)

    def test_try_now_asks_at_once(self):
        self.calm(30.0)
        self.answers = [SiteError("offline"), MASTER]
        self.player.play(KICK_PAGE, LIVE)
        self.assertTrue(wait_until(lambda: self.pill_text() is not None))
        self.opens(lambda: self.assertTrue(self.controller._live_controls.retry_now()))
        self.assertEqual(self.starts()[-1][0], MASTER)
        self.assertEqual(len(self.asked), 2)

    def test_other_links_reopen_as_before(self):
        plays = self.drops_direct(URL)
        self.assertTrue(wait_until(lambda: len(self.session.plays) == plays + 1))
        self.assertEqual(self.session.plays[-1][0], URL)  # the same address, once
        self.assertEqual(self.asked, [])  # no site was asked
        pause(200)
        self.assertEqual(len(self.session.plays), plays + 1)

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

    def test_a_newer_tune_is_asked_at_once_while_a_stopped_ask_still_runs(self):
        # Task 6 M3: a site's ask stops only between its requests (up to 10 s); the newer
        # choice is asked meanwhile, not queued behind it.
        late = threading.Event()
        self.addCleanup(late.set)

        def stuck(stop):
            late.wait(5)  # (one request still on its way: the stop is seen after it)
            return SiteError("network")

        self.answers = [stuck, MASTER_OTHER]
        self.player.play(KICK_PAGE, LIVE)
        self.assertTrue(wait_until(lambda: len(self.asked) == 1))
        self.opens(lambda: self.player.play(TWITCH_PAGE, LIVE))
        self.assertFalse(late.is_set())  # A's ask still runs: B did not wait for it
        self.assertEqual(self.starts()[-1][0], MASTER_OTHER)
        late.set()
        pause(100)
        self.assertEqual(self.errors(), [])

    def test_stop_and_quitting_end_a_site_ask_on_its_way(self):
        # Task 6 M5, M6: nobody waits for its answer any more.
        for how in ("stop", "quit"):
            with self.subTest(how):
                stopped = threading.Event()

                def waits(stop):
                    stopped.set() if stop.wait(5) else None
                    return SiteError("network")

                self.answers.append(waits)
                self.player.play(KICK_PAGE, LIVE)
                self.assertTrue(wait_until(lambda: self.asked and not self.answers))
                if how == "stop":
                    self.controller.stop()
                else:
                    self.controller.shutdown()
                self.assertTrue(wait_until(stopped.is_set))
                pause(100)
                self.assertEqual(self.errors(), [])

    def test_a_failed_resolve_counts_and_says_why(self):
        refused = "Kick refused QiTV's request: try again later, or paste the stream link"
        failed, timed_out = "The link could not be opened", "The link didn't answer in time"
        named = "examplechannel · Kick: "  # (a reason that names no channel follows its name)
        cases = (  # (the site's error; why, as Network streams keeps it; the toast)
            # (offline: the channel waits for it, said by the pill, `..._waits_and_plays_...`)
            (SiteError("unknown"), *["No channel named examplechannel on Kick"] * 2),
            (SiteError("refused"), refused, refused),
            (SiteError("network"), failed, named + failed),
            (SiteError("network", timed_out=True), timed_out, named + timed_out),
        )
        for count, (error, why, toast) in enumerate(cases, 1):
            with self.subTest(why):
                before = self.store.get(stream_id(KICK_PAGE))
                self.assertEqual(before.failed if before is not None else "", "")
                self.answers.append(error)
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

    def steps_over(self, *offline):
        """Network streams: URL (playing), THIRD, then the site pages `offline` (kept, never
        opened: listed last, the latest kept first), each of whose sites gives no stream."""
        ticks = iter(range(1, 10_000))
        self.player._now = lambda: NOW + next(ticks)
        third = LinkStepTests.THIRD
        for url in (third, URL):
            self.link(url)
            self.plays_now()
        for page in offline:
            self.player.keep_new(page, LIVE)
        self.answers.extend(SiteError("offline") for _ in offline)
        return third

    def fails_on(self, action, channel):
        """`action` steps onto a site page whose site gives no stream: said, and the link
        before it plays on."""
        said = len(self.errors())
        self.controller.handle(action)
        self.assertTrue(wait_until(lambda: len(self.errors()) == said + 1))
        self.assertEqual(self.errors()[-1], f"{channel} isn't live now")
        self.assertEqual(self.controller.now_playing().item["id"], stream_id(URL))

    def test_with_every_other_link_failed_a_step_asks_them_again(self):
        # Task 6: the run's links all failed (passed): the next press asks them in turn again,
        # never does nothing.
        self.player._now = lambda: NOW
        self.link(URL)
        self.plays_now()
        self.player.keep_new(KICK_PAGE, LIVE)
        self.answers.extend([SiteError("offline"), SiteError("offline")])
        self.fails_on(Action.CH_NEXT, "examplechannel")
        self.fails_on(Action.CH_NEXT, "examplechannel")  # asked again, not nothing
        self.assertEqual(len(self.asked), 2)

    def test_two_offline_pages_in_a_row_are_each_asked_once_and_stepped_past(self):
        third = self.steps_over(KICK_PAGE, TWITCH_PAGE)  # URL, THIRD, TWITCH_PAGE, KICK_PAGE
        self.fails_on(Action.CH_PREV, "examplechannel")  # before the first: the last
        self.fails_on(Action.CH_PREV, "otherchannel")  # on past it: the one before
        self.opens(lambda: self.controller.handle(Action.CH_PREV))  # and past both
        self.assertEqual(self.starts()[-1][0], third)
        pages = [page.channel for page, _on_gui in self.asked]
        self.assertEqual(pages, ["examplechannel", "otherchannel"])  # each asked once
        # Once a step plays, the run's failures are forgotten: a page is asked in its turn.
        self.plays_now()
        self.answers.append(SiteError("offline"))
        self.controller.handle(Action.CH_NEXT)  # THIRD's next: the Twitch page, asked again
        self.assertTrue(wait_until(lambda: len(self.asked) == 3))
        self.assertEqual(self.asked[-1][0].channel, "otherchannel")

    def test_a_reversed_press_after_an_offline_page_moves_from_the_playing_link(self):
        third = self.steps_over(KICK_PAGE)  # URL, THIRD, KICK_PAGE
        self.fails_on(Action.CH_PREV, "examplechannel")
        self.opens(lambda: self.controller.handle(Action.CH_NEXT))  # the other way, from URL
        self.assertEqual(self.starts()[-1][0], third)
        self.assertEqual(len(self.asked), 1)

    def test_a_side_play_of_an_offline_page_counts_in_its_row(self):
        # Play in VLC on a kept page whose site gives no stream: said, and its row says why,
        # as a play in QiTV would.
        self.player.keep_new(KICK_PAGE, LIVE)
        item = {
            "id": stream_id(KICK_PAGE),
            "name": "examplechannel · Kick",
            "cmd": KICK_PAGE,
            "_provider": NETWORK_PROVIDER,
        }
        self.answers.append(SiteError("offline"))
        with tvc.shown_menus() as popup:
            self.controller._context.show_target(Target(item, "live", item["name"]), QPoint(5, 5))
        tvc.menus_of(popup)[0][0].entry("Play in VLC").trigger()
        self.assertTrue(wait_until(lambda: self.errors()))
        self.assertEqual(self.errors(), ["examplechannel isn't live now"])
        entry = self.store.get(stream_id(KICK_PAGE))
        self.assertEqual(entry.failed, "examplechannel isn't live now")
        self.assertEqual(self.external.calls, [])
        self.assertEqual(len(self.asked), 1)

    def kept_item(self):
        self.player.keep_new(KICK_PAGE, LIVE)
        return {
            "id": stream_id(KICK_PAGE),
            "name": "examplechannel · Kick",
            "cmd": KICK_PAGE,
            "_provider": NETWORK_PROVIDER,
        }

    def test_a_phones_watch_of_an_offline_page_is_heard_by_the_phone_not_the_tv(self):
        # 2.15: play_elsewhere's `failed` hears why (the phone waits): the TV says nothing,
        # and its row says why, once.
        target = Target(self.kept_item(), "live", "examplechannel · Kick")
        heard, launched = [], []
        self.answers = [SiteError("offline"), SiteError("offline")]

        def hear(asked, why):
            heard.append((asked.item["cmd"], why))
            return len(heard) == 1  # it begins to wait, then waits on

        def launch(_target, url):
            launched.append(url)
            return True, ""

        self.assertTrue(self.controller.play_elsewhere(launch, target, failed=hear))
        self.assertTrue(wait_until(lambda: heard))
        self.assertEqual(heard, [(KICK_PAGE, OFFLINE)])
        self.assertEqual(self.store.get(stream_id(KICK_PAGE)).failed, OFFLINE)  # its row
        self.controller.play_elsewhere(launch, target, failed=hear, again=lambda: True)
        self.assertTrue(wait_until(lambda: len(heard) == 2))
        pause(100)
        self.assertEqual(self.errors(), [])
        self.assertEqual(launched, [])

    def test_a_phone_that_waits_for_the_channel_the_tv_waits_for_asks_alone(self):
        # 2.15: Watch on this phone while the large picture waits for its channel: the phone
        # waits for it now, and the TV's wait ends (its site is asked by one, not two).
        self.calm()
        self.answers = [SiteError("offline")]
        self.player.play(KICK_PAGE, LIVE)
        self.assertTrue(wait_until(lambda: OFFLINE in (self.pill_text() or "")))
        self.answers = [SiteError("offline")]
        heard = []
        self.controller.play_elsewhere(
            lambda _t, _url: (True, ""), None, failed=lambda t, why: heard.append(why) or True
        )
        self.assertTrue(wait_until(lambda: heard))
        self.assertEqual(len(self.asked), 2)
        pause(900)  # three of the TV's calm asks, were it still waiting
        self.assertEqual(len(self.asked), 2)
        self.assertIsNone(self.pill_text())
        self.assertEqual(self.errors(), [])

    def test_two_phones_asking_for_one_target_each_hear_their_own_answer(self):
        # Review 4: each phone's ask is its own: two asks for the same Target (two phones'
        # Watch of what the TV plays) are both asked, and each hook hears its own answer once.
        target = Target(self.kept_item(), "live", "examplechannel · Kick")
        heard = {"a": [], "b": []}
        self.answers = [SiteError("offline"), SiteError("offline")]
        for name in ("a", "b"):
            self.controller.play_elsewhere(
                lambda _t, _url: (True, ""),
                target,
                failed=lambda _t, why, name=name: heard[name].append(why) or True,
                again=lambda: True,
            )
        self.assertTrue(wait_until(lambda: heard["a"] and heard["b"]))
        pause(100)
        self.assertEqual(heard, {"a": [OFFLINE], "b": [OFFLINE]})
        self.assertEqual(len(self.asked), 2)

    def test_a_phones_watch_that_hears_failures_is_handed_its_link(self):
        # 2.15: a Watch with `failed` (its own copy of the target) plays as any Watch does:
        # the TV lets go, the phone gets the link, the hook hears nothing.
        heard, launched = [], []

        def launch(_target, url):
            launched.append(url)
            return True, ""

        target = Target(self.kept_item(), "live", "examplechannel · Kick")
        for asked, answer, link in ((target, MASTER, MASTER), (None, None, URL)):
            with self.subTest(asked=asked is not None):
                self.link(URL)  # the TV plays a link of its own
                self.plays_now()
                stops = len(self.live.named("stop"))
                if answer is not None:
                    self.answers = [answer]
                launched.clear()
                self.controller.play_elsewhere(
                    launch, asked, failed=lambda _t, why: heard.append(why) or True
                )
                self.assertTrue(wait_until(lambda: len(self.live.named("stop")) > stops))
                self.live.released.emit()
                self.assertTrue(wait_until(lambda: launched == [link]))
        self.assertEqual(heard, [])

    def test_a_waiting_phones_ask_drops_no_choice_of_the_tvs(self):
        # 2.15: its asks go beside the TV's choices (the TV's zap meanwhile still opens), and
        # once live its stream is handed on only while the phone still waits for it.
        target = Target(self.kept_item(), "live", "examplechannel · Kick")
        launched = []

        def launch(_target, url):
            launched.append(url)
            return True, ""

        self.link(URL)
        self.plays_now()
        generation, stops = self.controller._lanes._generation, len(self.live.named("stop"))
        self.answers = [MASTER]
        wanted = [False]
        self.controller.play_elsewhere(launch, target, again=lambda: wanted[0])
        self.assertTrue(wait_until(lambda: len(self.asked) == 1))
        pause(100)
        self.assertEqual(self.controller._lanes._generation, generation)  # nothing superseded
        self.assertEqual(launched, [])  # no longer wanted: its address goes unused
        self.assertEqual(self.controller.now_playing().item["cmd"], URL)  # the TV plays on
        self.assertEqual(len(self.live.named("stop")), stops)
        self.answers = [MASTER_FRESH]
        wanted[0] = True
        self.controller.play_elsewhere(launch, target, again=lambda: wanted[0])
        # the TV lets go for it
        self.assertTrue(wait_until(lambda: len(self.live.named("stop")) > stops))
        self.live.released.emit()
        self.assertTrue(wait_until(lambda: launched == [MASTER_FRESH]))

    def test_a_channel_that_plays_through_its_buffer_rewinds_into_its_sites_recording(self):
        # 2.15: once the channel plays through its buffer, its site is asked (on a worker)
        # for the running broadcast's recording, once a tune; the live session takes it.
        asked = []

        def running(page, stop=None, **_kwargs):
            asked.append((page, threading.current_thread() is threading.main_thread()))
            return live_sites.Recording(
                "https://rec.example/1080p60/playlist.m3u8", 6_400.0, True, True
            )

        self.enterContext(mock.patch.object(live_sites, "running_recording", running))
        self.answers = [MASTER]
        self.link(KICK_PAGE)
        self.live.play_pipeline()
        self.assertEqual(asked, [])  # never before its picture: the timed start waits for nothing
        self.plays_now()
        self.assertTrue(wait_until(lambda: self.live.named("set_site_recording")))
        (route,) = self.live.named("set_site_recording")[0]
        self.assertEqual(
            (route.url, route.start, route.dated, route.steps),
            ("https://rec.example/1080p60/playlist.m3u8", 6_400.0, True, True),
        )
        self.assertEqual(asked, [(SitePage(KICK, "examplechannel"), False)])  # off the GUI
        self.plays_now()  # it plays again, the same session's start: asked once
        pause(100)
        self.assertEqual(len(asked), 1)
        # Review (2026-11-25): its stream reopened (a renewed token): the session started
        # again and forgot the recording, so it is asked again once that plays; at most every
        # RECORDING_ASK_S per channel (second review), the ask after a reopen held back.
        self.enterContext(mock.patch("ui.live_controls.RECORDING_ASK_S", 1.0))
        self.live.start(MASTER)
        self.live.play_pipeline()
        self.plays_now()
        pause(150)
        self.assertEqual(len(asked), 1)  # held back
        self.assertTrue(wait_until(lambda: len(asked) == 2, timeout_ms=3000))
        self.assertTrue(wait_until(lambda: len(self.live.named("set_site_recording")) == 2))

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


TWITCH_VIDEO = "https://www.twitch.tv/videos/123456"
KICK_CLIP = "https://kick.com/examplechannel/clips/clip_01KABC"
VIDEO_URL = "https://cdn.example/abc_examplechannel_1/1080p60/index-dvr.m3u8"
CLIP_URL = "https://clips.example/clip/1080/index.mp4?sig=sentinel-sig&token=sentinel-token"


class SiteFilmTests(PlayerCase):
    """2.15: a Kick or Twitch video's or clip's page plays as a film: asked afresh of its site
    each play (services.live_sites.resolve_media, faked here), kept by its page, named by its
    title, resumed where it stopped; one that is gone says so."""

    def setUp(self):
        super().setUp()
        self.loaded()
        self.asked = []
        self.answers = []  # each ask's answer in turn: (address, title) or a SiteError
        self.enterContext(mock.patch.object(live_sites, "resolve_media", self.resolve))
        self.addCleanup(lambda: wait_until(lambda: self.controller._jobs.pending() == 0))

    def resolve(self, media, stop=None, **_kwargs):
        self.asked.append((media, threading.current_thread() is threading.main_thread()))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        url, title = answer
        return Resolved(url, live_sites.media_name(media, title))

    def errors(self):
        return [text for text, kind in self.toast.messages() if kind == "error"]

    def test_a_video_plays_as_a_film_asked_afresh_named_by_its_title_and_resumes(self):
        self.answers = [(VIDEO_URL, "A long evening"), (VIDEO_URL, "A long evening")]
        self.link("twitch.tv/videos/123456?t=1h", FILM)
        url, options = self.session.plays[-1]
        self.assertEqual(url, VIDEO_URL)  # the site's answer plays, as a film
        self.assertIsNone(options["start_ms"])
        self.assertEqual(self.asked, [(live_sites.SiteMedia("twitch", "video", "123456"), False)])
        self.assertEqual(self.catalog.links, [])
        self.plays_now()
        key = stream_id(TWITCH_VIDEO)
        entry = self.store.get(key)
        self.assertEqual((entry.url, entry.kind), ("twitch.tv/videos/123456?t=1h", FILM))
        self.assertEqual(entry.name, "A long evening · Twitch")
        cid = f"{NETWORK_PROVIDER}:movie:{key}"
        self.session.positionChanged.emit(125_000, 6_000_000)
        self.assertEqual(self.watch.positions[-1], (cid, 125_000, 6_000_000))
        self.watch.resume[cid] = 125_000
        self.opens(lambda: self.player.play_kept(key))
        url, options = self.session.plays[-1]
        self.assertEqual((url, options["start_ms"]), (VIDEO_URL, 125_000))
        self.assertEqual(len(self.asked), 2)  # asked again for this play
        self.assertEqual(len(self.store.entries()), 1)

    def test_the_title_names_a_row_once_and_never_the_viewers(self):
        self.answers = [(CLIP_URL, "Wow"), (CLIP_URL, "Wow, renamed by the site")]
        self.link(KICK_CLIP, FILM)
        self.plays_now()
        key = stream_id(KICK_CLIP)
        self.assertEqual(self.store.get(key).name, "Wow · Kick")
        self.assertEqual(key, stream_id("https://kick.com/examplechannel?clip=clip_01KABC"))
        self.player.rename(key, "Mine")
        self.opens(lambda: self.player.play_kept(key))
        self.plays_now()
        self.assertEqual(self.store.get(key).name, "Mine")

    def test_the_tokenized_address_is_never_kept_logged_or_remembered(self):
        self.answers = [(CLIP_URL, "Wow")]
        with self.assertLogs(level="DEBUG") as logs:
            self.link(KICK_CLIP, FILM)
            self.plays_now()
        self.assertEqual(self.session.plays[-1][0], CLIP_URL)
        self.assertNotIn("sentinel", "\n".join(logs.output))
        self.assertNotIn("sentinel", self.store.save_bytes().decode())
        provider, item, kind, link, _group = self.watch.plays[-1]
        self.assertEqual((provider, kind, link), (NETWORK_PROVIDER, "movie", KICK_CLIP))

    def test_copy_link_copies_the_videos_page(self):
        QGuiApplication.clipboard().setText("")
        self.answers = [(VIDEO_URL, "x")]
        self.link(TWITCH_VIDEO, FILM)
        self.plays_now()
        with tvc.shown_menus() as popup:
            self.controller.video_context(QPoint(10, 10))
        tvc.menus_of(popup)[0][0].entry("Copy stream link").trigger()
        self.assertEqual(QGuiApplication.clipboard().text(), TWITCH_VIDEO)
        self.assertEqual(len(self.asked), 1)

    def test_a_video_that_is_gone_or_private_says_so_plainly(self):
        for reason, text in (
            ("unknown", "This video is gone from Twitch"),
            ("private", "This video is private or for subscribers only on Twitch"),
        ):
            with self.subTest(reason):
                before = len(self.errors())
                self.answers = [SiteError(reason)]
                plays = len(self.session.plays)
                self.player.play(TWITCH_VIDEO, FILM)
                self.assertTrue(wait_until(lambda: len(self.errors()) > before))
                self.assertEqual(self.errors()[-1], text)  # said alone, in its own words
                self.assertEqual(len(self.session.plays), plays)  # nothing played
                self.assertEqual(self.store.get(stream_id(TWITCH_VIDEO)).failed, text)


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
