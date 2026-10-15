import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from config_manager import ConfigManager
from provider_manager import ProviderManager, StbHandshake
from workers import STB_DEVICE_REJECTED_MESSAGE, ProviderSetupWorker

PREHASH = "2614ddf9829ba9d284f389d88e8c669d81f6a5c2"


class _Response:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


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
        def fake_get(url, **kwargs):
            if "action=handshake" in url:
                return _Response({"js": {"token": "TOKEN"}})
            return _Response({"js": {"id": 1, "name": "Fixture"}})

        self.assertEqual(self.run_setup(fake_get), [("ok", {"name": "S"})])


if __name__ == "__main__":
    unittest.main()
