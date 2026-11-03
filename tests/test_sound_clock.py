"""Tests for SoundClock (services/live/ts.py): a sound whose stamps drift from the pictures' is
restamped from its own length, on synthetic streams: 25 pictures a second (H.264, PCR on the
video) and 48 kHz sound, MPEG audio Layer II (1152 samples a frame, 576 bytes at 192 kb/s) or
ADTS AAC (1024 samples), one frame a PES, interleaved by their real time."""

from __future__ import annotations

import unittest

from services.live.ts import _PTS_MODULO, _SOUND_WANDER_S, PACKET, SoundClock
from tests.ts_fixtures import (
    AUDIO_PID,
    PMT_PID,
    SYNC,
    VIDEO_PID,
    _pcr_bytes,
    _psi_packet,
    h264_access_unit,
    pat,
    pes_packets,
)

MPEG_AUDIO, AAC = 0x03, 0x0F
START = 900_000  # the pictures' first PTS
PICTURE = 3600  # 25 a second, in 90 kHz ticks
FRAME = {MPEG_AUDIO: 2160, AAC: 1920}  # a sound frame's length in ticks at 48 kHz


def mpeg_frame() -> bytes:
    """MPEG-1 Layer II, 192 kb/s, 48 kHz, no CRC, no padding: 576 bytes."""
    return b"\xff\xfd\xa4\x00" + b"\x55" * 572


def adts_frame(size: int = 400) -> bytes:
    """ADTS AAC LC, 48 kHz, stereo, one raw data block."""
    head = bytes(
        [
            0xFF,
            0xF1,
            0x4C,
            0x80 | (size >> 11) & 0x03,
            (size >> 3) & 0xFF,
            ((size & 0x07) << 5) | 0x1F,
            0xFC,
        ]
    )
    return head + b"\x55" * (size - 7)


def program_map(audio_type: int, cc: int) -> bytes:
    streams = bytes([0x1B]) + (0xE000 | VIDEO_PID).to_bytes(2, "big") + b"\xf0\x00"
    streams += bytes([audio_type]) + (0xE000 | AUDIO_PID).to_bytes(2, "big") + b"\xf0\x00"
    body = (0xE000 | VIDEO_PID).to_bytes(2, "big") + b"\xf0\x00" + streams
    head = bytes([0x02]) + (0xB000 | (5 + len(body) + 4)).to_bytes(2, "big")
    return _psi_packet(PMT_PID, head + (1).to_bytes(2, "big") + bytes([0xC1, 0, 0]) + body, cc)


def pcr_packet(base: int) -> bytes:
    """A packet of the pictures' PID with a PCR and no payload."""
    field = bytes([0x10]) + _pcr_bytes(base)
    field += b"\xff" * (183 - len(field))
    return bytes([SYNC, VIDEO_PID >> 8, VIDEO_PID & 0xFF, 0x20, 183]) + field


def stream(
    seconds: float,
    *,
    audio_type: int = MPEG_AUDIO,
    rate: float = 1.0,
    offset: int = 0,
    video_start: int = START,
    picture_every: int = 1,
    stall: tuple[float, float] | None = None,
    stall_pcr: bool = True,
    lose_every: int = 0,
) -> bytes:
    """`seconds` of the program; the sound's PTS step `rate` times its frames' length and
    start `offset` ticks from the pictures' (a provider's clock). A picture each
    `picture_every` 25ths of a second (a PCR with each, and one every 0.1 s between); none
    within `stall` (seconds), and with `stall_pcr` no PCR either. With `lose_every`, the
    provider loses one sound frame in that many (its stamps go on as if it came)."""
    frame_ticks = FRAME[audio_type]
    frame = mpeg_frame() if audio_type == MPEG_AUDIO else adts_frame()
    out: list[bytes] = []
    vcc = acc = psi = 0
    audio_i = 0
    for i in range(round(seconds * 25)):
        real = i * PICTURE
        if i % 12 == 0:
            out += [pat(cc=psi), program_map(audio_type, psi)]
            psi = (psi + 1) & 0x0F
        pts = (video_start + real) % _PTS_MODULO
        stalled = stall is not None and stall[0] <= real / 90000 < stall[1]
        if stalled or i % picture_every:
            if (not stalled or not stall_pcr) and i % 2 == 0:
                out.append(pcr_packet(pts))
        else:
            ps, vcc = pes_packets(
                VIDEO_PID, 0xE0, h264_access_unit(i % 25 == 0), pts, cc_start=vcc, pcr=pts
            )
            out += ps
        while audio_i * frame_ticks <= real:
            if not lose_every or audio_i % lose_every != lose_every - 1:
                stamp = (video_start + offset + round(audio_i * frame_ticks * rate)) % _PTS_MODULO
                ps, acc = pes_packets(AUDIO_PID, 0xC0, frame, stamp, cc_start=acc)
                out += ps
            audio_i += 1
    return b"".join(out)


def stamps(data: bytes) -> list[tuple[int, int]]:
    """(PID, PTS) of each PES start, in order."""
    found = []
    for at in range(0, len(data) - PACKET + 1, PACKET):
        p = data[at : at + PACKET]
        pid = ((p[1] & 0x1F) << 8) | p[2]
        if not p[1] & 0x40 or pid not in (VIDEO_PID, AUDIO_PID):
            continue
        start = 5 + p[4] if p[3] & 0x20 else 4
        b = p[start + 9 : start + 14]
        found.append(
            (pid, ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1)
        )
    return found


def gaps(data: bytes) -> list[float]:
    """Each sound PES's PTS less the latest picture's before it, in seconds."""
    video, out = None, []
    for pid, pts in stamps(data):
        if pid == VIDEO_PID:
            video = pts
        elif video is not None:
            d = (pts - video) % _PTS_MODULO
            out.append((d - _PTS_MODULO if d >= _PTS_MODULO >> 1 else d) / 90000)
    return out


def sound_steps(data: bytes) -> set[int]:
    pts = [p for pid, p in stamps(data) if pid == AUDIO_PID]
    return {(b - a) % _PTS_MODULO for a, b in zip(pts, pts[1:])}


def through(clock: SoundClock, data: bytes, chunk: int = 188 * 7 + 5) -> bytes:
    out = b"".join(clock.feed(data[at : at + chunk]) for at in range(0, len(data), chunk))
    return out + clock.flush()


class SoundClockTests(unittest.TestCase):
    def test_a_healthy_sound_passes_untouched(self):
        for audio_type in (MPEG_AUDIO, AAC):
            data = stream(90.0, audio_type=audio_type, offset=-1800)
            clock = SoundClock()
            self.assertEqual(through(clock, data), data)
            self.assertEqual(list(clock.restamped), [])

    def test_a_sound_stamped_slow_is_restamped_from_its_length_once_measured(self):
        # One provider's MPEG audio: stamps 2.2% slow, its sound in real time.
        data = stream(120.0, rate=0.978)
        clock = SoundClock()
        out = through(clock, data)
        [(pid, drift, gap)] = list(clock.restamped)
        self.assertEqual(pid, AUDIO_PID)
        self.assertAlmostEqual(drift, -0.022, delta=0.001)
        self.assertLess(abs(gap), 1.0)  # near the pictures still: anchored where it was in step
        after = gaps(out)[-40 * 60 :]  # the last minute
        self.assertLess(max(abs(g) for g in after), 0.1)  # its drift since the window began, gone
        self.assertLess(abs(after[-1] - after[0]), 0.1)  # no drift any more
        tail = out[len(out) * 3 // 4 :]
        self.assertEqual(sound_steps(tail[tail.find(b"\x47") :]) - {2160}, set())

    def test_a_sound_minutes_from_the_pictures_is_put_with_them_at_once(self):
        # Seen on the same channel after hours: its sound 466 s behind its pictures.
        data = stream(20.0, rate=0.978, offset=-466 * 90000)
        clock = SoundClock()
        out = through(clock, data)
        self.assertEqual(len(clock.restamped), 1)
        self.assertAlmostEqual(clock.restamped[0][2], -466.0, delta=1.0)
        found = gaps(out)
        late = [g for g in found if g < -1.0]
        self.assertLess(len(late), 42 * 3)  # restamped within its first 3 s
        self.assertLess(max(abs(g) for g in found[42 * 3 :]), 0.1)

    def test_sparse_or_stalled_pictures_leave_a_healthy_sound_alone(self):
        # The pictures' clock is the PCR (ten a second) plus how far the pictures run ahead of
        # it: a picture every 8 s, or none for 10 s, is no distance (seen restamped before).
        for data in (
            stream(120.0, picture_every=200),
            stream(60.0, stall=(20.0, 30.0)),
            stream(60.0, stall=(20.0, 30.0), stall_pcr=False),
        ):
            clock = SoundClock()
            self.assertEqual(through(clock, data), data)
            self.assertEqual(list(clock.restamped), [])

    def test_a_sound_muxed_seconds_ahead_of_its_pictures_is_left_alone(self):
        data = stream(60.0, offset=6 * 90000)  # sent 6 s before it plays: a mux may
        clock = SoundClock()
        self.assertEqual(through(clock, data), data)

    def test_sound_whose_frames_are_never_counted_keeps_a_bounded_window(self):
        data = bytearray(stream(200.0, audio_type=AAC))
        for at in range(0, len(data), PACKET):  # declared ADTS, its bytes another framing
            if ((data[at + 1] & 0x1F) << 8) | data[at + 2] == AUDIO_PID:
                body = bytes(data[at + 4 : at + PACKET])
                data[at + 4 : at + PACKET] = body.replace(b"\xff\xf1", b"\x56\xe0")
        clock = SoundClock()
        through(clock, bytes(data))
        track = clock._tracks[AUDIO_PID]
        self.assertLessEqual(len(track.seen), 4096)

    def test_after_a_discontinuity_sound_before_the_clock_is_dropped_not_misstamped(self):
        clock = SoundClock()
        through(clock, stream(10.0, rate=0.978, offset=-466 * 90000))
        clock.discontinuity()
        # The new run's sound comes 0.3 s before any picture or PCR.
        later = stream(
            10.0, rate=0.978, offset=-500 * 90000, video_start=5_000_000, stall=(0.0, 0.3)
        )
        out = through(clock, later)
        self.assertLess(max(abs(g) for g in gaps(out)), 0.1)  # none passed minutes off
        nulls = [
            at
            for at in range(0, len(out), PACKET)
            if out[at + 1] & 0x1F == 0x1F and out[at + 2] == 0xFF
        ]
        self.assertTrue(nulls)  # dropped, not passed 500 s off
        first_pcr = out.index(bytes([SYNC, VIDEO_PID >> 8, VIDEO_PID & 0xFF]))
        self.assertLess(nulls[-1], first_pcr + 10 * PACKET)  # only until the clock came

    def test_after_a_discontinuity_before_its_clock_lost_frames_are_still_pulled_back(self):
        # 2.9, the review: restamped for drift alone, anchored on the new run's first sound
        # before any PCR came, its distance from the pictures was never noted: a provider
        # losing frames then left it 5 s behind them (VLC drops such sound as late).
        clock = SoundClock()
        through(clock, stream(40.0, rate=0.978))
        clock.discontinuity()
        later = stream(40.0, rate=0.978, video_start=5_000_000, stall=(0.0, 0.3), lose_every=8)
        out = through(clock, later)
        self.assertLess(max(abs(g) for g in gaps(out)[-30:]), _SOUND_WANDER_S + 0.1)

    def test_aac_too(self):
        data = stream(20.0, audio_type=AAC, rate=1.03, offset=40 * 90000)
        clock = SoundClock()
        out = through(clock, data)
        self.assertEqual(len(clock.restamped), 1)
        self.assertLess(max(abs(g) for g in gaps(out)[47 * 3 :]), 0.1)
        tail = out[len(out) // 2 :]
        self.assertEqual(sound_steps(tail[tail.find(b"\x47") :]) - {1920}, set())

    def test_after_a_discontinuity_it_is_anchored_on_the_new_pictures_clock(self):
        clock = SoundClock()
        through(clock, stream(10.0, rate=0.978, offset=-466 * 90000))
        self.assertEqual(len(clock.restamped), 1)
        clock.discontinuity()
        later = stream(10.0, rate=0.978, offset=-500 * 90000, video_start=5_000_000)
        out = through(clock, later)
        self.assertLess(max(abs(g) for g in gaps(out)), 0.1)
        self.assertEqual(len(clock.restamped), 1)  # restamped all along, not judged again

    def test_a_step_in_the_providers_stamps_anchors_it_again(self):
        clock = SoundClock()
        first = stream(10.0, rate=0.978, offset=-466 * 90000)
        # The provider restarts: its sound's stamps jump by 30 s, the pictures go on.
        second = stream(10.0, rate=0.978, offset=-436 * 90000, video_start=START + 900000)
        out = through(clock, first + second)
        self.assertLess(max(abs(g) for g in gaps(out)[42 * 3 :]), 0.1)

    def test_a_stream_joined_inside_a_frame_is_counted_from_the_next_pes(self):
        data = stream(20.0, rate=0.978, offset=-466 * 90000)
        sound = [
            at
            for at in range(0, len(data), PACKET)
            if ((data[at + 1] & 0x1F) << 8) | data[at + 2] == AUDIO_PID and not data[at + 1] & 0x40
        ]
        cut = data[sound[0] :]  # starts with a sound packet inside a frame
        clock = SoundClock()
        out = through(clock, cut)
        self.assertEqual(len(clock.restamped), 1)
        self.assertLess(max(abs(g) for g in gaps(out)[42 * 3 :]), 0.1)

    def test_stamps_that_recover_pass_as_they_come_again(self):
        clock = SoundClock()
        through(clock, stream(40.0, rate=0.978))
        self.assertEqual(len(clock.restamped), 1)
        healthy = stream(70.0, video_start=START + 40 * 90000)
        with self.assertLogs("services.live.ts", "INFO") as logs:
            out = through(clock, healthy)
        self.assertIn("as it comes", "\n".join(logs.output))
        tail = out[len(out) * 9 // 10 :]
        tail = tail[tail.find(b"\x47") :]
        self.assertEqual(stamps(tail), stamps(healthy[len(healthy) - len(tail) :]))

    def test_a_sound_whose_frames_cant_be_counted_cleanly_is_never_restamped(self):
        # A header not where the frame before ended (a codec misread, damage) in every window:
        # its length is not known, so its stamps pass as they came.
        data = bytearray(stream(90.0, rate=0.978, offset=-466 * 90000))
        starts = [
            at
            for at in range(0, len(data), PACKET)
            if ((data[at + 1] & 0x1F) << 8) | data[at + 2] == AUDIO_PID and data[at + 1] & 0x40
        ]
        for at in starts[::20]:
            es = at + 4 + 14  # past the PES header (PTS only)
            data[es : es + 2] = b"\x00\x00"  # its frame's header, damaged
        clock = SoundClock()
        self.assertEqual(through(clock, bytes(data)), bytes(data))
        self.assertEqual(list(clock.restamped), [])


if __name__ == "__main__":
    unittest.main()
