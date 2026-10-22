import asyncio
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import aiohttp
from aiohttp import web
import mkv_builder as mb

from services.live import fmp4
from services.remote import mkv
from services.remote.films import (
    FORMAT,
    PROVIDER,
    SOUND,
    Film,
    FilmRefused,
    HttpFetcher,
    is_matroska,
)
from services.remote.hub import RemoteHub

FILMS = Path(__file__).parent / "fixtures" / "film"


class MemoryFetcher:
    """A film's bytes from memory, in chunks, counting the reads."""

    def __init__(self, data: bytes, chunk: int = 4096) -> None:
        self.data, self.chunk, self.reads = data, chunk, 0

    async def read(self, start, end):
        self.reads += 1
        return self.data[start:end]

    async def stream(self, start, end):
        self.reads += 1
        end = len(self.data) if end is None else min(end, len(self.data))
        for at in range(start, end, self.chunk):
            yield self.data[at : min(at + self.chunk, end)]


def opened(data: bytes) -> Film:
    film = Film(MemoryFetcher(data))
    asyncio.run(film.open())
    return film


def segments(film: Film) -> tuple[dict, list[list[fmp4.Sample]]]:
    """The film's init read back, and each piece's samples (services.live.fmp4 reads them)."""
    tracks = fmp4.read_init(film.init())
    pieces = []
    for index in range(len(film.pieces)):
        data = asyncio.run(film.piece(index))
        pieces.append(fmp4.read_segment(data, tracks))
    return tracks, pieces


def source_frames(data: bytes) -> list[mkv.Frame]:
    head = mkv.read_head(data)
    reader = mkv.ClusterReader({t.number: t for t in head.tracks}, head.first_cluster)
    return reader.feed(data[head.first_cluster :])


class FixtureFilmTests(unittest.TestCase):
    def film(self, name):
        data = (FILMS / name).read_bytes()
        return data, opened(data)

    def test_h264_aac_comes_whole_in_pieces_from_keyframes(self):
        data, film = self.film("h264_aac.mkv")
        duration = round(mkv.read_head(data).info.duration)
        self.assertEqual([(p.start, p.end) for p in film.pieces], [(0, 6000), (6000, duration)])
        text = film.playlist()
        self.assertIn('#EXT-X-MAP:URI="init.mp4"', text)
        self.assertIn("#EXTINF:6.000,\n0.m4s", text)
        self.assertTrue(text.rstrip().endswith("#EXT-X-ENDLIST"))
        tracks, pieces = segments(film)
        codecs = sorted((track.kind, track.codec) for track in tracks.values())
        self.assertEqual(codecs, [("audio", "aac"), ("video", "h264")])
        source = source_frames(data)
        # Every frame, in its piece, its presentation time as the file's.
        video = [s for piece in pieces for s in piece if s.track.kind == "video"]
        self.assertEqual(len(video), 192)
        self.assertEqual([s.pts for s in video], [f.time * 90 for f in source if f.track == 1])
        self.assertEqual([bytes(s.data) for s in video], [f.data for f in source if f.track == 1])
        audio = [s for piece in pieces for s in piece if s.track.kind == "audio"]
        self.assertEqual([bytes(s.data) for s in audio], [f.data for f in source if f.track == 2])
        for piece in pieces:  # each opens on a keyframe, its decode times rising
            first = next(s for s in piece if s.track.kind == "video")
            self.assertTrue(first.key)
            self.assertEqual(first.dts, first.pts)
        dts = [s.dts for s in video]
        self.assertEqual(dts, sorted(set(dts)))
        # The sound runs on from piece to piece without a gap.
        ends = [a.dts + a.duration for a in audio[:-1]]
        self.assertEqual(ends, [a.dts for a in audio[1:]])

    def test_hevc_with_ac3(self):
        data, film = self.film("hevc_ac3.mkv")
        tracks, pieces = segments(film)
        codecs = sorted((track.kind, track.codec) for track in tracks.values())
        self.assertEqual(codecs, [("audio", "ac3"), ("video", "hevc")])
        self.assertIn(b"hvc1", film.init())
        dac3 = film.init().split(b"dac3", 1)[1][:3]
        fscod, acmod = dac3[0] >> 6, (dac3[1] >> 3) & 0x07  # 48 kHz, 2/0
        self.assertEqual((fscod, acmod), (0, 2))
        samples = [s for piece in pieces for s in piece]
        self.assertEqual(sum(s.track.kind == "video" for s in samples), 200)
        self.assertEqual(sum(s.track.kind == "audio" for s in samples), 250)
        self.assertEqual({s.duration for s in samples if s.track.kind == "audio"}, {1536})

    def test_h264_with_eac3_51(self):
        data, film = self.film("h264_eac3.mkv")
        tracks, pieces = segments(film)
        audio = next(track for track in tracks.values() if track.kind == "audio")
        self.assertEqual((audio.codec, audio.timescale), ("eac3", 48000))
        dec3 = film.init().split(b"dec3", 1)[1][:5]
        acmod, lfeon = (dec3[3] >> 1) & 0x07, dec3[3] & 0x01  # 3/2 and LFE: 5.1
        self.assertEqual((acmod, lfeon), (7, 1))
        samples = [s for piece in pieces for s in piece if s.track.kind == "audio"]
        self.assertEqual(len(samples), 250)

    def test_a_piece_asked_again_is_not_read_again(self):
        data = (FILMS / "h264_aac.mkv").read_bytes()
        fetcher = MemoryFetcher(data)
        film = Film(fetcher)
        asyncio.run(film.open())
        first = asyncio.run(film.piece(1))
        reads = fetcher.reads
        self.assertEqual(asyncio.run(film.piece(1)), first)
        self.assertEqual(fetcher.reads, reads)
        with self.assertRaises(IndexError):
            asyncio.run(film.piece(2))

    def test_a_piece_reads_little_past_its_end(self):
        """A piece stops once it has its frames: the next keyframe and a sound frame past its
        span, not the rest of the file."""
        data = (FILMS / "h264_aac.mkv").read_bytes()
        fetcher = MemoryFetcher(data, chunk=1024)
        film = Film(fetcher)
        asyncio.run(film.open())
        read = []
        stream = fetcher.stream

        async def counted(start, end):
            async for chunk in stream(start, end):
                read.append(len(chunk))
                yield chunk

        with mock.patch.object(fetcher, "stream", counted):
            asyncio.run(film.piece(0))
        self.assertLess(sum(read), film.pieces[1].position - film.pieces[0].position + 8 * 1024)


AVC = bytes([1, 100, 0, 30, 0xFF, 0xE0, 0])  # avcC: High profile (no parameter sets needed)
ASC = bytes([0x11, 0x90])  # AAC LC, 48 kHz, stereo


def made(tracks, frames=None, cues=True):
    """A small film of `tracks`: a keyframe a second for 8 s (track 1), and AAC frames on
    track 3 when given."""
    clusters = []
    for second in range(8):
        blocks = [mb.simple_block(1, 0, [b"\x00\x00\x00\x01\x65"], True)]
        if frames:
            blocks += [mb.simple_block(3, n * 21, [b"a"], True) for n in range(47)]
        clusters.append(mb.cluster(second * 1000, *blocks))
    return mb.film(tracks, clusters, 8000, cues=cues)


class RefusedTests(unittest.TestCase):
    """What a phone doesn't play is refused with a reason its page words."""

    def refused(self, data) -> str:
        with self.assertRaises(FilmRefused) as caught:
            opened(data)
        return caught.exception.reason

    def test_a_sound_no_phone_plays(self):
        data = made([mb.track(1, 1, "V_MPEG4/ISO/AVC", AVC), mb.track(2, 2, "A_DTS")])
        self.assertEqual(self.refused(data), SOUND)

    def test_another_track_that_plays_is_taken(self):
        data = made(
            [
                mb.track(1, 1, "V_MPEG4/ISO/AVC", AVC),
                mb.track(2, 2, "A_TRUEHD", default=True),
                mb.track(3, 2, "A_AAC", ASC, default=False),
            ],
            frames=True,
        )
        film = opened(data)
        tracks, pieces = segments(film)
        audio = [s for piece in pieces for s in piece if s.track.kind == "audio"]
        self.assertEqual(len(audio), 8 * 47)

    def test_high_10_and_other_video(self):
        high10 = bytes([1, 110, 0, 30, 0xFF, 0xE0, 0])
        self.assertEqual(self.refused(made([mb.track(1, 1, "V_MPEG4/ISO/AVC", high10)])), FORMAT)
        self.assertEqual(self.refused(made([mb.track(1, 1, "V_VP9")])), FORMAT)

    def test_no_cues(self):
        data = made([mb.track(1, 1, "V_MPEG4/ISO/AVC", AVC)], cues=False)
        self.assertEqual(self.refused(data), FORMAT)

    def test_no_matroska(self):
        self.assertEqual(self.refused((FILMS.parent / "smoke.mp4").read_bytes()), FORMAT)

    def test_a_film_without_sound_plays_without(self):
        film = opened(made([mb.track(1, 1, "V_MPEG4/ISO/AVC", AVC)]))
        self.assertEqual(len(film.pieces), 2)
        self.assertNotIn(b"mp4a", film.init())


class MatroskaTests(unittest.TestCase):
    def test_what_goes_as_mkv(self):
        self.assertTrue(is_matroska({"container_extension": "mkv"}, "http://p.test/play?id=7"))
        self.assertTrue(is_matroska({}, "http://p.test/movie/u/p/7.MKV"))
        self.assertTrue(is_matroska({}, "http://p.test/play/movie.php?stream=7.mkv&t=x"))
        self.assertFalse(is_matroska({"container_extension": "mp4"}, "http://p.test/7.mp4"))


class Provider:
    """A provider on localhost serving the fixtures, with byte ranges or (`ranges` False)
    without: each answer whole."""

    def __init__(self, ranges=True):
        self.ranges = ranges
        self.loop = asyncio.new_event_loop()
        self.started = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self.started.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def serve(request):
            name = request.match_info["name"]
            path = FILMS.parent / name if name == "smoke.mp4" else FILMS / name
            if self.ranges:
                return web.FileResponse(path)
            return web.Response(body=path.read_bytes())

        app = web.Application()
        app.router.add_get("/movie/{name}", serve)
        self.runner = web.AppRunner(app)
        self.loop.run_until_complete(self.runner.setup())
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        self.loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]
        self.started.set()
        self.loop.run_forever()

    def url(self, name):
        return f"http://127.0.0.1:{self.port}/movie/{name}"

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)


class HttpTests(unittest.TestCase):
    def open(self, provider, name):
        async def go():
            async with aiohttp.ClientSession() as session:
                film = Film(HttpFetcher(session, provider.url(name), {"User-Agent": "t"}))
                await film.open()
                return film, await film.piece(1)

        return asyncio.run(go())

    def test_a_provider_with_ranges(self):
        provider = Provider()
        self.addCleanup(provider.stop)
        film, piece = self.open(provider, "h264_aac.mkv")
        self.assertEqual(len(film.pieces), 2)
        self.assertEqual(piece[4:8], b"moof")

    def test_a_provider_without_ranges_is_refused(self):
        provider = Provider(ranges=False)
        self.addCleanup(provider.stop)
        with self.assertRaises(FilmRefused) as caught:
            self.open(provider, "h264_aac.mkv")
        self.assertEqual(caught.exception.reason, PROVIDER)


class HomeDoorFilmTests(unittest.TestCase):
    """The home door hands a phone an MKV film as HLS: its playlist, init and pieces."""

    def setUp(self):
        web_dir = Path(tempfile.mkdtemp())
        (web_dir / "index.html").write_text("<!doctype html><title>QiTV remote</title>")
        self.hub = RemoteHub(lambda event: None, web_dir, ports=[0], host="127.0.0.1")
        self.hub.start({"version": 1, "enabled": True, "relay": False})
        self.assertTrue(self.hub.ready.wait(3))
        self.addCleanup(self.hub.stop)
        self.provider = Provider()
        self.addCleanup(self.provider.stop)
        self.sent: list[tuple[str, dict]] = []
        core = self.hub._core
        home = f"http://127.0.0.1:{self.hub.port}/"
        patches = [
            mock.patch.object(core, "send", lambda sid, message: self.sent.append((sid, message))),
            mock.patch.object(core, "home_url", lambda: home),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def open_film(self, name, message=None):
        message = message or {"t": "play", "name": "Harbour Lights", "start": 3}
        self.hub.open_film("s8", "d1", self.provider.url(name), {}, True, message)
        end = time.monotonic() + 10
        while not self.sent and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(self.sent)
        return self.sent[-1]

    def get(self, url):
        async def go():
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    return response.status, response.content_type, await response.read()

        return asyncio.run(go())

    def test_the_phone_gets_the_films_hls_address(self):
        sid, message = self.open_film("h264_aac.mkv")
        self.assertEqual(sid, "s8")
        self.assertEqual((message["name"], message["start"]), ("Harbour Lights", 3))
        url = message["url"]
        self.assertRegex(url, rf"^http://127\.0\.0\.1:{self.hub.port}/film/[\w-]+/index\.m3u8$")
        status, kind, body = self.get(url)
        self.assertEqual((status, kind), (200, "application/vnd.apple.mpegurl"))
        self.assertIn(b"1.m4s", body)
        base = url.rsplit("/", 1)[0]
        status, kind, body = self.get(base + "/init.mp4")
        self.assertEqual((status, kind), (200, "video/mp4"))
        self.assertIn(b"moov", body)
        status, kind, body = self.get(base + "/1.m4s")
        self.assertEqual(status, 200)
        self.assertEqual(body[4:8], b"moof")
        self.assertEqual(self.get(base + "/2.m4s")[0], 404)
        self.assertEqual(self.get(base.rsplit("/", 1)[0] + "/nope/index.m3u8")[0], 404)

    def test_the_tv_playing_again_ends_the_films(self):
        _sid, message = self.open_film("h264_aac.mkv")
        self.hub.drop_films()
        end = time.monotonic() + 3
        while self.get(message["url"])[0] != 404 and time.monotonic() < end:
            time.sleep(0.02)
        self.assertEqual(self.get(message["url"])[0], 404)

    def test_a_film_a_phone_cant_play_says_why(self):
        _sid, message = self.open_film("smoke.mp4")
        self.assertEqual(message, {"t": "play", "failed": True, "why": FORMAT})

    def test_a_phones_next_film_replaces_its_last(self):
        _sid, first = self.open_film("h264_aac.mkv")
        self.sent.clear()
        _sid, second = self.open_film("hevc_ac3.mkv")
        self.assertNotEqual(first["url"], second["url"])
        self.assertEqual(self.get(first["url"])[0], 404)
        self.assertEqual(self.get(second["url"])[0], 200)


if __name__ == "__main__":
    unittest.main()
