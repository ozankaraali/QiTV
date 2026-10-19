import unittest
from urllib.parse import quote

from services.provider_api import (
    https_fallback_candidates,
    stb_create_link_url,
    stb_endpoint,
    stb_portal_base,
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


if __name__ == "__main__":
    unittest.main()
