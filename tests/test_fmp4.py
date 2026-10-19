"""Tests for services/live/fmp4.py: fragmented MP4 (CMAF) HLS segments remuxed to MPEG-TS.

Hand-built boxes cover the parser's cases. The synthetic fixtures under tests/fixtures/fmp4
(ffmpeg's test sources, `make_fixtures.py`) are remuxed and compared with golden.json: each
PID's frames (PTS, DTS, keyframe, a digest of the payloads). Locally, with ffmpeg on the PATH
and outside CI, `FfmpegTests` also has ffmpeg decode each remuxed TS and compares its frames
with ffmpeg's decoding of the fMP4: golden.json is written only once they agree
(``python -m tests.test_fmp4 --golden``).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

from services.live import fmp4
from services.live.fmp4 import Fmp4Error, Fmp4Remuxer, annex_b, read_init, read_segment
from services.live.ts import PACKET, Opened, Segmenter, _crc32

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fmp4"
GOLDEN = FIXTURES / "golden.json"
RENDITIONS = ("avc-aac", "avc-ac3", "hevc-eac3", "split/0", "split/1")

SPS = b"\x67\x64\x00\x0a\xac\xd9"
PPS = b"\x68\xeb\xe3\xcb"
IDR = b"\x65\x88\x84\x00\x10"
SLICE = b"\x41\x9a\x21\x6c"
SYNC_FLAGS = 0x02000000  # depends on no other sample
NON_SYNC = 0x01010000  # depends on others, not a sync sample


# Boxes.


def box(kind: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), kind) + body


def full(kind: bytes, version: int, flags: int, *parts: bytes) -> bytes:
    return box(kind, bytes([version]) + flags.to_bytes(3, "big"), *parts)


def nals(*units: bytes, size: int = 4) -> bytes:
    return b"".join(len(unit).to_bytes(size, "big") + unit for unit in units)


def avcc(sps: bytes = SPS, pps: bytes = PPS, size: int = 4) -> bytes:
    body = bytes([1, sps[1], sps[2], sps[3], 0xFC | (size - 1), 0xE1])
    body += len(sps).to_bytes(2, "big") + sps + b"\x01" + len(pps).to_bytes(2, "big") + pps
    return box(b"avcC", body)


def visual(kind: bytes = b"avc1", *children: bytes) -> bytes:
    body = bytes(6) + b"\x00\x01" + bytes(16) + struct.pack(">HH", 128, 72)
    body += b"\x00\x48\x00\x00" * 2 + bytes(4) + b"\x00\x01" + bytes(32) + b"\x00\x18\xff\xff"
    return box(kind, body, *(children or (avcc(),)))


def asc(*fields: tuple[int, int]) -> bytes:
    """An AudioSpecificConfig of (value, bits) fields, padded to whole bytes."""
    value = width = 0
    for field, bits in fields:
        value, width = value << bits | field, width + bits
    pad = -width % 8
    return (value << pad).to_bytes((width + pad) // 8, "big")


def descriptor(tag: int, body: bytes) -> bytes:
    return bytes([tag, len(body)]) + body


def esds(config: bytes = b"\x11\x90", kind: int = 0x40) -> bytes:
    decoder = bytes([kind, 0x15]) + bytes(11) + descriptor(0x05, config)
    return full(b"esds", 0, 0, descriptor(0x03, b"\x00\x01\x00" + descriptor(0x04, decoder)))


def sound(kind: bytes = b"mp4a", *children: bytes, rate: int = 48000) -> bytes:
    body = bytes(6) + b"\x00\x01" + bytes(8) + struct.pack(">HH", 2, 16) + bytes(4)
    body += struct.pack(">I", rate << 16)
    return box(kind, body, *(children or (esds(),)))


def trak(
    track_id: int,
    handler: bytes,
    scale: int,
    entry: bytes,
    *,
    language: str = "und",
    edits: bytes = b"",
    cslg: int | None = None,
) -> bytes:
    code = 0
    for char in language:
        code = code << 5 | (ord(char) - 0x60)
    tkhd = full(b"tkhd", 0, 3, bytes(8), struct.pack(">I", track_id), bytes(68))
    mdhd = full(b"mdhd", 0, 0, bytes(8), struct.pack(">IIHH", scale, 0, code, 0))
    hdlr = full(b"hdlr", 0, 0, bytes(4), handler, bytes(12), b"\x00")
    stbl = [full(b"stsd", 0, 0, struct.pack(">I", 1), entry)]
    if cslg is not None:
        stbl.append(full(b"cslg", 0, 0, struct.pack(">iiiii", cslg, 0, 0, 0, 0)))
    minf = box(b"minf", box(b"stbl", *stbl))
    parts = [tkhd]
    if edits:
        parts.append(box(b"edts", edits))
    parts.append(box(b"mdia", mdhd, hdlr, minf))
    return box(b"trak", *parts)


def elst(*entries: tuple[int, int], version: int = 0) -> bytes:
    body = struct.pack(">I", len(entries))
    for duration, media in entries:
        if version:
            body += struct.pack(">Qqi", duration, media, 0x10000)
        else:
            body += struct.pack(">Iii", duration, media, 0x10000)
    return full(b"elst", version, 0, body)


def movie(
    *traks: bytes, trex: tuple[tuple[int, int, int, int], ...] = (), extra: bytes = b""
) -> bytes:
    mvhd = full(b"mvhd", 0, 0, bytes(8), struct.pack(">II", 1000, 0), bytes(80))
    mvex = box(
        b"mvex", *(full(b"trex", 0, 0, struct.pack(">IIIII", t, 1, d, s, f)) for t, d, s, f in trex)
    )
    return box(b"ftyp", b"iso6\x00\x00\x00\x00") + box(b"moov", mvhd, *traks, mvex, extra)


VIDEO_INIT = movie(trak(1, b"vide", 90000, visual()))
AUDIO_INIT = movie(trak(2, b"soun", 48000, sound()))
AV_INIT = movie(trak(1, b"vide", 90000, visual()), trak(2, b"soun", 48000, sound(), language="eng"))


def traf(
    track_id: int,
    samples: list[tuple[int, bytes, int | None, int]],
    *,
    tfdt: int | None = 0,
    tfhd_flags: int = 0x20000,
    base: int | None = None,
    version: int = 0,
    offset: int | None = 0,
    extra: bytes = b"",
) -> bytes:
    """A track fragment of (duration, data, flags or None, composition offset) samples whose
    trun's data offset is `offset` (filled in by `fragment` when 0; None: none)."""
    head = struct.pack(">I", track_id)
    if base is not None:
        tfhd_flags |= 0x01
        head += struct.pack(">Q", base)
    parts = [full(b"tfhd", 0, tfhd_flags, head)]
    if tfdt is not None:
        parts.append(full(b"tfdt", 1, 0, struct.pack(">Q", tfdt)))
    flags = 0x100 | 0x200 | 0x800 | (0x400 if any(s[2] is not None for s in samples) else 0)
    if offset is not None:
        flags |= 0x01
    rows = b""
    for duration, data, sample_flags, composition in samples:
        rows += struct.pack(">II", duration, len(data))
        if flags & 0x400:
            rows += struct.pack(">I", sample_flags or 0)
        rows += struct.pack(">i", composition)  # two's complement either way
    count = struct.pack(">I", len(samples))
    data_offset = struct.pack(">i", offset) if offset is not None else b""
    parts.append(full(b"trun", version, flags, count, data_offset, rows))
    return box(b"traf", *parts, extra)


def fragment(*trafs: tuple[bytes, list[bytes]], fill: bool = True) -> bytes:
    """A moof of `trafs` (each with its samples' data) and its mdat; each trun's data offset
    (when it has one, and `fill`) points at its samples in the mdat (moof-relative)."""
    moof = box(b"moof", full(b"mfhd", 0, 0, b"\x00\x00\x00\x01"), *(t for t, _ in trafs))
    if fill:
        position = len(moof) + 8
        out = bytearray(moof)
        at = 0
        for body, data in trafs:
            start = out.find(b"trun", at)
            at = start + 4
            if out[start + 7] & 0x01:
                out[start + 12 : start + 16] = struct.pack(">i", position)
            position += sum(len(d) for d in data)
        moof = bytes(out)
    return moof + box(b"mdat", *(d for _, data in trafs for d in data))


def video_samples(
    count: int, gop: int = 3, duration: int = 3600
) -> list[tuple[int, bytes, int | None, int]]:
    return [
        (
            duration,
            nals(IDR) if k % gop == 0 else nals(SLICE + bytes([k])),
            SYNC_FLAGS if k % gop == 0 else NON_SYNC,
            0,
        )
        for k in range(count)
    ]


def audio_samples(count: int) -> list[tuple[int, bytes, int | None, int]]:
    return [(1024, b"\x21\x10" + bytes([k]) * 10, None, 0) for k in range(count)]


def segment(track_id: int, samples, **kwargs) -> bytes:
    return fragment((traf(track_id, samples, **kwargs), [s[1] for s in samples]))


# Reading TS.


def packets(data: bytes) -> list[bytes]:
    assert len(data) % PACKET == 0
    return [data[at : at + PACKET] for at in range(0, len(data), PACKET)]


def pid_of(packet: bytes) -> int:
    return ((packet[1] & 0x1F) << 8) | packet[2]


def timestamp(data: bytes | bytearray, at: int) -> int:
    return (
        ((data[at] >> 1) & 7) << 30
        | data[at + 1] << 22
        | (data[at + 2] >> 1) << 15
        | data[at + 3] << 7
        | data[at + 4] >> 1
    )


class Frame:
    def __init__(self, pts: int, dts: int, key: bool, pcr: int | None, payload: bytes) -> None:
        self.pts, self.dts, self.key, self.pcr, self.payload = pts, dts, key, pcr, payload


def frames(data: bytes) -> dict[int, list[Frame]]:
    """Each PES PID's frames: PTS, DTS, the random access indicator, the PCR in its first
    packet, its payload."""
    pes: dict[int, list[tuple[bool, int | None, bytearray]]] = {}
    for packet in packets(data):
        pid = pid_of(packet)
        if pid in (0, fmp4._PMT_PID):
            continue
        control = (packet[3] >> 4) & 3
        at, key, pcr = 4, False, None
        if control & 2:
            length = packet[4]
            if length:
                key = bool(packet[5] & 0x40)
                if packet[5] & 0x10:
                    pcr = int.from_bytes(packet[6:12], "big") >> 15
            at = 5 + length
        if packet[1] & 0x40:
            pes.setdefault(pid, []).append((key, pcr, bytearray()))
        pes[pid][-1][2].extend(packet[at:])
    out: dict[int, list[Frame]] = {}
    for pid, units in pes.items():
        for key, pcr, unit in units:
            assert unit[:3] == b"\x00\x00\x01", unit[:8]
            pts = timestamp(unit, 9)
            dts = timestamp(unit, 14) if unit[7] & 0x40 else pts
            out.setdefault(pid, []).append(Frame(pts, dts, key, pcr, bytes(unit[9 + unit[8] :])))
    return out


def sections(data: bytes) -> dict[int, bytes]:
    found: dict[int, bytes] = {}
    for packet in packets(data):
        if packet[1] & 0x40 and pid_of(packet) in (0, fmp4._PMT_PID):
            begin = 5 + packet[4]
            length = ((packet[begin + 1] & 0x0F) << 8) | packet[begin + 2]
            found.setdefault(pid_of(packet), packet[begin : begin + 3 + length])
    return found


def remuxed(rendition: str) -> tuple[bytes, Fmp4Remuxer, list[str]]:
    """A fixture rendition's segments remuxed, one TS (and its segments' names)."""
    folder = FIXTURES / rendition
    playlist = (folder / "index.m3u8").read_text()
    init = re.search(r'#EXT-X-MAP:URI="([^"]+)"', playlist)
    assert init is not None
    names = [line for line in playlist.splitlines() if line and not line.startswith("#")]
    remuxer = Fmp4Remuxer()
    remuxer.init((folder / init.group(1)).read_bytes())
    return b"".join(remuxer.remux((folder / name).read_bytes()) for name in names), remuxer, names


def table(data: bytes) -> dict[str, dict[str, object]]:
    """golden.json's view of a TS: each PID's frames, first PTS and a digest of them all."""
    out = {}
    for pid, found in sorted(frames(data).items()):
        digest = hashlib.sha256()
        for frame in found:
            digest.update(f"{frame.pts},{frame.dts},{int(frame.key)};".encode())
            digest.update(hashlib.sha256(frame.payload).digest())
        out[str(pid)] = {
            "frames": len(found),
            "keys": sum(frame.key for frame in found),
            "first_pts": found[0].pts,
            "first_dts": found[0].dts,
            "digest": digest.hexdigest(),
        }
    return out


def write_golden() -> None:
    golden = {rendition: table(remuxed(rendition)[0]) for rendition in RENDITIONS}
    GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN}")


class InitTests(unittest.TestCase):
    def test_an_init_reads_its_video_and_audio_tracks(self):
        tracks = read_init(AV_INIT)
        video, audio = tracks[1], tracks[2]
        self.assertEqual(
            (video.kind, video.codec, video.timescale, video.stream_type),
            ("video", "h264", 90000, 0x1B),
        )
        self.assertEqual(
            video.parameter_sets, b"\x00\x00\x00\x01" + SPS + b"\x00\x00\x00\x01" + PPS
        )
        self.assertEqual(
            (audio.kind, audio.codec, audio.timescale, audio.stream_type),
            ("audio", "aac", 48000, 0x0F),
        )
        self.assertEqual(audio.adts, (1, 3, 2))  # AAC LC, 48 kHz, stereo
        self.assertEqual((video.language, audio.language), ("", "eng"))

    def test_an_edit_list_shifts_by_its_media_time_less_its_empty_edits(self):
        # The movie's timescale is 1000: an empty edit of 80 ms delays 90 kHz media 7200 ticks.
        delayed = movie(trak(1, b"vide", 90000, visual(), edits=elst((80, -1), (0, 6000))))
        self.assertEqual(read_init(delayed)[1].shift, 6000 - 7200)
        priming = movie(trak(2, b"soun", 48000, sound(), edits=elst((1000, 1024), version=1)))
        self.assertEqual(read_init(priming)[2].shift, 1024)

    def test_cslg_gives_the_decode_shift(self):
        tracks = read_init(movie(trak(1, b"vide", 90000, visual(), cslg=7200)))
        self.assertEqual(tracks[1].decode_shift, 7200)

    def test_encryption_is_refused(self):
        for init in (
            movie(trak(1, b"vide", 90000, visual(b"encv"))),
            movie(trak(1, b"vide", 90000, visual()), trak(2, b"soun", 48000, sound(b"enca"))),
            movie(trak(1, b"vide", 90000, visual()), extra=full(b"pssh", 0, 0, bytes(20))),
        ):
            with self.subTest(), self.assertRaisesRegex(Fmp4Error, "encrypted"):
                read_init(init)

    def test_a_video_or_every_audio_of_another_codec_is_refused(self):
        for init in (
            movie(trak(1, b"vide", 90000, visual(b"av01", box(b"av1C", b"\x81\x00\x0c\x00")))),
            movie(
                trak(1, b"vide", 90000, visual()),
                trak(2, b"soun", 48000, sound(b"Opus", box(b"dOps", bytes(11)))),
            ),
            movie(trak(2, b"soun", 48000, sound(b"mp4a", esds(kind=0xA5)))),  # AC-3 by OTI
        ):
            with self.subTest(), self.assertRaisesRegex(Fmp4Error, "unsupported codec"):
                read_init(init)

    def test_the_first_video_and_first_audio_are_kept_and_text_is_left_out(self):
        init = movie(
            trak(1, b"vide", 90000, visual()),
            trak(2, b"soun", 48000, sound(b"Opus", box(b"dOps", bytes(11)))),
            trak(3, b"soun", 48000, sound()),
            trak(4, b"soun", 44100, sound()),
            trak(5, b"text", 1000, box(b"wvtt", bytes(8))),
        )
        self.assertEqual(sorted(read_init(init)), [1, 3])

    def test_aac_configs_name_their_adts_fields(self):
        cases = {
            asc((2, 5), (4, 4), (1, 4)): (1, 4, 1),  # LC, 44.1 kHz, mono
            asc((2, 5), (3, 4), (2, 4)): (1, 3, 2),  # LC, 48 kHz, stereo
            asc((5, 5), (6, 4), (2, 4), (3, 4), (2, 5)): (1, 6, 2),  # HE-AAC: its LC core
            asc((29, 5), (6, 4), (1, 4), (3, 4), (2, 5)): (1, 6, 1),  # HE-AAC v2
            asc((1, 5), (4, 4), (2, 4)): (0, 4, 2),  # AAC main
        }
        for config, adts in cases.items():
            with self.subTest(config=config.hex()):
                init = movie(trak(2, b"soun", 48000, sound(b"mp4a", esds(config))))
                self.assertEqual(read_init(init)[2].adts, adts)
        mpeg2 = movie(
            trak(2, b"soun", 48000, sound(b"mp4a", esds(asc((2, 5), (3, 4), (2, 4)), kind=0x67)))
        )
        self.assertEqual(read_init(mpeg2)[2].adts, (1, 3, 2))  # MPEG-2 AAC LC: its OTI's profile
        refused = (
            asc((2, 5), (15, 4), (48000, 24), (2, 4)),  # an explicit frequency
            asc((2, 5), (3, 4), (0, 4)),  # channels in a PCE
            asc((31, 5), (10, 6), (3, 4), (2, 4)),  # USAC (object type 42): not ADTS
        )
        for config in refused:
            with (
                self.subTest(config=config.hex()),
                self.assertRaisesRegex(Fmp4Error, "unsupported"),
            ):
                read_init(movie(trak(2, b"soun", 48000, sound(b"mp4a", esds(config)))))

    def test_mpeg_audio_goes_as_it_is(self):
        tracks = read_init(movie(trak(2, b"soun", 48000, sound(b"mp4a", esds(b"", kind=0x6B)))))
        self.assertEqual((tracks[2].codec, tracks[2].stream_type), ("mp3", 0x03))

    def test_an_edit_lists_media_time_past_ten_seconds_is_left_out(self):
        far = movie(trak(1, b"vide", 90000, visual(), edits=elst((80, -1), (0, 20 * 90000))))
        track = read_init(far)[1]
        self.assertEqual((track.shift, track.edit_media), (-7200, 0))  # the empty edit stays
        near = read_init(movie(trak(1, b"vide", 90000, visual(), edits=elst((0, 3600)))))[1]
        self.assertEqual((near.shift, near.edit_media), (3600, 3600))  # a B-frame offset

    def test_a_box_pointing_past_its_data_is_damaged_whatever_meets_it(self):
        # A struct.error or IndexError from a field past the data ended the session
        # (direct play); as Fmp4Error it skips the segment, or refuses the init, as any damage.
        normal = trak(1, b"vide", 90000, visual())
        mdia = normal[normal.index(b"mdia") - 4 :]  # the trak's last box
        short_tkhd = box(b"moov", box(b"trak", mdia, full(b"tkhd", 0, 3, bytes(4))))  # last
        with self.assertRaisesRegex(Fmp4Error, "damaged"):
            read_init(short_tkhd)
        tracks = read_init(VIDEO_INIT)
        tfhd = full(b"tfhd", 0, 0x20000, struct.pack(">I", 1))
        empty_tfdt = box(b"moof", box(b"traf", tfhd, box(b"tfdt")))  # its version past the end
        with self.assertRaisesRegex(Fmp4Error, "damaged"):
            read_segment(empty_tfdt, tracks)
        remuxer = Fmp4Remuxer()
        remuxer.init(VIDEO_INIT)
        with self.assertRaisesRegex(Fmp4Error, "damaged"):
            remuxer.remux(empty_tfdt)

    def test_no_moov_or_a_damaged_one_is_refused(self):
        with self.assertRaisesRegex(Fmp4Error, "no init"):
            read_init(box(b"ftyp", bytes(8)))
        with self.assertRaisesRegex(Fmp4Error, "damaged"):
            read_init(AV_INIT[:-5])
        with self.assertRaisesRegex(Fmp4Error, "damaged"):
            read_init(movie(trak(1, b"vide", 90000, visual(b"avc1", box(b"avcC", b"\x01\x64")))))


class SegmentTests(unittest.TestCase):
    def test_a_track_run_gives_times_keys_and_data(self):
        tracks = read_init(VIDEO_INIT)
        samples = read_segment(segment(1, video_samples(4), tfdt=900000), tracks)
        self.assertEqual([s.dts for s in samples], [900000, 903600, 907200, 910800])
        self.assertEqual([s.key for s in samples], [True, False, False, True])
        self.assertEqual(bytes(samples[1].data), nals(SLICE + b"\x01"))

    def test_keys_come_from_the_pictures_without_flags(self):
        tracks = read_init(VIDEO_INIT)
        runs = [(3600, s[1], None, 0) for s in video_samples(4)]
        self.assertEqual(
            [s.key for s in read_segment(segment(1, runs), tracks)], [True, False, False, True]
        )
        # trex says every sample is a non-sync one; the IDR still is a key.
        tracks = read_init(movie(trak(1, b"vide", 90000, visual()), trex=((1, 3600, 0, NON_SYNC),)))
        self.assertEqual(
            [s.key for s in read_segment(segment(1, runs), tracks)], [True, False, False, True]
        )

    def test_the_first_sample_flags_and_tfhd_defaults_apply(self):
        tracks = read_init(VIDEO_INIT)
        body = struct.pack(">II", 1, 3600) + struct.pack(">I", NON_SYNC)
        tfhd = full(b"tfhd", 0, 0x20000 | 0x08 | 0x20, body)
        rows = b"".join(struct.pack(">I", len(nals(SLICE))) for _ in range(3))
        trun = full(
            b"trun",
            0,
            0x01 | 0x04 | 0x200,
            struct.pack(">I", 3),
            struct.pack(">i", 0),
            struct.pack(">I", SYNC_FLAGS),
            rows,
        )
        data = [nals(SLICE)] * 3  # no IDR: the flags decide
        moof = fragment((box(b"traf", tfhd, full(b"tfdt", 0, 0, struct.pack(">I", 0)), trun), data))
        samples = read_segment(moof, tracks)
        self.assertEqual(
            [(s.dts, s.key) for s in samples], [(0, True), (3600, False), (7200, False)]
        )

    def test_data_offsets_count_from_the_base_the_track_fragment_names(self):
        tracks = read_init(AV_INIT)
        video, audio = video_samples(2), audio_samples(3)
        # No base flags: the first track fragment counts from the moof, the second from the
        # end of the first's data; its trun has no data offset of its own.
        moof = fragment(
            (traf(1, video, tfhd_flags=0), [s[1] for s in video]),
            (traf(2, audio, tfhd_flags=0, offset=None), [s[1] for s in audio]),
        )
        samples = read_segment(moof, tracks)
        self.assertEqual([bytes(s.data) for s in samples], [s[1] for s in video + audio])
        # An explicit base-data-offset: the file's own offsets.
        moof_len = (
            len(fragment((traf(1, video, base=0, offset=None), [s[1] for s in video]), fill=False))
            - 8
            - sum(len(s[1]) for s in video)
        )
        explicit = fragment(
            (traf(1, video, base=moof_len + 8, offset=None), [s[1] for s in video]), fill=False
        )
        self.assertEqual(
            [bytes(s.data) for s in read_segment(explicit, tracks)], [s[1] for s in video]
        )

    def test_a_track_fragment_without_tfdt_follows_on_from_the_last(self):
        tracks = read_init(AUDIO_INIT)
        ends: dict[int, int] = {}
        first = read_segment(segment(2, audio_samples(3), tfdt=48000), tracks, ends)
        second = read_segment(segment(2, audio_samples(2), tfdt=None), tracks, ends)
        self.assertEqual([s.dts for s in first + second], [48000 + 1024 * k for k in range(5)])

    def test_a_track_not_remuxed_is_skipped(self):
        tracks = read_init(VIDEO_INIT)
        audio, video = audio_samples(2), video_samples(2)
        moof = fragment(
            (traf(7, audio, tfhd_flags=0), [s[1] for s in audio]),
            (traf(1, video, tfhd_flags=0, offset=None), [s[1] for s in video]),
        )
        self.assertEqual([bytes(s.data) for s in read_segment(moof, tracks)], [s[1] for s in video])

    def test_negative_composition_offsets_are_read_signed(self):
        tracks = read_init(VIDEO_INIT)
        runs = [
            (3600, nals(IDR), SYNC_FLAGS, 0),
            (3600, nals(SLICE), NON_SYNC, 3600),
            (3600, nals(SLICE), NON_SYNC, -3600),
        ]
        for version in (1, 0):  # version 0's too: some packagers write them so
            samples = read_segment(segment(1, runs, version=version), tracks)
            self.assertEqual(
                [(s.dts, s.pts) for s in samples], [(0, 0), (3600, 7200), (7200, 3600)]
            )

    def test_encrypted_samples_are_refused(self):
        tracks = read_init(VIDEO_INIT)
        for kind in (b"senc", b"saiz", b"saio"):
            with self.subTest(kind=kind), self.assertRaisesRegex(Fmp4Error, "encrypted"):
                read_segment(segment(1, video_samples(2), extra=full(kind, 0, 0, bytes(8))), tracks)

    def test_data_out_of_bounds_is_damaged(self):
        tracks = read_init(VIDEO_INIT)
        whole = segment(1, video_samples(3))
        with self.assertRaisesRegex(Fmp4Error, "damaged"):
            read_segment(whole[:-4], tracks)


class AnnexBTests(unittest.TestCase):
    def test_an_access_unit_delimiter_comes_first_and_parameter_sets_before_keyframes(self):
        track = read_init(VIDEO_INIT)[1]
        start = b"\x00\x00\x00\x01"
        self.assertEqual(
            annex_b(track, memoryview(nals(IDR)), True),
            fmp4._H264_AUD + track.parameter_sets + start + IDR,
        )
        self.assertEqual(
            annex_b(track, memoryview(nals(SLICE)), False), fmp4._H264_AUD + start + SLICE
        )
        # The sample's own delimiter and parameter sets: nothing added.
        own = nals(b"\x09\x10", SPS, PPS, IDR)
        self.assertEqual(
            annex_b(track, memoryview(own), True),
            start + b"\x09\x10" + start + SPS + start + PPS + start + IDR,
        )

    def test_two_byte_lengths_and_hevc(self):
        hvcc = bytes(21) + b"\x01" + b"\x02"
        for kind, unit in ((0x20, b"\x40\x01\x0c"), (0x21, b"\x42\x01\x01")):
            hvcc += bytes([0x80 | kind]) + b"\x00\x01" + len(unit).to_bytes(2, "big") + unit
        track = read_init(movie(trak(1, b"vide", 90000, visual(b"hvc1", box(b"hvcC", hvcc)))))[1]
        self.assertEqual((track.codec, track.nal_size, track.stream_type), ("hevc", 2, 0x24))
        irap = b"\x26\x01\xaf"  # IDR_W_RADL
        out = annex_b(track, memoryview(nals(irap, size=2)), True)
        self.assertEqual(out, fmp4._HEVC_AUD + track.parameter_sets + b"\x00\x00\x00\x01" + irap)
        self.assertTrue(fmp4._key(track, memoryview(nals(irap, size=2)), None))

    def test_a_length_past_the_sample_is_damaged(self):
        track = read_init(VIDEO_INIT)[1]
        with self.assertRaisesRegex(Fmp4Error, "damaged"):
            annex_b(track, memoryview(nals(IDR)[:-1]), True)


class RemuxTests(unittest.TestCase):
    def remux(self, *segments: bytes, init: bytes = AV_INIT, **kwargs) -> tuple[Fmp4Remuxer, bytes]:
        remuxer = Fmp4Remuxer(**kwargs)
        remuxer.init(init)
        return remuxer, b"".join(remuxer.remux(s) for s in segments)

    def av_segment(self, start: int) -> bytes:
        video, audio = video_samples(6), audio_samples(10)
        return fragment(
            (traf(1, video, tfdt=start), [s[1] for s in video]),
            (traf(2, audio, tfdt=start * 48000 // 90000), [s[1] for s in audio]),
        )

    def test_a_segment_is_tables_then_pes_in_dts_order(self):
        _, data = self.remux(self.av_segment(900000))
        pids = [pid_of(p) for p in packets(data)]
        self.assertEqual(pids[:2], [0, fmp4._PMT_PID])
        found = frames(data)
        self.assertEqual(len(found[fmp4._VIDEO_PID]), 6)
        self.assertEqual(len(found[fmp4._AUDIO_PID]), 10)
        order = [
            timestamp(p, 4 + (1 + p[4] if p[3] & 0x20 else 0) + 9)
            for p in packets(data)
            if p[1] & 0x40 and pid_of(p) > 0x20 and pid_of(p) != fmp4._PMT_PID
        ]
        self.assertEqual(order, sorted(order))
        audio = found[fmp4._AUDIO_PID][0]
        self.assertEqual(audio.payload[:2], b"\xff\xf1")  # ADTS
        self.assertEqual(audio.pts, 900000)

    def test_tables_name_the_streams_and_check_out(self):
        _, data = self.remux(self.av_segment(0))
        found = sections(data)
        self.assertEqual(_crc32(found[0]), 0)
        self.assertEqual(_crc32(found[fmp4._PMT_PID]), 0)
        pmt = found[fmp4._PMT_PID]
        self.assertEqual(((pmt[8] & 0x1F) << 8) | pmt[9], fmp4._VIDEO_PID)  # the PCR's PID
        self.assertIn(bytes([0x1B, 0xE1, 0x00, 0xF0, 0x00]), pmt)
        self.assertIn(bytes([0x0F, 0xE1, 0x01, 0xF0, 0x06, 0x0A, 0x04]) + b"eng\x00", pmt)

    def test_counters_run_on_and_every_packet_is_whole(self):
        _, data = self.remux(self.av_segment(0), self.av_segment(540000))
        last: dict[int, int] = {}
        for packet in packets(data):
            self.assertEqual(packet[0], 0x47)
            pid = pid_of(packet)
            if pid in last:
                self.assertEqual(packet[3] & 0x0F, (last[pid] + 1) & 0x0F)
            last[pid] = packet[3] & 0x0F

    def test_the_pcr_runs_before_each_video_dts_and_keys_are_marked(self):
        _, data = self.remux(self.av_segment(0), self.av_segment(540000))
        video = frames(data)[fmp4._VIDEO_PID]
        self.assertEqual(
            [f.pcr for f in video], [(f.dts - fmp4._PCR_LEAD) % (1 << 33) for f in video]
        )
        self.assertEqual([f.key for f in video], [k % 3 == 0 for k in range(12)])
        self.assertTrue(all(f.pcr is None and not f.key for f in frames(data)[fmp4._AUDIO_PID]))
        _, audio_only = self.remux(segment(2, audio_samples(4)), init=AUDIO_INIT)
        audio = frames(audio_only)[fmp4._AUDIO_PID]
        self.assertEqual(
            [f.pcr for f in audio], [(f.dts - fmp4._PCR_LEAD) % (1 << 33) for f in audio]
        )
        _, unclocked = self.remux(segment(2, audio_samples(4)), init=AUDIO_INIT, pcr=False)
        self.assertTrue(all(f.pcr is None for f in frames(unclocked)[fmp4._AUDIO_PID]))

    def test_negative_composition_offsets_leave_no_pts_before_its_dts(self):
        runs = [
            (3600, nals(IDR), SYNC_FLAGS, 0),
            (3600, nals(SLICE), NON_SYNC, 3600),
            (3600, nals(SLICE), NON_SYNC, -3600),
        ]
        _, data = self.remux(segment(1, runs, version=1, tfdt=90000), init=VIDEO_INIT)
        video = frames(data)[fmp4._VIDEO_PID]
        self.assertEqual([f.pts for f in video], [90000, 97200, 93600])  # presentation as given
        self.assertEqual([f.dts for f in video], [86400, 90000, 93600])  # decoding 3600 earlier
        self.assertTrue(all(f.pts >= f.dts for f in video))

    def test_the_first_segment_sets_the_dts_shift_for_good(self):
        # A later, more negative composition offset stepped every DTS (and the PCR)
        # back once; now that frame's DTS is clamped to its PTS and the rest keep their shift.
        first = [(3600, nals(IDR), SYNC_FLAGS, 0), (3600, nals(SLICE), NON_SYNC, 3600)]
        first += [(3600, nals(SLICE), NON_SYNC, -3600)]
        later = [(3600, nals(IDR), SYNC_FLAGS, 0), (3600, nals(SLICE), NON_SYNC, -7200)]
        later += [(3600, nals(SLICE), NON_SYNC, 0)]
        with self.assertLogs("services.live.fmp4", "INFO") as logs:
            _, data = self.remux(
                segment(1, first, version=1, tfdt=90000),
                segment(1, later, version=1, tfdt=90000 + 3 * 3600),
                init=VIDEO_INIT,
            )
        video = frames(data)[fmp4._VIDEO_PID]
        self.assertEqual([f.pts - f.dts for f in video], [3600, 7200, 0, 3600, 0, 3600])
        self.assertEqual(video[4].dts, video[4].pts)  # clamped, not below its PTS
        pcrs = [f.pcr for f in video]
        self.assertEqual(pcrs, sorted(pcrs))  # the clock never steps back
        self.assertTrue(any("outreached" in line for line in logs.output))

    def test_a_one_sided_edit_media_time_can_be_left_out(self):
        remuxer = Fmp4Remuxer()
        remuxer.init(movie(trak(1, b"vide", 90000, visual(), edits=elst((80, -1), (0, 180000)))))
        self.assertEqual(remuxer.edit_media_s, 2.0)
        remuxer.drop_edit_media(1.0)
        self.assertEqual((remuxer.tracks[1].shift, remuxer.edit_media_s), (-7200, 0.0))

    def test_times_wrap_at_33_bits(self):
        start = (1 << 33) + 4500 * 90000
        _, data = self.remux(segment(1, video_samples(2), tfdt=start), init=VIDEO_INIT)
        self.assertEqual(frames(data)[fmp4._VIDEO_PID][0].pts, start % (1 << 33))

    def test_a_segment_with_its_own_init_and_a_changed_init(self):
        remuxer = Fmp4Remuxer()
        with self.assertRaisesRegex(Fmp4Error, "no init"):
            remuxer.remux(segment(1, video_samples(2)))
        first = remuxer.remux(VIDEO_INIT + segment(1, video_samples(2)))
        self.assertEqual(sorted(remuxer.tracks), [1])
        self.assertEqual((sections(first)[0][5] >> 1) & 0x1F, 0)
        remuxer.init(AV_INIT)
        changed = remuxer.remux(segment(1, video_samples(2), tfdt=7200))
        self.assertEqual((sections(changed)[fmp4._PMT_PID][5] >> 1) & 0x1F, 1)
        remuxer.init(AV_INIT)  # the same again: no new version
        self.assertEqual(
            (sections(remuxer.remux(segment(1, video_samples(2))))[0][5] >> 1) & 0x1F, 1
        )


class FixtureTests(unittest.TestCase):
    """The synthetic fixtures remux as golden.json says (checked against ffmpeg locally)."""

    def test_the_fixtures_remux_to_their_golden_frames(self):
        golden = json.loads(GOLDEN.read_text())
        for rendition in RENDITIONS:
            with self.subTest(rendition=rendition):
                self.assertEqual(table(remuxed(rendition)[0]), golden[rendition])

    def test_the_segmenter_cuts_a_remuxed_stream_at_its_keyframes(self):
        data, _, _ = remuxed("avc-aac")
        keys = {f.pts for f in frames(data)[fmp4._VIDEO_PID] if f.key}
        segmenter = Segmenter(clock=lambda: 0.0)
        events = segmenter.feed(data) + segmenter.finish()
        opened = [event for event in events if isinstance(event, Opened)]
        self.assertGreaterEqual(len(opened), 3)
        self.assertTrue(all(event.first_pts in keys for event in opened))


@unittest.skipIf(os.environ.get("CI"), "local only: ffmpeg's decoders vary by build")
@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class FfmpegTests(unittest.TestCase):
    """ffmpeg decodes each remuxed fixture as it decodes the fMP4 (no errors, the same frames
    in the same order, the same first times)."""

    @staticmethod
    def decoded(path: str) -> tuple[dict[int, list[str]], str]:
        run = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-map", "0", "-f", "framemd5", "-"],
            capture_output=True,
            text=True,
        )
        hashes: dict[int, list[str]] = {}
        for line in run.stdout.splitlines():
            if line and not line.startswith("#"):
                fields = [field.strip() for field in line.split(",")]
                hashes.setdefault(int(fields[0]), []).append(fields[5])
        return hashes, run.stderr.strip()

    @staticmethod
    def first_times(path: str) -> dict[str, float]:
        run = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,start_time",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
        )
        return {s["codec_type"]: float(s["start_time"]) for s in json.loads(run.stdout)["streams"]}

    def test_the_remuxed_fixtures_decode_as_their_fmp4(self):
        with tempfile.TemporaryDirectory() as folder:
            for rendition in RENDITIONS:
                with self.subTest(rendition=rendition):
                    data, _, _ = remuxed(rendition)
                    path = os.path.join(folder, rendition.replace("/", "-") + ".ts")
                    Path(path).write_bytes(data)
                    source = str(FIXTURES / rendition / "index.m3u8")
                    expected, _ = self.decoded(source)
                    got, errors = self.decoded(path)
                    self.assertEqual(errors, "")
                    self.assertEqual(
                        sorted(map(len, got.values())), sorted(map(len, expected.values()))
                    )
                    self.assertEqual(sorted(got.values()), sorted(expected.values()))
                    times = self.first_times(path)
                    if rendition != "hevc-eac3":
                        self.assertEqual(times, self.first_times(source))
                    else:
                        # ffmpeg's demuxer counts the video's empty edit and its negative
                        # composition offsets both (0.16 s); the edit list alone says 0.08 s,
                        # as the same encoder's first picture in the other fixtures.
                        self.assertEqual(
                            times, {"video": 0.08, "audio": self.first_times(source)["audio"]}
                        )


if __name__ == "__main__":
    if "--golden" in sys.argv:
        write_golden()
    else:
        unittest.main()
