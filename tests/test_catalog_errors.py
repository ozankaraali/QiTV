"""Finding I5: failure messages leave the catalog without URLs or credentials."""

import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from services.catalog import (
    LOGIN_REFUSED_MESSAGE,
    PROVIDER_LOADING_MESSAGE,
    TOO_SLOW_MESSAGE,
    UNEXPECTED_ANSWER_MESSAGE,
    UNREACHABLE_MESSAGE,
    friendly_error,
)
from services.navigation import NavModel
from tests.test_catalog_service import collect, make_service

SECRET_URL = "http://portal.example:8080/player_api.php?username=alice&password=s3cret"
REFUSED = (
    "HTTPConnectionPool(host='portal.example', port=8080): Max retries exceeded with url: "
    "/player_api.php?username=alice&password=s3cret (Caused by NewConnectionError("
    "\"HTTPConnection(host='portal.example', port=8080): Failed to establish a new "
    'connection: [Errno 61] Connection refused"))'
)
DNS = (
    "HTTPConnectionPool(host='portal.example', port=80): Max retries exceeded with url: "
    "/player_api.php?username=alice&password=s3cret (Caused by NameResolutionError("
    "\"HTTPConnection(host='portal.example', port=80): Failed to resolve 'portal.example' "
    '([Errno 8] nodename nor servname provided, or not known)"))'
)
CONNECT_TIMEOUT = (
    "HTTPConnectionPool(host='portal.example', port=80): Max retries exceeded with url: "
    "/player_api.php?username=alice&password=s3cret (Caused by ConnectTimeoutError("
    "<HTTPConnection(host='portal.example', port=80) at 0x10>, 'Connection to "
    "portal.example timed out. (connect timeout=10)'))"
)
SAMPLES = {
    REFUSED: UNREACHABLE_MESSAGE,
    DNS: UNREACHABLE_MESSAGE,
    "Cannot connect to host portal.example:80 ssl:default [Connection refused]": (
        UNREACHABLE_MESSAGE
    ),
    "[Errno 2] No such file or directory: '/home/alice/list.m3u'": UNREACHABLE_MESSAGE,
    CONNECT_TIMEOUT: TOO_SLOW_MESSAGE,
    f"HTTPConnectionPool(host='portal.example', port=80): Read timed out. ({SECRET_URL})": (
        TOO_SLOW_MESSAGE
    ),
    "": TOO_SLOW_MESSAGE,
    f"401 Client Error: Unauthorized for url: {SECRET_URL}": LOGIN_REFUSED_MESSAGE,
    f"403 Client Error: Forbidden for url: {SECRET_URL}": LOGIN_REFUSED_MESSAGE,
    "Failed to fetch handshake: 403": LOGIN_REFUSED_MESSAGE,
    f"404 Client Error: Not Found for url: {SECRET_URL}": UNEXPECTED_ANSWER_MESSAGE,
    "Expecting value: line 1 column 1 (char 0)": UNEXPECTED_ANSWER_MESSAGE,
    "'js'": UNEXPECTED_ANSWER_MESSAGE,
    f"Unexpected answer from {SECRET_URL}": UNEXPECTED_ANSWER_MESSAGE,
}


class FriendlyErrorTests(unittest.TestCase):
    def test_worker_texts_map_to_fixed_messages(self):
        for text, expected in SAMPLES.items():
            with self.subTest(text=text[:60]):
                self.assertEqual(friendly_error(text), expected)

    def test_own_messages_pass_through(self):
        for text in (
            PROVIDER_LOADING_MESSAGE,
            "This provider has no series",
            "The portal returned an empty link",
            "The portal returned no data",
            "The background task failed",
            "Unknown section: bogus",
            "The provider rejected this device (check the MAC address or subscription)",
            UNREACHABLE_MESSAGE,
        ):
            self.assertEqual(friendly_error(text), text)

    def test_log_names_the_exception_type_without_the_url(self):
        with self.assertLogs("services.catalog", "INFO") as logs:
            friendly_error(REFUSED)
        output = "\n".join(logs.output)
        self.assertIn("NewConnectionError", output)
        for secret in ("alice", "s3cret", "portal.example", "player_api"):
            self.assertNotIn(secret, output)


class FailureSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def assert_safe(self, emitted):
        self.assertTrue(emitted)
        text = repr(emitted)
        for secret in ("alice", "s3cret", "portal.example", "http"):
            self.assertNotIn(secret, text)

    def test_every_failure_signal_carries_a_fixed_message(self):
        xtream = {"name": "X", "type": "XTREAM", "url": SECRET_URL}
        service, providers, factory, _ = make_service(xtream)
        for name in ("provider_setup", "xtream", "xtream_series"):
            factory.results[name] = {"fail": REFUSED}
        provider_failed = collect(service.providerFailed)
        section_failed = collect(service.sectionFailed)
        seasons_failed = collect(service.seasonsFailed)
        with self.assertLogs("services.catalog", "INFO"):
            service.select_provider()
            service.request_section("live")
            service.request_seasons({"id": 1, "name": "Show"})
        self.assertEqual(provider_failed, [("X", UNREACHABLE_MESSAGE)])
        self.assertEqual(section_failed, [("live", UNREACHABLE_MESSAGE)])
        self.assertEqual(seasons_failed, [("1", UNREACHABLE_MESSAGE)])

        stb = {"name": "S", "type": "STB", "url": "http://portal.example/c/"}
        service, providers, factory, _ = make_service(stb)
        providers.current_provider_content["vod"] = {"categories": [], "contents": {}}
        factory.results["stb_list"] = {
            "fail": f"401 Client Error: Unauthorized for url: {SECRET_URL}"
        }
        factory.results["link"] = {"fail": CONNECT_TIMEOUT}
        group_failed, episodes_failed = collect(service.groupFailed), collect(
            service.episodesFailed
        )
        link_failed = collect(service.linkFailed)
        with self.assertLogs("services.catalog", "INFO"):
            service.request_group("movies", "5")
            service.request_episodes({"id": 9, "name": "Show"}, {"id": "9:1"})
            link_id = service.request_link({"cmd": "c"}, section="live")
        self.assertEqual(group_failed, [("movies", "5", LOGIN_REFUSED_MESSAGE)])
        self.assertEqual(episodes_failed, [("9", "9:1", LOGIN_REFUSED_MESSAGE)])
        self.assertEqual(link_failed, [(link_id, TOO_SLOW_MESSAGE)])
        for emitted in (group_failed, episodes_failed, link_failed):
            self.assert_safe(emitted)

    def test_epg_failure_log_has_no_url(self):
        service, _, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h"})
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"fail": REFUSED}
        with self.assertLogs("services.catalog", "INFO") as logs:
            service.select_provider()
        output = "\n".join(logs.output)
        self.assertIn(UNREACHABLE_MESSAGE, output)
        self.assertNotIn("s3cret", output)

    def test_navigation_log_omits_the_message(self):
        service, _, _, _ = make_service({"name": "X", "type": "XTREAM", "url": "h"})
        nav = NavModel(service, SimpleNamespace(favorites=[], watch_history=[]))
        with self.assertLogs("services.navigation", "INFO") as logs:
            service.sectionFailed.emit("live", "secret text")
        self.assertNotIn("secret text", "\n".join(logs.output))
        self.assertEqual(nav.load_error(_section()), "secret text")


def _section():
    from services.navigation import NavNode

    return NavNode("section", "live", "Live TV", "live")


if __name__ == "__main__":
    unittest.main()
