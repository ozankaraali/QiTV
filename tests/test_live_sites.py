import json
import os
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from http_fixtures import LocalSource

from services import live_sites
from services.live_sites import (
    KICK,
    TWITCH,
    SiteError,
    SitePage,
    resolve,
    site_page,
)

JSON = {"Content-Type": "application/json"}
TOKEN = '{"channel":"examplechannel","expires":1}'
SIGNATURE = "0123abcd"
MASTER = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlow.m3u8\n"


class Routes(dict):
    """Routes keyed by path; the query (the token, a random number) is not part of the key."""

    def get(self, path, default=None):
        return super().get(path.split("?")[0], default)


def kick_body(live=True, playback="https://example.invalid/master.m3u8?token=SECRETJWT"):
    return json.dumps(
        {"playback_url": playback, "livestream": {"is_live": True} if live else None}
    ).encode()


class SitePageTests(unittest.TestCase):
    def test_site_page_recognizes_channel_pages(self):
        for site, host in ((KICK, "kick.com"), (TWITCH, "twitch.tv")):
            for url in (
                f"{host}/ExampleChannel",
                f"https://www.{host}/examplechannel/",
                f"http://m.{host}/examplechannel?x=1",
            ):
                self.assertEqual(site_page(url), SitePage(site, "examplechannel"), url)

    def test_site_page_rejects_other_pages(self):
        for url in (
            "kick.com/",
            "twitch.tv/directory",
            "twitch.tv/videos/123",
            "kick.com/examplechannel/videos",
            "twitch.tv/examplechannel/videos",
            "kick.com/categories",
            "twitch.tv/popout",
            "example.com/x",
            "ftp://kick.com/examplechannel",
            "notkick.com/examplechannel",
            "",
        ):
            self.assertIsNone(site_page(url), url)


class KickTests(unittest.TestCase):
    def serve(self, route):
        source = LocalSource(Routes({"/api/v2/channels/examplechannel": route}))
        self.addCleanup(source.close)
        return source

    def resolve(self, source):
        return resolve(SitePage(KICK, "examplechannel"), kick_api=source.url + "/api/v2/channels")

    def reason(self, source):
        with self.assertRaises(SiteError) as caught:
            self.resolve(source)
        return caught.exception.reason

    def test_kick_live(self):
        source = self.serve((200, JSON, kick_body()))
        resolved = self.resolve(source)
        self.assertEqual(resolved.url, "https://example.invalid/master.m3u8?token=SECRETJWT")
        self.assertEqual(resolved.name, "examplechannel · Kick")

    def test_kick_offline(self):
        self.assertEqual(self.reason(self.serve((200, JSON, kick_body(live=False)))), "offline")
        body = json.dumps({"playback_url": "x", "livestream": {"is_live": False}}).encode()
        self.assertEqual(self.reason(self.serve((200, JSON, body))), "offline")

    def test_kick_unknown(self):
        body = b'{"message":"Channel not found."}'
        self.assertEqual(self.reason(self.serve((404, JSON, body))), "unknown")

    def test_kick_refused(self):
        for status, body in (
            (403, b"no"),
            (429, b"slow"),
            (503, b"busy"),
            (200, b"<html>Just a moment...</html>"),
        ):
            self.assertEqual(self.reason(self.serve((status, {}, body))), "refused", status)

    def test_kick_other_status_is_network(self):
        self.assertEqual(self.reason(self.serve((500, {}, b"oops"))), "network")

    def test_kick_network(self):
        source = self.serve((200, JSON, kick_body()))
        source.close()  # nothing listens at the address any more
        self.assertEqual(self.reason(source), "network")


class TwitchTests(unittest.TestCase):
    def serve(self, gql=None, usher=None):
        self.gql_requests = []

        def default_gql(handler, count):
            self.gql_requests.append((dict(handler.headers), handler.body))
            grant = {
                "data": {"streamPlaybackAccessToken": {"value": TOKEN, "signature": SIGNATURE}}
            }
            return 200, JSON, json.dumps(grant).encode()

        source = LocalSource(
            Routes(
                {
                    "/gql": gql or default_gql,
                    "/hls/examplechannel.m3u8": usher
                    or (200, {"Content-Type": "application/vnd.apple.mpegurl"}, MASTER),
                }
            )
        )
        self.addCleanup(source.close)
        return source

    def resolve(self, source):
        return resolve(
            SitePage(TWITCH, "examplechannel"),
            twitch_gql=source.url + "/gql",
            twitch_usher=source.url + "/hls",
        )

    def reason(self, source):
        with self.assertRaises(SiteError) as caught:
            self.resolve(source)
        return caught.exception.reason

    def test_twitch_live(self):
        source = self.serve()
        resolved = self.resolve(source)
        self.assertEqual(resolved.name, "examplechannel · Twitch")
        headers, raw = self.gql_requests[0]
        self.assertEqual(headers["Client-ID"], "kimne78kx3ncx6brgo4mv6wki5h1ko")
        body = json.loads(raw)
        self.assertEqual(body["operationName"], "PlaybackAccessToken")
        self.assertEqual(
            body["extensions"]["persistedQuery"],
            {"version": 1, "sha256Hash": live_sites.TWITCH_QUERY_HASH},
        )
        self.assertEqual(
            body["variables"],
            {
                "isLive": True,
                "login": "examplechannel",
                "isVod": False,
                "vodID": "",
                "playerType": "embed",
                "platform": "site",
            },
        )
        parts = urlsplit(resolved.url)
        self.assertTrue(parts.path.endswith("/hls/examplechannel.m3u8"))
        query = parse_qs(parts.query)
        self.assertEqual(query["sig"], [SIGNATURE])
        self.assertEqual(query["token"], [TOKEN])
        for key, value in (
            ("allow_source", "true"),
            ("fast_bread", "true"),
            ("platform", "web"),
            ("playlist_include_framerate", "true"),
            ("multigroup_video", "true"),
            ("supported_codecs", "av1,h265,h264"),
        ):
            self.assertEqual(query[key], [value], key)
        self.assertTrue(0 <= int(query["p"][0]) <= 999999)
        self.assertIn("supported_codecs=av1,h265,h264", resolved.url)
        self.assertEqual(sum(source.counts.values()), 2)

    def test_twitch_offline(self):
        self.assertEqual(self.reason(self.serve(usher=(404, {}, b""))), "offline")

    def test_twitch_refused(self):
        self.assertEqual(self.reason(self.serve(gql=(401, {}, b"{}"))), "refused")
        null = json.dumps({"data": {"streamPlaybackAccessToken": None}}).encode()
        self.assertEqual(self.reason(self.serve(gql=(200, JSON, null))), "refused")
        self.assertEqual(self.reason(self.serve(gql=(200, {}, b"<html>"))), "refused")

    def test_twitch_usher_errors_are_network(self):
        self.assertEqual(self.reason(self.serve(usher=(500, {}, b""))), "network")

    def test_twitch_network(self):
        source = self.serve()
        source.close()
        self.assertEqual(self.reason(source), "network")


class PrivacyTests(unittest.TestCase):
    def test_no_token_in_logs(self):
        gql = (
            200,
            JSON,
            json.dumps(
                {"data": {"streamPlaybackAccessToken": {"value": TOKEN, "signature": SIGNATURE}}}
            ).encode(),
        )
        routes = Routes(
            {
                "/gql": gql,
                "/hls/examplechannel.m3u8": (200, {}, MASTER),
                "/api/v2/channels/examplechannel": (200, JSON, kick_body()),
                "/api/v2/channels/otherchannel": (200, JSON, kick_body(live=False)),
            }
        )
        source = LocalSource(routes)
        self.addCleanup(source.close)
        with self.assertLogs(level="DEBUG") as logs:
            resolve(
                SitePage(TWITCH, "examplechannel"),
                twitch_gql=source.url + "/gql",
                twitch_usher=source.url + "/hls",
            )
            resolve(SitePage(KICK, "examplechannel"), kick_api=source.url + "/api/v2/channels")
            with self.assertRaises(SiteError):
                resolve(SitePage(KICK, "otherchannel"), kick_api=source.url + "/api/v2/channels")
            source.close()
            with self.assertRaises(SiteError):
                resolve(
                    SitePage(TWITCH, "examplechannel"),
                    twitch_gql=source.url + "/gql",
                    twitch_usher=source.url + "/hls",
                )
        text = "\n".join(record.getMessage() for record in logs.records)
        self.assertIn("examplechannel is live", text)
        for secret in (SIGNATURE, "examplechannel\\\"", "token=", "SECRETJWT", "expires", "m3u8"):
            self.assertNotIn(secret, text)

    def test_requests_carry_only_the_browser_user_agent(self):
        seen = []

        def record(body, content_type=JSON):
            def route(handler, count):
                seen.append(dict(handler.headers))
                return 200, content_type, body

            return route

        grant = json.dumps(
            {"data": {"streamPlaybackAccessToken": {"value": TOKEN, "signature": SIGNATURE}}}
        ).encode()
        source = LocalSource(
            Routes(
                {
                    "/gql": record(grant),
                    "/hls/examplechannel.m3u8": record(MASTER, {}),
                    "/api/v2/channels/examplechannel": record(kick_body()),
                }
            )
        )
        self.addCleanup(source.close)
        resolve(
            SitePage(TWITCH, "examplechannel"),
            twitch_gql=source.url + "/gql",
            twitch_usher=source.url + "/hls",
        )
        resolve(SitePage(KICK, "examplechannel"), kick_api=source.url + "/api/v2/channels")
        self.assertEqual(len(seen), 3)
        for headers in seen:
            lowered = {name.lower(): value for name, value in headers.items()}
            self.assertEqual(lowered["user-agent"], live_sites.USER_AGENT)
            self.assertIn("Chrome/155.0.0.0", lowered["user-agent"])
            self.assertNotIn("cookie", lowered)

    def test_stop_before_a_request_sends_none(self):
        source = LocalSource(Routes({}))
        self.addCleanup(source.close)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(SiteError):
            resolve(SitePage(KICK, "examplechannel"), stop, kick_api=source.url + "/x")
        self.assertEqual(source.requests, [])


if __name__ == "__main__":
    unittest.main()
