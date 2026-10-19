"""Providers that turn away the requests library's own User-Agent still load.

Some panels' proxies answer 403 to "python-requests/…" and 502 to the bare host,
while players such as VLC get in: QiTV names itself on every provider request.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services import provider_api
from services.epg_manager import EpgManager, _xtream_cache_key
from services.provider_api import CLIENT_HEADERS
from services.workers import M3ULoaderWorker, XtreamLoaderWorker, XtreamSeriesInfoWorker
from ui.settings_dialog import ProviderVerificationWorker

XMLTV = (
    b'<?xml version="1.0"?><tv><channel id="one"><display-name>One</display-name></channel>'
    b'<programme start="20261017120000 +0000" stop="20261017130000 +0000" channel="one">'
    b"<title>Noon</title></programme></tv>"
)


class _Panel(BaseHTTPRequestHandler):
    """A panel behind a proxy that blocks library agents, like the reported provider."""

    fixture = None

    def do_HEAD(self):
        self._answer(head=True)

    def do_GET(self):
        self._answer(head=False)

    def _answer(self, head):
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        agent = self.headers.get("User-Agent", "")
        self.fixture.requests.append((url.path, agent))
        if agent.startswith("python-requests"):
            return self._send(403, b"<h1>403 Forbidden</h1>", "text/html", head)
        if url.path == "/":
            return self._send(502, b"Bad Gateway", "text/html", head)
        signed_in = (query.get("username"), query.get("password")) == ("user", "password")
        if url.path == "/player_api.php":
            action = query.get("action")
            if action is None:
                payload = {"user_info": {"auth": 1 if signed_in else 0}, "server_info": {}}
                if signed_in:
                    payload["user_info"].update(
                        status=self.fixture.status, allowed_output_formats=["ts"]
                    )
            elif not signed_in:
                payload = {"user_info": {"auth": 0}}
            elif action == "get_live_categories":
                payload = [{"category_id": "1", "category_name": "News"}]
            elif action == "get_live_streams":
                payload = [{"stream_id": 7, "name": "One", "category_id": "1"}]
            elif action == "get_series_info":
                payload = {"seasons": [], "info": {}, "episodes": {"1": [{"id": "9"}]}}
            else:
                return self._send(400, b"unexpected action", "text/plain", head)
            return self._send(200, json.dumps(payload).encode(), "application/json", head)
        if url.path == "/get.php" and signed_in:
            body = b"#EXTM3U\n#EXTINF:-1 group-title=\"News\",One\nhttp://127.0.0.1/7.ts\n"
            return self._send(200, body, "audio/x-mpegurl", head)
        if url.path == "/xmltv.php" and signed_in:
            return self._send(200, XMLTV, "application/xml", head)
        self._send(404, b"not found", "text/plain", head)

    def _send(self, status, body, content_type, head):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class BlockedLibraryAgentTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = "Active"
        handler = type("Handler", (_Panel,), {"fixture": self})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def tearDown(self):
        # Every request named QiTV's client, none fell back to the library default.
        self.assertTrue(self.requests)
        self.assertEqual({agent for _, agent in self.requests}, {CLIENT_HEADERS["User-Agent"]})

    def _run(self, worker):
        results, errors = [], []
        worker.finished.connect(results.append)
        worker.error.connect(errors.append)
        worker.run()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        return results[0]

    def _verify(self, **provider):
        verified = []
        worker = ProviderVerificationWorker(1, provider, False, True)
        worker.result.connect(lambda _id, _provider, ok: verified.append(ok))
        worker.run()
        return verified[0]

    def test_xtream_catalog_loads(self):
        catalog = self._run(XtreamLoaderWorker(self.base, "user", "password", "itv"))
        self.assertEqual([item["name"] for item in catalog["contents"]], ["One"])

    def test_xtream_series_info_loads(self):
        info = self._run(XtreamSeriesInfoWorker(self.base, "user", "password", "5"))
        self.assertTrue(info)

    def test_m3u_playlist_from_the_panel_loads(self):
        url = f"{self.base}/get.php?username=user&password=password&type=m3u_plus"
        playlist = self._run(M3ULoaderWorker(url))
        self.assertEqual([item["name"] for item in playlist["contents"]], ["One"])

    def test_xtream_epg_loads(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-agent-")
        self.addCleanup(directory.cleanup)
        config = SimpleNamespace(
            get_config_dir=lambda: directory.name,
            prefer_https=False,
            ssl_verify=True,
            xmltv_channel_map=SimpleNamespace(get_keys=lambda key: None),
        )
        provider = {"type": "XTREAM", "name": "X", "url": self.base}
        manager = EpgManager(config, SimpleNamespace(current_provider=provider, headers={}))
        manager._fetch_epg_from_xtream(self.base, "user", "password")
        self.assertEqual([path for path, _ in self.requests], ["/xmltv.php"])
        self.assertTrue(any(manager.index.values()))  # a guide, not a failed source
        # A stray path 2.2.0 cut off (the panel is at the root): the root's guide, once.
        self.requests.clear()
        with mock.patch.dict(provider_api._answered_at_root, clear=True):
            manager._fetch_epg_from_xtream(f"{self.base}/c/", "user", "password")
        self.assertEqual([path for path, _ in self.requests], ["/c/xmltv.php", "/xmltv.php"])
        key = _xtream_cache_key(f"{self.base}/c/", "user")
        self.assertTrue(manager.index.get(key))

    def test_xtream_verify_signs_in_instead_of_probing_the_host(self):
        # The bare host answers 502 here, so only a real sign-in can verify it.
        self.assertTrue(
            self._verify(type="XTREAM", url=self.base, username="user", password="password")
        )

    def test_xtream_verify_rejects_a_wrong_password(self):
        self.assertFalse(
            self._verify(type="XTREAM", url=self.base, username="user", password="wrong")
        )

    def test_m3u_verify_names_the_client(self):
        url = f"{self.base}/get.php?username=user&password=password"
        self.assertTrue(self._verify(type="M3UPLAYLIST", url=url))


if __name__ == "__main__":
    unittest.main()
