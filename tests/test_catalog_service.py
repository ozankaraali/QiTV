from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.catalog import ALL_GROUP_ID, UNEXPECTED_ANSWER_MESSAGE, CatalogService, Group
from services.jobs import ImmediateRunner, JobRunner


class FakeWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, result=None, fail=None, on_run=None):
        super().__init__()
        self.result, self.fail, self.on_run = result, fail, on_run

    def run(self):
        if self.on_run:
            self.on_run()
        if self.fail:
            self.error.emit(self.fail)
        else:
            self.finished.emit(self.result)


class FakeFactory:
    """Records calls; returns FakeWorkers with canned results."""

    def __init__(self):
        self.calls = []
        self.results = {}

    def _worker(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        spec = self.results.get(name, {})
        return FakeWorker(spec.get("result"), spec.get("fail"), spec.get("on_run"))

    def __getattr__(self, name):
        return lambda *args, **kwargs: self._worker(name, *args, **kwargs)


class FakeProviders:
    def __init__(self, provider):
        self.providers = [provider]
        self.current_provider = provider
        self.current_provider_content = {}
        self.headers = {"Authorization": "Bearer T"}
        self.stale = set()
        self.saved = 0

    def is_content_stale(self, ct):
        return ct in self.stale

    def mark_content_fresh(self, ct):
        self.stale.discard(ct)

    def save_provider(self):
        self.saved += 1


class ManualRunner:
    """Holds jobs until the test completes them, to model slow workers."""

    def __init__(self):
        self.jobs = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.jobs.append((worker, on_finished, on_error, context))
        return worker

    def complete(self, index):
        worker, on_finished, on_error, context = self.jobs[index]
        outcome = []
        worker.finished.connect(lambda payload: outcome.append(("ok", payload)))
        worker.error.connect(lambda message: outcome.append(("err", message)))
        worker.run()
        kind, value = outcome[0]
        (on_finished if kind == "ok" else on_error)(context, value)


def make_service(provider, factory=None, runner=None):
    config = SimpleNamespace(
        selected_provider_name=provider["name"],
        prefer_https=False,
        ssl_verify=True,
        channel_epg=True,
        save_config=lambda: None,
    )
    providers = FakeProviders(provider)
    epg = SimpleNamespace(get_programs_for_channel=lambda channel, start, limit: [])
    factory = factory or FakeFactory()
    service = CatalogService(
        config, providers, epg, runner=runner or ImmediateRunner(), factory=factory
    )
    return service, providers, factory, epg


def collect(signal):
    seen = []
    signal.connect(lambda *args: seen.append(args))
    return seen


XTREAM_PAYLOAD = {
    "categories": [
        {"id": "*", "title": "All"},
        {"id": "10", "title": "News"},
        {"id": "11", "title": "Empty"},
    ],
    "contents": [
        {"id": 1, "number": "1", "name": "One", "tv_genre_id": "10", "cmd": "http://x/1.ts"},
        {"id": 2, "number": "2", "name": "Two", "tv_genre_id": "10", "cmd": "http://x/2.ts"},
    ],
    "sorted_channels": {"10": [0, 1], "11": []},
    "max_connections": 2,
}


class CatalogServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def pump(self):
        QCoreApplication.processEvents()

    def test_sections_by_provider_type(self):
        service, *_ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        self.assertEqual(service.sections(), ["live", "movies", "series"])
        service, *_ = make_service({"name": "M", "type": "M3UPLAYLIST", "url": "/tmp/a.m3u"})
        self.assertEqual(service.sections(), ["live"])

    def test_provider_setup_then_epg(self):
        service, _, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        ready, epg = collect(service.providerReady), collect(service.epgReady)
        service.select_provider()
        self.assertEqual(ready, [("X",)])
        self.assertEqual(len(epg), 1)
        self.assertTrue(service.epg_ready())

    def test_provider_failure_is_reported(self):
        service, _, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        factory.results["provider_setup"] = {"fail": "handshake refused"}
        failed = collect(service.providerFailed)
        service.select_provider()
        self.assertEqual(failed, [("X", UNEXPECTED_ANSWER_MESSAGE)])  # raw texts never leave

    def test_xtream_section_groups_and_items(self):
        provider = {"name": "X", "type": "XTREAM", "url": "h:1", "username": "u", "password": "p"}
        service, providers, factory, _ = make_service(provider)
        factory.results["xtream"] = {"result": XTREAM_PAYLOAD}
        ready = collect(service.sectionReady)
        service.request_section("live")
        self.assertEqual(ready, [("live",)])
        self.assertIs(providers.current_provider_content["itv"], XTREAM_PAYLOAD)
        self.assertEqual(providers.saved, 1)
        groups = service.groups("live")
        self.assertEqual(
            groups, [Group("live", "*", "All", 2), Group("live", "10", "News", 2)]
        )  # the empty group is hidden
        self.assertEqual([i["name"] for i in service.items("live", "10")], ["One", "Two"])
        self.assertEqual(len(service.items("live", ALL_GROUP_ID)), 2)
        self.assertEqual(service.max_connections(), 2)
        name, args, kwargs = factory.calls[-1]
        self.assertEqual(name, "xtream")
        self.assertEqual(args[:4], ("h:1", "u", "p", "itv"))

    def test_cached_fresh_section_does_not_reload(self):
        service, providers, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        providers.current_provider_content["itv"] = XTREAM_PAYLOAD
        ready = collect(service.sectionReady)
        service.request_section("live")
        self.assertEqual(ready, [])  # deferred to the event loop
        self.pump()
        self.assertEqual(ready, [("live",)])
        self.assertEqual(factory.calls, [])

    def test_stale_section_reloads(self):
        service, providers, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        providers.current_provider_content["itv"] = XTREAM_PAYLOAD
        providers.stale.add("itv")
        factory.results["xtream"] = {"result": XTREAM_PAYLOAD}
        service.request_section("live")
        self.assertEqual(factory.calls[-1][0], "xtream")

    def test_legacy_flat_m3u_list_is_one_group(self):
        service, providers, _, _ = make_service({"name": "M", "type": "M3UPLAYLIST", "url": "/a"})
        providers.current_provider_content["itv"] = [{"name": "A"}, {"name": "B"}]
        self.assertEqual(service.groups("live"), [Group("live", "*", "All", 2)])
        self.assertEqual(len(service.items("live", "*")), 2)

    def test_m3u_stream_is_a_single_channel(self):
        service, providers, _, _ = make_service(
            {"name": "S", "type": "M3USTREAM", "url": "http://s"}
        )
        service.request_section("live")
        self.pump()
        self.assertEqual(service.items("live", "*")[0]["cmd"], "http://s")

    def test_stb_on_demand_groups(self):
        provider = {"name": "S", "type": "STB", "url": "http://portal/c/"}
        service, providers, factory, _ = make_service(provider)
        providers.current_provider_content["vod"] = {
            "categories": [{"id": "*", "title": "All"}, {"id": "5", "title": "Films"}],
            "contents": {},
        }
        self.assertEqual(service.groups("movies"), [Group("movies", "5", "Films", None)])
        self.assertIsNone(service.items("movies", "5"))
        factory.results["stb_list"] = {
            "result": {"category_id": "5", "items": [{"id": 9, "name": "F"}]}
        }
        ready = collect(service.groupReady)
        service.request_group("movies", "5")
        self.assertEqual(ready, [("movies", "5")])
        self.assertEqual(service.items("movies", "5"), [{"id": 9, "name": "F"}])
        self.assertEqual(service.groups("movies"), [Group("movies", "5", "Films", 1)])
        kwargs = factory.calls[-1][2]
        self.assertEqual(kwargs["url"], "http://portal/server/load.php")
        self.assertEqual(kwargs["category_id"], "5")

    def test_results_after_provider_switch_are_dropped(self):
        provider = {"name": "X", "type": "XTREAM", "url": "h:1"}
        service, providers, factory, _ = make_service(provider)
        # The section job "finishes" only after a provider switch has happened.
        factory.results["xtream"] = {
            "result": XTREAM_PAYLOAD,
            "on_run": lambda: service.select_provider(),
        }
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        ready = collect(service.sectionReady)
        service.request_section("live")
        self.assertEqual(ready, [])
        self.assertNotIn("itv", providers.current_provider_content)

    def test_programmes_need_ready_epg(self):
        service, _, factory, epg = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        epg.get_programs_for_channel = lambda c, s, l: [
            {
                "@start": "20261015200000 +0000",
                "@stop": "20261015210000 +0000",
                "title": {"__text": "On"},
            }
        ]
        now = datetime(2026, 10, 15, 20, 30, tzinfo=timezone.utc)
        self.assertEqual(service.programmes({"id": 1}, now), [])
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        service.select_provider()
        self.assertEqual([p.title for p in service.programmes({"id": 1}, now)], ["On"])

    def test_is_live(self):
        service, *_ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        self.assertTrue(service.is_live("live"))
        self.assertFalse(service.is_live("movies"))
        service, *_ = make_service({"name": "M", "type": "M3UPLAYLIST", "url": "/a"})
        self.assertIsNone(service.is_live("live"))

    def test_real_runner_delivers_provider_epg_and_section(self):
        for _ in range(20):
            provider = {"name": "X", "type": "XTREAM", "url": "h:1"}
            factory = FakeFactory()
            factory.results["provider_setup"] = {"result": {"name": "X"}}
            factory.results["epg"] = {"result": {}}
            factory.results["xtream"] = {"result": XTREAM_PAYLOAD}
            service, *_ = make_service(provider, factory, runner=JobRunner())
            ready, epg, sections = (
                collect(service.providerReady),
                collect(service.epgReady),
                collect(service.sectionReady),
            )
            # Requests made while the setup runs fail fast; the shell asks once it is ready.
            service.providerReady.connect(lambda _name: service.request_section("live"))
            service.select_provider()
            for _ in range(300):
                if ready and epg and sections:
                    break
                QTest.qWait(10)
            self.assertEqual((ready, len(epg), sections), ([("X",)], 1, [("live",)]))

    def test_overlapping_setups_are_serialized_and_only_latest_reports(self):
        runner = ManualRunner()
        provider = {"name": "X", "type": "XTREAM", "url": "h:1"}
        service, _, factory, _ = make_service(provider, runner=runner)
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        ready = collect(service.providerReady)
        service.select_provider("A")
        service.select_provider("B")
        service.select_provider("C", force_refresh=True)
        self.assertEqual(len(runner.jobs), 1)  # later requests wait for the running setup
        runner.complete(0)
        self.assertEqual(ready, [])  # superseded
        self.assertEqual(len(runner.jobs), 2)  # only the latest request started
        self.assertEqual(
            factory.calls[-1], ("provider_setup", (service.provider_manager, True), {})
        )
        self.assertEqual(service.config.selected_provider_name, "C")
        runner.complete(1)
        self.assertEqual(ready, [("X",)])

    def test_setup_failure_names_the_requested_provider(self):
        service, _, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        factory.results["provider_setup"] = {"fail": "nope"}
        failed = collect(service.providerFailed)
        service.select_provider("Other")
        self.assertEqual(failed, [("Other", UNEXPECTED_ANSWER_MESSAGE)])

    def test_group_result_after_switch_is_dropped(self):
        runner = ManualRunner()
        provider = {"name": "S", "type": "STB", "url": "http://portal/c/"}
        service, providers, factory, _ = make_service(provider, runner=runner)
        content = {"categories": [{"id": "5", "title": "Films"}], "contents": {}}
        providers.current_provider_content["vod"] = content
        factory.results["stb_list"] = {"result": {"items": [{"id": 9}]}}
        ready = collect(service.groupReady)
        service.request_group("movies", "5")
        service.select_provider()
        runner.complete(0)
        self.assertEqual(ready, [])
        self.assertEqual(content["contents"], {})

    def test_deferred_cached_emit_is_dropped_after_switch(self):
        service, providers, _, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        providers.current_provider_content["itv"] = XTREAM_PAYLOAD
        ready = collect(service.sectionReady)
        service.request_section("live")
        service.select_provider()
        self.pump()
        self.assertEqual(ready, [])

    def test_identical_group_request_is_not_duplicated(self):
        runner = ManualRunner()
        provider = {"name": "S", "type": "STB", "url": "http://portal/c/"}
        service, providers, factory, _ = make_service(provider, runner=runner)
        providers.current_provider_content["vod"] = {"categories": [], "contents": {}}
        factory.results["stb_list"] = {"result": {"items": [{"id": 1}]}}
        service.request_group("movies", "5")
        service.request_group("movies", "5")
        self.assertEqual(len(runner.jobs), 1)
        runner.complete(0)
        self.assertEqual(service.items("movies", "5"), [{"id": 1}])

    def test_group_result_with_unloaded_section_fails(self):
        # Formerly dropped silently, which left the drawer loading forever (finding m1).
        runner = ManualRunner()
        provider = {"name": "S", "type": "STB", "url": "http://portal/c/"}
        service, providers, factory, _ = make_service(provider, runner=runner)
        factory.results["stb_list"] = {"result": {"items": []}}
        ready, failed = collect(service.groupReady), collect(service.groupFailed)
        service.request_group("movies", "5")
        with self.assertLogs("services.catalog", "WARNING"):
            runner.complete(0)
        self.assertEqual(ready, [])
        self.assertEqual(failed, [("movies", "5", "This group isn't available")])

    def test_non_stb_group_without_items_fails_without_a_request(self):
        service, providers, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h"})
        providers.current_provider_content["vod"] = {"categories": [], "contents": {}}
        failed = collect(service.groupFailed)
        service.request_group("movies", "5")
        self.pump()
        self.assertEqual(failed, [("movies", "5", "This group isn't available")])
        self.assertEqual(factory.calls, [])


if __name__ == "__main__":
    unittest.main()
