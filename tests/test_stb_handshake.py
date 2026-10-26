import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

from services import provider_api
from services.catalog import UNEXPECTED_ANSWER_MESSAGE, UNREACHABLE_MESSAGE
from services.config_manager import ConfigManager
from services.provider_manager import ProviderManager, StbHandshake
from services.workers import STB_DEVICE_REJECTED_MESSAGE, ProviderSetupWorker, STBCategoriesWorker

PREHASH = "2614ddf9829ba9d284f389d88e8c669d81f6a5c2"


class _Response:
    status_code = 200

    def __init__(self, body, status_code=None):
        self._body = body
        if status_code is not None:
            self.status_code = status_code

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _accepting(url, **kwargs):
    if "action=handshake" in url:
        return _Response({"js": {"token": "TOKEN"}})
    return _Response({"js": {"id": 1, "name": "Fixture"}})


def _raising(error):
    def fake_get(url, **kwargs):
        raise error

    return fake_get


class StbHandshakeTests(unittest.TestCase):
    def test_profile_request_sends_plain_prehash(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if "action=handshake" in url:
                return _Response({"js": {"token": "TOKEN"}})
            return _Response({"js": {"id": 1, "name": "Fixture"}})

        with patch("services.provider_manager.requests.get", side_effect=fake_get):
            self.assertTrue(StbHandshake().run("http://portal.test", "00:1A:79:00:00:01"))

        profile = next(url for url in calls if "action=get_profile" in url)
        self.assertEqual(parse_qs(urlsplit(profile).query)["prehash"], [PREHASH])

    def test_prefer_https_falls_back_to_http_on_an_http_only_portal(self):
        # Neither portal path answers over HTTPS; the portal answers over HTTP at either one.
        for path in ("/portal.php", "/server/load.php"):
            with self.subTest(path=path):
                calls = []

                def fake_get(url, **kwargs):
                    calls.append(urlsplit(url)._replace(query="").geturl())
                    if url.startswith("https://"):
                        raise requests.exceptions.SSLError("wrong version number")
                    if path not in url:
                        return _Response({}, status_code=404)
                    return _accepting(url)

                handshake = StbHandshake(prefer_https=True)
                with (
                    patch.object(provider_api, "_http_only", set()),
                    patch("services.provider_manager.requests.get", side_effect=fake_get),
                ):
                    self.assertTrue(handshake.run("http://portal.test", "00:1A:79:00:00:01"))
                self.assertIsNone(handshake.failure)
                self.assertEqual(
                    calls[:3],
                    [
                        "https://portal.test/portal.php",
                        "https://portal.test/server/load.php",
                        "http://portal.test/portal.php",
                    ],
                )
                self.assertTrue(calls[-1].startswith(f"http://portal.test{path}"), calls)

    def test_a_portal_signed_in_over_http_is_asked_over_http_from_then_on(self):
        # 2.7.1: its lists, links and guide too, and the next sign-in asks no HTTPS
        calls = []

        def fake_get(url, **kwargs):
            calls.append(urlsplit(url).scheme)
            if url.startswith("https://"):
                raise requests.exceptions.SSLError("wrong version number")
            if "action=get_genres" in url:
                return _Response({"js": [{"id": "1", "title": "News"}]})
            if "action=get_all_channels" in url:
                return _Response({"js": {"data": []}})
            return _accepting(url)

        url = "http://portal.test/c/"
        with (
            patch.object(provider_api, "_http_only", set()),
            patch("services.provider_manager.requests.get", side_effect=fake_get),
            patch("services.workers.requests.get", side_effect=fake_get),
        ):
            handshake = StbHandshake(prefer_https=True)
            self.assertTrue(handshake.run(url, "00:1A:79:00:00:01"))
            calls.clear()
            worker = STBCategoriesWorker(url, handshake.headers, "itv", prefer_https=True)
            done = []
            worker.finished.connect(done.append)
            worker.run()
            self.assertEqual(len(done), 1)
            self.assertTrue(StbHandshake(prefer_https=True).run(url, "00:1A:79:00:00:01"))
        self.assertEqual(set(calls), {"http"})

    def test_a_large_live_list_gets_time_and_a_second_try(self):  # 2.8
        # A portal's whole live list (tens of thousands of channels) can take longer than 10 s.
        asked = []

        def fake_get(url, **kwargs):
            if "action=get_genres" in url:
                return _Response({"js": [{"id": "1", "title": "News"}]})
            if "action=get_all_channels" in url:
                asked.append(kwargs.get("timeout"))
                if len(asked) == 1:
                    raise requests.Timeout("read timed out")
                return _Response({"js": {"data": [{"id": "7", "tv_genre_id": "1"}]}})
            return _accepting(url)

        with patch("services.workers.requests.get", side_effect=fake_get):
            worker = STBCategoriesWorker("http://portal.test/c/", {}, "itv")
            done = []
            worker.finished.connect(done.append)
            worker.run()
        self.assertEqual(asked, [30, 30])
        self.assertEqual(len(done[0]["contents"]), 1)

    def test_prefer_https_failing_everywhere_warns_once(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            raise requests.ConnectionError("Connection refused")

        handshake = StbHandshake(prefer_https=True)
        with (
            patch("services.provider_manager.requests.get", side_effect=fake_get),
            self.assertLogs("services.provider_manager", "WARNING") as logs,
        ):
            self.assertFalse(handshake.run("http://portal.test", "00:1A:79:00:00:01"))
        self.assertEqual(len(calls), 4)
        self.assertEqual(len([r for r in logs.records if r.levelname == "WARNING"]), 1)

    def test_headers_name_the_portals_host_and_refer_to_the_url_as_given(self):
        for url, host, referer in (
            ("http://portal.example:8080", "portal.example:8080", "http://portal.example:8080/c/"),
            (
                "http://portal.example:8080/stalker_portal/c",
                "portal.example:8080",
                "http://portal.example:8080/stalker_portal/c/",
            ),
            (
                "https://user@portal.example/c/",
                "user@portal.example",
                "https://user@portal.example/c//",
            ),
            ("portal.example:8080", "", "portal.example:8080/"),
        ):
            with self.subTest(url=url):
                headers = StbHandshake.create_headers(url, "00:1A:79:00:00:01", "TOKEN")
                self.assertEqual((headers["Host"], headers["Referer"]), (host, referer))


class ProviderSetupWorkerTests(unittest.TestCase):
    """A rejected STB handshake is a setup failure, not a ready provider."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-setup-")
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"QITV_CONFIG_DIR": directory.name})
        env.start()
        self.addCleanup(env.stop)
        self.manager = ProviderManager(ConfigManager())
        self.addCleanup(self.manager._cache_writer.shutdown, wait=True)
        self.manager.providers = [
            {"type": "STB", "name": "S", "url": "http://portal.test", "mac": "00:1A:79:00:00:01"}
        ]
        self.manager.config_manager.selected_provider_name = "S"

    def run_setup(self, fake_get):
        worker = ProviderSetupWorker(self.manager)
        outcome = []
        worker.finished.connect(lambda payload: outcome.append(("ok", payload)))
        worker.error.connect(lambda message: outcome.append(("error", message)))
        with patch("services.provider_manager.requests.get", side_effect=fake_get):
            worker.run()
        return outcome

    def test_rejected_handshake_reports_the_device_message(self):
        class Refused(_Response):
            status_code = 403

        with self.assertLogs("services.provider_manager", "WARNING"):
            outcome = self.run_setup(lambda url, **kwargs: Refused({}))
        self.assertEqual(outcome, [("error", STB_DEVICE_REJECTED_MESSAGE)])
        self.assertEqual(
            STB_DEVICE_REJECTED_MESSAGE,
            "The provider rejected this device (check the MAC address or subscription)",
        )

    def test_accepted_handshake_is_ready(self):
        self.assertEqual(self.run_setup(_accepting), [("ok", {"name": "S"})])

    def assertSetupFails(self, fake_get, message):
        with self.assertLogs("services.provider_manager", "WARNING"):
            outcome = self.run_setup(fake_get)
        self.assertEqual(outcome, [("error", message)])

    def test_unreachable_portal_is_not_a_rejected_device(self):
        # DNS, connection and timeout failures, and server errors, say the portal could not
        # be reached; they say nothing about the MAC address.
        cases = {
            "dns": _raising(requests.ConnectionError("Failed to resolve 'portal.test'")),
            "refused": _raising(requests.ConnectionError("Connection refused")),
            "timeout": _raising(requests.ReadTimeout("Read timed out")),
            "connect timeout": _raising(requests.ConnectTimeout("Connect timed out")),
            "5xx": lambda url, **kwargs: _Response({}, status_code=503),
        }
        for name, fake_get in cases.items():
            with self.subTest(name):
                self.assertSetupFails(fake_get, UNREACHABLE_MESSAGE)

    def test_portal_refusal_or_missing_token_is_a_rejected_device(self):
        def blocked(url, **kwargs):
            if "action=handshake" in url:
                return _Response({"js": {"token": "TOKEN"}})
            return _Response({"js": {"id": None, "name": None}})

        cases = {
            "403": lambda url, **kwargs: _Response({}, status_code=403),
            "401": lambda url, **kwargs: _Response({}, status_code=401),
            "no token": lambda url, **kwargs: _Response({"js": {}}),
            "blocked profile": blocked,
        }
        for name, fake_get in cases.items():
            with self.subTest(name):
                self.assertSetupFails(fake_get, STB_DEVICE_REJECTED_MESSAGE)

    def test_other_answers_are_unexpected(self):
        not_json = ValueError("Expecting value: line 1 column 1 (char 0)")
        cases = {
            "404": lambda url, **kwargs: _Response({}, status_code=404),
            "not json": lambda url, **kwargs: _Response(not_json),
        }
        for name, fake_get in cases.items():
            with self.subTest(name):
                self.assertSetupFails(fake_get, UNEXPECTED_ANSWER_MESSAGE)

    def test_a_refusal_on_either_portal_path_wins(self):
        # The portal refuses at /portal.php; /server/load.php does not exist there.
        def fake_get(url, **kwargs):
            return _Response({}, status_code=403 if "/portal.php" in url else 404)

        self.assertSetupFails(fake_get, STB_DEVICE_REJECTED_MESSAGE)


class FailedSetupSessionTests(unittest.TestCase):
    """A setup that fails part-way never leaves the previous portal's session behind."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-setup-")
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"QITV_CONFIG_DIR": directory.name})
        env.start()
        self.addCleanup(env.stop)
        self.manager = ProviderManager(ConfigManager())
        self.addCleanup(self.manager._cache_writer.shutdown, wait=True)
        self.progress = type("Progress", (), {"emit": staticmethod(lambda message: None)})()

    def test_setup_failing_before_the_handshake_drops_the_previous_session(self):
        manager = self.manager
        manager.providers = [
            {"type": "STB", "name": "A", "url": "http://a.test/c/", "mac": "00:1A:79:AA:AA:AA"},
            {"type": "STB", "name": "B", "url": "http://b.test/c/"},  # malformed: no mac
        ]
        manager.config_manager.selected_provider_name = "A"
        with patch("services.provider_manager.requests.get", side_effect=_accepting):
            self.assertTrue(manager.set_current_provider(self.progress))
        self.assertIn("00:1A:79:AA:AA:AA", manager.headers["Cookie"])
        self.assertEqual(manager.token, "TOKEN")

        manager.config_manager.selected_provider_name = "B"
        with self.assertRaises(KeyError):
            manager.set_current_provider(self.progress)
        self.assertEqual(manager.headers, {})
        self.assertEqual(manager.token, "")


if __name__ == "__main__":
    unittest.main()
