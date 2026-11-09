"""Where one speaker gives way to another, apart from who they are (2026-11-09, the owner:
"dialogs are mostly theater thing short fast dialog exchanges ... cut points especially male to
female should be straightforward"). Sortformer and the voice prints (services.captions.sortformer)
still decide who speaks, and so each colour; the voice's pitch says male or female for a part of
a line when it is clear (`Pitch.gender`), and a clear flip between adjacent parts is a cut
whatever the diarizer heard (services.captions.speakers.told_parts: a dash, or a fresh colour).

The pitch is an ONNX graph written here (services.captions.onnx_graph: each frame's
autocorrelation through its power spectrum), run through the onnxruntime library the sherpa-onnx
runtime carries (services.captions.onnx_c: no numpy); about 0.005 core-seconds a second of sound.
pyannote's segmentation 3.0 was tried for the cuts too and left out: on the 8.8-minute set it found
17 changes inside lines, none kept, and its "the line begins with another speaker" made cut
precision 0.84 -> 0.63. No Qt.
"""

from __future__ import annotations

from array import array
import math
from pathlib import Path
import threading
from typing import Any, Optional, Sequence

from services.captions import onnx_graph

RATE = 16_000

# ---- the pitch ---------------------------------------------------------------------------
PITCH_FRAME = 640  # 40 ms a frame
PITCH_HOP = 320
PITCH_FFT = 1024
LOWEST_HZ, HIGHEST_HZ = 70, 450
VOICED_RATIO = 0.4  # a frame whose autocorrelation peak is this much of its energy is voiced
QUIET = 0.01  # ... and louder than this (RMS)
MIN_VOICED = 10  # a part with fewer voiced frames than this (0.2 s) has no pitch said
# Chosen on Common Voice Turkish speakers of known gender and the 8.8-minute set (2026-11-09): a
# part whose median pitch is under MALE_BELOW is a man's, over FEMALE_ABOVE a woman's, between
# them unsure. Two parts of one Common Voice speaker's are told apart so 0.8 % of the time (a
# man's and a woman's 24 %); on the set, man-woman cuts found 0.78 -> 0.81 (Moonshine's lines)
MALE_BELOW = 125.0
FEMALE_ABOVE = 210.0
PITCH_GRAPH = "pitch.onnx"
_LAGS = range(RATE // HIGHEST_HZ, RATE // LOWEST_HZ + 1)


def _hann(n: int) -> list[float]:
    return [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]


def pitch_graph() -> bytes:
    """Frames [T, PITCH_FRAME] in; each one's autocorrelation at the lags of LOWEST_HZ to
    HIGHEST_HZ [T, lags] and at lag 0 [T, 1] out (a Hann window, zero-padded to PITCH_FFT: the
    power spectrum's inverse transform as one more matrix product)."""
    bins = PITCH_FFT // 2 + 1
    window = _hann(PITCH_FRAME)
    real = array("f", bytes(4 * PITCH_FRAME * bins))
    imag = array("f", bytes(4 * PITCH_FRAME * bins))
    for n in range(PITCH_FRAME):
        for k in range(bins):
            angle = 2 * math.pi * n * k / PITCH_FFT
            real[n * bins + k] = window[n] * math.cos(angle)
            imag[n * bins + k] = -window[n] * math.sin(angle)
    lags = list(_LAGS)
    back = array("f", bytes(4 * bins * len(lags)))  # r[k] = sum_f w_f P[f] cos(2 pi f k / N) / N
    zero = array("f", bytes(4 * bins))
    for f in range(bins):
        weight = (1.0 if f in (0, bins - 1) else 2.0) / PITCH_FFT
        zero[f] = weight
        for j, lag in enumerate(lags):
            back[f * len(lags) + j] = weight * math.cos(2 * math.pi * f * lag / PITCH_FFT)
    G = onnx_graph.Node
    nodes = [
        G("MatMul", ["frames", "real"], ["re"]),
        G("MatMul", ["frames", "imag"], ["im"]),
        G("Mul", ["re", "re"], ["re2"]),
        G("Mul", ["im", "im"], ["im2"]),
        G("Add", ["re2", "im2"], ["power"]),
        G("MatMul", ["power", "back"], ["auto"]),
        G("MatMul", ["power", "zero"], ["energy"]),
    ]
    weights = [
        ("real", [PITCH_FRAME, bins], real),
        ("imag", [PITCH_FRAME, bins], imag),
        ("back", [bins, len(lags)], back),
        ("zero", [bins, 1], zero),
    ]
    return onnx_graph.model(
        nodes,
        [("frames", ["t", PITCH_FRAME])],
        [("auto", ["t", len(lags)]), ("energy", ["t", 1])],
        weights,
        "pitch",
    )


class Pitch:
    """A part's pitch and, when it is clear, its speaker's gender."""

    def __init__(self, folder: str, threads: int = 1) -> None:
        from services.captions import onnx_c

        self._onnx = onnx_c
        self._api: Any = onnx_c.Api(onnx_c.find_library())
        graph = Path(folder) / PITCH_GRAPH
        wanted = pitch_graph()
        if not graph.is_file() or graph.read_bytes() != wanted:
            tmp = graph.with_name(graph.name + ".part")
            tmp.write_bytes(wanted)
            tmp.replace(graph)
        self._session: Any = onnx_c.Session(self._api, graph, threads)
        self._lock = threading.Lock()
        self._quiet = QUIET * QUIET * sum(w * w for w in _hann(PITCH_FRAME))

    def hz(self, samples: Sequence[float]) -> Optional[float]:
        """The median pitch of the voiced frames of `samples`, None with too few of them."""
        count = (len(samples) - PITCH_FRAME) // PITCH_HOP + 1
        if count < MIN_VOICED:
            return None
        frames = array("f")
        for i in range(count):
            frames.extend(samples[i * PITCH_HOP : i * PITCH_HOP + PITCH_FRAME])
        with self._lock:
            tensor = self._api.tensor(frames, (count, PITCH_FRAME), self._onnx.FLOAT)
            auto, energy = (out.floats() for out in self._session.run([tensor]))
        lags = list(_LAGS)
        width = len(lags)
        found = []
        for i in range(count):
            total = energy[i]
            if total < self._quiet:
                continue
            row = auto[i * width : (i + 1) * width]
            best = max(range(width), key=row.__getitem__)
            if row[best] >= VOICED_RATIO * total:
                found.append(RATE / lags[best])
        if len(found) < MIN_VOICED:
            return None
        found.sort()
        return found[len(found) // 2]

    def gender(self, samples: Sequence[float]) -> Optional[str]:
        """'m' or 'f' when the pitch says so clearly, else None."""
        return gender_of(self.hz(samples))


def gender_of(hz: Optional[float]) -> Optional[str]:
    if hz is None:
        return None
    if hz < MALE_BELOW:
        return "m"
    return "f" if hz > FEMALE_ABOVE else None
