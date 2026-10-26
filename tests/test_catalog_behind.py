"""2.8: a provider's film and series lists load behind what the Library shows.

A saved list past its six hours shows at once and loads again behind it; a provider that is
ready loads its lists ahead, before the Library opens. Xtream panels and Jellyfin/Emby servers
(whole lists); a portal's (STB: its genres load one by one) and the live list load as before.
"""

import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from services import catalog as catalog_module
from tests.test_catalog_service import ManualRunner, collect, make_service

FILMS = {
    "categories": [{"id": "*", "title": "All"}, {"id": "1", "title": "Drama"}],
    "contents": [{"id": 1, "name": "Old Film", "tv_genre_id": "1", "cmd": "http://x/1.mp4"}],
}
NEWER = {
    "categories": [{"id": "*", "title": "All"}, {"id": "1", "title": "Drama"}],
    "contents": [
        {"id": 1, "name": "Old Film", "tv_genre_id": "1", "cmd": "http://x/1.mp4"},
        {"id": 2, "name": "New Film", "tv_genre_id": "1", "cmd": "http://x/2.mp4"},
    ],
}


def pump():
    QCoreApplication.processEvents()


class ListsBehindTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def service(self, ptype="XTREAM"):
        self.runner = ManualRunner()
        service, providers, factory, _ = make_service(
            {"name": "X", "type": ptype, "url": "h:1"}, runner=self.runner
        )
        self.addCleanup(service.shutdown)
        return service, providers, factory

    def test_a_stale_film_list_shows_at_once_and_loads_again_behind(self):
        service, providers, factory = self.service()
        providers.current_provider_content["vod"] = FILMS
        providers.stale.add("vod")
        factory.results["xtream"] = {"result": NEWER}
        ready = collect(service.sectionReady)
        service.request_section("movies")
        pump()
        self.assertEqual(ready, [("movies",)])  # the saved list, before anything loads
        self.assertEqual([c[0] for c in factory.calls], ["xtream"])
        self.runner.complete(0)
        self.assertEqual(ready, [("movies",), ("movies",)])  # the new list, in its place
        self.assertEqual(len(providers.current_provider_content["vod"]["contents"]), 2)
        self.assertNotIn("vod", providers.stale)

    def test_a_failed_load_behind_keeps_the_saved_list_quietly(self):
        service, providers, factory = self.service()
        providers.current_provider_content["vod"] = FILMS
        providers.stale.add("vod")
        factory.results["xtream"] = {"fail": "HTTPConnectionPool: Max retries exceeded"}
        failed = collect(service.sectionFailed)
        service.request_section("movies")
        pump()
        with self.assertLogs("services.catalog", "INFO"):
            self.runner.complete(0)
        self.assertEqual(failed, [])
        self.assertIs(providers.current_provider_content["vod"], FILMS)

    def test_a_media_server_s_lists_load_behind_too(self):
        service, providers, factory = self.service("EMBY")
        providers.current_provider_content["series"] = FILMS
        providers.stale.add("series")
        ready = collect(service.sectionReady)
        service.request_section("series")
        pump()
        self.assertEqual(ready, [("series",)])
        self.assertEqual([c[0] for c in factory.calls], ["media_section"])

    def test_refresh_loads_a_panel_s_lists_again_behind(self):  # 2.8: the Library's
        service, providers, factory = self.service()
        providers.current_provider_content["vod"] = FILMS  # fresh: still asked again
        factory.results["xtream"] = {"result": NEWER}
        ready = collect(service.sectionReady)
        self.assertTrue(service.refresh_section("movies"))
        pump()
        self.assertEqual([c[0] for c in factory.calls], ["xtream"])
        self.assertIs(providers.current_provider_content["vod"], FILMS)  # shown meanwhile
        self.runner.complete(0)
        self.assertEqual(ready, [("movies",)])
        self.assertEqual(len(providers.current_provider_content["vod"]["contents"]), 2)
        service.refresh_section("movies")
        self.assertTrue(service.refresh_section("movies"))  # asked again while it loads: one
        pump()
        self.assertEqual(len(factory.calls), 2)

    def test_a_refresh_during_a_setup_loads_once_it_is_ready(self):  # 2.8 (owner)
        # Refresh pressed while the provider sets up waits for the setup, then loads.
        service, providers, factory = self.service()
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        providers.current_provider_content["vod"] = FILMS  # fresh: only Refresh loads it
        service.select_provider()
        self.assertTrue(service.refresh_section("movies"))  # it will load: "Refreshing…"
        pump()
        self.assertEqual([c[0] for c in factory.calls], ["provider_setup"])
        self.runner.complete(0)  # the setup
        pump()
        loads = [c[1][3] for c in factory.calls if c[0] == "xtream"]
        self.assertIn("vod", loads)  # the films

    def test_a_refresh_waiting_for_a_setup_that_fails_loads_nothing(self):
        service, providers, factory = self.service()
        factory.results["provider_setup"] = {"fail": "HTTPConnectionPool: Max retries exceeded"}
        providers.current_provider_content["vod"] = FILMS
        service.select_provider()
        service.refresh_section("movies")
        with self.assertLogs("services.catalog", "INFO"):
            self.runner.complete(0)
        pump()
        self.assertEqual([c[0] for c in factory.calls], ["provider_setup"])

    def test_refresh_says_whether_anything_loads(self):  # 2.8 review M7
        service, _providers, factory = self.service("M3UPLAYLIST")
        self.assertFalse(service.refresh_section("movies"))  # a playlist has no films
        pump()
        self.assertEqual(factory.calls, [])

    def test_refresh_asks_a_server_what_changed_and_merges_it(self):
        from services import media_server as ms

        service, providers, factory = self.service("JELLYFIN")
        libraries = [{"id": "L1", "name": "Movies", "type": "movies"}]
        saved = ms.section_payload(libraries, {"L1": [{"Id": "f1", "Name": "Salt"}]}, "h:1")
        saved.update(server_since="2026-10-26T10:00:00Z", server_full=5.0)
        providers.current_provider_content["vod"] = saved
        delta = {
            "delta": True,
            "libraries": libraries,
            "order": {"L1": ["f1", "f2"]},
            "changed": {"f2": ms.title_item({"Id": "f2", "Name": "Tide"}, "h:1", "L1")},
            "server_since": "2026-10-26T11:50:00Z",
        }
        factory.results["media_section"] = {"result": delta}
        service.refresh_section("movies")
        pump()
        _name, _args, kwargs = factory.calls[-1]
        self.assertEqual(kwargs["saved"]["members"], {"L1": ["f1"]})
        self.runner.complete(0)
        content = providers.current_provider_content["vod"]
        self.assertEqual([i["name"] for i in content["contents"]], ["Salt", "Tide"])
        self.assertEqual(content["server_since"], "2026-10-26T11:50:00Z")

    def test_refresh_loads_a_portal_s_genres_again(self):
        service, providers, factory = self.service("STB")
        providers.current_provider_content["vod"] = FILMS
        service.refresh_section("movies")
        pump()
        self.assertEqual([c[0] for c in factory.calls], ["stb_categories"])

    def test_a_portal_s_and_the_live_lists_still_load_first(self):
        for ptype, section, ct in (("STB", "movies", "vod"), ("XTREAM", "live", "itv")):
            with self.subTest(ptype=ptype, section=section):
                service, providers, factory = self.service(ptype)
                providers.current_provider_content[ct] = FILMS
                providers.stale.add(ct)
                ready = collect(service.sectionReady)
                service.request_section(section)
                pump()
                self.assertEqual(ready, [])  # the new list first, as before
                self.assertEqual(len(factory.calls), 1)

    def test_a_list_kept_for_other_settings_is_not_shown(self):
        # The provider was edited (another account): its old list is not shown meanwhile.
        service, providers, factory = self.service()
        providers.current_provider_content["vod"] = FILMS
        providers.stale.add("vod")
        providers.other_identity = True
        ready = collect(service.sectionReady)
        service.request_section("movies")
        pump()
        self.assertEqual(ready, [])

    def test_a_ready_provider_loads_its_lists_ahead(self):
        service, providers, factory = self.service()
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["xtream"] = {"fail": "HTTPConnectionPool: Max retries exceeded"}
        providers.current_provider_content["vod"] = FILMS  # fresh: not loaded again
        with mock.patch.object(catalog_module, "LOAD_AHEAD_MS", 0):
            service.select_provider()
            self.runner.complete(0)  # the setup
            pump()
        loads = [c for c in factory.calls if c[0] == "xtream"]
        self.assertEqual([c[1][3] for c in loads], ["series"])  # the missing one only
        failed = collect(service.sectionFailed)
        jobs = [k for k, job in enumerate(self.runner.jobs) if job[3].kind == "section_behind"]
        with self.assertLogs("services.catalog", "INFO"):
            self.runner.complete(jobs[0])
        self.assertEqual(failed, [])  # nobody waits for it: quietly

    def test_a_list_asked_for_while_it_loads_ahead_waits_for_it(self):
        service, providers, factory = self.service()
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["xtream"] = {"fail": "HTTPConnectionPool: Max retries exceeded"}
        with mock.patch.object(catalog_module, "LOAD_AHEAD_MS", 0):
            service.select_provider()
            self.runner.complete(0)
            pump()
        ready, failed = collect(service.sectionReady), collect(service.sectionFailed)
        calls = len(factory.calls)
        service.request_section("movies")  # the Library opens meanwhile
        pump()
        self.assertEqual(len(factory.calls), calls)  # no second load
        movies = next(
            k
            for k, job in enumerate(self.runner.jobs)
            if job[3].kind == "section_behind" and job[3].data == "movies"
        )
        with self.assertLogs("services.catalog", "INFO"):
            self.runner.complete(movies)
        self.assertEqual([f[0] for f in failed], ["movies"])  # it waited: told
        self.assertEqual(ready, [])

    def test_a_wait_dropped_with_its_provider_tells_no_later_failure(self):  # review M8
        # The Library waited for a list (nothing saved); a new setup dropped that load. The
        # list loaded ahead later fails quietly: nobody waits for it any more.
        service, providers, factory = self.service()
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["xtream"] = {"fail": "HTTPConnectionPool: Max retries exceeded"}
        service.request_section("movies")  # waited: nothing saved
        pump()
        with mock.patch.object(catalog_module, "LOAD_AHEAD_MS", 0):
            service.select_provider(force_refresh=True)  # its load is dropped
            setup = next(k for k, job in enumerate(self.runner.jobs) if job[3].kind == "provider")
            self.runner.complete(setup)
            pump()
        providers.current_provider_content["vod"] = FILMS  # saved since (another load)
        failed = collect(service.sectionFailed)
        behind = [k for k, job in enumerate(self.runner.jobs) if job[3].kind == "section_behind"]
        with self.assertLogs("services.catalog", "INFO"):
            for k in behind:
                self.runner.complete(k)
        self.assertEqual(failed, [])

    def test_a_portal_or_a_playlist_loads_nothing_ahead(self):
        for ptype in ("STB", "M3UPLAYLIST"):
            with self.subTest(ptype=ptype):
                service, providers, factory = self.service(ptype)
                factory.results["provider_setup"] = {"result": {"name": "X"}}
                with mock.patch.object(catalog_module, "LOAD_AHEAD_MS", 0):
                    service.select_provider()
                    self.runner.complete(0)
                    pump()
                kinds = [job[3].kind for job in self.runner.jobs]
                self.assertNotIn("section_behind", kinds)


if __name__ == "__main__":
    unittest.main()
