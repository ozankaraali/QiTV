"""Raw MPEG-TS and HLS sources for the live pipeline.

Ported from main's tests/test_timeshift_source.py. Main decoded the media with PyAV; these
sources yield raw TS bytes, so every decode assertion became a byte assertion. Fixtures are
synthetic (tests/ts_fixtures.py) and every server is local.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import gzip
import logging
import ntpath
from pathlib import Path
import re
import shutil
import socket
import ssl
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import warnings

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad
from http_fixtures import LocalSource, leaf, tls_server_context
import m3u8
import requests
from test_fmp4 import (
    FIXTURES,
    SITE_INIT,
    box,
    elst,
    frames,
    full,
    movie,
    site_piece,
    sound,
    trak,
    visual,
)
import ts_fixtures
from urllib3.exceptions import InsecureRequestWarning

from services.live import interruptible_http, source as source_module
from services.live.fmp4 import Fmp4Remuxer
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
from services.live.stay_live import LagFilter
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


def prefetching(sequence, names, prefetch, *, target=1, ended=False, tag="#EXT-X-PREFETCH:"):
    """A live media playlist of finished pieces (`names`, ``target`` seconds each) and after
    them the pieces still being made (`prefetch`), as a live site lists them: one
    ``#EXT-X-PREFETCH:<uri>`` line each (``tag``: another site's name for it)."""
    text = f"#EXTM3U\n#EXT-X-TARGETDURATION:{target}\n#EXT-X-MEDIA-SEQUENCE:{sequence}\n"
    text += "".join(f"#EXTINF:{target},live\n{name}\n" for name in names)
    text += "".join(f"{tag}{name}\n" for name in prefetch)
    return (text + ("#EXT-X-ENDLIST\n" if ended else "")).encode()


DAY = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)  # when a dated playlist's piece 0 began


def stamp(seconds):
    """A date `seconds` after `DAY`, as a live site writes it."""
    return (DAY + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ad_range(start, duration, *, ident="stitched-ad-1700000000-30", kind="twitch-stitched-ad"):
    """A live site's ad break as its playlist marks it (Twitch's attributes): a date
    range of that class from `start` seconds after `DAY`, `duration` seconds long."""
    return (
        f'#EXT-X-DATERANGE:ID="{ident}",CLASS="{kind}",START-DATE="{stamp(start)}",'
        f'DURATION={duration:.3f},X-TV-TWITCH-AD-POD-FILLED-DURATION="{duration:g}"\n'
    )


def dated(sequence, names, *, ads=(), ranges=None, prefetch=(), titles=None, target=1):
    """A live site's media playlist: each piece `target` seconds and dated by its media
    sequence (piece k began k × `target` seconds after `DAY`); the pieces in `ads` are an ad
    break's, a discontinuity before its first and before the first after it. `ranges` puts
    lines (`ad_range`) before the piece of that name, None's after the finished ones;
    `titles`: a piece's EXTINF title ("live" else); `prefetch`: pieces still being made. It
    carries one of the site's own tags (the time it has been live)."""
    ranges, titles = ranges or {}, titles or {}
    text = f"#EXTM3U\n#EXT-X-TARGETDURATION:{target}\n#EXT-X-MEDIA-SEQUENCE:{sequence}\n"
    text += f"#EXT-X-TWITCH-ELAPSED-SECS:{sequence * target:.3f}\n"
    ad = False
    for k, name in enumerate(names):
        if (name in ads) != ad:
            text, ad = text + "#EXT-X-DISCONTINUITY\n", name in ads
        text += ranges.get(name, "")
        text += f"#EXT-X-PROGRAM-DATE-TIME:{stamp((sequence + k) * target)}\n"
        text += f"#EXTINF:{target:.3f},{titles.get(name, 'live')}\n{name}\n"
    text += ranges.get(None, "")
    for name in prefetch:
        if (name in ads) != ad:
            text, ad = text + "#EXT-X-DISCONTINUITY\n", name in ads
        text += f"#EXT-X-PREFETCH:{name}\n"
    return text.encode()


def site_playlist(
    sequence,
    finished,
    prefetch,
    *,
    length=2.0,
    dates=True,
    shift=0.0,
    tag="#EXT-X-TWITCH-PREFETCH:",
):
    """A live site's media playlist as Twitch writes it: `finished` pieces of `length` seconds
    from media sequence `sequence` under a 6 s target duration, each dated by its sequence
    (piece k began k × `length` after `DAY`, `shift` seconds later; undated without `dates`),
    then `prefetch`, the pieces still being made, on `tag` lines (Kick's: ``#EXT-X-PREFETCH:``,
    and nothing of Twitch's in it then)."""
    text = f"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:{sequence}\n"
    for k, name in enumerate(finished):
        if dates:
            text += f"#EXT-X-PROGRAM-DATE-TIME:{stamp((sequence + k) * length + shift)}\n"
        text += f"#EXTINF:{length:.3f},live\n{name}\n"
    text += "".join(f"{tag}{name}\n" for name in prefetch)
    return text.encode()


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


def drain_into(source, parts, *, reconnecting=False):
    """`drain`, into `parts` as the items come (what came before a failure stays there)."""
    try:
        for item in source.chunks():
            if isinstance(item, CaughtUp) or (isinstance(item, Reconnecting) and not reconnecting):
                continue
            if isinstance(item, bytes) and parts and isinstance(parts[-1], bytes):
                parts[-1] += item
            else:
                parts.append(item)
    finally:
        source.close()
    return parts


def ts_fixtures_has_audio(data):
    from services.live.ts import program_has_audio

    return program_has_audio(data) is True


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


LADDER = b"""#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080
hd.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720
mid.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
mid-low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=3840x2160,CODECS="av01.0.04M.08,mp4a.40.2"
unsupported.m3u8
"""


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

    def test_byte_range_hls_is_unsupported(self):
        # Main's FFmpeg played byte ranges; fragmented MP4 is remuxed (`FragmentedMp4Tests`),
        # but not from byte ranges of one file, a segment's or its init section's.
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
                # A master whose variants are fragmented MP4 in byte ranges.
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

    def test_a_live_playlists_newest_segments_are_checked_not_its_whole_window(self):
        # A DVR window lists hours of segments and is reloaded every few seconds: resolving
        # every URI on each reload took a third of a core on one public channel. A live
        # playlist's newest five minutes are checked before fetching; a finished one's all.
        base = "http://provider.test/live.m3u8"
        head = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nftp://elsewhere/old.ts\n"
        body = "".join(f"#EXTINF:10,\nseg{k}.ts\n" for k in range(31))
        source_module._check_media(m3u8.loads(head + body, uri=base), base)  # 310 s back
        for text in (head + body + "#EXT-X-ENDLIST\n", head + body.replace("seg30", "ftp://x/n")):
            with self.subTest(text[-24:]), self.assertRaises(SourceError) as raised:
                source_module._check_media(m3u8.loads(text, uri=base), base)
            self.assertEqual(str(raised.exception), source_module._NON_HTTP)

    def test_a_live_playlists_key_methods_are_looked_at_whole(self):
        # A start further back than the URIs checked must meet no other key either.
        base = "http://provider.test/live.m3u8"
        text = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n"
        text += (
            '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k"\n#EXTINF:10,\nold.ts\n#EXT-X-KEY:METHOD=NONE\n'
        )
        text += "".join(f"#EXTINF:10,\nseg{k}.ts\n" for k in range(31))
        with self.assertRaises(SourceError) as raised:
            source_module._check_media(m3u8.loads(text, uri=base), base)
        self.assertEqual(str(raised.exception), source_module._ENCRYPTION)

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

    def test_a_cap_plays_the_best_variant_within_it(self):  # 2.8: the live ⚙
        # Its sizes are the playable variants' (an unplayable 4K one is none), largest first;
        # under a cap the best within it plays, below every size the smallest. Only the
        # chosen variant's playlist is asked for.
        playlists = ("hd", "mid", "mid-low", "low", "unsupported")
        for cap, chosen in ((0, "hd"), (1080, "hd"), (720, "mid"), (480, "low"), (240, "low")):
            routes = {"/master.m3u8": (200, {}, LADDER), "/video.ts": (200, {}, self.video)}
            routes.update({f"/{name}.m3u8": (200, {}, leaf("video.ts")) for name in playlists})
            server = self.server(routes)
            with self.subTest(cap=cap):
                source = open_source(
                    server.url + "/master.m3u8",
                    headers={},
                    verify_ssl=True,
                    stop=threading.Event(),
                    max_height=cap,
                )
                try:
                    self.assertEqual(source.heights, (1080, 720, 360))
                    self.assertEqual(source.height, {"hd": 1080, "mid": 720, "low": 360}[chosen])
                    self.assertEqual(drain(source), [self.video])
                finally:
                    source.close()
                asked = [name for name in playlists if server.counts[f"/{name}.m3u8"]]
                self.assertEqual(asked, [chosen])

    def _picked(self, master, max_height=0):
        playlist, _heights = source_module._select_manifest(
            master.encode(), "http://x/m.m3u8", max_height
        )
        return playlist.playlists[0].uri

    def test_a_taller_variant_wins_over_more_bandwidth(self):
        master = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,FRAME-RATE=60,CODECS="avc1.640028"\nfull.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=2560x1440,FRAME-RATE=60,CODECS="hvc1.1.6.L150"\nwide.m3u8\n'
        )
        self.assertEqual(self._picked(master), "wide.m3u8")

    def test_same_height_prefers_the_better_codec(self):
        master = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,FRAME-RATE=60,CODECS="avc1.640028"\nh264.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,FRAME-RATE=60,CODECS="hvc1.1.6.L120"\nhevc.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=1920x1080,FRAME-RATE=30,CODECS="hvc1.1.6.L120"\nslow.m3u8\n'
        )
        self.assertEqual(self._picked(master), "hevc.m3u8")

    def test_a_rate_a_hair_lower_is_the_same_rate(self):
        # Task 3: 59.94 is 60's (to the whole frame), so the codec decides between them.
        master = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,FRAME-RATE=60.000,CODECS="avc1.640028"\nh264.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,FRAME-RATE=59.940,CODECS="hvc1.1.6.L120"\nhevc.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=1920x1080,FRAME-RATE=30.000,CODECS="hvc1.1.6.L120"\nslow.m3u8\n'
        )  # fmt: skip
        self.assertEqual(self._picked(master), "hevc.m3u8")

    def test_av1_only_when_carried(self):
        # The spike (.superpowers/docs/sites/av1-spike.md): VLC 3's TS demuxer cannot read AV1,
        # so an AV1 variant is passed over and the best H.264 one plays.
        master = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=2560x1440,FRAME-RATE=60,CODECS="av01.0.12M.08"\nav1.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,FRAME-RATE=60,CODECS="avc1.640028"\nh264.m3u8\n'
        )
        self.assertEqual(self._picked(master), "h264.m3u8")

    def test_the_live_cap_still_bounds_the_height(self):
        master = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=2560x1440,FRAME-RATE=60,CODECS="hvc1.1.6.L150"\nwide.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,FRAME-RATE=60,CODECS="avc1.640028"\nh264.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,FRAME-RATE=60,CODECS="hvc1.1.6.L120"\nhevc.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,FRAME-RATE=60,CODECS="avc1.64001f"\nmid.m3u8\n'
        )
        self.assertEqual(self._picked(master, 1080), "hevc.m3u8")
        self.assertEqual(self._picked(master, 720), "mid.m3u8")

    def test_a_site_master_with_live_subtitles_plays_its_best_variant_alone(self):
        # Spec E.5: a Kick-shaped master (its names made up) carries subtitles in
        # 7 languages: the chosen variant keeps its SUBTITLES group in its media list, and
        # nothing fetches them or another variant; its pieces play.
        languages = ("en", "tr", "de", "es", "fr", "pt", "ar")
        master = "#EXTM3U\n#EXT-X-INDEPENDENT-SEGMENTS\n"
        master += "".join(
            f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{lang}",LANGUAGE="{lang}",'
            f'AUTOSELECT=YES,DEFAULT=NO,FORCED=NO,URI="subs/{lang}.m3u8"\n'
            for lang in languages
        )
        variants = (  # (name, bandwidth, resolution, frame rate)
            ("chunked", 13000000, "1920x1080", 60.0),
            ("720p60", 6000000, "1280x720", 60.0),
            ("480p30", 2500000, "852x480", 30.0),
            ("160p30", 400000, "284x160", 30.0),
        )
        for name, bandwidth, resolution, rate in variants:
            master += (
                f'#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="{name}",NAME="{name}",AUTOSELECT=YES,'
                "DEFAULT=YES\n"
                f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={resolution},"
                f'CODECS="avc1.64002A,mp4a.40.2",VIDEO="{name}",FRAME-RATE={rate:.3f},'
                f'SUBTITLES="subs"\n{name}/index.m3u8\n'
            )
        routes = {"/master.m3u8": (200, {}, master.encode()), "/video.ts": (200, {}, self.video)}
        for name, *_ in variants:
            routes[f"/{name}/index.m3u8"] = (200, {}, leaf("../video.ts"))
        routes.update({f"/subs/{lang}.m3u8": (200, {}, leaf("cue.vtt")) for lang in languages})
        server = self.server(routes)
        playlist, heights = source_module._select_manifest(
            master.encode(), server.url + "/master.m3u8"
        )
        (selected,) = playlist.playlists
        self.assertEqual((selected.uri, heights), ("chunked/index.m3u8", (1080, 720, 480, 160)))
        kept = [(item.type, item.language) for item in playlist.media]
        self.assertEqual([lang for kind, lang in kept if kind == "SUBTITLES"], list(languages))
        self.assertEqual(media(server.url + "/master.m3u8"), [self.video])
        asked = {path for path, count in server.counts.items() if count}
        self.assertEqual(asked, {"/master.m3u8", "/chunked/index.m3u8", "/video.ts"})

    def test_height_ranks_first_only_when_every_variant_names_one(self):
        # Ruling R17: an IPTV master listing its source without RESOLUTION (6 Mbit/s) beside a
        # sized 720p (2 Mbit/s) plays the source, as before; named everywhere, height wins.
        sized = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1280x720\nwide.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080\ntall.m3u8\n"
        )
        self.assertEqual(self._picked(sized), "tall.m3u8")
        mixed = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=6000000\nsource.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,FRAME-RATE=60\nmid.m3u8\n"
        )
        self.assertEqual(self._picked(mixed), "source.m3u8")
        self.assertEqual(self._picked(mixed, 720), "mid.m3u8")  # a cap leaves the sized ones

    def test_a_master_naming_no_size_or_rate_keeps_the_bandwidth_order(self):
        master = (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nlow.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=300000\ntop.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=200000\nmid.m3u8\n"
        )
        self.assertEqual(self._picked(master), "top.m3u8")

    def test_a_cap_is_moot_without_sizes(self):
        # A master that names no RESOLUTION (many IPTV ones) plays its best as before; a
        # variant without one is left out under a cap while others name theirs; a media
        # playlist has no sizes.
        bare = (
            b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nlow.m3u8\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=200000\nhigh.m3u8\n"
        )
        mixed = (
            b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000\nunknown.m3u8\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=640x360\nlow.m3u8\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=1280x720\nhigh.m3u8\n"
        )
        cases = (
            ("/bare.m3u8", 360, "high", (), 0),
            ("/mixed.m3u8", 720, "high", (720, 360), 720),
            ("/mixed.m3u8", 0, "unknown", (720, 360), 0),  # one names none: bandwidth
            ("/high.m3u8", 360, "high", (), 0),
        )
        for path, cap, chosen, heights, height in cases:
            server = self.server(
                {
                    "/bare.m3u8": (200, {}, bare),
                    "/mixed.m3u8": (200, {}, mixed),
                    "/unknown.m3u8": (200, {}, leaf("video.ts")),
                    "/low.m3u8": (200, {}, leaf("video.ts")),
                    "/high.m3u8": (200, {}, leaf("video.ts")),
                    "/video.ts": (200, {}, self.video),
                }
            )
            with self.subTest(path=path, cap=cap):
                source = open_source(
                    server.url + path,
                    headers={},
                    verify_ssl=True,
                    stop=threading.Event(),
                    max_height=cap,
                )
                try:
                    self.assertEqual((source.heights, source.height), (heights, height))
                    drain(source)
                finally:
                    source.close()
                self.assertGreater(server.counts[f"/{chosen}.m3u8"], 0)
                others = {"unknown", "low", "high"} - {chosen, path[1:-5]}
                self.assertEqual([n for n in others if server.counts[f"/{n}.m3u8"]], [])

    def test_an_external_audio_group_is_woven_in_unless_the_variant_carries_audio(self):
        # Some multi-track restream servers name each track's playlist and still mux the first
        # audio into the video's: whether the variant carries audio of its own, its first
        # segment's program tables tell (2.1: public channels VLC played were refused for their
        # master alone), and the separate playlist is left out. A video-only variant has its
        # separate default audio woven in, segment by segment (2.2); a segment the audio
        # playlist does not list (after one reload) plays silent.
        master = b'''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="languages",NAME="English",LANGUAGE="eng",DEFAULT=YES,AUTOSELECT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="languages",NAME="French",LANGUAGE="fra",DEFAULT=NO,AUTOSELECT=YES,URI="audio/fr.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100000,CODECS="mp2v,mp4a.40.2",AUDIO="languages"
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=200000,CODECS="mp2v,mp4a.40.2",AUDIO="languages"
high.m3u8
'''
        video_only = ts_fixtures.make_stream(4, audio=False)
        audio = ts_fixtures.audio_only(4)
        for name, segment, own in (
            ("its own audio", self.video, True),
            ("video only", video_only, False),
        ):
            with self.subTest(name):
                server = self.server(
                    {
                        "/master.m3u8": (200, {}, master),
                        "/high.m3u8": (200, {}, leaf("video.ts", "next.ts")),
                        "/audio/en.m3u8": (200, {}, leaf("en.ts")),
                        "/audio/en.ts": (200, {}, audio),
                        "/video.ts": (200, {}, segment),
                        "/next.ts": (200, {}, segment),
                    }
                )
                source = open_source(
                    server.url + "/master.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
                )
                parts = drain(source)
                if own:
                    self.assertEqual(parts, joined(segment, segment))
                    self.assertEqual(server.counts["/audio/en.m3u8"], 0)  # never fetched
                else:
                    (out,) = parts
                    first = out[: len(segment) + len(audio) - 2 * 188]  # its PAT and PMT left out
                    self.assertTrue(ts_fixtures_has_audio(first))
                    self.assertEqual(len(out), len(first) + len(segment))  # the next one silent
                    self.assertEqual(server.counts["/audio/en.m3u8"], 1)  # finished: no reload
                    self.assertEqual(server.counts["/audio/en.ts"], 1)
                self.assertEqual(server.counts["/next.ts"], 1)
                self.assertEqual(server.counts["/master.m3u8"], 1)
                self.assertEqual(server.counts["/high.m3u8"], 1)
                self.assertEqual(server.counts["/video.ts"], 1)

    def test_an_audio_playlist_a_little_behind_the_video_is_reloaded_for_it(self):
        # At the live edge the audio packager may list a segment after the video's: its
        # playlist is reloaded at once (what of the video it does not cover yet is held for
        # it meanwhile: `test_video_whose_audio_is_not_listed_yet_goes_on_as_far_as_its_audio`).
        master = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100000,AUDIO="a"
video.m3u8
"""

        def audio_playlist(_request, count):
            return 200, {}, leaf("a0.ts", *(["a1.ts"] if count > 1 else []), ended=False)

        video = ts_fixtures.make_stream(4, audio=False)
        later = ts_fixtures.make_stream(4, audio=False, pts_start=900000 + 4 * 90000)
        server = self.server(
            {
                "/master.m3u8": (200, {}, master),
                "/video.m3u8": (200, {}, leaf("v0.ts", "v1.ts")),
                "/audio.m3u8": audio_playlist,
                "/v0.ts": (200, {}, video),
                "/v1.ts": (200, {}, later),
                "/a0.ts": (200, {}, ts_fixtures.audio_only(4)),
                "/a1.ts": (200, {}, ts_fixtures.audio_only(4, pts_start=900000 + 4 * 90000)),
            }
        )
        (out,) = media(server.url + "/master.m3u8")
        self.assertEqual(server.counts["/audio.m3u8"], 2)  # once more for a1.ts, then listed
        self.assertEqual((server.counts["/a0.ts"], server.counts["/a1.ts"]), (1, 1))
        self.assertEqual(len(out), 2 * (len(video) + len(ts_fixtures.audio_only(4)) - 2 * 188))

    def held_tail_routes(self, video_playlist):
        """A master whose video (4 s from 900000) has audio for its first 2 s at first, and
        for the rest from its second load on (at the video playlist's reload: it is not
        reloaded again sooner than a second, half its target duration)."""
        master = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100000,AUDIO="a"
video.m3u8
"""

        def audio_playlist(_request, count):
            names = ["a0.ts", *(["a1.ts"] if count > 1 else [])]
            return 200, {}, hls(0, names, target=2, ended=count > 1)

        return {
            "/master.m3u8": (200, {}, master),
            "/video.m3u8": video_playlist,
            "/audio.m3u8": audio_playlist,
            "/v0.ts": (200, {}, ts_fixtures.make_stream(4, audio=False)),
            "/v1.ts": (200, {}, ts_fixtures.make_stream(4, audio=False, pts_start=90000)),
            "/a0.ts": (200, {}, ts_fixtures.audio_only(2)),
            "/a1.ts": (200, {}, ts_fixtures.audio_only(2, pts_start=900000 + 2 * 90000)),
        }

    def test_video_whose_audio_is_not_listed_yet_goes_on_as_far_as_its_audio(self):
        # Renditions cut apart publish their segments apart: the video segment goes on as far
        # as the audio covers it, and its tail once the audio for it is listed (2.3: waiting
        # for the audio whole held 10 s segments back 5-8 s, and VLC ran dry between them).
        def video_playlist(_request, count):
            return 200, {}, hls(0, ["v0.ts"], target=4, ended=count > 1)

        server = self.server(self.held_tail_routes(video_playlist))
        head, caught_up, tail = media(server.url + "/master.m3u8", caught_up=True)
        self.assertIsInstance(caught_up, CaughtUp)
        video = ts_fixtures.make_stream(4, audio=False)
        whole = head + tail
        packets = [whole[at : at + 188] for at in range(0, len(whole), 188)]
        own = [video[at : at + 188] for at in range(0, len(video), 188)]
        pmt = ts_fixtures.PMT_PID
        pid = lambda p: ((p[1] & 0x1F) << 8) | p[2]  # noqa: E731
        self.assertEqual(
            [p[4:] for p in packets if pid(p) not in (0x101, pmt)],
            [p[4:] for p in own if pid(p) != pmt],
        )  # every video packet once, in order (the counters run on)
        self.assertLess(len(head), len(video))  # the tail waited for its audio
        times = [frame.pts for frame in frames(whole)[0x101]]
        audio = ts_fixtures.audio_only(2) + ts_fixtures.audio_only(2, pts_start=900000 + 180000)
        self.assertEqual(times, [frame.pts for frame in frames(audio)[0x101]])
        self.assertEqual(server.counts["/audio.m3u8"], 2)  # read, then at the reload

    def test_a_burst_of_video_does_not_reload_the_audio_playlist_for_each_segment(self):
        # Each video segment of a burst reloaded the audio playlist that did not
        # list its audio yet (up to 2 MB a second of a DVR window); at most every half target
        # duration now, and once more at a finished playlist's end.
        names = ["v0.ts", "v1.ts", "v2.ts", "v3.ts"]
        routes = self.held_tail_routes(
            lambda _request, _count: (200, {}, hls(0, names, target=4, ended=True))
        )
        routes["/audio.m3u8"] = (200, {}, hls(0, ["a0.ts"], target=2, ended=False))
        for k in range(1, 4):
            later = ts_fixtures.make_stream(4, audio=False, pts_start=900000 + k * 4 * 90000)
            routes[f"/v{k}.ts"] = (200, {}, later)
        server = self.server(routes)
        media(server.url + "/master.m3u8")
        self.assertEqual(server.counts["/audio.m3u8"], 2)

    def test_the_audio_list_keeps_the_newest_segments_of_a_dvr_window(self):
        routes = self.held_tail_routes(
            lambda _request, _count: (200, {}, hls(0, ["v0.ts"], target=4, ended=True))
        )
        window = [f"a{k}.ts" for k in range(400)]
        routes["/audio.m3u8"] = (200, {}, hls(100, window, target=1, ended=False))
        server = self.server(routes)
        source = open_source(
            server.url + "/master.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
        )
        drain(source)
        listed = sorted(source._audio_list)
        self.assertEqual((len(listed), listed[-1]), (300, 499))

    def test_after_a_break_the_audio_playlist_is_read_again_before_its_audio_is_found(self):
        # After a break the audio was looked for in the list read before it.
        def video_playlist(_request, _count):
            text = hls(0, ["v0.ts"], target=4).decode()
            text += "#EXT-X-DISCONTINUITY\n#EXTINF:4,\nv1.ts\n#EXT-X-ENDLIST\n"
            return 200, {}, text.encode()

        def audio_playlist(_request, count):
            text = hls(0, ["a0.ts"], target=4).decode()
            if count > 1:
                text += "#EXT-X-DISCONTINUITY\n#EXTINF:4,\nb0.ts\n"
            return 200, {}, text.encode()

        routes = self.held_tail_routes(video_playlist)
        routes["/audio.m3u8"] = audio_playlist
        routes["/a0.ts"] = (200, {}, ts_fixtures.audio_only(4))
        routes["/b0.ts"] = (200, {}, ts_fixtures.audio_only(4, pts_start=90000))
        server = self.server(routes)
        _first, _marker, second = media(server.url + "/master.m3u8")
        times = [frame.pts for frame in frames(second)[0x101]]
        self.assertEqual(times[0], 90000)  # the new timeline's audio
        self.assertEqual(server.counts["/b0.ts"], 1)

    def test_a_tail_still_held_at_a_break_goes_on_before_its_marker(self):
        def video_playlist(_request, count):
            text = hls(0, ["v0.ts"], target=4).decode()
            text += "#EXT-X-DISCONTINUITY\n#EXTINF:4,\nv1.ts\n#EXT-X-ENDLIST\n"
            return 200, {}, text.encode()

        routes = self.held_tail_routes(video_playlist)
        routes["/audio.m3u8"] = (200, {}, hls(0, ["a0.ts"], target=2, ended=False))
        server = self.server(routes)
        first, marker, second = media(server.url + "/master.m3u8")
        self.assertIsInstance(marker, Discontinuity)
        video = ts_fixtures.make_stream(4, audio=False)
        self.assertGreaterEqual(len(first), len(video))  # all of v0, its tail silent

    @staticmethod
    def full_pmt_video(pts_start=900000):
        """4 s of video only whose PMT fills its packet (a private descriptor of 160 bytes):
        no room for one more stream."""
        info = bytes([0xF0, 160]) + b"\x00" * 160
        video = (0xE000 | ts_fixtures.VIDEO_PID).to_bytes(2, "big")
        body = video + (0xF000 | len(info)).to_bytes(2, "big") + info
        body += bytes([0x1B]) + video + b"\xf0\x00"
        head = bytes([0x02]) + (0xB000 | (5 + len(body) + 4)).to_bytes(2, "big")
        section = head + (1).to_bytes(2, "big") + bytes([0xC1, 0, 0]) + body
        data = ts_fixtures.make_stream(4, audio=False, pts_start=pts_start)
        out = []
        for at in range(0, len(data), 188):
            packet = data[at : at + 188]
            if ((packet[1] & 0x1F) << 8 | packet[2]) == ts_fixtures.PMT_PID:
                packet = ts_fixtures._psi_packet(ts_fixtures.PMT_PID, section, packet[3] & 0x0F)
            out.append(packet)
        return b"".join(out)

    def test_audio_its_pmt_cannot_take_is_not_fetched_again_for_each_segment(self):
        # The video's PMT has no room for the separate audio: the video plays as it came, and
        # its audio is not fetched (and thrown away) for every segment of the session.
        master = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100000,AUDIO="a"
video.m3u8
"""
        videos = [self.full_pmt_video(900000 + k * 4 * 90000) for k in range(4)]
        routes = {
            "/master.m3u8": (200, {}, master),
            "/video.m3u8": (200, {}, hls(0, [f"v{k}.ts" for k in range(4)], target=4, ended=True)),
            "/audio.m3u8": (200, {}, hls(0, [f"a{k}.ts" for k in range(4)], target=4, ended=True)),
        }
        for k in range(4):
            routes[f"/v{k}.ts"] = (200, {}, videos[k])
            audio = ts_fixtures.audio_only(4, pts_start=900000 + k * 4 * 90000)
            routes[f"/a{k}.ts"] = (200, {}, audio)
        server = self.server(routes)
        with self.assertLogs("services.live.hls_audio", "INFO") as logs:
            parts = media(server.url + "/master.m3u8")
        self.assertEqual(parts, joined(*videos))  # as they came
        self.assertEqual(sum(server.counts[f"/a{k}.ts"] for k in range(4)), 1)
        self.assertEqual(sum("cannot take its audio" in line for line in logs.output), 1)

    def test_a_tail_held_for_audio_its_pmt_cannot_take_goes_on_as_it_came(self):
        # The audio covered the video's first 2 s: its head, PMT and all, could not take it,
        # and the tail held for the rest goes on as it came, no audio fetched for it.
        def video_playlist(_request, count):
            return 200, {}, hls(0, ["v0.ts"], target=4, ended=count > 1)

        routes = self.held_tail_routes(video_playlist)
        video = self.full_pmt_video()
        routes["/v0.ts"] = (200, {}, video)
        server = self.server(routes)
        self.assertEqual(media(server.url + "/master.m3u8"), joined(video))
        self.assertEqual((server.counts["/a0.ts"], server.counts["/a1.ts"]), (1, 0))

    def test_a_break_tries_the_tables_again_without_a_picture_time_too(self):
        # A PMT without room refused the audio; after a break, a first segment without a
        # picture's time (its audio taken by its number) has tables that take it.
        master = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100000,AUDIO="a"
video.m3u8
"""
        text = hls(0, ["v0.ts"], target=4).decode()
        text += "#EXT-X-DISCONTINUITY\n#EXTINF:4,\nv1.ts\n#EXT-X-ENDLIST\n"
        tables = ts_fixtures.pat() + ts_fixtures.pmt(audio_pids=())  # no picture: no time
        server = self.server(
            {
                "/master.m3u8": (200, {}, master),
                "/video.m3u8": (200, {}, text.encode()),
                "/audio.m3u8": (200, {}, hls(0, ["a0.ts", "a1.ts"], target=4, ended=True)),
                "/v0.ts": (200, {}, self.full_pmt_video()),
                "/v1.ts": (200, {}, tables),
                "/a0.ts": (200, {}, ts_fixtures.audio_only(4)),
                "/a1.ts": (200, {}, ts_fixtures.audio_only(4, pts_start=90000)),
            }
        )
        first, marker, second = media(server.url + "/master.m3u8")
        self.assertIsInstance(marker, Discontinuity)
        self.assertFalse(ts_fixtures_has_audio(first))  # as it came
        self.assertTrue(ts_fixtures_has_audio(second))  # the new tables took it
        self.assertEqual(server.counts["/a1.ts"], 1)

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

    def test_a_low_latency_playlists_unfinished_segment_waits_until_it_is_listed_whole(self):
        # LL-HLS lists the segment still being made as its parts alone: no URI of its own.
        # Taken for a segment, the playlist itself was fetched in its place, a break marked
        # and that sequence number passed over: every reload left a segment out.
        head = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-PART-INF:PART-TARGET=1.0\n"
        head += "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:2,\nfirst.ts\n"
        parts = '#EXT-X-PART:DURATION=1.0,INDEPENDENT=YES,URI="{0}-0.ts"\n'
        parts += '#EXT-X-PART:DURATION=1.0,URI="{0}-1.ts"\n'

        def playlist(_request, count):
            if count == 1:
                return 200, {}, (head + parts.format("second")).encode()
            text = head + parts.format("second") + "#EXTINF:2,\nsecond.ts\n#EXT-X-ENDLIST\n"
            return 200, {}, text.encode()

        server = self.server(
            {
                "/live.m3u8": playlist,
                "/first.ts": (200, {}, self.video),
                "/second.ts": (200, {}, self.next_video),
            }
        )
        self.assertEqual(media(server.url + "/live.m3u8"), joined(self.video, self.next_video))
        self.assertEqual(server.counts["/live.m3u8"], 2)
        self.assertEqual((server.counts["/first.ts"], server.counts["/second.ts"]), (1, 1))

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

    def test_a_live_playlist_down_beyond_the_quick_delays_is_asked_again(self):
        quick = len(FAST_RETRIES)

        def playlist(_request, count):
            text = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1,\na.ts\n"
            if count == 1:
                return 200, {}, text.encode()
            if count <= 2 + quick + 1:  # the reload, its quick tries and one long one
                return 503, {}, b""
            return 200, {}, (text + "#EXTINF:1,\nb.ts\n#EXT-X-ENDLIST\n").encode()

        server = self.server(
            {
                "/live.m3u8": playlist,
                "/a.ts": (200, {}, self.video),
                "/b.ts": (200, {}, self.next_video),
            }
        )
        source = open_source(
            server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
        )
        heard = []
        source.listen_retries(heard.append)
        fast = {"_RECONNECT_DELAYS": FAST_RETRIES, "_ENDLESS_DELAYS": (0.02,)}
        with patch.multiple(source_module, **fast):
            parts = drain(source)
        self.assertEqual(parts, joined(self.video, self.next_video))
        self.assertEqual(server.counts["/live.m3u8"], 2 + quick + 2)
        self.assertEqual(len(heard), 3)  # two long waits told, then None
        self.assertIsNone(heard[-1])

    def test_a_reload_answered_404_a_moment_is_asked_again(self):
        # 2.9: a public channel's CDN answered one 404 for its playlist: the session ended,
        # played the channel directly, and AI captions had no buffer to read.
        def playlist(_request, count):
            text = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1,\na.ts\n"
            if count == 1:
                return 200, {}, text.encode()
            if count in (2, 3):
                return 404, {}, b""
            return 200, {}, (text + "#EXTINF:1,\nb.ts\n#EXT-X-ENDLIST\n").encode()

        server = self.server(
            {
                "/live.m3u8": playlist,
                "/a.ts": (200, {}, self.video),
                "/b.ts": (200, {}, self.next_video),
            }
        )
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            parts = media(server.url + "/live.m3u8")
        self.assertEqual(parts, joined(self.video, self.next_video))
        self.assertEqual(server.counts["/live.m3u8"], 4)

    def test_a_variant_gone_for_good_is_found_again_through_the_channels_master(self):
        # A variant's address that expired: its master names it anew.
        def master(_request, count):
            variant = "v1.m3u8" if count == 1 else "v2.m3u8"
            return 200, {}, f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n{variant}\n".encode()

        def first(_request, count):
            if count == 1:
                text = (
                    "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1,\na.ts\n"
                )
                return 200, {}, text.encode()
            return 404, {}, b""

        second = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n#EXT-X-ENDLIST\n"
        )
        server = self.server(
            {
                "/channel.m3u8": master,
                "/v1.m3u8": first,
                "/v2.m3u8": (200, {}, second.encode()),
                "/a.ts": (200, {}, self.video),
                "/b.ts": (200, {}, self.next_video),
            }
        )
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            parts = media(server.url + "/channel.m3u8")
        self.assertEqual(parts, joined(self.video, self.next_video))
        self.assertEqual(server.counts["/channel.m3u8"], 2)
        self.assertEqual(server.counts["/v1.m3u8"], 1 + 1 + len(FAST_RETRIES))

    def test_a_playlist_gone_without_a_master_still_fails(self):
        def playlist(_request, count):
            text = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1,\na.ts\n"
            return (200, {}, text.encode()) if count == 1 else (404, {}, b"")

        server = self.server({"/live.m3u8": playlist, "/a.ts": (200, {}, self.video)})
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            with self.assertRaises(SourceError) as failure:
                media(server.url + "/live.m3u8")
        self.assertEqual((failure.exception.kind, failure.exception.status), ("http", 404))

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

    def test_https_first_opens_a_stream_over_https(self):
        # 2.7.1: "Prefer HTTPS": the provider's http:// address is asked over HTTPS first.
        source_module._HTTPS_FAILED.clear()
        with self.tls_context() as context:
            server = self.server({"/user/secret.ts": (200, {}, self.video)}, tls=context)
            plain = server.url.replace("https://", "http://", 1) + "/user/secret.ts"
            with insecure():
                source = open_source(
                    plain, headers={}, verify_ssl=False, stop=threading.Event(), https_first=True
                )
                self.assertTrue(source.secure)  # the helper's state says so (`secure`)
                parts = drain(source)
            self.assertEqual(parts, [self.video])
            self.assertEqual(server.counts["/user/secret.ts"], 1)

    def test_https_first_falls_back_to_http_and_remembers_the_host(self):
        # A provider whose streams have no HTTPS: HTTP after the failed attempt, and for the
        # rest of the run that host opens over HTTP at once.
        source_module._HTTPS_FAILED.clear()
        server = self.server({"/user/secret.ts": (200, {}, self.video)})
        with source_logs() as logs:
            parts = media(server.url + "/user/secret.ts", https_first=True)
        self.assertEqual(parts, [self.video])
        self.assertTrue(any("HTTPS" in line for line in logs))
        self.assertFalse(any("secret" in line or "127.0.0.1" in line for line in logs))
        connections = server.connections
        source = open_source(
            server.url + "/user/secret.ts",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            https_first=True,
        )
        self.assertFalse(source.secure)
        self.assertEqual(drain(source), [self.video])
        self.assertEqual(server.connections, connections + 1)  # no second HTTPS attempt

    def test_https_first_opens_an_https_address_that_fails_over_http(self):
        # An address QiTV chose HTTPS for (an Xtream panel's that answered there): when its
        # stream does not open over HTTPS, it opens over HTTP (the user can watch).
        source_module._HTTPS_FAILED.clear()
        server = self.server({"/user/secret.ts": (200, {}, self.video)})  # HTTP alone
        secure = server.url.replace("http://", "https://", 1) + "/user/secret.ts"
        with self.assertRaises(SourceError):
            media(secure)  # as given, without the flag: it fails
        with source_logs() as logs:
            source = open_source(
                secure, headers={}, verify_ssl=True, stop=threading.Event(), https_first=True
            )
            self.assertFalse(source.secure)
            self.assertEqual(drain(source), [self.video])
        self.assertTrue(any("over HTTP" in line for line in logs))
        self.assertFalse(any("secret" in line or "127.0.0.1" in line for line in logs))

    def test_https_first_keeps_a_limit_refusal_over_https(self):
        # 2.8.12: the channel's server refused over a working HTTPS connection (its limit):
        # the refusal stands (asked over HTTP it adds a request and loses the refusal), and
        # the host keeps HTTPS for its next channels.
        source_module._HTTPS_FAILED.clear()
        with self.tls_context() as context:
            server = self.server({"/user/secret.ts": (458, {}, b"")}, tls=context)
            plain = server.url.replace("https://", "http://", 1) + "/user/secret.ts"
            with insecure(), self.assertRaises(SourceError) as failure:
                open_source(
                    plain, headers={}, verify_ssl=False, stop=threading.Event(), https_first=True
                )
            self.assertEqual(str(failure.exception), STREAM_LIMIT)
            self.assertEqual(source_module._HTTPS_FAILED, set())
            self.assertEqual(server.counts["/user/secret.ts"], 1)

    def test_https_first_keeps_a_plain_refusal_of_an_https_address(self):  # 2.9.7
        # The provider's own HTTPS address refused the stream (407: its one stream, the
        # closed one still counted): asked over HTTP it would add a request and lose the why.
        source_module._HTTPS_FAILED.clear()
        with self.tls_context() as context:
            server = self.server({"/user/secret.ts": (407, {}, b"")}, tls=context)
            with insecure(), self.assertRaises(SourceError) as failure:
                open_source(
                    server.url + "/user/secret.ts",
                    headers={},
                    verify_ssl=False,
                    stop=threading.Event(),
                    https_first=True,
                )
            self.assertEqual(str(failure.exception), STREAM_REFUSED)
            self.assertEqual(failure.exception.status, 407)
            self.assertEqual(server.counts["/user/secret.ts"], 1)

    def test_an_https_address_whose_http_has_nothing_says_what_https_answered(self):  # 2.9.7
        # Over HTTP the host had nothing at that address (another server at port 80): the
        # HTTPS server's answer is why the channel did not open, not that 404.
        def opened(url, **_kwargs):
            if url.startswith("https://"):
                raise SourceError("http", status=451)
            raise SourceError("http", status=404)

        with patch.object(source_module, "_open_url", opened), source_logs() as logs:
            with self.assertRaises(SourceError) as failure:
                open_source(
                    "https://panel.test/live/u/p/7.ts",
                    headers={},
                    verify_ssl=True,
                    stop=threading.Event(),
                    https_first=True,
                )
        self.assertEqual(failure.exception.status, 451)
        self.assertTrue(any("HTTP 451" in line for line in logs))  # the log says which
        self.assertFalse(any("panel.test" in line for line in logs))

    def test_https_first_keeps_https_after_a_plain_403_over_it(self):
        # A plain 403 over HTTPS may be the channel's own refusal: this open tries HTTP, but
        # the host keeps HTTPS; another server's 404 at its port marks it (as before).
        for status, marked in ((403, False), (404, True)):
            with self.subTest(status=status):
                source_module._HTTPS_FAILED.clear()
                with self.tls_context() as context:
                    server = self.server({"/user/secret.ts": (status, {}, b"")}, tls=context)
                    plain = server.url.replace("https://", "http://", 1) + "/user/secret.ts"
                    with insecure(), self.assertRaises(SourceError):
                        open_source(
                            plain,
                            headers={},
                            verify_ssl=False,
                            stop=threading.Event(),
                            https_first=True,
                        )
                    self.assertEqual(bool(source_module._HTTPS_FAILED), marked)

    def test_recordings_answered_again_and_again_are_asked_for_the_whole_patience(self):
        # 2.8.12: each answer that was a recording (`not_live`) took a step of the reconnect
        # schedule: after six the source failed "network" in about 2 minutes, not the 7 it
        # promised, and the session played the provider directly: the recording.
        clip = ts_fixtures.make_stream(2)

        def answer(_request, _count):
            def body(request):
                request.wfile.write(clip)

            return 200, {}, body  # close-delimited, as a live channel

        server = self.server({"/live.ts": answer})
        with patch.multiple(
            source_module,
            _RECONNECT_DELAYS=FAST_RETRIES,
            _REFUSED_RETRY_S=0.02,
            _REFUSED_PATIENCE_S=3.0,  # room for more asks than the schedule on a busy CI Mac
        ):
            source = open_source(
                server.url + "/live.ts", headers={}, verify_ssl=True, stop=threading.Event()
            )
            fresh = True
            with self.assertRaises(SourceError) as failure:
                try:
                    for item in source.chunks():
                        if isinstance(item, Discontinuity):
                            fresh = True
                        elif isinstance(item, bytes) and fresh:
                            fresh = False
                            source.not_live()  # the guard: a recording, not the channel
                finally:
                    source.close()
        self.assertEqual(str(failure.exception), STREAM_LIMIT)  # not "network"
        self.assertGreater(server.counts["/live.ts"], len(FAST_RETRIES) + 1)

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
            extra=(
                '#EXT-X-KEY:METHOD=AES-128,URI="keys/channel.key",'
                "IV=0x00000000000000000000000000000000\n"
            ),
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
                if count > 2:
                    return 404, {}, b""  # what ends a live channel's input here (2.8.12)
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
                    source = open_source(
                        server.url + "/live.ts",
                        headers={},
                        verify_ssl=not tls,
                        stop=threading.Event(),
                    )
                    parts = []
                    if keep_alive:  # its first answer declared a length: a finite body
                        drain_into(source, parts, reconnecting=True)
                    else:  # a live channel's: the reconnect's sized end is no end (2.8.12)
                        with self.assertRaises(SourceError):
                            drain_into(source, parts, reconnecting=True)
                # The helper hears that a new connection opens before it waits for one.
                self.assertEqual(parts[:4], [head, Reconnecting(), Discontinuity(), rest])
                self.assertEqual(server.counts["/live.ts"], 2 if keep_alive else 3)
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
        # 2.1: the GUI waits longer only while a raw-TS answer stands. One whose
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
        # 2.1: a panel that counted the first answer may refuse the next with its
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

    def broken_then(self, later):
        """A raw TS channel whose first connection breaks half way; `later(count)` answers
        each request after it (count 2 on)."""
        half = len(self.video) // 2 // 188 * 188

        def live(_request, count):
            if count == 1:

                def broken(request):
                    request.wfile.write(self.video[:half])

                return 200, {"Content-Length": str(len(self.video))}, broken
            return later(count, half)

        return self.server({"/user/secret/live.ts": live})

    def test_raw_ts_reconnects_go_on_after_the_quick_tries(self):
        # The owner's channel (2026-11-09): its connections closed for 40 s, the source gave
        # up after six tries, and the picture stayed dead for 74 minutes until the channel was
        # changed. A live channel is never given up on while it is watched: after the quick
        # delays come the long ones, each told to the listener (the helper's state).
        quick = len(FAST_RETRIES)
        server = self.broken_then(
            lambda count, half: (
                (503, {}, b"") if count <= 1 + quick + 2 else (200, {}, self.video[half:])
            )
        )
        source = open_source(
            server.url + "/user/secret/live.ts", headers={}, verify_ssl=True, stop=threading.Event()
        )
        heard = []
        source.listen_retries(heard.append)
        fast = {"_RECONNECT_DELAYS": FAST_RETRIES, "_ENDLESS_DELAYS": (0.02, 0.04)}
        with patch.multiple(source_module, **fast):
            received = list(source.chunks())
        media_bytes = b"".join(item for item in received if isinstance(item, bytes))
        self.assertEqual(media_bytes, self.video)
        self.assertIn(Reconnecting(), received)
        self.assertEqual(server.counts["/user/secret/live.ts"], 1 + quick + 3)
        # Before each long wait, when the next try is due (a time of day); then None.
        self.assertEqual(len(heard), 4)
        self.assertTrue(all(isinstance(at, float) for at in heard[:3]))
        self.assertLessEqual(heard[0], heard[1])
        self.assertIsNone(heard[-1])

    def test_the_long_waits_grow_to_the_last_and_stay_there(self):
        backoff = source_module._Backoff(endless=True)
        quick = [backoff.next_delay() for _ in source_module._RECONNECT_DELAYS]
        self.assertEqual(quick, list(source_module._RECONNECT_DELAYS))
        self.assertFalse(backoff.long())
        later = [backoff.next_delay() for _ in range(7)]
        self.assertTrue(backoff.long())
        self.assertEqual(later, [10.0, 30.0, 60.0, 120.0, 300.0, 300.0, 300.0])
        once = source_module._Backoff()  # a first answer's, a segment's: it runs out
        for _ in source_module._RECONNECT_DELAYS:
            once.next_delay()
        self.assertIsNone(once.next_delay())

    def test_retry_now_ends_a_long_wait(self):
        quick = len(FAST_RETRIES)
        server = self.broken_then(
            lambda count, half: (
                (503, {}, b"") if count <= 1 + quick else (200, {}, self.video[half:])
            )
        )
        source = open_source(
            server.url + "/user/secret/live.ts", headers={}, verify_ssl=True, stop=threading.Event()
        )

        def heard(at):
            if at is not None:  # the user's Try now, from the GUI's side
                threading.Thread(target=source.retry_now, daemon=True).start()

        source.listen_retries(heard)
        started = time.monotonic()
        fast = {"_RECONNECT_DELAYS": FAST_RETRIES, "_ENDLESS_DELAYS": (60.0,)}
        with patch.multiple(source_module, **fast):
            received = list(source.chunks())
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(b"".join(i for i in received if isinstance(i, bytes)), self.video)

    def test_a_close_ends_a_long_wait_quietly(self):
        server = self.broken_then(lambda count, half: (503, {}, b""))
        source = open_source(
            server.url + "/user/secret/live.ts", headers={}, verify_ssl=True, stop=threading.Event()
        )
        source.listen_retries(
            lambda at: at is not None and threading.Timer(0.05, source.close).start()
        )
        started = time.monotonic()
        fast = {"_RECONNECT_DELAYS": FAST_RETRIES, "_ENDLESS_DELAYS": (60.0,)}
        with patch.multiple(source_module, **fast):
            received = list(source.chunks())  # no error: a channel change, say
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertIn(Reconnecting(), received)

    def test_an_answer_that_means_it_still_ends_the_reconnects(self):
        # A 404 is no outage: the session ends, and the channel plays directly.
        server = self.broken_then(lambda count, half: (404, {}, b""))
        source = open_source(
            server.url + "/user/secret/live.ts", headers={}, verify_ssl=True, stop=threading.Event()
        )
        heard = []
        source.listen_retries(heard.append)
        with patch.multiple(source_module, _RECONNECT_DELAYS=FAST_RETRIES):
            with self.assertRaises(SourceError) as raised:
                list(source.chunks())
        self.assertEqual((raised.exception.kind, raised.exception.status), ("http", 404))
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(server.counts["/user/secret/live.ts"], 2)
        self.assertEqual(heard, [])

    def test_a_refused_reconnect_is_asked_again_patiently_until_the_slot_frees(self):
        # 2.8.5: a provider that allows one stream counts the connection that broke for
        # minutes: 407 for a while (as seen), then the stream again. The source waits it out.
        half = len(self.video) // 2 // 188 * 188

        def live(_request, count):
            if count == 1:

                def broken(request):
                    request.wfile.write(self.video[:half])

                return 200, {"Content-Length": str(len(self.video))}, broken
            if count <= 4:
                return 407, {}, b""
            return 200, {}, self.video[half:]

        server = self.server({"/user/secret/live.ts": live})
        source = open_source(
            server.url + "/user/secret/live.ts", headers={}, verify_ssl=True, stop=threading.Event()
        )
        patient = {"_REFUSED_RETRY_S": 0.05, "_REFUSED_PATIENCE_S": 5.0}
        with patch.multiple(source_module, _RECONNECT_DELAYS=FAST_RETRIES, **patient):
            received = list(source.chunks())
        media_bytes = b"".join(item for item in received if isinstance(item, bytes))
        self.assertEqual(media_bytes, self.video)
        self.assertIn(Reconnecting(), received)
        self.assertEqual(server.counts["/user/secret/live.ts"], 5)

    def test_a_reconnect_refused_beyond_the_patience_ends_with_the_refusal(self):
        half = len(self.video) // 2 // 188 * 188

        def live(_request, count):
            if count == 1:

                def broken(request):
                    request.wfile.write(self.video[:half])

                return 200, {"Content-Length": str(len(self.video))}, broken
            return 407, {}, b""

        server = self.server({"/user/secret/live.ts": live})
        source = open_source(
            server.url + "/user/secret/live.ts", headers={}, verify_ssl=True, stop=threading.Event()
        )
        patient = {"_REFUSED_RETRY_S": 0.05, "_REFUSED_PATIENCE_S": 0.3}
        with patch.multiple(source_module, _RECONNECT_DELAYS=FAST_RETRIES, **patient):
            with self.assertRaises(SourceError) as raised:
                list(source.chunks())
        self.assertEqual((raised.exception.kind, raised.exception.status), ("http", 407))
        self.assertGreater(server.counts["/user/secret/live.ts"], 3)

    def test_a_links_refused_reconnect_is_final_at_once_and_an_accounts_is_patient(self):
        # Plan 4: a link no provider owns (`once`) counts nowhere: a refusal at its reconnect
        # is final at the first ask. A provider's channel (an account's link too) is asked
        # again patiently, as its closed connection may still count there.
        half = len(self.video) // 2 // 188 * 188
        for once, path in ((True, "/link/live.ts"), (False, "/user/secret/live.ts")):

            def live(_request, count):
                if count == 1:

                    def broken(request):
                        request.wfile.write(self.video[:half])

                    return 200, {"Content-Length": str(len(self.video))}, broken
                if count <= 3:
                    return 403, {}, b""
                return 200, {}, self.video[half:]

            server = self.server({path: live})
            with self.subTest(once=once):
                source = open_source(
                    server.url + path,
                    headers={},
                    verify_ssl=True,
                    stop=threading.Event(),
                    once=once,
                )
                patient = {"_REFUSED_RETRY_S": 0.05, "_REFUSED_PATIENCE_S": 5.0}
                with patch.multiple(source_module, _RECONNECT_DELAYS=FAST_RETRIES, **patient):
                    if once:
                        with self.assertRaises(SourceError) as raised:
                            list(source.chunks())
                        self.assertEqual(
                            (raised.exception.kind, raised.exception.status), ("http", 403)
                        )
                        self.assertEqual(server.counts[path], 2)  # the stream, one reconnect
                    else:
                        received = list(source.chunks())
                        media_bytes = b"".join(i for i in received if isinstance(i, bytes))
                        self.assertEqual(media_bytes, self.video)
                        self.assertEqual(server.counts[path], 4)  # two refusals waited out

    def test_a_link_answered_with_a_recording_is_not_asked_again(self):
        # Plan 4: the same for a reconnect answered with a recording, not the channel.
        clip = ts_fixtures.make_stream(2)

        def answer(_request, _count):
            def body(request):
                request.wfile.write(clip)

            return 200, {}, body

        server = self.server({"/live.ts": answer})
        with patch.multiple(source_module, _RECONNECT_DELAYS=FAST_RETRIES, _REFUSED_RETRY_S=0.02):
            source = open_source(
                server.url + "/live.ts",
                headers={},
                verify_ssl=True,
                stop=threading.Event(),
                once=True,
            )
            with self.assertRaises(SourceError) as failure:
                try:
                    for item in source.chunks():
                        if isinstance(item, bytes):
                            source.not_live()  # the guard: a recording, not the channel
                finally:
                    source.close()
        self.assertEqual(str(failure.exception), STREAM_LIMIT)
        self.assertEqual(server.counts["/live.ts"], 1)

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
        heard = []
        source.listen_retries(heard.append)
        fast = {"_RECONNECT_DELAYS": FAST_RETRIES, "_ENDLESS_DELAYS": (0.01,)}
        with patch.multiple(source_module, **fast):
            items = list(source.chunks())  # never given up on: it came back at the 21st
        self.assertEqual(server.counts["/live.ts"], 21)
        self.assertEqual(items.count(Discontinuity()), 20)
        # Past the six quick tries, each answer that broke off at once waited the long way.
        self.assertEqual(len([at for at in heard if at is not None]), 21 - 1 - 6)

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
                if count == 2:
                    return 200, {}, self.video[half:]  # sized, but this one is live
                return 404, {}, b""  # what ends the test

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
                    source = open_source(
                        server.url + path, headers={}, verify_ssl=True, stop=threading.Event()
                    )
                    parts = []
                    with self.assertRaises(SourceError) as failure:  # the 404, not an end
                        parts = drain_into(source, parts)
                    self.assertEqual(failure.exception.status, 404)
                    self.assertEqual(
                        parts,
                        (
                            [self.video[:half], Discontinuity(), self.video[half:], Discontinuity()]
                            if parts[-1:] == [Discontinuity()]
                            else [self.video[:half], Discontinuity(), self.video[half:]]
                        ),
                    )
            self.assertEqual(media(server.url + "/sized.ts"), [self.video])
        self.assertEqual(server.counts, {"/fin.ts": 3, "/chunked.ts": 3, "/sized.ts": 1})

    def test_a_tune_answered_with_a_sized_recording_leaves_the_channel_live(self):
        # Review: the tune's first answer, a sized recording the guard dropped, made the whole
        # source "finite": a later reconnect answered with an empty sized body ended it.
        clip, half = ts_fixtures.make_stream(2), len(self.video) // 2 // 188 * 188

        def route(_request, count):
            if count == 1:
                return 200, {}, clip  # sized (Content-Length): the recording
            if count == 2:

                def first_half(request):
                    request.wfile.write(self.video[:half])

                return 200, {}, first_half  # the channel, live
            if count == 3:
                return 200, {"Content-Length": "0"}, b""
            if count == 4:
                return 200, {}, self.video[half:]
            return 404, {}, b""

        server = self.server({"/live.ts": route})
        with patch.multiple(source_module, _RECONNECT_DELAYS=FAST_RETRIES, _REFUSED_RETRY_S=0.01):
            source = open_source(
                server.url + "/live.ts", headers={}, verify_ssl=True, stop=threading.Event()
            )
            told = False
            with self.assertRaises(SourceError):  # the 404, not an end
                try:
                    for item in source.chunks():
                        if isinstance(item, bytes) and not told:
                            told = True
                            source.not_live()
                finally:
                    source.close()
        self.assertGreaterEqual(server.counts["/live.ts"], 5)

    def test_a_live_channels_reconnect_answered_with_an_empty_sized_body_is_no_end(self):
        # 2.8.12: a reconnect answered "200, Content-Length: 0" (or an error page, a short
        # clip) ended a live channel's input for good: playback stopped at the buffer's end.
        half = len(self.video) // 2 // 188 * 188

        def first_half(request):
            request.wfile.write(self.video[:half])

        def route(_request, count):
            if count == 1:
                return 200, {}, first_half
            if count == 2:
                return 200, {"Content-Length": "0"}, b""
            if count == 3:
                return 200, {}, self.video[half:]
            return 404, {}, b""

        server = self.server({"/live.ts": route})
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES):
            source = open_source(
                server.url + "/live.ts", headers={}, verify_ssl=True, stop=threading.Event()
            )
            parts = []
            with self.assertRaises(SourceError):
                drain_into(source, parts)
        self.assertIn(self.video[half:], [p for p in parts if isinstance(p, bytes)])
        self.assertGreaterEqual(server.counts["/live.ts"], 3)

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
            "/407.ts": (answer(407), STREAM_REFUSED),  # 2.9.7: a panel's one stream too many
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

    def test_logs_how_far_the_newest_piece_ends_behind_the_clock(self):
        # The latency instrument: the last piece's date is now - 10 s and it lasts 4 s, so it
        # ends about 6 s behind the clock (the start is at the last piece: one line).
        names = [f"s{number}.ts" for number in range(3)]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        now = time.time()
        text = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n"
        for number, name in enumerate(names):
            when = now - 10 - 4 * (len(names) - 1 - number)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when))
            stamp += f".{int(when % 1 * 1000):03d}Z"
            text += f"#EXT-X-PROGRAM-DATE-TIME:{stamp}\n#EXTINF:4,\n{name}\n"
        routes["/live.m3u8"] = (200, {}, text.encode())
        server = self.server(routes)
        stop = threading.Event()
        timer = threading.Timer(1.0, stop.set)  # before the first reload
        timer.start()
        with self.assertLogs("services.live.source", "DEBUG") as logs:
            media(server.url + "/live.m3u8", stop=stop, live_delay=0.0)
        timer.join()
        lines = [r.getMessage() for r in logs.records if "behind the clock" in r.getMessage()]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"ends 6\.\d s behind")

    def lag_lines(self, text, names):
        """The lag lines of a run on the playlist `text` whose pieces are `names` (one second
        of it: the start's batch, before a reload brings anything new)."""
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/live.m3u8"] = (200, {}, text.encode())
        server = self.server(routes)
        stop = threading.Event()
        timer = threading.Timer(1.0, stop.set)
        timer.start()
        with self.assertLogs("services.live.source", "DEBUG") as logs:
            media(server.url + "/live.m3u8", stop=stop, live_delay=0.0)
        timer.join()
        return [r.getMessage() for r in logs.records if "behind the clock" in r.getMessage()]

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset to set the local zone")
    def test_a_date_without_a_zone_is_read_as_utc(self):
        # A PROGRAM-DATE-TIME without a zone read as local time put the piece hours away on
        # any machine whose clock is not on UTC.
        when = time.time() - 10
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when))
        text = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n"
        text += f"#EXT-X-PROGRAM-DATE-TIME:{stamp}\n#EXTINF:4,\ns0.ts\n"
        self.addCleanup(time.tzset)
        with patch.dict("os.environ", {"TZ": "QIT-5"}):  # five hours east of UTC
            time.tzset()
            lines = self.lag_lines(text, ["s0.ts"])
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"ends [67]\.\d s behind")

    def test_a_prefetch_piece_logs_the_time_since_its_last_byte_came(self):
        # A piece read as it is made ends at the clock once its last byte came: its date (the
        # last finished piece's plus that one's length: it would end 2 s behind) is no measure.
        when = time.time() - 10
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when))
        text = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n"
        text += f"#EXT-X-PROGRAM-DATE-TIME:{stamp}.000Z\n#EXTINF:4,live\ns0.ts\n"
        text += "#EXT-X-PREFETCH:p1.ts\n"
        lines = self.lag_lines(text, ["s0.ts", "p1.ts"])
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"ends 0\.[0-2] s behind")

    def test_live_hls_starts_far_enough_back_for_the_sessions_target(self):
        # 2.1: a sparse-PCR channel's 10 s segments fetched from 6 s before the end
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
        # A finished (ENDLIST) playlist plays from its beginning; a segment
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
        # 2.1: the helper predicts the live edge once the backlog fetched at the
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

    def test_a_reload_that_brings_the_same_copy_is_not_parsed_again(self):  # 2.8
        # A long DVR window's parse takes tens of ms; about half the reloads bring nothing new.
        same = hls(0, ["a.ts", "b.ts"])
        copies = [same, same, same, hls(0, ["a.ts", "b.ts", "c.ts"], ended=True)]

        def playlist(_request, count):
            return 200, {}, copies[min(count, len(copies)) - 1]

        routes = {f"/{n}": (200, {}, self.video) for n in ("a.ts", "b.ts", "c.ts")}
        routes["/live.m3u8"] = playlist
        server = self.server(routes)
        loads = []
        real = source_module.m3u8.loads

        def counted(text, *args, **kwargs):
            loads.append(1)
            return real(text, *args, **kwargs)

        with patch.object(source_module.m3u8, "loads", side_effect=counted):
            media(server.url + "/live.m3u8", live_delay=2.0)
        self.assertEqual(server.counts["/live.m3u8"], 4)
        self.assertEqual(len(loads), 2)  # the first copy and the new one

    def test_a_resumed_run_goes_on_after_the_run_it_joins(self):  # 2.8: no replay
        # A rewound viewer's kept buffer taken back (a new picture size, a quick zap back):
        # the new run starts after the old run's last whole segment when its playlist still
        # lists the one after it and that is no further back than its usual start; else as
        # any start.
        names = [f"s{k}.ts" for k in range(10, 21)]
        copies = [hls(10, names[:10]), hls(11, names[1:], ended=True)]
        cases = ((17, "s18.ts"), (19, "s20.ts"), (12, "s17.ts"), (40, "s17.ts"), (None, "s17.ts"))
        for after, first in cases:
            with self.subTest(after=after):

                def playlist(_request, count, copies=copies):
                    return 200, {}, copies[min(count, len(copies)) - 1]

                routes = {f"/{name}": (200, {}, self.video) for name in names}
                routes["/live.m3u8"] = playlist
                server = self.server(routes)
                media(server.url + "/live.m3u8", live_delay=3.0, after_seq=after)
                self.assertEqual(requested_segments(server)[0], first)

    def test_a_media_sequence_restart_resumes_at_the_new_live_start(self):
        # New names numbered back: believed once its copies persist as long as a stale copy is
        # waited out (2.6.1: another origin group's copy looks alike, and a reload soon answers
        # ours again); a newer discontinuity sequence says so at once.
        restarted = hls(0, ["r0.ts", "r1.ts", "r2.ts"])
        cases = {
            "new segment names": (
                [
                    hls(100, ["s100.ts", "s101.ts", "s102.ts"]),
                    *[restarted] * 4,  # three target durations: 1 s each here
                    hls(1, ["r1.ts", "r2.ts", "r3.ts"], ended=True),
                ],
                ["s102.ts", "r2.ts", "r3.ts"],
            ),
            "new discontinuity sequence": (
                [
                    hls(100, ["x.ts", "y.ts", "z.ts"], extra="#EXT-X-DISCONTINUITY-SEQUENCE:0\n"),
                    hls(5, ["y.ts", "z.ts", "n.ts"], extra="#EXT-X-DISCONTINUITY-SEQUENCE:1\n"),
                    hls(
                        6,
                        ["z.ts", "n.ts", "m.ts"],
                        ended=True,
                        extra="#EXT-X-DISCONTINUITY-SEQUENCE:1\n",
                    ),
                ],
                ["z.ts", "n.ts", "m.ts"],
            ),
        }
        for name, (copies, fetched) in cases.items():
            with self.subTest(name):

                def playlist(_request, count, copies=copies):
                    return 200, {}, copies[min(count, len(copies)) - 1]

                data = dict(zip(fetched, (self.video, self.next_video, self.video)))
                routes = {f"/{segment}": (200, {}, body) for segment, body in data.items()}
                routes["/live.m3u8"] = playlist
                server = self.server(routes)
                with patch.object(source_module, "_OTHER_COPY_S", 0.0):  # three targets
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

    def test_hls_source_names_the_media_sequence_it_yields(self):
        # 2.7: each buffer segment that begins an HLS segment knows its media sequence.
        routes = {f"/{name}.ts": (200, {}, self.video) for name in "abc"}
        routes["/live.m3u8"] = (200, {}, hls(10, ["a.ts", "b.ts", "c.ts"], ended=True))
        server = self.server(routes)
        source = open_source(
            server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
        )
        seen = []
        try:
            for item in source.chunks():
                if isinstance(item, bytes) and item and source.segment_sequence not in seen:
                    seen.append(source.segment_sequence)
        finally:
            source.close()
        self.assertEqual(seen, [10, 11, 12])

    def test_a_live_hls_source_reports_its_window(self):
        # 2.7: how long the server keeps the channel (a long window is offered on the bar).
        cases = (("live", False, 30.0, 104), ("finished", True, 0.0, 104))
        for name, ended, window, edge in cases:
            with self.subTest(name):
                names = [f"s{n}.ts" for n in range(5)]
                routes = {"/live.m3u8": (200, {}, hls(100, names, target=6, ended=ended))}
                server = self.server(routes)
                source = open_source(
                    server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
                )
                try:
                    self.assertEqual(source.window_s, window)
                    self.assertEqual(source.edge_sequence, edge)
                finally:
                    source.close()

    def test_a_server_part_starts_back_and_stops_before_a_sequence(self):
        # 2.7: a server part opens back in the window and its input ends before the live
        # buffer's first media sequence: it never fetches what the buffer holds.
        names = [f"s{n}.ts" for n in range(100, 110)]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/s107.ts"] = (200, {}, self.next_video)
        routes["/live.m3u8"] = (200, {}, hls(100, names))
        server = self.server(routes)
        window = {"back_s": 4.0, "until_seq": 108}
        parts = media(server.url + "/live.m3u8", live_delay=1.0, window=window)
        self.assertEqual(parts, joined(self.video, self.next_video))
        self.assertEqual(requested_segments(server), ["s106.ts", "s107.ts"])

    @staticmethod
    def recording(names, *, piece=10, ended=True, dated=True):
        """A site's recording of the running broadcast as Kick lists it: 10 s pieces from
        piece 0, each dated (`DAY` on), EVENT, and ENDLIST while it still grows."""
        text = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:13\n#EXT-X-PLAYLIST-TYPE:EVENT\n"
        text += "#EXT-X-MEDIA-SEQUENCE:0\n"
        for n, name in enumerate(names):
            if dated:
                text += f"#EXT-X-PROGRAM-DATE-TIME:{stamp(n * piece)}\n"
            text += f"#EXTINF:{piece}.000,\n{name}\n"
        return (text + ("#EXT-X-ENDLIST\n" if ended else "")).encode()

    def test_a_recording_part_starts_at_its_moment_and_stops_at_the_buffers_date(self):
        # 2.15: the site's recording of the running broadcast from the piece holding the
        # moment asked, its input ending at the piece edge where the buffer begins (by date).
        names = [f"r{n}.ts" for n in range(6)]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/rec.m3u8"] = (200, {}, self.recording(names))
        server = self.server(routes)
        window = {"from_s": 25.0, "stop_s": 999.0, "stop_date": DAY.timestamp() + 50.0}
        source = open_source(
            server.url + "/rec.m3u8",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            live_delay=1.0,
            window=window,
        )
        drain(source)
        self.assertEqual(requested_segments(server), ["r2.ts", "r3.ts", "r4.ts"])
        self.assertEqual(source.part_start_s, 20.0)
        self.assertEqual(source.part_date, DAY.timestamp() + 20.0)
        self.assertEqual(source.listed_s, 60.0)
        self.assertIsNone(source.part_wall)  # (placed by its date, never its arrival)

    def test_an_undated_recording_part_stops_by_its_place_in_the_listing(self):
        # Twitch's lists no dates: its stop is its place, the recording's start counted on.
        names = [f"r{n}.ts" for n in range(6)]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/rec.m3u8"] = (200, {}, self.recording(names, ended=False, dated=False))
        server = self.server(routes)
        window = {"from_s": 0.0, "stop_s": 30.0, "stop_date": DAY.timestamp() + 30.0}
        media(server.url + "/rec.m3u8", live_delay=1.0, window=window)
        self.assertEqual(requested_segments(server), ["r0.ts", "r1.ts", "r2.ts"])

    def test_a_recording_part_ends_with_the_piece_reaching_its_stop_unlisted_next(self):
        # Live 2026-11-25: the piece after it was not listed yet (Kick lists in 30-60 s
        # steps): waiting for it held the join 4.5 s. The piece reaching the stop is its last.
        names = [f"r{n}.ts" for n in range(4)]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/rec.m3u8"] = (200, {}, self.recording(names))
        server = self.server(routes)
        window = {
            "from_s": 10.0,
            "stop_s": 999.0,
            "stop_date": DAY.timestamp() + 35.0,
            "grows": True,
            "reload_s": 30.0,
        }
        began, stop = time.monotonic(), threading.Event()
        timer = threading.Timer(6.0, stop.set)  # (bounds a run that waits for the reload)
        timer.start()
        self.addCleanup(timer.cancel)
        try:
            media(server.url + "/rec.m3u8", live_delay=1.0, window=window, stop=stop)
        except Exception:
            pass
        self.assertLess(time.monotonic() - began, 5.0)  # never the 30 s reload
        self.assertEqual(requested_segments(server), ["r1.ts", "r2.ts", "r3.ts"])
        self.assertEqual(server.counts["/rec.m3u8"], 1)

    def test_a_recording_part_reads_on_past_its_stop_while_listed_and_ends_at_its_listing(self):
        # Review (2026-11-25): its join lies inside the buffer by `late_s` while the recording
        # lists that far, so the buffer's start may move on; where it lists less, its listing's
        # end past the stop is its last (no wait for more).
        names = [f"r{n}.ts" for n in range(10)]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/rec.m3u8"] = (200, {}, self.recording(names))
        server = self.server(routes)
        window = {"from_s": 0.0, "stop_s": 999.0, "stop_date": DAY.timestamp() + 25.0}
        media(server.url + "/rec.m3u8", live_delay=1.0, window=dict(window, late_s=20.0))
        self.assertEqual(requested_segments(server), [f"r{n}.ts" for n in range(5)])  # to 50
        short = {f"/r{n}.ts": (200, {}, self.video) for n in range(4)}
        short["/rec.m3u8"] = (200, {}, self.recording([f"r{n}.ts" for n in range(4)]))
        server = self.server(short)
        window.update(late_s=70.0, grows=True, reload_s=30.0)
        began = time.monotonic()
        media(server.url + "/rec.m3u8", live_delay=1.0, window=window)
        self.assertLess(time.monotonic() - began, 5.0)
        self.assertEqual(requested_segments(server), [f"r{n}.ts" for n in range(4)])

    def test_a_recording_part_is_paced_by_its_stop_ahead_from_its_stop_on(self):
        # 2.15, AI captions: fetched far ahead (its `ahead_s`) up to its stop, and from there
        # by `stop_ahead_s`, so its input ends no sooner than before (its join's room).
        names = [f"r{n}.ts" for n in range(8)]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/rec.m3u8"] = (200, {}, self.recording(names))
        server = self.server(routes)
        asked = []

        def hold(ahead=None):
            asked.append((len(requested_segments(server)), ahead))
            return False

        window = {
            "from_s": 0.0,
            "stop_s": 999.0,
            "stop_date": DAY.timestamp() + 30.0,
            "late_s": 30.0,
            "stop_ahead_s": 60.0,
        }
        source = open_source(
            server.url + "/rec.m3u8",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            live_delay=1.0,
            window=window,
            hold=hold,
        )
        drain(source)
        self.assertEqual(requested_segments(server), [f"r{n}.ts" for n in range(6)])
        # before r0-r2 its own pace; before r3-r5 (from the stop at 30 s on) a minute's
        self.assertEqual([ahead for _count, ahead in asked], [None] * 3 + [60.0] * 3)
        self.assertEqual([count for count, _ahead in asked], list(range(6)))

    def test_a_recording_part_waiting_at_its_listings_end_reads_it_again_sooner(self):
        # Brief 2, 8: once all it lists is fetched short of its stop, its listing is read again
        # every 10 s (not Kick's 30 s step), one ask at a time.
        def playlist(_request, count):
            listed = 2 if count == 1 else 4
            return 200, {}, self.recording([f"r{n}.ts" for n in range(listed)])

        routes = {f"/r{n}.ts": (200, {}, self.video) for n in range(4)}
        routes["/rec.m3u8"] = playlist
        server = self.server(routes)
        window = {
            "from_s": 0.0,
            "stop_s": 999.0,
            "stop_date": DAY.timestamp() + 35.0,
            "grows": True,
            "reload_s": 30.0,
        }
        self.enterContext(patch.object(source_module, "_HOLE_RELOAD_S", 0.2))
        began = time.monotonic()
        media(server.url + "/rec.m3u8", live_delay=1.0, window=window)
        self.assertLess(time.monotonic() - began, 5.0)
        self.assertEqual(requested_segments(server), [f"r{n}.ts" for n in range(4)])

    def test_a_growing_recording_goes_on_past_its_ended_listing_to_its_stop(self):
        # Kick's says ENDLIST while it grows in steps: read again, on to the stop.
        def playlist(_request, count):
            listed = 3 if count == 1 else 6
            return 200, {}, self.recording([f"r{n}.ts" for n in range(listed)])

        routes = {f"/r{n}.ts": (200, {}, self.video) for n in range(6)}
        routes["/rec.m3u8"] = playlist
        server = self.server(routes)
        window = {
            "from_s": 0.0,
            "stop_s": 999.0,
            "stop_date": DAY.timestamp() + 40.0,
            "grows": True,
            "reload_s": 0.05,
        }
        media(server.url + "/rec.m3u8", live_delay=1.0, window=window)
        self.assertEqual(requested_segments(server), ["r0.ts", "r1.ts", "r2.ts", "r3.ts"])
        self.assertGreaterEqual(server.counts["/rec.m3u8"], 2)

    def test_a_server_part_whose_start_left_the_window_goes_on_from_its_oldest(self):
        # Review Focus 2: near the window's oldest end the window slides past the next
        # segment; the part goes on from the window's oldest, not from the live end.
        def playlist(_request, count):
            if count == 1:
                return 200, {}, hls(100, ["s100.ts", "s101.ts"])
            return 200, {}, hls(105, ["s105.ts", "s106.ts", "s107.ts"], ended=True)

        routes = {f"/s{n}.ts": (200, {}, self.video) for n in range(100, 108)}
        routes["/live.m3u8"] = playlist
        server = self.server(routes)
        parts = media(server.url + "/live.m3u8", live_delay=1.0, window={"back_s": 2.0})
        self.assertIn(Discontinuity(), parts)  # s102 to s104 left the window meanwhile
        fetched = ["s100.ts", "s101.ts", "s105.ts", "s106.ts", "s107.ts"]
        self.assertEqual(requested_segments(server), fetched)  # a live source: s107 only

    def test_a_server_part_on_another_groups_copy_asks_again(self):
        # Review Focus 1: the first copy numbers another encoding (no 108 in its window): it
        # is asked for again, and the part stops before 108 in ours.
        ours = hls(100, [f"s{n}.ts" for n in range(100, 110)])

        def playlist(_request, count):
            return 200, {}, (hls(500, ["x.ts", "y.ts", "z.ts"]) if count == 1 else ours)

        routes = {f"/s{n}.ts": (200, {}, self.video) for n in range(100, 110)}
        routes["/s107.ts"] = (200, {}, self.next_video)
        routes["/live.m3u8"] = playlist
        server = self.server(routes)
        window = {"back_s": 4.0, "until_seq": 108}
        parts = media(server.url + "/live.m3u8", live_delay=1.0, window=window)
        self.assertEqual(parts, joined(self.video, self.next_video))
        self.assertEqual(requested_segments(server), ["s106.ts", "s107.ts"])

    def test_a_server_part_reports_the_media_before_its_stop(self):
        # 2.7: the session places the part flush against the buffer's first HLS segment.
        names = [f"s{n}.ts" for n in range(100, 110)]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/live.m3u8"] = (200, {}, hls(100, names))
        server = self.server(routes)
        source = open_source(
            server.url + "/live.m3u8",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            window={"back_s": 4.0, "until_seq": 108},
        )
        try:
            next(iter(source.chunks()))
            self.assertEqual(source.part_media_s, 2.0)  # s106 and s107, 1 s each
        finally:
            source.close()

    def test_a_server_part_counts_back_from_its_stop(self):
        # Review I8: from the stop, not the playlist's end (a segment published meanwhile, or
        # a wall clock off, would otherwise start at or past the stop: an empty part).
        names = [f"s{n}.ts" for n in range(100, 110)]
        for before, fetched in ((2.0, ["s106.ts", "s107.ts"]), (0.5, ["s107.ts"])):
            with self.subTest(before=before):
                routes = {f"/{name}": (200, {}, self.video) for name in names}
                routes["/live.m3u8"] = (200, {}, hls(100, names))
                server = self.server(routes)
                window = {"back_s": 1.0, "before_s": before, "until_seq": 108}
                media(server.url + "/live.m3u8", live_delay=1.0, window=window)
                self.assertEqual(requested_segments(server), fetched)

    def test_a_server_part_whose_other_encoding_persists_drops_its_stop(self):
        # Review I5: its stop was ours; the buffer's later stops are too: none applies.
        def playlist(_request, count):
            return 200, {}, hls(500, ["x.ts", "y.ts", "z.ts"], ended=count >= 4)

        routes = {f"/{name}.ts": (200, {}, self.video) for name in "xyz"}
        routes["/live.m3u8"] = playlist
        server = self.server(routes)
        with (
            patch.object(source_module, "_OTHER_COPY_S", 0.0),
            patch.object(source_module, "_STALE_TARGETS", 1),
        ):
            source = open_source(
                server.url + "/live.m3u8",
                headers={},
                verify_ssl=True,
                stop=threading.Event(),
                window={"back_s": 3.0, "until_seq": 108},
            )
            try:
                parts = list(source.chunks())
            finally:
                source.close()
        self.assertTrue(source.until_dropped)
        source.until_seq = 110  # the buffer's start moved: not this part's numbers
        self.assertIsNone(source.until_seq)
        self.assertTrue(any(isinstance(part, bytes) and part for part in parts))

    @patch.object(source_module, "_HELD_RELOAD_S", 1.0)
    def test_a_held_part_reloads_before_going_on(self):
        # Review I4: a part held long (a pause) reloads first: the window may have slid past
        # its next segment, and its URLs may have expired.
        held = threading.Event()

        def first(request):
            held.set()  # the player is behind: hold before the next one
            request.wfile.write(self.video)

        def playlist(_request, count):
            if count == 1:
                return 200, {}, hls(100, ["s100.ts", "s101.ts", "s102.ts"])
            return 200, {}, hls(105, ["s105.ts", "s106.ts", "s107.ts"], ended=True)

        routes = {f"/s{n}.ts": (200, {}, self.video) for n in range(101, 108)}
        routes["/s100.ts"] = (200, {"Content-Length": str(len(self.video))}, first)
        routes["/live.m3u8"] = playlist
        server = self.server(routes)
        source = open_source(
            server.url + "/live.m3u8",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            window={"back_s": 3.0},
            hold=held.is_set,
        )
        got = []
        reader = threading.Thread(target=lambda: got.extend(source.chunks()), daemon=True)
        reader.start()
        time.sleep(1.5)  # held longer than a target duration (1 s)
        held.clear()
        reader.join(5)
        source.close()
        self.assertEqual(requested_segments(server), ["s100.ts", "s105.ts", "s106.ts", "s107.ts"])
        self.assertIn(Discontinuity(), got)

    def test_a_part_held_for_a_segment_goes_on_without_reloading(self):
        # A paced part holds about a segment at a time; reading a 24 h
        # playlist again (~0.6 s of the helper's CPU) after each such hold starved the player.
        held = threading.Event()

        def first(request):
            held.set()  # the player is a segment behind: hold before the next one
            request.wfile.write(self.video)

        names = ["s100.ts", "s101.ts", "s102.ts"]
        routes = {f"/{name}": (200, {}, self.video) for name in names[1:]}
        routes["/s100.ts"] = (200, {"Content-Length": str(len(self.video))}, first)
        routes["/live.m3u8"] = (200, {}, hls(100, names, ended=True))
        server = self.server(routes)
        source = open_source(
            server.url + "/live.m3u8",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            window={"back_s": 3.0},
            hold=held.is_set,
        )
        reader = threading.Thread(target=lambda: list(source.chunks()), daemon=True)
        reader.start()
        time.sleep(1.5)  # longer than a target duration (1 s), shorter than a pause
        held.clear()
        reader.join(5)
        source.close()
        self.assertEqual(requested_segments(server), names)
        self.assertEqual(server.counts["/live.m3u8"], 1)

    def test_a_held_hls_source_fetches_nothing_until_let_go(self):
        # 2.7: `hold` pauses the fetching between segments (a server part's pacing).
        names = ["a.ts", "b.ts", "c.ts"]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/live.m3u8"] = (200, {}, hls(0, names, ended=True))
        server = self.server(routes)
        held = threading.Event()
        held.set()
        source = open_source(
            server.url + "/live.m3u8",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            hold=held.is_set,
        )
        got = []
        reader = threading.Thread(target=lambda: got.extend(source.chunks()), daemon=True)
        reader.start()
        time.sleep(0.6)
        self.assertEqual(requested_segments(server), [])
        held.clear()
        reader.join(5)
        source.close()
        self.assertEqual(requested_segments(server), names)

    def test_a_ts_archive_plays_through_the_buffer(self):
        # 2.7: a provider archive's raw TS opens as a server part (an archive's window,
        # `until_s`): paced by `hold`, and a broken connection ends it (a reconnect would play
        # the address from its start again).
        body = self.video * 4
        half = len(body) // 2 // 188 * 188

        def broken(request):
            request.wfile.write(body[:half])

        server = self.server({"/archive.ts": (200, {"Content-Length": str(len(body))}, broken)})
        held = threading.Event()
        held.set()
        source = open_source(
            server.url + "/archive.ts",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            window={"until_s": 600.0, "ahead_s": 60.0},
            hold=held.is_set,
        )
        got = []
        reader = threading.Thread(target=lambda: got.extend(source.chunks()), daemon=True)
        reader.start()
        time.sleep(0.6)
        while_held = sum(len(item) for item in list(got) if isinstance(item, bytes))
        held.clear()
        reader.join(5)
        source.close()
        self.assertLess(while_held, half)  # the bytes the open read, no more
        self.assertEqual(b"".join(item for item in got if isinstance(item, bytes)), body[:half])
        self.assertNotIn(Reconnecting(), got)
        self.assertEqual(server.counts["/archive.ts"], 1)

    def test_an_hls_archive_starts_at_its_first_segment(self):
        # 2.7: an archive's playlist begins at the moment asked, also one still listed live.
        names = [f"s{n}.ts" for n in range(30)]  # a live start would be ~6 s from its end
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/archive.m3u8"] = (200, {}, hls(0, names, ended=False))
        server = self.server(routes)
        window = {"until_s": 600.0, "ahead_s": 60.0}
        source = open_source(
            server.url + "/archive.m3u8",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            window=window,
        )
        try:
            for item in source.chunks():
                if isinstance(item, bytes):
                    break
        finally:
            source.close()
        self.assertEqual(requested_segments(server)[0], "s0.ts")

    @patch.object(source_module, "_HELD_RELOAD_S", 1.0)
    def test_a_finished_archive_goes_on_after_a_long_hold(self):
        # Final review C3: a held ENDLIST archive (the player behind) reloads and goes on; it
        # does not end at the hold.
        held = threading.Event()
        names = [f"s{n}.ts" for n in range(4)]

        def first(request):
            held.set()
            request.wfile.write(self.video)

        routes = {f"/{name}": (200, {}, self.video) for name in names[1:]}
        routes["/s0.ts"] = (200, {"Content-Length": str(len(self.video))}, first)
        routes["/archive.m3u8"] = (200, {}, hls(0, names, ended=True))
        server = self.server(routes)
        source = open_source(
            server.url + "/archive.m3u8",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            window={"until_s": 600.0, "ahead_s": 60.0},
            hold=held.is_set,
        )
        reader = threading.Thread(target=lambda: list(source.chunks()), daemon=True)
        reader.start()
        time.sleep(1.5)  # held longer than a target duration (1 s)
        held.clear()
        reader.join(5)
        source.close()
        self.assertEqual(requested_segments(server), names)

    def test_a_server_parts_first_wall_is_its_time_of_day(self):
        # Its first segment's time of day: the opened copy's segments after it, counted back
        # from now (the bar places the part by it).
        names = [f"s{n}.ts" for n in range(100, 110)]
        routes = {f"/{name}": (200, {}, self.video) for name in names}
        routes["/live.m3u8"] = (200, {}, hls(100, names))
        server = self.server(routes)
        with patch.object(source_module, "_wall_time", lambda: 1000.0):
            source = open_source(
                server.url + "/live.m3u8",
                headers={},
                verify_ssl=True,
                stop=threading.Event(),
                window={"back_s": 4.0, "until_seq": 108},
            )
            try:
                next(iter(source.chunks()))
                self.assertEqual(source.part_wall, 996.0)
                self.assertTrue(source.finite)
            finally:
                source.close()

    def test_a_copy_from_another_origin_group_is_set_aside(self):
        # Two origin groups behind one address, each numbering and naming its own encoding
        # (measured 2.6.1, a 24 h public playlist): the other group's copy is neither a stale
        # copy of ours nor a restart, and a reload a second later answers ours again.
        ours = [
            hls(10, ["a.ts", "b.ts", "c.ts"]),
            hls(11, ["b.ts", "c.ts", "d.ts"]),
            hls(12, ["c.ts", "d.ts", "e.ts"], ended=True),
        ]
        others = {
            "numbered far ahead": hls(500, ["x.ts", "y.ts", "z.ts"]),
            "numbered behind": hls(2, ["x.ts", "y.ts", "z.ts"]),
            "numbered alike": hls(11, ["x.ts", "y.ts", "z.ts"]),
        }
        for case, other in others.items():
            with self.subTest(case):
                copies = [ours[0], other, ours[1], other, other, ours[2]]

                def playlist(_request, count, copies=copies):
                    return 200, {}, copies[min(count, len(copies)) - 1]

                routes = {f"/{name}.ts": (200, {}, self.video) for name in "abcexyz"}
                routes["/d.ts"] = (200, {}, self.next_video)
                routes["/live.m3u8"] = playlist
                server = self.server(routes)
                parts = media(server.url + "/live.m3u8", live_delay=1.0)
                self.assertEqual(parts, joined(self.video, self.next_video, self.video))
                self.assertEqual(requested_segments(server), ["c.ts", "d.ts", "e.ts"])

    def test_another_groups_copy_is_asked_again_on_a_new_connection(self):
        # A load balancer that pins a connection to one origin group (measured 2.6.1): the
        # kept-alive connection went on answering the other group's copies, so the playlist
        # is asked for again on a fresh one.
        ours = [
            hls(10, ["a.ts", "b.ts", "c.ts"]),
            hls(11, ["b.ts", "c.ts", "d.ts"]),
            hls(12, ["c.ts", "d.ts", "e.ts"], ended=True),
        ]
        first_port = []
        served = []

        def playlist(request, _count):
            port = request.client_address[1]
            if first_port and port == first_port[0] and served:
                return 200, {}, hls(500, ["x.ts", "y.ts", "z.ts"])  # it went over
            first_port[:] = first_port or [port]
            served.append(ours[min(len(served), 2)])
            return 200, {}, served[-1]

        routes = {f"/{name}.ts": (200, {}, self.video) for name in "abcexyz"}
        routes["/d.ts"] = (200, {}, self.next_video)
        routes["/live.m3u8"] = playlist
        server = self.server(routes, keep_alive=True)
        parts = media(server.url + "/live.m3u8", live_delay=1.0)
        self.assertEqual(parts, joined(self.video, self.next_video, self.video))
        self.assertEqual(requested_segments(server), ["c.ts", "d.ts", "e.ts"])

    def test_another_groups_copies_that_persist_are_followed(self):
        # Ours stopped answering (its encoder went down): once the other group's copies
        # persisted three target durations (at least `_OTHER_COPY_S`), it plays on from them.
        def playlist(_request, count):
            if count == 1:
                return 200, {}, hls(10, ["a.ts", "b.ts", "c.ts"])
            return 200, {}, hls(500, ["x.ts", "y.ts", "z.ts"], ended=count >= 6)

        server = self.server(
            {
                "/live.m3u8": playlist,
                "/c.ts": (200, {}, self.video),
                "/z.ts": (200, {}, self.next_video),
            }
        )
        with patch.object(source_module, "_OTHER_COPY_S", 2.0):
            parts = media(server.url + "/live.m3u8", live_delay=1.0)
        self.assertEqual(parts, joined(self.video, Discontinuity(), self.next_video))
        self.assertEqual(requested_segments(server), ["c.ts", "z.ts"])
        self.assertGreaterEqual(server.counts["/live.m3u8"], 5)  # it waited 3 s for ours

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
                        # Its separate playlist cannot be read (404): unsupported, as before.
                        with self.assertRaises(SourceError) as raised:
                            media(server.url + "/master.m3u8")
                        self.assertEqual(raised.exception.kind, "unsupported")
                woven = not accepted  # a video-only variant asks for the separate one
                self.assertEqual(server.counts["/audio/fr.m3u8"], int(woven))
                if accepted and "external" in name:  # the default, left out for the variant's
                    self.assertTrue(any("left out" in line for line in logs), logs)
                elif accepted:  # an alternate rendition
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

    # ---- a live site's prefetch pieces: read as they are made --------------------------------

    def copies(self, copies, pieces, bodies=None):
        """A server whose playlist answers each of `copies` in turn (the last one from then
        on), and `pieces` (names) with the test video, or what `bodies` gives a name."""
        routes = {f"/{name}": (200, {}, self.video) for name in pieces}
        routes.update(bodies or {})

        def playlist(_request, count):
            return 200, {}, copies[min(count, len(copies)) - 1]

        routes["/live.m3u8"] = playlist
        return self.server(routes)

    def made(self, copies, pieces):
        """A server whose playlist stands as a live site's does while its pieces are made: its
        copy k once k pieces being made (the names starting "p") have been sent (the last copy
        from then on). Such a piece sends its last bytes as it ends, so a reload after two of
        them finds the playlist two pieces on. `pieces` (names) have the test video."""
        routes = {f"/{name}": (200, {}, self.video) for name in pieces}
        server = None

        def playlist(_request, _count):
            sent = sum(n for path, n in server.counts.items() if path.startswith("/p"))
            return 200, {}, copies[min(sent, len(copies) - 1)]

        routes["/live.m3u8"] = playlist
        server = self.server(routes)
        return server

    def run_for(self, server, seconds, **options):
        """`media` on the server's playlist, stopped after `seconds`."""
        stop = threading.Event()
        timer = threading.Timer(seconds, stop.set)
        timer.start()
        try:
            return media(server.url + "/live.m3u8", stop=stop, **options)
        finally:
            timer.cancel()

    def test_prefetch_pieces_are_fetched_after_the_finished_ones(self):
        names = [f"s{number}.ts" for number in range(6)]
        server = self.copies(
            [prefetching(0, names, ["p6.ts", "p7.ts"], target=2)], names + ["p6.ts", "p7.ts"]
        )
        self.run_for(server, 0.5, live_delay=100.0)  # all of it, before the first reload
        self.assertEqual(requested_segments(server), names + ["p6.ts", "p7.ts"])

    def test_each_piece_says_its_date_as_its_bytes_come(self):
        # 2026-11-25, the statistics' "Behind the broadcaster": the helper places the site's
        # dates on its timeline where each piece begins. A dated piece's own; one after it
        # dated by it; a piece being made the date reckoned for it (`_with_prefetch`); none
        # undated.
        day = DAY.timestamp()
        names, made = ["s0.ts", "s1.ts"], ["p2.ts"]
        once = (
            f"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:0\n"
            f"#EXT-X-PROGRAM-DATE-TIME:{stamp(0)}\n#EXTINF:2.000,live\ns0.ts\n"
            "#EXTINF:2.000,live\ns1.ts\n#EXT-X-PREFETCH:p2.ts\n"
        ).encode()
        cases = (
            ("each dated", site_playlist(0, names, made), [day, day + 2.0, day + 4.0]),
            ("the first dated", once, [day, day + 2.0, day + 4.0]),
            ("undated", site_playlist(0, names, made, dates=False), [None, None, None]),
        )
        for label, copy, dates in cases:
            with self.subTest(label):
                server = self.copies([copy], names + made)
                source = open_source(
                    server.url + "/live.m3u8",
                    headers={},
                    verify_ssl=True,
                    stop=threading.Event(),
                    live_delay=100.0,
                )
                seen = {}
                try:
                    self.assertIsNone(source.segment_date)
                    for item in source.chunks():
                        if isinstance(item, bytes) and item:
                            seen.setdefault(source.segment_sequence, source.segment_date)
                        if len(seen) == len(dates):
                            break
                finally:
                    source.close()
                self.assertEqual(list(seen), [0, 1, 2])
                for got, wanted in zip(seen.values(), dates):
                    if wanted is None:
                        self.assertIsNone(got)
                    else:
                        self.assertAlmostEqual(got, wanted, places=3)

    def test_starts_at_the_first_prefetch_piece(self):
        # Under Auto, Normal and Low: not the target's depth back from the end (three 6 s
        # target durations, Kick's: 7.6 s behind the clock in all). Under Stable (2026-11-25,
        # replacing ruling R4): its 6 s target back from where the pieces being made begin.
        names = [f"s{number}.ts" for number in range(8)]
        copy = prefetching(0, names, ["p8.ts", "p9.ts"], target=2)
        cases = (  # (latency, live delay, the session's target, the first piece fetched)
            ("auto", 6.0, 1.0, "p8.ts"),
            ("normal", 6.0, 2.5, "p8.ts"),
            ("low", 3.0, 0.5, "p8.ts"),
            ("stable", 12.0, 6.0, "s5.ts"),  # 6 s back from the last finished piece's end
        )
        for latency, delay, target, first in cases:
            with self.subTest(latency=latency):
                server = self.copies([copy], names + ["p8.ts", "p9.ts"])
                self.run_for(server, 0.5, live_delay=delay, target=target)
                self.assertEqual(requested_segments(server)[0], first)

    @staticmethod
    def site_copy(count, length, target, prefetch=2):
        """A live site's media playlist: `count` finished pieces of `length` seconds (s0.ts
        on) under a `target` s target duration, then `prefetch` pieces still being made
        (Kick: 4.167 s pieces, 6 s target duration; Twitch: 2 s pieces), and their names."""
        names = [f"s{number}.ts" for number in range(count)]
        made = [f"p{count + number}.ts" for number in range(prefetch)]
        text = f"#EXTM3U\n#EXT-X-TARGETDURATION:{target}\n#EXT-X-MEDIA-SEQUENCE:0\n"
        text += "".join(f"#EXTINF:{length},live\n{name}\n" for name in names)
        text += "".join(f"#EXT-X-PREFETCH:{name}\n" for name in made)
        return text.encode(), names + made

    def test_stable_on_a_prefetch_source_starts_about_six_seconds_back_and_targets_six(self):
        # 2026-11-25 (replaces ruling R4, which kept 3 target durations: 18 s back on Kick,
        # where the start Stable aims at is 6 s): the piece that begins Stable's 6 s target
        # before the pieces being made (the live edge is in the first of them, so at least
        # that far behind it), counted over the pieces' own lengths. The session picks its
        # start at 6 s from what that brings (`StablePrefetchTests`): there at once.
        cases = (  # (piece length, target duration, the first piece without prefetch, with)
            (4.167, 6, "s3.ts", "s6.ts"),  # Kick: 8.3 s before the first prefetch piece
            (2.0, 6, "s0.ts", "s5.ts"),  # Twitch: 6 s before it
        )
        for length, target, today, first in cases:
            with self.subTest(length=length):
                copy, pieces = self.site_copy(8, length, target)
                server = self.copies([copy], pieces)
                self.run_for(server, 0.5, live_delay=12.0, target=6.0)
                self.assertEqual(requested_segments(server)[0], first)
                plain = hls(0, pieces[:8], target=target).replace(
                    f"#EXTINF:{target},".encode(), f"#EXTINF:{length},".encode()
                )
                server = self.copies([plain], pieces[:8])
                self.run_for(server, 0.5, live_delay=12.0, target=6.0)
                self.assertEqual(requested_segments(server)[0], today)  # without: as today

    def test_a_resumed_stable_run_joins_its_earlier_one_as_far_back_as_today(self):  # 2.8
        # Stable's shallower start on a prefetch source is a fresh start's: a resumed run
        # still goes on after its earlier one's last piece as far back as the usual depth.
        copy, pieces = self.site_copy(8, 4.167, 6)
        for after, first in ((2, "s3.ts"), (0, "s6.ts")):
            with self.subTest(after=after):
                server = self.copies([copy], pieces)
                self.run_for(server, 0.5, live_delay=12.0, target=6.0, after_seq=after)
                self.assertEqual(requested_segments(server)[0], first)

    def test_a_resumed_run_joins_its_earlier_one_before_the_prefetch_pieces(self):  # 2.8
        # The pieces between the earlier run's last one and the first prefetch piece come
        # too: nothing is left out where they join (as far back as the usual depth).
        names = [f"s{number}.ts" for number in range(8)]
        copy = prefetching(0, names, ["p8.ts", "p9.ts"], target=2)
        for after, first in ((5, "s6.ts"), (8, "p9.ts"), (1, "p8.ts")):
            with self.subTest(after=after):
                server = self.copies([copy], names + ["p8.ts", "p9.ts"])
                self.run_for(server, 0.5, live_delay=6.0, target=2.5, after_seq=after)
                self.assertEqual(requested_segments(server)[0], first)

    def test_a_prefetch_piece_streams_its_bytes_as_they_come(self):
        # A piece still being made sends what it has, then the rest at real time: its first
        # bytes pass on at once, not once the piece is whole (four quarters, 0.3 s apart).
        quarter = len(self.video) // 188 // 4 * 188
        cuts = [0, quarter, 2 * quarter, 3 * quarter, len(self.video)]
        ended = []

        def trickle(request):
            for index in range(4):
                if index:
                    time.sleep(0.3)
                request.wfile.write(self.video[cuts[index] : cuts[index + 1]])
                request.wfile.flush()
            ended.append(time.monotonic())

        copy = prefetching(0, ["s0.ts"], ["p1.ts"], target=4)
        server = self.copies([copy], ["s0.ts"], {"/p1.ts": (200, {}, trickle)})
        stop = threading.Event()
        timer = threading.Timer(3.0, stop.set)  # a bound: nothing new comes after p1
        timer.start()
        source = open_source(server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=stop)
        first = None
        try:
            for item in source.chunks():
                if isinstance(item, bytes) and first is None:
                    first = time.monotonic()
                if ended:
                    break
        finally:
            source.close()
            timer.cancel()
        self.assertEqual(requested_segments(server), ["p1.ts"])
        self.assertIsNotNone(first)
        self.assertLess(first, ended[0] - 0.6)  # before the last two quarters were sent

    def test_a_prefetch_piece_relisted_finished_is_not_fetched_again(self):
        names = [f"s{number}.ts" for number in range(6)]
        copies = [
            prefetching(0, names, ["p6.ts", "p7.ts"], target=2),
            prefetching(1, names[1:] + ["p6.ts"], ["p7.ts", "p8.ts"], target=2),
            hls(2, names[2:] + ["p6.ts", "p7.ts", "p8.ts"], target=2, ended=True),
        ]
        server = self.copies(copies, names + ["p6.ts", "p7.ts", "p8.ts"])
        media(server.url + "/live.m3u8", live_delay=6.0)
        self.assertEqual(requested_segments(server), ["p6.ts", "p7.ts", "p8.ts"])

    def relisted(self, copies):
        """What a source makes of a live site's three `copies` (`made`: the third read after
        the first's two pieces being made, p3 and p4) in 2.5 s: its items, the pieces asked
        for and its log lines."""
        names = ["s0.ts", "s1.ts", "s2.ts", "r3.ts", "r4.ts"]
        server = self.made(copies, names + [f"p{k}.ts" for k in range(3, 7)])
        with source_logs() as logs:
            parts = self.run_for(server, 2.5)
        return parts, server, logs

    def test_a_prefetch_piece_relisted_under_another_name_is_ours(self):
        # 2026-11-25: Twitch lists a piece being made finished in its next copy under another
        # name (the same bytes), at the media sequence it was fetched at and dated as reckoned
        # for it. That name was never fetched, so the copy read as another encoding's: the
        # source waited 18 s of every ~22 s for ours. It is the piece already played, dated
        # within half a piece of its reckoning or, undated, in a copy listing pieces being made.
        for dates, shift in ((True, 0.0), (True, 0.8), (False, 0.0)):
            with self.subTest(dates=dates, shift=shift):
                copies = [
                    site_playlist(0, ["s0.ts", "s1.ts", "s2.ts"], ["p3.ts", "p4.ts"], dates=dates),
                    site_playlist(
                        1, ["s1.ts", "s2.ts", "r3.ts"], ["p4.ts", "p5.ts"], dates=dates, shift=shift
                    ),
                    site_playlist(
                        2, ["s2.ts", "r3.ts", "r4.ts"], ["p5.ts", "p6.ts"], dates=dates, shift=shift
                    ),
                ]
                parts, server, logs = self.relisted(copies)
                self.assertEqual([line for line in logs if "another encoding" in line], [])
                pieces = ["p3.ts", "p4.ts", "p5.ts", "p6.ts"]
                self.assertEqual(requested_segments(server), pieces)  # no r3 or r4, each once
                self.assertEqual([server.counts[f"/{name}"] for name in pieces], [1, 1, 1, 1])
                self.assertEqual(parts, joined(self.video, self.video, self.video, self.video))

    def test_another_encoding_is_still_caught_on_a_prefetch_source(self):
        # 2.6.1's two origin groups: a copy that lists, at the media sequence of the piece being
        # made fetched last, a name never seen dated more than half a piece from its reckoning
        # is another encoding's, as before; so is an undated one listing no piece being made.
        others = ["x2.ts", "x3.ts", "x4.ts"]
        for dates, other in (
            (True, site_playlist(2, others, ["x5.ts", "x6.ts"], shift=1.5)),
            (False, site_playlist(2, others, [], dates=False)),
        ):
            with self.subTest(dates=dates):
                ours = site_playlist(
                    0, ["s0.ts", "s1.ts", "s2.ts"], ["p3.ts", "p4.ts"], dates=dates
                )
                pieces = ["s0.ts", "s1.ts", "s2.ts", "p3.ts", "p4.ts"] + others + ["x5.ts", "x6.ts"]
                server = self.made([ours, other], pieces)
                with source_logs() as logs:
                    self.run_for(server, 2.0)
                waiting = "An HLS playlist copy numbers another encoding; waiting for ours."
                self.assertIn(waiting, logs)
                self.assertEqual(requested_segments(server), ["p3.ts", "p4.ts"])

    def test_another_group_of_a_prefetch_source_not_twitchs_is_caught_as_before(self):
        # Ruling R23: only Twitch relists a piece being made under another name. Another origin
        # group of any other site's source (Kick's prefetch lines here), listing pieces being
        # made of its own, is another encoding's as before Task 11: undated, or dated on the
        # same clock (within half a piece of the reckoning by construction).
        kick = {"length": 4.167, "tag": "#EXT-X-PREFETCH:"}
        others = ["x2.ts", "x3.ts", "x4.ts"]
        for dates in (False, True):
            with self.subTest(dates=dates):
                ours = site_playlist(
                    0, ["s0.ts", "s1.ts", "s2.ts"], ["p3.ts", "p4.ts"], dates=dates, **kick
                )
                other = site_playlist(2, others, ["x5.ts", "x6.ts"], dates=dates, **kick)
                pieces = ["s0.ts", "s1.ts", "s2.ts", "p3.ts", "p4.ts"] + others + ["x5.ts", "x6.ts"]
                server = self.made([ours, other], pieces)
                with source_logs() as logs:
                    self.run_for(server, 2.0)
                waiting = "An HLS playlist copy numbers another encoding; waiting for ours."
                self.assertIn(waiting, logs)
                self.assertEqual(requested_segments(server), ["p3.ts", "p4.ts"])

    def test_a_prefetch_piece_finished_under_its_own_name_plays_as_today(self):
        # Kick's shape: 4.167 s pieces, its own prefetch lines, a piece listed finished under
        # the name it was made under. Ours, as before: each piece once, nothing waited for.
        kick = {"length": 4.167, "tag": "#EXT-X-PREFETCH:"}
        copies = [
            site_playlist(0, ["s0.ts", "s1.ts", "s2.ts"], ["p3.ts", "p4.ts"], **kick),
            site_playlist(1, ["s1.ts", "s2.ts", "p3.ts"], ["p4.ts", "p5.ts"], **kick),
            site_playlist(2, ["s2.ts", "p3.ts", "p4.ts"], ["p5.ts", "p6.ts"], **kick),
        ]
        parts, server, logs = self.relisted(copies)
        self.assertEqual([line for line in logs if "another encoding" in line], [])
        self.assertEqual(requested_segments(server), ["p3.ts", "p4.ts", "p5.ts", "p6.ts"])
        self.assertEqual(parts, joined(self.video, self.video, self.video, self.video))

    def test_caught_up_comes_before_the_prefetch_fetches(self):
        # The start's backlog has landed once the finished pieces came: what follows comes as
        # it is made, and the session need not wait for it (under Stable too, which fetches
        # finished pieces first). The media still to come counts finished pieces only.
        names = [f"s{number}.ts" for number in range(8)]
        copy = prefetching(0, names, ["p8.ts", "p9.ts"], target=2)
        for delay, target, before in ((6.0, 2.5, []), (12.0, 6.0, names[5:])):
            with self.subTest(live_delay=delay):
                server = self.copies([copy], names + ["p8.ts", "p9.ts"])
                stop = threading.Event()
                source = open_source(
                    server.url + "/live.m3u8",
                    headers={},
                    verify_ssl=True,
                    stop=stop,
                    live_delay=delay,
                    target=target,
                )
                seen, rest = None, []
                try:
                    for item in source.chunks():
                        if isinstance(item, bytes) and source.segment_sequence is not None:
                            rest.append((source.segment_sequence, source.rest_s))
                        if isinstance(item, CaughtUp):
                            seen = list(requested_segments(server))
                            break
                finally:
                    source.close()
                self.assertEqual(seen, before)
                if before:  # s5..s7 (Stable's 6 s): 2 s each, nothing counted for p8 and p9
                    self.assertEqual(rest[0], (5, 4.0))

    def test_a_prefetch_piece_that_breaks_is_skipped_once(self):
        # Missing, or cut off after its first bytes: skipped with a marker, never asked again
        # (its sequence is passed once a later copy lists it finished), and the next one comes.
        names = [f"s{number}.ts" for number in range(6)]
        copies = [
            prefetching(0, names, ["p6.ts", "p7.ts"], target=2),
            prefetching(2, names[2:] + ["p6.ts", "p7.ts"], ["p8.ts", "p9.ts"], target=2),
            hls(4, names[4:] + ["p6.ts", "p7.ts", "p8.ts", "p9.ts"], target=2, ended=True),
        ]

        def cut(request):
            request.wfile.write(self.video[: 188 * 10])
            request.wfile.flush()

        broken = {
            "missing": (404, {}, b"Not found"),
            "cut": (200, {"Content-Length": str(len(self.video))}, cut),
        }
        for name, route in broken.items():
            with self.subTest(name):
                pieces = names + ["p6.ts", "p8.ts", "p9.ts"]
                server = self.copies(copies, pieces, {"/p7.ts": route})
                parts = media(server.url + "/live.m3u8", live_delay=6.0)
                self.assertEqual(requested_segments(server), ["p6.ts", "p7.ts", "p8.ts", "p9.ts"])
                self.assertEqual(server.counts["/p7.ts"], 1)
                self.assertEqual(sum(isinstance(part, Discontinuity) for part in parts), 1)
                self.assertIsInstance(parts[-2], Discontinuity)  # before p8 and p9
                self.assertEqual(plain(parts[-1]), plain(self.video * 2))

    def test_a_prefetch_source_reports_no_burst(self):
        # Its pieces come as they are made, not a target duration at a time: no burst, from
        # the first state on and after reloads; without prefetch pieces, as before.
        names = [f"s{number}.ts" for number in range(4)]
        copies = [
            prefetching(0, names, ["p4.ts", "p5.ts"], target=6),
            prefetching(1, names[1:] + ["p4.ts"], ["p5.ts", "p6.ts"], target=6),
        ]
        server = self.copies(copies, names + ["p4.ts", "p5.ts", "p6.ts"])
        source = open_source(
            server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
        )
        bursts = [(source.prefetching, source.burst_s)]
        try:
            for _item in source.chunks():
                bursts.append((source.prefetching, source.burst_s))
                if "p6.ts" in requested_segments(server):
                    break
        finally:
            source.close()
        self.assertGreaterEqual(server.counts["/live.m3u8"], 2)
        self.assertEqual(set(bursts), {(True, 0.0)})
        plain_server = self.copies([hls(0, names, target=6)], names)
        source = open_source(
            plain_server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
        )
        try:
            self.assertEqual((source.prefetching, source.burst_s), (False, 6.0))
        finally:
            source.close()

    def test_without_prefetch_the_depth_is_unchanged(self):
        # (test_live_hls_starts_far_enough_back_for_the_sessions_target's cases, pinned.)
        names = [f"s{number}.ts" for number in range(8)]
        cases = ((10, 2.5, 6.0, 5), (6, 2.5, 6.0, 5), (2, 6.0, 12.0, 2), (2, 1.0, 3.0, 5))
        for duration, target, delay, first in cases:
            with self.subTest(duration=duration, target=target):
                server = self.copies([hls(0, names, target=duration)], names)
                source = open_source(
                    server.url + "/live.m3u8",
                    headers={},
                    verify_ssl=True,
                    stop=threading.Event(),
                    live_delay=delay,
                    target=target,
                )
                try:
                    self.assertIs(source.prefetching, False)
                    next(item for item in source.chunks() if isinstance(item, bytes))
                finally:
                    source.close()
                self.assertEqual(requested_segments(server)[0], names[first])

    def test_a_reload_that_brings_no_new_piece_while_prefetching_comes_within_a_second(self):
        # A copy that lists nothing new is asked again a second later, not half a 6 s target
        # duration (3 s): the pieces stream as they are made, and the start holds ~3 s.
        names = [f"s{number}.ts" for number in range(4)]
        first = prefetching(0, names, ["p4.ts", "p5.ts"], target=6)
        newer = prefetching(1, names[1:] + ["p4.ts"], ["p5.ts", "p6.ts"], target=6)
        asked = []
        copies = [first, first, newer]

        def playlist(_request, count):
            asked.append(time.monotonic())
            return 200, {}, copies[min(count, len(copies)) - 1]

        routes = {f"/{name}": (200, {}, self.video) for name in names + ["p4.ts", "p5.ts", "p6.ts"]}
        routes["/live.m3u8"] = playlist
        server = self.server(routes)
        source = open_source(
            server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
        )
        try:
            for _item in source.chunks():
                if "p6.ts" in requested_segments(server):
                    break
        finally:
            source.close()
        self.assertEqual(requested_segments(server), ["p4.ts", "p5.ts", "p6.ts"])
        self.assertEqual(len(asked), 3)
        self.assertLess(asked[2] - asked[1], 1.5)  # the stale copy's reload: a second later

    def test_a_prefetch_piece_is_listed_as_the_piece_it_will_be(self):
        # Its length and date follow the last finished piece's; it has that one's key and map
        # (fMP4: it is remuxed whole, as any). Without a finished piece: the target duration
        # and no date. A finished playlist (ENDLIST) has no piece still being made.
        url = "https://cdn.example.test/live/media.m3u8"
        text = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:40\n"
            '#EXT-X-MAP:URI="init.mp4"\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            "#EXT-X-PROGRAM-DATE-TIME:2026-11-24T10:00:00.000Z\n"
            "#EXTINF:2.000,live\na.mp4\n#EXTINF:2.000,live\nb.mp4\n"
            "#EXT-X-TWITCH-PREFETCH:https://cdn.example.test/live/c.mp4\n"
            "#EXT-X-TWITCH-PREFETCH:https://cdn.example.test/live/d.mp4\n"
        )
        playlist, _heights = source_module._select_manifest(text.encode(), url)
        a, b, c, d = playlist.segments
        self.assertEqual([c.uri, d.uri], [f"https://cdn.example.test/live/{n}.mp4" for n in "cd"])
        self.assertEqual((c.duration, d.duration, c.title), (2.0, 2.0, "prefetch"))
        dates = [piece.program_date_time.isoformat() for piece in (c, d)]
        self.assertEqual(dates, ["2026-11-24T10:00:04+00:00", "2026-11-24T10:00:06+00:00"])
        self.assertEqual((c.init_section.uri, c.key.uri), ("init.mp4", "key.bin"))
        self.assertEqual((d.base_uri, d.media_sequence), (b.base_uri, 43))
        bare = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-PREFETCH:p0.ts\n"
        (piece,) = source_module._select_manifest(bare.encode(), url)[0].segments
        self.assertEqual((piece.duration, piece.program_date_time), (6.0, None))
        ended = prefetching(0, ["a.ts"], ["b.ts"], target=2, ended=True)
        self.assertEqual(len(source_module._select_manifest(ended, url)[0].segments), 1)

    def until(self, server, last, seconds=15.0, **options):
        """A live source on the server's playlist, read until the piece named `last` brings
        bytes (at most `seconds`): its items but `CaughtUp`."""
        stop = threading.Event()
        timer = threading.Timer(seconds, stop.set)
        timer.start()
        source = open_source(
            server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=stop, **options
        )
        items = []
        try:
            for item in source.chunks():
                if isinstance(item, CaughtUp):
                    continue
                items.append(item)
                if isinstance(item, bytes) and last in requested_segments(server):
                    break
        finally:
            source.close()
            timer.cancel()
        return items

    @staticmethod
    def markers(items):
        return [item for item in items if not isinstance(item, bytes)]

    def test_what_marks_a_piece_as_an_ad(self):
        # A date inside a range of the ad class or whose id says so (the end is not in it); a
        # range of another kind is none; a date or range without a zone is UTC's. A title
        # naming the ad server only in a playlist that carries the site's own tags: an IPTV
        # provider's piece may be titled anything.
        ranges = [
            ad_range(1, 2),
            ad_range(4, 1, ident="stitched-ad-2", kind="other"),
            ad_range(6, 1, ident="playlist-session-1", kind="twitch-session"),
            f'#EXT-X-DATERANGE:ID="stitched-ad-3",START-DATE="{stamp(7)[:-1]}",'
            f'END-DATE="{stamp(8)}"\n',
        ]
        pieces = [
            f"#EXT-X-PROGRAM-DATE-TIME:{stamp(k)[:-1] if k == 7 else stamp(k)}\n"
            f"#EXTINF:1.000,{'Amazon|1' if k == 9 else 'live'}\nk{k}.ts\n"
            for k in range(10)
        ]
        url = "https://cdn.example.test/live/media.m3u8"
        for tag, titled in (("#EXT-X-TWITCH-ELAPSED-SECS:0.000\n", True), ("", False)):
            with self.subTest(site=titled):
                text = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n" + tag + "".join(ranges + pieces)
                playlist = source_module._select_manifest(text.encode(), url)[0]
                self.assertIs(source_module._from_site(playlist), titled)
                marked = source_module._ad_ranges(playlist).values()
                found = [source_module._is_ad(piece, marked, titled) for piece in playlist.segments]
                expected = [False, True, True, False, True, False, False, True, False, titled]
                self.assertEqual(found, expected)

    def test_marked_ad_pieces_are_skipped_with_a_marker(self):
        # Three pieces of an ad break between live ones: never asked for, one discontinuity
        # where the channel goes on, and the break said as it begins and as it ends. Marked by
        # a date range of the ad class (the last of them dated in it only after the range
        # left the playlist), or by their titles.
        AdBreak = source_module.AdBreak
        ads = {"a2.ts", "a3.ts", "a4.ts"}
        marked = {"a2.ts": ad_range(2, 3)}
        amazon = {name: "Amazon|720239851" for name in ads}
        first = ["s0.ts", "s1.ts", "a2.ts", "a3.ts"]
        for how, ranges, titles in (("date range", marked, None), ("title", None, amazon)):
            with self.subTest(how):
                copies = [
                    dated(0, first, ads=ads, ranges=ranges, titles=titles),
                    dated(3, ["a3.ts", "a4.ts"], ads=ads, titles=titles),
                    dated(4, ["a4.ts", "s5.ts", "s6.ts"], ads=ads, titles=titles),
                ]
                server = self.copies(copies, ["s0.ts", "s1.ts", "s5.ts", "s6.ts"] + sorted(ads))
                with source_logs() as logs:
                    items = self.until(server, "s6.ts", live_delay=100.0)
                self.assertEqual(requested_segments(server), ["s0.ts", "s1.ts", "s5.ts", "s6.ts"])
                self.assertEqual(
                    self.markers(items), [AdBreak(True), AdBreak(False), Discontinuity()]
                )
                self.assertEqual([line for line in logs if "another encoding" in line], [])

    def test_a_providers_piece_titled_like_an_ad_plays(self):
        # Without Twitch's own tags a title is no ad marker: every piece is fetched, an IPTV
        # provider's and another site's (Kick's prefetch lines, a low-latency server's) alike.
        text = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,live\na.ts\n"
        text += "#EXTINF:4,Amazon|1\nb.ts\n#EXTINF:4,live\nc.ts\n"
        url = "https://cdn.example.test/live/media.m3u8"
        tails = (("provider", "#EXT-X-ENDLIST\n"), ("prefetch", "#EXT-X-PREFETCH:d.ts\n"))
        for how, tail in tails:
            with self.subTest(how):
                copy = (text + tail).encode()
                playlist = source_module._select_manifest(copy, url)[0]
                self.assertIs(source_module._from_site(playlist), False)
                server = self.copies([copy], ["a.ts", "b.ts", "c.ts", "d.ts"])
                parts = self.run_for(server, 0.5, live_delay=100.0)
                fetched = ["a.ts", "b.ts", "c.ts"] + (["d.ts"] if how == "prefetch" else [])
                self.assertEqual(requested_segments(server), fetched)
                self.assertEqual(self.markers(parts), [])

    def test_an_ad_range_lasts_at_most_the_longest_break(self):
        # A range's DURATION, PLANNED-DURATION or END-DATE is taken up to `_AD_MAX_S`: a huge
        # one neither marks the channel's pieces for days nor fails to read.
        start = DAY + timedelta(seconds=2)
        cases = (
            (ad_range(2, 30), 30.0),
            (ad_range(2, 1e300), source_module._AD_MAX_S),
            (ad_range(2, 30).replace("DURATION=", "PLANNED-DURATION="), 30.0),
            (
                f'#EXT-X-DATERANGE:ID="stitched-ad-9",START-DATE="{stamp(2)}",'
                f'END-DATE="{stamp(86400 * 365)}"',
                source_module._AD_MAX_S,
            ),
        )
        for line, seconds in cases:
            with self.subTest(line=line[:60]):
                _ident, begins, ends = source_module._ad_range(line.strip())
                self.assertEqual((begins, ends), (start, start + timedelta(seconds=seconds)))

    def test_a_break_that_never_ends_counts_as_frozen_after_a_while(self):
        # A playlist that lists only ad pieces, the same copy, for longer than any break
        # (`_AD_MAX_S`: here 1.5 s; a channel gone off mid-break, a stuck edge) is frozen
        # again: the source fails as for any copy that stands still (here 1 s).
        ads = {"a0.ts", "a1.ts", "a2.ts"}
        copy = dated(0, sorted(ads), ads=ads, ranges={"a0.ts": ad_range(0, 3)})
        server = self.copies([copy], sorted(ads))
        stop = threading.Event()
        timer = threading.Timer(10.0, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        source = open_source(server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=stop)
        started = time.monotonic()
        with source_logs() as logs:
            with patch.multiple(source_module, _AD_MAX_S=1.5, _FROZEN_TIMEOUT=1.0):
                with self.assertRaises(SourceError) as raised:
                    drain(source)
        self.assertEqual(raised.exception.kind, "timeout")
        self.assertGreaterEqual(time.monotonic() - started, 1.5)
        self.assertEqual(requested_segments(server), [])
        self.assertEqual(len([line for line in logs if "only ad pieces" in line]), 1)

    def test_prefetch_pieces_inside_a_break_are_skipped(self):
        # Pieces still being made, dated (from the last finished piece's date on) inside an ad
        # break's range, are never asked for: neither as they are made nor once finished. The
        # range is listed after the finished pieces, before the prefetch lines.
        AdBreak = source_module.AdBreak
        ads = {"p3.ts", "p4.ts"}
        finished = ["s0.ts", "s1.ts", "s2.ts"]
        copies = [
            dated(0, finished, ads=ads, ranges={None: ad_range(3, 2)}, prefetch=sorted(ads)),
            dated(
                1,
                finished[1:] + sorted(ads),
                ads=ads,
                ranges={"p3.ts": ad_range(3, 2)},
                prefetch=["p5.ts", "p6.ts"],
            ),
        ]
        server = self.copies(copies, finished + sorted(ads) + ["p5.ts", "p6.ts"])
        items = self.until(server, "p6.ts", live_delay=100.0)
        self.assertEqual(requested_segments(server), finished + ["p5.ts", "p6.ts"])
        self.assertEqual(self.markers(items), [AdBreak(True), AdBreak(False), Discontinuity()])

    def test_a_pre_roll_at_tune_in_waits(self):
        # A tune into an ad break: the playlist lists only its pieces, the same copy for longer
        # than a frozen playlist is given (60 s; here 1 s), and the source waits for the
        # channel without failing; its first piece plays once it comes (no marker before the
        # first bytes: nothing played before them).
        AdBreak = source_module.AdBreak
        ads = {"a0.ts", "a1.ts", "a2.ts"}
        pre = dated(0, sorted(ads), ads=ads, ranges={"a0.ts": ad_range(0, 3)})
        copies = [
            pre,
            pre,
            pre,
            dated(1, ["a1.ts", "a2.ts", "s3.ts"], ads=ads),
            dated(2, ["a2.ts", "s3.ts", "s4.ts"], ads=ads),
        ]
        server = self.copies(copies, sorted(ads) + ["s3.ts", "s4.ts"])
        with patch.object(source_module, "_FROZEN_TIMEOUT", 1.0):
            items = self.until(server, "s4.ts")
        self.assertEqual(requested_segments(server), ["s3.ts", "s4.ts"])
        self.assertEqual(self.markers(items), [AdBreak(True), AdBreak(False)])
        self.assertGreaterEqual(server.counts["/live.m3u8"], 4)

    def test_a_copy_without_prefetch_pieces_keeps_the_prefetch_pace(self):
        # One copy that lists no piece being made (the next is about to start) neither slows
        # the reloads (Kick's 6 s target duration: 3 s) nor reports a burst (the session would
        # take its target duration for one at once): the source stays prefetching for about
        # two pieces after the last copy that listed some.
        names = [f"s{number}.ts" for number in range(4)]
        copies = [
            prefetching(0, names, ["p4.ts", "p5.ts"], target=6),
            prefetching(1, names[1:] + ["p4.ts", "p5.ts"], [], target=6),
            prefetching(2, names[2:] + ["p4.ts", "p5.ts"], ["p6.ts"], target=6),
        ]
        asked, seen, opened, bursts = [], [], [], []
        done = threading.Event()

        def playlist(_request, count):
            asked.append(time.monotonic())
            if count == 3:  # what the copy without them left
                seen.append((opened[0].prefetching, opened[0].burst_s))
            return 200, {}, copies[min(count, len(copies)) - 1]

        def sample():  # the burst the helper's state reports, throughout
            while not done.wait(0.05):
                if opened:
                    bursts.append((time.monotonic(), opened[0].burst_s))

        routes = {f"/{name}": (200, {}, self.video) for name in names + ["p4.ts", "p5.ts", "p6.ts"]}
        routes["/live.m3u8"] = playlist
        server = self.server(routes)
        source = open_source(
            server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
        )
        opened.append(source)
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        try:
            for _item in source.chunks():
                if "p6.ts" in requested_segments(server):
                    break
        finally:
            done.set()
            sampler.join(1.0)
            source.close()
        self.assertEqual(requested_segments(server), ["p4.ts", "p5.ts", "p6.ts"])
        self.assertEqual(seen, [(True, 0.0)])
        self.assertLess(asked[2] - asked[1], 1.5)
        # Neither the burst reported nor what stay-live's lag filter keeps of it (a kept one
        # holds the target up for the rest of the tune) ever took the target duration.
        self.assertGreater(len(bursts), 10)
        self.assertEqual({burst for _at, burst in bursts}, {0.0})
        lags = LagFilter()
        for at, burst in bursts:
            lags.add(at, at, burst, 0.0)
        self.assertEqual((lags.kept, lags.burst), (0.0, 0.0))

    def test_copies_that_stop_listing_prefetch_pieces_end_the_prefetching(self):
        # About two pieces (here 1 s each) after the last copy that listed one, the playlist
        # is read as any again: its target duration a piece at a time.
        copies = [prefetching(0, ["s0.ts"], ["p1.ts"]), prefetching(1, ["p1.ts"], [])]
        server = self.copies(copies, ["s0.ts", "p1.ts"])
        stop = threading.Event()
        source = open_source(server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=stop)
        seen = {}

        def look(when):
            seen[when] = (source.prefetching, source.burst_s)

        timers = [
            threading.Timer(1.5, look, ["held"]),
            threading.Timer(3.6, look, ["lapsed"]),
            threading.Timer(3.8, stop.set),
        ]
        for timer in timers:
            timer.start()
            self.addCleanup(timer.cancel)
        drain(source)
        self.assertEqual(seen, {"held": (True, 0.0), "lapsed": (False, 1.0)})

    def test_an_ad_break_keeps_a_prefetch_source_prefetching(self):
        # A break's copies list no piece being made (its pieces are ads): longer than the hold
        # above, the source still reports no burst, so neither the session's stall bound nor
        # its target takes the target duration through the break.
        ads = {"a2.ts", "a3.ts", "a4.ts", "a5.ts"}
        marked = {"a2.ts": ad_range(2, 4)}
        copies = [
            dated(0, ["s0.ts"], prefetch=["p1.ts"]),
            dated(1, ["p1.ts", "a2.ts"], ads=ads, ranges=marked),
            dated(2, ["a2.ts", "a3.ts"], ads=ads, ranges=marked),
            dated(3, ["a3.ts", "a4.ts"], ads=ads),
            dated(4, ["a4.ts", "a5.ts"], ads=ads),
            dated(5, ["a5.ts", "s6.ts"], ads=ads, prefetch=["p7.ts"]),
        ]
        server = self.copies(copies, ["s0.ts", "p1.ts", "s6.ts", "p7.ts"] + sorted(ads))
        stop = threading.Event()
        timer = threading.Timer(15.0, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        source = open_source(server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=stop)
        seen = [(source.prefetching, source.burst_s)]
        try:
            for item in source.chunks():
                seen.append((source.prefetching, source.burst_s))
                if isinstance(item, bytes) and "p7.ts" in requested_segments(server):
                    break
        finally:
            source.close()
        self.assertEqual(requested_segments(server), ["p1.ts", "s6.ts", "p7.ts"])
        self.assertGreaterEqual(server.counts["/live.m3u8"], 6)
        self.assertEqual(set(seen), {(True, 0.0)})

    def test_a_discontinuity_before_a_prefetch_piece_is_marked(self):
        # A discontinuity written before a prefetch line is that piece's (the m3u8 parser
        # would keep it for a finished piece that never comes): the source marks it there.
        text = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXTINF:2.000,live\ns0.ts\n#EXT-X-DISCONTINUITY\n"
            "#EXT-X-PREFETCH:p1.ts\n#EXT-X-PREFETCH:p2.ts\n"
        ).encode()
        url = "https://cdn.example.test/live/media.m3u8"
        pieces = source_module._select_manifest(text, url)[0].segments
        self.assertEqual([piece.discontinuity for piece in pieces], [False, True, False])
        server = self.copies([text], ["s0.ts", "p1.ts", "p2.ts"])
        parts = self.run_for(server, 0.5, live_delay=100.0)
        self.assertEqual(parts, joined(self.video, Discontinuity(), self.video, self.video))

    def test_twitch_pieces_being_made_after_a_cut_from_the_channel_are_an_ad_breaks(self):
        # Final review I2: a Twitch copy lists the channel's pieces, a discontinuity, and two
        # pieces still being made before any range dates them (their dates are only reckoned):
        # the break's first pieces. Never asked for, and the break is said once, not twice.
        # Once the break's pieces are listed finished, the pieces made after its end play.
        AdBreak = source_module.AdBreak
        ads = {"p3.ts", "p4.ts"}
        finished = ["s0.ts", "s1.ts", "s2.ts"]
        copies = [
            dated(0, finished, ads=ads, prefetch=sorted(ads)),  # no range yet
            dated(
                1,
                finished[1:] + sorted(ads),
                ads=ads,
                ranges={"p3.ts": ad_range(3, 2)},
                prefetch=["p5.ts", "p6.ts"],  # after a cut back to the channel: theirs
            ),
        ]
        url = "https://cdn.example.test/live/media.m3u8"
        first = source_module._select_manifest(copies[0], url)[0]
        self.assertEqual([piece.discontinuity for piece in first.segments][-2:], [True, False])
        server = self.copies(copies, finished + sorted(ads) + ["p5.ts", "p6.ts"])
        items = self.until(server, "p6.ts", live_delay=100.0)
        self.assertEqual(requested_segments(server), finished + ["p5.ts", "p6.ts"])
        self.assertEqual(self.markers(items), [AdBreak(True), AdBreak(False), Discontinuity()])

    def test_twitch_ad_pieces_relisted_under_other_names_are_the_ones_skipped(self):
        # I2's break, its pieces being made listed finished in the next copy under other names
        # (2026-11-25: as Twitch relists the channel's own): the pieces already skipped, not
        # another encoding's. The break is said once, and the pieces made after it play.
        AdBreak = source_module.AdBreak
        ads = {"p3.ts", "p4.ts", "r3.ts", "r4.ts"}
        finished = ["s0.ts", "s1.ts", "s2.ts"]
        copies = [
            dated(0, finished, ads=ads, prefetch=["p3.ts", "p4.ts"]),
            dated(
                1,
                finished[1:] + ["r3.ts", "r4.ts"],
                ads=ads,
                ranges={"r3.ts": ad_range(3, 2)},
                prefetch=["p5.ts", "p6.ts"],
            ),
        ]
        server = self.copies(copies, finished + sorted(ads) + ["p5.ts", "p6.ts"])
        with source_logs() as logs:
            items = self.until(server, "p6.ts", seconds=3.0, live_delay=100.0)
        self.assertEqual([line for line in logs if "another encoding" in line], [])
        self.assertEqual(requested_segments(server), finished + ["p5.ts", "p6.ts"])
        self.assertEqual(self.markers(items), [AdBreak(True), AdBreak(False), Discontinuity()])

    def test_another_sites_pieces_being_made_after_a_cut_play(self):
        # Without Twitch's own tags (Kick's prefetch lines, a low-latency server's), a cut
        # before the pieces being made is the stream's own: they play, marked.
        copy = prefetching(0, ["s0.ts", "s1.ts"], [], target=1).decode()
        copy += "#EXT-X-DISCONTINUITY\n#EXT-X-PREFETCH:p2.ts\n#EXT-X-PREFETCH:p3.ts\n"
        server = self.copies([copy.encode()], ["s0.ts", "s1.ts", "p2.ts", "p3.ts"])
        parts = self.run_for(server, 0.5, live_delay=100.0)
        self.assertEqual(requested_segments(server), ["s0.ts", "s1.ts", "p2.ts", "p3.ts"])
        self.assertEqual(self.markers(parts), [Discontinuity()])

    def test_a_twitch_discontinuity_between_two_of_the_channels_pieces_is_none(self):
        # Final review M3: Twitch writes some between its own live pieces, where the stream
        # has no break; around its ad breaks, and in any other playlist, each is one.
        body = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n{tag}"
            "#EXTINF:1.000,live\ns0.ts\n#EXT-X-DISCONTINUITY\n#EXTINF:1.000,live\ns1.ts\n"
            "#EXTINF:1.000,live\ns2.ts\n"
        )
        for tag, marked in (("#EXT-X-TWITCH-ELAPSED-SECS:0.000\n", False), ("", True)):
            with self.subTest(twitch=not marked):
                server = self.copies([body.format(tag=tag).encode()], ["s0.ts", "s1.ts", "s2.ts"])
                parts = self.run_for(server, 0.5, live_delay=100.0)
                self.assertEqual(requested_segments(server), ["s0.ts", "s1.ts", "s2.ts"])
                self.assertEqual(self.markers(parts), [Discontinuity()] if marked else [])


class FragmentedMp4Tests(SourceTestCase):
    """HLS of fragmented MP4 (CMAF): each segment remuxed to TS (`services.live.fmp4`), and a
    separate fMP4 audio rendition remuxed and woven in. The media are tests/fixtures/fmp4's
    (`tests/test_fmp4.py`)."""

    @staticmethod
    def routes(folder):
        """Every file of a fixture folder, served from the root."""
        root = FIXTURES / folder
        return {
            "/" + path.relative_to(root).as_posix(): (200, {}, path.read_bytes())
            for path in root.rglob("*")
            if path.is_file()
        }

    @staticmethod
    def remuxed(folder, init, count, pcr=True):
        """The first `count` segments of a fixture rendition, each remuxed."""
        remuxer = Fmp4Remuxer(pcr=pcr)
        remuxer.init((FIXTURES / folder / init).read_bytes())
        return [
            remuxer.remux((FIXTURES / folder / f"seg{k}.m4s").read_bytes()) for k in range(count)
        ]

    def test_it_says_its_segments_are_fragmented_mp4(self):
        # The statistics over the picture name the source (2.3).
        server = self.server(self.routes("avc-aac"))
        source = open_source(
            server.url + "/index.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
        )
        self.assertFalse(source.fmp4)  # nothing read yet
        drain(source)
        self.assertTrue(source.fmp4)

    def test_a_fragmented_mp4_variant_plays_remuxed_to_ts(self):
        server = self.server(self.routes("avc-aac"))
        (out,) = media(server.url + "/index.m3u8")
        self.assertEqual(plain(out), plain(b"".join(self.remuxed("avc-aac", "init.mp4", 3))))
        self.assertEqual(server.counts["/init.mp4"], 1)  # read once for all its segments
        self.assertEqual(len(frames(out)[0x100]), 150)

    def woven_audio(self, out, count):
        """The audio PTS woven into `out`, and those of the split fixture's audio rendition
        (its `count` segments remuxed) that its video's time span takes: from a second before
        its first picture (VLC still plays those) to a frame before its last picture's end (a
        frame across a segment's end goes with the next one)."""
        found = frames(out)
        video = sorted(f.pts for f in found[0x100])
        start, end = video[0] - 90000, video[-1] + 3600 - 3840  # a frame: 2048 at 48 kHz
        rendition = [
            f.pts
            for ts in self.remuxed("split/1", "init_1.mp4", count, pcr=False)
            for f in frames(ts)[0x101]
        ]
        return [f.pts for f in found[0x101]], [pts for pts in rendition if start <= pts < end]

    def test_a_separate_fmp4_audio_rendition_is_remuxed_and_woven_in(self):
        server = self.server(self.routes("split"))
        (out,) = media(server.url + "/master.m3u8")
        self.assertTrue(ts_fixtures_has_audio(out))
        found = frames(out)
        video = [
            f.payload for ts in self.remuxed("split/0", "init_0.mp4", 3) for f in frames(ts)[0x100]
        ]
        self.assertEqual([f.payload for f in found[0x100]], video)
        woven, rendition = self.woven_audio(out, 4)
        self.assertEqual(woven, rendition)
        self.assertTrue(all(f.pcr is None for f in found[0x101]))  # the video's PCR alone
        self.assertEqual((server.counts["/0/init_0.mp4"], server.counts["/1/init_1.mp4"]), (1, 1))
        self.assertEqual([server.counts[f"/1/seg{k}.m4s"] for k in range(3)], [1, 1, 1])

    def test_an_audio_rendition_numbered_unlike_its_video_is_matched_by_time(self):
        # One packager numbers its audio segments its own way (and cuts them at other
        # durations): matched by media sequence number, its channels played silent.
        routes = self.routes("split")
        playlist = routes["/1/index.m3u8"][2].replace(
            b"#EXT-X-MEDIA-SEQUENCE:0", b"#EXT-X-MEDIA-SEQUENCE:500"
        )
        routes["/1/index.m3u8"] = (200, {}, playlist)
        server = self.server(routes)
        (out,) = media(server.url + "/master.m3u8")
        woven, rendition = self.woven_audio(out, 4)
        self.assertEqual(woven, rendition)
        self.assertGreater(len(woven), 100)

    def test_a_damaged_segment_is_skipped_with_a_marker(self):
        # A field past the data (here a tfdt with nothing in it) was an IndexError
        # that ended the session; it is a damaged segment, skipped as any other.
        tfhd = full(b"tfhd", 0, 0x20000, struct.pack(">I", 1))
        damaged = box(b"moof", box(b"traf", tfhd, box(b"tfdt")))
        routes = self.routes("avc-aac")
        routes["/seg1.m4s"] = (200, {}, damaged)
        server = self.server(routes)
        first, marker, last = media(server.url + "/index.m3u8")
        self.assertIsInstance(marker, Discontinuity)
        segments = self.remuxed("avc-aac", "init.mp4", 3)
        self.assertEqual(len(frames(first)[0x100]), len(frames(segments[0])[0x100]))
        self.assertEqual(len(frames(last)[0x100]), len(frames(segments[2])[0x100]))

    def test_one_sided_edit_lists_are_balanced(self):
        def remuxer(media):
            edits = elst((0, media)) if media is not None else b""
            made = Fmp4Remuxer()
            made.init(movie(trak(1, b"vide", 90000, visual(), edits=edits)))
            return made

        for video, audio, kept in ((180000, None, 0.0), (7200, None, 0.08), (180000, 90000, 2.0)):
            with self.subTest(video=video, audio=audio):
                pair = remuxer(video), remuxer(audio)
                source_module._balance_edits(*pair)
                self.assertEqual(pair[0].edit_media_s, kept)

    def test_an_init_that_cannot_be_remuxed_leaves_the_channel_unsupported(self):
        encrypted = movie(trak(1, b"vide", 90000, visual(b"encv")))
        other = movie(trak(1, b"vide", 90000, visual(b"av01", box(b"av1C", b"\x81\x00\x0c\x00"))))
        for init, words in ((encrypted, "encrypted"), (other, "unsupported codec")):
            with self.subTest(words):
                server = self.server(
                    {
                        "/index.m3u8": (
                            200,
                            {},
                            leaf("a.m4s", extra='#EXT-X-MAP:URI="init.mp4"\n'),
                        ),
                        "/init.mp4": (200, {}, init),
                        "/a.m4s": (200, {}, b"\x00" * 100),
                    }
                )
                with self.assertRaises(SourceError) as raised:
                    media(server.url + "/index.m3u8")
                self.assertEqual(raised.exception.kind, "unsupported")
                self.assertIn(words, str(raised.exception))
                self.assertEqual(server.counts["/a.m4s"], 0)

    def test_fmp4_audio_whose_init_cannot_be_remuxed_leaves_the_channel_unsupported(self):
        master = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100000,AUDIO="a"
video.m3u8
"""
        opus = movie(trak(2, b"soun", 48000, sound(b"Opus", box(b"dOps", bytes(11)))))
        server = self.server(
            {
                "/master.m3u8": (200, {}, master),
                "/video.m3u8": (200, {}, leaf("video.ts")),
                "/audio.m3u8": (200, {}, leaf("a1.m4s", extra='#EXT-X-MAP:URI="init.mp4"\n')),
                "/init.mp4": (200, {}, opus),
                "/video.ts": (200, {}, ts_fixtures.make_stream(4, audio=False)),
            }
        )
        with self.assertRaises(SourceError) as raised:
            media(server.url + "/master.m3u8")
        self.assertEqual(raised.exception.kind, "unsupported")
        self.assertIn("separate", str(raised.exception))
        self.assertEqual((server.counts["/audio.m3u8"], server.counts["/a1.m4s"]), (1, 0))

    def test_a_new_init_section_is_read_where_the_playlist_names_it(self):
        routes = {
            "/aac.mp4": (200, {}, (FIXTURES / "avc-aac" / "init.mp4").read_bytes()),
            "/aac.m4s": (200, {}, (FIXTURES / "avc-aac" / "seg0.m4s").read_bytes()),
            "/ac3.mp4": (200, {}, (FIXTURES / "avc-ac3" / "init.mp4").read_bytes()),
            "/ac3.m4s": (200, {}, (FIXTURES / "avc-ac3" / "seg0.m4s").read_bytes()),
            "/index.m3u8": (
                200,
                {},
                b"#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:2\n"
                b'#EXT-X-MAP:URI="aac.mp4"\n#EXTINF:2,\naac.m4s\n#EXTINF:2,\naac.m4s\n'
                b'#EXT-X-DISCONTINUITY\n#EXT-X-MAP:URI="ac3.mp4"\n#EXTINF:2,\nac3.m4s\n'
                b"#EXT-X-ENDLIST\n",
            ),
        }
        server = self.server(routes)
        first, marker, second = media(server.url + "/index.m3u8")
        self.assertIsInstance(marker, Discontinuity)
        self.assertEqual(frames(first)[0x101][0].payload[:2], b"\xff\xf1")  # AAC in ADTS
        self.assertEqual(frames(second)[0x101][0].payload[:2], b"\x0b\x77")  # AC-3
        self.assertEqual((server.counts["/aac.mp4"], server.counts["/ac3.mp4"]), (1, 1))

    # ---- a live site's fMP4 piece being made: passed on fragment by fragment ------------------

    @staticmethod
    def site_routes(finished, made, bodies, *, tag="#EXT-X-TWITCH-PREFETCH:"):
        """A live site's fMP4 playlist (Twitch's shape: an init section, 2 s pieces; another
        site's with Kick's `tag`) of `finished` pieces, then `made` (still being made), and
        the pieces' `bodies` by name."""
        playlist = site_playlist(0, finished, made, tag=tag).replace(
            b"#EXT-X-MEDIA-SEQUENCE:0\n", b'#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-MAP:URI="init.mp4"\n'
        )
        routes = {"/live.m3u8": (200, {}, playlist), "/init.mp4": (200, {}, SITE_INIT)}
        routes.update({f"/{name}": (200, {}, body) for name, body in bodies.items()})
        return routes

    def trickled(self, *, prefetch, tag="#EXT-X-TWITCH-PREFETCH:"):
        """What a source passes on of a live site's fMP4 piece (`site_routes`, `site_piece`'s
        four moof/mdat pairs), its body sent a pair every 0.3 s, still being made (`prefetch`,
        the playlist's second piece) or finished (its only one): each TS block with when it
        came, when the body ended, the piece's TS remuxed whole, and the server."""
        parts = site_piece(900000)
        ended = []

        def trickle(request):
            for index, part in enumerate(parts):
                if index:
                    time.sleep(0.3)
                request.wfile.write(part)
                request.wfile.flush()
            ended.append(time.monotonic())

        finished, made = (["s0.mp4"], ["p1.mp4"]) if prefetch else (["p1.mp4"], [])
        bodies = {"s0.mp4": b"".join(site_piece(720000)), "p1.mp4": trickle}
        server = self.server(self.site_routes(finished, made, bodies, tag=tag))
        stop = threading.Event()
        timer = threading.Timer(4.0, stop.set)  # a bound: nothing new comes after p1
        timer.start()
        source = open_source(server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=stop)
        blocks = []
        try:
            for item in source.chunks():
                if isinstance(item, bytes):
                    blocks.append((time.monotonic(), item))
                if isinstance(item, CaughtUp) and blocks:  # the piece's batch is done
                    break
        finally:
            source.close()
            timer.cancel()
        remuxer = Fmp4Remuxer()
        remuxer.init(SITE_INIT)
        return blocks, ended, remuxer.remux(b"".join(parts)), server

    def test_an_fmp4_prefetch_piece_yields_ts_before_its_body_ends(self):
        # Twitch's re-encoded variants send fMP4, a 2 s piece being made in tenths of a
        # second: each moof and its mdat pass on as they come, as a TS piece's bytes do,
        # not once the piece is whole.
        blocks, ended, whole, server = self.trickled(prefetch=True)
        self.assertEqual((server.counts["/p1.mp4"], server.counts["/s0.mp4"]), (1, 0))
        self.assertTrue(blocks and ended)
        self.assertLess(blocks[0][0], ended[0] - 0.6)  # before the last two pairs were sent
        self.assertGreaterEqual(len(blocks), 4)  # a block for each pair as it came
        self.assertEqual(plain(b"".join(block for _, block in blocks)), plain(whole))

    def test_a_finished_fmp4_piece_still_joins_as_today(self):
        # A finished piece comes whole at once from the site (in well under 0.1 s): it is
        # remuxed whole, as any fMP4 segment.
        blocks, ended, whole, server = self.trickled(prefetch=False)
        self.assertEqual(server.counts["/p1.mp4"], 1)
        self.assertEqual(len(blocks), 1)
        self.assertGreaterEqual(blocks[0][0], ended[0])
        self.assertEqual(plain(blocks[0][1]), plain(whole))

    def test_another_sites_fmp4_piece_being_made_is_remuxed_whole_as_before(self):
        # Ruling R29: only Twitch's (its own tags), whose pieces were checked against the
        # first moof's DTS shift; a low-latency server's (Kick's tag) waits for its piece.
        blocks, ended, whole, server = self.trickled(prefetch=True, tag="#EXT-X-PREFETCH:")
        self.assertEqual((server.counts["/p1.mp4"], server.counts["/s0.mp4"]), (1, 0))
        self.assertEqual(len(blocks), 1)
        self.assertGreaterEqual(blocks[0][0], ended[0])
        self.assertEqual(plain(blocks[0][1]), plain(whole))

    def test_a_twitch_piece_cut_off_plays_what_came_and_only_nothing_is_unsupported(self):
        # A first piece being made whose last box is cut off while its body ends cleanly: the
        # pairs that came play, then the usual skip marker and the next piece, as after a
        # broken body; the channel is unsupported only when nothing of it could be read.
        first, second = b"".join(site_piece(900000)), b"".join(site_piece(936000))
        remuxer = Fmp4Remuxer()
        remuxer.init(SITE_INIT)
        remuxer.begin()
        came = remuxer.feed(first[:-5])  # three of its four pairs
        remuxer.begin()
        after = remuxer.feed(second) + remuxer.end()
        bodies = {"s0.mp4": first, "p1.mp4": first[:-5], "p2.mp4": second}
        server = self.server(self.site_routes(["s0.mp4"], ["p1.mp4", "p2.mp4"], bodies))
        stop = threading.Event()
        timer = threading.Timer(1.5, stop.set)  # before its next reload
        timer.start()
        try:
            with source_logs() as logs:
                parts = media(server.url + "/live.m3u8", stop=stop)
        finally:
            timer.cancel()
        self.assertEqual(len(parts), 3)
        self.assertEqual(plain(parts[0]), plain(came))
        self.assertIsInstance(parts[1], Discontinuity)
        self.assertEqual(plain(parts[2]), plain(after))
        self.assertEqual([server.counts[f"/p{k}.mp4"] for k in (1, 2)], [1, 1])
        self.assertIn("Skipped an HLS segment (unsupported ).", logs)
        bodies["p1.mp4"] = first[:100]  # its emsg and the start of its first moof
        server = self.server(self.site_routes(["s0.mp4"], ["p1.mp4", "p2.mp4"], bodies))
        parts = []
        with self.assertRaises(SourceError) as raised:
            drain_into(
                open_source(
                    server.url + "/live.m3u8", headers={}, verify_ssl=True, stop=threading.Event()
                ),
                parts,
            )
        self.assertEqual(raised.exception.kind, "unsupported")
        self.assertIn("damaged", str(raised.exception))
        self.assertEqual(parts, [])
        self.assertEqual(server.counts["/p2.mp4"], 0)


class SlicedTlsSocketTests(unittest.TestCase):
    def test_only_a_slice_timeout_is_retried(self):
        # The 0.25 s slice's own timeout has no errno and is retried until the deadline. A
        # Winsock WSAETIMEDOUT (errno 10060) is sticky: retrying it would spin until then.
        sliced = interruptible_http._sliced_tls_socket(interruptible_http._Interrupter())
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
        patcher = patch.object(interruptible_http, "_INTERRUPT_OWNERS", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Nothing outside the reader may touch a TLS socket: on Windows OpenSSL keeps
        # using its raw handle value, which a new socket may already own.
        self.aborted = []
        abort = interruptible_http._abort

        def spy(sock):
            self.aborted.append(type(sock))
            abort(sock)

        spier = patch.object(interruptible_http, "_abort", spy)
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
        adopt = interruptible_http._Interrupter.adopt

        def record(interrupter, sock):
            adopted.append(sock)  # a strong reference: the owner set itself is weak
            adopt(interrupter, sock)

        with self.tls_context() as context, insecure():
            with patch.object(interruptible_http._Interrupter, "adopt", record):
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


class NetworkDetailTests(unittest.TestCase):  # 2.8.1: how a connection failed, in the log
    def failure_of(self, url):
        try:
            requests.get(url, timeout=5)
        except requests.exceptions.RequestException as error:
            return source_module._translate(error)
        self.fail("the request did not fail")

    def test_a_refused_connection_says_so(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()  # nothing listens there now
        failure = self.failure_of(f"http://127.0.0.1:{port}/channel")
        self.assertEqual(
            (failure.kind, str(failure)),
            ("network", "The channel's server refused the connection."),
        )

    def test_a_server_that_closes_without_answering_says_so(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def close_one():
            connection, _address = listener.accept()
            connection.recv(4096)  # the request, then no answer at all
            connection.close()

        threading.Thread(target=close_one, daemon=True).start()
        failure = self.failure_of(f"http://127.0.0.1:{listener.getsockname()[1]}/channel")
        self.assertEqual(
            str(failure), "The channel's server closed the connection without answering."
        )

    def test_a_connection_broken_off_says_so(self):
        from urllib3.exceptions import ProtocolError

        broken = ProtocolError("Connection aborted.", ConnectionResetError(54, "reset by peer"))
        failure = source_module._translate(requests.exceptions.ConnectionError(broken))
        self.assertEqual(str(failure), "The channel's server broke the connection off.")

    def test_another_failure_keeps_the_general_text_and_never_its_own(self):
        error = requests.exceptions.ConnectionError("http://secret.test/ failed")
        failure = source_module._translate(error)
        self.assertEqual(str(failure), "The channel could not be reached over HTTP.")


if __name__ == "__main__":
    unittest.main()
