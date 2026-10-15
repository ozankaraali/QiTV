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
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import warnings

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad
from http_fixtures import LocalSource, leaf
import requests
import ts_fixtures
from urllib3.exceptions import InsecureRequestWarning

from services.live import source as source_module
from services.live.source import Discontinuity, SourceError, _scheme, open_source

KEY = bytes(range(16))
FAST_RETRIES = (0.01,) * 6


def encrypt(data, iv, key=KEY):
    return AES.new(key, AES.MODE_CBC, iv=iv).encrypt(pad(data, 16))


def drain(source):
    """Every item from ``chunks()``, with adjacent byte blocks joined."""
    parts = []
    try:
        for item in source.chunks():
            if isinstance(item, bytes) and parts and isinstance(parts[-1], bytes):
                parts[-1] += item
            else:
                parts.append(item)
    finally:
        source.close()
    return parts


def media(url, *, headers=None, verify_ssl=True, stop=None):
    source = open_source(
        url, headers=headers or {}, verify_ssl=verify_ssl, stop=stop or threading.Event()
    )
    return drain(source)


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


class LiveSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video = ts_fixtures.make_stream(4)
        cls.next_video = ts_fixtures.make_stream(4, pts_start=900000 + 4 * 90000)

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()

    def server(self, routes, tls=None):
        server = LocalSource(routes, tls=tls)
        self.servers.append(server)
        return server

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

    def test_an_external_audio_group_is_unsupported(self):
        master = b'''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="languages",NAME="English",LANGUAGE="eng",DEFAULT=YES,AUTOSELECT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="languages",NAME="French",LANGUAGE="fra",DEFAULT=NO,AUTOSELECT=YES,URI="audio/fr.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100000,CODECS="mp2v,mp4a.40.2",AUDIO="languages"
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=200000,CODECS="mp2v,mp4a.40.2",AUDIO="languages"
high.m3u8
'''
        server = self.server(
            {
                "/master.m3u8": (200, {}, master),
                "/high.m3u8": (200, {}, leaf("video.ts")),
                "/audio/en.m3u8": (200, {}, leaf("en.ts")),
                "/video.ts": (200, {}, self.video),
            }
        )
        with self.assertRaises(SourceError) as raised:
            open_source(
                server.url + "/master.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
            )
        self.assertEqual(raised.exception.kind, "unsupported")
        self.assertEqual(server.counts["/master.m3u8"], 1)
        self.assertEqual(server.counts["/high.m3u8"], 0)
        self.assertEqual(server.counts["/audio/en.m3u8"], 0)

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
        self.assertEqual(media(server.url + "/entry"), [self.video + self.next_video])
        self.assertGreaterEqual(server.counts["/live/channel.m3u8"], 2)
        self.assertEqual(server.counts["/entry"], 1)
        self.assertEqual(server.counts["/live/first.ts"], 1)
        self.assertEqual(server.counts["/live/second.ts"], 1)

    def test_a_media_sequence_gap_on_reload_marks_a_discontinuity(self):
        def playlist(_request, count):
            if count == 1:
                text = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:10\n"
                return 200, {}, (text + "#EXTINF:2,\nten.ts\n").encode()
            text = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:13\n"
            return 200, {}, (text + "#EXTINF:2,\nthirteen.ts\n#EXT-X-ENDLIST\n").encode()

        server = self.server(
            {
                "/live.m3u8": playlist,
                "/ten.ts": (200, {}, self.video),
                "/thirteen.ts": (200, {}, self.next_video),
            }
        )
        parts = media(server.url + "/live.m3u8")
        self.assertEqual(parts, [self.video, Discontinuity(), self.next_video])
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
        self.assertEqual(parts, [self.video, Discontinuity(), self.next_video])

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
        self.assertEqual(parts, [self.video + self.next_video])
        self.assertEqual(server.counts["/live.m3u8"], 4)
        self.assertEqual(server.counts["/b.ts"], 2)

    @contextmanager
    def tls_context(self):
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("openssl is required to generate a local test certificate")
        with tempfile.TemporaryDirectory(prefix="qitv-source-tls-") as directory:
            key = Path(directory) / "key.pem"
            certificate = Path(directory) / "cert.pem"
            subprocess.run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-keyout",
                    str(key),
                    "-out",
                    str(certificate),
                ],
                check=True,
                capture_output=True,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            yield context

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
        self.assertEqual(media(server.url + "/channel.m3u8"), [self.video + self.next_video])
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
        release = threading.Event()
        head = self.video[: 188 * 50]

        def stalled(request):
            request.wfile.write(head)
            request.wfile.flush()
            release.wait(10)

        server = self.server({"/user/secret/live.ts": (200, {}, stalled)})
        try:
            source = open_source(
                server.url + "/user/secret/live.ts",
                headers={},
                verify_ssl=True,
                stop=threading.Event(),
            )
            items, errors = [], []
            received, finished = threading.Event(), threading.Event()

            def consume():
                try:
                    for item in source.chunks():
                        items.append(item)
                        received.set()
                except Exception as error:
                    errors.append(error)
                finally:
                    finished.set()

            threading.Thread(target=consume, daemon=True).start()
            self.assertTrue(received.wait(5))
            time.sleep(0.2)  # the consumer is now blocked waiting for more body bytes
            started = time.monotonic()
            source.close()
            self.assertTrue(finished.wait(1.0), "close() did not abort the blocked read")
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(errors, [])
            self.assertEqual(b"".join(items), head)
            self.assertEqual(server.counts["/user/secret/live.ts"], 1)
        finally:
            release.set()

    def test_close_aborts_an_hls_segment_stalled_before_its_headers(self):
        release = threading.Event()

        def stalled(_request, _count):
            release.wait(10)
            return 200, {}, self.next_video

        server = self.server(
            {
                "/channel.m3u8": (200, {}, leaf("a.ts", "b.ts")),
                "/a.ts": (200, {}, self.video),
                "/b.ts": stalled,
            }
        )
        try:
            source = open_source(
                server.url + "/channel.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
            )
            items, errors = [], []
            finished = threading.Event()

            def consume():
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
            self.assertTrue(finished.wait(1.0), "close() did not abort the stalled request")
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

        server = self.server({"/user/secret/live.ts": stalled})
        # A listener that accepts TCP but never answers the TLS handshake.
        silent = socket.create_server(("127.0.0.1", 0))
        held = []
        threading.Thread(target=lambda: held.append(silent.accept()), daemon=True).start()
        urls = (
            server.url + "/user/secret/live.ts",
            f"https://127.0.0.1:{silent.getsockname()[1]}/user/secret/live.ts",
        )
        try:
            for url in urls:
                with self.subTest(url=url.split(":")[0]):
                    stop = threading.Event()
                    timer = threading.Timer(0.3, stop.set)
                    timer.start()
                    started = time.monotonic()
                    with self.assertRaises(SourceError) as raised:
                        open_source(url, headers={}, verify_ssl=True, stop=stop)
                    timer.join()
                    self.assertLess(time.monotonic() - started, 1.3)
                    self.assertNotIn("secret", str(raised.exception))
        finally:
            release.set()
            for connection, _address in held:
                connection.close()
            silent.close()

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
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(b"".join(received), self.video[:half])
        self.assertEqual(server.counts["/user/secret/live.ts"], 7)

    def test_local_paths_and_file_urls_read_the_file(self):
        with tempfile.TemporaryDirectory(prefix="qitv-source-local-") as directory:
            path = Path(directory) / "local.ts"
            path.write_bytes(self.video)
            self.assertEqual(media(str(path)), [self.video])
            self.assertEqual(media(path.as_uri()), [self.video])

    def test_windows_drive_letters_are_paths_and_other_schemes_are_unsupported(self):
        with patch("os.path.splitdrive", ntpath.splitdrive):
            self.assertEqual(_scheme(r"C:\media\channel.ts"), "")
            self.assertEqual(_scheme("c:/media/channel.ts"), "")
            self.assertEqual(_scheme("HTTP://example.invalid/live.ts"), "http")
        with self.assertRaises(SourceError) as raised:
            open_source(
                "rtsp://example.invalid/live", headers={}, verify_ssl=True, stop=threading.Event()
            )
        self.assertEqual(raised.exception.kind, "unsupported")


if __name__ == "__main__":
    unittest.main()
