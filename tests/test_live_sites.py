import json
import logging
import os
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from http_fixtures import LocalSource
import requests

from services import live_sites
from services.live_sites import (
    CLIP,
    KICK,
    TWITCH,
    VIDEO,
    Recording,
    SiteError,
    SiteMedia,
    SitePage,
    media_address,
    media_key,
    resolve,
    resolve_media,
    running_recording,
    site_media,
    site_page,
)
from services.log_file import _Masked

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
            "https://user@kick.com/examplechannel",  # (Task 5: no user, no port of its own)
            "https://kick.com:8443/examplechannel",
            "twitch.tv:80/examplechannel",
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

    def test_kick_live_without_its_stream_is_refused(self):
        # A live answer that names no stream (a changed answer): refused, never offline.
        for playback in (None, "", 7):
            body = json.dumps({"playback_url": playback, "livestream": {"is_live": True}})
            self.assertEqual(self.reason(self.serve((200, JSON, body.encode()))), "refused")

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
        with self.assertRaises(SiteError) as caught:
            self.resolve(source)
        self.assertEqual((caught.exception.reason, caught.exception.timed_out), ("network", False))

    def test_a_site_that_never_answers_in_time_is_told_apart(self):
        # Still "network", but known to have timed out (requests' Timeout: to connect or read).
        class Session:
            def __init__(self, error):
                self.error = error

            def request(self, *args, **kwargs):
                raise self.error

        for error, timed_out in (
            (requests.exceptions.ReadTimeout("slow"), True),
            (requests.exceptions.ConnectTimeout("slow"), True),
            (requests.exceptions.ConnectionError("refused"), False),
        ):
            with self.subTest(type(error).__name__):
                with self.assertRaises(SiteError) as caught:
                    resolve(SitePage(KICK, "examplechannel"), session=Session(error))
                self.assertEqual(caught.exception.reason, "network")
                self.assertEqual(caught.exception.timed_out, timed_out)


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

    def test_twitch_unknown(self):
        # Ruling R16 (measured 2026-11-24): a login that doesn't exist is answered 200 with an
        # explicit null grant inside "data".
        null = {"data": {"streamPlaybackAccessToken": None}, "extensions": {"requestID": "x"}}
        source = self.serve(gql=(200, JSON, json.dumps(null).encode()))
        self.assertEqual(self.reason(source), "unknown")
        self.assertEqual(source.counts.get("/hls/examplechannel.m3u8", 0), 0)  # usher unasked

    def test_twitch_refused(self):
        # An "errors" array (a rotted persisted-query hash), or no "data": the site's answer
        # changed, said in the log; so is any other answer that carries no grant.
        rotted = {
            "errors": [
                {
                    "message": "PersistedQueryNotFound",
                    "extensions": {"code": "PERSISTED_QUERY_NOT_FOUND"},
                }
            ]
        }
        # An error beside a null grant is an error, not a missing channel.
        partial = {"errors": [{"message": "error"}], "data": {"streamPlaybackAccessToken": None}}
        for name, status, body in (
            ("rotted hash", 200, json.dumps(rotted).encode()),
            ("error beside a null grant", 200, json.dumps(partial).encode()),
            ("no data", 200, b"{}"),
            ("no grant key", 200, json.dumps({"data": {}}).encode()),
            ("grant without a token", 200, b'{"data": {"streamPlaybackAccessToken": {}}}'),
            ("not JSON", 200, b"<html>"),
            ("HTTP 401", 401, b"{}"),
        ):
            with self.subTest(name):
                with self.assertLogs("services.live_sites", logging.WARNING) as logs:
                    reason = self.reason(self.serve(gql=(status, JSON, body)))
                self.assertEqual(reason, "refused")
                self.assertTrue(any("answer changed" in line for line in logs.output), logs)

    def test_twitch_usher_refusal_is_refused(self):
        # M4: a sub-only or restricted channel's usher answers 403: refused, not "network".
        self.assertEqual(self.reason(self.serve(usher=(403, {}, b""))), "refused")

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
        # Each record as the app writes it (the log handlers format through _Masked).
        masked = _Masked(logging.BASIC_FORMAT.replace("%(levelname)s:%(name)s:", ""))
        text = "\n".join(masked.format(record) for record in logs.records)
        self.assertIn("examplechannel is live", text)
        for secret in (SIGNATURE, TOKEN, '"channel"', "token=", "SECRETJWT", "expires", "m3u8"):
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


UUID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
VOD_MASTER = (
    "#EXTM3U\n"
    '#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="1080p60",NAME="1080p60"\n'
    '#EXT-X-STREAM-INF:BANDWIDTH=7500000,CODECS="avc1.4D402A,mp4a.40.2",RESOLUTION=1920x1080,'
    'VIDEO="1080p60"\n'
    "https://cdn.example.invalid/abc_examplechannel_1/1080p60/index-dvr.m3u8\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=9900000,CODECS="av01.0.08M.08,mp4a.40.2",RESOLUTION=1920x1080\n'
    "https://cdn.example.invalid/abc_examplechannel_1/av1/index-dvr.m3u8\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=3000000,CODECS="avc1.4D401F,mp4a.40.2",RESOLUTION=1280x720\n'
    "https://cdn.example.invalid/abc_examplechannel_1/720p60/index-dvr.m3u8\n"
).encode()
RECORDING = (
    "#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:10\n#EXT-X-PLAYLIST-TYPE:EVENT\n"
    '#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-MAP:URI="init-0.mp4"\n'
    "#EXTINF:10.000,\n0.mp4\n#EXTINF:10.000,\n1.mp4\n"
).encode()
KICK_MASTER = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=9000000,CODECS="avc1.64002A,mp4a.40.2",RESOLUTION=1920x1080\n'
    "1080p60/playlist.m3u8\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=2000000,CODECS="avc1.4D401F,mp4a.40.2",RESOLUTION=852x480\n'
    "480p30/playlist.m3u8\n"
).encode()
KICK_RECORDING = (
    "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:13\n#EXT-X-PLAYLIST-TYPE:EVENT\n"
    "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PROGRAM-DATE-TIME:2026-11-25T09:58:35.031Z\n"
    "#EXTINF:10.000,\n0.ts\n#EXT-X-PROGRAM-DATE-TIME:2026-11-25T09:58:45.031Z\n"
    "#EXTINF:10.000,\n1.ts\n#EXT-X-ENDLIST\n"
).encode()


def gql_answers(answers):
    """A GQL route answering each request by what its body asks: a persisted token ask by
    `answers["token"]`, any other query by the first key of `answers` its text names."""

    def route(handler, count):
        body = json.loads(handler.body)
        if body.get("operationName") == "PlaybackAccessToken":
            data = answers["token"]
        else:
            query = body.get("query", "")
            data = next(v for k, v in answers.items() if k != "token" and k in query)
        return 200, JSON, json.dumps({"data": data}).encode()

    return route


class SiteMediaTests(unittest.TestCase):
    def test_videos_and_clips_are_recognized_in_every_form(self):
        slug = "BraveTinyOtterKappa-AbC_12"
        for url, media in (
            ("twitch.tv/videos/123456", SiteMedia(TWITCH, VIDEO, "123456")),
            ("https://www.twitch.tv/videos/123456?t=1h2m3s", SiteMedia(TWITCH, VIDEO, "123456")),
            ("https://m.twitch.tv/videos/123456", SiteMedia(TWITCH, VIDEO, "123456")),
            (f"https://clips.twitch.tv/{slug}", SiteMedia(TWITCH, CLIP, slug)),
            (f"twitch.tv/ExampleChannel/clip/{slug}?x=1", SiteMedia(TWITCH, CLIP, slug)),
            (f"https://kick.com/examplechannel/videos/{UUID}", SiteMedia(KICK, VIDEO, UUID)),
            (f"kick.com/video/{UUID}", SiteMedia(KICK, VIDEO, UUID)),
            (
                "kick.com/examplechannel/clips/clip_01KABCdef",
                SiteMedia(KICK, CLIP, "clip_01KABCdef"),
            ),
            (
                "https://kick.com/ExampleChannel?clip=clip_01KABCdef",
                SiteMedia(KICK, CLIP, "clip_01KABCdef"),
            ),
        ):
            with self.subTest(url):
                self.assertEqual(site_media(url), media)
                self.assertIsNone(site_page(url))  # never a channel's page

    def test_a_clip_slug_keeps_its_letter_case(self):
        self.assertEqual(site_media("clips.twitch.tv/AbC").ident, "AbC")
        self.assertNotEqual(site_media("clips.twitch.tv/AbC"), site_media("clips.twitch.tv/abc"))

    def test_other_pages_are_no_video(self):
        for url in (
            "twitch.tv/videos/abc",
            "twitch.tv/examplechannel",
            "twitch.tv/examplechannel/videos",
            "clips.twitch.tv/embed",
            "kick.com/examplechannel",
            "kick.com/examplechannel?clip=nope",
            "kick.com/examplechannel/videos/not-a-uuid",
            "kick.com/video/123",
            "kick.com/categories/clips/clip_1",
            "https://user@kick.com/video/" + UUID,
            "example.com/videos/123",
            "",
        ):
            self.assertIsNone(site_media(url), url)

    def test_one_video_is_one_key_however_its_page_was_written(self):
        kick = [
            site_media(f"kick.com/examplechannel/videos/{UUID}"),
            site_media(f"kick.com/video/{UUID}"),
        ]
        self.assertEqual(media_key(kick[0]), media_key(kick[1]))
        clip = [
            site_media("kick.com/examplechannel/clips/clip_1"),
            site_media("kick.com/examplechannel?clip=clip_1"),
        ]
        self.assertEqual(media_key(clip[0]), media_key(clip[1]))
        self.assertEqual(media_address(clip[0]), "https://kick.com/examplechannel/clips/clip_1")
        self.assertEqual(media_address(kick[0]), f"https://kick.com/video/{UUID}")
        self.assertEqual(
            media_address(site_media("twitch.tv/examplechannel/clip/Slug")),
            "https://clips.twitch.tv/Slug",
        )
        self.assertEqual(
            media_address(site_media("m.twitch.tv/videos/9?t=1")), "https://www.twitch.tv/videos/9"
        )


class TwitchMediaTests(unittest.TestCase):
    def serve(self, answers, usher=None, variant=None):
        routes = Routes(
            {
                "/gql": gql_answers(answers),
                "/vod/123.m3u8": usher or (200, {}, VOD_MASTER),
            }
        )
        source = LocalSource(routes)
        self.addCleanup(source.close)
        return source

    def resolve(self, source, ident="123", kind=VIDEO, keep_dir=None):
        return resolve_media(
            SiteMedia(TWITCH, kind, ident),
            twitch_gql=source.url + "/gql",
            twitch_vod=source.url + "/vod",
            keep_dir=keep_dir,
        )

    def reason(self, source, **kwargs):
        with self.assertRaises(SiteError) as caught:
            self.resolve(source, **kwargs)
        return caught.exception.reason

    def answers(self, status="RECORDED", grant=True):
        token = {"videoPlaybackAccessToken": {"value": TOKEN, "signature": SIGNATURE}}
        return {
            "video(id": {"video": {"title": "  A long   evening  ", "status": status}},
            "token": token if grant else {"videoPlaybackAccessToken": None},
        }

    def test_a_video_plays_its_source_variant_named_by_its_title(self):
        source = self.serve(self.answers())
        resolved = self.resolve(source)
        # The H.264 source, not the AV1 one listed with a higher bandwidth (VLC's film codecs).
        self.assertEqual(
            resolved.url, "https://cdn.example.invalid/abc_examplechannel_1/1080p60/index-dvr.m3u8"
        )
        self.assertEqual(resolved.name, "A long evening · Twitch")
        query = parse_qs(urlsplit(source.requests[-1][0]).query)
        self.assertEqual(query["sig"], [SIGNATURE])
        self.assertEqual(query["token"], [TOKEN])
        token_ask = next(json.loads(b) for b in source.bodies if b"PlaybackAccessToken" in b)
        self.assertEqual(token_ask["variables"]["vodID"], "123")
        self.assertIs(token_ask["variables"]["isVod"], True)

    def test_a_video_still_recorded_plays_as_far_as_it_is_listed(self):
        folder = self.enterContext(__import__("tempfile").TemporaryDirectory())
        source = self.serve(self.answers(status="RECORDING"))
        master = VOD_MASTER.replace(b"https://cdn.example.invalid", source.url.encode())
        source.routes["/vod/123.m3u8"] = (200, {}, master)
        source.routes["/abc_examplechannel_1/1080p60/index-dvr.m3u8"] = (200, {}, RECORDING)
        resolved = self.resolve(source, keep_dir=folder)
        self.assertTrue(resolved.url.startswith("file://"), resolved.url)
        path = urlsplit(resolved.url).path
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("#EXT-X-ENDLIST", text)
        self.assertIn(source.url + "/abc_examplechannel_1/1080p60/1.mp4", text)
        self.assertIn(source.url + "/abc_examplechannel_1/1080p60/init-0.mp4", text)
        for secret in (SIGNATURE, "token="):
            self.assertNotIn(secret, text)

    def test_a_video_nobody_has_is_gone(self):
        null = {"video(id": {"video": None}, "token": {"videoPlaybackAccessToken": None}}
        self.assertEqual(self.reason(self.serve(null)), "unknown")
        self.assertEqual(self.reason(self.serve(self.answers(grant=False))), "unknown")

    def test_a_subscribers_video_is_private(self):
        self.assertEqual(self.reason(self.serve(self.answers(), usher=(403, {}, b""))), "private")

    def test_a_clip_plays_its_best_file_with_its_access(self):
        clip = {
            "title": "The goal",
            "playbackAccessToken": {"value": TOKEN, "signature": SIGNATURE},
            "videoQualities": [
                {
                    "quality": "1080",
                    "frameRate": 59.99,
                    "sourceURL": "https://c.invalid/1080/index.mp4",
                },
                {"quality": "720", "frameRate": 60, "sourceURL": "https://c.invalid/720/index.mp4"},
            ],
        }
        source = self.serve({"clip(slug": {"clip": clip}})
        resolved = self.resolve(source, ident="Slug", kind=CLIP)
        parts = urlsplit(resolved.url)
        self.assertEqual(parts.netloc + parts.path, "c.invalid/1080/index.mp4")
        self.assertEqual(parse_qs(parts.query), {"sig": [SIGNATURE], "token": [TOKEN]})
        self.assertEqual(resolved.name, "The goal · Twitch")

    def test_a_clip_nobody_has_is_gone(self):
        source = self.serve({"clip(slug": {"clip": None}})
        self.assertEqual(self.reason(source, ident="Slug", kind=CLIP), "unknown")


class KickMediaTests(unittest.TestCase):
    def serve(self, video=None, clip=None):
        source = LocalSource(Routes({}))
        self.addCleanup(source.close)
        master = source.url + "/media/hls/master.m3u8"
        source.routes.update(
            {
                f"/api/v1/video/{UUID}": video
                or (
                    200,
                    JSON,
                    json.dumps(
                        {"source": master, "livestream": {"session_title": "Night"}}
                    ).encode(),
                ),
                "/api/v2/clips/clip_1": clip
                or (
                    200,
                    JSON,
                    json.dumps(
                        {"clip": {"title": "Wow", "privacy": "public", "clip_url": master}}
                    ).encode(),
                ),
                "/media/hls/master.m3u8": (200, {}, KICK_MASTER),
            }
        )
        return source

    def resolve(self, source, kind=VIDEO):
        ident = UUID if kind == VIDEO else "clip_1"
        return resolve_media(
            SiteMedia(KICK, kind, ident),
            kick_video_api=source.url + "/api/v1/video",
            kick_clip_api=source.url + "/api/v2/clips",
        )

    def reason(self, source, kind=VIDEO):
        with self.assertRaises(SiteError) as caught:
            self.resolve(source, kind)
        return caught.exception.reason

    def test_a_video_and_a_clip_play_their_source_variant(self):
        source = self.serve()
        video = self.resolve(source)
        self.assertEqual(video.url, source.url + "/media/hls/1080p60/playlist.m3u8")
        self.assertEqual(video.name, "Night · Kick")
        clip = self.resolve(source, CLIP)
        self.assertEqual(clip.url, source.url + "/media/hls/1080p60/playlist.m3u8")
        self.assertEqual(clip.name, "Wow · Kick")

    def test_a_video_that_went_is_gone_and_a_private_one_says_so(self):
        for body, reason in (
            ({"deleted_at": "2026-11-01", "source": "x"}, "unknown"),
            ({"is_pruned": True, "source": "x"}, "unknown"),
            ({"source": None}, "unknown"),
            ({"is_private": True, "source": "x"}, "private"),
        ):
            with self.subTest(body=body):
                source = self.serve(video=(200, JSON, json.dumps(body).encode()))
                self.assertEqual(self.reason(source), reason)
        self.assertEqual(self.reason(self.serve(video=(404, JSON, b"{}"))), "unknown")
        clip = {"clip": {"privacy": "private", "clip_url": "x"}}
        self.assertEqual(
            self.reason(self.serve(clip=(200, JSON, json.dumps(clip).encode())), CLIP), "private"
        )
        self.assertEqual(self.reason(self.serve(clip=(404, JSON, b"{}")), CLIP), "unknown")


class RecordingTests(unittest.TestCase):
    def test_twitch_the_running_broadcasts_video(self):
        archive = {"id": "123", "status": "RECORDING", "createdAt": "2026-11-25T16:06:52Z"}
        answers = {
            "user(login": {"user": {"stream": {"archiveVideo": archive}}},
            "token": {"videoPlaybackAccessToken": {"value": TOKEN, "signature": SIGNATURE}},
        }
        source = LocalSource(
            Routes({"/gql": gql_answers(answers), "/vod/123.m3u8": (200, {}, VOD_MASTER)})
        )
        self.addCleanup(source.close)
        found = running_recording(
            SitePage(TWITCH, "examplechannel"),
            twitch_gql=source.url + "/gql",
            twitch_vod=source.url + "/vod",
        )
        self.assertEqual(
            found,
            Recording(
                "https://cdn.example.invalid/abc_examplechannel_1/1080p60/index-dvr.m3u8",
                1795622812.0,
                dated=False,
                steps=False,
            ),
        )
        none = {"user(login": {"user": {"stream": {"archiveVideo": None}}}}
        source.routes["/gql"] = gql_answers(none)
        self.assertIsNone(
            running_recording(SitePage(TWITCH, "examplechannel"), twitch_gql=source.url + "/gql")
        )

    def test_kick_the_live_video_dated_by_its_first_piece(self):
        source = LocalSource(Routes({}))
        self.addCleanup(source.close)
        master = source.url + "/media/hls/master.m3u8"
        rows = [{"is_live": True, "source": master, "start_time": "2026-11-25 09:58:38"}]
        source.routes.update(
            {
                "/api/v2/channels/examplechannel/videos": (200, JSON, json.dumps(rows).encode()),
                "/media/hls/master.m3u8": (200, {}, KICK_MASTER),
                "/media/hls/1080p60/playlist.m3u8": (200, {}, KICK_RECORDING),
            }
        )
        found = running_recording(
            SitePage(KICK, "examplechannel"), kick_api=source.url + "/api/v2/channels"
        )
        self.assertEqual(found.url, source.url + "/media/hls/1080p60/playlist.m3u8")
        self.assertAlmostEqual(found.start, 1795600715.031, places=3)
        self.assertTrue(found.dated and found.steps)
        rows[0]["is_live"] = False  # the newest video is a past one: none runs
        source.routes["/api/v2/channels/examplechannel/videos"] = (
            200,
            JSON,
            json.dumps(rows).encode(),
        )
        self.assertIsNone(
            running_recording(
                SitePage(KICK, "examplechannel"), kick_api=source.url + "/api/v2/channels"
            )
        )


class MediaPrivacyTests(unittest.TestCase):
    def test_no_token_in_logs_for_videos_and_clips(self):
        clip = {
            "title": "x",
            "playbackAccessToken": {"value": TOKEN, "signature": SIGNATURE},
            "videoQualities": [
                {"quality": "1080", "frameRate": 60, "sourceURL": "https://c.invalid/a.mp4"}
            ],
        }
        answers = {
            "video(id": {"video": {"title": "t", "status": "RECORDED"}},
            "clip(slug": {"clip": clip},
            "token": {"videoPlaybackAccessToken": {"value": TOKEN, "signature": SIGNATURE}},
        }
        source = LocalSource(
            Routes({"/gql": gql_answers(answers), "/vod/123.m3u8": (200, {}, VOD_MASTER)})
        )
        self.addCleanup(source.close)
        with self.assertLogs(level="DEBUG") as logs:
            for kind, ident in ((VIDEO, "123"), (CLIP, "Slug")):
                resolve_media(
                    SiteMedia(TWITCH, kind, ident),
                    twitch_gql=source.url + "/gql",
                    twitch_vod=source.url + "/vod",
                )
        masked = _Masked(logging.BASIC_FORMAT.replace("%(levelname)s:%(name)s:", ""))
        text = "\n".join(masked.format(record) for record in logs.records)
        self.assertIn("Twitch: a clip plays", text)
        for secret in (SIGNATURE, TOKEN, "token=", "m3u8", "examplechannel_1", "c.invalid"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
