"""Synthetic radio sound for the tests (not a test module): MPEG audio and ADTS frames whose
bodies differ frame to frame, the station's inline metadata (ICY), and a reader of the TS that
`services.live.raw_audio.AudioMuxer` writes."""

from __future__ import annotations

PACKET = 188


def mp3_frame(index: int, *, mpeg1: bool = True) -> bytes:
    """One MPEG-1 Layer III frame (128 kbit/s, 44.1 kHz: 417 bytes) or MPEG-2 Layer III frame
    (64 kbit/s, 22.05 kHz: 208 bytes), its body numbered by `index`."""
    if mpeg1:
        head, length = bytes([0xFF, 0xFB, 0x90, 0x64]), 417
    else:
        head, length = bytes([0xFF, 0xF3, 0x80, 0x64]), 208
    body = index.to_bytes(4, "big") + bytes(length - 8)
    return head + body


def mpeg_frame(
    index: int, *, version: int = 3, layer: int = 1, bitrate: int = 9, rate: int = 0, padding=0
) -> bytes:
    """One MPEG audio frame of any kind: `version` 3 MPEG-1, 2 MPEG-2, 0 MPEG-2.5; `layer` 3
    Layer I, 2 Layer II, 1 Layer III; `bitrate` and `rate` their header indexes; its body
    numbered by `index`. Its length as the standard reckons it."""
    kbps_tables = {
        (True, 3): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
        (True, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
        (True, 1): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
        (False, 3): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
        (False, 2): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
        (False, 1): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    }
    rates = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
    mpeg1 = version == 3
    kbps, hz = kbps_tables[(mpeg1, layer)][bitrate - 1], rates[version][rate]
    if layer == 3:
        length = (12000 * kbps // hz + padding) * 4
    elif layer == 2 or mpeg1:
        length = 144000 * kbps // hz + padding
    else:
        length = 72000 * kbps // hz + padding
    head = bytes(
        [0xFF, 0xE0 | version << 3 | layer << 1 | 1, bitrate << 4 | rate << 2 | padding << 1, 0x64]
    )
    return head + index.to_bytes(4, "big") + bytes(length - 8)


def adts_frame(index: int, length: int = 371, rate: int = 4) -> bytes:
    """One ADTS AAC-LC frame (stereo, no CRC; `rate` its rate index: 4 is 44.1 kHz) of `length`
    bytes, numbered by `index`."""
    head = bytes(
        [
            0xFF,
            0xF1,
            0x40 | rate << 2,
            0x80 | (length >> 11) & 0x03,
            (length >> 3) & 0xFF,
            (length & 0x07) << 5 | 0x1F,
            0xFC,
        ]
    )
    return head + index.to_bytes(4, "big") + bytes(length - 11)


def mp3_stream(count: int, start: int = 0, *, mpeg1: bool = True) -> bytes:
    return b"".join(mp3_frame(k, mpeg1=mpeg1) for k in range(start, start + count))


def adts_stream(count: int, start: int = 0) -> bytes:
    return b"".join(adts_frame(k) for k in range(start, start + count))


def id3_tag(size: int = 32, *, timestamp: int | None = None) -> bytes:
    """An ID3v2.4 tag of `size` body bytes (a PRIV frame dating packed HLS audio with
    `timestamp`, else padding)."""
    if timestamp is not None:
        owner = b"com.apple.streaming.transportStreamTimestamp\x00"
        data = owner + timestamp.to_bytes(8, "big")
        body = b"PRIV" + len(data).to_bytes(4, "big") + b"\x00\x00" + data
        size = len(body)
    else:
        body = bytes(size)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x04\x00\x00" + syncsafe + body


def metadata_block(title: str | None) -> bytes:
    """One ICY metadata block: its length byte and the text, padded to 16 (empty: none)."""
    if title is None:
        return b"\x00"
    text = f"StreamTitle='{title}';StreamUrl='';".encode()
    text += b"\x00" * (-len(text) % 16)
    return bytes([len(text) // 16]) + text


def with_icy(sound: bytes, every: int, titles: dict[int, str] | None = None) -> bytes:
    """`sound` as a station sends it with `icy-metaint: every`: a metadata block after each
    `every` bytes, the k-th naming `titles[k]` (else empty)."""
    titles = titles or {}
    out, block = bytearray(), 0
    for at in range(0, len(sound), every):
        piece = sound[at : at + every]
        out += piece
        if len(piece) == every:
            out += metadata_block(titles.get(block))
            block += 1
    return bytes(out)


def packets(data: bytes) -> list[bytes]:
    assert len(data) % PACKET == 0, len(data)
    return [data[at : at + PACKET] for at in range(0, len(data), PACKET)]


def pid_of(packet: bytes) -> int:
    return (packet[1] & 0x1F) << 8 | packet[2]


def payload_of(packet: bytes) -> bytes:
    start = 5 + packet[4] if packet[3] & 0x20 else 4
    return packet[start:]


def pcr_of(packet: bytes) -> int | None:
    if packet[3] & 0x20 and packet[4] >= 7 and packet[5] & 0x10:
        b = packet[6:12]
        return b[0] << 25 | b[1] << 17 | b[2] << 9 | b[3] << 1 | b[4] >> 7
    return None


def pes_units(data: bytes, pid: int) -> list[tuple[int, bytes]]:
    """Each PES on `pid`: (its PTS, its payload)."""
    units: list[tuple[int, bytearray]] = []
    for packet in packets(data):
        if pid_of(packet) != pid:
            continue
        payload = payload_of(packet)
        if packet[1] & 0x40:
            assert payload[:3] == b"\x00\x00\x01", payload[:6]
            b = payload[9:14]
            pts = ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1
            units.append((pts, bytearray(payload[9 + payload[8] :])))
        elif units:
            units[-1][1].extend(payload)
    return [(pts, bytes(body)) for pts, body in units]


def stream_types(data: bytes, pmt_pid: int) -> list[int]:
    """The stream types the first PMT on `pmt_pid` lists."""
    for packet in packets(data):
        if pid_of(packet) == pmt_pid and packet[1] & 0x40:
            payload = payload_of(packet)
            section = payload[1 + payload[0] :]
            end = 3 + ((section[1] & 0x0F) << 8 | section[2]) - 4
            at = 12 + ((section[10] & 0x0F) << 8 | section[11])
            found = []
            while at + 5 <= end:
                found.append(section[at])
                at += 5 + ((section[at + 3] & 0x0F) << 8 | section[at + 4])
            return found
    return []
