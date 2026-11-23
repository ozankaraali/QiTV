"""Writing MPEG-TS: program tables and PES in packets, for the remuxers that make TS of what
is not TS: fragmented MP4 (`services.live.fmp4`), packed HLS audio (`services.live.hls_audio`)
and a radio station's raw sound (`services.live.raw_audio`).

Each PID's continuity counter runs on in a `Counters`; a section goes in one packet after its
pointer field, the rest filled with 0xFF; a PES is cut into packets whose first carries the PCR
and the random access indicator, when given, in its adaptation field, and whose last is
stuffed by its own. Pure logic.
"""

from __future__ import annotations

from services.live.ts import PACKET, SYNC, _crc32

PAYLOAD = PACKET - 4
PTS_MODULO = 1 << 33
PCR_LEAD = 63000  # the PCR runs 0.7 s before the timestamp it comes with (ffmpeg's mux delay)


def section(table: int, number: int, version: int, body: bytes) -> bytes:
    """A PSI section (PAT or PMT) with its CRC."""
    length = 5 + len(body) + 4
    head = bytes([table, 0xB0 | length >> 8, length & 0xFF]) + number.to_bytes(2, "big")
    out = head + bytes([0xC1 | version << 1, 0, 0]) + body
    return out + _crc32(out).to_bytes(4, "big")


def stamp(prefix: int, value: int) -> bytes:
    """A PES timestamp field: `prefix` (2: PTS alone, 3: PTS before a DTS, 1: DTS)."""
    value %= PTS_MODULO
    return bytes(
        [
            prefix << 4 | (value >> 29) & 0x0E | 1,
            (value >> 22) & 0xFF,
            (value >> 14) & 0xFE | 1,
            (value >> 7) & 0xFF,
            (value << 1) & 0xFE | 1,
        ]
    )


class Counters:
    """Each PID's continuity counter, run on by every packet written with it."""

    def __init__(self) -> None:
        self.values: dict[int, int] = {}

    def head(self, pid: int, start: bool, control: int) -> bytes:
        """A packet's four header bytes on `pid` (`control`: its adaptation field control)."""
        counter = self.values.get(pid, 0)
        self.values[pid] = (counter + 1) & 0x0F
        return bytes([SYNC, (0x40 if start else 0) | pid >> 8, pid & 0xFF, control | counter])


def psi_packet(counters: Counters, pid: int, data: bytes) -> bytes:
    """A section in one packet after its pointer field, the rest filled with 0xFF."""
    payload = b"\x00" + data
    return counters.head(pid, True, 0x10) + payload + b"\xff" * (PAYLOAD - len(payload))


def pes_packets(
    counters: Counters, pid: int, pes: bytes, pcr: int | None = None, key: bool = False
) -> list[bytes]:
    """A PES in TS packets on `pid`: the first with the PCR (its 90 kHz base) and the random
    access indicator in its adaptation field, the last stuffed by its own."""
    packets: list[bytes] = []
    flags = b""
    if pcr is not None or key:
        flags = bytes([(0x40 if key else 0) | (0x10 if pcr is not None else 0)])
        if pcr is not None:
            flags += ((pcr % PTS_MODULO) << 15 | 0x7E00).to_bytes(6, "big")
    at, total = 0, len(pes)
    while at < total:
        room = PAYLOAD - (1 + len(flags) if flags else 0)
        chunk = pes[at : at + room]
        start = at == 0
        at += len(chunk)
        if flags or len(chunk) < PAYLOAD:
            fill = PAYLOAD - 1 - len(chunk)  # the adaptation field's length
            if fill:
                field = bytes([fill]) + (flags or b"\x00")
                field += b"\xff" * (fill + 1 - len(field))
            else:
                field = b"\x00"
            packets.append(counters.head(pid, start, 0x30) + field + chunk)
        else:
            packets.append(counters.head(pid, start, 0x10) + chunk)
        flags = b""
    return packets


def audio_pes(stream_id: int, pts: int, payload: bytes, *, aligned: bool = True) -> bytes:
    """One audio PES: its header with the PTS alone (`aligned`: the data alignment indicator
    set, the payload starting with a frame), then `payload`."""
    header = (b"\x84" if aligned else b"\x80") + b"\x80\x05" + stamp(2, pts)
    length = len(header) + len(payload)
    size = length if length <= 0xFFFF else 0
    return b"\x00\x00\x01" + bytes([stream_id]) + size.to_bytes(2, "big") + header + payload
