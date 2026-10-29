"""Tests for CodecRelabeler (services/live/ts.py): a PMT that names a stream's codec wrongly
is renamed after the PES, on synthetic streams (`ts_fixtures.make_stream`, whose sound is ADTS
AAC that its PMT declares as such, stream type 0x0F, and whose pictures are H.264 access units
it declares as `video_type`, 0x1B unless told otherwise)."""

from __future__ import annotations

import unittest

from services.live import ts
from services.live.ts import PACKET, CodecRelabeler
from tests.ts_fixtures import (
    AUDIO_PID,
    PMT_PID,
    VIDEO_PID,
    crc32_mpeg,
    h264_access_unit,
    make_stream,
    pat,
    pes_packets,
    pmt,
)

MPEG_AUDIO, AAC = 0x03, 0x0F
H264, HEVC = 0x1B, 0x24
HEVC_AUD = b"\x00\x00\x00\x01\x46\x01\x50"


def hevc_access_unit(key: bool) -> bytes:
    """An HEVC picture: its access unit delimiter, then VPS, SPS, PPS and an IDR slice when
    `key`, else a trailing slice."""
    nal = lambda head, size: b"\x00\x00\x00\x01" + head + b"\x55" * size  # noqa: E731
    au = HEVC_AUD
    if key:
        au += nal(b"\x40\x01", 20) + nal(b"\x42\x01", 30) + nal(b"\x44\x01", 8)
        return au + nal(b"\x26\x01", 2000)
    return au + nal(b"\x02\x01", 1500)


def pictures(access_unit, video_type: int, frames: int = 30) -> bytes:
    """`frames` pictures (a key one every 10) under a PMT declaring them as `video_type`, its
    PAT and PMT before every fifth."""
    out, cc, psi_cc = [], 0, 0
    for i in range(frames):
        if i % 5 == 0:
            out += [pat(cc=psi_cc), pmt(video_type=video_type, audio_pids=(), cc=psi_cc)]
            psi_cc = (psi_cc + 1) & 0x0F
        pts = 900000 + i * 3600
        ps, cc = pes_packets(VIDEO_PID, 0xE0, access_unit(i % 10 == 0), pts, cc_start=cc, pcr=pts)
        out += ps
    return b"".join(out)


def packets(data: bytes) -> list[bytes]:
    return [data[at : at + PACKET] for at in range(0, len(data) - PACKET + 1, PACKET)]


def pid_of(p: bytes) -> int:
    return ((p[1] & 0x1F) << 8) | p[2]


def declared(data: bytes, pid: int = AUDIO_PID) -> list[tuple[int, int, bool]]:
    """(`pid`'s stream type, version, CRC intact) of each whole PMT section in `data`."""
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
                if ((body[i + 1] & 0x1F) << 8) | body[i + 2] == pid:
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


def pmt_section(
    audio_type: int, filler_streams: int = 0, version: int = 0, program: int = 1
) -> bytes:
    """A PMT section without its CRC: the picture, the sound as `audio_type`, and as many
    private streams with a 30-byte descriptor (to make it span packets)."""
    streams = bytes([0x1B]) + (0xE000 | VIDEO_PID).to_bytes(2, "big") + b"\xf0\x00"
    streams += bytes([audio_type]) + (0xE000 | AUDIO_PID).to_bytes(2, "big") + b"\xf0\x00"
    for k in range(filler_streams):
        streams += b"\x06" + (0xE000 | (0x200 + k)).to_bytes(2, "big") + b"\xf0\x20"
        streams += b"\x52\x1e" + bytes(30)
    body = (0xE000 | VIDEO_PID).to_bytes(2, "big") + b"\xf0\x00" + streams
    head = bytes([0x02]) + (0xB000 | (5 + len(body) + 4)).to_bytes(2, "big")
    return head + program.to_bytes(2, "big") + bytes([0xC1 | version << 1, 0, 0]) + body


def labelled(data: bytes, audio_type: int, filler_streams: int = 0, version: int = 0) -> bytes:
    """`data` with each PMT replaced by one declaring the sound as `audio_type`."""
    out, cc = [], 0
    for p in packets(data):
        if pid_of(p) == PMT_PID:
            pmt = psi(pmt_section(audio_type, filler_streams, version), cc)
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


def relabelled(data: bytes, chunk: int | None = None) -> tuple[bytes, CodecRelabeler]:
    relabeler = CodecRelabeler()
    step = chunk or len(data)
    out = b"".join(relabeler.feed(data[at : at + step]) for at in range(0, len(data), step))
    return out + relabeler.flush(), relabeler


class CodecRelabelerTests(unittest.TestCase):
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

    def test_a_provider_that_declares_the_true_type_again_is_trusted_at_once(self):
        # Renamed to AAC; then the provider's PMT itself says AAC: nothing more is renamed,
        # and the stream VLC sees keeps its type and version (no second re-creation).
        later = labelled(make_stream(3, pts_start=900000 + 3 * 90000), AAC)
        out, relabeler = relabelled(labelled(make_stream(3), MPEG_AUDIO) + later, chunk=1000)
        tables = declared(out)
        self.assertEqual(tables[-1], (AAC, 1, True))
        self.assertEqual({t for t in tables[1:]}, {(AAC, 1, True)})
        self.assertEqual(len(relabeler.renamings), 1)

    def test_a_pmt_that_declares_another_type_is_trusted_at_once(self):
        # Renamed to AAC under a stale "MPEG audio"; then the provider switches to MPEG-2
        # audio and declares it (0x04): its PMT passes as declared from its first one, with
        # the next version (VLC re-creates the stream), not after three PES as AAC.
        start = 900000 + 3 * 90000
        later = labelled(sound_from(make_stream(3, pts_start=start), 0, b"\xff\xf5"), 0x04)
        out, relabeler = relabelled(labelled(make_stream(3), MPEG_AUDIO) + later, chunk=1000)
        tables = declared(out)
        first_later = next(k for k, t in enumerate(tables) if t[0] == 0x04)
        self.assertEqual(tables[first_later - 1], (AAC, 1, True))
        self.assertEqual(set(tables[first_later:]), {(0x04, 2, True)})
        self.assertEqual(len(relabeler.renamings), 1)

    def test_a_reconnect_trusts_the_pmt_again_and_moves_the_version_each_time(self):
        # After a reconnect the stale PMT passes as declared (another version than the renamed
        # one, so VLC re-creates the stream), and three PES later it is renamed again.
        data = labelled(make_stream(3), MPEG_AUDIO)
        relabeler = CodecRelabeler()
        out = relabeler.feed(data) + relabeler.flush()
        relabeler.reconnect()
        out += relabeler.feed(data) + relabeler.flush()
        tables = declared(out)
        versions = [version for _, version, _ in tables]
        self.assertEqual(tables[-1], (AAC, 3, True))
        self.assertIn((MPEG_AUDIO, 2, True), tables)
        self.assertEqual(versions, sorted(versions))
        self.assertTrue(all(intact for _, _, intact in tables))

    def test_a_pmt_that_changes_under_the_same_version_gets_the_next_one(self):
        # An encoder that restarts switches its sound from MPEG audio to AAC and back on one
        # PID and says so in its PMT, whose version stays 0: VLC ignores a version it has seen
        # (and keeps decoding AAC as MPEG audio: silence). Each change passes with the next
        # version and its CRC recomputed, its repeats at that same version; the PMT before the
        # first change passes byte for byte, and nothing is renamed (the PMT names the sound).
        mpeg = b"\xff\xfd"  # MPEG-1 layer II
        first = labelled(sound_from(make_stream(3), 0, mpeg), MPEG_AUDIO)
        second = labelled(make_stream(3, pts_start=900000 + 3 * 90000), AAC)
        third = labelled(sound_from(make_stream(3, pts_start=900000 + 6 * 90000), 0, mpeg), 3)
        data = first + second + third
        for chunk in (None, 1000, 100):
            with self.subTest(chunk=chunk):
                out, relabeler = relabelled(data, chunk)
                self.assertEqual(len(out), len(data))
                self.assertEqual(out[: len(first)], first)
                tables = declared(out)
                count = len(declared(first))
                self.assertEqual(set(tables[:count]), {(MPEG_AUDIO, 0, True)})
                self.assertEqual(set(tables[count : 2 * count]), {(AAC, 1, True)})
                self.assertEqual(set(tables[2 * count :]), {(MPEG_AUDIO, 2, True)})
                self.assertEqual(list(relabeler.renamings), [])
                changed = {pid_of(a) for a, b in zip(packets(data), packets(out)) if a != b}
                self.assertEqual(changed, {PMT_PID})

    def test_a_pmt_that_changes_with_its_version_or_another_programs_passes_untouched(self):
        # The same switch with the version moved by the encoder itself; and a second program's
        # map on the same PID between the first's (not the program the PAT lists first).
        second = labelled(make_stream(3, pts_start=900000 + 3 * 90000), AAC, version=1)
        mpeg = labelled(sound_from(make_stream(3), 0, b"\xff\xfd"), MPEG_AUDIO)
        other = psi(pmt_section(AAC, program=2), 0)
        shared = []
        for p in packets(make_stream(4)):
            shared.append(p)
            if pid_of(p) == PMT_PID:
                shared.extend(other)
        for name, data in (("versioned", mpeg + second), ("two programs", b"".join(shared))):
            with self.subTest(name):
                out, relabeler = relabelled(data, chunk=1000)
                self.assertEqual(out, data)
                self.assertEqual(list(relabeler.renamings), [])

    def test_a_map_announced_for_later_and_then_applied_passes_untouched(self):
        # current v0, next v1, current v1, next v1, current v1: the encoder moves its own
        # version, and a "next" section (current_next 0) is never a change under one version.
        from tests.ts_fixtures import pat

        sections = [(MPEG_AUDIO, 0, True), (AAC, 1, False), (AAC, 1, True), (AAC, 1, False)]
        sections += [(AAC, 1, True), (AAC, 1, True)]
        data, cc = [pat()], 0  # the PAT names the map's PID
        for audio, version, current in sections:
            section = bytearray(pmt_section(audio, version=version))
            if not current:
                section[5] &= 0xFE
            data.extend(psi(bytes(section), cc))
            cc = (cc + 1) & 0x0F
        data = b"".join(data)
        out, _ = relabelled(data)
        self.assertEqual(out, data)

    def test_a_map_moved_to_another_pid_is_not_a_change_under_its_version(self):
        from tests.ts_fixtures import _psi_packet, pat

        moved = 0x1001
        first = labelled(sound_from(make_stream(2), 0, b"\xff\xfd"), MPEG_AUDIO)
        section = pmt_section(AAC)  # v0 again, on the new PID
        data = first + pat(pmt_pid=moved) + _psi_packet(moved, section, 0)
        out, _ = relabelled(data)
        self.assertEqual(out, data)

    def test_a_section_too_short_to_read_passes(self):
        tiny = psi(bytes([0x02, 0xB0, 0x09, 0x00, 0x01, 0xC1, 0x00, 0x00, 0xE1]), 0)
        data = labelled(make_stream(1), MPEG_AUDIO) + b"".join(tiny)
        out, _ = relabelled(data)
        self.assertEqual(out[-PACKET:], tiny[0])

    def test_a_payload_without_a_known_sync_counts_for_nothing(self):
        data = labelled(sound_from(make_stream(3), 0, b"\x00\x00"), MPEG_AUDIO)
        out, relabeler = relabelled(data)
        self.assertEqual(out, data)
        self.assertEqual(list(relabeler.renamings), [])

    def test_h264_declared_as_hevc_is_renamed_after_three_pes(self):
        # A channel that switched from HEVC to H.264 and kept declaring HEVC: no picture
        # decodes (VLC keeps the last one on screen) until its PMT says H.264.
        data = make_stream(4, video_type=HEVC)
        for chunk in (None, 1000, 100):
            with self.subTest(chunk=chunk):
                out, relabeler = relabelled(data, chunk)
                self.assertEqual(len(out), len(data))
                tables = declared(out, VIDEO_PID)
                self.assertEqual(tables[0], (HEVC, 0, True))  # before three PES came
                self.assertEqual(set(tables[1:]), {(H264, 1, True)})
                self.assertEqual(list(relabeler.renamings), [(VIDEO_PID, HEVC, H264)])
                self.assertEqual({t[0] for t in declared(out)}, {AAC})  # the sound as it came
                changed = {pid_of(a) for a, b in zip(packets(data), packets(out)) if a != b}
                self.assertEqual(changed, {PMT_PID})

    def test_h264_behind_a_stray_hevc_delimiter_is_still_h264(self):
        # The provider puts an HEVC access unit delimiter in front of each H.264 picture's own.
        data = pictures(lambda key: HEVC_AUD + h264_access_unit(key), HEVC)
        out, relabeler = relabelled(data, chunk=1000)
        self.assertEqual(declared(out, VIDEO_PID)[-1], (H264, 1, True))
        self.assertEqual(list(relabeler.renamings), [(VIDEO_PID, HEVC, H264)])

    def test_hevc_declared_as_h264_is_renamed_and_true_hevc_passes(self):
        out, relabeler = relabelled(pictures(hevc_access_unit, H264), chunk=1000)
        self.assertEqual(declared(out, VIDEO_PID)[-1], (HEVC, 1, True))
        self.assertEqual(list(relabeler.renamings), [(VIDEO_PID, H264, HEVC)])
        for access_unit, video_type in ((hevc_access_unit, HEVC), (h264_access_unit, H264)):
            data = pictures(access_unit, video_type)
            out, relabeler = relabelled(data, chunk=1000)
            self.assertEqual(out, data)
            self.assertEqual(list(relabeler.renamings), [])

    def test_pictures_without_a_delimiter_or_parameter_set_count_for_nothing(self):
        # Slices alone tell neither codec apart: the PMT is trusted.
        slices = lambda key: b"\x00\x00\x00\x01\x41" + b"\x55" * 1500  # noqa: E731
        data = pictures(slices, HEVC)
        out, relabeler = relabelled(data, chunk=1000)
        self.assertEqual(out, data)
        self.assertEqual(list(relabeler.renamings), [])

    def test_a_flush_passes_a_partial_packet_on(self):
        data = make_stream(2)
        relabeler = CodecRelabeler()
        out = relabeler.feed(data[:-100])
        self.assertEqual(out + relabeler.flush(), data[:-100])


if __name__ == "__main__":
    unittest.main()
