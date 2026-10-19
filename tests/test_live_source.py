"""Raw MPEG-TS and HLS sources for the live pipeline.

Ported from main's tests/test_timeshift_source.py. Main decoded the media with PyAV; these
sources yield raw TS bytes, so every decode assertion became a byte assertion. Fixtures are
synthetic (tests/ts_fixtures.py) and every server is local.
"""

from contextlib import contextmanager
import gzip
import logging
import ntpath
from pathlib import Path
import re
import shutil
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import warnings

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad
from http_fixtures import LocalSource, leaf, tls_server_context
import requests
import ts_fixtures
from urllib3.exceptions import InsecureRequestWarning

from services.live import source as source_module
from services.live.source import (
    STREAM_LIMIT,
    STREAM_REFUSED,
    CaughtUp,
    Discontinuity,
    Reconnecting,
    SourceError,
    _scheme,
    open_source,
)
from services.live.ts import ContinuityJoiner

KEY = bytes(range(16))
FAST_RETRIES = (0.01,) * 6


def encrypt(data, iv, key=KEY):
    return AES.new(key, AES.MODE_CBC, iv=iv).encrypt(pad(data, 16))


class TokenRoutes(dict):
    """Routes that ignore a rotating ``/tok<N>`` path prefix and the query string."""

    def get(self, path, default=None):
        return super().get(re.sub(r"^/tok\d+", "", path.split("?")[0]), default)


def requested_segments(server):
    """Segment names in request order, without rotating tokens or queries."""
    paths = (path.split("?")[0] for path, _headers in server.requests)
    return [re.sub(r"^/tok\d+", "", path)[1:] for path in paths if path.endswith(".ts")]


def hls(sequence, names, *, target=1, ended=False, extra=""):
    """A media playlist whose segments each last ``target`` seconds."""
    text = f"#EXTM3U\n#EXT-X-TARGETDURATION:{target}\n#EXT-X-MEDIA-SEQUENCE:{sequence}\n{extra}"
    text += "".join(f"#EXTINF:{target},\n{name}\n" for name in names)
    return (text + ("#EXT-X-ENDLIST\n" if ended else "")).encode()


def drain(source, *, caught_up=False, reconnecting=False):
    """Every item from ``chunks()``, with adjacent byte blocks joined; `CaughtUp` markers
    (the helper's cue to report its live edge, no media) only with `caught_up`, and
    `Reconnecting` ones (its cue to say a new connection opens) only with `reconnecting`."""
    parts = []
    try:
        for item in source.chunks():
            if isinstance(item, CaughtUp) and not caught_up:
                continue
            if isinstance(item, Reconnecting) and not reconnecting:
                continue
            if isinstance(item, bytes) and parts and isinstance(parts[-1], bytes):
                parts[-1] += item
            else:
                parts.append(item)
    finally:
        source.close()
    return parts


def joined(*items):
    """What an HLS source yields for these segments (bytes) and markers, as `drain` joins it:
    each PID's continuity counter runs on across the segments, and the first PCR after a
    marker carries the discontinuity indicator (2.0.2; tests/test_ts_continuity.py)."""
    joiner, parts = ContinuityJoiner(), []
    for item in items:
        if isinstance(item, Discontinuity):
            joiner.discontinuity()
            parts.append(item)
            continue
        joiner.segment()
        block = joiner.feed(item) + joiner.end()
        if parts and isinstance(parts[-1], bytes):
            parts[-1] += block
        else:
            parts.append(block)
    return parts


def plain(data):
    """`data` without its continuity counters and discontinuity indicators."""
    out = bytearray(data)
    for at in range(0, len(out) - 187, 188):
        out[at + 3] &= 0xF0
        if out[at + 3] & 0x20 and out[at + 4]:
            out[at + 5] &= 0x7F
    return bytes(out)


def media(
    url, *, headers=None, verify_ssl=True, stop=None, caught_up=False, reconnecting=False, **options
):
    source = open_source(
        url,
        headers=headers or {},
        verify_ssl=verify_ssl,
        stop=stop or threading.Event(),
        **options,
    )
    return drain(source, caught_up=caught_up, reconnecting=reconnecting)


@contextmanager
def insecure():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", InsecureRequestWarning)
        yield


@contextmanager
def source_logs():
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("services.live.source")
    handler = Collect(logging.DEBUG)
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


class SourceTestCase(unittest.TestCase):
    """Fixtures shared by the source tests (no tests of its own)."""

    @classmethod
    def setUpClass(cls):
        cls.video = ts_fixtures.make_stream(4)
        cls.next_video = ts_fixtures.make_stream(4, pts_start=900000 + 4 * 90000)

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()

    def server(self, routes, tls=None, keep_alive=False):
        server = LocalSource(routes, tls=tls, keep_alive=keep_alive)
        self.servers.append(server)
        return server

    @contextmanager
    def tls_context(self):
        with tls_server_context() as context:  # skips the test without openssl
            yield context

    @contextmanager
    def optional_tls_context(self):
        """The local TLS context, or None without an openssl binary: then only the TLS
        subtests skip (`skip_without_tls`) and the plain ones still run."""
        if not shutil.which("openssl"):
            yield None
            return
        with self.tls_context() as context:
            yield context

    def skip_without_tls(self, context):
        if context is None:
            self.skipTest("openssl is required to generate a local test certificate")


class LiveSourceTests(SourceTestCase):
    def test_direct_transport_stream_uses_one_request_with_the_provider_user_agent(self):
        server = self.server(
            {
                "/account/password.ts": (200, {}, self.video),
                "/default.ts": (200, {}, self.video),
            }
        )
        source = open_source(
            server.url + "/account/password.ts",
            headers={"User-Agent": "Provider/1.0"},
            verify_ssl=True,
            stop=threading.Event(),
        )
        self.assertEqual(source.user_agent, "Provider/1.0")
        self.assertEqual(drain(source), [self.video])
        self.assertEqual(media(server.url + "/default.ts"), [self.video])
        self.assertEqual(server.counts, {"/account/password.ts": 1, "/default.ts": 1})
        self.assertEqual(server.requests[0][1]["User-Agent"], "Provider/1.0")
        self.assertEqual(server.requests[1][1]["User-Agent"], "VLC/3.0.20")
        for _path, headers in server.requests:
            self.assertNotIn("Range", headers)

    def test_requests_use_sixty_second_timeouts_and_the_spec_backoff(self):
        self.assertEqual(source_module._RECONNECT_DELAYS, (0.5, 1.0, 2.0, 4.0, 8.0, 8.0))
        server = self.server({"/video.ts": (200, {}, self.video)})
        session = requests.Session()
        calls = []
        get = session.get

        def capture(*args, **kwargs):
            calls.append(kwargs)
            return get(*args, **kwargs)

        with patch("services.live.source.requests.Session", return_value=session):
            with patch.object(session, "get", side_effect=capture):
                self.assertEqual(media(server.url + "/video.ts"), [self.video])
        self.assertEqual([call["timeout"] for call in calls], [(60.0, 60.0)])
        self.assertTrue(calls[0]["stream"])

    def test_gzip_encoded_http_media_and_nested_playlist_are_decoded(self):
        server = self.server(
            {
                "/video.ts": (200, {"Content-Encoding": "gzip"}, gzip.compress(self.video)),
                "/master.m3u8": (
                    200,
                    {"Content-Encoding": "gzip"},
                    gzip.compress(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nleaf.m3u8\n"),
                ),
                "/leaf.m3u8": (200, {"Content-Encoding": "gzip"}, gzip.compress(leaf("video.ts"))),
            }
        )
        for path in ("/video.ts", "/master.m3u8"):
            with self.subTest(path=path):
                self.assertEqual(media(server.url + path), [self.video])
        self.assertEqual(server.counts["/video.ts"], 2)
        self.assertEqual(server.counts["/leaf.m3u8"], 1)

    def test_playlist_sniffing_tolerates_a_bom_and_leading_whitespace(self):
        server = self.server(
            {
                "/channel": (200, {}, b"\xef\xbb\xbf\r\n \t" + leaf("video.ts")),
                "/video.ts": (200, {}, self.video),
            }
        )
        self.assertEqual(media(server.url + "/channel"), [self.video])
        self.assertEqual(server.counts, {"/channel": 1, "/video.ts": 1})

    def test_streaming_redirect_bodies_do_not_delay_media(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                release = threading.Event()
                finished = threading.Event()
                disconnected = []
                result = []

                def redirect(location, *, chunked):
                    closed = threading.Event()
                    disconnected.append(closed)

                    def body(request):
                        block = b"x" * 8192
                        if chunked:
                            block = b"2000\r\n" + block + b"\r\n"
                        try:
                            # No natural EOF: a reader that drains redirects can
                            # never reach the target until the test releases it.
                            while not release.is_set():
                                request.wfile.write(block)
                                if release.wait(0.01):
                                    break
                            if chunked:
                                request.wfile.write(b"0\r\n\r\n")
                        except ConnectionError:
                            closed.set()

                    headers = {
                        "Location": location,
                        "Set-Cookie": "access=allowed; Path=/",
                    }
                    if chunked:
                        headers["Transfer-Encoding"] = "chunked"
                    else:
                        headers["Content-Length"] = str(1 << 40)
                    return 302, headers, body

                routes = {"/media/video.ts": (200, {}, self.video)}
                if nested:
                    routes.update(
                        {
                            "/start": (
                                200,
                                {},
                                b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nredirect.m3u8\n",
                            ),
                            "/redirect.m3u8": redirect("/media/leaf.m3u8", chunked=True),
                            "/media/leaf.m3u8": (200, {}, leaf("redirect.ts")),
                            "/media/redirect.ts": redirect("/media/video.ts", chunked=False),
                        }
                    )
                else:
                    routes["/start"] = redirect("/media/video.ts", chunked=True)
                server = self.server(routes)

                def decode():
                    try:
                        result.append(media(server.url + "/start"))
                    except Exception as error:
                        result.append(error)
                    finally:
                        finished.set()

                worker = threading.Thread(target=decode, daemon=True)
                worker.start()
                try:
                    self.assertTrue(
                        finished.wait(5),
                        "media remained blocked on a streaming redirect body",
                    )
                    self.assertEqual(result, [[self.video]])
                    for closed in disconnected:
                        self.assertTrue(closed.wait(2), "redirect body connection was not closed")
                    targets = [
                        headers
                        for path, headers in server.requests
                        if path in ("/media/leaf.m3u8", "/media/video.ts")
                    ]
                    for headers in targets:
                        self.assertIn("access=allowed", headers.get("Cookie", ""))
                finally:
                    release.set()
                    worker.join(5)
                    server.close()
                    self.servers.remove(server)
                self.assertFalse(worker.is_alive(), "source worker survived cleanup")

    def test_redirect_connections_close_while_the_source_is_still_open(self):
        # Each finished connection must close at once, not when the session ends.
        release, closed = threading.Event(), threading.Event()

        def endless(request):
            try:
                while not release.is_set():
                    request.wfile.write(b"x" * 8192)
                    if release.wait(0.01):
                        break
            except ConnectionError:
                closed.set()

        def stalled(request):
            request.wfile.write(self.video)
            request.wfile.flush()
            release.wait(10)

        # Another origin, so the redirect's connection object is not reused for the target.
        target = self.server({"/live.ts": (200, {}, stalled)})
        location = target.url + "/live.ts"
        server = self.server(
            {"/start": (302, {"Location": location, "Content-Length": str(1 << 40)}, endless)}
        )
        try:
            source = open_source(
                server.url + "/start", headers={}, verify_ssl=True, stop=threading.Event()
            )
            try:
                self.assertTrue(closed.wait(2), "the redirect connection stayed open")
            finally:
                source.close()
        finally:
            release.set()

    def test_redirect_chain_strips_cross_origin_authorization_and_keeps_cookies(self):
        destination = self.server(
            {
                "/entry": (
                    302,
                    {"Location": "/video.ts", "Set-Cookie": "access=allowed; Path=/"},
                    b"",
                ),
                "/video.ts": (200, {}, self.video),
            }
        )
        origin = self.server({"/start": (302, {"Location": destination.url + "/entry"}, b"")})
        url = origin.url.replace("http://", "http://user:password@") + "/start"
        self.assertEqual(media(url), [self.video])
        self.assertIn("Authorization", origin.requests[0][1])
        for _path, headers in destination.requests:
            self.assertNotIn("Authorization", headers)
        self.assertIn("access=allowed", destination.requests[-1][1].get("Cookie", ""))

    def test_fragmented_and_byte_range_hls_is_unsupported(self):
        # Inverted from main, where FFmpeg played fMP4 and byte ranges.
        fragmented = (
            "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:4\n"
            '#EXT-X-MAP:URI="movie.mp4",BYTERANGE="1000@0"\n'
            "#EXTINF:4,\n#EXT-X-BYTERANGE:5000@1000\nmovie.mp4\n#EXT-X-ENDLIST\n"
        ).encode()
        ranges = (
            "#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4,\n#EXT-X-BYTERANGE:1000@0\nvideo.ts\n#EXT-X-ENDLIST\n"
        ).encode()
        server = self.server(
            {
                # A master whose variants are all fragmented MP4.
                "/master.m3u8": (
                    200,
                    {},
                    b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nleaf.m3u8\n"
                    b"#EXT-X-STREAM-INF:BANDWIDTH=50000\nlow.m3u8\n",
                ),
                "/leaf.m3u8": (302, {"Location": "/nested/leaf.m3u8"}, b""),
                "/nested/leaf.m3u8": (200, {}, fragmented),
                "/nested/movie.mp4": (200, {}, b"\x00" * 6000),
                "/ranges.m3u8": (200, {}, ranges),
                "/video.ts": (200, {}, self.video),
            }
        )
        for path in ("/master.m3u8", "/ranges.m3u8"):
            with self.subTest(path=path):
                with self.assertRaises(SourceError) as raised:
                    open_source(
                        server.url + path, headers={}, verify_ssl=True, stop=threading.Event()
                    )
                self.assertEqual(raised.exception.kind, "unsupported")
        self.assertEqual(server.counts["/nested/movie.mp4"], 0)
        self.assertEqual(server.counts["/video.ts"], 0)
        self.assertEqual(server.counts["/low.m3u8"], 0)

    def test_hls_segments_that_are_not_transport_streams_are_unsupported(self):
        server = self.server(
            {
                "/channel.m3u8": (200, {}, leaf("page.ts")),
                "/page.ts": (200, {}, b"<html>not media</html>" * 20),
            }
        )
        with self.assertRaises(SourceError) as raised:
            media(server.url + "/channel.m3u8")
        self.assertEqual(raised.exception.kind, "unsupported")

    def test_delayed_http_startup_exceeds_old_open_timeout(self):
        def delayed(_request, _count):
            time.sleep(5.2)
            return 200, {}, self.video

        server = self.server({"/slow.ts": delayed})
        self.assertEqual(media(server.url + "/slow.ts"), [self.video])
        self.assertEqual(server.counts["/slow.ts"], 1)

    def test_master_selects_the_highest_compatible_quality_and_never_fetches_others(self):
        master = b"""#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=100000,CODECS="mp2v,mp4a.40.2"
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=200000,CODECS="mp2v,mp4a.40.2"
high.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=300000,CODECS="av01.0.04M.08,mp4a.40.2"
unsupported.m3u8
"""
        routes = {
            "/start": (
                302,
                {"Location": "/media/master.m3u8", "Set-Cookie": "access=allowed; Path=/media/"},
                b"",
            ),
            "/media/master.m3u8": (200, {}, master),
        }

        def protected(body):
            def respond(request, _count):
                if "access=allowed" not in request.headers.get("Cookie", ""):
                    return 403, {}, b""
                if request.headers.get("User-Agent") != "Provider/2.0":
                    return 403, {}, b""
                return 200, {}, body

            return respond

        routes["/media/high.m3u8"] = protected(leaf("video.ts"))
        routes["/media/video.ts"] = protected(self.video)
        server = self.server(routes)
        parts = media(server.url + "/start", headers={"User-Agent": "Provider/2.0"})
        self.assertEqual(parts, [self.video])
        self.assertEqual(server.counts["/media/master.m3u8"], 1)
        self.assertEqual(server.counts["/media/high.m3u8"], 1)
        self.assertEqual(server.counts["/media/low.m3u8"], 0)
        self.assertEqual(server.counts["/media/unsupported.m3u8"], 0)

    def test_an_external_audio_group_is_unsupported_unless_the_variant_carries_audio(self):
        # A TS-only fetcher cannot mux a separate audio playlist. Some multi-track restream
        # servers name each track's playlist and still mux the first audio into the video's,
        # though: whether the variant carries audio of its own, its first segment's program
        # tables tell (2.1: public channels VLC played were refused for their master alone).
        master = b'''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="languages",NAME="English",LANGUAGE="eng",DEFAULT=YES,AUTOSELECT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="languages",NAME="French",LANGUAGE="fra",DEFAULT=NO,AUTOSELECT=YES,URI="audio/fr.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100000,CODECS="mp2v,mp4a.40.2",AUDIO="languages"
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=200000,CODECS="mp2v,mp4a.40.2",AUDIO="languages"
high.m3u8
'''
        video_only = ts_fixtures.make_stream(4, audio=False)
        for name, segment, plays in (
            ("its own audio", self.video, True),
            ("video only", video_only, False),
        ):
            with self.subTest(name):
                server = self.server(
                    {
                        "/master.m3u8": (200, {}, master),
                        "/high.m3u8": (200, {}, leaf("video.ts", "next.ts")),
                        "/audio/en.m3u8": (200, {}, leaf("en.ts")),
                        "/video.ts": (200, {}, segment),
                        "/next.ts": (200, {}, segment),
                    }
                )
                source = open_source(
                    server.url + "/master.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
                )
                if plays:
                    self.assertEqual(drain(source), joined(segment, segment))
                    self.assertEqual(server.counts["/next.ts"], 1)
                else:
                    parts = []
                    with self.assertRaises(SourceError) as raised:
                        parts.extend(source.chunks())
                    self.assertEqual(raised.exception.kind, "unsupported")
                    self.assertIn("separate", str(raised.exception))
                    self.assertEqual(parts, [])  # not one byte of it
                    self.assertEqual(server.counts["/next.ts"], 0)
                self.assertEqual(server.counts["/master.m3u8"], 1)
                self.assertEqual(server.counts["/high.m3u8"], 1)
                self.assertEqual(server.counts["/audio/en.m3u8"], 0)  # never fetched
                self.assertEqual(server.counts["/video.ts"], 1)

    def test_live_leaf_reloads_real_redirect_url_and_tolerates_segment_gap(self):
        def playlist(_request, count):
            if count == 1:
                return 200, {}, leaf("first.ts", ended=False)
            return 200, {}, leaf("first.ts", "second.ts")

        def delayed_segment(_request, _count):
            time.sleep(2.2)
            return 200, {}, self.next_video

        server = self.server(
            {
                "/entry": (302, {"Location": "/live/channel.m3u8"}, b""),
                "/live/channel.m3u8": playlist,
                "/live/first.ts": (200, {}, self.video),
                "/live/second.ts": delayed_segment,
            }
        )
        self.assertEqual(media(server.url + "/entry"), joined(self.video, self.next_video))
        self.assertGreaterEqual(server.counts["/live/channel.m3u8"], 2)
        self.assertEqual(server.counts["/entry"], 1)
        self.assertEqual(server.counts["/live/first.ts"], 1)
        self.assertEqual(server.counts["/live/second.ts"], 1)

    def test_a_media_sequence_gap_on_reload_marks_a_discontinuity(self):
        def playlist(_request, count):
            if count == 1:
                text = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:10\n"
                return 200, {}, (text + "#EXTINF:2,\nten.ts\n").encode()
            text = "#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXT-X-MEDIA-SEQUENCE:13\n"
            return 200, {}, (text + "#EXTINF:3,\nthirteen.ts\n#EXT-X-ENDLIST\n").encode()

        server = self.server(
            {
                "/live.m3u8": playlist,
                "/ten.ts": (200, {}, self.video),
                "/thirteen.ts": (200, {}, self.next_video),
            }
        )
        source = open_source(
            server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
        )
        self.assertEqual(source.burst_s, 2.0)
        self.assertEqual(drain(source), joined(self.video, Discontinuity(), self.next_video))
        self.assertEqual(source.burst_s, 3.0)  # follows the reloaded target duration
        self.assertEqual(server.counts["/live.m3u8"], 2)

    def test_discontinuity_tag_yields_a_marker_between_segments(self):
        text = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-DISCONTINUITY\n#EXTINF:4,\na.ts\n"
            "#EXT-X-DISCONTINUITY\n#EXTINF:4,\nb.ts\n#EXT-X-ENDLIST\n"
        )
        server = self.server(
            {
                "/channel.m3u8": (200, {}, text.encode()),
                "/a.ts": (200, {}, self.video),
                "/b.ts": (200, {}, self.next_video),
            }
        )
        # Nothing precedes the first segment, so only the second tag is a break.
        parts = media(server.url + "/channel.m3u8")
        self.assertEqual(parts, joined(self.video, Discontinuity(), self.next_video))

    def test_hls_retries_transient_reload_and_segment_failures(self):
        def playlist(_request, count):
            text = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1,\na.ts\n"
            if count == 1:
                return 200, {}, text.encode()
            if count == 2:
                return 500, {}, b""
            if count == 3:
                return 200, {}, b""  # an empty answer from a flaky origin
            return 200, {}, (text + "#EXTINF:1,\nb.ts\n#EXT-X-ENDLIST\n").encode()

        def flaky(_request, count):
            return (503, {}, b"") if count == 1 else (200, {}, self.next_video)

        server = self.server(
            {"/live.m3u8": playlist, "/a.ts": (200, {}, self.video), "/b.ts": flaky}
        )
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            parts = media(server.url + "/live.m3u8")
        self.assertEqual(parts, joined(self.video, self.next_video))
        self.assertEqual(server.counts["/live.m3u8"], 4)
        self.assertEqual(server.counts["/b.ts"], 2)

    def test_tls_verification_default_rejects_and_explicit_opt_out_decodes(self):
        with self.tls_context() as context:
            server = self.server({"/user/secret.ts": (200, {}, self.video)}, tls=context)
            with self.assertRaises(SourceError) as failure:
                media(server.url + "/user/secret.ts")
            self.assertEqual(failure.exception.kind, "tls")
            self.assertNotIn("secret", str(failure.exception))
            self.assertNotIn(server.url, str(failure.exception))
            with insecure():
                parts = media(server.url + "/user/secret.ts", verify_ssl=False)
            self.assertEqual(parts, [self.video])

    def test_http_redirect_to_tls_obeys_verification_setting(self):
        with self.tls_context() as context:
            secure = self.server({"/video.ts": (200, {}, self.video)}, tls=context)
            server = self.server({"/redirect": (302, {"Location": secure.url + "/video.ts"}, b"")})
            with self.assertRaises(SourceError) as failure:
                media(server.url + "/redirect")
            self.assertEqual(failure.exception.kind, "tls")
            with insecure():
                parts = media(server.url + "/redirect", verify_ssl=False)
            self.assertEqual(parts, [self.video])

    def test_nested_hls_tls_is_verified_without_enabling_file_protocol(self):
        with self.tls_context() as context:
            secure = self.server({"/video.ts": (200, {}, self.video)}, tls=context)
            server = self.server({"/channel.m3u8": (200, {}, leaf(secure.url + "/video.ts"))})
            with self.assertRaises(SourceError) as failure:
                media(server.url + "/channel.m3u8")
            self.assertEqual(failure.exception.kind, "tls")
            with insecure():
                parts = media(server.url + "/channel.m3u8", verify_ssl=False)
            self.assertEqual(parts, [self.video])

    def test_remote_playlist_cannot_read_local_media(self):
        with tempfile.TemporaryDirectory(prefix="qitv-source-private-") as directory:
            private = Path(directory) / "private.ts"
            private.write_bytes(self.video)
            server = self.server({"/channel.m3u8": (200, {}, leaf(private.as_uri()))})
            with self.assertRaises(SourceError) as raised:
                media(server.url + "/channel.m3u8")
            self.assertEqual(raised.exception.kind, "unsupported")
            self.assertNotIn(directory, str(raised.exception))

    def test_encrypted_leaf_retains_relative_key_semantics(self):
        encrypted = encrypt(self.video, bytes(16))
        playlist = leaf(
            "encrypted.ts",
            extra='#EXT-X-KEY:METHOD=AES-128,URI="keys/channel.key",IV=0x00000000000000000000000000000000\n',
        )
        server = self.server(
            {
                "/media/channel.m3u8": (200, {}, playlist),
                "/media/encrypted.ts": (200, {}, encrypted),
                "/media/keys/channel.key": (200, {}, KEY),
            }
        )
        self.assertEqual(media(server.url + "/media/channel.m3u8"), [self.video])
        self.assertEqual(server.counts["/media/keys/channel.key"], 1)

    def test_aes_128_without_an_iv_uses_the_media_sequence(self):
        text = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:41\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="channel.key"\n'
            "#EXTINF:4,\nfirst.ts\n#EXTINF:4,\nsecond.ts\n#EXT-X-ENDLIST\n"
        )
        server = self.server(
            {
                "/channel.m3u8": (200, {}, text.encode()),
                "/first.ts": (200, {}, encrypt(self.video, (41).to_bytes(16, "big"))),
                "/second.ts": (200, {}, encrypt(self.next_video, (42).to_bytes(16, "big"))),
                "/channel.key": (200, {}, KEY),
            }
        )
        self.assertEqual(media(server.url + "/channel.m3u8"), joined(self.video, self.next_video))
        self.assertEqual(server.counts["/channel.key"], 1)

    def test_unsupported_key_methods_are_rejected(self):
        text = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:4\n"
            '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key"\n'
            "#EXTINF:4,\nvideo.ts\n#EXT-X-ENDLIST\n"
        )
        server = self.server(
            {"/channel.m3u8": (200, {}, text.encode()), "/video.ts": (200, {}, self.video)}
        )
        with self.assertRaises(SourceError) as raised:
            open_source(
                server.url + "/channel.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
            )
        self.assertEqual(raised.exception.kind, "unsupported")
        self.assertEqual(server.counts["/video.ts"], 0)

    def test_failed_open_closes_response_and_redacts_credentials(self):
        master = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="av01.0.04M.08"\nvideo.m3u8\n'
        server = self.server(
            {
                "/user/secret/denied": (403, {}, b"denied"),
                "/user/secret/master": (200, {}, master),
            }
        )
        for path, kind in (("/user/secret/denied", "http"), ("/user/secret/master", "unsupported")):
            with self.subTest(path=path):
                session = requests.Session()
                responses = []
                get = session.get

                def capture(*args, **kwargs):
                    response = get(*args, **kwargs)
                    responses.append(response)
                    return response

                with patch("services.live.source.requests.Session", return_value=session):
                    with patch.object(session, "get", side_effect=capture):
                        with patch.object(session, "close", wraps=session.close) as close:
                            with source_logs() as logs:
                                with self.assertRaises(SourceError) as raised:
                                    open_source(
                                        server.url + path,
                                        headers={},
                                        verify_ssl=True,
                                        stop=threading.Event(),
                                    )
                            close.assert_called_once()
                self.assertTrue(responses[0].raw.closed)
                self.assertEqual(raised.exception.kind, kind)
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn(server.url, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)
                self.assertFalse(any("secret" in line for line in logs))

    def test_close_from_another_thread_aborts_a_stalled_body_read(self):
        # Close-delimited bodies are woken by the response's socket shutdown; keep-alive
        # bodies (sized, connection still pooled) by the connection's duplicate descriptor.
        # On Windows plain sockets are closed and TLS sockets wait in slices.
        head = self.video[: 188 * 50]
        with self.optional_tls_context() as context, insecure():
            for tls, keep_alive in ((False, False), (False, True), (True, False), (True, True)):
                release = threading.Event()  # per subtest: every server must stall

                def stalled(request, release=release):
                    request.wfile.write(head)
                    request.wfile.flush()
                    release.wait(10)

                with self.subTest(tls=tls, keep_alive=keep_alive):
                    if tls:
                        self.skip_without_tls(context)
                    headers = {"Content-Length": str(1 << 30)} if keep_alive else {}
                    server = self.server(
                        {"/user/secret/live.ts": (200, headers, stalled)},
                        tls=context if tls else None,
                        keep_alive=keep_alive,
                    )
                    source = open_source(
                        server.url + "/user/secret/live.ts",
                        headers={},
                        verify_ssl=not tls,
                        stop=threading.Event(),
                    )
                    items, errors = [], []
                    received, finished = threading.Event(), threading.Event()

                    def consume(source=source, items=items, errors=errors):
                        try:
                            for item in source.chunks():
                                items.append(item)
                                received.set()
                        except Exception as error:
                            errors.append(error)
                        finally:
                            finished.set()

                    close = requests.Response.close

                    def slow_close(response):
                        time.sleep(0.5)  # like a close waiting for the reader's buffer lock
                        close(response)

                    try:
                        threading.Thread(target=consume, daemon=True).start()
                        self.assertTrue(received.wait(5))
                        time.sleep(0.2)  # the consumer is now blocked waiting for more bytes
                        started = time.monotonic()
                        with patch.object(requests.Response, "close", slow_close):
                            source.close()
                        self.assertLess(
                            time.monotonic() - started, 0.2, "close() blocked its caller"
                        )
                        self.assertTrue(
                            finished.wait(1.0), "close() did not abort the blocked read"
                        )
                        self.assertLess(time.monotonic() - started, 1.0)
                        self.assertEqual(errors, [])
                        self.assertEqual(b"".join(items), head)
                        self.assertEqual(server.counts["/user/secret/live.ts"], 1)
                    finally:
                        release.set()

    def test_close_aborts_an_hls_segment_stalled_before_its_headers(self):
        with self.optional_tls_context() as context, insecure():
            for tls in (False, True):
                release = threading.Event()

                def stalled(_request, _count, release=release):
                    release.wait(10)
                    return 200, {}, self.next_video

                with self.subTest(tls=tls):
                    if tls:
                        self.skip_without_tls(context)
                    server = self.server(
                        {
                            "/channel.m3u8": (200, {}, leaf("a.ts", "b.ts")),
                            "/a.ts": (200, {}, self.video),
                            "/b.ts": stalled,
                        },
                        tls=context if tls else None,
                    )
                    try:
                        source = open_source(
                            server.url + "/channel.m3u8",
                            headers={},
                            verify_ssl=not tls,
                            stop=threading.Event(),
                        )
                        items, errors = [], []
                        finished = threading.Event()

                        def consume(source=source, items=items, errors=errors):
                            try:
                                items.extend(source.chunks())
                            except Exception as error:
                                errors.append(error)
                            finally:
                                finished.set()

                        threading.Thread(target=consume, daemon=True).start()
                        deadline = time.monotonic() + 5
                        while server.counts["/b.ts"] == 0 and time.monotonic() < deadline:
                            time.sleep(0.01)
                        time.sleep(0.2)
                        started = time.monotonic()
                        source.close()
                        self.assertTrue(
                            finished.wait(1.0), "close() did not abort the stalled request"
                        )
                        self.assertLess(time.monotonic() - started, 1.0)
                        self.assertEqual(errors, [])
                        self.assertEqual(b"".join(items), self.video)
                    finally:
                        release.set()

    def test_stop_aborts_an_open_stalled_mid_header_or_mid_handshake(self):
        release = threading.Event()

        def stalled(_request, _count):
            release.wait(10)
            return 200, {}, self.video

        # A listener that accepts TCP but never answers the TLS handshake.
        silent = socket.create_server(("127.0.0.1", 0))
        held = []
        threading.Thread(target=lambda: held.append(silent.accept()), daemon=True).start()
        try:
            with self.optional_tls_context() as context, insecure():
                plain = self.server({"/user/secret/live.ts": stalled})
                secure = None
                if context is not None:
                    secure = self.server({"/user/secret/live.ts": stalled}, tls=context)
                cases = (
                    ("http header", plain.url + "/user/secret/live.ts", True),
                    ("https header", secure and secure.url + "/user/secret/live.ts", False),
                    (
                        "https handshake",  # a raw listener: no certificate needed
                        f"https://127.0.0.1:{silent.getsockname()[1]}/user/secret/live.ts",
                        True,
                    ),
                )
                for name, url, verify_ssl in cases:
                    with self.subTest(name):
                        if url is None:
                            self.skip_without_tls(context)
                        stop = threading.Event()
                        timer = threading.Timer(0.3, stop.set)
                        timer.start()
                        started = time.monotonic()
                        with self.assertRaises(SourceError) as raised:
                            open_source(url, headers={}, verify_ssl=verify_ssl, stop=stop)
                        timer.join()
                        self.assertLess(time.monotonic() - started, 1.3)
                        self.assertNotIn("secret", str(raised.exception))
        finally:
            release.set()
            for connection, _address in held:
                connection.close()
            silent.close()

    def test_tls_waits_longer_than_a_slice_keep_the_overall_timeout(self):
        # Windows TLS sockets wait in 0.25 s slices: late bytes must still arrive, and the
        # caller's whole timeout must still apply.
        half = len(self.video) // 2
        release = threading.Event()

        def slow(_request, _count):
            time.sleep(0.6)  # late headers

            def body(request):
                request.wfile.write(self.video[:half])
                request.wfile.flush()
                time.sleep(0.6)  # a pause mid-body
                request.wfile.write(self.video[half:])

            return 200, {"Content-Length": str(len(self.video))}, body

        def stalled(_request, _count):
            release.wait(10)
            return 200, {}, b""

        try:
            with self.tls_context() as context, insecure():
                server = self.server({"/slow.ts": slow, "/stalled.ts": stalled}, tls=context)
                self.assertEqual(media(server.url + "/slow.ts", verify_ssl=False), [self.video])
                with patch.object(source_module, "_TIMEOUT", 1.0):
                    started = time.monotonic()
                    with self.assertRaises(SourceError) as raised:
                        open_source(
                            server.url + "/stalled.ts",
                            headers={},
                            verify_ssl=False,
                            stop=threading.Event(),
                        )
                    elapsed = time.monotonic() - started
        finally:
            release.set()
        self.assertEqual(raised.exception.kind, "timeout")
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 2.0)

    def test_requests_does_not_pass_its_own_ssl_context_to_urllib3(self):
        # The Windows interrupt mode installs its sliced TLS socket class only into a TLS
        # context it builds itself; a requests upgrade that passed its own would lose it.
        adapter = requests.adapters.HTTPAdapter()
        request = requests.Request("GET", "https://example.invalid/live.ts").prepare()
        try:
            for verify in (True, False):
                with self.subTest(verify=verify):
                    _host, pool = adapter.build_connection_pool_key_attributes(request, verify)
                    self.assertNotIn("ssl_context", pool)
            self.assertNotIn("ssl_context", adapter.poolmanager.connection_pool_kw)
        finally:
            adapter.close()

    def test_raw_ts_reconnects_after_the_server_drops_mid_stream(self):
        half = len(self.video) // 2 // 188 * 188

        def live(_request, count):
            if count == 1:

                def broken(request):
                    request.wfile.write(self.video[:half])

                return 200, {"Content-Length": str(len(self.video))}, broken
            return 200, {}, self.video[half:]

        server = self.server({"/user/secret/live.ts": live})
        started = time.monotonic()
        with source_logs() as logs:
            parts = media(server.url + "/user/secret/live.ts")
        self.assertGreaterEqual(time.monotonic() - started, 0.45)  # first backoff is 0.5 s
        self.assertEqual(parts, [self.video[:half], Discontinuity(), self.video[half:]])
        self.assertEqual(server.counts["/user/secret/live.ts"], 2)
        self.assertTrue(logs)
        self.assertFalse(any("secret" in line or "127.0.0.1" in line for line in logs))

    def test_a_raw_ts_body_that_stalls_after_data_reconnects_at_the_stall_timeout(self):
        # Once media flowed, a silent connection is broken after the raw TS stall timeout
        # (20 s in production) instead of the 60 s read timeout. The first byte keeps the full
        # timeout.
        head = self.video[: 188 * 50]
        rest = self.video[188 * 50 :]
        tls_context = self.enterContext(self.optional_tls_context())
        self.enterContext(insecure())
        for tls, keep_alive in ((False, False), (False, True), (True, False), (True, True)):
            release = threading.Event()
            marks = {}

            def live(_request, count, release=release, marks=marks, keep_alive=keep_alive):
                if count > 1:
                    marks.setdefault("reconnected", time.monotonic())
                    return 200, {"Content-Length": str(len(rest))}, rest

                def stalled(request):
                    time.sleep(0.6)  # a first byte slower than the stall timeout
                    request.wfile.write(head)
                    request.wfile.flush()
                    marks["stalled"] = time.monotonic()
                    release.wait(3)  # then the connection stays open, silent

                headers = {"Content-Length": str(1 << 30)} if keep_alive else {}
                return 200, headers, stalled

            with self.subTest(tls=tls, keep_alive=keep_alive):
                if tls:
                    self.skip_without_tls(tls_context)
                self.addCleanup(release.set)
                context = tls_context if tls else None
                server = self.server({"/live.ts": live}, tls=context, keep_alive=keep_alive)
                fast = {"_TS_STALL_TIMEOUT": 0.3, "_RECONNECT_DELAYS": FAST_RETRIES}
                with patch.multiple(source_module, **fast):
                    parts = media(server.url + "/live.ts", verify_ssl=not tls, reconnecting=True)
                # The helper hears that a new connection opens before it waits for one.
                self.assertEqual(parts, [head, Reconnecting(), Discontinuity(), rest])
                self.assertEqual(server.counts["/live.ts"], 2)
                waited = marks["reconnected"] - marks["stalled"]
                self.assertGreaterEqual(waited, 0.25)
                self.assertLess(waited, 1.3, "the stalled connection was kept too long")

    def test_a_raw_ts_pause_longer_than_an_hls_stall_goes_on_in_the_same_connection(self):
        # One provider pauses its raw TS for up to ~14 s and then goes on: a pause past the
        # HLS stall timeout but within the raw TS one is waited out, with no reconnect.
        head, rest = self.video[: 188 * 50], self.video[188 * 50 :]

        def paused(request):
            request.wfile.write(head)
            request.wfile.flush()
            time.sleep(0.8)
            request.wfile.write(rest)

        server = self.server({"/live.ts": (200, {"Content-Length": str(len(self.video))}, paused)})
        fast = {"_STALL_TIMEOUT": 0.3, "_TS_STALL_TIMEOUT": 2.0, "_RECONNECT_DELAYS": FAST_RETRIES}
        with patch.multiple(source_module, **fast):
            parts = media(server.url + "/live.ts", reconnecting=True)
        self.assertEqual(b"".join(parts), self.video)
        self.assertNotIn(Discontinuity(), parts)
        self.assertNotIn(Reconnecting(), parts)
        self.assertEqual(server.counts["/live.ts"], 1)

    def test_raw_ts_follows_redirects_to_another_port(self):
        # An Xtream load balancer answers a channel's link with a 302 to a stream server on
        # another port, which may redirect once more.
        stream = self.server({"/live/play/1.ts": (200, {}, self.video)})
        hop = self.server(
            {"/balance/1.ts": (302, {"Location": stream.url + "/live/play/1.ts"}, b"")}
        )
        entry = self.server(
            {"/live/user/secret/1.ts": (302, {"Location": hop.url + "/balance/1.ts"}, b"")}
        )
        parts = media(entry.url + "/live/user/secret/1.ts", headers={"User-Agent": "Provider/3.0"})
        self.assertEqual(parts, [self.video])
        for server in (entry, hop, stream):
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(server.requests[0][1]["User-Agent"], "Provider/3.0")

    def test_the_open_says_the_channel_answered_before_its_first_byte(self):
        # An on-demand restream answers at once and starts its body seconds later (VLC's
        # second request then finds it warm): the open says so as the headers come, so the
        # helper can tell the GUI to wait for the body rather than play directly.
        released = threading.Event()
        self.addCleanup(released.set)

        def warming_up(request):
            released.wait(5)
            request.wfile.write(self.video)

        sized = {"Content-Length": str(len(self.video))}
        server = self.server({"/live.ts": (200, sized, warming_up)})
        answered = []

        def on_answer(answering):
            answered.append((answering, time.monotonic()))
            threading.Timer(0.3, released.set).start()  # the body follows a while later

        started = time.monotonic()
        source = open_source(
            server.url + "/live.ts",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            on_answer=on_answer,
        )
        self.assertEqual([answering for answering, _at in answered], [True])
        self.assertLess(answered[0][1] - started, 2.0)  # not after the body's 5 s
        self.assertEqual(drain(source), [self.video])

    def test_an_answer_is_taken_back_once_its_body_fails_or_turns_out_a_playlist(self):
        # 2.1 review M2/M4: the GUI waits longer only while a raw-TS answer stands. One whose
        # body ends before its first byte is taken back before it is asked for again, and so is
        # one whose body is a playlist after all (an HLS channel waits longer only once segment
        # bytes flow).
        def empty_first(_request, count):
            return (200, {}, b"") if count == 1 else (200, {}, self.video)

        server = self.server(
            {
                "/cold.ts": empty_first,
                "/play": (200, {}, leaf("video.ts")),  # neither its type nor its path says so
                "/video.ts": (200, {}, self.video),
            }
        )
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            for path, calls in (("/cold.ts", [True, False, True]), ("/play", [True, False])):
                with self.subTest(path):
                    answered = []
                    source = open_source(
                        server.url + path,
                        headers={},
                        verify_ssl=True,
                        stop=threading.Event(),
                        on_answer=answered.append,
                    )
                    self.assertEqual(drain(source), [self.video])
                    self.assertEqual(answered, calls)

    def test_an_answer_that_names_a_playlist_is_no_cold_restream(self):
        # The longer wait is for raw TS (cold restreams): an HLS playlist that answers says
        # nothing until segment bytes flow, by its content type or its path.
        playlist = leaf("video.ts")
        server = self.server(
            {
                "/live.m3u8": (200, {}, playlist),
                "/play": (200, {"Content-Type": "application/vnd.apple.mpegurl"}, playlist),
                "/legacy": (200, {"Content-Type": "audio/x-mpegURL"}, playlist),
                "/video.ts": (200, {}, self.video),
            }
        )
        for path in ("/live.m3u8", "/play", "/legacy"):
            with self.subTest(path):
                answered = []
                source = open_source(
                    server.url + path,
                    headers={},
                    verify_ssl=True,
                    stop=threading.Event(),
                    on_answer=answered.append,
                )
                self.assertEqual(drain(source), [self.video])
                self.assertEqual(answered, [])

    def test_a_first_answer_that_failed_in_passing_is_asked_for_again(self):
        # An on-demand restream may refuse the request that starts it, with a 5xx or a body
        # that ends before its first byte, and serve the next one, as VLC's own retry finds.
        def once(first):
            return lambda _request, count: first if count == 1 else (200, {}, self.video)

        cases = {
            "/busy.ts": (503, {}, b""),
            "/gateway.ts": (502, {}, b""),
            "/empty.ts": (200, {}, b""),  # Content-Length: 0
            "/closed.ts": (200, {}, lambda _request: None),  # no length: the FIN at once
        }
        server = self.server({path: once(first) for path, first in cases.items()})
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            for path in cases:
                with self.subTest(path):
                    self.assertEqual(media(server.url + path), [self.video])
                    self.assertEqual(server.counts[path], 2)

    def test_a_limit_met_on_asking_again_reports_the_first_failure(self):
        # 2.1 review M1: a panel that counted the first answer may refuse the next with its
        # limit. The channel then plays directly as before 2.1 (the first failure stands, and
        # the GUI's direct-play retries follow), and nothing more is asked.
        def counted(_request, count):
            return (200, {}, b"") if count == 1 else (509, {}, b"")

        server = self.server({"/counted.ts": counted})
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            with self.assertRaises(SourceError) as raised:
                media(server.url + "/counted.ts")
        self.assertEqual(raised.exception.kind, "network")
        self.assertNotEqual(str(raised.exception), STREAM_LIMIT)
        self.assertEqual(server.counts["/counted.ts"], 2)

    def test_a_host_that_does_not_resolve_is_not_asked_again(self):
        # A DNS failure is no restream warming up: the fallback comes at once, not 3.5 s later.
        from urllib3.exceptions import MaxRetryError, NameResolutionError

        failure = requests.exceptions.ConnectionError(
            MaxRetryError(None, "/", NameResolutionError("nowhere.invalid", None, OSError()))
        )
        with patch.object(source_module._Http, "get", side_effect=failure) as get:
            with self.assertRaises(SourceError) as raised:
                open_source(
                    "http://nowhere.invalid/live.ts",
                    headers={},
                    verify_ssl=True,
                    stop=threading.Event(),
                )
        self.assertEqual(raised.exception.kind, "network")
        self.assertEqual(get.call_count, 1)

    def test_a_first_answer_that_keeps_failing_ends_with_its_own_error(self):
        server = self.server(
            {
                "/down.ts": (503, {}, b""),
                "/silent.ts": (200, {}, b""),
                "/missing.ts": (404, {}, b""),
            }
        )
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            for path, kind, status, asked in (
                ("/down.ts", "http", 503, 4),  # the first answer and the first three delays
                ("/silent.ts", "network", None, 4),
                ("/missing.ts", "http", 404, 1),  # an answer that means it: never again
            ):
                with self.subTest(path):
                    with self.assertRaises(SourceError) as raised:
                        media(server.url + path)
                    self.assertEqual(
                        (raised.exception.kind, raised.exception.status), (kind, status)
                    )
                    self.assertEqual(server.counts[path], asked)

    def test_raw_ts_reconnect_gives_up_after_six_tries(self):
        half = len(self.video) // 2 // 188 * 188

        def live(_request, count):
            if count == 1:

                def broken(request):
                    request.wfile.write(self.video[:half])

                return 200, {"Content-Length": str(len(self.video))}, broken
            return 503, {}, b""

        server = self.server({"/user/secret/live.ts": live})
        source = open_source(
            server.url + "/user/secret/live.ts", headers={}, verify_ssl=True, stop=threading.Event()
        )
        received = []
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            with self.assertRaises(SourceError) as raised:
                for item in source.chunks():
                    received.append(item)
        self.assertEqual(raised.exception.kind, "network")
        self.assertIsNone(raised.exception.status)
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(received[-1], Reconnecting())  # said before the retries began
        self.assertEqual(b"".join(received[:-1]), self.video[:half])
        self.assertEqual(server.counts["/user/secret/live.ts"], 7)

    def test_local_paths_and_file_urls_need_allow_local(self):
        with tempfile.TemporaryDirectory(prefix="qitv-source-local-") as directory:
            path = Path(directory) / "local.ts"
            path.write_bytes(self.video)
            for url in (str(path), path.as_uri()):
                with self.subTest(url=url[:5]):
                    self.assertEqual(media(url, allow_local=True), [self.video])
                    with self.assertRaises(SourceError) as raised:
                        media(url)
                    self.assertEqual(raised.exception.kind, "unsupported")
                    self.assertNotIn(directory, str(raised.exception))

    def test_windows_drive_letters_are_paths_and_other_schemes_are_unsupported(self):
        with patch("os.path.splitdrive", ntpath.splitdrive):
            self.assertEqual(_scheme(r"C:\media\channel.ts"), "")
            self.assertEqual(_scheme("c:/media/channel.ts"), "")
            self.assertEqual(_scheme("HTTP://example.invalid/live.ts"), "http")
        for url in (
            "rtsp://example.invalid/live",
            "http://[::1/user/secret/live.ts",
            "http://example.invalid:secret/live.ts",
        ):
            with self.subTest(url=url):
                with self.assertRaises(SourceError) as raised:
                    open_source(url, headers={}, verify_ssl=True, stop=threading.Event())
                self.assertEqual(raised.exception.kind, "unsupported")
                self.assertNotIn("secret", str(raised.exception))
                self.assertIsNone(raised.exception.__context__)

    # ---- raw TS reconnect policy ---------------------------------------------------------

    def test_a_flapping_server_does_not_refill_the_reconnect_budget(self):
        head = self.video[: 188 * 20]

        def flapping(_request, count):
            if count > 20:  # ends a regression that reconnects forever
                return 200, {}, self.video

            def broken(request):
                request.wfile.write(head)

            return 200, {"Content-Length": str(1 << 30)}, broken

        server = self.server({"/live.ts": flapping})
        source = open_source(
            server.url + "/live.ts", headers={}, verify_ssl=True, stop=threading.Event()
        )
        items = []
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            with self.assertRaises(SourceError) as raised:
                for item in source.chunks():
                    items.append(item)
        self.assertEqual(raised.exception.kind, "network")
        self.assertEqual(server.counts["/live.ts"], 7)
        self.assertEqual(items.count(Discontinuity()), 6)

    def test_the_reconnect_budget_refills_after_data_flows_again(self):
        drops = 9  # more than the six-try budget

        def live(_request, count):
            if count > drops:
                return 200, {}, self.video

            def broken(request):
                request.wfile.write(self.video[: 188 * 20])

            return 200, {"Content-Length": str(1 << 30)}, broken

        server = self.server({"/live.ts": live})
        healthy = {"_HEALTHY_SECONDS": 0.0, "_HEALTHY_BYTES": 188 * 20}
        with patch.multiple(source_module, _RECONNECT_DELAYS=FAST_RETRIES, **healthy):
            parts = media(server.url + "/live.ts")
        self.assertEqual(parts.count(Discontinuity()), drops)
        self.assertEqual(parts[-1], self.video)
        self.assertEqual(server.counts["/live.ts"], drops + 1)

    def test_only_a_declared_length_ends_a_raw_ts_body(self):
        # A live body that ends without reaching a Content-Length is a disconnect, also when
        # it ends cleanly: with a FIN, or with a chunked body's terminating chunk (nginx when
        # the channel's upstream restarts). Both reconnect; only a sized body is finite.
        half = len(self.video) // 2 // 188 * 188

        def first_half(request):
            request.wfile.write(self.video[:half])  # then a clean FIN

        def chunked_half(request):
            for start in range(0, half, 4096):
                block = self.video[start : min(start + 4096, half)]
                request.wfile.write(b"%x\r\n%s\r\n" % (len(block), block))
            request.wfile.write(b"0\r\n\r\n")

        def ends(body, headers):
            def route(_request, count):
                if count == 1:
                    return 200, headers, body
                return 200, {}, self.video[half:]  # sized: the end of the input

            return route

        server = self.server(
            {
                "/fin.ts": ends(first_half, {}),
                "/chunked.ts": ends(chunked_half, {"Transfer-Encoding": "chunked"}),
                "/sized.ts": (200, {}, self.video),
            }
        )
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            for path in ("/fin.ts", "/chunked.ts"):
                with self.subTest(path):
                    parts = media(server.url + path)
                    self.assertEqual(parts, [self.video[:half], Discontinuity(), self.video[half:]])
            self.assertEqual(media(server.url + "/sized.ts"), [self.video])
        self.assertEqual(server.counts, {"/fin.ts": 2, "/chunked.ts": 2, "/sized.ts": 1})

    def test_http_errors_carry_their_status_and_429_is_never_retried(self):
        def limited(_request, _count):
            return 429, {}, b""

        server = self.server(
            {
                "/full.ts": limited,
                "/channel.m3u8": (200, {}, leaf("a.ts", "b.ts")),
                "/a.ts": (200, {}, self.video),
                "/b.ts": limited,
            }
        )
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            for path in ("/full.ts", "/channel.m3u8"):
                with self.subTest(path=path):
                    with self.assertRaises(SourceError) as raised:
                        media(server.url + path)
                    self.assertEqual(raised.exception.kind, "http")
                    self.assertEqual(raised.exception.status, 429)
        self.assertEqual(server.counts["/full.ts"], 1)
        self.assertEqual(server.counts["/b.ts"], 1)

    def test_a_refusal_for_the_connection_limit_says_so_and_is_never_retried(self):
        def answer(status, body=b""):
            return lambda _request, _count: (status, {}, body)

        cases = {
            "/403.ts": (answer(403), STREAM_REFUSED),  # also a forbidden channel: said softer
            "/403-text.ts": (answer(403, b"Too many connections"), True),
            "/429.ts": (answer(429), True),
            "/458.ts": (answer(458), True),
            "/509.ts": (answer(509), True),
            "/400.ts": (answer(400, b"<h1>Max connections reached for this line</h1>"), True),
            "/401.ts": (answer(401, b"Your connection limit is exceeded"), True),
            "/404.ts": (answer(404, b"Not found"), False),
            # A 5xx that names the limit is no limit refusal (direct play follows, as before
            # 2.1), but it is never asked for again either.
            "/500.ts": (answer(500, b"max connections"), False),
            "/503.ts": (answer(503, b"<h1>Max connections reached for this line</h1>"), False),
        }
        server = self.server({path: route for path, (route, _limited) in cases.items()})
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            for path, (_route, limited) in cases.items():
                with self.subTest(path=path):
                    with self.assertRaises(SourceError) as raised:
                        media(server.url + path)
                    error = raised.exception
                    self.assertEqual((error.kind, error.status), ("http", int(path[1:4])))
                    if isinstance(limited, str):
                        self.assertEqual(str(error), limited)
                    else:
                        self.assertEqual(str(error) == STREAM_LIMIT, limited)
                    self.assertNotIn("127.0.0.1", str(error))
                    self.assertEqual(server.counts[path], 1)  # a limit is never asked again

    # ---- live HLS --------------------------------------------------------------------------

    def test_live_hls_starts_live_delay_seconds_before_the_end(self):
        names = [f"s{number}.ts" for number in range(6)]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/live.m3u8"] = (200, {}, hls(0, names, target=2))
        for delay, first in ((6.0, 3), (3.0, 4), (0.0, 5), (100.0, 0)):
            with self.subTest(live_delay=delay):
                server = self.server(routes)
                stop = threading.Event()
                timer = threading.Timer(0.5, stop.set)  # before the first reload
                timer.start()
                media(server.url + "/live.m3u8", stop=stop, live_delay=delay)
                timer.join()
                fetched = [path[1:] for path, _headers in server.requests if path.endswith(".ts")]
                self.assertEqual(fetched, names[first:])

    def test_live_hls_starts_far_enough_back_for_the_sessions_target(self):
        # 2.1 stage 3: a sparse-PCR channel's 10 s segments fetched from 6 s before the end
        # gave one segment, and VLC ran dry before the next landed. Now: at least three
        # target durations (or HOLD-BACK), and room for a start at the target (the segment
        # duration under Auto, plus what Auto added) and one more segment.
        names = [f"s{number}.ts" for number in range(8)]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        cases = (
            # (target duration, HOLD-BACK, target, extra, first segment fetched)
            (10, None, 2.5, 0.0, 5),  # 30 s: three segments
            (10, None, 2.5, 4.0, 4),  # Auto learned 4 s more: 34 s
            (6, None, 2.5, 0.0, 5),  # 18 s, which holds a start at 6 s plus a segment
            (10, 45.0, 2.5, 0.0, 3),  # the playlist's HOLD-BACK
            (10, 10.0, 2.5, 0.0, 5),  # a HOLD-BACK too short for the target: 25 s
            (2, None, 6.0, 0.0, 2),  # Stable's 6 s target on 2 s segments: 6 + 3 < 12
            (10, 500.0, 2.5, 0.0, 0),  # more than the playlist lists: all of it
        )
        for duration, hold_back, target, extra, first in cases:
            with self.subTest(duration=duration, hold_back=hold_back, extra=extra):
                control = (
                    "" if hold_back is None else f"#EXT-X-SERVER-CONTROL:HOLD-BACK={hold_back}\n"
                )
                routes["/live.m3u8"] = (200, {}, hls(0, names, target=duration, extra=control))
                server = self.server(routes)
                stop = threading.Event()
                timer = threading.Timer(0.5, stop.set)  # before the first reload
                timer.start()
                delay = 12.0 if target == 6.0 else 6.0
                media(
                    server.url + "/live.m3u8",
                    stop=stop,
                    live_delay=delay,
                    target=target,
                    extra=extra,
                )
                timer.join()
                self.assertEqual(requested_segments(server), names[first:])

    def test_hls_says_whether_its_playlist_is_finished_and_which_segments_came_whole(self):
        # Review I1: a finished (ENDLIST) playlist plays from its beginning; I3: a segment
        # that failed leaves the backlog, and the media/EXTINF ratio counts whole ones only.
        names = ["a.ts", "b.ts", "c.ts"]
        routes = {"/a.ts": (200, {}, self.video), "/c.ts": (200, {}, self.video)}
        routes["/b.ts"] = (404, {}, b"Not found")
        for ended in (True, False):
            with self.subTest(ended=ended):
                routes["/live.m3u8"] = (200, {}, hls(0, names, target=2, ended=ended))
                server = self.server(dict(routes))
                stop = threading.Event()
                timer = threading.Timer(0.5, stop.set)  # before the first reload
                timer.start()
                source = open_source(
                    server.url + "/live.m3u8",
                    headers={},
                    verify_ssl=True,
                    stop=stop,
                    live_delay=100.0,
                )
                seen = []
                try:
                    for item in source.chunks():
                        if isinstance(item, bytes):
                            seen.append((source.segment_serial, source.last_whole))
                finally:
                    source.close()
                    timer.join()
                self.assertIs(source.finite, ended)
                self.assertEqual(seen[0], (1, None))  # a.ts is under way
                self.assertEqual(seen[-1], (3, (1, 2.0)))  # c.ts: a.ts came whole, b.ts not
                self.assertEqual(source.segment_s, 0.0)

    def test_a_failed_segment_leaves_the_backlog(self):
        names = ["a.ts", "b.ts"]
        routes = {"/a.ts": (200, {}, self.video), "/b.ts": (404, {}, b"Not found")}
        routes["/live.m3u8"] = (200, {}, hls(0, names, target=2, ended=True))
        server = self.server(routes)
        source = open_source(
            server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
        )
        failed = []
        real_skip = source._skip

        def skip(*args, **kwargs):
            real_skip(*args, **kwargs)
            failed.append((source.segment_serial, source.segment_s))

        source._skip = skip
        try:
            for _ in source.chunks():
                pass
        finally:
            source.close()
        self.assertEqual(failed, [(2, 0.0)])

    def test_live_hls_says_how_much_listed_media_is_still_to_come(self):
        # 2.1 stage 3: the helper predicts the live edge once the backlog fetched at the
        # start has arrived, so the start need not wait for all of it.
        names = ["a.ts", "b.ts", "c.ts"]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/live.m3u8"] = (200, {}, hls(0, names, target=2, ended=True))
        server = self.server(routes)
        source = open_source(
            server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
        )
        seen = []
        try:
            for item in source.chunks():
                listed = (source.segment_serial, source.segment_s, source.rest_s)
                if not seen or seen[-1] != listed:
                    seen.append(listed)
        finally:
            source.close()
        # (the segment fetched, its EXTINF, the EXTINF of the listed ones after it)
        self.assertEqual(seen, [(1, 2.0, 4.0), (2, 2.0, 2.0), (3, 2.0, 0.0), (3, 0.0, 0.0)])

    def test_live_hls_says_when_it_delivered_every_listed_segment(self):
        # The helper reports its live edge on it: the end of a batch is otherwise reported
        # only with the next one (its state messages are throttled to four a second).
        copies = [hls(0, ["a.ts", "b.ts"]), hls(1, ["b.ts", "c.ts"], ended=True)]

        def playlist(_request, count):
            return 200, {}, copies[min(count, 2) - 1]

        routes = {f"/{name}.ts": (200, {}, self.video) for name in "abc"}
        routes["/live.m3u8"] = playlist
        server = self.server(routes)
        parts = media(server.url + "/live.m3u8", live_delay=100.0, caught_up=True)
        markers = [type(part).__name__ for part in parts if not isinstance(part, bytes)]
        self.assertEqual(markers, ["CaughtUp", "CaughtUp"])
        self.assertIsInstance(parts[1], CaughtUp)  # right after a.ts and b.ts
        self.assertIsInstance(parts[-1], CaughtUp)  # and after c.ts

    def test_a_media_sequence_restart_resumes_at_the_new_live_start(self):
        cases = {
            "new segment names": (
                hls(100, ["s100.ts", "s101.ts", "s102.ts"]),
                hls(0, ["r0.ts", "r1.ts", "r2.ts"]),
                hls(1, ["r1.ts", "r2.ts", "r3.ts"], ended=True),
                ["s102.ts", "r2.ts", "r3.ts"],
            ),
            "new discontinuity sequence": (
                hls(100, ["x.ts", "y.ts", "z.ts"], extra="#EXT-X-DISCONTINUITY-SEQUENCE:0\n"),
                hls(5, ["y.ts", "z.ts", "n.ts"], extra="#EXT-X-DISCONTINUITY-SEQUENCE:1\n"),
                hls(
                    6,
                    ["z.ts", "n.ts", "m.ts"],
                    ended=True,
                    extra="#EXT-X-DISCONTINUITY-SEQUENCE:1\n",
                ),
                ["z.ts", "n.ts", "m.ts"],
            ),
        }
        for name, (first, second, third, fetched) in cases.items():
            with self.subTest(name):
                copies = [first, second, third]

                def playlist(_request, count, copies=copies):
                    return 200, {}, copies[min(count, 3) - 1]

                data = dict(zip(fetched, (self.video, self.next_video, self.video)))
                routes = {f"/{segment}": (200, {}, body) for segment, body in data.items()}
                routes["/live.m3u8"] = playlist
                server = self.server(routes)
                parts = media(server.url + "/live.m3u8", live_delay=1.0)
                self.assertEqual(
                    parts, joined(self.video, Discontinuity(), self.next_video, self.video)
                )
                requested = [path[1:] for path, _headers in server.requests if path.endswith(".ts")]
                self.assertEqual(requested, fetched)

    def test_a_stale_playlist_copy_is_waited_out(self):
        # The second copy lags behind and overlaps: an older copy from a lagging CDN node.
        shapes = [(10, "abc", False), (9, "zab", False), (11, "bcd", True)]
        cases = (
            ("static names", False, (None, None, None)),
            ("rotating tokens", True, (None, None, None)),
            ("lower discontinuity sequence", False, (5, 4, 5)),
        )
        for case, tokens, discontinuities in cases:
            with self.subTest(case):

                def playlist(_request, count, tokens=tokens, discontinuities=discontinuities):
                    sequence, letters, ended = shapes[min(count, 3) - 1]
                    names = [f"{letter}.ts" for letter in letters]
                    if tokens:
                        names = [f"tok{count}/{name}?token={count}" for name in names]
                    discontinuity = discontinuities[min(count, 3) - 1]
                    extra = ""
                    if discontinuity is not None:
                        extra = f"#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuity}\n"
                    return 200, {}, hls(sequence, names, ended=ended, extra=extra)

                routes = TokenRoutes({f"/{name}.ts": (200, {}, self.video) for name in "abcz"})
                routes["/d.ts"] = (200, {}, self.next_video)
                routes["/live.m3u8"] = playlist
                server = self.server(routes)
                parts = media(server.url + "/live.m3u8", live_delay=100.0)
                self.assertEqual(parts, joined(self.video, self.video, self.video, self.next_video))
                self.assertEqual(requested_segments(server), ["a.ts", "b.ts", "c.ts", "d.ts"])
                self.assertEqual(server.counts["/live.m3u8"], 3)

    def test_an_empty_playlist_copy_is_waited_out(self):
        window = [f"w{number}.ts" for number in range(10)]

        def playlist(_request, count):
            if count == 1:
                return 200, {}, hls(100, window)
            if count == 2:
                return 200, {}, hls(100, [])
            return 200, {}, hls(101, window[1:] + ["w10.ts"], ended=True)

        routes = {f"/{name}": (200, {}, self.video) for name in window}
        routes["/w10.ts"] = (200, {}, self.next_video)
        routes["/live.m3u8"] = playlist
        server = self.server(routes)
        parts = media(server.url + "/live.m3u8", live_delay=1.0)
        self.assertEqual(parts, joined(self.video, self.next_video))
        self.assertEqual(requested_segments(server), ["w9.ts", "w10.ts"])

    def test_a_lagging_copy_that_persists_counts_as_a_restart(self):
        def playlist(_request, count):
            if count == 1:
                return 200, {}, hls(10, ["a.ts", "b.ts", "c.ts"])
            # The sequence went back but the names overlap: stale, until it persists.
            return 200, {}, hls(0, ["b.ts", "c.ts", "d.ts"], ended=count >= 5)

        server = self.server(
            {
                "/live.m3u8": playlist,
                "/c.ts": (200, {}, self.video),
                "/d.ts": (200, {}, self.next_video),
            }
        )
        with patch.object(source_module, "_STALE_TARGETS", 1):  # one target duration
            parts = media(server.url + "/live.m3u8", live_delay=1.0)
        self.assertEqual(parts, joined(self.video, Discontinuity(), self.next_video))
        self.assertEqual(requested_segments(server), ["c.ts", "d.ts"])

    def test_falling_behind_the_window_resumes_at_the_live_start(self):
        def playlist(_request, count):
            if count == 1:
                return 200, {}, hls(10, ["s10.ts"])
            if count == 2:
                return 200, {}, hls(13, ["s13.ts", "s14.ts", "s15.ts", "s16.ts"])
            return 200, {}, hls(14, ["s14.ts", "s15.ts", "s16.ts", "s17.ts"], ended=True)

        server = self.server(
            {
                "/live.m3u8": playlist,
                "/s10.ts": (200, {}, self.video),
                "/s16.ts": (200, {}, self.next_video),
                "/s17.ts": (200, {}, self.video),
            }
        )
        parts = media(server.url + "/live.m3u8", live_delay=1.0)
        self.assertEqual(parts, joined(self.video, Discontinuity(), self.next_video, self.video))
        self.assertEqual(requested_segments(server), ["s10.ts", "s16.ts", "s17.ts"])

    def test_a_stalled_segment_is_skipped_while_the_playlist_advances(self):
        release = threading.Event()
        head = self.video[: 188 * 100]

        def stalled(request):
            request.wfile.write(head)
            request.wfile.flush()
            release.wait(10)

        def playlist(_request, count):
            return 200, {}, hls(count, [f"s{count}.ts"], ended=count >= 4)

        server = self.server(
            {
                "/live.m3u8": playlist,
                "/s1.ts": (200, {}, self.video),
                "/s2.ts": (200, {"Content-Length": str(len(self.video))}, stalled),
                "/s3.ts": (200, {}, self.next_video),
                "/s4.ts": (200, {}, self.video),
            }
        )
        # As in production, a segment read may time out exactly as late as the frozen timer.
        try:
            with patch.multiple(source_module, _TIMEOUT=1.0, _FROZEN_TIMEOUT=1.0):
                parts = media(server.url + "/live.m3u8", live_delay=1.0)
        finally:
            release.set()
        self.assertEqual(
            parts, joined(self.video, head, Discontinuity(), self.next_video, self.video)
        )

    def test_a_segment_that_stalls_after_data_is_skipped_at_the_stall_timeout(self):
        release = threading.Event()
        head = self.video[: 188 * 100]
        marks = {}

        def stalled(request):
            request.wfile.write(head)
            request.wfile.flush()
            marks["stalled"] = time.monotonic()
            release.wait(3)

        def next_segment(_request, _count):
            marks.setdefault("skipped", time.monotonic())
            return 200, {}, self.next_video

        def playlist(_request, count):
            return 200, {}, hls(count, [f"s{count}.ts"], ended=count >= 4)

        server = self.server(
            {
                "/live.m3u8": playlist,
                "/s1.ts": (200, {}, self.video),
                "/s2.ts": (200, {"Content-Length": str(len(self.video))}, stalled),
                "/s3.ts": next_segment,
                "/s4.ts": (200, {}, self.video),
            }
        )
        try:
            with patch.object(source_module, "_STALL_TIMEOUT", 0.3):
                parts = media(server.url + "/live.m3u8", live_delay=1.0)
        finally:
            release.set()
        self.assertEqual(
            parts, joined(self.video, head, Discontinuity(), self.next_video, self.video)
        )
        # The skip comes at the stall timeout; s3 then waits for the next reload (1 s).
        self.assertLess(marks["skipped"] - marks["stalled"], 2.0)

    def test_a_frozen_live_playlist_times_out(self):
        server = self.server(
            {"/live.m3u8": (200, {}, hls(5, ["a.ts"])), "/a.ts": (200, {}, self.video)}
        )
        stop = threading.Event()
        timer = threading.Timer(5.0, stop.set)  # ends a regression that reloads forever
        timer.start()
        self.addCleanup(timer.cancel)
        source = open_source(server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=stop)
        items = []
        with patch.object(source_module, "_FROZEN_TIMEOUT", 1.5):
            with self.assertRaises(SourceError) as raised:
                for item in source.chunks():
                    items.append(item)
        self.assertEqual(raised.exception.kind, "timeout")
        self.assertIsInstance(items.pop(), CaughtUp)  # a.ts was all the playlist listed
        self.assertEqual(b"".join(items), self.video)
        self.assertGreaterEqual(server.counts["/live.m3u8"], 2)

    def test_missing_and_gap_segments_are_skipped_with_a_marker(self):
        text = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4,\na.ts\n#EXTINF:4,\ngone.ts\n#EXTINF:4,\nb.ts\n"
            "#EXT-X-GAP\n#EXTINF:4,\nhole.ts\n#EXTINF:4,\nc.ts\n"
            "#EXTINF:4,\nexpired.ts\n#EXTINF:4,\nd.ts\n#EXT-X-ENDLIST\n"
        )
        server = self.server(
            {
                "/channel.m3u8": (200, {}, text.encode()),
                "/a.ts": (200, {}, self.video),
                "/b.ts": (200, {}, self.next_video),
                "/hole.ts": (200, {}, self.video),
                "/c.ts": (200, {}, self.video),
                "/expired.ts": (410, {}, b""),
                "/d.ts": (200, {}, self.next_video),
            }
        )  # gone.ts is not routed: 404
        parts = media(server.url + "/channel.m3u8")
        mark = Discontinuity()
        self.assertEqual(
            parts,
            joined(self.video, mark, self.next_video, mark, self.video, mark, self.next_video),
        )
        self.assertEqual(server.counts["/gone.ts"], 1)
        self.assertEqual(server.counts["/expired.ts"], 1)
        self.assertEqual(server.counts["/hole.ts"], 0)

    def test_a_missing_key_or_a_segment_that_exhausts_its_retries_is_skipped(self):
        zero = "IV=0x" + "0" * 32
        text = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\na.ts\n"
            f'#EXT-X-KEY:METHOD=AES-128,URI="gone.key",{zero}\n#EXTINF:4,\nlocked.ts\n'
            "#EXT-X-KEY:METHOD=NONE\n#EXTINF:4,\nb.ts\n#EXTINF:4,\nbusy.ts\n"
            "#EXTINF:4,\nc.ts\n#EXT-X-ENDLIST\n"
        )
        server = self.server(
            {
                "/channel.m3u8": (200, {}, text.encode()),
                "/a.ts": (200, {}, self.video),
                "/locked.ts": (200, {}, encrypt(self.video, bytes(16))),
                "/b.ts": (200, {}, self.next_video),
                "/busy.ts": (503, {}, b""),
                "/c.ts": (200, {}, self.video),
            }
        )  # gone.key is not routed: 404
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            parts = media(server.url + "/channel.m3u8")
        mark = Discontinuity()
        self.assertEqual(parts, joined(self.video, mark, self.next_video, mark, self.video))
        self.assertEqual(server.counts["/gone.key"], 1)
        self.assertEqual(server.counts["/locked.ts"], 0)
        self.assertEqual(server.counts["/busy.ts"], 7)

    def test_adjacent_segment_failures_give_one_marker(self):
        text = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\na.ts\n#EXTINF:4,\ngone.ts\n"
            "#EXTINF:4,\npage.ts\n#EXTINF:4,\nb.ts\n#EXT-X-ENDLIST\n"
        )
        server = self.server(
            {
                "/channel.m3u8": (200, {}, text.encode()),
                "/a.ts": (200, {}, self.video),
                "/page.ts": (200, {}, b"<html>busy</html>" * 20),
                "/b.ts": (200, {}, self.next_video),
            }
        )
        parts = media(server.url + "/channel.m3u8")
        self.assertEqual(parts, joined(self.video, Discontinuity(), self.next_video))

    def test_bad_segments_after_the_first_delivery_are_skipped(self):
        zero = "IV=0x" + "0" * 32
        text = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4,\na.ts\n#EXTINF:4,\npage.ts\n#EXTINF:4,\nb.ts\n"
            f'#EXT-X-KEY:METHOD=AES-128,URI="short.key",{zero}\n#EXTINF:4,\nlocked.ts\n'
            f'#EXT-X-KEY:METHOD=AES-128,URI="good.key",{zero}\n#EXTINF:4,\nc.ts\n'
            "#EXTINF:4,\ncut.ts\n#EXTINF:4,\nd.ts\n#EXT-X-ENDLIST\n"
        )
        server = self.server(
            {
                "/channel.m3u8": (200, {}, text.encode()),
                "/a.ts": (200, {}, self.video),
                "/page.ts": (200, {}, b"<html>busy</html>" * 20),
                "/b.ts": (200, {}, self.next_video),
                "/short.key": (200, {}, KEY[:8]),
                "/locked.ts": (200, {}, encrypt(self.video, bytes(16))),
                "/good.key": (200, {}, KEY),
                "/c.ts": (200, {}, encrypt(self.video, bytes(16))),
                "/cut.ts": (200, {}, encrypt(self.next_video, bytes(16))[:-5]),
                "/d.ts": (200, {}, encrypt(self.next_video, bytes(16))),
            }
        )
        parts = media(server.url + "/channel.m3u8")
        mark = Discontinuity()
        expected = joined(self.video, mark, self.next_video, mark, self.video)
        self.assertEqual(parts[:4], expected[:4])
        self.assertTrue(parts[4].startswith(expected[4]))  # c, then what cut.ts gave
        self.assertEqual(parts[5], mark)
        # d: its counters run on from where cut.ts broke off.
        self.assertEqual(plain(parts[6]), plain(self.next_video))
        self.assertEqual(len(parts), 7)

    def test_three_consecutive_segment_failures_end_the_session(self):
        page = (200, {}, b"<html>busy</html>" * 20)
        for name, routes, kind, status in (
            ("missing", {}, "http", 404),
            ("not ts", {"/x.ts": page, "/y.ts": page, "/z.ts": page}, "unsupported", None),
        ):
            with self.subTest(name):
                routes = dict(routes)
                routes["/channel.m3u8"] = (200, {}, leaf("a.ts", "x.ts", "y.ts", "z.ts", "b.ts"))
                routes["/a.ts"] = (200, {}, self.video)
                routes["/b.ts"] = (200, {}, self.next_video)
                server = self.server(routes)
                source = open_source(
                    server.url + "/channel.m3u8",
                    headers={},
                    verify_ssl=True,
                    stop=threading.Event(),
                )
                items = []
                with self.assertRaises(SourceError) as raised:
                    for item in source.chunks():
                        items.append(item)
                self.assertEqual((raised.exception.kind, raised.exception.status), (kind, status))
                self.assertEqual(b"".join(i for i in items if isinstance(i, bytes)), self.video)
                self.assertEqual(server.counts["/b.ts"], 0)

    # ---- audio renditions, headers and resources -------------------------------------------

    def test_audio_follows_the_default_rendition(self):
        def master(renditions):
            return (
                "#EXTM3U\n"
                + "".join(
                    f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",{rendition}\n'
                    for rendition in renditions
                )
                + '#EXT-X-STREAM-INF:BANDWIDTH=100000,AUDIO="aac"\nvideo.m3u8\n'
            ).encode()

        muxed = 'NAME="English",LANGUAGE="eng"'
        french = 'NAME="French",LANGUAGE="fra",URI="audio/fr.m3u8"'
        video_only = ts_fixtures.make_stream(4, audio=False)
        cases = (
            ("muxed default", [muxed + ",DEFAULT=YES", french], self.video, True),
            ("muxed first", [muxed, french], self.video, True),
            ("external first", [french, muxed], video_only, False),
            ("external first, the variant's own", [french, muxed], self.video, True),
        )
        for name, renditions, segment, accepted in cases:
            with self.subTest(name):
                server = self.server(
                    {
                        "/master.m3u8": (200, {}, master(renditions)),
                        "/video.m3u8": (200, {}, leaf("video.ts")),
                        "/video.ts": (200, {}, segment),
                    }
                )
                with source_logs() as logs:
                    if accepted:
                        self.assertEqual(media(server.url + "/master.m3u8"), [segment])
                    else:
                        with self.assertRaises(SourceError) as raised:
                            media(server.url + "/master.m3u8")
                        self.assertEqual(raised.exception.kind, "unsupported")
                self.assertEqual(server.counts["/audio/fr.m3u8"], 0)
                if accepted:
                    self.assertTrue(any("French" in line for line in logs), logs)

    def test_the_user_agent_and_referer_reach_every_origin_and_no_other_header_does(self):
        # 2.1: an M3U channel's http-referrer goes with its playlists and segments alike,
        # wherever they are (a CDN's hotlink check reads it), as VLC sends it.
        cdn = self.server({"/segment/a.ts": (200, {}, self.video)})

        def live(_request, count):
            names = [cdn.url + "/segment/a.ts"] + (["b.ts"] if count > 1 else [])
            return 200, {}, hls(0, names, ended=count > 1)

        hop = self.server({"/live.m3u8": live, "/b.ts": (200, {}, self.next_video)})
        entry = self.server({"/play": (302, {"Location": hop.url + "/live.m3u8"}, b"")})
        headers = {
            "User-Agent": "Provider/3.0",
            "Referer": "http://portal.invalid/",
            "X-Token": "secret",
            "Cookie": "session=secret",
            "Authorization": "Bearer secret",
            "Host": "evil.invalid",
            "Range": "bytes=0-1",
            "Connection": "close",
            "Content-Length": "5",
        }
        parts = media(entry.url + "/play", headers=headers)
        self.assertEqual(parts, joined(self.video, self.next_video))
        for server in (entry, hop, cdn):
            for _path, received in server.requests:
                self.assertEqual(received["User-Agent"], "Provider/3.0")
                for name in ("X-Token", "Cookie", "Authorization", "Range", "Content-Length"):
                    self.assertNotIn(name, received)
                self.assertTrue(received["Host"].startswith("127.0.0.1:"))
                self.assertNotEqual(received.get("Connection"), "close")
        hop_paths = [path for path, _received in hop.requests]
        self.assertEqual(hop_paths, ["/live.m3u8", "/live.m3u8", "/b.ts"])
        for server in (entry, hop, cdn):  # the cdn holds a segment of its own
            for _path, received in server.requests:
                self.assertEqual(received.get("Referer"), "http://portal.invalid/")

    def test_keep_alive_connections_are_reused_for_reloads_and_segments(self):
        def playlist(_request, count):
            return 200, {}, hls(0, ["a.ts"]) if count == 1 else hls(0, ["a.ts", "b.ts"], ended=True)

        server = self.server(
            {
                "/live.m3u8": playlist,
                "/a.ts": (200, {}, self.video),
                "/b.ts": (200, {}, self.next_video),
            },
            keep_alive=True,
        )
        self.assertEqual(media(server.url + "/live.m3u8"), joined(self.video, self.next_video))
        self.assertEqual(len(server.requests), 4)
        self.assertEqual(server.connections, 1)

    def test_http_resources_close_and_consumer_errors_keep_their_identity(self):
        server = self.server({"/video.ts": (200, {}, self.video)})
        session = requests.Session()
        responses = []
        get = session.get

        def capture(*args, **kwargs):
            response = get(*args, **kwargs)
            responses.append(response)
            return response

        failure = ValueError("consumer mux failure")
        with patch("services.live.source.requests.Session", return_value=session):
            with patch.object(session, "get", side_effect=capture):
                with patch.object(session, "close", wraps=session.close) as close:
                    source = open_source(
                        server.url + "/video.ts",
                        headers={},
                        verify_ssl=True,
                        stop=threading.Event(),
                    )
                    with self.assertRaises(ValueError) as raised:
                        try:
                            for item in source.chunks():
                                self.assertEqual(item[:1], b"\x47")
                                raise failure
                        finally:
                            source.close()
                    self.assertIs(raised.exception, failure)
                    close.assert_called_once()
        self.assertTrue(responses[0].raw.closed)

    def test_burst_is_the_hls_target_duration_and_zero_for_raw_ts(self):
        server = self.server(
            {"/video.ts": (200, {}, self.video), "/channel.m3u8": (200, {}, leaf("video.ts"))}
        )
        with tempfile.TemporaryDirectory(prefix="qitv-source-burst-") as directory:
            path = Path(directory) / "local.ts"
            path.write_bytes(self.video)
            for url, burst in (
                (server.url + "/video.ts", 0.0),
                (server.url + "/channel.m3u8", 4.0),
                (str(path), 0.0),
            ):
                with self.subTest(url=url[-9:]):
                    source = open_source(
                        url, headers={}, verify_ssl=True, stop=threading.Event(), allow_local=True
                    )
                    try:
                        self.assertEqual(source.burst_s, burst)
                    finally:
                        source.close()


class SlicedTlsSocketTests(unittest.TestCase):
    def test_only_a_slice_timeout_is_retried(self):
        # The 0.25 s slice's own timeout has no errno and is retried until the deadline. A
        # Winsock WSAETIMEDOUT (errno 10060) is sticky: retrying it would spin until then.
        sliced = source_module._sliced_tls_socket(source_module._Interrupter())
        sock = sliced.__new__(sliced)  # never connected: only the retry logic runs
        sock._logical_timeout = 5.0
        slice_timeout = TimeoutError("The read operation timed out")
        with patch.object(ssl.SSLSocket, "read", side_effect=[slice_timeout] * 2 + [b"x"]):
            self.assertEqual(sock.read(1), b"x")
        sticky = TimeoutError(10060, "A connection attempt failed")
        with patch.object(ssl.SSLSocket, "read", side_effect=[sticky, b"x"]) as read:
            started = time.monotonic()
            with self.assertRaises(TimeoutError) as raised:
                sock.read(1)
        self.assertIs(raised.exception, sticky)
        self.assertEqual(read.call_count, 1)
        self.assertLess(time.monotonic() - started, 1.0)


class WindowsInterruptModeTests(SourceTestCase):
    """The Windows interrupt mode (no duplicate descriptors), forced here.

    Plain sockets: the owning objects are interrupted (off Windows by a shutdown only; a
    close right after can lose the wakeup on macOS). These tests prove that every plain
    phase hands its owning socket to the interrupter, including a close-delimited body.

    TLS sockets: never touched from another thread (asserted in ``tearDown``). They wait
    in slices and stop on their own, exactly as on Windows, so the https subtests run
    the real mechanism here. The TLS tests also prove that verification is unchanged by
    the TLS context this mode supplies. Waking a plain socket by closing its handle is
    Winsock behaviour, which only the Windows CI run can check.
    """

    def setUp(self):
        super().setUp()
        patcher = patch.object(source_module, "_INTERRUPT_OWNERS", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Nothing outside the reader may touch a TLS socket: on Windows OpenSSL keeps
        # using its raw handle value, which a new socket may already own.
        self.aborted = []
        abort = source_module._abort

        def spy(sock):
            self.aborted.append(type(sock))
            abort(sock)

        spier = patch.object(source_module, "_abort", spy)
        spier.start()
        self.addCleanup(spier.stop)
        self.tls_shutdowns = []
        shutdown = ssl.SSLSocket.shutdown

        def record(sock, how):
            if not sock.server_side:  # the test servers shut their own sockets down
                self.tls_shutdowns.append(how)
            shutdown(sock, how)

        shutdowns = patch.object(ssl.SSLSocket, "shutdown", record)
        shutdowns.start()
        self.addCleanup(shutdowns.stop)

    def tearDown(self):
        super().tearDown()
        touched = [kind.__name__ for kind in self.aborted if issubclass(kind, ssl.SSLSocket)]
        self.assertEqual(touched, [], "a TLS socket was interrupted from another thread")
        self.assertEqual(self.tls_shutdowns, [], "a source's TLS socket was shut down")

    def test_only_plain_connections_hand_over_their_socket(self):
        # An HTTPS connection's raw socket becomes its TLS socket's handle; closing it from
        # another thread, even before the handshake, would close a TLS handle.
        adopted = []
        adopt = source_module._Interrupter.adopt

        def record(interrupter, sock):
            adopted.append(sock)  # a strong reference: the owner set itself is weak
            adopt(interrupter, sock)

        with self.tls_context() as context, insecure():
            with patch.object(source_module._Interrupter, "adopt", record):
                for tls in (False, True):
                    with self.subTest(tls=tls):
                        adopted.clear()
                        server = self.server(
                            {"/live.ts": (200, {}, self.video)}, tls=context if tls else None
                        )
                        source = open_source(
                            server.url + "/live.ts",
                            headers={},
                            verify_ssl=not tls,
                            stop=threading.Event(),
                        )
                        source.close()
                        self.assertEqual(len(adopted), 0 if tls else 1)

    test_close_from_another_thread_aborts_a_stalled_body_read = (
        LiveSourceTests.test_close_from_another_thread_aborts_a_stalled_body_read
    )
    test_close_aborts_an_hls_segment_stalled_before_its_headers = (
        LiveSourceTests.test_close_aborts_an_hls_segment_stalled_before_its_headers
    )
    test_stop_aborts_an_open_stalled_mid_header_or_mid_handshake = (
        LiveSourceTests.test_stop_aborts_an_open_stalled_mid_header_or_mid_handshake
    )
    test_tls_verification_default_rejects_and_explicit_opt_out_decodes = (
        LiveSourceTests.test_tls_verification_default_rejects_and_explicit_opt_out_decodes
    )
    test_http_redirect_to_tls_obeys_verification_setting = (
        LiveSourceTests.test_http_redirect_to_tls_obeys_verification_setting
    )
    test_nested_hls_tls_is_verified_without_enabling_file_protocol = (
        LiveSourceTests.test_nested_hls_tls_is_verified_without_enabling_file_protocol
    )
    test_redirect_connections_close_while_the_source_is_still_open = (
        LiveSourceTests.test_redirect_connections_close_while_the_source_is_still_open
    )
    test_keep_alive_connections_are_reused_for_reloads_and_segments = (
        LiveSourceTests.test_keep_alive_connections_are_reused_for_reloads_and_segments
    )
    test_tls_waits_longer_than_a_slice_keep_the_overall_timeout = (
        LiveSourceTests.test_tls_waits_longer_than_a_slice_keep_the_overall_timeout
    )
    test_a_raw_ts_body_that_stalls_after_data_reconnects_at_the_stall_timeout = (
        LiveSourceTests.test_a_raw_ts_body_that_stalls_after_data_reconnects_at_the_stall_timeout
    )


if __name__ == "__main__":
    unittest.main()
