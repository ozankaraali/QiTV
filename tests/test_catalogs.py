import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from job_fixtures import ImmediateRunner
from mixed_fixtures import DEMO, HARBOR, HeldSetups, live_content, mixed_config
import orjson
from test_catalog_lifecycle import RealSetupFactory
from test_catalog_service import FakeWorker

from services.catalogs import Catalogs
from services.provider_ids import provider_of, with_provider
from ui.link_lanes import LinkLanes, Target
from ui.tv_window import ABANDONED_EXIT_CODE


class RecordingFactory(RealSetupFactory):
    """The real provider setup (recording whose it is); the guide is a fake."""

    def __init__(self):
        super().__init__()
        self.setups = []
        self.epg_loads = []

    def provider_setup(self, provider_manager, force_refresh):
        self.setups.append(provider_manager._pinned_provider)
        return super().provider_setup(provider_manager, force_refresh)

    def epg(self, epg_manager, force_refresh):
        self.epg_loads.append(epg_manager.name)
        return FakeWorker({})


class FakeEpg:
    def __init__(self, name=""):
        self.name = name
        self.released = 0

    def release(self):
        self.released += 1


class CatalogsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-catalogs-")
        self.addCleanup(directory.cleanup)
        self.config, self.registry = mixed_config(
            directory.name,
            [DEMO, HARBOR],
            {
                "Demo TV": live_content(["One", "Two"], {"News": [0, 1]}),
                "Harbor IPTV": live_content(["Four"], {"News": [0]}),
            },
        )
        self.addCleanup(self.registry._cache_writer.shutdown, wait=True)
        self.factory = RecordingFactory()
        self.in_use = set()
        self.epgs = []
        self.catalogs = Catalogs(
            self.config,
            self.registry,
            runner=ImmediateRunner(),
            factory=self.factory,
            make_epg=self.make_epg,
            in_use=lambda: self.in_use,
        )
        self.links, self.failures = [], []
        self.catalogs.linkReady.connect(lambda rid, url: self.links.append((rid, url)))
        self.catalogs.linkFailed.connect(lambda rid, msg: self.failures.append((rid, msg)))
        self.addCleanup(self.catalogs.shutdown)

    def make_epg(self, manager):
        epg = FakeEpg(manager._pinned_provider)
        self.epgs.append(epg)
        return epg

    def ready_links(self):
        QTest.qWait(30)  # a link answer is emitted from the event loop
        return list(self.links)

    def delete_provider(self, name):
        self.registry.providers[:] = [p for p in self.registry.providers if p["name"] != name]

    def test_a_catalog_per_provider_created_on_first_use(self):
        self.assertEqual(self.catalogs.names(), ["Demo TV", "Harbor IPTV"])
        self.assertIs(self.catalogs.catalog("Demo TV"), self.catalogs.catalog("Demo TV"))
        self.assertIsNot(self.catalogs.catalog("Demo TV"), self.catalogs.catalog("Harbor IPTV"))
        self.assertEqual(self.factory.setups, [])  # creating one starts no network

    def test_links_go_to_the_items_own_provider(self):
        harbor = with_provider({"id": 9, "cmd": "http://h/9.ts"}, "Harbor IPTV")
        request = self.catalogs.request_link(harbor, section="live")
        self.assertEqual(self.ready_links(), [(request, "http://h/9.ts")])
        self.assertEqual(self.factory.setups, ["Harbor IPTV"])  # signed in on first use
        self.assertTrue(self.catalogs.is_ready("Harbor IPTV"))
        self.assertFalse(self.catalogs.is_ready("Demo TV"))

    def test_the_ready_providers_hold_their_content_for_search_to_read(self):
        self.assertEqual(self.catalogs.loaded_contents(), {})  # nothing is set up yet
        self.catalogs.ensure("Harbor IPTV")
        QTest.qWait(30)
        loaded = self.catalogs.loaded_contents()
        self.assertEqual(list(loaded), ["Harbor IPTV"])
        self.assertEqual(loaded["Harbor IPTV"]["itv"]["contents"][0]["name"], "Four")

    def test_link_ids_are_unique_across_providers(self):
        ids = {
            self.catalogs.request_link(with_provider({"cmd": "x"}, n), section="live")
            for n in ("Demo TV", "Harbor IPTV", "Demo TV")
        }
        self.assertEqual(len(ids), 3)
        self.assertEqual({rid for rid, _ in self.ready_links()}, ids)

    def test_an_unknown_provider_fails_the_link(self):
        request = self.catalogs.request_link(with_provider({"cmd": "x"}, "Gone"), section="live")
        QTest.qWait(30)
        self.assertEqual([rid for rid, _ in self.failures], [request])

    def test_a_provider_deleted_while_it_plays_stays_until_out_of_use(self):
        self.in_use = {"Harbor IPTV"}
        old = self.catalogs.catalog("Harbor IPTV")
        self.delete_provider("Harbor IPTV")
        self.catalogs.reload()
        self.assertIs(self.catalogs.catalog("Harbor IPTV"), old)
        self.assertNotIn("Harbor IPTV", self.catalogs.names())
        self.in_use = set()
        self.catalogs.reload()
        self.assertNotIn("Harbor IPTV", self.catalogs._by_name)

    def test_an_edited_provider_is_rebuilt_on_next_use(self):
        old = self.catalogs.catalog("Demo TV")
        self.registry.providers[0]["url"] = "http://127.0.0.1:9/other.m3u"
        self.catalogs.reload()
        self.assertIsNot(self.catalogs.catalog("Demo TV"), old)

    def test_an_edited_provider_in_use_is_set_up_again_once_with_its_new_settings(self):
        # A renewed password must reach the pinned manager of a provider a page shows;
        # Settings' own forced refresh of it is not a second sign-in.
        runner = HeldSetups()
        catalogs = Catalogs(
            self.config,
            self.registry,
            runner=runner,
            factory=self.factory,
            make_epg=self.make_epg,
            in_use=lambda: {"Harbor IPTV"},
        )
        self.addCleanup(catalogs.shutdown)
        catalogs.ensure("Harbor IPTV")
        old = catalogs.catalog("Harbor IPTV")
        runner.held.add("Harbor IPTV")  # the next sign-in takes a while
        self.registry.providers[1] = {**self.registry.providers[1], "password": "renewed"}
        catalogs.reload()
        self.assertEqual(self.factory.setups, ["Harbor IPTV", "Harbor IPTV"])  # signing in
        catalogs.select("Harbor IPTV", force_refresh=True)
        runner.release("Harbor IPTV")
        self.assertIs(catalogs.catalog("Harbor IPTV"), old)  # what it plays keeps playing
        self.assertEqual(self.factory.setups, ["Harbor IPTV", "Harbor IPTV"])
        self.assertEqual(old.provider_manager.current_provider["password"], "renewed")
        self.assertTrue(catalogs.is_ready("Harbor IPTV"))

    def test_an_edit_that_keeps_the_connection_signs_in_and_loads_nothing_again(self):
        # Polish: only the stream limit changed on a provider a page shows. Its cache was
        # wiped and downloaded again (and an open drawer jumped back to live).
        self.in_use = {"Harbor IPTV"}
        self.catalogs.ensure("Harbor IPTV")
        catalog = self.catalogs.catalog("Harbor IPTV")
        cache = self.registry._provider_cache_name("Harbor IPTV")
        self.registry.providers[1] = {**self.registry.providers[1], "max_streams": 2}
        self.catalogs.reload()
        self.assertEqual(self.factory.setups, ["Harbor IPTV"])  # no second sign-in
        self.assertTrue(os.path.exists(cache))  # its cache stays
        self.assertTrue(catalog.has_content("live"))
        self.assertTrue(self.catalogs.is_ready("Harbor IPTV"))
        self.assertIs(self.catalogs.catalog("Harbor IPTV"), catalog)
        self.assertEqual(catalog.current_provider()["max_streams"], 2)  # the budget's limit
        self.catalogs.select("Harbor IPTV", force_refresh=True)  # Settings' own refresh runs
        self.assertEqual(self.factory.setups, ["Harbor IPTV", "Harbor IPTV"])

    def test_a_connection_changed_and_changed_back_before_the_setup_ends_signs_in_again(self):
        # Polish 2: the first edit's setup was running (the manager still held the old
        # entry), and changing it back looked like "nothing changed" and was skipped while the
        # running setup used the in-between entry.
        runner = HeldSetups()
        catalogs = Catalogs(
            self.config,
            self.registry,
            runner=runner,
            factory=self.factory,
            make_epg=self.make_epg,
            in_use=lambda: {"Harbor IPTV"},
        )
        self.addCleanup(catalogs.shutdown)
        catalogs.ensure("Harbor IPTV")
        original = dict(self.registry.providers[1])
        runner.held.add("Harbor IPTV")
        self.registry.providers[1] = {**original, "password": "in between"}
        catalogs.reload()
        self.assertEqual(self.factory.setups, ["Harbor IPTV", "Harbor IPTV"])
        self.registry.providers[1] = dict(original)  # back, before that setup ended
        catalogs.reload()
        runner.release("Harbor IPTV")  # the one running ends; the queued one takes the entry
        self.assertEqual(self.factory.setups, ["Harbor IPTV", "Harbor IPTV", "Harbor IPTV"])
        manager = catalogs.catalog("Harbor IPTV").provider_manager
        self.assertEqual(manager.current_provider.get("password"), original.get("password"))

    def test_an_edited_provider_in_use_is_not_set_up_again_by_the_next_reload(self):
        # The record its catalog was built for follows the edit, so a later save without
        # changes renews nothing.
        self.in_use = {"Harbor IPTV"}
        self.catalogs.ensure("Harbor IPTV")
        self.registry.providers[1] = {**self.registry.providers[1], "password": "renewed"}
        self.catalogs.reload()
        self.assertEqual(self.factory.setups, ["Harbor IPTV", "Harbor IPTV"])
        self.catalogs.reload()
        self.assertEqual(self.factory.setups, ["Harbor IPTV", "Harbor IPTV"])

    def test_an_unstamped_item_goes_to_the_first_provider_without_a_selection(self):
        # One provider, a stale selection (an old 2.0 Recent entry).
        self.config.selected_provider_name = "Gone"
        request = self.catalogs.request_link({"id": 9, "cmd": "http://d/9.ts"}, section="live")
        self.assertEqual(self.ready_links(), [(request, "http://d/9.ts")])
        self.assertEqual(self.factory.setups, ["Demo TV"])

    def test_an_unchanged_provider_keeps_its_catalog(self):
        old = self.catalogs.catalog("Demo TV")
        self.catalogs.reload()
        self.assertIs(self.catalogs.catalog("Demo TV"), old)

    def test_use_guides_releases_the_others(self):
        self.catalogs.catalog("Demo TV")
        self.catalogs.catalog("Harbor IPTV")
        self.catalogs.use_guides({"Demo TV"})
        self.assertEqual([e.released for e in self.epgs], [0, 1])

    def test_only_guides_in_use_are_loaded(self):
        self.catalogs.use_guides({"Demo TV"})
        QTest.qWait(50)
        self.assertEqual(self.factory.epg_loads, ["Demo TV"])
        self.catalogs.use_guides({"Harbor IPTV"})
        QTest.qWait(50)
        self.assertEqual(self.factory.epg_loads, ["Demo TV", "Harbor IPTV"])
        self.assertEqual([e.name for e in self.epgs if e.released], ["Demo TV"])

    def test_a_provider_set_up_for_browsing_loads_no_guide_once_guides_are_chosen(self):
        self.catalogs.use_guides({"Demo TV"})
        self.catalogs.ensure("Harbor IPTV")
        QTest.qWait(50)
        self.assertEqual(self.factory.epg_loads, ["Demo TV"])

    def test_every_guide_loads_until_guides_are_chosen(self):  # one provider: as before 2.1
        self.catalogs.ensure("Demo TV")
        QTest.qWait(50)
        self.assertEqual(self.factory.epg_loads, ["Demo TV"])

    def test_a_guide_released_while_it_loads_and_wanted_again_loads_again(self):
        # The load that ran ended "ready" although its guide may have been released after
        # it was stored: the guide stayed empty until the next refresh.
        runner = HeldGuides()
        catalogs = Catalogs(
            self.config,
            self.registry,
            runner=runner,
            factory=self.factory,
            make_epg=self.make_epg,
        )
        self.addCleanup(catalogs.shutdown)
        catalogs.use_guides({"Demo TV"})
        catalog = catalogs.catalog("Demo TV")
        self.assertTrue(catalog.epg_loading())
        catalogs.use_guides(set())  # the drawer moved on: the guide gives its memory back
        catalogs.use_guides({"Demo TV"})  # and back, before the load ended
        runner.finish_guides()
        self.assertFalse(catalog.epg_ready())
        self.assertEqual(self.factory.epg_loads, ["Demo TV", "Demo TV"])  # loads it again
        runner.finish_guides()
        self.assertTrue(catalog.epg_ready())
        self.assertEqual(self.factory.epg_loads, ["Demo TV", "Demo TV"])

    def test_a_guide_released_while_it_loads_stays_released(self):
        runner = HeldGuides()
        catalogs = Catalogs(
            self.config,
            self.registry,
            runner=runner,
            factory=self.factory,
            make_epg=self.make_epg,
        )
        self.addCleanup(catalogs.shutdown)
        catalogs.use_guides({"Demo TV"})
        catalogs.use_guides(set())
        runner.finish_guides()
        self.assertFalse(catalogs.catalog("Demo TV").epg_ready())
        self.assertEqual(self.factory.epg_loads, ["Demo TV"])

    def test_a_dropped_catalog_gives_its_guide_and_lists_back(self):
        # The hub forgot a removed provider's catalog, but the pages' views still held it,
        # and with it its guide and its channel lists, until QiTV quit.
        self.catalogs.ensure("Harbor IPTV")
        QTest.qWait(30)
        catalog = self.catalogs.catalog("Harbor IPTV")
        epg = self.epgs[-1]
        self.assertTrue(catalog.has_content("live"))
        self.delete_provider("Harbor IPTV")
        self.catalogs.reload()
        self.assertEqual(epg.released, 1)
        self.assertEqual(catalog.provider_manager.current_provider_content, {})


CACHE_WRITE_AT_EXIT_SCRIPT = r"""
import atexit, os, sys, time
from services.config_manager import ConfigManager
from services.provider_manager import ProviderManager
import ui.tv_window as tw

manager = ProviderManager(ConfigManager())
manager.current_provider = {"type": "M3UPLAYLIST", "name": "Demo TV", "url": "http://127.0.0.1:9/x"}
manager.current_provider_content = {"itv": {"categories": [], "contents": []}}
write = manager._write_provider_cache
manager._write_provider_cache = lambda *args: (time.sleep(0.5), write(*args))
manager.save_provider()  # still writing when the window gives up waiting and leaves
manager._cache_writer.shutdown(wait=False)  # Catalogs._stop
atexit.register(tw._exit_now)  # TVWindow._abandon_threads
print(manager._provider_cache_name("Demo TV"), flush=True)
"""


class CacheWriteAtExitTests(unittest.TestCase):
    def test_a_cache_write_queued_at_quit_ends_before_the_hard_exit(self):
        # Catalogs._stop does not wait for the cache writer; the interpreter joins its
        # thread before any atexit handler runs, _exit_now's os._exit included.
        repo = Path(__file__).resolve().parent.parent
        directory = tempfile.TemporaryDirectory(prefix="qitv-exit-cache-")
        self.addCleanup(directory.cleanup)
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QITV_CONFIG_DIR": directory.name}
        result = subprocess.run(
            [sys.executable, "-c", CACHE_WRITE_AT_EXIT_SCRIPT],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, ABANDONED_EXIT_CODE, result.stderr[-2000:])
        with open(result.stdout.strip(), "rb") as f:
            self.assertEqual(orjson.loads(f.read())["itv"], {"categories": [], "contents": []})


class HeldGuides(ImmediateRunner):
    """Runs every job at once except guide loads, which wait for finish_guides()."""

    def __init__(self):
        self.guides: list[tuple] = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        if getattr(context, "kind", "") == "epg":
            self.guides.append((worker, on_finished, on_error, context))
            return worker
        return super().run_worker(worker, on_finished, on_error, context)

    def finish_guides(self) -> None:
        guides, self.guides = self.guides, []
        for job in guides:
            super().run_worker(*job)


class LinksDuringSetupTests(unittest.TestCase):
    """Links asked while their provider sets up (first use, a refresh) wait for it."""

    ONE = with_provider({"id": 1, "cmd": "http://127.0.0.1:9/live/1.ts"}, "Demo TV")
    TWO = with_provider({"id": 2, "cmd": "http://127.0.0.1:9/live/2.ts"}, "Demo TV")
    setUpClass = CatalogsTests.setUpClass
    make_epg = CatalogsTests.make_epg

    def setUp(self):
        CatalogsTests.setUp(self)  # type: ignore[arg-type]
        self.runner = HeldSetups()
        self.hub = Catalogs(
            self.config,
            self.registry,
            runner=self.runner,
            factory=self.factory,
            make_epg=self.make_epg,
        )
        self.addCleanup(self.hub.shutdown)
        self.hub_links, self.hub_failures = [], []
        self.hub.linkReady.connect(lambda rid, url: self.hub_links.append((rid, url)))
        self.hub.linkFailed.connect(lambda rid, msg: self.hub_failures.append((rid, msg)))

    def lanes(self):
        """The TV shell's link lanes over the hub, settled as the controller settles them on
        the provider's providerReady and providerFailed (`dropped`: the tunes it drops)."""
        lanes = LinkLanes(self.hub)
        self.dropped, self.side_lost = [], []

        def failed(name, _message):
            dropped, lost = lanes.provider_failed(name, provider_of)
            self.dropped.append(dropped)
            self.side_lost.append(lost)

        self.hub.providerReady.connect(
            lambda name: self.dropped.append(lanes.provider_ready(name, provider_of))
        )
        self.hub.providerFailed.connect(failed)
        self.tunes, self.side = [], []
        lanes.tuneReady.connect(lambda target, url: self.tunes.append(url))
        lanes.tuneFailed.connect(lambda target, message: self.tunes.append(message))
        lanes.sideFailed.connect(lambda target, message: self.side.append(message))
        return lanes

    def test_a_link_asked_during_a_refresh_waits_for_it(self):
        # A refresh left the provider "ready", so a link asked meanwhile failed with
        # "The provider is still loading" instead of waiting for the refresh.
        self.hub.ensure("Demo TV")
        self.runner.held.add("Demo TV")
        self.hub.select("Demo TV", force_refresh=True)  # ↻: its channels and guide again
        self.assertFalse(self.hub.is_ready("Demo TV"))
        request = self.hub.request_link(self.ONE, section="live")
        QTest.qWait(30)
        self.assertEqual(self.hub_failures, [])
        self.runner.release("Demo TV")
        QTest.qWait(30)
        self.assertEqual(self.hub_links, [(request, "http://127.0.0.1:9/live/1.ts")])
        self.assertEqual(self.hub_failures, [])

    def test_links_held_for_a_setup_reach_their_lanes_once(self):
        # The shell dropped every request of a provider that became ready (its catalog
        # drops what it was asked before), also those the hub held for the setup: a tune
        # was asked twice (two STB links) and a menu's link was lost without a word.
        self.runner.held.add("Demo TV")
        demo = self.hub.catalog("Demo TV")
        asked = []
        ask = demo.request_link
        demo.request_link = lambda item, **kwargs: asked.append(item) or ask(item, **kwargs)
        lanes = self.lanes()
        lanes.request(Target(self.ONE, "live", "One"))
        two = Target(self.TWO, "live", "Two")
        lanes.request_side(two, self.side.append, play=False)
        lanes.request_side(Target(self.ONE, "live", "One"), self.side.append, play=False)
        lanes.request_side(two, self.side.append, play=False)  # the PiP asks again: one ask
        self.runner.release("Demo TV")
        QTest.qWait(30)
        self.assertEqual(self.dropped, [None])  # nothing to ask again: the hub answers them
        self.assertEqual(self.tunes, ["http://127.0.0.1:9/live/1.ts"])
        # The menu's second link waited behind the first (before, it was lost).
        self.assertEqual(
            self.side, ["http://127.0.0.1:9/live/2.ts", "http://127.0.0.1:9/live/1.ts"]
        )
        self.assertEqual(len(asked), 3)

    def test_a_newer_choice_goes_at_once_past_a_tune_held_for_another_providers_setup(self):
        # A tune held for Harbor's refresh kept a newer choice of a ready provider queued
        # until that refresh ended. The newest choice goes now; the held one is never
        # answered, nor asked of its portal.
        seven = with_provider({"id": 7, "cmd": "http://127.0.0.1:9/live/7.ts"}, "Harbor IPTV")
        self.hub.ensure("Demo TV")
        self.hub.ensure("Harbor IPTV")
        self.runner.held.add("Harbor IPTV")
        self.hub.select("Harbor IPTV", force_refresh=True)  # its refresh runs a while
        harbor, asked = self.hub.catalog("Harbor IPTV"), []
        ask = harbor.request_link
        harbor.request_link = lambda item, **kwargs: asked.append(item) or ask(item, **kwargs)
        lanes = self.lanes()
        lanes.request(Target(seven, "live", "Seven"))
        lanes.request(Target(self.ONE, "live", "One"))  # chosen since, of Demo (ready)
        QTest.qWait(30)
        self.assertEqual(self.tunes, ["http://127.0.0.1:9/live/1.ts"])
        self.runner.release("Harbor IPTV")
        QTest.qWait(30)
        self.assertEqual(self.tunes, ["http://127.0.0.1:9/live/1.ts"])
        self.assertEqual(asked, [])
        self.assertEqual(self.hub_failures, [])

    def test_links_held_for_a_failed_setup_are_dropped_with_it(self):
        self.runner.held.add("Demo TV")
        setups = []

        def failing_setup(manager, force):
            setups.append(manager._pinned_provider)
            return FakeWorker(fail="Portal down")

        self.factory.provider_setup = failing_setup
        lanes = self.lanes()
        lanes.request(Target(self.ONE, "live", "One"))
        lanes.request_side(Target(self.TWO, "live", "Two"), self.side.append, play=False)
        lanes.request_side(Target(self.ONE, "live", "One"), self.side.append, play=False)
        self.runner.release("Demo TV")
        QTest.qWait(30)
        self.assertEqual([t.title for t in self.dropped if t is not None], ["One"])
        self.assertEqual(self.side_lost, [True])  # the shell says so, once
        self.assertEqual(self.tunes, [])  # the shell says why (the provider failed)
        self.assertEqual(self.side, [])
        self.assertEqual([rid for rid, _ in self.hub_failures], [1, 2])  # nothing more asked
        self.assertEqual(setups, ["Demo TV"])  # and no sign-in again


if __name__ == "__main__":
    unittest.main()
