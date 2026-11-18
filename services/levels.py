"""The sound's band levels for the audio screen's Stage (Plan 4: its 60 bars).

A frame every FRAME_MS (30 ms) of sound: a Hann window of the next WINDOW samples (512 at
16 kHz), its spectrum (a plain-Python FFT: QiTV has no numpy, and the captions process neither),
and BANDS (60) levels in log-spaced bands from LOW_HZ (40 Hz) to HIGH_HZ (8 kHz), each 0–1:

- a band's level is its loudest bin, in dB; a band narrower than a bin (the lowest ~24, under
  ~340 Hz) reads the spectrum at its centre, between the two bins around it;
- TILT_DB an octave lifts the highs above 1 kHz (music's spectrum falls with frequency; under
  1 kHz none, so a low note's bar stays its own);
- each is placed in the RANGE_DB below a slow running peak (it falls DECAY_DB a second and
  rises at once), never under PEAK_FLOOR_DB: a quiet song still moves, and silence stays down;
- a bar falls at most FALL a frame (it rises at once), as bars do.

`band_levels(samples, rate)` reads a stretch of sound whole; `Levels` reads a stream of it, push
after push (the running peak and the fall carry over), and `LevelStream` places it in time and
sends it in chunks (`pack`: one byte a band, 0–255), as the visualizer's helper process
(services.visualizer.helper) and the captions process (services.captions.worker) do. No Qt.
"""

from __future__ import annotations

from array import array
import bisect
import cmath
from collections.abc import Callable, Sequence
import math
import sys
from typing import Optional

BANDS = 60
LOW_HZ = 40.0
HIGH_HZ = 8000.0
FRAME_MS = 30
WINDOW = 512  # at 16 kHz; other rates: the power of two holding a frame's samples
TILT_DB = 3.0  # an octave, above 1 kHz
RANGE_DB = 45.0
PEAK_FLOOR_DB = -40.0  # dB of a full-scale sine's 0
DECAY_DB = 3.0  # a second
FALL = 0.08  # a frame
CHUNK_FRAMES = 64  # a chunk sent: about 2 s of frames
GAP_S = 0.06  # sound this far from where the last ended starts anew there

_SILENT = 1e-9


def band_edges(count: int = BANDS) -> list[float]:
    """The bands' edges in Hz: count + 1 of them, log-spaced from LOW_HZ to HIGH_HZ."""
    ratio = HIGH_HZ / LOW_HZ
    return [LOW_HZ * ratio ** (j / count) for j in range(count + 1)]


def band_of(hz: float) -> int:
    """The band `hz` falls in (-1 below the first, BANDS past the last)."""
    edges = band_edges()
    if hz < edges[0]:
        return -1
    return min(BANDS, bisect.bisect_right(edges, hz) - 1)


class _Spectrum:
    """A real FFT of `size` samples (a complex FFT of half its size, then unpacked), windowed,
    as each bin's amplitude (a full-scale sine on a bin reads 1)."""

    def __init__(self, size: int) -> None:
        half = size // 2
        self.size = size
        bits = half.bit_length() - 1
        self._order = [int(format(i, f"0{bits}b")[::-1], 2) for i in range(half)] if bits else [0]
        self._stages: list[tuple[int, int, list[complex]]] = []
        step = 2
        while step <= half:
            self._stages.append(
                (step, step // 2, [cmath.exp(-2j * math.pi * k / step) for k in range(step // 2)])
            )
            step *= 2
        self._unpack = [cmath.exp(-2j * math.pi * k / size) for k in range(half)]
        # Hann, scaled so a sine of amplitude 1 on a bin reads 1 (its coherent gain is 1/2)
        self.window = [
            (0.5 - 0.5 * math.cos(2 * math.pi * n / size)) * 4.0 / size for n in range(size)
        ]

    def amplitudes(self, samples: Sequence[float], window: Sequence[float]) -> list[float]:
        """Bins 0 … size/2 - 1 of `samples` (size of them), through `window` (self.window, or
        it scaled to the samples' unit)."""
        x = [v * w for v, w in zip(samples, window)]
        a = [complex(r, i) for r, i in zip(x[0::2], x[1::2])]
        a = [a[i] for i in self._order]
        half = len(a)
        for step, mid, twiddles in self._stages:
            for start in range(0, half, step):
                for k in range(mid):
                    i = start + k
                    j = i + mid
                    t = a[j] * twiddles[k]
                    u = a[i]
                    a[i] = u + t
                    a[j] = u - t
        conj = [c.conjugate() for c in a]
        mirror = [conj[0]] + conj[:0:-1]  # conj(Z[half - k]), Z[half] being Z[0]
        return [abs((z + m) * 0.5 - 0.5j * w * (z - m)) for z, m, w in zip(a, mirror, self._unpack)]


class Levels:
    """Band levels of one stream of sound (16 kHz mono floats -1–1, or signed 16-bit samples:
    an array of typecode "h"), frame after frame: `push` returns the frames its sound
    completed, `finish` the rest (the last ones' windows padded with silence)."""

    def __init__(self, rate: int = 16_000) -> None:
        self.rate = int(rate)
        self.hop = max(1, round(self.rate * FRAME_MS / 1000))
        size = 1
        while size < max(self.hop, WINDOW * self.rate // 16_000):
            size *= 2
        self.size = max(size, 4)
        self._spectrum = _Spectrum(self.size)
        self._bands = self._band_reads()
        self._tilt = [TILT_DB * max(0.0, math.log2(c / 1000.0)) for c in self._centres]
        self._decay = DECAY_DB * FRAME_MS / 1000.0
        self._peak = PEAK_FLOOR_DB
        self._last = [0.0] * BANDS
        self._pending: list[float] = []
        # the window for the samples held: floats (-1–1), or 16-bit ones read as they are
        self._floats = self._spectrum.window
        self._ints = [w / 32768.0 for w in self._floats]
        self._window = self._floats
        self._pushed = 0  # samples since the last drop()
        self._made = 0  # frames since the last drop()

    def _band_reads(self) -> list[tuple[int, int, float]]:
        """Each band's read of the bins: (first, last + 1, 0.0) for the bins in it, or
        (bin, bin + 1, fraction) for the spectrum at its centre between two bins."""
        bin_hz = self.rate / self.size
        top = self.size // 2 - 1
        edges = band_edges()
        self._centres = [math.sqrt(lo * hi) for lo, hi in zip(edges, edges[1:])]
        reads = []
        for (lo, hi), centre in zip(zip(edges, edges[1:]), self._centres):
            first, last = math.ceil(lo / bin_hz), math.ceil(hi / bin_hz)
            last = min(last, top + 1)
            if first < last:
                reads.append((first, last, 0.0))
            else:
                at = min(centre / bin_hz, float(top))
                low = min(int(at), top - 1)
                reads.append((low, low + 1, at - low))
        return reads

    def push(self, samples: Sequence[float]) -> list[list[float]]:
        if not len(samples):
            return []
        ints = getattr(samples, "typecode", "") == "h"
        if self._pushed == 0:
            self._window = self._ints if ints else self._floats
        if ints == (self._window is self._ints):
            self._pending.extend(samples)
        elif ints:  # (one stream is one kind: these only keep it right)
            self._pending.extend(v / 32768.0 for v in samples)
        else:
            self._pending.extend(v * 32768.0 for v in samples)
        self._pushed += len(samples)
        frames = []
        while len(self._pending) >= self.size:
            frames.append(self._frame(self._pending[: self.size]))
            del self._pending[: self.hop]
        return frames

    def finish(self) -> list[list[float]]:
        """The frames the sound pushed still holds (one a FRAME_MS of it), padded with silence."""
        frames = []
        while self._made < self._pushed // self.hop:
            window = self._pending[: self.size]
            window = window + [0.0] * (self.size - len(window))
            frames.append(self._frame(window))
            del self._pending[: self.hop]
        self.drop()
        return frames

    def drop(self) -> None:
        """Forget the sound not read yet (what comes next is from elsewhere); the running peak
        and the bars' fall stay (the same song)."""
        self._pending = []
        self._pushed = self._made = 0

    def _frame(self, window: Sequence[float]) -> list[float]:
        self._made += 1
        bins = self._spectrum.amplitudes(window, self._window)
        db = []
        for (first, last, fraction), tilt in zip(self._bands, self._tilt):
            if fraction:
                value = bins[first] + (bins[last] - bins[first]) * fraction
            else:
                value = max(bins[first:last])
            db.append(20.0 * math.log10(max(value, _SILENT)) + tilt)
        self._peak = max(max(db), self._peak - self._decay, PEAK_FLOOR_DB)
        bottom = self._peak - RANGE_DB
        levels = []
        for i, value in enumerate(db):
            level = min(1.0, max(0.0, (value - bottom) / RANGE_DB))
            level = max(level, self._last[i] - FALL)
            levels.append(level)
        self._last = levels
        return levels


def band_levels(samples: Sequence[float], rate: int) -> list[list[float]]:
    """`samples` (mono, floats -1–1 or an array("h")) as frames of BANDS levels 0–1, one a
    FRAME_MS: len(samples) // (rate * FRAME_MS / 1000) of them."""
    levels = Levels(rate)
    return levels.push(samples) + levels.finish()


def pack(frames: Sequence[Sequence[float]]) -> bytes:
    """Frames as bytes, a band a byte (0–255)."""
    return bytes(round(min(1.0, max(0.0, v)) * 255) for frame in frames for v in frame)


def unpack(blob: bytes, index: int = 0) -> list[float]:
    """Frame `index` of a pack()ed chunk, as BANDS levels 0–1."""
    return [v / 255.0 for v in blob[index * BANDS : (index + 1) * BANDS]]


class LevelStream:
    """Sound placed in time (a film's second, a file's) in, chunks of band levels out:
    `send(start_ms, frame_ms, blob)` for each CHUNK_FRAMES frames, the first starting at
    start_ms. Sound that doesn't follow on where the last ended (a seek, a skip) starts a new
    chunk where it is; a small step (under GAP_S: a decoder's priming, its stamps' rounding) is
    filled with silence or overlapped, so the frames keep to the sound's own time. Flushed, a
    last frame short by at most half (a stretch cut by a time, its samples rounded) is padded
    whole. Not thread-safe: one thread pushes."""

    def __init__(
        self,
        send: Callable[[int, int, bytes], None],
        rate: int = 16_000,
        *,
        chunk_frames: int = CHUNK_FRAMES,
    ) -> None:
        self._send = send
        self._levels = Levels(rate)
        self._rate = self._levels.rate
        self._chunk_frames = max(1, chunk_frames)
        self._origin_ms: Optional[float] = None  # where the frames since the last anchor start
        self._next_s: Optional[float] = None  # where the sound pushed last ends
        self._frames: list[list[float]] = []
        self._first = 0  # the frames' index (since the anchor) of the chunk's first
        self._count = 0  # samples pushed since the anchor

    def push(self, at_s: float, samples: Sequence[float]) -> None:
        if not len(samples):
            return
        if self._next_s is None or abs(at_s - self._next_s) > GAP_S:
            self.flush()
            self._origin_ms = at_s * 1000.0
            self._first = self._count = 0
        else:
            step = round((at_s - self._next_s) * self._rate)
            if step > 0:
                self._take([0.0] * step)  # (a small gap: silence keeps the frames in time)
            elif step < 0:
                samples, at_s = samples[-step:], at_s - step / self._rate
                if not len(samples):
                    return
        self._next_s = at_s + len(samples) / self._rate
        self._take(samples)

    def _take(self, samples: Sequence[float]) -> None:
        self._count += len(samples)
        self._add(self._levels.push(samples))

    def flush(self) -> None:
        """Send what is held (the sound ended, or moves elsewhere)."""
        if self._origin_ms is not None:
            short = -self._count % self._levels.hop
            if 0 < short <= self._levels.hop // 2:
                self._take([0.0] * short)
            self._add(self._levels.finish(), whole=True)
        self._levels.drop()
        self._next_s = None
        self._origin_ms = None

    def _add(self, frames: list[list[float]], whole: bool = False) -> None:
        for frame in frames:
            self._frames.append(frame)
            if len(self._frames) >= self._chunk_frames:
                self._out()
        if whole and self._frames:
            self._out()

    def _out(self) -> None:
        frames, self._frames = self._frames, []
        origin = self._origin_ms or 0.0
        start_ms = round(origin + self._first * FRAME_MS)
        self._first += len(frames)
        self._send(start_ms, FRAME_MS, pack(frames))


def pcm_samples(pcm: bytes) -> array:
    """Signed 16-bit little-endian samples as an array("h") (in this machine's order)."""
    ints = array("h")
    ints.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        ints.byteswap()
    return ints
