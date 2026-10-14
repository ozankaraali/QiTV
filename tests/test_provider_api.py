import unittest
from urllib.parse import quote

from urlobject import URLObject

from services.provider_api import (
    https_fallback_candidates,
    stb_create_link_url,
    stb_endpoint,
    stb_portal_base,
)

PORTAL_URLS = [
    "http://portal.example:8080/c/",
    "https://portal.example/stalker_portal/c/",
    "HTTP://Portal.Example:80/c/index.html?x=1#frag",
    "http://user:pass@portal.example:8000",
    " http://portal.example/c/\r\n",
    "portal.example:8080/c/",
    "",
]


def legacy_portal_base(url, prefer_https):
    # The URLObject-based expression these helpers replaced in the workers and mixins.
    url = URLObject(url)
    scheme = url.scheme
    if prefer_https and scheme == "http":
        scheme = "https"
    return f"{scheme}://{url.netloc}"


class StbUrlTests(unittest.TestCase):
    def test_portal_base_matches_previous_urlobject_parsing(self):
        for url in PORTAL_URLS:
            for prefer_https in (False, True):
                with self.subTest(url=url, prefer_https=prefer_https):
                    self.assertEqual(
                        stb_portal_base(url, prefer_https), legacy_portal_base(url, prefer_https)
                    )

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


if __name__ == "__main__":
    unittest.main()
