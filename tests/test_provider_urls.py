"""Providers asked over HTTPS first, and export URL shapes.

These cases guarded the old UI mixins. The behaviour now lives in the services the TV
window uses: CatalogService (links, episode listings), the link worker, the exporter. Since
2.7.1 there is no "Prefer HTTPS" setting: HTTPS is chosen by what each provider answers.
"""

import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services import provider_api
from services.catalog import WorkerFactory
from services.export import save_stb_content
from services.exporter import ExportKind, ExportWorker
from services.provider_api import remember_http_only, stb_portal_base
from tests.test_catalog_service import make_service

ITEMS = [{"name": "One", "cmd": "ffmpeg http://localhost/ch/123_"}]


class _Response:
    status_code = 200
    content = b"{}"

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


def _stb_service(provider):
    service, _, factory, _ = make_service({"name": "S", "type": "STB", **provider})
    return service, factory


class _HttpsMemory(unittest.TestCase):
    """A test of its own run: no host is known to answer only over HTTP yet."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        memory = patch.object(provider_api, "_http_only", set())
        memory.start()
        self.addCleanup(memory.stop)


class CreateLinkHttpsTests(_HttpsMemory):
    def _created_url(self, provider):
        """The create_link request of an STB channel, as CatalogService asks for it."""
        service, factory = _stb_service(provider)
        factory.results["link"] = {"result": {"link": "http://stream.test/1.ts"}}
        service.request_link({"cmd": "ffmpeg http://localhost/ch/1_"}, section="live")
        name, _, kwargs = factory.calls[-1]
        self.assertEqual(name, "link")
        worker = WorkerFactory().link(**kwargs)  # the real one, as the app makes it
        calls, links = [], []
        worker.finished.connect(links.append)

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Response({"js": {"cmd": "ffmpeg http://stream.test/1.ts"}})

        with patch("services.workers.requests.get", side_effect=fake_get):
            worker.run()
        self.assertEqual(links, [{"link": "http://stream.test/1.ts"}])
        return calls[0]

    def test_a_link_is_asked_over_https_first(self):
        # an older QiTV's saved "prefer_https" is read no more
        for saved in ({}, {"prefer_https": False}):
            with self.subTest(saved=saved):
                url = self._created_url({"url": "http://portal.test/c/", **saved})
                self.assertTrue(url.startswith("https://portal.test/"), url)

    def test_a_portal_that_answered_only_over_http_is_asked_over_http(self):
        remember_http_only("http://portal.test/c/")
        url = self._created_url({"url": "http://portal.test/c/"})
        self.assertTrue(url.startswith("http://portal.test/"), url)


class StbSeriesFetchHttpsTests(_HttpsMemory):
    def test_complete_series_listing_is_asked_over_https_first(self):
        provider = {"type": "STB", "url": "http://portal.test/c/"}
        worker = ExportWorker(
            ExportKind.COMPLETE_SERIES,
            provider,
            {},
            "series",
            os.devnull,
            verify_ssl=True,
            headers={},
            cancel=threading.Event(),
        )
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Response({"js": {"data": []}})

        session = SimpleNamespace(get=fake_get, close=lambda: None)
        worker._fetch(session, {"category_id": "1", "id": "9"}, 0, "name")
        self.assertTrue(calls[0].startswith("https://portal.test/server/load.php"), calls[0])


class StbEpisodeLoadingHttpsTests(_HttpsMemory):
    def test_stb_episodes_are_asked_over_https_first(self):
        service, factory = _stb_service({"url": "http://portal.test/c/"})
        factory.results["stb_list"] = {"result": {"items": []}}
        service.request_episodes({"id": "9", "category_id": "1"}, {"id": "2"})
        name, _, kwargs = factory.calls[-1]
        self.assertEqual(name, "stb_list")
        self.assertEqual(kwargs["url"], "https://portal.test/server/load.php")


class StbExportBaseTests(unittest.TestCase):
    def test_raw_portal_url_is_reduced_to_scheme_and_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.m3u"
            save_stb_content("http://portal.test/c/", ITEMS, "00:1A:79:00:00:01", str(path))
            text = path.read_text(encoding="utf-8")
        self.assertIn(
            "http://portal.test/play/live.php?mac=00:1A:79:00:00:01&stream=123&extension=m3u8",
            text,
        )
        self.assertNotIn("//play", text.replace("http://", ""))


class ProviderCacheIdentityTests(unittest.TestCase):
    def _identity(self, provider):
        from services.provider_manager import ProviderManager

        manager = ProviderManager.__new__(ProviderManager)
        manager.config_manager = SimpleNamespace(ssl_verify=True)
        return manager.provider_cache_identity(provider)

    def test_an_older_saved_https_preference_does_not_change_the_identity(self):
        base = {"type": "STB", "url": "http://p.test"}
        for saved in (None, True, False):
            with self.subTest(saved=saved):
                self.assertEqual(
                    self._identity({**base, "prefer_https": saved}), self._identity(base)
                )


class ExportLinkSchemeTests(unittest.TestCase):
    def test_exported_links_keep_provider_scheme(self):
        provider = {"type": "STB", "url": "http://portal.test/c/", "mac": "m"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.m3u"
            worker = ExportWorker(
                ExportKind.CACHED,
                provider,
                {"itv": list(ITEMS)},
                "live",
                str(path),
                verify_ssl=True,
                headers={},
                cancel=threading.Event(),
            )
            written = []
            worker.finished.connect(written.append)
            worker.run()
            text = path.read_text(encoding="utf-8")
        self.assertEqual(written, [1])
        self.assertIn("http://portal.test/play/live.php", text)
        self.assertNotIn("https://", text)

    def test_fetch_base_is_https_first(self):
        with patch.object(provider_api, "_http_only", set()):
            self.assertEqual(stb_portal_base("http://portal.test/c/", True), "https://portal.test")


if __name__ == "__main__":
    unittest.main()
