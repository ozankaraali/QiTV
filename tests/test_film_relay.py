"""The film relay (2.9): a provider's film for the picture and AI captions over one connection.

A stand-in provider (tests/film_provider.py) serves a film behind a redirect to a tokenized
address and notes any moment two of its connections are open at once; every test ends by
checking there was none."""

from http.client import HTTPConnection, IncompleteRead
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from film_provider import FilmProvider

from services.film_relay import FilmRelay, Limits, relayable

KIB, MIB = 1024, 1024 * 1024
SMALL = Limits(
    block=64 * KIB,
    ahead=1 * MIB,
    behind=1 * MIB,
    keep=3 * MIB,
    pin_head=256 * KIB,
    pin_tail=256 * KIB,
    near=128 * KIB,
    detour=128 * KIB,
    detour_lead=256 * KIB,
    detour_calm_s=0.3,
    open_wait_s=5.0,
    leader_wait_s=5.0,
    follower_wait_s=1.5,
    connect_s=3.0,
    head_s=3.0,
    read_s=3.0,
    retries=2,
    retry_s=0.1,
)
FILM = os.urandom(6 * MIB)


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class Reader:
    """A reader of one of the relay's addresses (as libVLC asks: a range from `first` on)."""

    def __init__(self, url, first=0, last=None, method="GET"):
        parts = urlsplit(url)
        self.connection = HTTPConnection(parts.hostname, parts.port, timeout=10)
        headers = {"Range": f"bytes={first}-{'' if last is None else last}"}
        self.connection.request(method, parts.path, headers=headers)
        self.response = self.connection.getresponse()
        self.status = self.response.status

    def read(self, size):
        found = b""
        while len(found) < size:
            data = self.response.read(size - len(found))
            if not data:
                break
            found += data
        return found

    def close(self):
        self.connection.close()


class RelayTestCase(unittest.TestCase):
    extension = ".mkv"
    content_type = "video/x-matroska"
    rate = None

    def setUp(self):
        self.provider = FilmProvider(
            FILM, extension=self.extension, content_type=self.content_type, rate=self.rate
        )
        self.addCleanup(self.provider.close)
        self.relay = self.make_relay()

    def make_relay(self, **kwargs):
        options = {"user_agent": "VLC/3.0.20", "limits": SMALL}
        options.update(kwargs)
        relay = FilmRelay(self.provider.url, **options)
        relay.start()
        self.addCleanup(relay.wait_closed, 3.0)
        self.addCleanup(relay.stop)
        return relay

    def tearDown(self):
        self.assertEqual(self.provider.overlaps, [], "two upstream connections were open at once")

    def read(self, url, first, size, last=None):
        reader = Reader(url, first, last)
        try:
            self.assertEqual(reader.status, 206)
            return reader.read(size)
        finally:
            reader.close()


class AddressTests(unittest.TestCase):
    def test_what_the_relay_carries(self):
        self.assertTrue(relayable("http://host:8080/movie/u/p/7.mkv"))
        self.assertTrue(relayable("https://host/series/u/p/9.mp4"))
        self.assertTrue(relayable("http://host/play?id=3"))
        self.assertFalse(relayable("http://host/movie/u/p/7.m3u8"))
        self.assertFalse(relayable("http://host/play?id=3&output=m3u8"))
        self.assertFalse(relayable("file:///films/a.mkv"))
        self.assertFalse(relayable("rtsp://host/a"))


class PictureTests(RelayTestCase):
    def test_the_picture_plays_the_film_as_the_provider_serves_it(self):
        self.assertTrue(self.relay.leader_url.endswith("/leader.mkv"))
        data = self.read(self.relay.leader_url, 0, len(FILM))
        self.assertEqual(data, FILM)
        asks = self.provider.film_asks()
        self.assertEqual(len(asks), 1)
        self.assertEqual((asks[0]["first"], asks[0]["agent"]), (0, "VLC/3.0.20"))
        self.assertEqual(asks[0]["cookie"], "session=abc")  # the redirect's cookie goes on

    def test_the_whole_film_without_a_range_and_its_length(self):
        reader = Reader(self.relay.leader_url, method="HEAD")
        self.assertEqual(reader.status, 206)
        self.assertEqual(
            reader.response.getheader("Content-Range"), f"bytes 0-{len(FILM) - 1}/{len(FILM)}"
        )
        self.assertEqual(reader.response.getheader("Content-Type"), "video/x-matroska")
        reader.close()

    def test_a_seek_moves_the_upstream_closing_the_old_connection_first(self):
        reader = Reader(self.relay.leader_url, 0)
        self.assertEqual(reader.read(200 * KIB), FILM[: 200 * KIB])
        far = 4 * MIB + 1000
        self.assertEqual(
            self.read(self.relay.leader_url, far, 300 * KIB), FILM[far : far + 300 * KIB]
        )
        reader.close()
        firsts = [ask["first"] for ask in self.provider.film_asks()]
        self.assertEqual(firsts[0], 0)
        self.assertIn(far - far % SMALL.block, firsts)  # from the block the seek landed in
        self.assertGreaterEqual(self.relay.stats["moves"], 1)

    def test_a_short_seek_ahead_waits_for_the_upstream_instead_of_moving_it(self):
        reader = Reader(self.relay.leader_url, 0)
        reader.read(64 * KIB)
        reader.close()
        near = 64 * KIB + 100 * KIB
        self.assertEqual(
            self.read(self.relay.leader_url, near, 64 * KIB), FILM[near : near + 64 * KIB]
        )
        self.assertEqual(len(self.provider.film_asks()), 1)

    def test_a_seek_back_into_what_is_kept_needs_no_connection(self):
        self.assertEqual(self.read(self.relay.leader_url, 0, 512 * KIB), FILM[: 512 * KIB])
        asks = len(self.provider.film_asks())
        self.assertEqual(
            self.read(self.relay.leader_url, 100 * KIB, 200 * KIB), FILM[100 * KIB : 300 * KIB]
        )
        self.assertEqual(len(self.provider.film_asks()), asks)


class CapTests(RelayTestCase):
    def test_the_upstream_reads_no_further_than_its_cap_past_the_picture_then_waits(self):
        reader = Reader(self.relay.leader_url, 0)
        reader.read(100 * KIB)  # then the picture's libVLC reads nothing for a while
        time.sleep(1.0)
        read = self.relay.stats["bytes"]
        picture = self.relay._leader_pos
        self.assertLessEqual(read, picture + SMALL.ahead + 64 * KIB)
        time.sleep(0.5)
        self.assertEqual(self.relay.stats["bytes"], read)  # held: TCP backpressure
        self.assertEqual(self.provider.open_now(), 1)  # the connection is kept, not closed
        self.assertEqual(reader.read(2 * MIB), FILM[100 * KIB : 100 * KIB + 2 * MIB])
        self.assertGreater(self.relay.stats["bytes"], read)  # and goes on as the picture reads
        self.assertEqual(len(self.provider.film_asks()), 1)
        reader.close()

    def test_a_captions_reader_that_stops_reading_never_holds_the_picture_back(self):
        picture = Reader(self.relay.leader_url, 0)
        self.assertEqual(picture.read(64 * KIB), FILM[: 64 * KIB])
        follower = Reader(self.relay.follower_url, 0)
        follower.read(16 * KIB)  # then it holds (a paused captions reader)
        self.assertEqual(picture.read(len(FILM)), FILM[64 * KIB :])
        follower.close()
        picture.close()

    def test_what_is_far_behind_the_picture_is_let_go(self):
        self.assertEqual(self.read(self.relay.leader_url, 0, len(FILM)), FILM)
        kept = sum(len(b) for i, b in self.relay._blocks.items() if not self.relay._pinned(i))
        self.assertLessEqual(kept, SMALL.keep)


class CaptionsTests(RelayTestCase):
    def test_the_captions_read_a_little_past_the_picture_on_the_same_connection(self):
        picture = Reader(self.relay.leader_url, 0)
        picture.read(300 * KIB)
        at = 600 * KIB
        self.assertEqual(
            self.read(self.relay.follower_url, at, 256 * KIB), FILM[at : at + 256 * KIB]
        )
        self.assertEqual(len(self.provider.film_asks()), 1)
        behind = 100 * KIB  # and behind the picture's read, where libVLC has read ahead
        self.assertEqual(
            self.read(self.relay.follower_url, behind, 64 * KIB), FILM[behind : behind + 64 * KIB]
        )
        self.assertEqual(len(self.provider.film_asks()), 1)
        picture.close()

    def test_the_captions_wait_for_the_picture_to_ask_first(self):
        found = {}

        def follow():
            found["data"] = self.read(self.relay.follower_url, 0, 64 * KIB)

        thread = threading.Thread(target=follow)
        thread.start()
        time.sleep(0.5)
        self.assertEqual(self.provider.requests, [])  # nothing opens before the picture asks
        picture = Reader(self.relay.leader_url, 0)
        picture.read(64 * KIB)
        thread.join(5)
        self.assertEqual(found["data"], FILM[: 64 * KIB])
        picture.close()

    def test_a_tail_the_picture_read_serves_the_captions(self):
        tail = len(FILM) - 40 * KIB  # an MKV's Cues, which libVLC reads when it opens the film
        self.assertEqual(self.read(self.relay.leader_url, tail, 40 * KIB), FILM[tail:])
        picture = Reader(self.relay.leader_url, 0)
        picture.read(200 * KIB)
        asks = len(self.provider.film_asks())
        self.assertEqual(self.read(self.relay.follower_url, tail, 40 * KIB), FILM[tail:])
        self.assertEqual(len(self.provider.film_asks()), asks)
        picture.close()

    def test_an_mkvs_cues_the_picture_did_not_keep_come_by_a_detour(self):
        picture = Reader(self.relay.leader_url, 0)
        self.assertEqual(picture.read(600 * KIB), FILM[: 600 * KIB])  # cached ahead: it can wait
        cues = len(FILM) - 30 * KIB
        self.assertEqual(self.read(self.relay.follower_url, cues, 30 * KIB), FILM[cues:])
        self.assertEqual(self.relay.stats["detours"], 1)
        rest = 600 * KIB + 2 * MIB
        self.assertEqual(picture.read(2 * MIB), FILM[600 * KIB : rest])  # the picture goes on
        picture.close()
        asks = self.provider.film_asks()
        tail = [a for a in asks if a["first"] >= len(FILM) - SMALL.pin_tail]
        self.assertEqual(len(tail), 1)
        self.assertIsNotNone(tail[0]["last"])  # a detour asks for its bytes only
        resumed = asks[asks.index(tail[0]) + 1]
        self.assertGreater(resumed["first"], 600 * KIB)  # from where the picture's run was
        self.assertIsNone(resumed["last"])

    def test_an_mp4s_moov_at_the_end_comes_in_detours_and_is_kept(self):
        picture = Reader(self.relay.leader_url, 0)
        picture.read(600 * KIB)
        moov = len(FILM) - 250 * KIB  # larger than one detour
        self.assertEqual(self.read(self.relay.follower_url, moov, 250 * KIB), FILM[moov:])
        self.assertGreaterEqual(self.relay.stats["detours"], 2)
        asks = len(self.provider.film_asks())
        self.assertEqual(self.read(self.relay.follower_url, moov, 250 * KIB), FILM[moov:])
        self.assertEqual(len(self.provider.film_asks()), asks)  # kept (pinned)
        self.assertEqual(picture.read(1 * MIB), FILM[600 * KIB : 1624 * KIB])
        picture.close()

    def test_no_detour_while_the_picture_waits_for_the_upstream(self):
        self.provider.rate = 400 * KIB  # a slow provider: the picture reads at its edge
        limits = Limits(**{**SMALL.__dict__, "follower_wait_s": 6.0})
        relay = self.make_relay(limits=limits)
        picture = Reader(relay.leader_url, 0)
        picture.read(32 * KIB)
        done = {}

        def play():
            picture.read(400 * KIB)  # about a second at the provider's pace
            done["at"] = time.monotonic()

        reading = threading.Thread(target=play)
        reading.start()
        cues = len(FILM) - 20 * KIB
        follower = Reader(relay.follower_url, cues)
        data = follower.read(20 * KIB)
        follower.close()
        reading.join(10)
        self.assertEqual(data, FILM[cues:])  # once the picture had enough cached ahead
        tail = [a for a in self.provider.film_asks() if a["first"] >= len(FILM) - SMALL.pin_tail]
        self.assertGreater(tail[0]["at"], done["at"])  # not while the picture waited
        picture.close()

    def test_a_captions_read_the_upstream_is_not_heading_for_fails_in_time(self):
        picture = Reader(self.relay.leader_url, 0)
        picture.read(100 * KIB)
        far = 4 * MIB
        started = time.monotonic()
        follower = Reader(self.relay.follower_url, far)
        self.assertEqual(follower.status, 206)
        self.assertEqual(follower.read(64 * KIB), b"")  # its connection ends: captions restart
        follower.close()
        self.assertLess(time.monotonic() - started, SMALL.follower_wait_s + 1.0)
        self.assertEqual(len(self.provider.film_asks()), 1)  # the captions never moved it
        picture.close()


class ProviderTests(RelayTestCase):
    def test_an_expired_address_starts_again_from_the_providers(self):
        self.assertEqual(self.read(self.relay.leader_url, 0, 100 * KIB), FILM[: 100 * KIB])
        self.provider.expire()
        far = 3 * MIB
        self.assertEqual(
            self.read(self.relay.leader_url, far, 100 * KIB), FILM[far : far + 100 * KIB]
        )
        paths = [r["path"] for r in self.provider.requests]
        self.assertEqual(paths[0], urlsplit(self.provider.url).path)  # the provider's address
        self.assertTrue(paths[1].startswith("/stream/1/"))  # where it redirected
        self.assertTrue(paths[2].startswith("/stream/1/"))  # asked again while it answered…
        self.assertEqual(paths[3], paths[0])  # …until it expired
        self.assertTrue(paths[4].startswith("/stream/2/"))

    def test_a_dropped_connection_is_tried_again_where_it_was(self):
        self.provider.cut = 300 * KIB
        self.assertEqual(self.read(self.relay.leader_url, 0, 1 * MIB), FILM[: 1 * MIB])
        firsts = [a["first"] for a in self.provider.film_asks()]
        self.assertEqual(firsts[0], 0)
        self.assertEqual(firsts[1], 300 * KIB)

    def test_server_errors_are_tried_again_a_few_times(self):
        self.provider.answers = [503, 502]
        self.assertEqual(self.read(self.relay.leader_url, 0, 100 * KIB), FILM[: 100 * KIB])
        self.assertEqual(len(self.provider.film_asks()), 3)

    def test_a_refusal_reaches_the_picture(self):
        self.provider.answers = [404]
        reader = Reader(self.relay.leader_url, 0)
        self.assertEqual(reader.status, 404)
        reader.close()

    def test_the_referrer_goes_as_libvlc_sends_it(self):
        relay = self.make_relay(user_agent="Box/1.0", referrer="http://portal.local/")
        self.read(relay.leader_url, 0, 10 * KIB)
        ask = self.provider.requests[0]
        self.assertEqual((ask["agent"], ask["referrer"]), ("Box/1.0", "http://portal.local/"))

    def test_a_playlist_is_left_to_libvlc_and_the_captions_refused(self):
        self.provider.content_type = "application/vnd.apple.mpegurl"
        reader = Reader(self.relay.leader_url, 0)
        self.assertEqual(reader.status, 302)
        self.assertEqual(reader.response.getheader("Location"), self.provider.url)
        reader.close()
        self.assertTrue(self.relay.declined())
        self.assertFalse(self.relay.followable())
        follower = Reader(self.relay.follower_url, 0)
        self.assertEqual(follower.status, 503)
        follower.close()
        self.assertEqual(self.provider.open_now(), 0)

    def test_a_film_without_ranges_is_left_to_libvlc(self):
        self.provider.ranges = False
        reader = Reader(self.relay.leader_url, 0)
        self.assertEqual(reader.status, 302)
        reader.close()
        self.assertTrue(self.relay.declined())


class StopTests(RelayTestCase):
    def test_stop_closes_the_upstream_and_both_readers_at_once(self):
        picture = Reader(self.relay.leader_url, 0)
        picture.read(100 * KIB)  # the upstream is held open (its cap)
        follower = Reader(self.relay.follower_url, 0)
        follower.read(10 * KIB)
        self.assertTrue(wait_until(lambda: self.provider.open_now() == 1))
        started = time.monotonic()
        self.relay.stop()
        self.assertLess(time.monotonic() - started, 0.05)  # never blocks its caller
        self.assertTrue(self.relay.wait_closed(2.0))
        self.assertTrue(wait_until(lambda: self.provider.open_now() == 0, 2.0))
        for reader in (picture, follower):  # the readers' connections end too
            with self.assertRaises(IncompleteRead):
                reader.response.read()
            reader.close()
        with self.assertRaises(OSError):
            Reader(self.relay.leader_url, 0)

    def test_a_relay_stopped_before_it_opened_anything_is_closed_at_once(self):
        relay = self.make_relay()
        relay.stop()
        self.assertTrue(relay.closed())


@unittest.skipIf(shutil.which("openssl") is None, "openssl makes the test certificate")
class HttpsTests(unittest.TestCase):
    """A provider over HTTPS (the owner's providers serve films so)."""

    def setUp(self):
        from film_provider import _Server

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name)
        cert, key = folder / "cert.pem", folder / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
        )
        self.provider = FilmProvider(FILM)
        self.addCleanup(self.provider.close)
        server: _Server = self.provider._server
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self.trust = ssl.create_default_context(cafile=str(cert))

    def test_a_film_over_https(self):
        url = self.provider.url.replace("http://", "https://")
        relay = FilmRelay(url, user_agent="VLC/3.0.20", ssl_context=self.trust, limits=SMALL)
        relay.start()
        self.addCleanup(relay.wait_closed, 3.0)
        self.addCleanup(relay.stop)
        reader = Reader(relay.leader_url, 0)
        self.assertEqual(reader.read(1 * MIB), FILM[: 1 * MIB])
        far = 4 * MIB
        self.assertEqual(Reader(relay.leader_url, far).read(64 * KIB), FILM[far : far + 64 * KIB])
        reader.close()
        relay.stop()
        self.assertTrue(relay.wait_closed(2.0))
        self.assertEqual(self.provider.overlaps, [])

    def test_an_untrusted_certificate_is_refused(self):
        url = self.provider.url.replace("http://", "https://")
        relay = FilmRelay(url, user_agent="VLC/3.0.20", limits=SMALL)  # certifi's roots only
        relay.start()
        self.addCleanup(relay.wait_closed, 3.0)
        self.addCleanup(relay.stop)
        reader = Reader(relay.leader_url, 0)
        self.assertEqual(reader.status, 502)
        reader.close()


if __name__ == "__main__":
    unittest.main()
