"""Provider credentials never reach the logs, at any level (URLs carry them)."""

import logging
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
import requests

from services.epg_manager import EpgManager
from services.jobs import ImmediateRunner
from services.provider_manager import StbHandshake
from services.redact import redact_url, safe_exc, safe_message, safe_traceback
from services.workers import (
    EpgLoadWorker,
    ProviderSetupWorker,
    XtreamLoaderWorker,
    XtreamSeriesInfoWorker,
)

USERNAME, PASSWORD, MAC = "quser7", "qpass9", "00:1A:79:12:34:56"
CREDENTIAL_URL = f"http://h/player_api.php?username={USERNAME}&password={PASSWORD}"
SECRETS = (USERNAME, PASSWORD, "username=", "password=", MAC, "player_api.php")


def refused(url=CREDENTIAL_URL):
    """A requests ConnectionError as requests raises it: its text and request name the URL."""
    path = url.split("://", 1)[-1].split("/", 1)[-1]
    return requests.ConnectionError(
        f"HTTPConnectionPool(host='h', port=80): Max retries exceeded with url: /{path} "
        "(Caused by NewConnectionError('Failed to establish a new connection'))",
        request=requests.Request("GET", url).prepare(),
    )


class _Response:
    def __init__(self, body=None, status_code=200, content=b"{}"):
        self._body = body if body is not None else {}
        self.status_code = status_code
        self.content = content
        self.ok = status_code < 400

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} Error for url: {CREDENTIAL_URL}")

    def close(self):
        pass


class RedactTests(unittest.TestCase):
    def test_redact_url_keeps_only_scheme_and_host(self):
        self.assertEqual(redact_url(CREDENTIAL_URL), "http://h/…")
        self.assertEqual(
            redact_url("https://user:pw@portal.test:8080/c/?mac=x#f"), "https://portal.test/…"
        )
        self.assertEqual(redact_url("not a url"), "…")
        self.assertEqual(redact_url(None), "…")

    def test_safe_exc_names_the_type_and_host_only(self):
        self.assertEqual(safe_exc(refused()), "ConnectionError (http://h/…)")
        self.assertEqual(safe_exc(RuntimeError(CREDENTIAL_URL)), "RuntimeError")

    def test_safe_message_keeps_the_text_without_paths_or_addresses(self):
        locked = PermissionError(13, "Access is denied", "/home/alice/config.json")
        self.assertEqual(safe_message(locked), "PermissionError: [Errno 13] Access is denied")
        self.assertEqual(
            safe_message(TypeError("Type is not JSON serializable: bytes")),
            "TypeError: Type is not JSON serializable: bytes",
        )
        text = safe_message(RuntimeError(f"Failed for {CREDENTIAL_URL}"))
        self.assertEqual(text, "RuntimeError: Failed for …")
        self.assertEqual(safe_message(ValueError()), "ValueError")
        for secret in SECRETS:
            self.assertNotIn(secret, safe_message(refused()))

    def test_safe_traceback_has_frames_but_no_message(self):
        try:
            raise refused()
        except requests.ConnectionError as e:
            text = safe_traceback(e)
        self.assertIn("test_safe_traceback_has_frames_but_no_message", text)
        self.assertIn("ConnectionError (http://h/…)", text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)


class LogPathTests(unittest.TestCase):
    """Each logging path that saw a credential URL, fed a ConnectionError for one."""

    def assertNoSecrets(self, output):
        text = "\n".join(output)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_xtream_categories_failure(self):
        class Session:
            def __init__(self):
                self.headers = {}

            def get(self, url, **kwargs):
                if "action=get_live_categories" in url:
                    raise refused(url)
                if "action=" in url:
                    return _Response([])
                return _Response({})

            def close(self):
                pass

        worker = XtreamLoaderWorker("http://h", USERNAME, PASSWORD, "itv")
        with (
            patch("services.workers.requests.Session", return_value=Session()),
            self.assertLogs(level="DEBUG") as logs,
        ):
            worker.run()
        self.assertIn("ConnectionError", "\n".join(logs.output))
        self.assertNoSecrets(logs.output)

    def test_xtream_series_info_failure(self):
        worker = XtreamSeriesInfoWorker("http://h", USERNAME, PASSWORD, "5")
        errors = []
        worker.error.connect(errors.append)
        with (
            patch("services.workers.requests.get", side_effect=refused()),
            self.assertLogs(level="DEBUG") as logs,
        ):
            worker.run()
        self.assertTrue(errors)  # the text goes to the catalog, which makes it user-safe
        self.assertIn("ConnectionError", "\n".join(logs.output))
        self.assertNoSecrets(logs.output)

    def _epg_manager(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-redact-")
        self.addCleanup(directory.cleanup)
        config = SimpleNamespace(
            get_config_dir=lambda: directory.name,
            prefer_https=False,
            ssl_verify=True,
            xmltv_channel_map=SimpleNamespace(get_keys=lambda key: None),
        )
        provider = {"type": "XTREAM", "name": "X", "url": "http://h"}
        return EpgManager(config, SimpleNamespace(current_provider=provider, headers={}))

    def test_xtream_epg_failure(self):
        manager = self._epg_manager()
        with (
            patch("services.epg_manager.requests.get", side_effect=refused()),
            self.assertLogs(level="DEBUG") as logs,
        ):
            manager._fetch_epg_from_xtream("http://h", USERNAME, PASSWORD)
        self.assertNoSecrets(logs.output)

    def test_xtream_epg_bad_status(self):
        manager = self._epg_manager()
        response = _Response(status_code=500, content=b"busy")
        with (
            patch("services.epg_manager.requests.get", return_value=response),
            self.assertLogs(level="DEBUG") as logs,
        ):
            manager._fetch_epg_from_xtream("http://h", USERNAME, PASSWORD)
        self.assertIn("500", "\n".join(logs.output))
        self.assertNoSecrets(logs.output)

    def test_stb_handshake_failure(self):
        profile = f"http://h/portal.php?type=stb&action=get_profile&metrics=%7B%22mac%22%3A%22{MAC}"
        with (
            patch("services.provider_manager.requests.get", side_effect=refused(profile)),
            self.assertLogs(level="DEBUG") as logs,
        ):
            self.assertFalse(StbHandshake().run("http://h", MAC))
        self.assertNoSecrets(logs.output)

    def test_job_runner_worker_exception(self):
        class Raising(QObject):
            finished = Signal(object)
            error = Signal(str)

            def run(self):
                raise refused()

        errors = []
        with self.assertLogs(level="DEBUG") as logs:
            ImmediateRunner().run_worker(
                Raising(), lambda c, p: None, lambda c, m: errors.append(m)
            )
        self.assertTrue(errors)
        error_lines = [line for line in logs.output if line.startswith("ERROR:")]
        self.assertTrue(
            any("Raising" in line and "ConnectionError" in line for line in error_lines)
        )
        self.assertNoSecrets(logs.output)

    def test_setup_and_epg_worker_failures(self):
        def boom(*args, **kwargs):
            raise refused()

        setup = ProviderSetupWorker(SimpleNamespace(set_current_provider=boom))
        epg = EpgLoadWorker(SimpleNamespace(set_current_epg=boom))
        for worker in (setup, epg):
            with self.subTest(worker=type(worker).__name__):
                with self.assertLogs(level="DEBUG") as logs:
                    worker.run()
                self.assertNoSecrets(logs.output)


class RawUrlLogTests(unittest.TestCase):
    """The remaining log lines that named a URL, or an exception text naming one (H5)."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def assertNoSecrets(self, output):
        text = "\n".join(output)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_content_loader_page_and_load_failures(self):
        import asyncio

        import aiohttp

        from services.content_loader import ContentLoader

        class Session:
            def get(self, *args, **kwargs):
                raise aiohttp.ClientError(f"Cannot connect for {CREDENTIAL_URL}")

        loader = ContentLoader("http://h/server/load.php", {}, "vod")
        with self.assertLogs(level="DEBUG") as logs:
            asyncio.run(loader.fetch_page(Session(), 1, max_retries=1, timeout=1))
        self.assertIn("ClientError", "\n".join(logs.output))
        self.assertNoSecrets(logs.output)

        async def boom():
            raise RuntimeError(f"failed for {CREDENTIAL_URL}")

        loader.load_content = boom
        with self.assertLogs(level="DEBUG") as logs:
            loader.run()
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNoSecrets(logs.output)

    def test_image_download_failures(self):
        import asyncio

        import aiohttp

        from services.image_loader import ImageLoader
        from services.image_manager import ImageManager

        directory = tempfile.TemporaryDirectory(prefix="qitv-redact-")
        self.addCleanup(directory.cleanup)
        manager = ImageManager(SimpleNamespace(get_config_dir=lambda: directory.name))

        class Session:
            def get(self, *args, **kwargs):
                raise aiohttp.ClientError(f"Cannot connect for {CREDENTIAL_URL}")

        with self.assertLogs(level="DEBUG") as logs:
            asyncio.run(manager.cache_image_from_url(Session(), CREDENTIAL_URL, True, 1))
        self.assertNoSecrets(logs.output)

        async def refuse(*args, **kwargs):
            raise RuntimeError(f"failed for {CREDENTIAL_URL}")

        manager.cache_image_from_url = refuse
        loader = ImageLoader([CREDENTIAL_URL], manager)
        with self.assertLogs(level="DEBUG") as logs:
            loader.run()  # the download, the batch and the loader each log it
        self.assertGreaterEqual(len(logs.output), 2)
        self.assertNoSecrets(logs.output)

    def test_an_unreadable_logo(self):
        from PySide6.QtGui import QImage

        from ui.logos import ImageCache

        cache = ImageCache(None, SimpleNamespace(ssl_verify=True))
        cache._batch = [CREDENTIAL_URL]
        with self.assertLogs("ui.logos", "INFO") as logs:
            cache._on_progress(1, 1, {"rank": 0, "cache_path": "/x.png", "image": QImage()})
        self.assertNoSecrets(logs.output)

    def test_a_url_base_that_cannot_be_parsed(self):
        from services.provider_api import base_from_url

        with self.assertLogs("services.provider_api", "WARNING") as logs:
            base_from_url(CREDENTIAL_URL.encode())  # not text: find() raises
        self.assertNoSecrets(logs.output)

    def test_provider_verification_failures(self):
        from ui.settings_dialog import ProviderVerificationWorker

        stb = {"type": "STB", "url": "http://h/c/", "mac": MAC}
        worker = ProviderVerificationWorker(1, stb, False, True)
        with (
            patch("ui.settings_dialog.StbHandshake.run", side_effect=refused()),
            self.assertLogs(level="DEBUG") as logs,
        ):
            worker.run()
        self.assertNoSecrets(logs.output)
        with (
            patch("ui.settings_dialog.requests.head", side_effect=ValueError(CREDENTIAL_URL)),
            self.assertLogs(level="DEBUG") as logs,
        ):
            self.assertFalse(ProviderVerificationWorker.verify_url(CREDENTIAL_URL))
        self.assertNoSecrets(logs.output)

    def test_a_job_callback_that_raises(self):
        from services.jobs import JobRunner
        from tests.test_catalog_service import FakeWorker

        def callback(context, payload):
            raise RuntimeError(f"failed for {CREDENTIAL_URL}")

        runner = JobRunner()
        done = []
        with self.assertLogs(level="DEBUG") as logs:
            runner.run_worker(FakeWorker({}), lambda c, p: (done.append(p), callback(c, p)))
            deadline = __import__("time").monotonic() + 10
            while not done and __import__("time").monotonic() < deadline:
                self.app.processEvents()
        self.assertTrue(done)
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNoSecrets(logs.output)

    def test_a_failed_close_step(self):
        from ui.tv_window import TVWindow

        def step():
            raise RuntimeError(f"failed for {CREDENTIAL_URL}")

        with self.assertLogs("ui.tv_window", "DEBUG") as logs:
            TVWindow._run_steps(("stop the catalog", step))
        self.assertIn("stop the catalog", "\n".join(logs.output))
        self.assertNoSecrets(logs.output)


class DebugCurlTests(unittest.TestCase):
    """QITV_DEBUG_CURL prints the STB EPG request as curl: the device and token stay masked."""

    SERIAL, DEVICE, TOKEN = "SN0042QX", "DEV9F3A", "tok3n5ecret"
    HEADERS = {
        "User-Agent": "MAG200",
        "Cookie": f"mac={MAC}; stb_lang=en; timezone=UTC; PHPSESSID=null; sn={SERIAL}; "
        f"device_id={DEVICE};",
        "Authorization": f"Bearer {TOKEN}",
    }

    def curl_log(self, mode):
        import asyncio

        import aiohttp

        from services.content_loader import ContentLoader

        class Session:
            def get(self, *args, **kwargs):
                raise aiohttp.ClientError("refused")

        loader = ContentLoader(
            "http://portal.test/server/load.php",
            self.HEADERS,
            "itv",
            action="get_epg_info",
            period=5,
        )
        with (
            patch.dict(os.environ, {"QITV_DEBUG_CURL": mode}),
            self.assertLogs("services.content_loader", "INFO") as logs,
        ):
            asyncio.run(loader.fetch_page(Session(), 1, max_retries=1, timeout=1))
        return next(line for line in logs.output if "curl" in line)

    def test_cookie_values_and_the_token_are_masked_by_default(self):
        line = self.curl_log("1")
        for secret in (MAC, self.SERIAL, self.DEVICE, self.TOKEN):
            self.assertNotIn(secret, line)
        self.assertIn(
            '-H "Cookie: mac=…; stb_lang=…; timezone=…; PHPSESSID=…; sn=…; device_id=…;"', line
        )
        self.assertIn('-H "Authorization: Bearer …"', line)
        self.assertIn('-H "User-Agent: MAG200"', line)
        self.assertIn("action=get_epg_info", line)

    def test_raw_shows_them(self):
        line = self.curl_log("raw")
        for secret in (MAC, self.SERIAL, self.DEVICE, self.TOKEN):
            self.assertIn(secret, line)


if __name__ == "__main__":
    unittest.main()
