import asyncio
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import aiohttp

from services.live.ts import _crc32
from services.remote import recorded as rec
from services.remote.films import FORMAT, SOUND
from services.remote.hub import RemoteHub

FIXTURE = Path(__file__).parent / "fixtures" / "live.ts"
PACKET = 188
TICKS = 90000
FRAME = 3600  # 25 pictures a second
VIDEO, AUDIO, OTHER, PMT_PID = 0x100, 0x101, 0x102, 0x1000

# ---- a transport stream made here --------------------------------------------------------


def packet(pid, payload, *, start=False, cc=0, rai=False):
    head = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF])
    if not rai and len(payload) == 184:
        return head + bytes([0x10 | cc]) + payload
    room = 183 - len(payload)  # the adaptation field's length byte excluded
    field = bytes([room]) + (bytes([0x40 if rai else 0]) + b"\xff" * (room - 1) if room else b"")
    return head + bytes([0x30 | cc]) + field + payload


def section(table_id, extension, body):
    length = 5 + len(body) + 4
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF, extension >> 8, extension & 0xFF])
    data = head + b"\xc1\x00\x00" + body
    return data + _crc32(data).to_bytes(4, "big")


def tables(streams, pcr=VIDEO):
    pat = section(0, 1, bytes([0, 1, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF]))
    body = bytes([0xE0 | (pcr >> 8), pcr & 0xFF, 0xF0, 0])
    for kind, pid, descriptors in streams:
        size = len(descriptors)
        body += bytes([kind, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0 | (size >> 8), size & 0xFF])
        body += descriptors
    pmt = section(2, 1, body)
    return packet(0, b"\x00" + pat, start=True) + packet(PMT_PID, b"\x00" + pmt, start=True)


def pes(stream_id, pts, data):
    stamp = bytes(
        [
            0x21 | ((pts >> 29) & 0x0E),
            (pts >> 22) & 0xFF,
            0x01 | ((pts >> 14) & 0xFE),
            (pts >> 7) & 0xFF,
            0x01 | ((pts << 1) & 0xFE),
        ]
    )
    return b"\x00\x00\x01" + bytes([stream_id, 0, 0, 0x80, 0x80, 5]) + stamp + data


KEY = b"\x00\x00\x00\x01\x09\xf0\x00\x00\x00\x01\x67\x64\x00\x28\x00\x00\x01\x65\x88"
HIGH_10_KEY = b"\x00\x00\x00\x01\x09\xf0\x00\x00\x00\x01\x67\x6e\x00\x28\x00\x00\x01\x65\x88"
PICTURE = b"\x00\x00\x00\x01\x09\xf0\x00\x00\x01\x41\x9a"
AAC = (0x0F, AUDIO, b"")
H264 = (0x1B, VIDEO, b"")


def stream(
    seconds,
    *,
    streams=(H264, AAC),
    first=10 * TICKS,
    key_every=25,
    jump_at=None,
    jump_to=0,
    rai=True,
    key=KEY,
):
    """`seconds` of pictures (25 a second, a keyframe every `key_every`) and sound, behind
    their tables; at picture `jump_at` the timestamps start again at `jump_to`."""
    out = bytearray(tables(streams))
    pts = first
    for n in range(int(seconds * 25)):
        if n == jump_at:
            pts = jump_to
        is_key = n % key_every == 0
        es = key if is_key else PICTURE
        out += packet(VIDEO, pes(0xE0, pts, es), start=True, cc=n % 16, rai=rai and is_key)
        out += packet(AUDIO, pes(0xC0, pts, b"\xff\xf1"), start=True, cc=n % 16)
        out += packet(OTHER, b"\x00" * 184, cc=n % 16)
        if n % 25 == 12:
            out += tables(streams)  # tables come again in the stream, as broadcast
        pts += FRAME
    return bytes(out)


def pids(data):
    return [((data[i + 1] & 0x1F) << 8) | data[i + 2] for i in range(0, len(data), PACKET)]


def lines(text):
    return text.strip().split("\n")


def durations(text):
    return [float(line[8:].rstrip(",")) for line in lines(text) if line.startswith("#EXTINF:")]


def wait(predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        time.sleep(0.01)
    return predicate()


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, data, name="Harbour News 2026-10-26 20.00.ts", age=600):
        path = self.dir / name
        path.write_bytes(data)
        then = time.time() - age  # a recording not written in a while has ended
        os.utime(path, (then, then))
        return path

    def open(self, path, start_s=0.0):
        recorded = rec.Recorded(str(path), start_s=start_s)
        asyncio.run(recorded.open())
        return recorded

    def piece(self, recorded, n):
        return asyncio.run(recorded.piece(n))


class PiecesTests(Files):
    def test_the_fixture_comes_in_pieces_from_keyframes(self):
        recorded = self.open(self.write(FIXTURE.read_bytes()))
        text = recorded.playlist()
        self.assertEqual(lines(text)[0], "#EXTM3U")
        self.assertIn("#EXT-X-PLAYLIST-TYPE:VOD", text)
        self.assertEqual(lines(text)[-1], "#EXT-X-ENDLIST")
        self.assertEqual(len(durations(text)), 2)  # keyframes each second, pieces of 6
        self.assertAlmostEqual(sum(durations(text)), 12.0, delta=0.05)  # from its first keyframe
        first = self.piece(recorded, 0)
        self.assertEqual(len(first) % PACKET, 0)
        self.assertEqual(pids(first)[:2], [0, rec.parse_tables(FIXTURE.read_bytes()).pmt_pid])

    def test_pieces_hold_the_picture_and_one_sound_behind_tables_naming_only_them(self):
        mp2 = (0x03, AUDIO, b"")
        aac = (0x0F, 0x103, b"")
        teletext = (0x06, OTHER, b"\x56\x05eng\x09\x00")
        recorded = self.open(self.write(stream(12, streams=(H264, mp2, aac, teletext))))
        data = self.piece(recorded, 1)
        self.assertEqual(set(pids(data)), {0, PMT_PID, VIDEO})  # no sound packets were made
        self.assertEqual(pids(data)[:2], [0, PMT_PID])
        self.assertEqual(pids(data).count(PMT_PID), 1)  # the stream's own tables left out
        pmt = data[PACKET + 5 :]
        length = ((pmt[1] & 0x0F) << 8) | pmt[2]
        self.assertEqual(_crc32(pmt[: 3 + length]), 0)
        entries = pmt[12 : 3 + length - 4]
        self.assertEqual(entries, bytes([0x1B, 0xE1, 0x00, 0xF0, 0, 0x0F, 0xE1, 0x03, 0xF0, 0]))

    def test_each_piece_starts_on_a_keyframe(self):
        recorded = self.open(self.write(stream(20, rai=False)))  # keyframes told by their SPS
        self.assertEqual(durations(recorded.playlist()), [6.0, 6.0, 6.0, 2.0])
        for n in range(4):
            data = self.piece(recorded, n)
            self.assertIn(KEY, data[2 * PACKET : 3 * PACKET], n)

    def test_the_resume_point_is_where_the_phone_starts(self):
        recorded = self.open(self.write(stream(20)), start_s=7.5)
        self.assertIn("#EXT-X-START:TIME-OFFSET=7.500,PRECISE=YES", recorded.playlist())
        self.assertNotIn("#EXT-X-START", self.open(self.write(stream(20), "b.ts")).playlist())

    def test_a_timestamp_jump_starts_a_piece_after_a_discontinuity(self):
        recorded = self.open(self.write(stream(16, jump_at=200, jump_to=TICKS)))
        text = recorded.playlist()
        self.assertEqual(text.count("#EXT-X-DISCONTINUITY"), 1)
        self.assertEqual(durations(text), [6.0, 2.0, 6.0, 2.0])
        before = lines(text).index("#EXT-X-DISCONTINUITY")
        self.assertEqual(lines(text)[before + 2], "2.ts")

    def test_a_piece_past_the_last_is_not_there(self):
        recorded = self.open(self.write(stream(8)))
        with self.assertRaises(IndexError):
            self.piece(recorded, 2)


class GrowingTests(Files):
    def test_a_recording_still_written_grows_as_the_phone_asks_again(self):
        whole = stream(20)
        cut = len(stream(9))
        path = self.write(whole[:cut], age=0)
        recorded = self.open(path)
        text = recorded.playlist()
        self.assertIn("#EXT-X-PLAYLIST-TYPE:EVENT", text)
        self.assertIn("#EXT-X-START:TIME-OFFSET=0.000", text)  # from its start, not its end
        self.assertNotIn("#EXT-X-ENDLIST", text)
        self.assertEqual(durations(text), [6.0])  # the piece still written isn't listed
        with path.open("ab") as file:
            file.write(whole[cut:])
        later = asyncio.run(recorded.fresh_playlist())
        self.assertEqual(durations(later), [6.0, 6.0, 6.0])
        self.assertTrue(later.startswith(text))  # what the phone has stays as it was
        then = time.time() - 600
        os.utime(path, (then, then))  # the recording ended
        ended = asyncio.run(recorded.fresh_playlist())
        self.assertEqual(durations(ended), [6.0, 6.0, 6.0, 2.0])
        self.assertIn("#EXT-X-PLAYLIST-TYPE:EVENT", ended)  # its type stays
        self.assertEqual(lines(ended)[-1], "#EXT-X-ENDLIST")


class LargeTests(Files):  # 2.8 final review 3, 4, 8
    def patched(self, **values):
        for name, value in values.items():
            patch = mock.patch.object(rec, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def held(self):
        """The reading behind the playlist waits for `gate`; its thread goes in `threads`."""
        gate, threads = threading.Event(), []
        real = rec.Recorded._read_behind

        def held(recorded):
            threads.append(threading.current_thread())
            gate.wait(5)
            real(recorded)

        patch = mock.patch.object(rec.Recorded, "_read_behind", held)
        patch.start()
        self.addCleanup(patch.stop)
        return gate, threads

    def test_a_large_recording_answers_before_it_is_read_whole(self):
        # Read whole, a recording of hours would keep the phone waiting past its patience:
        # once read past where the phone starts it answers, and the rest is read behind.
        self.patched(OPEN_PREFER_S=0.0, BATCH=PACKET * 600)
        gate, _threads = self.held()
        recorded = self.open(self.write(stream(60)))
        first = recorded.playlist()
        self.assertIn("#EXT-X-PLAYLIST-TYPE:EVENT", first)
        self.assertNotIn("#EXT-X-ENDLIST", first)
        self.assertTrue(0 < len(durations(first)) < 10, durations(first))
        gate.set()
        self.assertTrue(wait(lambda: "#EXT-X-ENDLIST" in recorded.playlist()))
        last = recorded.playlist()
        self.assertEqual(durations(last), [6.0] * 10)
        self.assertTrue(last.startswith(first))  # what the phone has stays as it was

    def test_it_reads_on_to_where_the_phone_starts(self):
        self.patched(OPEN_PREFER_S=0.0, BATCH=PACKET * 600)
        self.held()
        recorded = self.open(self.write(stream(60)), start_s=40.0)
        self.assertGreater(sum(durations(recorded.playlist())), 40.0)

    def test_closing_stops_the_reading(self):
        self.patched(OPEN_PREFER_S=0.0, BATCH=PACKET * 600)
        gate, threads = self.held()
        recorded = self.open(self.write(stream(60)))
        recorded.close()
        gate.set()
        threads[0].join(5)
        self.assertFalse(threads[0].is_alive())
        self.assertNotIn("#EXT-X-ENDLIST", recorded.playlist())  # it read no further

    def test_one_closed_before_it_opens_reads_nothing(self):
        recorded = rec.Recorded(str(self.write(stream(8))))
        recorded.close()
        with self.assertRaises(OSError):
            asyncio.run(recorded.open())

    def test_a_long_gop_comes_in_pieces_the_target_holds(self):
        # Keyframes 15 s apart: pieces start at pictures in between, so a growing playlist's
        # target duration (which may not change) holds every piece.
        text = self.open(self.write(stream(60, key_every=25 * 15))).playlist()
        self.assertTrue(all(d < rec.EVENT_TARGET_S for d in durations(text)), durations(text))
        self.assertAlmostEqual(sum(durations(text)), 60.0, places=1)

    def test_a_piece_too_large_is_left_out(self):
        # Its keyframes lost (only another stream for a while): the piece over it would be
        # read whole into memory; it is left out, and the playlist goes on after a break.
        self.patched(MAX_PIECE_BYTES=200_000)
        gap = packet(OTHER, b"\x00" * 184) * 2000
        data = stream(12) + gap + stream(12, first=22 * TICKS)
        recorded = self.open(self.write(data))
        text = recorded.playlist()
        self.assertEqual(durations(text), [6.0, 6.0, 6.0])
        self.assertIn("#EXT-X-DISCONTINUITY", text)
        self.assertTrue(all(p.end - p.start <= 200_000 for p in recorded._pieces))

    def test_the_tv_says_whether_it_is_still_written(self):
        # A stall (nothing written for minutes) does not end a recording the TV still writes.
        still = [True]
        path = self.write(stream(9), age=600)
        recorded = rec.Recorded(str(path), writing=lambda: still[0])
        asyncio.run(recorded.open())
        self.assertNotIn("#EXT-X-ENDLIST", asyncio.run(recorded.fresh_playlist()))
        still[0] = False
        self.assertIn("#EXT-X-ENDLIST", asyncio.run(recorded.fresh_playlist()))


class RefusedTests(Files):
    def refused(self, data):
        with self.assertRaises(rec.FilmRefused) as caught:
            self.open(self.write(data))
        return caught.exception.reason

    def test_hevc_and_mpeg_2_pictures(self):
        self.assertEqual(self.refused(stream(8, streams=((0x24, VIDEO, b""), AAC))), FORMAT)
        self.assertEqual(self.refused(stream(8, streams=((0x02, VIDEO, b""), AAC))), FORMAT)

    def test_high_10(self):
        self.assertEqual(self.refused(stream(8, key=HIGH_10_KEY, rai=False)), FORMAT)

    def test_a_sound_no_phone_plays(self):
        self.assertEqual(self.refused(stream(8, streams=(H264, (0x03, AUDIO, b"")))), SOUND)

    def test_ac3_and_eac3_as_dvb_and_atsc_name_them(self):
        for sound in (
            (0x06, AUDIO, b"\x6a\x01\x00"),
            (0x06, AUDIO, b"\x7a\x01\x00"),
            (0x81, AUDIO, b""),
            (0x87, AUDIO, b""),
        ):
            recorded = self.open(self.write(stream(8, streams=(H264, sound))))
            self.assertIn(AUDIO, pids(self.piece(recorded, 0)), sound)

    def test_no_tables(self):
        self.assertEqual(self.refused(b"\x47" + b"\x1f\xff\x10" + b"\xff" * 184), FORMAT)


class HomeDoorRecordingTests(unittest.TestCase):
    """The home door hands a phone a recording as HLS: its playlist and pieces."""

    def setUp(self):
        web_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, web_dir, True)
        (web_dir / "index.html").write_text("<!doctype html><title>QiTV remote</title>")
        self.path = web_dir / "Harbour News 2026-10-26 20.00.ts"
        self.path.write_bytes(stream(20))
        os.utime(self.path, (time.time() - 600, time.time() - 600))
        self.hub = RemoteHub(lambda event: None, web_dir, ports=[0], host="127.0.0.1")
        self.hub.start({"version": 1, "enabled": True, "relay": False})
        self.assertTrue(self.hub.ready.wait(3))
        self.addCleanup(self.hub.stop)
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

    def open(self, path, message=None):
        message = message or {"t": "play", "name": "Harbour News", "start": 7}
        self.hub.open_recording("s8", "d1", str(path), message)
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

    def test_the_phone_gets_the_recordings_hls_address(self):
        sid, message = self.open(self.path)
        self.assertEqual(sid, "s8")
        self.assertEqual((message["name"], message["start"]), ("Harbour News", 7))
        url = message["url"]
        self.assertRegex(url, rf"^http://127\.0\.0\.1:{self.hub.port}/rec/[\w-]+/index\.m3u8$")
        status, kind, body = self.get(url)
        self.assertEqual((status, kind), (200, "application/vnd.apple.mpegurl"))
        self.assertIn(b"#EXT-X-START:TIME-OFFSET=7.000", body)
        self.assertIn(b"3.ts", body)
        base = url.rsplit("/", 1)[0]
        status, kind, body = self.get(base + "/3.ts")
        self.assertEqual((status, kind), (200, "video/mp2t"))
        self.assertEqual(body[0], 0x47)
        self.assertEqual(self.get(base + "/4.ts")[0], 404)
        self.assertEqual(self.get(base + "/init.mp4")[0], 404)  # a film's route: no film here
        film_base = base.replace("/rec/", "/film/")
        self.assertEqual(self.get(film_base + "/index.m3u8")[0], 404)

    def test_the_tv_playing_again_ends_the_recordings(self):
        _sid, message = self.open(self.path)
        self.hub.drop_films()
        end = time.monotonic() + 3
        while self.get(message["url"])[0] != 404 and time.monotonic() < end:
            time.sleep(0.02)
        self.assertEqual(self.get(message["url"])[0], 404)

    def test_a_recording_a_phone_cant_play_says_why(self):
        hevc = self.path.with_name("hevc.ts")
        hevc.write_bytes(stream(8, streams=((0x24, VIDEO, b""), AAC)))
        self.assertEqual(self.open(hevc)[1], {"t": "play", "failed": True, "why": "rec_format"})

    def test_a_phone_removed_loses_its_recording(self):  # 2.8 final review 10
        _sid, message = self.open(self.path)
        with mock.patch.object(self.hub._core.store, "remove_device", return_value=True):
            self.hub.remove_device("d1")
            self.assertTrue(wait(lambda: self.get(message["url"])[0] == 404))

    def test_an_opening_dropped_stops_reading(self):  # 2.8 final review 3
        closed = threading.Event()

        async def slow(recorded):
            await asyncio.sleep(5)

        with (
            mock.patch.object(rec.Recorded, "open", slow),
            mock.patch.object(rec.Recorded, "close", lambda recorded: closed.set()),
        ):
            self.hub.open_recording("s8", "d1", str(self.path), {"t": "play", "start": 0})
            time.sleep(0.1)
            self.hub.forget_film("d1")
            self.assertTrue(closed.wait(3))

    def test_a_recording_gone_from_the_disk_says_it_did_not_open(self):
        gone = self.path.with_name("gone.ts")
        self.assertEqual(self.open(gone)[1], {"t": "play", "failed": True, "why": "rec_open"})


if __name__ == "__main__":
    unittest.main()
