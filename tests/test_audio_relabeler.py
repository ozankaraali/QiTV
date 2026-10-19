"""Tests for AudioRelabeler (services/live/ts.py): a PMT that names an audio stream's codec
wrongly is renamed after the PES, on synthetic streams (`ts_fixtures.make_stream`, whose sound
is ADTS AAC that its PMT declares as such, stream type 0x0F)."""

from __future__ import annotations

import unittest

from services.live import ts
from services.live.ts import PACKET, AudioRelabeler
from tests.ts_fixtures import AUDIO_PID, PMT_PID, VIDEO_PID, crc32_mpeg, make_stream

MPEG_AUDIO, AAC = 0x03, 0x0F


def packets(data: bytes) -> list[bytes]:
    return [data[at : at + PACKET] for at in range(0, len(data) - PACKET + 1, PACKET)]


def pid_of(p: bytes) -> int:
    return ((p[1] & 0x1F) << 8) | p[2]


def declared(data: bytes, extra_streams: int = 0) -> list[tuple[int, int, bool]]:
    """(audio stream type, version, CRC intact) of each whole PMT section in `data`."""
    out, section = [], b""
    for p in packets(data):
        if pid_of(p) != PMT_PID:
            continue
        payload = p[4:]
        if p[1] & 0x40:
            payload = payload[1 + payload[0] :]
            section = b""
        section += payload
        if len(section) >= 3 and len(section) >= 3 + (((section[1] & 0x0F) << 8) | section[2]):
            body = section[: 3 + (((section[1] & 0x0F) << 8) | section[2])]
            i, end, audio = 12 + (((body[10] & 0x0F) << 8) | body[11]), len(body) - 4, None
            while i + 5 <= end:
                if ((body[i + 1] & 0x1F) << 8) | body[i + 2] == AUDIO_PID:
                    audio = body[i]
                i += 5 + (((body[i + 3] & 0x0F) << 8) | body[i + 4])
            out.append((audio, (body[5] >> 1) & 0x1F, crc32_mpeg(body) == 0))
            section = b"\xff" * 1000  # until the next start
    return out


def psi(section: bytes, cc: int) -> list[bytes]:
    """A section (with its CRC appended) in PMT packets: the first with the pointer field."""
    section += crc32_mpeg(section).to_bytes(4, "big")
    payload = b"\x00" + section
    out = []
    for k, at in enumerate(range(0, len(payload), 184)):
        chunk = payload[at : at + 184]
        head = bytes([0x47, (0x40 if k == 0 else 0) | (PMT_PID >> 8), PMT_PID & 0xFF])
        out.append(head + bytes([0x10 | ((cc + k) & 0x0F)]) + chunk + b"\xff" * (184 - len(chunk)))
    return out


def pmt_section(audio_type: int, filler_streams: int = 0) -> bytes:
    """A PMT section without its CRC: the picture, the sound as `audio_type`, and as many
    private streams with a 30-byte descriptor (to make it span packets)."""
    streams = bytes([0x1B]) + (0xE000 | VIDEO_PID).to_bytes(2, "big") + b"\xf0\x00"
    streams += bytes([audio_type]) + (0xE000 | AUDIO_PID).to_bytes(2, "big") + b"\xf0\x00"
    for k in range(filler_streams):
        streams += b"\x06" + (0xE000 | (0x200 + k)).to_bytes(2, "big") + b"\xf0\x20"
        streams += b"\x52\x1e" + bytes(30)
    body = (0xE000 | VIDEO_PID).to_bytes(2, "big") + b"\xf0\x00" + streams
    head = bytes([0x02]) + (0xB000 | (5 + len(body) + 4)).to_bytes(2, "big")
    return head + (1).to_bytes(2, "big") + bytes([0xC1, 0, 0]) + body


def labelled(data: bytes, audio_type: int, filler_streams: int = 0) -> bytes:
    """`data` with each PMT replaced by one declaring the sound as `audio_type`."""
    out, cc = [], 0
    for p in packets(data):
        if pid_of(p) == PMT_PID:
            pmt = psi(pmt_section(audio_type, filler_streams), cc)
            cc = (cc + len(pmt)) & 0x0F
            out.extend(pmt)
        else:
            out.append(p)
    return b"".join(out)


def sound_from(data: bytes, first: int, sync: bytes) -> bytes:
    """`data` with the sound's frames from its `first`th PES on starting with `sync`."""
    out, seen = [], 0
    for p in packets(data):
        if pid_of(p) == AUDIO_PID and p[1] & 0x40:
            seen += 1
            if seen > first:
                es = ts._pes_start(p)[2]
                p = p[:es] + sync + p[es + len(sync) :]
        out.append(p)
    return b"".join(out)


def relabelled(data: bytes, chunk: int | None = None) -> tuple[bytes, AudioRelabeler]:
    relabeler = AudioRelabeler()
    step = chunk or len(data)
    out = b"".join(relabeler.feed(data[at : at + step]) for at in range(0, len(data), step))
    return out + relabeler.flush(), relabeler


class AudioRelabelerTests(unittest.TestCase):
    def test_a_stream_its_pmt_names_right_passes_untouched(self):
        for data in (make_stream(4), labelled(sound_from(make_stream(4), 0, b"\xff\xfd"), 3)):
            out, relabeler = relabelled(data, chunk=1000)
            self.assertEqual(out, data)
            self.assertEqual(list(relabeler.renamings), [])

    def test_aac_declared_as_mpeg_audio_is_renamed_after_three_pes(self):
        data = labelled(make_stream(4), MPEG_AUDIO)
        for chunk in (None, 1000, 100):
            with self.subTest(chunk=chunk):
                out, relabeler = relabelled(data, chunk)
                self.assertEqual(len(out), len(data))
                tables = declared(out)
                self.assertEqual(tables[0], (MPEG_AUDIO, 0, True))  # before three PES came
                self.assertEqual(tables[-1], (AAC, 1, True))
                self.assertEqual({t for t in tables[1:]}, {(AAC, 1, True)})
                self.assertEqual(list(relabeler.renamings), [(AUDIO_PID, MPEG_AUDIO, AAC)])
                # Only the PMT changed.
                changed = {pid_of(a) for a, b in zip(packets(data), packets(out)) if a != b}
                self.assertEqual(changed, {PMT_PID})

    def test_a_switch_back_to_mpeg_audio_renames_it_back(self):
        # Two seconds of AAC under a PMT that says MPEG audio, then MPEG-1 layer II.
        data = labelled(sound_from(make_stream(5), 2 * 47, b"\xff\xfd"), MPEG_AUDIO)
        out, relabeler = relabelled(data, chunk=1000)
        tables = declared(out)
        self.assertIn((AAC, 1, True), tables)
        self.assertEqual(tables[-1], (MPEG_AUDIO, 2, True))
        self.assertEqual(
            list(relabeler.renamings),
            [(AUDIO_PID, MPEG_AUDIO, AAC), (AUDIO_PID, AAC, MPEG_AUDIO)],
        )

    def test_a_pmt_that_spans_packets_is_renamed_whole(self):
        data = labelled(make_stream(3), MPEG_AUDIO, filler_streams=6)
        self.assertGreater(sum(1 for p in packets(data) if pid_of(p) == PMT_PID), 6)
        for chunk in (None, 300):
            with self.subTest(chunk=chunk):
                out, _ = relabelled(data, chunk)
                self.assertEqual(len(out), len(data))
                tables = declared(out)
                self.assertEqual(tables[-1], (AAC, 1, True))
                self.assertTrue(all(intact for _, _, intact in tables))

    def test_a_payload_without_a_known_sync_counts_for_nothing(self):
        data = labelled(sound_from(make_stream(3), 0, b"\x00\x00"), MPEG_AUDIO)
        out, relabeler = relabelled(data)
        self.assertEqual(out, data)
        self.assertEqual(list(relabeler.renamings), [])

    def test_a_flush_passes_a_partial_packet_on(self):
        data = make_stream(2)
        relabeler = AudioRelabeler()
        out = relabeler.feed(data[:-100])
        self.assertEqual(out + relabeler.flush(), data[:-100])


if __name__ == "__main__":
    unittest.main()
