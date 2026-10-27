import unittest
from unittest.mock import patch
from urllib.parse import quote

from services import provider_api
from services.provider_api import (
    https_fallback_candidates,
    known_http_only,
    portal_http_only,
    remember_http_only,
    stb_command_has_number,
    stb_command_link,
    stb_command_shape,
    stb_commands_summary,
    stb_create_link_url,
    stb_endpoint,
    stb_link_names_no_stream,
    stb_link_shape,
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

    def test_an_stb_portal_that_answered_only_over_http_says_so_for_its_links(self):  # 2.8.1
        portal = {"type": "STB", "url": "http://portal.example:8080/c/"}
        self.assertFalse(portal_http_only(portal))
        remember_http_only(portal["url"])
        self.assertTrue(portal_http_only(portal))
        self.assertTrue(portal_http_only({**portal, "type": "stb"}))
        self.assertFalse(portal_http_only({**portal, "type": "M3UPLAYLIST"}))
        self.assertFalse(portal_http_only({"type": "STB"}))

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


class StbLinkShapeTests(unittest.TestCase):  # 2.8.1: what a portal's link is, in the log
    PORTAL = "http://portal.example:8080/c/"

    def test_a_link_is_described_without_its_host_or_any_value(self):
        link = (
            "http://portal.example:8080/play/live.php"
            "?mac=00:1A:79:12:34:56&stream=4711&extension=ts&play_token=Zq9x"
        )
        shape = stb_link_shape(link, self.PORTAL)
        self.assertEqual(
            shape,
            "http, the portal's host, port 8080, a path of 2 parts ending in .php, "
            "asking mac, stream, extension, play_token",
        )
        for secret in ("portal.example", "00:1A", "4711", "Zq9x", "live"):
            self.assertNotIn(secret, shape)

    def test_another_host_its_default_port_and_empty_values_are_named(self):
        shape = stb_link_shape("https://cdn.example/play/live.php?stream=&play_token=", self.PORTAL)
        self.assertEqual(
            shape,
            "https, another host, its default port, a path of 2 parts ending in .php, "
            "asking stream, play_token (empty: stream, play_token)",
        )

    def test_a_local_host_and_a_path_without_extension(self):
        self.assertEqual(
            stb_link_shape("http://localhost/ch/1_", self.PORTAL),
            "http, this computer (localhost), its default port, "
            "a path of 2 parts without an extension, asking nothing",
        )

    def test_what_is_no_address_is_said_so(self):
        for link in ("auto", "", "/media/1.mpg"):
            with self.subTest(link=link):
                self.assertEqual(stb_link_shape(link, self.PORTAL), "no address")


class StbCommandShapeTests(unittest.TestCase):  # 2.8.3: a portal's commands, in the log
    PORTAL = "http://portal.example:8080/c/"

    def test_a_command_is_its_players_word_its_address_and_whether_it_holds_a_number(self):
        self.assertEqual(
            stb_command_shape("ffrt http://localhost/ch/4711_", self.PORTAL),
            "ffrt http, this computer (localhost), its default port, "
            "a path of 2 parts without an extension, asking nothing, a number in it",
        )
        self.assertEqual(
            stb_command_shape("ffmpeg http://localhost/ch/", self.PORTAL),
            "ffmpeg http, this computer (localhost), its default port, "
            "a path of 1 part without an extension, asking nothing, no number in it",
        )

    def test_an_empty_command_and_a_word_of_its_own_say_nothing_of_it(self):
        for cmd in ("", "  "):
            with self.subTest(cmd=cmd):
                self.assertEqual(stb_command_shape(cmd, self.PORTAL), "empty")
        shape = stb_command_shape("Zq9x http://localhost/ch/1_", self.PORTAL)
        self.assertTrue(shape.startswith("a word http, "))
        self.assertNotIn("Zq9x", shape)

    def test_a_list_is_counted_with_one_command_without_a_number_shown(self):
        channels = [
            {"cmd": "ffmpeg http://localhost/ch/1_"},
            {"cmd": "ffmpeg http://localhost/ch/"},
            {"cmd": ""},
            "not a channel",
        ]
        self.assertEqual(
            stb_commands_summary(channels, self.PORTAL),
            "3 channels, 1 with a number in its command; one without: ffmpeg http, this "
            "computer (localhost), its default port, a path of 1 part without an extension, "
            "asking nothing, no number in it",
        )
        self.assertEqual(
            stb_commands_summary([{"cmd": "ffrt http://localhost/ch/2_"}], self.PORTAL),
            "1 channel, 1 with a number in its command",
        )

    def test_a_play_link_names_its_number_in_its_stream(self):  # 2.8.4
        listed = "ffmpeg http://portal.example/play/live.php?mac=00:1A&stream=4711&play_token=x"
        self.assertTrue(stb_command_has_number(listed))
        self.assertTrue(stb_command_has_number("ffrt http://localhost/ch/4711_"))
        for cmd in ("ffmpeg http://portal.example/play/live.php?mac=00:1A:79&stream=", ""):
            with self.subTest(cmd=cmd):
                self.assertFalse(stb_command_has_number(cmd))

    def test_a_commands_own_link_is_one_on_a_server_naming_what_it_plays(self):  # 2.8.4
        listed = "http://portal.example/play/live.php?mac=00:1A&stream=4711&play_token=x"
        self.assertEqual(stb_command_link(f"ffmpeg {listed}", self.PORTAL), listed)
        self.assertEqual(stb_command_link(listed, self.PORTAL), listed)
        for cmd in (
            "ffrt http://localhost/ch/4711_",  # a box's placeholder: the portal makes its link
            "ffmpeg http://127.0.0.1/ch/4711_",
            "ffmpeg http://portal.example/play/live.php?mac=00:1A&stream=&play_token=x",
            "auto /media/4711.mpg",
            "",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(stb_command_link(cmd, self.PORTAL), "")

    def test_a_portal_on_this_computer_has_its_own_links_there(self):
        listed = "http://127.0.0.1:8080/play/live.php?mac=00:1A&stream=4711&play_token=x"
        self.assertEqual(stb_command_link(listed, "http://127.0.0.1:8080/c/"), listed)

    def test_a_link_asking_an_empty_stream_names_nothing_to_play(self):
        names_none = "http://p.example/play/live.php?mac=00:1A&stream=&extension=ts&play_token=x"
        self.assertTrue(stb_link_names_no_stream(names_none))
        for link in (
            "http://p.example/play/live.php?mac=00:1A&stream=4711&extension=ts&play_token=x",
            "http://p.example/live/4711.ts?play_token=",
            "",
        ):
            with self.subTest(link=link):
                self.assertFalse(stb_link_names_no_stream(link))


if __name__ == "__main__":
    unittest.main()
