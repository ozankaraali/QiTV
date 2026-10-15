"""Provider HTTPS preferences and export URL shapes.

These cases guarded the old UI mixins. The behaviour now lives in the services the TV
window uses: CatalogService (links, episode listings), the link worker, the exporter.
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

from services.export import save_stb_content
from services.exporter import ExportKind, ExportWorker
from services.provider_api import provider_prefers_https, stb_portal_base
from services.workers import LinkCreatorWorker
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


def _stb_service(provider, global_https):
    service, _, factory, _ = make_service({"name": "S", "type": "STB", **provider})
    service.config.prefer_https = global_https
    return service, factory


class ProviderPrefersHttpsTests(unittest.TestCase):
    def test_provider_value_overrides_global(self):
        self.assertTrue(provider_prefers_https({"prefer_https": True}, False))
        self.assertFalse(provider_prefers_https({"prefer_https": False}, True))

    def test_missing_or_none_falls_back_to_global(self):
        self.assertTrue(provider_prefers_https({}, True))
        self.assertFalse(provider_prefers_https({"prefer_https": None}, False))


class CreateLinkHttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _created_url(self, provider, global_https):
        """The create_link request of an STB channel, as CatalogService asks for it."""
        service, factory = _stb_service(provider, global_https)
        factory.results["link"] = {"result": {"link": "http://stream.test/1.ts"}}
        service.request_link({"cmd": "ffmpeg http://localhost/ch/1_"}, section="live")
        name, _, kwargs = factory.calls[-1]
        self.assertEqual(name, "link")
        worker = LinkCreatorWorker(**kwargs)
        calls, links = [], []
        worker.finished.connect(links.append)

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Response({"js": {"cmd": "ffmpeg http://stream.test/1.ts"}})

        with patch("services.workers.requests.get", side_effect=fake_get):
            worker.run()
        self.assertEqual(links, [{"link": "http://stream.test/1.ts"}])
        return calls[0]

    def test_provider_https_preference_is_used(self):
        url = self._created_url({"url": "http://portal.test/c/", "prefer_https": True}, False)
        self.assertTrue(url.startswith("https://portal.test/"), url)

    def test_provider_can_opt_out_of_global_https(self):
        url = self._created_url({"url": "http://portal.test/c/", "prefer_https": False}, True)
        self.assertTrue(url.startswith("http://portal.test/"), url)


class StbSeriesFetchHttpsTests(unittest.TestCase):
    def test_complete_series_listing_uses_provider_preference(self):
        provider = {"type": "STB", "url": "http://portal.test/c/", "prefer_https": True}
        worker = ExportWorker(
            ExportKind.COMPLETE_SERIES,
            provider,
            {},
            "series",
            os.devnull,
            verify_ssl=True,
            prefer_https=False,
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


class StbEpisodeLoadingHttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_stb_episodes_use_provider_preference(self):
        provider = {"url": "http://portal.test/c/", "prefer_https": True}
        service, factory = _stb_service(provider, False)
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
    def _identity(self, provider, global_https):
        from services.provider_manager import ProviderManager

        manager = ProviderManager.__new__(ProviderManager)
        manager.config_manager = SimpleNamespace(prefer_https=global_https, ssl_verify=True)
        return manager.provider_cache_identity(provider)

    def test_unset_provider_value_matches_global_default(self):
        base = {"type": "STB", "url": "http://p.test"}
        self.assertEqual(
            self._identity({**base, "prefer_https": None}, True),
            self._identity({**base, "prefer_https": True}, False),
        )
        self.assertNotEqual(
            self._identity({**base, "prefer_https": None}, True),
            self._identity({**base, "prefer_https": False}, True),
        )


class ExportLinkSchemeTests(unittest.TestCase):
    def test_exported_links_keep_provider_scheme_despite_prefer_https(self):
        provider = {"type": "STB", "url": "http://portal.test/c/", "mac": "m", "prefer_https": True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.m3u"
            worker = ExportWorker(
                ExportKind.CACHED,
                provider,
                {"itv": list(ITEMS)},
                "live",
                str(path),
                verify_ssl=True,
                prefer_https=True,
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

    def test_fetch_base_still_honours_prefer_https(self):
        self.assertEqual(stb_portal_base("http://portal.test/c/", True), "https://portal.test")


if __name__ == "__main__":
    unittest.main()
