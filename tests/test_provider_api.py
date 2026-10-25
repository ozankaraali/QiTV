import unittest
from unittest.mock import patch
from urllib.parse import quote

from services import provider_api
from services.provider_api import (
    https_fallback_candidates,
    known_http_only,
    remember_http_only,
    stb_create_link_url,
    stb_endpoint,
    stb_portal_base,
    xtream_https_bases,
    xtream_signed_in,
)

# A portal URL, its base, and its base when HTTPS is preferred: as the URLObject parsing these
# helpers replaced in the workers and mixins gave them.
PORTAL_BASES = [
    ("http://portal.example:8080/c/", "http://portal.example:8080", "https://portal.example:8080"),
    (
        "https://portal.example/stalker_portal/c/",
        "https://portal.example",
        "https://portal.example",
    ),
    (
        "HTTP://Portal.Example:80/c/index.html?x=1#frag",
        "http://Portal.Example:80",
        "https://Portal.Example:80",
    ),
    (
        "http://user:pass@portal.example:8000",
        "http://user:pass@portal.example:8000",
        "https://user:pass@portal.example:8000",
    ),
    (" http://portal.example/c/\r\n", "http://portal.example", "https://portal.example"),
    ("portal.example:8080/c/", "portal.example://", "portal.example://"),
    ("", "://", "://"),
]


class StbUrlTests(unittest.TestCase):
    def test_portal_base_matches_previous_urlobject_parsing(self):
        for url, plain, https in PORTAL_BASES:
            for prefer_https, expected in ((False, plain), (True, https)):
                with self.subTest(url=url, prefer_https=prefer_https):
                    self.assertEqual(stb_portal_base(url, prefer_https), expected)

    def test_create_link_url_keeps_wire_format(self):
        base = "https://portal.example:8080"
        cmd = "ffmpeg http://localhost/ch/1234_ & more"
        self.assertEqual(
            stb_create_link_url(base, "itv", cmd),
            f"{base}/server/load.php?type=itv&action=create_link"
            f"&cmd={quote(cmd)}&JsHttpRequest=1-xml",
        )
        # Episodes of a series are requested as VOD with their episode number.
        self.assertEqual(
            stb_create_link_url(base, "series", cmd, is_episode=True, series=3),
            f"{base}/server/load.php?type=vod&action=create_link"
            f"&cmd={quote(cmd)}&series=3&JsHttpRequest=1-xml",
        )
        self.assertEqual(
            stb_create_link_url(base, "vod", cmd, is_episode=True, series=None),
            f"{base}/server/load.php?type=vod&action=create_link"
            f"&cmd={quote(cmd)}&series=None&JsHttpRequest=1-xml",
        )
        self.assertEqual(stb_endpoint(base), f"{base}/server/load.php")

    def test_https_fallback_candidates(self):
        self.assertEqual(
            https_fallback_candidates("http://h/x", True), ["https://h/x", "http://h/x"]
        )
        self.assertEqual(https_fallback_candidates("http://h/x", False), ["http://h/x"])
        self.assertEqual(https_fallback_candidates("https://h/x", True), ["https://h/x"])


class XtreamSignInTests(unittest.TestCase):
    def test_an_active_account_is_signed_in(self):
        for auth in (1, "1"):
            body = {"user_info": {"auth": auth, "status": "Active"}, "server_info": {}}
            self.assertTrue(xtream_signed_in(body))
        self.assertTrue(xtream_signed_in({"user_info": {"auth": 1}}))  # no status given

    def test_refusals_as_panels_send_them(self):
        for body in (None, [], {}, {"user_info": {"auth": 0}}, {"user_info": []}, "nope"):
            self.assertFalse(xtream_signed_in(body), body)

    def test_an_account_that_cannot_watch_is_refused(self):
        for status in ("Expired", "Banned", "Disabled"):
            self.assertFalse(xtream_signed_in({"user_info": {"auth": 1, "status": status}}))


class HttpsChoiceTests(unittest.TestCase):
    """2.7.1: HTTPS is chosen by what answers; a host that answered only over HTTP is asked
    over HTTP for the rest of the run."""

    def setUp(self):
        memory = patch.object(provider_api, "_http_only", set())
        memory.start()
        self.addCleanup(memory.stop)

    def test_a_host_that_answered_only_over_http_is_asked_over_http(self):
        url = "http://portal.example:8080/c/"
        self.assertEqual(
            https_fallback_candidates(url, True), ["https://portal.example:8080/c/", url]
        )
        self.assertEqual(stb_portal_base(url, True), "https://portal.example:8080")
        remember_http_only(url)
        self.assertTrue(known_http_only("http://Portal.Example:8080/live/1.ts"))
        self.assertFalse(known_http_only("http://portal.example:8081/c/"))
        self.assertFalse(known_http_only("https://portal.example:8080/c/"))
        self.assertEqual(https_fallback_candidates(url, True), [url])
        self.assertEqual(stb_portal_base(url, True), "http://portal.example:8080")

    def test_the_default_port_is_one_host_written_with_or_without_it(self):
        remember_http_only("http://panel.example")
        self.assertTrue(known_http_only("http://panel.example:80/live/u/p/1.ts"))

    def test_xtream_https_bases_are_the_advertised_port_then_the_addresss_own(self):
        for base, info, expected in (
            ("http://panel.example", {"https_port": "443"}, ["https://panel.example"]),
            ("http://panel.example:80", {"https_port": "0"}, ["https://panel.example"]),
            ("http://panel.example:8000", {"https_port": ""}, ["https://panel.example:8000"]),
            (
                "http://panel.example:8080",
                {"https_port": "8443"},
                ["https://panel.example:8443", "https://panel.example:8080"],
            ),
            ("http://panel.example/xc", {"https_port": 443}, ["https://panel.example/xc"]),
            ("http://panel.example", {"https_port": "x"}, ["https://panel.example"]),
            ("http://panel.example", None, ["https://panel.example"]),
            ("https://panel.example", {"https_port": "443"}, []),
        ):
            with self.subTest(base=base, info=info):
                self.assertEqual(xtream_https_bases(base, info), expected)


if __name__ == "__main__":
    unittest.main()
