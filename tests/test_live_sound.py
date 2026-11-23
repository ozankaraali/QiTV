"""A radio station's raw sound through the time-shift helper (2.14): `open_source(radio=True)`
reads an Icecast or SHOUTcast body of MPEG audio or ADTS AAC as one program's TS, the station's
inline metadata out of it and its songs as `Title` markers, its clock running on across
reconnects; formats it cannot read are refused at once, and a channel's open is as before. The
helper's sessions carry the songs in their state, and a station's light connect burst is no
recording. Every server is local; the sound is synthetic (tests/radio_fixtures.py)."""

from contextlib import contextmanager
import math
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from http_fixtures import LocalSource, leaf
from radio_fixtures import (
    adts_frame,
    adts_stream,
    id3_tag,
    metadata_block,
    mp3_frame,
    mp3_stream,
    pes_units,
    stream_types,
    with_icy,
)
from test_live_worker import RAM, InProcessHelper
import ts_fixtures

from services.live import source as source_module
from services.live.raw_audio import AUDIO_PID, PMT_PID
from services.live.source import (
    Discontinuity,
    IcyTsSource,
    Reconnecting,
    SoundSource,
    SourceError,
    Title,
    TsSource,
    open_source,
)
from services.live.worker import BurstMeter

FAST = {"_RECONNECT_DELAYS": (0.01,) * 6, "_ENDLESS_DELAYS": (0.02,)}
PATH = "/stream"


def paced(body: bytes, piece: int = 4096, pause: float = 0.0):
    """A route's body that writes `body` in pieces (`pause` between them), then closes."""

    def write(request):
        for at in range(0, len(body), piece):
            request.wfile.write(body[at : at + piece])
            request.wfile.flush()
            if pause:
                time.sleep(pause)

    return write


def collect(source) -> list:
    """Every item from `chunks()` until the input ends (a 404 ends a station's here), the
    adjacent byte blocks joined."""
    items: list = []
    try:
        for item in source.chunks():
            if isinstance(item, bytes) and items and isinstance(items[-1], bytes):
                items[-1] += item
            else:
                items.append(item)
    except SourceError as error:
        items.append(error)
    finally:
        source.close()
    return items


def sound_of(items) -> bytes:
    return b"".join(item for item in items if isinstance(item, bytes))


class StationSourceTests(unittest.TestCase):
    def setUp(self):
        self.servers = []
        self.enterContext(patch.multiple(source_module, **FAST))

    def tearDown(self):
        for server in self.servers:
            server.close()

    def server(self, routes):
        server = LocalSource(routes)
        self.servers.append(server)
        return server

    def open(self, server, *, radio=True, path=PATH):
        return open_source(
            server.url + path, headers={}, verify_ssl=True, stop=threading.Event(), radio=radio
        )

    def test_an_icecast_streams_sound_comes_as_ts_with_its_songs_and_a_reconnect_runs_on(self):
        first = with_icy(mp3_stream(120), 8000, {0: "Artist - First", 3: "Artist - Second"})
        # The reconnect's burst starts 40 frames back, mid-frame: those repeat what was sent.
        again = mp3_stream(80, 80)[100:]

        def station(_request, count):
            if count == 1:
                return 200, {"Content-Type": "audio/mpeg", "icy-metaint": "8000"}, paced(first)
            if count == 2:
                return 200, {"Content-Type": "audio/mpeg"}, paced(again)
            return 404, {}, b""

        server = self.server({PATH: station})
        source = self.open(server)
        self.assertIsInstance(source, SoundSource)
        self.assertFalse(source.hls)
        self.assertEqual(source.burst_s, 0.0)
        items = collect(source)
        self.assertEqual([path for path, _ in server.requests], [PATH] * 3)
        for _path, headers in server.requests:
            self.assertEqual(headers.get("Icy-MetaData"), "1")  # the song, inline
        self.assertFalse(any(isinstance(item, Discontinuity) for item in items))
        self.assertTrue(any(isinstance(item, Reconnecting) for item in items))
        self.assertIsInstance(items[-1], SourceError)  # the 404
        units = pes_units(sound_of(items), AUDIO_PID)
        self.assertEqual([body for _, body in units], [mp3_frame(k) for k in range(160)])
        steps = {b[0] - a[0] for a, b in zip(units, units[1:])}
        self.assertEqual(steps, {2351, 2352})  # one clock across the reconnect
        self.assertEqual(stream_types(sound_of(items), PMT_PID), [0x03])
        # Each song comes before the sound after its block: 8000 bytes of sound each.
        titles, frames_before = [], 0
        for item in items:
            if isinstance(item, Title):
                titles.append((item.text, frames_before))
            elif isinstance(item, bytes):
                frames_before += len(pes_units(item, AUDIO_PID))
        self.assertEqual([text for text, _ in titles], ["Artist - First", "Artist - Second"])
        self.assertEqual([at for _, at in titles], [8000 // 417, 32000 // 417])

    def test_an_adts_station_without_metadata(self):
        body = adts_stream(100)
        routes = {PATH: lambda _r, n: (200, {}, paced(body)) if n == 1 else (404, {}, b"")}
        items = collect(self.open(self.server(routes)))
        units = pes_units(sound_of(items), AUDIO_PID)
        self.assertEqual([b for _, b in units], [adts_frame(k) for k in range(100)])
        self.assertEqual(stream_types(sound_of(items), PMT_PID), [0x0F])
        self.assertFalse(any(isinstance(item, Title) for item in items))

    def test_a_format_it_cannot_read_is_refused_at_once_in_one_request(self):
        for name, body in (
            ("ogg", b"OggS\x00\x02" + bytes(4000)),
            ("flac", b"fLaC" + bytes(4000)),
            ("a page", b"<html><body>Station offline</body></html>"),
            ("noise", bytes(300_000)),
        ):
            with self.subTest(name):
                server = self.server({PATH: (200, {"Content-Type": "audio/ogg"}, body)})
                started = time.monotonic()
                with self.assertRaises(SourceError) as raised:
                    self.open(server)
                self.assertEqual(raised.exception.kind, "unsupported")
                self.assertLess(time.monotonic() - started, 2.0)
                self.assertEqual(server.counts[PATH], 1)

    def test_sound_that_stops_being_sound_ends_as_unsupported(self):
        body = mp3_stream(50) + bytes(70_000)
        items = collect(self.open(self.server({PATH: (200, {}, body)})))
        self.assertIsInstance(items[-1], SourceError)
        self.assertEqual(items[-1].kind, "unsupported")
        self.assertEqual(len(pes_units(sound_of(items), AUDIO_PID)), 50)

    def test_a_channels_open_is_as_before(self):
        # A raw body of sound for a channel (no station): untouched, as 2.13 read it (the
        # segmenter then refuses it), and no song asked for.
        body = mp3_stream(30)
        routes = {PATH: lambda _r, n: (200, {}, body) if n == 1 else (404, {}, b"")}
        server = self.server(routes)
        source = self.open(server, radio=False)
        self.assertIs(type(source), TsSource)
        self.assertEqual(sound_of(collect(source)), body)
        self.assertNotIn("Icy-MetaData", server.requests[0][1])

    def test_a_station_that_sends_ts_is_read_as_ts(self):
        body = ts_fixtures.make_stream(2)
        routes = {PATH: lambda _r, n: (200, {}, body) if n == 1 else (404, {}, b"")}
        source = self.open(self.server(routes))
        self.assertIs(type(source), IcyTsSource)  # the worker reads it as any raw TS
        self.assertFalse(getattr(source, "sound", False))
        self.assertEqual(sound_of(collect(source)), body)

    def test_a_ts_stations_inline_metadata_is_taken_out(self):
        # An Icecast server can send TS and interleave its metadata blocks into it.
        body = ts_fixtures.make_stream(2)
        sent = with_icy(body, 8000, {0: "Mira Sol - Ferries"})
        routes = {
            PATH: lambda _r, n: (200, {"icy-metaint": "8000"}, sent) if n == 1 else (404, {}, b"")
        }
        items = collect(self.open(self.server(routes)))
        self.assertEqual(sound_of(items), body)
        self.assertIn(Title("Mira Sol - Ferries"), items)

    def test_sound_whose_first_byte_is_a_ts_sync_byte_is_sound(self):
        # Joined mid-frame, an ADTS stream's first byte can be 0x47: one byte is no TS.
        body = b"\x47" + adts_stream(60)[1:]
        routes = {PATH: lambda _r, n: (200, {}, body) if n == 1 else (404, {}, b"")}
        source = self.open(self.server(routes))
        self.assertIsInstance(source, SoundSource)
        units = pes_units(sound_of(collect(source)), AUDIO_PID)
        self.assertEqual([b for _, b in units], [adts_frame(k) for k in range(1, 60)])

    def test_metadata_cut_by_a_reconnect_leaves_no_bytes_in_the_sound(self):
        # The first connection breaks inside a metadata block: the next answer's blocks are
        # counted afresh, and nothing of the cut block reaches the sound.
        songs = {0: "Wexlo Tide - Pell", 1: "Mira Sol - Ferries"}
        first = with_icy(mp3_stream(60), 8000, songs)
        second_block = 8000 + len(metadata_block(songs[0])) + 8000
        broken = first[: second_block + 1 + 10]  # its length byte and 10 bytes of its text
        again = with_icy(mp3_stream(40, 40)[300:], 6000, {0: "Mira Sol - Ferries"})

        def station(_request, count):
            if count == 1:
                return 200, {"icy-metaint": "8000"}, paced(broken)
            if count == 2:
                return 200, {"icy-metaint": "6000"}, paced(again)
            return 404, {}, b""

        items = collect(self.open(self.server({PATH: station})))
        bodies = [b for _, b in pes_units(sound_of(items), AUDIO_PID)]
        expected = [mp3_frame(k) for k in range(16000 // 417)]  # the first's whole frames
        expected += [mp3_frame(k) for k in range(41, 80)]  # the second's, from mid-frame 40
        self.assertEqual(bodies, expected)
        titles = [item.text for item in items if isinstance(item, Title)]
        self.assertEqual(titles, ["Wexlo Tide - Pell", "Mira Sol - Ferries"])

    def test_an_old_shoutcast_status_line_is_read(self):
        # "ICY 200 OK": http.client refuses that line; the station's answer is HTTP/1.0's.
        body = with_icy(mp3_stream(40), 4000, {0: "Old - Server"})
        with icy_server(b"ICY 200 OK\r\nicy-metaint: 4000\r\nicy-name: x\r\n\r\n", body) as url:
            source = open_source(
                url, headers={}, verify_ssl=True, stop=threading.Event(), radio=True
            )
            items = collect(source)
        self.assertEqual(len(pes_units(sound_of(items), AUDIO_PID)), 40)
        self.assertIn(Title("Old - Server"), items)


@contextmanager
def icy_server(head: bytes, body: bytes):
    """A server whose first answer is `head` and `body`, then a 404 for any other."""
    listener = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def serve():
        listener.settimeout(0.1)
        count = 0
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(2)
                data = b""
                while b"\r\n\r\n" not in data:
                    part = connection.recv(4096)
                    if not part:
                        break
                    data += part
                count += 1
                if count == 1:
                    connection.sendall(head + body)
                else:
                    connection.sendall(b"HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\n\r\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/stream"
    finally:
        stop.set()
        thread.join(2)
        listener.close()


class StationHlsTests(unittest.TestCase):
    """An HLS station (2.14): a master of sound alone, its segments packed ADTS dated by ID3."""

    MASTER = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"\naudio.m3u8\n'

    def serve(self, segment):
        routes = {"/master.m3u8": (200, {}, self.MASTER)}
        routes["/audio.m3u8"] = (200, {}, leaf("s0.aac", "s1.aac", "s2.aac"))
        for n in range(3):
            routes[f"/s{n}.aac"] = (200, {}, segment(n))
        server = LocalSource(routes)
        self.addCleanup(server.close)
        return server

    def open(self, server, radio):
        return open_source(
            server.url + "/master.m3u8",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            live_delay=60.0,  # from its first segment
            radio=radio,
        )

    def test_its_packed_sound_comes_as_ts_at_its_own_times(self):
        ticks = 43 * 1024 * 90000 / 44100  # a segment of 43 frames

        def segment(n):
            return id3_tag(timestamp=900_000 + round(n * ticks)) + adts_stream(43, 43 * n)

        server = self.serve(segment)
        source = self.open(server, radio=True)
        self.assertTrue(source.hls)
        ts = sound_of(collect(source))
        units = pes_units(ts, AUDIO_PID)
        self.assertEqual([body for _, body in units], [adts_frame(k) for k in range(129)])
        self.assertEqual(
            [units[43 * n][0] for n in range(3)], [900_000 + round(n * ticks) for n in range(3)]
        )
        self.assertEqual(stream_types(ts, PMT_PID), [0x0F])
        # Undated, a segment follows on from the one before.
        server = self.serve(lambda n: adts_stream(43, 43 * n))
        units = pes_units(sound_of(collect(self.open(server, radio=True))), AUDIO_PID)
        self.assertEqual(len(units), 129)
        steps = {b[0] - a[0] for a, b in zip(units, units[1:])}
        self.assertEqual(steps, {2090, 2089})

    def test_a_list_naming_an_endless_stream_is_refused_not_read_whole(self):
        # A station's link to a plain M3U list of its stream (VLC plays those): its "segment"
        # never ends, so it is no packed sound; the station plays directly.
        def endless(request):
            while True:
                request.wfile.write(mp3_stream(64))

        routes = {"/list.m3u": (200, {}, b"#EXTM3U\n#EXTINF:-1,Wexlo FM\n/stream\n")}
        routes["/stream"] = (200, {}, endless)
        server = LocalSource(routes)
        self.addCleanup(server.close)
        source = open_source(
            server.url + "/list.m3u",
            headers={},
            verify_ssl=True,
            stop=threading.Event(),
            radio=True,
        )
        with self.assertRaises(SourceError) as raised:
            list(source.chunks())
        self.assertEqual(raised.exception.kind, "unsupported")

    def test_an_endless_segment_is_given_up_in_time_not_read_for_minutes(self):
        # At a station's real time, its 8 MiB bound would take minutes: a time bound ends it.
        def endless(request):
            while True:
                request.wfile.write(mp3_stream(4))
                request.wfile.flush()
                time.sleep(0.1)

        routes = {"/list.m3u": (200, {}, b"#EXTM3U\n#EXTINF:-1,Wexlo FM\n/stream\n")}
        routes["/stream"] = (200, {}, endless)
        server = LocalSource(routes)
        self.addCleanup(server.close)
        with patch.object(source_module, "_PACKED_SLOW_S", 1.0):
            source = open_source(
                server.url + "/list.m3u",
                headers={},
                verify_ssl=True,
                stop=threading.Event(),
                radio=True,
            )
            started = time.monotonic()
            with self.assertRaises(SourceError) as raised:
                list(source.chunks())
        self.assertEqual(raised.exception.kind, "unsupported")
        self.assertLess(time.monotonic() - started, 5.0)

    def test_its_clock_goes_on_across_a_discontinuity_that_starts_one_behind(self):
        # A sound-only stream has no picture the clock joiner would re-base a new clock onto:
        # a discontinuity whose ID3 time goes back follows on from the sound written.
        ticks = 43 * 1024 * 90000 / 44100
        stamps = (900_000, 900_000 + round(ticks), 90_000)

        def segment(n):
            return id3_tag(timestamp=stamps[n]) + adts_stream(43, 43 * n)

        routes = {"/master.m3u8": (200, {}, self.MASTER)}
        text = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n"
        text += "#EXTINF:1,\ns0.aac\n#EXTINF:1,\ns1.aac\n#EXT-X-DISCONTINUITY\n"
        text += "#EXTINF:1,\ns2.aac\n#EXT-X-ENDLIST\n"
        routes["/audio.m3u8"] = (200, {}, text.encode())
        for n in range(3):
            routes[f"/s{n}.aac"] = (200, {}, segment(n))
        server = LocalSource(routes)
        self.addCleanup(server.close)
        items = collect(self.open(server, radio=True))
        self.assertIn(Discontinuity(), items)
        units = pes_units(sound_of(items), AUDIO_PID)
        self.assertEqual(len(units), 129)
        steps = {b[0] - a[0] for a, b in zip(units, units[1:])}
        self.assertEqual(steps, {2089, 2090})  # never back

    def test_a_channels_master_of_sound_alone_is_refused_as_before(self):
        server = self.serve(lambda n: adts_stream(43, 43 * n))
        with self.assertRaises(SourceError) as raised:
            self.open(server, radio=False)
        self.assertEqual(raised.exception.kind, "unsupported")


class StationSessionTests(unittest.TestCase):
    """The helper's session of a station: its songs in the state, no recording guard."""

    def test_a_station_plays_from_the_buffer_with_its_songs_in_the_state(self):
        sound = mp3_stream(800)  # 20.9 s
        body = with_icy(sound, 16000, {0: "One - Song", 10: "Two - Song"})
        # Its connect burst of some 5 s at once, then about real time (sped up 4x here).
        burst = 90_000
        rest = body[burst:]

        def write(request):
            request.wfile.write(body[:burst])
            request.wfile.flush()
            for at in range(0, len(rest), 2000):
                request.wfile.write(rest[at : at + 2000])
                request.wfile.flush()
                time.sleep(2000 / 16000 / 4)

        upstream = LocalSource({PATH: lambda _r, n: (200, {"icy-metaint": "16000"}, write)})
        self.addCleanup(upstream.close)
        helper = InProcessHelper(self)
        helper.parent.send(("open", "r", upstream.url + PATH, {}, True, RAM, 6.0, {"radio": True}))
        helper.wait_for(lambda m: m == ("ready", "r"), timeout=15)

        def titled(message):
            if message[:2] != ("state", "r"):
                return False
            return len(message[2].get("titles") or []) == 2

        state = helper.wait_for(titled, timeout=15)[2]
        (first, at_first), (second, at_second) = [tuple(t)[::-1] for t in state["titles"]]
        self.assertEqual((first, second), ("One - Song", "Two - Song"))
        block_s = 16000 / 417 * 1152 / 44100  # each song where its block came in the sound
        self.assertAlmostEqual(at_first, block_s, delta=0.1)
        self.assertAlmostEqual(at_second, 11 * block_s, delta=0.1)
        self.assertEqual(state["not_live"], 0)
        self.assertFalse(state["hls"])
        self.assertFalse(state["video"])
        self.assertEqual(state["restarts"], 0)
        with urlopen(f"{helper.base_url}/r/stream/0.ts", timeout=5) as answer:
            served = answer.read(64 * 1024)
        served = served[: len(served) // 188 * 188]
        self.assertTrue(pes_units(served, AUDIO_PID))
        self.assertEqual(stream_types(served, PMT_PID), [0x03])
        self.assertEqual(upstream.requests[0][1].get("Icy-MetaData"), "1")

    def test_a_light_stations_long_connect_burst_is_kept(self):
        # A channel's connection that brings 90 s of light media at once is a recording, not
        # the channel (2.8.10, NotLiveGuard). A station's sound can: a 32 kbit/s station's
        # 400 KiB burst is 100 s. It is kept.
        frame = bytes([0xFF, 0xFB, 0x10, 0x64]) + bytes(100)  # 32 kbit/s: 104 bytes a frame
        burst = frame * 4000  # 104 s

        def write(request):
            request.wfile.write(burst)
            request.wfile.flush()
            time.sleep(3)

        upstream = LocalSource({PATH: lambda _r, n: (200, {}, write)})
        self.addCleanup(upstream.close)
        helper = InProcessHelper(self)
        helper.parent.send(("open", "r", upstream.url + PATH, {}, True, RAM, 6.0, {"radio": True}))
        helper.wait_for(lambda m: m == ("ready", "r"), timeout=15)
        state = helper.wait_for(
            lambda m: m[:2] == ("state", "r") and m[2]["live_edge"] > 90, timeout=15
        )[2]
        self.assertEqual(state["not_live"], 0)


class StationResumeTests(unittest.TestCase):
    """A station's kept buffer (2.6 park and resume): the resumed run's sound goes on after the
    old run's, never back onto timestamps the player had (libVLC 3 drops such sound as late)."""

    def test_a_resumed_stations_sound_follows_the_old_runs(self):
        first = LocalSource({PATH: (200, {}, mp3_stream(200))})
        second = LocalSource({PATH: (200, {}, mp3_stream(200, 500))})
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        helper = InProcessHelper(self)
        radio = {"radio": True}
        helper.parent.send(("open", "r", first.url + PATH, {}, True, RAM, 6.0, radio))
        helper.wait_for(lambda m: m == ("ended", "r"), timeout=15)
        helper.parent.send(("park", "r"))
        helper.wait_for(lambda m: m == ("parked", "r"))
        before = [m for m in helper.messages if m[:2] == ("state", "r")]
        last = max(addition[0] for m in before for addition in m[4])
        resume = ("resume", "r", second.url + PATH, {}, True, 6.0, radio)
        helper.parent.send(resume)
        helper.wait_for(lambda m: m == ("resumed", "r"), timeout=15)
        helper.wait_for(lambda m: m == ("ended", "r"), timeout=15)
        with urlopen(f"{helper.base_url}/r/stream/{last}.ts", timeout=5) as answer:
            body = answer.read()
        units = pes_units(body[: len(body) // 188 * 188], AUDIO_PID)
        bodies = [unit for _, unit in units]
        self.assertIn(mp3_frame(199), bodies)
        self.assertIn(mp3_frame(500), bodies)  # the resumed run's sound, after the old run's
        steps = [b[0] - a[0] for a, b in zip(units, units[1:])]
        self.assertTrue(all(0 < step <= 2 * 2352 for step in steps), sorted(set(steps)))

    def test_a_stations_kept_buffer_taken_back_goes_on_past_its_sound(self):
        # Taken back by the next helper (2.6 adopt): no earlier run to ask, so past the newest
        # segment's last frame.
        root = self.enterContext(tempfile.TemporaryDirectory(prefix="qitv-radio-adopt-"))
        first_up = LocalSource({PATH: (200, {}, mp3_stream(200))})
        second_up = LocalSource({PATH: (200, {}, mp3_stream(200, 500))})
        self.addCleanup(first_up.close)
        self.addCleanup(second_up.close)
        first = InProcessHelper(self, root)
        directory = ".qitv-buffer-" + "ab" * 16
        ring = {"kind": "disk", "root": root, "dir": directory, "max_bytes": 1 << 30}
        radio = {"radio": True}
        first.parent.send(("open", "a", first_up.url + PATH, {}, True, ring, 6.0, radio))
        first.parent.send(("keep", "a", {"channel": "k1"}))
        first.wait_for(lambda m: m == ("ended", "a"), timeout=15)
        first.parent.send(("park", "a"))
        first.wait_for(lambda m: m == ("parked", "a"))
        states = [m for m in first.messages if m[:2] == ("state", "a")]
        last = max(addition[0] for m in states for addition in m[4])
        first.helper.shutdown()
        second = InProcessHelper(self, root)
        second.helper._dispatch(("adopt", "z", directory, 1 << 30))
        second.wait_for(lambda m: m[:2] == ("kept", "z"))
        second.parent.send(("resume", "z", second_up.url + PATH, {}, True, 6.0, radio))
        second.wait_for(lambda m: m == ("resumed", "z"), timeout=15)
        second.wait_for(lambda m: m == ("ended", "z"), timeout=15)
        with urlopen(f"{second.base_url}/z/stream/{last}.ts", timeout=5) as answer:
            body = answer.read()
        units = pes_units(body[: len(body) // 188 * 188], AUDIO_PID)
        self.assertIn(mp3_frame(500), [unit for _, unit in units])
        steps = [b[0] - a[0] for a, b in zip(units, units[1:])]
        # Each frame's own step, and once, at the junction, the newest segment's last frame's
        # start plus 0.15 s (past the longest frame).
        self.assertEqual(sorted(set(steps)), [2351, 2352, 13_500])
        self.assertEqual(steps.count(13_500), 1)

    def test_an_hls_stations_undated_sound_follows_the_old_runs_after_a_resume(self):
        first = LocalSource(
            {
                "/a.m3u8": (200, {}, leaf("s0.aac", "s1.aac")),
                "/s0.aac": (200, {}, adts_stream(43)),
                "/s1.aac": (200, {}, adts_stream(43, 43)),
            }
        )
        second = LocalSource(
            {
                "/a.m3u8": (200, {}, leaf("t0.aac", "t1.aac")),
                "/t0.aac": (200, {}, adts_stream(43, 500)),
                "/t1.aac": (200, {}, adts_stream(43, 543)),
            }
        )
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        helper = InProcessHelper(self)
        radio = {"radio": True}
        helper.parent.send(("open", "h", first.url + "/a.m3u8", {}, True, RAM, 6.0, radio))
        helper.wait_for(lambda m: m == ("ended", "h"), timeout=15)
        helper.parent.send(("park", "h"))
        helper.wait_for(lambda m: m == ("parked", "h"))
        states = [m for m in helper.messages if m[:2] == ("state", "h")]
        last = max(addition[0] for m in states for addition in m[4])
        helper.parent.send(("resume", "h", second.url + "/a.m3u8", {}, True, 6.0, radio))
        helper.wait_for(lambda m: m == ("resumed", "h"), timeout=15)
        helper.wait_for(lambda m: m == ("ended", "h"), timeout=15)
        with urlopen(f"{helper.base_url}/h/stream/{last}.ts", timeout=5) as answer:
            body = answer.read()
        units = pes_units(body[: len(body) // 188 * 188], AUDIO_PID)
        self.assertIn(adts_frame(500), [unit for _, unit in units])
        steps = {b[0] - a[0] for a, b in zip(units, units[1:])}
        self.assertEqual(steps, {2089, 2090})  # the new run's sound right after the old's


FFMPEG = shutil.which("ffmpeg")


@unittest.skipUnless(FFMPEG, "ffmpeg makes the station's sound")
class StationFmp4Tests(unittest.TestCase):
    """An HLS station of sound alone in fragmented MP4 (CMAF AAC, CODECS mp4a only): played
    from the buffer, and the AI captions' decoder reads its sound off the side output."""

    MASTER = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=96000,CODECS="mp4a.40.2"\naudio.m3u8\n'

    def test_a_sound_only_fmp4_station_plays_and_its_captions_read_its_sound(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="qitv-fmp4-")))
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i"]
            + ["aevalsrc=0.4*sin(2*PI*(300+15*t)*t):s=44100:d=12", "-c:a", "aac", "-b:a", "96k"]
            + ["-f", "hls", "-hls_time", "4", "-hls_segment_type", "fmp4"]
            + ["-hls_playlist_type", "vod", "-hls_fmp4_init_filename", "init.mp4"]
            + [
                "-hls_segment_filename",
                str(directory / "seg%d.m4s"),
                str(directory / "audio.m3u8"),
            ],
            check=True,
        )
        self.assertIn(b"#EXT-X-MAP", (directory / "audio.m3u8").read_bytes())
        routes = {f"/{f.name}": (200, {}, f.read_bytes()) for f in directory.iterdir()}
        routes["/master.m3u8"] = (200, {}, self.MASTER)
        upstream = LocalSource(routes)
        self.addCleanup(upstream.close)
        helper = InProcessHelper(self)
        url = upstream.url + "/master.m3u8"
        helper.parent.send(("open", "f", url, {}, True, RAM, 60.0, {"radio": True}))
        helper.wait_for(lambda m: m == ("ended", "f"), timeout=30)
        states = [m for m in helper.messages if m[:2] == ("state", "f")]
        first = min(addition[0] for m in states for addition in m[4])
        self.assertTrue(states[-1][2]["fmp4"])
        self.assertFalse(states[-1][2]["video"])
        with urlopen(f"{helper.base_url}/f/side/{first}.ts", timeout=10) as answer:
            body = answer.read()
        body = body[: len(body) // 188 * 188]
        self.assertEqual(stream_types(body, 0x1000), [0x0F])
        from services.vlc_bundle import use_bundled_vlc

        use_bundled_vlc()
        from services.captions.decoder import RATE, AudioDecoder

        blocks: list[bytes] = []
        decoder = AudioDecoder(lambda _us, pcm: blocks.append(bytes(pcm)))
        decoder.start()
        try:
            for at in range(0, len(body), 64 * 1024):
                decoder.feed(body[at : at + 64 * 1024])
            decoder.end()
            deadline = time.monotonic() + 20
            while not decoder.finished() and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            decoder.stop()
        pcm = b"".join(blocks)
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
        self.assertGreater(len(samples), 10 * RATE)  # most of its 12 s
        rms = math.sqrt(sum(v * v for v in samples) / len(samples))
        self.assertGreater(rms, 1000)  # the tone, not silence


class BurstTests(unittest.TestCase):
    def test_a_connect_burst_then_real_time_is_a_steady_source(self):
        # An Icecast station: 5 s at connect, then its sound at real time in small reads.
        meter = BurstMeter()
        meter.add(0.0, 5.0)
        wall, media = 0.0, 5.0
        for _ in range(600):  # a minute
            wall += 0.1
            media += 0.1
            meter.add(wall, media)
        self.assertEqual(meter.burst_s, 0.0)
