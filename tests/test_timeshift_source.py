from collections import Counter
from contextlib import contextmanager
from fractions import Fraction
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
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
from urllib.parse import urlsplit

import av
import requests

from services.timeshift_source import SourceError, open_source


def transport_stream(*, audio=False, offset=0, container="mpegts", options=None):
    destination = io.BytesIO()
    with av.open(destination, "w", format=container, options=options) as output:
        if audio:
            stream = output.add_stream("mp2", rate=48000)
            stream.layout = "mono"
            for sample in range(0, 4 * 48000, 1152):
                frame = av.AudioFrame(format="s16", layout="mono", samples=1152)
                frame.planes[0].update(bytes(frame.planes[0].buffer_size))
                frame.sample_rate = 48000
                frame.pts = sample + offset * 48000
                frame.time_base = Fraction(1, 48000)
                output.mux(stream.encode(frame))
        else:
            stream = output.add_stream("mpeg2video", rate=25)
            stream.width, stream.height = 96, 64
            stream.pix_fmt = "yuv420p"
            stream.codec_context.gop_size = 25
            stream.codec_context.max_b_frames = 0
            for index in range(100):
                frame = av.VideoFrame(96, 64, "yuv420p")
                for plane, value in zip(frame.planes, (40 + index, 100, 140)):
                    plane.update(bytes([value]) * plane.buffer_size)
                frame.pts = index + offset * 25
                frame.time_base = Fraction(1, 25)
                output.mux(stream.encode(frame))
        output.mux(stream.encode(None))
    return destination.getvalue()


def leaf(*segments, ended=True, extra=""):
    text = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
    text += "#EXT-X-MEDIA-SEQUENCE:0\n" + extra
    text += "".join(f"#EXTINF:4,\n{segment}\n" for segment in segments)
    if ended:
        text += "#EXT-X-ENDLIST\n"
    return text.encode()


class LocalSource:
    def __init__(self, routes, tls=None):
        self.routes = routes
        self.requests = []
        self.counts = Counter()
        self.stop = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                owner.requests.append((self.path, dict(self.headers)))
                owner.counts[self.path] += 1
                route = owner.routes.get(self.path, (404, {}, b""))
                if callable(route):
                    route = route(self, owner.counts[self.path])
                status, headers, body = route
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError, ConnectionResetError, ssl.SSLError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        if tls:
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}
        )
        self.thread.start()

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class TimeshiftSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video = transport_stream()
        cls.next_video = transport_stream(offset=4)
        cls.audio = transport_stream(audio=True)

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()

    def server(self, routes, tls=None):
        server = LocalSource(routes, tls=tls)
        self.servers.append(server)
        return server

    def test_direct_transport_stream_uses_one_request_and_decodes(self):
        server = self.server({"/account/password.ts": (200, {}, self.video)})
        with open_source(server.url + "/account/password.ts") as source:
            frames = list(source.decode(video=0))
        self.assertEqual(len(frames), 100)
        self.assertEqual(server.counts, {"/account/password.ts": 1})
        self.assertEqual(server.requests[0][1]["User-Agent"], "libmpv")
        self.assertNotIn("Range", server.requests[0][1])

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
                with open_source(server.url + path) as source:
                    self.assertEqual(sum(1 for _ in source.decode(video=0)), 100)
        self.assertEqual(server.counts["/video.ts"], 2)
        self.assertEqual(server.counts["/leaf.m3u8"], 1)

    def test_redirected_leaf_keeps_fragmented_init_and_byte_ranges(self):
        movie = transport_stream(
            container="mp4",
            options={"movflags": "frag_keyframe+empty_moov+default_base_moof"},
        )
        offset = 0
        while movie[offset + 4 : offset + 8] != b"moof":
            size = int.from_bytes(movie[offset : offset + 4], "big")
            self.assertGreater(size, 0)
            offset += size
            self.assertLess(offset, len(movie))
        manifest = (
            "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:4\n"
            f'#EXT-X-MAP:URI="movie.mp4",BYTERANGE="{offset}@0"\n'
            f"#EXTINF:4,\n#EXT-X-BYTERANGE:{len(movie) - offset}@{offset}\n"
            "movie.mp4\n#EXT-X-ENDLIST\n"
        ).encode()

        def ranged(request, _count):
            value = request.headers.get("Range")
            if value:
                start, end = value.removeprefix("bytes=").split("-")
                start = int(start)
                end = int(end) if end else len(movie) - 1
                return (
                    206,
                    {"Content-Range": f"bytes {start}-{end}/{len(movie)}"},
                    movie[start : end + 1],
                )
            return 200, {}, movie

        server = self.server(
            {
                "/master.m3u8": (
                    200,
                    {},
                    b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nleaf.m3u8\n",
                ),
                "/leaf.m3u8": (302, {"Location": "/nested/leaf.m3u8"}, b""),
                "/nested/leaf.m3u8": (200, {}, manifest),
                "/nested/movie.mp4": ranged,
            }
        )
        with open_source(server.url + "/master.m3u8") as source:
            self.assertEqual(sum(1 for _ in source.decode(video=0)), 100)
        ranges = [
            headers.get("Range") for path, headers in server.requests if path == "/nested/movie.mp4"
        ]
        self.assertIn(f"bytes=0-{offset - 1}", ranges)
        self.assertIn(f"bytes={offset}-{len(movie) - 1}", ranges)
        self.assertEqual(server.counts["/movie.mp4"], 0)

    def test_delayed_http_startup_exceeds_old_open_timeout(self):
        def delayed(_request, _count):
            time.sleep(5.2)
            return 200, {}, self.video

        server = self.server({"/slow.ts": delayed})
        with open_source(server.url + "/slow.ts") as source:
            self.assertEqual(sum(1 for _ in source.decode(video=0)), 100)
        self.assertEqual(server.counts["/slow.ts"], 1)

    def test_master_selects_compatible_quality_and_keeps_languages_after_redirect(self):
        master = b'''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="languages",NAME="English",LANGUAGE="eng",DEFAULT=YES,AUTOSELECT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="languages",NAME="French",LANGUAGE="fra",DEFAULT=NO,AUTOSELECT=YES,URI="audio/fr.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100000,CODECS="mp2v,mp4a.40.2",AUDIO="languages"
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=200000,CODECS="mp2v,mp4a.40.2",AUDIO="languages"
high.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=300000,CODECS="av01.0.04M.08,mp4a.40.2",AUDIO="languages"
unsupported.m3u8
'''
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
                if request.headers.get("User-Agent") != "libmpv":
                    return 403, {}, b""
                return 200, {}, body

            return respond

        routes["/media/high.m3u8"] = protected(leaf("video.ts"))
        routes["/media/video.ts"] = protected(self.video)
        for language in ("en", "fr"):
            routes[f"/media/audio/{language}.m3u8"] = protected(leaf(f"{language}.ts"))
            routes[f"/media/audio/{language}.ts"] = protected(self.audio)
        server = self.server(routes)
        with open_source(server.url + "/start") as source:
            self.assertEqual(len(source.streams.video), 1)
            self.assertEqual(len(source.streams.audio), 2)
            languages = {stream.metadata.get("language") for stream in source.streams.audio}
            self.assertEqual(languages, {"eng", "fra"})
            packets = Counter(packet.stream.type for packet in source.demux() if packet.size)
            self.assertGreater(packets["video"], 0)
            self.assertGreater(packets["audio"], 0)
        self.assertEqual(server.counts["/media/master.m3u8"], 1)
        self.assertEqual(server.counts["/media/low.m3u8"], 0)
        self.assertEqual(server.counts["/media/unsupported.m3u8"], 0)

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
        with open_source(server.url + "/entry") as source:
            frames = list(source.decode(video=0))
        self.assertEqual(len(frames), 200)
        self.assertGreaterEqual(server.counts["/live/channel.m3u8"], 2)
        self.assertEqual(server.counts["/entry"], 1)
        self.assertEqual(server.counts["/live/second.ts"], 1)

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
                with open_source(server.url + "/user/secret.ts"):
                    self.fail("untrusted TLS was accepted")
            self.assertNotIn("secret", str(failure.exception))
            self.assertNotIn(server.url, str(failure.exception))
            with open_source(server.url + "/user/secret.ts", verify_ssl=False) as source:
                self.assertEqual(sum(1 for _ in source.decode(video=0)), 100)

    def test_http_redirect_to_tls_obeys_verification_setting(self):
        with self.tls_context() as context:
            secure = self.server({"/video.ts": (200, {}, self.video)}, tls=context)
            server = self.server({"/redirect": (302, {"Location": secure.url + "/video.ts"}, b"")})
            with self.assertRaises(SourceError):
                with open_source(server.url + "/redirect"):
                    self.fail("redirect bypassed certificate verification")
            with open_source(server.url + "/redirect", verify_ssl=False) as source:
                self.assertEqual(sum(1 for _ in source.decode(video=0)), 100)

    def test_nested_hls_tls_is_verified_without_enabling_file_protocol(self):
        with self.tls_context() as context:
            secure = self.server({"/video.ts": (200, {}, self.video)}, tls=context)
            server = self.server({"/channel.m3u8": (200, {}, leaf(secure.url + "/video.ts"))})
            with self.assertRaises(SourceError):
                with open_source(server.url + "/channel.m3u8"):
                    self.fail("nested TLS bypassed certificate verification")
            with open_source(server.url + "/channel.m3u8", verify_ssl=False) as source:
                self.assertEqual(sum(1 for _ in source.decode(video=0)), 100)

    def test_remote_playlist_cannot_read_local_media(self):
        with tempfile.TemporaryDirectory(prefix="qitv-source-private-") as directory:
            private = Path(directory) / "private.ts"
            private.write_bytes(self.video)
            server = self.server({"/channel.m3u8": (200, {}, leaf(private.as_uri()))})
            with self.assertRaises(SourceError):
                with open_source(server.url + "/channel.m3u8"):
                    self.fail("remote HLS opened a local file")

    def test_encrypted_leaf_retains_relative_key_semantics(self):
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("openssl is required for the encrypted HLS fixture")
        key = bytes(range(16))
        encrypted = subprocess.run(
            [
                openssl,
                "enc",
                "-aes-128-cbc",
                "-K",
                key.hex(),
                "-iv",
                "0" * 32,
            ],
            input=self.video,
            capture_output=True,
            check=True,
        ).stdout
        playlist = leaf(
            "encrypted.ts",
            extra='#EXT-X-KEY:METHOD=AES-128,URI="keys/channel.key",IV=0x00000000000000000000000000000000\n',
        )
        server = self.server(
            {
                "/media/channel.m3u8": (200, {}, playlist),
                "/media/encrypted.ts": (200, {}, encrypted),
                "/media/keys/channel.key": (200, {}, key),
            }
        )
        with open_source(server.url + "/media/channel.m3u8") as source:
            self.assertEqual(sum(1 for _ in source.decode(video=0)), 100)
        self.assertEqual(server.counts["/media/keys/channel.key"], 1)

    def test_hls_bridge_is_private_and_closes_after_consumer_failure(self):
        server = self.server(
            {
                "/channel.m3u8": (200, {}, leaf("video.ts")),
                "/video.ts": (200, {}, self.video),
            }
        )
        failure = ValueError("mux failure")
        with self.assertRaises(ValueError) as raised:
            with open_source(server.url + "/channel.m3u8") as source:
                address = urlsplit(source.name)
                with requests.get(
                    f"http://{address.hostname}:{address.port}/wrong-token/resource",
                    timeout=2,
                ) as response:
                    self.assertEqual(response.status_code, 404)
                raise failure
        self.assertIs(raised.exception, failure)
        with self.assertRaises(OSError):
            with socket.create_connection((address.hostname, address.port), timeout=1):
                self.fail("source relay survived closing its context")

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
        with patch("services.timeshift_source.requests.Session", return_value=session):
            with patch.object(session, "get", side_effect=capture):
                with patch.object(session, "close", wraps=session.close) as close:
                    with self.assertRaises(ValueError) as raised:
                        with open_source(server.url + "/video.ts") as source:
                            self.assertEqual(next(source.decode(video=0)).width, 96)
                            raise failure
                    self.assertIs(raised.exception, failure)
                    close.assert_called_once()
        self.assertTrue(responses[0].raw.closed)

    def test_failed_open_closes_response_and_redacts_credentials(self):
        server = self.server({"/user/secret": (200, {}, b"not media at all")})
        session = requests.Session()
        responses = []
        get = session.get

        def capture(*args, **kwargs):
            response = get(*args, **kwargs)
            responses.append(response)
            return response

        with patch("services.timeshift_source.requests.Session", return_value=session):
            with patch.object(session, "get", side_effect=capture):
                with patch.object(session, "close", wraps=session.close) as close:
                    with self.assertRaises(SourceError) as raised:
                        with open_source(server.url + "/user/secret"):
                            self.fail("invalid media opened")
                    close.assert_called_once()
        self.assertTrue(responses[0].raw.closed)
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn(server.url, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
