from datetime import datetime, timezone
import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from job_fixtures import ImmediateRunner

from services.catalog import (
    ALL_GROUP_ID,
    SECTION_UNAVAILABLE_MESSAGE,
    UNEXPECTED_ANSWER_MESSAGE,
    CatalogService,
    Group,
)
from services.jobs import JobRunner


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


class ShowingWorker(FakeWorker):
    """A guide load that can say its saved guide is up before it finishes."""

    shown = Signal()


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

    other_identity = False  # the saved content is for other settings of the provider

    def content_expired(self, ct):
        return ct in self.stale and ct in self.current_provider_content and not self.other_identity

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
        ssl_verify=True,
        channel_epg=True,
        save_config=lambda: None,
    )
    providers = FakeProviders(provider)
    epg = SimpleNamespace(get_programs_for_channel=lambda channel: [])
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

    def test_sections_the_provider_lacks_fail_without_loading(self):
        service, providers, factory, _ = make_service(
            {"name": "M", "type": "M3UPLAYLIST", "url": "/tmp/a.m3u"}
        )
        failed, ready = collect(service.sectionFailed), collect(service.sectionReady)
        service.request_section("movies")
        service.request_section("series", force=True)
        self.pump()
        self.assertEqual(
            failed,
            [("movies", SECTION_UNAVAILABLE_MESSAGE), ("series", SECTION_UNAVAILABLE_MESSAGE)],
        )
        self.assertEqual(ready, [])
        self.assertEqual(factory.calls, [])
        self.assertNotIn("vod", providers.current_provider_content)

    def test_provider_setup_then_epg(self):
        service, _, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        ready, epg = collect(service.providerReady), collect(service.epgReady)
        service.select_provider()
        self.assertEqual(ready, [("X",)])
        self.assertEqual(len(epg), 1)
        self.assertTrue(service.epg_ready())

    def test_a_saved_guide_shown_while_a_fresh_one_loads_reads_at_once(self):
        runner, factory = ManualRunner(), FakeFactory()
        service, _, _, _ = make_service(
            {"name": "X", "type": "XTREAM", "url": "h:1"}, factory, runner
        )
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        loading = ShowingWorker({})
        factory.epg = lambda manager, force: loading
        ready = collect(service.epgReady)
        service.select_provider()
        runner.complete(0)  # the provider is ready: its guide loads
        self.assertFalse(service.epg_ready())
        loading.shown.emit()  # the saved guide is up; the download goes on
        self.assertTrue(service.epg_ready())
        self.assertEqual(len(ready), 1)
        runner.complete(1)  # the fresh guide is in
        self.assertEqual(len(ready), 2)

    def test_a_saved_guide_shown_after_its_release_is_not_read(self):
        runner, factory = ManualRunner(), FakeFactory()
        service, _, _, epg = make_service(
            {"name": "X", "type": "XTREAM", "url": "h:1"}, factory, runner
        )
        epg.release = lambda: None
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        loading = ShowingWorker({})
        factory.epg = lambda manager, force: loading
        service.select_provider()
        runner.complete(0)
        service.release_epg()  # no longer in use
        loading.shown.emit()
        self.assertFalse(service.epg_ready())

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
        self.assertEqual(service.items("movies", "5"), [{"id": 9, "name": "F", "_provider": "S"}])
        self.assertEqual(service.groups("movies"), [Group("movies", "5", "Films", 1)])
        kwargs = factory.calls[-1][2]
        # 2.7.1: over HTTPS first (HTTP once the portal's sign-in found it answers only so)
        self.assertEqual(kwargs["url"], "https://portal/server/load.php")
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
        epg.get_programs_for_channel = lambda c: [
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

    def test_the_programme_on_now_is_the_grids_when_programmes_overlap(self):
        # A placeholder over a day long, listed first, starts with a real programme: the
        # info line (one programme asked) shows what the guide's grid shows, not it.
        service, _, factory, epg = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})

        def raw(start, stop, title):
            return {"@start": start, "@stop": stop, "title": {"__text": title}}

        epg.get_programs_for_channel = lambda c: [
            raw("20261018211500 +0000", "20261019235950 +0000", "Placeholder"),
            raw("20261018211500 +0000", "20261018230000 +0000", "Late Film"),
            raw("20261018230000 +0000", "20261019003000 +0000", "Comedy Night"),
        ]
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        service.select_provider()
        now = datetime(2026, 10, 18, 23, 29, tzinfo=timezone.utc)
        on_now = service.programmes({"id": 1}, now, limit=1)
        self.assertEqual([p.title for p in on_now], ["Comedy Night"])

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

    def test_loading_provider_names_the_setup_that_will_report(self):
        runner = ManualRunner()
        provider = {"name": "X", "type": "XTREAM", "url": "h:1"}
        service, _, factory, _ = make_service(provider, runner=runner)
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        self.assertIsNone(service.loading_provider())
        service.select_provider()  # the selected provider, as at startup
        self.assertEqual(service.loading_provider(), "X")
        service.select_provider("B")
        self.assertEqual(service.loading_provider(), "B")  # queued: it is the one to report
        runner.complete(0)
        self.assertEqual(service.loading_provider(), "B")
        runner.complete(1)
        self.assertIsNone(service.loading_provider())

    def test_a_forced_setup_loads_its_guide_with_force(self):
        # The drawer's refresh needs no refresh_epg(): it would be dropped (no ready provider
        # during the setup), and the setup forces the guide itself.
        runner = ManualRunner()
        provider = {"name": "X", "type": "XTREAM", "url": "h:1"}
        service, _, factory, epg_manager = make_service(provider, runner=runner)
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        service.select_provider(force_refresh=True)
        service.refresh_epg(force=True)
        self.assertEqual([call[0] for call in factory.calls], ["provider_setup"])
        runner.complete(0)
        self.assertEqual(factory.calls[-1], ("epg", (epg_manager, True), {}))

    def test_a_queued_forced_refresh_is_never_downgraded_for_the_same_provider(self):
        runner = ManualRunner()
        provider = {"name": "X", "type": "XTREAM", "url": "h:1"}
        service, _, factory, _ = make_service(provider, runner=runner)
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        service.select_provider("A")
        service.select_provider("X", force_refresh=True)  # Settings changed X
        service.select_provider("X")  # the Library asks for X again
        runner.complete(0)
        self.assertEqual(
            factory.calls[-1], ("provider_setup", (service.provider_manager, True), {})
        )

    def test_a_queued_forced_refresh_for_another_provider_is_replaced(self):
        runner = ManualRunner()
        provider = {"name": "X", "type": "XTREAM", "url": "h:1"}
        service, _, factory, _ = make_service(provider, runner=runner)
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        service.select_provider("A")
        service.select_provider("X", force_refresh=True)
        service.select_provider("B")
        runner.complete(0)
        self.assertEqual(
            factory.calls[-1], ("provider_setup", (service.provider_manager, False), {})
        )
        self.assertEqual(service.config.selected_provider_name, "B")

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
        self.assertEqual(service.items("movies", "5"), [{"id": 1, "_provider": "S"}])

    def test_group_result_with_unloaded_section_fails(self):
        # Formerly dropped silently, which left the drawer loading forever.
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


SIGNALS = (
    "providerReady",
    "providerFailed",
    "sectionReady",
    "sectionFailed",
    "groupReady",
    "groupFailed",
    "epgReady",
    "seasonsReady",
    "seasonsFailed",
    "episodesReady",
    "episodesFailed",
    "linkReady",
    "linkFailed",
)


JELLY = {
    "name": "Home server",
    "type": "JELLYFIN",
    "url": "http://server.test:8096",
    "username": "me",
    "token": "T",
    "user_id": "U",
    "device_id": "D",
}
MEDIA_FILMS = {
    "categories": [{"id": "*", "title": "All"}, {"id": "L1", "title": "Movies"}],
    "contents": [{"id": "f1", "name": "Salt Roads", "tv_genre_id": "L1", "cmd": ""}],
    "sorted_channels": {"L1": [0]},
}


class MediaServerCatalogTests(unittest.TestCase):
    """Jellyfin and Emby (2.8): films and series only, asked of the server's workers."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_a_server_has_films_and_series_and_no_channels(self):
        for kind in ("JELLYFIN", "EMBY"):
            service, *_ = make_service({**JELLY, "type": kind})
            self.assertEqual(service.sections(), ["movies", "series"])
            self.assertFalse(service.is_live("movies"))

    def test_its_films_come_from_its_section_worker(self):
        service, providers, factory, _ = make_service(dict(JELLY))
        factory.results["media_section"] = {"result": MEDIA_FILMS}
        ready = collect(service.sectionReady)
        service.request_section("movies")
        QCoreApplication.processEvents()
        self.assertEqual(ready, [("movies",)])
        name, args, _kwargs = factory.calls[-1]
        self.assertEqual((name, args[0]["type"], args[1]), ("media_section", "JELLYFIN", "vod"))
        self.assertEqual([i["name"] for i in service.items("movies", "L1")], ["Salt Roads"])

    def test_a_series_seasons_carry_their_episodes(self):
        service, _providers, factory, _ = make_service(dict(JELLY))
        episodes = [{"id": "e1", "ename": "One", "cmd": ""}]
        seasons = {
            "seasons": [
                {"id": "s1", "name": "Season 1", "season_number": "1", "episodes": episodes}
            ]
        }
        factory.results["media_series"] = {"result": seasons}
        ready = collect(service.seasonsReady)
        series = {"id": "show", "name": "Show"}
        service.request_seasons(series)
        QCoreApplication.processEvents()
        self.assertEqual(ready, [("show",)])
        (season,) = service.seasons(series)
        self.assertEqual(season["number"], "1")
        self.assertEqual([e["ename"] for e in service.episodes(series, season)], ["One"])

    def test_a_titles_stream_is_asked_with_the_settings_choices(self):
        service, _providers, factory, _ = make_service(dict(JELLY))
        service.config.media_4k_versions = "always"
        service.config.media_max_mbps = 8
        service.screen_4k = lambda: True
        factory.results["media_link"] = {
            "result": {"link": "http://server.test:8096/Videos/f1/stream.mkv"}
        }
        links = collect(service.linkReady)
        request = service.request_link({"id": "f1", "cmd": ""}, section="movies")
        QCoreApplication.processEvents()
        self.assertEqual(links, [(request, "http://server.test:8096/Videos/f1/stream.mkv")])
        name, args, kwargs = factory.calls[-1]
        self.assertEqual((name, args[1]), ("media_link", "f1"))
        self.assertEqual(kwargs, {"screen_4k": True, "four_k": "always", "max_bitrate": 8_000_000})


class CatalogShutdownTests(unittest.TestCase):
    """The window is closing: start no more jobs and report nothing."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def stb_service(self):
        runner = ManualRunner()
        provider = {"name": "S", "type": "STB", "url": "http://portal/c/"}
        service, providers, factory, _ = make_service(provider, runner=runner)
        providers.current_provider_content["vod"] = {"categories": [], "contents": {}}
        factory.results["provider_setup"] = {"result": {"name": "S"}}
        factory.results["epg"] = {"result": {}}
        factory.results["stb_list"] = {"result": {"items": [{"id": 1}]}}
        factory.results["link"] = {"result": {"link": "http://stream/1"}}
        seen = []
        for name in SIGNALS:
            getattr(service, name).connect(lambda *_args, n=name: seen.append(n))
        return service, runner, seen

    def test_a_running_setup_finishing_after_shutdown_starts_nothing(self):
        service, runner, seen = self.stb_service()
        service.select_provider()
        service.select_provider("Other")  # queued behind the running setup
        service.shutdown()
        runner.complete(0)
        self.assertEqual(len(runner.jobs), 1)  # neither the queued setup nor an EPG load
        self.assertEqual(seen, [])

    def test_requests_after_shutdown_do_nothing(self):
        service, runner, seen = self.stb_service()
        service.shutdown()
        service.select_provider()
        service.refresh_epg(force=True)
        service.request_section("live")
        service.request_group("movies", "5")
        service.request_seasons({"id": 1})
        service.request_episodes({"id": 1}, {"id": 2})
        service.request_link({"cmd": "ffrt x"}, section="live")
        QTest.qWait(20)
        self.assertEqual(runner.jobs, [])
        self.assertEqual(seen, [])

    def test_running_jobs_report_nothing_and_the_queue_stops(self):
        service, runner, seen = self.stb_service()
        for group in ("5", "6", "7"):  # two run, one waits for a slot
            service.request_group("movies", group)
        link = service.request_link({"cmd": "ffrt x"}, section="live")
        self.assertIsInstance(link, int)
        self.assertEqual(len(runner.jobs), 3)
        service.shutdown()
        for index in range(3):
            runner.complete(index)
        QTest.qWait(20)
        self.assertEqual(len(runner.jobs), 3)  # the waiting group never starts
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
