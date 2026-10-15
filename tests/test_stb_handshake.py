import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from provider_manager import StbHandshake

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


if __name__ == "__main__":
    unittest.main()
