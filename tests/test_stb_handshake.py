import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

from config_manager import ConfigManager
from provider_manager import ProviderManager, StbHandshake
from services.catalog import UNEXPECTED_ANSWER_MESSAGE, UNREACHABLE_MESSAGE
from workers import STB_DEVICE_REJECTED_MESSAGE, ProviderSetupWorker

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

        with patch("provider_manager.requests.get", side_effect=fake_get):
            self.assertTrue(StbHandshake().run("http://portal.test", "00:1A:79:00:00:01"))

        profile = next(url for url in calls if "action=get_profile" in url)
        self.assertEqual(parse_qs(urlsplit(profile).query)["prehash"], [PREHASH])


class ProviderSetupWorkerTests(unittest.TestCase):
    """Finding I7: a rejected STB handshake is a setup failure, not a ready provider."""

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
        with patch("provider_manager.requests.get", side_effect=fake_get):
            worker.run()
        return outcome

    def test_rejected_handshake_reports_the_device_message(self):
        class Refused(_Response):
            status_code = 403

        with self.assertLogs("provider_manager", "WARNING"):
            outcome = self.run_setup(lambda url, **kwargs: Refused({}))
        self.assertEqual(outcome, [("error", STB_DEVICE_REJECTED_MESSAGE)])
        self.assertEqual(
            STB_DEVICE_REJECTED_MESSAGE,
            "The provider rejected this device (check the MAC address or subscription)",
        )

    def test_accepted_handshake_is_ready(self):
        self.assertEqual(self.run_setup(_accepting), [("ok", {"name": "S"})])

    def assertSetupFails(self, fake_get, message):
        with self.assertLogs("provider_manager", "WARNING"):
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
        with patch("provider_manager.requests.get", side_effect=_accepting):
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
