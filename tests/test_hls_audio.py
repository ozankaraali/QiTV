"""Tests for AudioWeaver (services/live/hls_audio.py): a separate HLS audio rendition woven into
the video variant's TS, on synthetic segments: `ts_fixtures.make_stream` without audio as the
video (a PCR on each picture), an audio-only TS, and packed ADTS with an ID3 timestamp."""

from __future__ import annotations

import unittest

from services.live import ts
from services.live.hls_audio import _LEAD_TICKS, AudioWeaver, video_start
from services.live.ts import PACKET, program_has_audio, pts_delta
from tests.ts_fixtures import CLOCK, PMT_PID, VIDEO_PID, crc32_mpeg, make_stream, pat, pes_packets

AUDIO_TICKS = 1024 * CLOCK // 48000  # one AAC frame at 48 kHz: 1920


def packets(data: bytes) -> list[bytes]:
    return [data[at : at + PACKET] for at in range(0, len(data) - PACKET + 1, PACKET)]


def pid_of(p: bytes) -> int:
    return ((p[1] & 0x1F) << 8) | p[2]


def adts(index: int) -> bytes:
    """An ADTS AAC frame (MPEG-4, 48 kHz, stereo) only frame `index` has."""
    body = bytes(0x80 | ((index >> (7 * k)) & 0x7F) for k in range(3)) + b"\x21" * 40
    length = 7 + len(body)
    head = bytes(
        [
            0xFF,
            0xF1,
            0x40 | 3 << 2,
            0x80 | length >> 11,
            (length >> 3) & 0xFF,
            (length & 7) << 5 | 0x1F,
            0xFC,
        ]
    )
    return head + body


def audio_pmt(pid: int, stream_type: int = 0x0F) -> bytes:
    body = (0xE000 | pid).to_bytes(2, "big") + b"\xf0\x00"
    body += bytes([stream_type]) + (0xE000 | pid).to_bytes(2, "big") + b"\xf0\x03\x0a\x01\x00"
    head = bytes([0x02]) + (0xB000 | (5 + len(body) + 4)).to_bytes(2, "big")
    section = head + (1).to_bytes(2, "big") + bytes([0xC1, 0, 0]) + body
    section += crc32_mpeg(section).to_bytes(4, "big")
    payload = b"\x00" + section
    return (
        bytes([0x47, 0x40 | PMT_PID >> 8, PMT_PID & 0xFF, 0x10])
        + payload
        + b"\xff" * (184 - len(payload))
    )


def audio_ts(seconds: float, pid: int, start: int = 900000) -> bytes:
    """An audio-only TS segment: PAT, PMT, then one ADTS frame per PES from `start`."""
    out, cc = [pat(), audio_pmt(pid)], 0
    for k in range(int(seconds * CLOCK) // AUDIO_TICKS):
        pes, cc = pes_packets(pid, 0xC0, adts(k), start + k * AUDIO_TICKS, cc_start=cc)
        out.extend(pes)
    return b"".join(out)


def packed(seconds: float, start: int | None) -> bytes:
    """Packed ADTS: an ID3 tag dating the first frame (none when `start` is None), then frames."""
    out = b""
    if start is not None:
        owner = b"com.apple.streaming.transportStreamTimestamp\x00" + start.to_bytes(8, "big")
        frame = b"PRIV" + len(owner).to_bytes(4, "big") + b"\x00\x00" + owner
        out += b"ID3\x04\x00\x00" + bytes((len(frame) >> s) & 0x7F for s in (21, 14, 7, 0)) + frame
    for k in range(int(seconds * CLOCK) // AUDIO_TICKS):
        out += adts(k)
    return out


def pmt_streams(data: bytes) -> list[tuple[int, int]]:
    """(stream type, PID) of the first PMT's streams; its CRC must hold."""
    for p in packets(data):
        if pid_of(p) == PMT_PID and p[1] & 0x40:
            section = ts._section_at(p, 0)
            assert section is not None and ts._crc32(section) == 0
            end, i, out = len(section) - 4, 12 + (((section[10] & 0x0F) << 8) | section[11]), []
            while i + 5 <= end:
                out.append((section[i], ((section[i + 1] & 0x1F) << 8) | section[i + 2]))
                i += 5 + (((section[i + 3] & 0x0F) << 8) | section[i + 4])
            return out
    return []


def audio_starts(data: bytes, pid: int) -> list[tuple[int, int | None]]:
    """(PTS, the last PCR before it) of each audio PES start on `pid`."""
    out, pcr = [], None
    for p in packets(data):
        if p[3] & 0x20 and p[4] >= 7 and p[5] & 0x10:
            pcr = ts._pcr_base(p, 6)
        if pid_of(p) == pid and p[1] & 0x40:
            out.append((ts._pes_times(p, 0)[0], pcr))
    return out


class AudioWeaverTests(unittest.TestCase):
    def setUp(self):
        self.video = make_stream(4, audio=False)  # PTS from 900000, a PCR on each picture
        self.assertFalse(program_has_audio(self.video))

    def assert_woven(self, out: bytes, pid: int, first: int, count: int) -> None:
        # The video's packets as they came, in order (but its PMT); the audio's PES each once,
        # on its PID, its PTS kept, woven in before the video's clock reaches it.
        self.assertEqual(
            [p for p in packets(out) if pid_of(p) not in (pid, PMT_PID)],
            [p for p in packets(self.video) if pid_of(p) != PMT_PID],
        )
        self.assertIn((0x0F, pid), pmt_streams(out))
        self.assertTrue(program_has_audio(out))
        starts = audio_starts(out, pid)
        self.assertEqual(
            [pts for pts, _ in starts], [first + k * AUDIO_TICKS for k in range(count)]
        )
        for pts, pcr in starts:
            if pcr is not None:
                self.assertGreaterEqual(pts_delta(pcr, pts), 0)  # never late
                self.assertLessEqual(pts_delta(pcr, pts), _LEAD_TICKS + 4000)  # nor long early

    def test_a_ts_audio_segment_is_woven_in_on_a_pid_of_its_own(self):
        out = AudioWeaver().weave(self.video, audio_ts(4, 0x102))
        self.assert_woven(out, 0x102, 900000, int(4 * CLOCK) // AUDIO_TICKS)

    def test_an_audio_pid_the_video_uses_moves_and_stays_moved(self):
        # Two packagers' renditions both number their stream 0x100, the video's PID.
        weaver = AudioWeaver()
        out = weaver.weave(self.video, audio_ts(4, VIDEO_PID))
        self.assert_woven(out, 0x101, 900000, int(4 * CLOCK) // AUDIO_TICKS)
        again = weaver.weave(self.video, audio_ts(4, VIDEO_PID))
        self.assertIn((0x0F, 0x101), pmt_streams(again))
        self.assertEqual(weaver.woven, 2)

    def test_packed_adts_is_dated_by_its_id3_tag_and_then_follows_on(self):
        weaver = AudioWeaver()
        count = int(4 * CLOCK) // AUDIO_TICKS
        out = weaver.weave(self.video, packed(4, 900000))
        self.assert_woven(out, 0x101, 900000, count)
        # The next segment without a tag: its frames follow the last one's.
        later = weaver.weave(self.video, packed(1, None))
        starts = audio_starts(later, 0x101)
        self.assertEqual(starts[0][0], 900000 + count * AUDIO_TICKS)
        counters = [p[3] & 0x0F for p in packets(out + later) if pid_of(p) == 0x101]
        self.assertEqual(counters, [k & 0x0F for k in range(len(counters))])  # they run on

    def test_a_pmt_packet_stuffed_by_its_adaptation_field_takes_the_audio(self):
        # One packager fills its PMT packet with an adaptation field of stuffing: no room after
        # the section, until that field is left out.
        def stuffed(p: bytes) -> bytes:
            if pid_of(p) != PMT_PID or not p[1] & 0x40:
                return p
            content = p[4 : 5 + 3 + (((p[6] & 0x0F) << 8) | p[7])]  # pointer and section
            room = 184 - 1 - len(content)
            field = bytes([room, 0x00]) + b"\xff" * (room - 1)
            return bytes([p[0], p[1], p[2], 0x30 | (p[3] & 0x0F)]) + field + content

        self.video = b"".join(stuffed(p) for p in packets(self.video))
        out = AudioWeaver().weave(self.video, audio_ts(4, 0x102))
        self.assert_woven(out, 0x102, 900000, int(4 * CLOCK) // AUDIO_TICKS)

    def test_unreadable_audio_leaves_the_video_as_it_came(self):
        for audio in (b"", b"\x00" * 1000, packed(1, None), b"\x47" + b"\x00" * 1000):
            with self.subTest(audio=audio[:4]):
                self.assertEqual(AudioWeaver().weave(self.video, audio), self.video)


class QueuedAudioTests(unittest.TestCase):
    """The audio queue `HlsSource` matches renditions by time with (2.3)."""

    def setUp(self):
        self.video = make_stream(4, audio=False)  # PTS from 900000
        self.later = make_stream(4, audio=False, pts_start=900000 + 4 * CLOCK)

    def test_each_video_segment_takes_the_audio_due_before_its_end(self):
        weaver = AudioWeaver()
        self.assertTrue(weaver.add(audio_ts(6, 0x102)))  # 6 s of audio for 4 s of video
        start = video_start(self.video)
        self.assertEqual(start, 900000)
        first = weaver.weave_due(self.video, start + 4 * CLOCK)
        due = int(4 * CLOCK) // AUDIO_TICKS + 1  # those that start before the end
        self.assertEqual(
            [pts for pts, _ in audio_starts(first, 0x102)],
            [900000 + k * AUDIO_TICKS for k in range(due)],
        )
        self.assertTrue(weaver.reaches(start + 4 * CLOCK))
        self.assertFalse(weaver.reaches(start + 8 * CLOCK))
        second = weaver.weave_due(self.later, start + 8 * CLOCK)
        rest = [pts for pts, _ in audio_starts(second, 0x102)]
        self.assertEqual(rest[0], 900000 + due * AUDIO_TICKS)  # on from where the first ended
        self.assertEqual(len(rest), int(6 * CLOCK) // AUDIO_TICKS - due)
        self.assertEqual(weaver._queue, [])
        empty = weaver.weave_due(self.later, start + 8 * CLOCK)
        self.assertIn((0x0F, 0x102), pmt_streams(empty))  # still listed, none due

    def test_overlapping_segments_queue_each_frame_once_and_a_new_timeline_restarts(self):
        weaver = AudioWeaver()
        base = 200 * CLOCK
        weaver.add(audio_ts(4, 0x102, start=base))
        weaver.add(audio_ts(4, 0x102, start=base + 2 * CLOCK))  # its last 2 s again
        times = [pts for pts, _ in weaver._queue]
        self.assertEqual(times, sorted(set(times)))
        self.assertEqual(len(times), len({pts for pts in times}))
        self.assertGreater(times[-1], base + 5 * CLOCK)
        weaver.add(audio_ts(1, 0x102, start=100 * CLOCK))  # over a minute back: a new timeline
        self.assertEqual(weaver._queue[-1][0] // CLOCK, 100)
        weaver.add(audio_ts(4, 0x102, start=97 * CLOCK))  # 3 s back: an overlap, left out
        self.assertEqual(weaver._queue[-1][0] // CLOCK, 100)

    def test_audio_too_late_for_the_video_is_dropped(self):
        weaver = AudioWeaver()
        weaver.add(audio_ts(4, 0x102))
        weaver.drop(900000 + 2 * CLOCK)
        self.assertEqual(weaver._queue[0][0], 900000 + (2 * CLOCK // AUDIO_TICKS + 1) * AUDIO_TICKS)
        weaver.reset()
        self.assertEqual(weaver._queue, [])

    def test_peek_leaves_packed_audio_where_it_was(self):
        weaver = AudioWeaver()
        weaver.add(packed(1, 900000))
        self.assertEqual(
            weaver.peek(packed(1, None)), (900000 + 46 * AUDIO_TICKS, 900000 + 91 * AUDIO_TICKS)
        )
        weaver.add(packed(1, None))  # still follows on from the first, not from what peek read
        self.assertEqual(weaver._queue[46][0], 900000 + 46 * AUDIO_TICKS)

    def test_covered_is_the_video_the_queued_audio_can_go_with(self):
        weaver = AudioWeaver()
        self.assertEqual(weaver.covered(self.video)[0], 2 * PACKET)  # no audio: the tables alone
        weaver.add(audio_ts(2, 0x102))  # 2 s of the 4 s
        cut, due = weaver.covered(self.video)
        self.assertTrue(0 < cut < len(self.video) and cut % PACKET == 0)
        self.assertGreater(pts_delta(900000 + 2 * CLOCK - AUDIO_TICKS, due), 0)  # past the audio
        self.assertLessEqual(pts_delta(900000 + 2 * CLOCK, due), 3600)  # by a picture at most
        self.assertEqual(weaver.covered(self.video, since=len(self.video))[0], len(self.video))
        weaver.add(audio_ts(6, 0x102))  # past the video and the lead
        self.assertEqual(weaver.covered(self.video), (len(self.video), None))

    def test_the_audio_pids_counters_run_on_whatever_was_left_out(self):
        # The replays of 17 public fMP4 channels showed counter breaks on the woven audio:
        # units left out (overlaps, audio too late) and renditions whose counters restart each
        # segment. The weaver numbers the packets it writes there itself.
        weaver = AudioWeaver()
        weaver.add(audio_ts(4, 0x102))
        weaver.add(audio_ts(4, 0x102, start=900000 + 2 * CLOCK))  # 2 s again; counters from 0
        first = weaver.weave_due(self.video, 900000 + 4 * CLOCK)
        second = weaver.weave_due(self.later, 900000 + 8 * CLOCK)
        counters = [p[3] & 0x0F for p in packets(first + second) if pid_of(p) == 0x102]
        self.assertGreater(len(counters), 100)
        self.assertEqual(counters, [k & 0x0F for k in range(len(counters))])

    def test_video_start_is_the_earliest_picture(self):
        self.assertEqual(video_start(self.later), 900000 + 4 * CLOCK)
        self.assertIsNone(video_start(audio_ts(1, 0x102)))


if __name__ == "__main__":
    unittest.main()
