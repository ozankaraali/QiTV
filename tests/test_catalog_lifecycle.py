"""Catalog request lifecycle: provider switches, EPG serialization, de-duplication, job cap."""

import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
import orjson

from services.catalog import (
    MAX_CATALOG_JOBS,
    PROVIDER_LOADING_MESSAGE,
    UNEXPECTED_ANSWER_MESSAGE,
    CatalogService,
)
from services.config_manager import ConfigManager
from services.navigation import NavModel
from services.provider_manager import ProviderManager
from services.workers import ProviderSetupWorker
from tests.test_catalog_service import (
    XTREAM_PAYLOAD,
    FakeFactory,
    FakeWorker,
    ManualRunner,
    collect,
    make_service,
)

A_LIVE = {
    "categories": [{"id": "1", "title": "A-News"}],
    "contents": [{"id": 1, "name": "A channel", "cmd": "http://a/1.ts", "tv_genre_id": "1"}],
    "sorted_channels": {"1": [0]},
}
B_LIVE = {
    "categories": [{"id": "2", "title": "B-News"}],
    "contents": [{"id": 1, "name": "B channel", "cmd": "http://b/1.ts", "tv_genre_id": "2"}],
    "sorted_channels": {"2": [0]},
}


class _Response:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _fake_portal(url, **kwargs):
    if "action=handshake" in url:
        return _Response({"js": {"token": "T-" + url.split("//")[1].split("/")[0]}})
    return _Response({"js": {"id": 1, "name": "Box"}})


class RealSetupFactory(FakeFactory):
    """Real ProviderSetupWorker against a real ProviderManager; catalog workers are fakes."""

    def provider_setup(self, provider_manager, force_refresh):
        self.calls.append(("provider_setup", (provider_manager, force_refresh), {}))
        return ProviderSetupWorker(provider_manager, force_refresh)

    def xtream(self, base_url, *args):
        self.calls.append(("xtream", (base_url, *args), {}))
        return FakeWorker(A_LIVE if "a.example" in base_url else B_LIVE)


def job_indexes(runner, kind):
    return [i for i, job in enumerate(runner.jobs) if job[3].kind == kind]


class ProviderSwitchRaceTests(unittest.TestCase):
    """Finding C2: requests between select_provider() and providerReady."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, providers):
        directory = tempfile.TemporaryDirectory(prefix="qitv-switch-")
        self.addCleanup(directory.cleanup)
        env = mock.patch.dict(os.environ, {"QITV_CONFIG_DIR": directory.name})
        env.start()
        self.addCleanup(env.stop)
        portal = mock.patch("services.provider_manager.requests.get", side_effect=_fake_portal)
        portal.start()
        self.addCleanup(portal.stop)
        config = ConfigManager()
        manager = ProviderManager(config)
        self.addCleanup(manager._cache_writer.shutdown, wait=True)
        manager.providers = providers
        config.selected_provider_name = providers[0]["name"]
        runner, factory = ManualRunner(), RealSetupFactory()
        factory.results["epg"] = {"result": {}}
        service = CatalogService(config, manager, None, runner=runner, factory=factory)
        # Provider A is ready (its setup and EPG load have finished).
        service.select_provider()
        runner.complete(job_indexes(runner, "provider")[-1])
        runner.complete(job_indexes(runner, "epg")[-1])
        return service, manager, runner, factory

    def disk_cache(self, manager, name):
        manager._cache_writer.submit(lambda: None).result()
        path = manager._provider_cache_name(name)
        if not os.path.exists(path):
            return {}
        with open(path, "rb") as f:
            return orjson.loads(f.read())

    def test_section_requested_during_setup_fails_fast_and_never_stores_old_catalog(self):
        a = {"type": "XTREAM", "name": "A", "url": "http://a.example:80", "username": "ua"}
        b = {"type": "XTREAM", "name": "B", "url": "http://b.example:80", "username": "ub"}
        service, manager, runner, factory = self.make([a, b])
        failed, ready = collect(service.sectionFailed), collect(service.sectionReady)
        service.select_provider("B")
        service.request_section("live")  # the user opens Live TV before the setup finishes
        QCoreApplication.processEvents()
        self.assertEqual(failed, [("live", PROVIDER_LOADING_MESSAGE)])
        self.assertNotIn("xtream", [c[0] for c in factory.calls])
        runner.complete(job_indexes(runner, "provider")[-1])
        self.assertEqual(manager.current_provider["name"], "B")
        self.assertNotIn("itv", manager.current_provider_content)
        service.request_section("live")  # what the shell does on providerReady
        self.assertEqual(factory.calls[-1][1][0], "http://b.example:80")
        runner.complete(job_indexes(runner, "section")[-1])
        self.assertEqual(ready, [("live",)])
        self.assertEqual([g.title for g in service.groups("live")], ["B-News"])
        names = [c["name"] for c in self.disk_cache(manager, "B")["itv"]["contents"]]
        self.assertEqual(names, ["B channel"])

    def test_section_issued_before_the_switch_is_dropped(self):
        a = {"type": "XTREAM", "name": "A", "url": "http://a.example:80", "username": "ua"}
        b = {"type": "XTREAM", "name": "B", "url": "http://b.example:80", "username": "ub"}
        service, manager, runner, factory = self.make([a, b])
        ready = collect(service.sectionReady)
        service.request_section("live")  # A's catalog download is in flight
        service.select_provider("B")
        runner.complete(job_indexes(runner, "provider")[-1])  # B is ready
        runner.complete(job_indexes(runner, "section")[-1])  # A's result arrives late
        self.assertEqual(ready, [])
        self.assertNotIn("itv", manager.current_provider_content)
        self.assertNotIn("itv", self.disk_cache(manager, "B"))

    def test_stb_requests_never_send_the_old_portal_credentials(self):
        a = {"type": "STB", "name": "A", "url": "http://a.example/c/", "mac": "00:1A:79:00:00:0A"}
        b = {"type": "STB", "name": "B", "url": "http://b.example/c/", "mac": "00:1A:79:00:00:0B"}
        service, manager, runner, factory = self.make([a, b])
        manager.current_provider_content["vod"] = {"categories": [], "contents": {}}
        manager.current_provider_content["series"] = {}
        failures = [
            collect(signal)
            for signal in (
                service.sectionFailed,
                service.groupFailed,
                service.seasonsFailed,
                service.episodesFailed,
                service.linkFailed,
            )
        ]
        calls_before = len(factory.calls)
        service.select_provider("B")
        series, season = {"id": 9, "name": "Show"}, {"id": "9:1"}
        service.request_section("live")
        service.request_group("movies", "5")
        service.request_seasons(series)
        service.request_episodes(series, season)
        link_id = service.request_link({"cmd": "ffmpeg http://localhost/ch/1_"}, section="live")
        QCoreApplication.processEvents()
        self.assertEqual(
            failures,
            [
                [("live", PROVIDER_LOADING_MESSAGE)],
                [("movies", "5", PROVIDER_LOADING_MESSAGE)],
                [("9", PROVIDER_LOADING_MESSAGE)],
                [("9", "9:1", PROVIDER_LOADING_MESSAGE)],
                [(link_id, PROVIDER_LOADING_MESSAGE)],
            ],
        )
        # Nothing but B's setup was built while the switch was under way.
        self.assertEqual([c[0] for c in factory.calls[calls_before:]], ["provider_setup"])
        runner.complete(job_indexes(runner, "provider")[-1])
        service.request_section("live")
        service.request_link({"cmd": "ffmpeg http://localhost/ch/1_"}, section="live")
        for name, args, kwargs in factory.calls[calls_before + 1 :]:
            headers = args[1] if name == "stb_categories" else kwargs.get("headers")
            if headers is None:
                continue
            self.assertEqual(headers["Host"], "b.example", name)
            self.assertIn("mac=00:1A:79:00:00:0B", headers["Cookie"], name)
            self.assertNotIn("a.example", repr((args, kwargs)), name)
        built = [c[0] for c in factory.calls[calls_before + 1 :]]
        self.assertIn("stb_categories", built)
        self.assertIn("link", built)


class CatalogQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def pump(self):
        QCoreApplication.processEvents()

    def test_setup_ready_starts_a_new_generation(self):
        runner = ManualRunner()
        service, _, factory, _ = make_service({"name": "X", "type": "XTREAM"}, runner=runner)
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        service.select_provider()
        before = service._generation
        runner.complete(0)
        self.assertGreater(service._generation, before)
        self.assertGreater(runner.jobs[1][3].generation, runner.jobs[0][3].generation)

    def test_stale_section_loads_are_deduplicated_in_flight(self):
        runner = ManualRunner()
        service, providers, factory, _ = make_service(
            {"name": "X", "type": "XTREAM", "url": "h:1"}, runner=runner
        )
        providers.current_provider_content["itv"] = XTREAM_PAYLOAD
        providers.stale.add("itv")
        factory.results["xtream"] = {"result": XTREAM_PAYLOAD}
        nav = NavModel(service, SimpleNamespace(favorites=[], watch_history=[]))
        ready = collect(service.sectionReady)
        for _ in range(3):
            nav.reset_to("live", "10")  # the shell opens the drawer three times
        service.request_section("live", force=True)
        self.assertEqual([c[0] for c in factory.calls], ["xtream"])
        runner.complete(0)
        self.assertEqual(ready, [("live",)])
        service.request_section("live", force=True)  # finished: a new request loads again
        self.assertEqual([c[0] for c in factory.calls], ["xtream", "xtream"])

    def test_failed_refresh_of_a_stale_section_keeps_the_saved_channels(self):
        # Finding I2, end to end: the stale cache stays visible and autotune can use it.
        service, providers, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h"})
        providers.current_provider_content["itv"] = XTREAM_PAYLOAD
        providers.stale.add("itv")
        factory.results["xtream"] = {"fail": "HTTPConnectionPool: Max retries exceeded"}
        nav = NavModel(service, SimpleNamespace(favorites=[], watch_history=[]))
        notices = collect(nav.notice)
        with self.assertLogs("services.catalog", "INFO"):
            nav.reset_to("live", "10")  # what the shell does on providerReady
        self.assertEqual(notices, [("Couldn't refresh Live TV; showing saved channels",)])
        self.assertEqual(nav.path[-1].key, "10")
        _, (parent, children) = nav.columns()
        self.assertIsNone(nav.load_error(parent))
        self.assertEqual([n.title for n in children], ["One", "Two"])

    def test_content_version_changes_with_the_catalog(self):
        runner = ManualRunner()
        service, providers, factory = self._stb_movies(runner)
        factory.results["provider_setup"] = {"result": {"name": "S"}}
        versions = [service.content_version()]

        def changed():
            versions.append(service.content_version())
            return versions[-1] != versions[-2]

        service.request_group("movies", "1")
        self.assertFalse(changed())  # requested, nothing stored yet
        runner.complete(0)
        self.assertTrue(changed())  # the group's items are stored
        service.request_seasons({"id": 9, "name": "Show"})
        runner.complete(1)
        self.assertTrue(changed())
        service.select_provider()
        self.assertTrue(changed())
        runner.complete(2)  # the setup replaced the content
        self.assertTrue(changed())

    def _stb_movies(self, runner):
        provider = {"name": "S", "type": "STB", "url": "http://portal/c/"}
        service, providers, factory, _ = make_service(provider, runner=runner)
        providers.current_provider_content["vod"] = {"categories": [], "contents": {}}
        factory.results["stb_list"] = {"result": {"items": [{"id": 1}]}}
        return service, providers, factory

    def test_at_most_two_catalog_jobs_run_and_the_newest_waiting_runs_next(self):
        runner = ManualRunner()
        service, _, factory = self._stb_movies(runner)
        for group in "12345":
            service.request_group("movies", group)
        self.assertEqual(MAX_CATALOG_JOBS, 2)
        started = [c[2]["category_id"] for c in factory.calls]
        self.assertEqual(started, ["1", "2"])
        runner.complete(0)
        runner.complete(1)
        started = [c[2]["category_id"] for c in factory.calls]
        self.assertEqual(started, ["1", "2", "5", "4"])
        runner.complete(2)
        runner.complete(3)
        runner.complete(4)
        started = [c[2]["category_id"] for c in factory.calls]
        self.assertEqual(started, ["1", "2", "5", "4", "3"])
        self.assertEqual(service.items("movies", "3"), [{"id": 1}])

    def test_waiting_request_asked_again_is_not_duplicated_and_goes_next(self):
        runner = ManualRunner()
        service, _, factory = self._stb_movies(runner)
        for group in "1234":
            service.request_group("movies", group)
        service.request_group("movies", "3")  # back to group 3: the latest focus
        service.request_group("movies", "1")  # running: nothing new
        runner.complete(0)
        runner.complete(1)
        self.assertEqual([c[2]["category_id"] for c in factory.calls], ["1", "2", "3", "4"])

    def test_failed_jobs_free_their_slot(self):
        runner = ManualRunner()
        service, _, factory = self._stb_movies(runner)
        factory.results["stb_list"] = {"fail": "boom"}
        failed = collect(service.groupFailed)
        for group in "123":
            service.request_group("movies", group)
        runner.complete(0)
        self.assertEqual(failed, [("movies", "1", UNEXPECTED_ANSWER_MESSAGE)])
        self.assertEqual(len(runner.jobs), 3)

    def test_links_and_epg_do_not_wait_for_catalog_jobs(self):
        runner = ManualRunner()
        service, _, factory = self._stb_movies(runner)
        factory.results["provider_setup"] = {"result": {"name": "S"}}
        service.select_provider()
        runner.complete(0)  # ready; the EPG load starts
        for group in "123":
            service.request_group("movies", group)
        service.request_link({"cmd": "c"}, section="live")
        kinds = [job[3].kind for job in runner.jobs]
        self.assertEqual(kinds, ["provider", "epg", "group", "group", "link"])

    def test_provider_switch_drops_waiting_requests(self):
        runner = ManualRunner()
        service, providers, factory = self._stb_movies(runner)
        factory.results["provider_setup"] = {"result": {"name": "S"}}
        for group in "1234":
            service.request_group("movies", group)
        service.select_provider()
        runner.complete(2)  # setup: ready
        runner.complete(0)  # the two old jobs finish and free their slots
        runner.complete(1)
        groups = [c[2]["category_id"] for c in factory.calls if c[0] == "stb_list"]
        self.assertEqual(groups, ["1", "2"])
        self.assertEqual(providers.current_provider_content["vod"]["contents"], {})

    def test_previous_provider_jobs_hold_no_slots_of_the_new_provider(self):
        runner = ManualRunner()
        service, providers, factory = self._stb_movies(runner)
        other = {"name": "T", "type": "STB", "url": "http://other.test/c/"}
        providers.providers.append(other)
        factory.results["provider_setup"] = {
            "result": {"name": "T"},
            "on_run": lambda: setattr(providers, "current_provider", other),
        }
        service.request_group("movies", "1")
        service.request_group("movies", "2")  # the previous provider's crawls fill the cap
        service.select_provider("T")
        runner.complete(2)  # setup: ready (its EPG load starts)
        for group in "345":
            service.request_group("movies", group)

        def started():
            return [c[2]["category_id"] for c in factory.calls if c[0] == "stb_list"]

        self.assertEqual(started(), ["1", "2", "3", "4"])  # the new provider's first two
        runner.complete(0)  # an old job finishing frees no slot of the new provider
        runner.complete(1)
        self.assertEqual(started(), ["1", "2", "3", "4"])
        runner.complete(job_indexes(runner, "group")[2])  # group 3 finishes: 5 runs
        self.assertEqual(started(), ["1", "2", "3", "4", "5"])

    def test_a_forced_refresh_never_runs_more_portal_jobs_than_the_cap(self):
        # The jobs of the provider's previous generation still hit the same portal.
        runner = ManualRunner()
        service, _, factory = self._stb_movies(runner)
        factory.results["provider_setup"] = {"result": {"name": "S"}}
        service.request_group("movies", "1")
        service.request_group("movies", "2")
        service.select_provider(force_refresh=True)  # Settings: the same provider again
        runner.complete(2)  # setup: ready (its EPG load starts)
        for group in "345":
            service.request_group("movies", group)

        def started():
            return [c[2]["category_id"] for c in factory.calls if c[0] == "stb_list"]

        self.assertEqual(started(), ["1", "2"])  # both slots are still taken
        runner.complete(0)  # an old job finishes: its slot goes to the newest request
        self.assertEqual(started(), ["1", "2", "5"])
        runner.complete(1)
        self.assertEqual(started(), ["1", "2", "5", "4"])
        self.assertEqual(service.items("movies", "1"), None)  # the old results are dropped

    def test_reselecting_the_same_provider_keeps_its_old_jobs_in_the_cap(self):
        runner = ManualRunner()
        service, _, factory = self._stb_movies(runner)
        factory.results["provider_setup"] = {"result": {"name": "S"}}
        service.request_group("movies", "1")
        service.request_group("movies", "2")
        service.select_provider("S")  # e.g. the drawer's source row
        runner.complete(2)
        service.request_group("movies", "3")
        self.assertEqual([c[0] for c in factory.calls].count("stb_list"), 2)

    def test_shutdown_cancels_the_running_jobs(self):
        # An XMLTV download or an STB crawl must not hold the window's close (H10).
        runner = ManualRunner()
        runner.cancel_all = mock.Mock()
        service, _, _ = self._stb_movies(runner)
        service.request_group("movies", "1")
        service.shutdown()
        runner.cancel_all.assert_called_once_with()

    def test_is_loading_while_a_setup_runs_or_waits(self):
        runner = ManualRunner()
        service, _, factory = self._stb_movies(runner)
        factory.results["provider_setup"] = {"result": {"name": "S"}}
        self.assertFalse(service.is_loading())
        service.select_provider()
        self.assertTrue(service.is_loading())
        runner.complete(0)  # ready; the EPG load runs
        self.assertFalse(service.is_loading())
        service.select_provider()  # waits behind the EPG load
        self.assertTrue(service.is_loading())
        runner.complete(1)  # EPG done; the setup starts
        self.assertTrue(service.is_loading())
        runner.complete(2)
        self.assertFalse(service.is_loading())

    def test_requests_during_setup_fail_fast(self):
        runner = ManualRunner()
        service, _, factory = self._stb_movies(runner)
        service.select_provider()
        failed = collect(service.groupFailed)
        service.request_group("movies", "1")
        self.pump()
        self.assertEqual(failed, [("movies", "1", PROVIDER_LOADING_MESSAGE)])
        self.assertEqual([c[0] for c in factory.calls], ["provider_setup"])


class EpgSerializationTests(unittest.TestCase):
    """Finding I6/I11: EPG loads never overlap setups or each other; refresh_epg()."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self):
        runner = ManualRunner()
        service, _, factory, _ = make_service(
            {"name": "X", "type": "XTREAM", "url": "h:1"}, runner=runner
        )
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        return service, runner, factory

    def kinds(self, runner):
        return [job[3].kind for job in runner.jobs]

    def test_setup_waits_for_a_running_epg_load(self):
        service, runner, _ = self.make()
        epg = collect(service.epgReady)
        service.select_provider()
        runner.complete(0)  # ready: the EPG load starts
        service.select_provider()  # switch while the old provider's EPG still loads
        self.assertEqual(self.kinds(runner), ["provider", "epg"])
        runner.complete(1)  # the old EPG finishes: dropped, then the setup starts
        self.assertEqual(epg, [])
        self.assertFalse(service.epg_ready())
        self.assertEqual(self.kinds(runner), ["provider", "epg", "provider"])
        runner.complete(2)
        self.assertEqual(self.kinds(runner), ["provider", "epg", "provider", "epg"])
        runner.complete(3)
        self.assertEqual(len(epg), 1)

    def test_refresh_epg_reloads_and_never_overlaps(self):
        service, runner, factory = self.make()
        epg = collect(service.epgReady)
        service.select_provider()
        runner.complete(0)
        service.refresh_epg()  # the first load is still running: queued behind it
        service.refresh_epg(force=True)
        self.assertEqual(self.kinds(runner), ["provider", "epg"])
        runner.complete(1)
        self.assertEqual(self.kinds(runner), ["provider", "epg", "epg"])
        self.assertEqual(factory.calls[-1][1][1], True)  # the queued loads merged: forced
        runner.complete(2)
        self.assertEqual(len(epg), 2)
        service.refresh_epg()
        self.assertEqual(self.kinds(runner), ["provider", "epg", "epg", "epg"])
        self.assertEqual(factory.calls[-1][1][1], False)
        runner.complete(3)
        self.assertEqual(len(epg), 3)

    def test_refresh_epg_is_ignored_before_ready_and_during_setup(self):
        service, runner, _ = self.make()
        service.refresh_epg()
        self.assertEqual(runner.jobs, [])
        service.select_provider()
        service.refresh_epg()
        self.assertEqual(self.kinds(runner), ["provider"])
        runner.complete(0)
        self.assertEqual(self.kinds(runner), ["provider", "epg"])

    def test_refresh_epg_after_shutdown_starts_nothing_for_a_ready_provider(self):
        # With a ready provider refresh_epg reaches _request_epg: its closed check holds.
        service, runner, _ = self.make()
        service.select_provider()
        runner.complete(0)  # ready: its guide loads
        runner.complete(1)
        service.shutdown()
        service.refresh_epg(force=True)
        self.assertEqual(self.kinds(runner), ["provider", "epg"])

    def test_queued_epg_refresh_is_dropped_by_a_switch(self):
        service, runner, _ = self.make()
        service.select_provider()
        runner.complete(0)
        service.refresh_epg()  # queued behind the running load
        service.select_provider()
        runner.complete(1)
        self.assertEqual(self.kinds(runner), ["provider", "epg", "provider"])


if __name__ == "__main__":
    unittest.main()
