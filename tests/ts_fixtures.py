"""Synthetic MPEG-TS generator for the live pipeline tests (not a test module)."""

from __future__ import annotations

SYNC = 0x47
VIDEO_PID = 0x100
AUDIO_PID = 0x101
PMT_PID = 0x1000
CLOCK = 90000
AUDIO_FRAME_TICKS = 1024 * CLOCK // 48000  # 1920
FRAME_BYTES = 400


def crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = (
                ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if crc & 0x80000000
                else (crc << 1) & 0xFFFFFFFF
            )
    return crc


def _packet(
    pid: int, payload: bytes, cc: int, *, pusi=False, af: bytes | None = None, scrambled=False
) -> bytes:
    """Build one 188-byte packet; `af` is the adaptation-field body (after its length byte)."""
    space = 184
    if af is not None:
        room = space - 1 - len(payload)  # bytes available for af body
        af = af + b"\xff" * (room - len(af))
        body = bytes([len(af)]) + af + payload
        afc = 3
    else:
        body = payload
        afc = 1
    assert len(body) == space, len(body)
    hdr = bytes(
        [
            SYNC,
            (0x40 if pusi else 0) | (pid >> 8),
            pid & 0xFF,
            ((2 if scrambled else 0) << 6) | (afc << 4) | (cc & 0x0F),
        ]
    )
    return hdr + body


def _psi_packet(pid: int, section: bytes, cc: int) -> bytes:
    section += crc32_mpeg(section).to_bytes(4, "big")
    payload = b"\x00" + section
    return _packet(pid, payload + b"\xff" * (184 - len(payload)), cc, pusi=True)


def pat(pmt_pid=PMT_PID, cc=0) -> bytes:
    body = (1).to_bytes(2, "big") + (0xE000 | pmt_pid).to_bytes(2, "big")
    head = bytes([0x00]) + (0xB000 | (5 + len(body) + 4)).to_bytes(2, "big")
    sec = head + (1).to_bytes(2, "big") + bytes([0xC1, 0, 0]) + body
    return _psi_packet(0, sec, cc)


def pmt(video_pid=VIDEO_PID, video_type=0x1B, audio_pids=(AUDIO_PID,), pcr_pid=None, cc=0) -> bytes:
    pcr_pid = video_pid if pcr_pid is None else pcr_pid
    streams = bytes([video_type]) + (0xE000 | video_pid).to_bytes(2, "big") + b"\xf0\x00"
    for a in audio_pids:
        streams += bytes([0x0F]) + (0xE000 | a).to_bytes(2, "big") + b"\xf0\x00"
    body = (0xE000 | pcr_pid).to_bytes(2, "big") + b"\xf0\x00" + streams
    head = bytes([0x02]) + (0xB000 | (5 + len(body) + 4)).to_bytes(2, "big")
    sec = head + (1).to_bytes(2, "big") + bytes([0xC1, 0, 0]) + body
    return _psi_packet(PMT_PID, sec, cc)


def _pts_bytes(pts: int, marker: int = 0x20) -> bytes:
    pts &= (1 << 33) - 1
    return bytes(
        [
            marker | ((pts >> 30) & 7) << 1 | 1,
            (pts >> 22) & 0xFF,
            ((pts >> 15) & 0x7F) << 1 | 1,
            (pts >> 7) & 0xFF,
            (pts & 0x7F) << 1 | 1,
        ]
    )


def _pcr_bytes(pcr_base: int) -> bytes:
    pcr_base &= (1 << 33) - 1
    v = (pcr_base << 15) | (0x3F << 9)  # base(33) reserved(6) ext(9)=0
    return v.to_bytes(6, "big")


def pes_packets(
    pid, stream_id, payload: bytes, pts, *, rai=False, cc_start=0, pcr=None, scrambled=False
):
    """Split one PES over packets; returns (packets, next_cc)."""
    if pts is None:
        header = b"\x00\x00\x01" + bytes([stream_id]) + b"\x00\x00" + b"\x80\x00\x00"
    else:
        header = (
            b"\x00\x00\x01"
            + bytes([stream_id])
            + b"\x00\x00"
            + b"\x80\x80\x05"
            + _pts_bytes(pts, 0x20)
        )
    data = header + payload
    out = []
    cc = cc_start
    first = True
    pos = 0
    while pos < len(data):
        af_body: bytes | None = None
        flags = 0
        extra = b""
        if first:
            if rai:
                flags |= 0x40
            if pcr is not None:
                flags |= 0x10
                extra = _pcr_bytes(pcr)
            if flags:
                af_body = bytes([flags]) + extra
        room = 184 - (len(af_body) + 1 if af_body is not None else 0)
        chunk = data[pos : pos + room]
        pos += len(chunk)
        if len(chunk) < room and af_body is None:
            # stuffing-only adaptation field: length byte, then a zero flags byte if it fits
            af_body = b"\x00" if 184 - len(chunk) >= 2 else b""
        out.append(_packet(pid, chunk, cc, pusi=first, af=af_body, scrambled=scrambled))
        cc = (cc + 1) & 0x0F
        first = False
    return out, cc


def _nal(t: int, size: int) -> bytes:
    return b"\x00\x00\x00\x01" + bytes([0x60 | t]) + b"\x55" * (size - 5)


def h264_access_unit(idr: bool) -> bytes:
    au = b"\x00\x00\x00\x01\x09\xf0"
    if idr:
        au += _nal(7, 20) + _nal(8, 8) + _nal(5, FRAME_BYTES - len(au) - 28)
    else:
        au += _nal(1, FRAME_BYTES - len(au))
    return au


def make_stream(
    seconds: float,
    *,
    fps=25,
    gop_s=1.0,
    audio=True,
    rai=True,
    video_type=0x1B,
    pts_start=900000,
    resets: tuple[float, ...] = (),
    packet_size=188,
    scrambled=False,
) -> bytes:
    n_frames = int(round(seconds * fps))
    gop_frames = max(1, int(round(gop_s * fps)))
    frame_ticks = CLOCK // fps
    ccs = {0: 0, PMT_PID: 0, VIDEO_PID: 0, AUDIO_PID: 0}
    events: list[tuple[int, int, int]] = []  # (time_ticks, order, index)
    for i in range(n_frames):
        events.append((i * frame_ticks, 1, i))
    if audio:
        for j in range(int(seconds * CLOCK) // AUDIO_FRAME_TICKS):
            events.append((j * AUDIO_FRAME_TICKS, 2, j))
    events.sort()
    reset_ticks = sorted(int(r * CLOCK) for r in resets)

    def pts_at(t: int) -> int:
        past = [r for r in reset_ticks if t >= r]
        return pts_start + (t - past[-1] if past else t)

    psi_every = CLOCK // 2
    next_psi = 0
    pkts: list[bytes] = []
    for t, kind, idx in events:
        while t >= next_psi:
            pkts.append(pat(cc=ccs[0]))
            ccs[0] = (ccs[0] + 1) & 0xF
            pkts.append(
                pmt(
                    video_type=video_type, audio_pids=(AUDIO_PID,) if audio else (), cc=ccs[PMT_PID]
                )
            )
            ccs[PMT_PID] = (ccs[PMT_PID] + 1) & 0xF
            next_psi += psi_every
        pts = pts_at(t)
        if kind == 1:
            key = idx % gop_frames == 0
            ps, ccs[VIDEO_PID] = pes_packets(
                VIDEO_PID,
                0xE0,
                h264_access_unit(key),
                pts,
                rai=rai and key,
                cc_start=ccs[VIDEO_PID],
                pcr=pts,
                scrambled=scrambled,
            )
        else:
            ps, ccs[AUDIO_PID] = pes_packets(
                AUDIO_PID, 0xC0, b"\xff\xf1" + b"\xaa" * 200, pts, cc_start=ccs[AUDIO_PID]
            )
        pkts.extend(ps)
    if packet_size == 192:
        return b"".join(b"\x00\x00\x00\x00" + p for p in pkts)
    if packet_size == 204:
        return b"".join(p + b"\x00" * 16 for p in pkts)
    return b"".join(pkts)
