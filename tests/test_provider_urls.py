import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mixins.content_loading_mixin import ContentLoadingMixin
from mixins.export_mixin import ExportMixin
from mixins.playback_mixin import PlaybackMixin
from services.export import save_stb_content
from services.provider_api import provider_prefers_https, stb_portal_base


class _Response:
    status_code = 200
    content = b"{}"

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _host(cls, provider, global_https):
    host = cls.__new__(cls)
    host.provider_manager = SimpleNamespace(
        current_provider=provider, headers={}, current_provider_content={}
    )
    host.config_manager = SimpleNamespace(prefer_https=global_https, ssl_verify=True)
    host.content_type = "itv"
    host.sanitize_url = lambda url: url
    return host


class ProviderPrefersHttpsTests(unittest.TestCase):
    def test_provider_value_overrides_global(self):
        self.assertTrue(provider_prefers_https({"prefer_https": True}, False))
        self.assertFalse(provider_prefers_https({"prefer_https": False}, True))

    def test_missing_or_none_falls_back_to_global(self):
        self.assertTrue(provider_prefers_https({}, True))
        self.assertFalse(provider_prefers_https({"prefer_https": None}, False))


class CreateLinkHttpsTests(unittest.TestCase):
    def _created_url(self, provider, global_https):
        host = _host(PlaybackMixin, provider, global_https)
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Response({"js": {"cmd": "ffmpeg http://stream.test/1.ts"}})

        with patch("mixins.playback_mixin.requests.get", side_effect=fake_get):
            link = host.create_link({"cmd": "ffmpeg http://localhost/ch/1_"})
        self.assertEqual(link, "http://stream.test/1.ts")
        return calls[0]

    def test_provider_https_preference_is_used(self):
        url = self._created_url({"url": "http://portal.test/c/", "prefer_https": True}, False)
        self.assertTrue(url.startswith("https://portal.test/"), url)

    def test_provider_can_opt_out_of_global_https(self):
        url = self._created_url({"url": "http://portal.test/c/", "prefer_https": False}, True)
        self.assertTrue(url.startswith("http://portal.test/"), url)


class StbSeriesFetchHttpsTests(unittest.TestCase):
    def test_sync_series_listing_uses_provider_preference(self):
        provider = {"url": "http://portal.test/c/", "prefer_https": True}
        host = _host(ExportMixin, provider, False)
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Response({"js": {"data": []}})

        with patch("mixins.export_mixin.requests.get", side_effect=fake_get):
            host._fetch_series_list_sync({"category_id": "1", "id": "9"}, 0, "added", "seasons")
        self.assertTrue(calls[0].startswith("https://portal.test/server/load.php"), calls[0])


class StbEpisodeLoadingHttpsTests(unittest.TestCase):
    def test_stb_episodes_use_provider_preference(self):
        provider = {"type": "STB", "url": "http://portal.test/c/", "prefer_https": True}
        host = _host(ContentLoadingMixin, provider, False)
        host.current_category = {"id": "1"}
        host.current_series = {"id": "9"}
        host.cancel_content_loading = lambda: None
        host._start_content_worker = lambda *args, **kwargs: None
        host.cancel_button = SimpleNamespace(setText=lambda text: None)
        captured = {}

        def fake_loader(**kwargs):
            captured.update(kwargs)
            return object()

        with patch("mixins.content_loading_mixin.ContentLoader", side_effect=fake_loader):
            host.load_season_episodes({"id": "2"})
        self.assertEqual(captured["url"], "https://portal.test/server/load.php")


class StbExportBaseTests(unittest.TestCase):
    def test_raw_portal_url_is_reduced_to_scheme_and_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.m3u"
            items = [{"name": "One", "cmd": "ffmpeg http://localhost/ch/123_"}]
            save_stb_content("http://portal.test/c/", items, "00:1A:79:00:00:01", str(path))
            text = path.read_text(encoding="utf-8")
        self.assertIn(
            "http://portal.test/play/live.php?mac=00:1A:79:00:00:01&stream=123&extension=m3u8",
            text,
        )
        self.assertNotIn("//play", text.replace("http://", ""))


class ProviderCacheIdentityTests(unittest.TestCase):
    def _identity(self, provider, global_https):
        from provider_manager import ProviderManager

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
        host = _host(ExportMixin, {"url": "http://portal.test/c/", "mac": "m"}, True)
        base = host._export_link_base(host.provider_manager.current_provider)
        self.assertEqual(base, "http://portal.test")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.m3u"
            items = [{"name": "One", "cmd": "ffmpeg http://localhost/ch/123_"}]
            save_stb_content(base, items, "m", str(path))
            text = path.read_text(encoding="utf-8")
        self.assertIn("http://portal.test/play/live.php", text)
        self.assertNotIn("https://", text)

    def test_fetch_base_still_honours_prefer_https(self):
        self.assertEqual(stb_portal_base("http://portal.test/c/", True), "https://portal.test")


class StbEpisodeStringTests(unittest.TestCase):
    def test_non_decimal_digit_strings_are_skipped_not_crashing(self):
        host = _host(PlaybackMixin, {"type": "STB"}, False)
        built = []
        host._build_stb_episode_item = lambda num, season: built.append(num) or {"n": num}
        items = host._build_episode_list_from_season({"series": ["1", " 2 ", "\u00b2", 3]})
        self.assertEqual(built, [1, 2, 3])
        self.assertEqual(len(items), 3)


if __name__ == "__main__":
    unittest.main()
