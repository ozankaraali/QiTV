"""Who speaks, as NVIDIA's Nemotron 3 Diarization hears it (2026-11-09, the owner: "JUST DOWNLOAD
PRETRAINED DIARIZER SOTA AND CONNECT TO IT"): a streaming Sortformer (100M parameters, up to 8
speakers, OpenMDW-1.1) that keeps an arrival-order speaker cache, so a speaker keeps one index
(one colour) through a run, and says every 10 ms who is talking.

The network runs through the onnxruntime library the sherpa-onnx runtime carries
(services.captions.onnx_c: no numpy, no onnxruntime wheel), from a community ONNX export
(models.DIARIZER_FILES: `embed.onnx`, feature stacking to 80 ms embeddings, and
`step_int8.onnx`, the encoder and head with int8 weights). Around it, as the model's NeMo and
transformers code do:

- the log-mel front end (NeMo's AudioToMelSpectrogramPreprocessor as the model was trained: 16 kHz,
  pre-emphasis 0.97, a symmetric 400-sample Hann window centred in 512, hop 160, power, a Slaney
  128-band filterbank, log(x + 2^-24), no normalisation): the samples are framed here and the
  rest is a small ONNX graph written at first use (services.captions.onnx_graph: the DFT as two
  matrix products);
- the streaming: each step reads [speaker cache, FIFO, chunk, right context] embeddings; the chunk's
  frames are told then; the FIFO and the arrival-order speaker cache are updated (score-based
  compression) as `Nemotron3DiarizationSpeakerCache` does.

`Sortformer.push` takes placed sound; `Sortformer.frames` gives each told 10 ms frame's speaker
probabilities over a stretch of the timeline, and `covered` how far they are told; `changes` and
`speaker` read them for a caption line.
"""

from __future__ import annotations

from array import array
from collections import deque
from dataclasses import dataclass
import math
from pathlib import Path
import sys
import threading
from typing import Any, Optional, Sequence

from services.captions import onnx_graph

RATE = 16_000
N_FFT = 512
WINDOW_LEN = 400
HOP = 160
MELS = 128
PREEMPH = 0.97
GUARD = 2.0**-24
SUB = 8  # mel frames per embedding: an embedding (and a step's frame) is 80 ms
HIDDEN = 512
SPEAKERS = 8
FRAME_S = HOP / RATE  # 10 ms: how often it says who speaks
LOG_HALF = math.log(0.5)
KEEP_FRAMES = 6000  # the told frames kept (a minute: lines are out within seconds)
# 2026-11-09, the owner's live test: sound that does not go on from the sound before (Speaker
# marks off for a moment, a dropped block) keeps its place on the timeline: a gap up to JUMP_S is
# filled with silence, sound already read is let go, a jump further starts the front end afresh
JUMP_S = 5.0
_SLACK_S = 0.005  # (block times are rounded)
MEL_GRAPH = "mel-128.onnx"  # written beside the model on first use
MEL_VERSION = 1  # (the file's name holds no settings: a change of them writes it again)


@dataclass(frozen=True)
class Settings:
    """A streaming configuration, in 80 ms frames (the model card's names). Its delay is about
    (chunk + right_context) frames; each step reads cache + fifo + chunk + right_context."""

    chunk: int = 60  # (with right_context: told about 5.1 s after it is said)
    right_context: int = 4
    fifo: int = 60
    cache: int = 264
    update_period: int = 60
    silence_frames: int = 1
    pred_threshold: float = 0.25
    latest_boost: float = 0.05
    min_pos_rate: float = 0.5
    strong_rate: float = 0.75
    weak_rate: float = 1.5


def hann() -> list[float]:
    """torch's symmetric Hann window of WINDOW_LEN, centred in N_FFT with zeros either side."""
    window = [0.0] * N_FFT
    offset = (N_FFT - WINDOW_LEN) // 2
    for i in range(WINDOW_LEN):
        window[offset + i] = 0.5 - 0.5 * math.cos(2 * math.pi * i / (WINDOW_LEN - 1))
    return window


def mel_graph(filters: Sequence[float]) -> bytes:
    """The front end's ONNX model: windowed frames [T, N_FFT] in, log-mel features [T, MELS]
    out, with `filters` the Slaney filterbank [MELS, N_FFT // 2 + 1] (row-major)."""
    bins = N_FFT // 2 + 1
    window = hann()
    real, imag = array("f", bytes(4 * N_FFT * bins)), array("f", bytes(4 * N_FFT * bins))
    for n in range(N_FFT):
        w = window[n]
        if not w:
            continue
        for k in range(bins):
            angle = 2 * math.pi * n * k / N_FFT
            real[n * bins + k] = w * math.cos(angle)
            imag[n * bins + k] = -w * math.sin(angle)
    bank = array("f", bytes(4 * bins * MELS))  # filters transposed: [bins, MELS]
    for m in range(MELS):
        for k in range(bins):
            bank[k * MELS + m] = filters[m * bins + k]
    G = onnx_graph.Node
    nodes = [
        G("MatMul", ["frames", "real"], ["re"]),
        G("MatMul", ["frames", "imag"], ["im"]),
        G("Mul", ["re", "re"], ["re2"]),
        G("Mul", ["im", "im"], ["im2"]),
        G("Add", ["re2", "im2"], ["power"]),
        G("MatMul", ["power", "bank"], ["mel"]),
        G("Add", ["mel", "guard"], ["guarded"]),
        G("Log", ["guarded"], ["features"]),
    ]
    weights = [
        ("real", [N_FFT, bins], real),
        ("imag", [N_FFT, bins], imag),
        ("bank", [bins, MELS], bank),
        ("guard", [1], array("f", [GUARD])),
    ]
    return onnx_graph.model(
        nodes, [("frames", ["t", N_FFT])], [("features", ["t", MELS])], weights, "log_mel"
    )


def floats(path: Path) -> array:
    """A little-endian float32 file."""
    data = array("f")
    data.frombytes(path.read_bytes())
    if sys.byteorder == "big":
        data.byteswap()
    return data


class SpeakerCache:
    """The arrival-order speaker cache and the FIFO (batch of one), as transformers'
    Nemotron3DiarizationSpeakerCache keeps them: embeddings (HIDDEN floats each) and, for the
    cache, each frame's speaker probabilities once compressed."""

    def __init__(self, silence: array, settings: Settings) -> None:
        self.silence = silence
        self.s = settings
        self.embeds: list[array] = []
        self.probs: list[list[float]] = []
        self.fifo: list[array] = []
        self.compressed = False
        budget = settings.cache // SPEAKERS - settings.silence_frames
        self.min_pos = int(budget * settings.min_pos_rate)
        self.n_strong = int(budget * settings.strong_rate)
        self.n_weak = int(budget * settings.weak_rate)

    def held(self) -> list[array]:
        return self.embeds + self.fifo

    def update(self, inputs: list[array], probs: list[list[float]], chunk: int) -> None:
        """After a step over `inputs` (cache, FIFO, chunk, right context) with `probs` per input
        frame: the chunk's `chunk` frames join the FIFO, and what overflows it the cache."""
        s = self.s
        nc, nf = len(self.embeds), len(self.fifo)
        start = nc + nf
        fifo = self.fifo + inputs[start : start + chunk]
        popped = 0
        if len(fifo) > s.fifo:
            popped = min(max(s.update_period, len(fifo) - s.fifo), len(fifo))
        if popped:
            fifo_probs = probs[nc : nc + len(fifo)]
            stored = self.probs if self.compressed else probs[:nc]
            cache_e = self.embeds + fifo[:popped]
            cache_p = stored + fifo_probs[:popped]
            fifo = fifo[popped:]
            if len(cache_e) > s.cache:
                cache_e, cache_p = self._compress(cache_e, cache_p)
                self.compressed = True
            self.embeds, self.probs = cache_e, cache_p
        self.fifo = fifo

    def _scores(self, probs: list[list[float]]) -> list[list[float]]:
        th = self.s.pred_threshold
        scores = []
        positive = [0] * SPEAKERS
        for p in probs:
            lc = [math.log(max(1 - x, th)) for x in p]
            total = sum(lc)
            row = []
            for k, x in enumerate(p):
                v = math.log(max(x, th)) - lc[k] + total - LOG_HALF if x > 0.5 else -math.inf
                if v > 0:
                    positive[k] += 1
                row.append(v)
            scores.append(row)
        for t, p in enumerate(probs):
            for k in range(SPEAKERS):
                if not scores[t][k] > 0 and p[k] > 0.5 and positive[k] >= self.min_pos:
                    scores[t][k] = -math.inf
        return scores

    @staticmethod
    def _boost(scores: list[list[float]], count: int, amount: float) -> None:
        for k in range(SPEAKERS):
            order = sorted(range(len(scores)), key=lambda t: (-scores[t][k], t))
            for t in order[:count]:
                scores[t][k] += amount

    def _compress(
        self, embeds: list[array], probs: list[list[float]]
    ) -> tuple[list[array], list[list[float]]]:
        s = self.s
        n = len(probs)
        scores = self._scores(probs)
        for t in range(s.cache, n):
            scores[t] = [v + s.latest_boost for v in scores[t]]
        self._boost(scores, self.n_strong, -2 * LOG_HALF)
        self._boost(scores, self.n_weak, -LOG_HALF)
        rows = n + s.silence_frames
        flat = [
            (scores[t][k] if t < n else math.inf, k * rows + t)
            for k in range(SPEAKERS)
            for t in range(rows)
        ]
        flat.sort(key=lambda pair: (-pair[0], pair[1]))
        sentinel = rows * SPEAKERS
        picked = sorted(sentinel if v == -math.inf else i for v, i in flat[: s.cache])
        zero = [0.0] * SPEAKERS
        out_e, out_p = [], []
        for i in picked:
            f = n if i == sentinel else min(i % rows, n)
            out_e.append(self.silence if f == n else embeds[f])
            out_p.append(zero if f == n else probs[f])
        return out_e, out_p


class Sortformer:
    """A run's diarization: placed sound in (`push`, from one thread), each 10 ms's speaker
    probabilities out (`frames`, `covered`, from others). One is loaded for the captions
    process; each run starts it afresh (`reset`), and the token that gives keeps a stopped run's
    last sound out of the next one."""

    def __init__(self, folder: str, settings: Settings = Settings(), threads: int = 1) -> None:
        from services.captions import onnx_c

        base = Path(folder)
        self._onnx = onnx_c
        self._api: Any = onnx_c.Api(onnx_c.find_library())
        graph = base / MEL_GRAPH
        filters = floats(base / "mel_filters.bin")
        wanted = mel_graph(filters)
        if not graph.is_file() or graph.read_bytes() != wanted:
            tmp = graph.with_name(graph.name + ".part")
            tmp.write_bytes(wanted)
            tmp.replace(graph)
        self._mel: Any = onnx_c.Session(self._api, graph, 1)
        self._embed: Any = onnx_c.Session(self._api, base / "embed.onnx", 1)
        self._step: Any = onnx_c.Session(self._api, base / "step_int8.onnx", threads)
        self.silence = floats(base / "silence_embeds.bin")
        self.settings = settings
        self._lock = threading.Lock()  # what is told (read from other threads)
        self._work = threading.Lock()  # the sound in (a run's thread, the next run's reset)
        self._token = 0
        self.reset(0.0)

    def reset(self, at: float) -> int:
        """A new run's sound begins at timeline second `at`: the token its `push`es give."""
        with self._work:
            self._token += 1
            self._restart(at, told=True)
            return self._token

    def _restart(self, at: float, told: bool) -> None:
        """The front end from timeline second `at` on; the speakers heard so far are kept
        unless `told` (a new run)."""
        with self._lock:
            self._origin = at  # the timeline second of the first sample
            self._pre = array("f", bytes(4 * (N_FFT // 2)))  # centred: N_FFT/2 zeros before
            self._last = 0.0  # the sample before (pre-emphasis)
            self._fed = 0  # samples pushed
            self._frames_done = 0  # mel frames made
            self._mel_rows: list[array] = []  # mel frames not yet embedded
            self._embeds: list[array] = []  # embeddings from `_embeds_from` on
            self._embeds_from = 0
            self._next = 0  # the next chunk's first embedding
            if told:
                self._cache = SpeakerCache(self.silence, self.settings)
            self._told: deque[tuple[int, list[float]]] = deque()  # (10 ms frame, probabilities)
            self._told_to = 0  # 10 ms frames told so far

    # ---- in ------------------------------------------------------------------------------
    def push(self, at: float, samples: Sequence[float], token: Optional[int] = None) -> None:
        """Placed sound from timeline second `at` (a run's `token`: a stopped run's is let go),
        kept in its place: a gap after the sound before up to JUMP_S filled with silence, what
        overlaps it dropped; sound further away starts the front end there, the speakers
        heard kept."""
        with self._work:
            if token is not None and token != self._token:
                return
            gap = at - (self._origin + self._fed / RATE)
            if abs(gap) > JUMP_S:
                self._restart(at, told=False)
            elif gap > _SLACK_S:
                self._push(array("f", bytes(4 * round(gap * RATE))))
            elif gap < -_SLACK_S:
                samples = samples[round(-gap * RATE) :]
            if len(samples):
                self._push(samples)

    def _push(self, samples: Sequence[float]) -> None:
        pre = array("f")
        last = self._last
        for x in samples:
            pre.append(x - PREEMPH * last if self._fed or pre else x)
            last = x
        self._last = last
        self._fed += len(samples)
        self._pre.extend(pre)
        self._frames()
        self._steps()

    def _frames(self) -> None:
        """Mel frames for each hop whose window has all its samples."""
        count = (len(self._pre) - N_FFT) // HOP + 1 if len(self._pre) >= N_FFT else 0
        allowed = self._fed // HOP - self._frames_done  # NeMo keeps floor(samples / hop)
        count = min(count, allowed)
        if count <= 0:
            return
        frames = array("f")
        for i in range(count):
            frames.extend(self._pre[i * HOP : i * HOP + N_FFT])
        tensor = self._api.tensor(frames, (count, N_FFT), self._onnx.FLOAT)
        features = self._mel.run([tensor])[0].floats()
        for i in range(count):
            self._mel_rows.append(features[i * MELS : (i + 1) * MELS])
        del self._pre[: count * HOP]
        self._frames_done += count
        whole = len(self._mel_rows) // SUB * SUB
        if whole:
            rows = array("f")
            for mel in self._mel_rows[:whole]:
                rows.extend(mel)
            tensor = self._api.tensor(rows, (1, whole, MELS), self._onnx.FLOAT)
            out = self._embed.run([tensor])[0].floats()
            for i in range(whole // SUB):
                self._embeds.append(out[i * HIDDEN : (i + 1) * HIDDEN])
            del self._mel_rows[:whole]

    def _steps(self) -> None:
        s = self.settings
        while len(self._embeds) + self._embeds_from >= self._next + s.chunk + s.right_context:
            self._step_once(s.chunk)

    def _step_once(self, chunk: int) -> None:
        s = self.settings
        local = self._next - self._embeds_from
        ahead = self._embeds[local : local + chunk + s.right_context]
        held = self._cache.held()
        inputs = held + ahead
        data = array("f")
        for embed in inputs:
            data.extend(embed)
        tensor = self._api.tensor(data, (1, len(inputs), HIDDEN), self._onnx.FLOAT)
        logits = self._step.run([tensor])[0].floats()
        pooled: list[list[float]] = []
        told: list[list[float]] = []
        for t in range(len(inputs)):
            mean = [0.0] * SPEAKERS
            for j in range(SUB):
                base = (t * SUB + j) * SPEAKERS
                probs = [1.0 / (1.0 + math.exp(-logits[base + k])) for k in range(SPEAKERS)]
                for k in range(SPEAKERS):
                    mean[k] += probs[k] / SUB
                if len(held) <= t < len(held) + chunk:
                    told.append(probs)
            pooled.append(mean)
        self._cache.update(inputs, pooled, chunk)
        with self._lock:
            for probs in told:
                self._told.append((self._told_to, probs))
                self._told_to += 1
            while len(self._told) > KEEP_FRAMES:
                self._told.popleft()
        self._next += chunk
        drop = self._next - self._embeds_from
        if drop > 0:
            del self._embeds[:drop]
            self._embeds_from += drop

    # ---- out -----------------------------------------------------------------------------
    @property
    def token(self) -> int:
        """The run whose sound it reads (as `reset` gave it)."""
        return self._token

    def covered(self) -> float:
        """The timeline second up to which the speakers are told."""
        with self._lock:
            return self._origin + self._told_to * FRAME_S

    def frames(self, start: float, end: float) -> list[list[float]]:
        """Each told 10 ms frame's speaker probabilities in timeline seconds [start, end)."""
        with self._lock:
            first = math.ceil((start - self._origin) / FRAME_S - 1e-9)
            last = math.ceil((end - self._origin) / FRAME_S - 1e-9)
            return [p for f, p in self._told if first <= f < last]

    def time_of(self, frame_offset: int, start: float) -> float:
        """The timeline second of the `frame_offset`-th frame `frames(start, ...)` gives."""
        with self._lock:
            first = math.ceil((start - self._origin) / FRAME_S - 1e-9)
            if self._told:
                first = max(first, self._told[0][0])
            return self._origin + (max(first, 0) + frame_offset) * FRAME_S


MIN_RUN_S = 0.3  # a speaker alone for less than this is no turn of their own
MIN_SHARE = 0.15  # a stretch whose likeliest speaker talks for less of it than this: untold
# 2026-11-09, the owner's live test ("one asked one answered", both one colour): a short answer
# written as a line of its own also covers the silence after it; a frame where nobody is this
# likely is silence, and the share is of the frames where someone talks
VOICED_P = 0.2


def labels(frames: Sequence[Sequence[float]]) -> list[int]:
    """Per 10 ms frame: 0 nobody, k + 1 speaker k alone (p > 0.5), -1 two or more at once."""
    out = []
    for p in frames:
        on = [k for k, x in enumerate(p) if x > 0.5]
        out.append(on[0] + 1 if len(on) == 1 else (0 if not on else -1))
    return out


def runs(labels: list[int], shortest: int) -> list[tuple[int, int, int]]:
    """Each speaker's turns in per-frame labels (0 nobody, k > 0 a speaker alone, -1 two at
    once): (speaker, first frame, last frame + 1), a speaker heard for fewer than `shortest`
    frames left out, and the same speaker's turns across a gap joined."""
    found: list[tuple[int, int, int]] = []
    i = 0
    while i < len(labels):
        who = labels[i]
        j = i
        while j < len(labels) and labels[j] == who:
            j += 1
        if who > 0 and j - i >= shortest:
            if found and found[-1][0] == who:
                found[-1] = (who, found[-1][1], j)
            else:
                found.append((who, i, j))
        i = j
    return found


def changes(frames: Sequence[Sequence[float]], start: float) -> list[float]:
    """The timeline seconds where one speaker gives way to another in `frames`, the told 10 ms
    frames from timeline second `start` on (a turn shorter than MIN_RUN_S is none)."""
    turns = runs(labels(frames), max(1, round(MIN_RUN_S / FRAME_S)))
    return [
        start + (end + begin) / 2 * FRAME_S
        for (a, _f, end), (b, begin, _l) in zip(turns, turns[1:])
        if a != b
    ]


def speaker(frames: Sequence[Sequence[float]]) -> Optional[int]:
    """The speaker (0 to SPEAKERS - 1, in the order they came) most heard in `frames`, of those
    where someone talks (VOICED_P); None when nobody talks, or the likeliest for less than
    MIN_SHARE of them."""
    voiced = [p for p in frames if max(p) >= VOICED_P]
    if not voiced:
        return None
    mass = [sum(p[k] for p in voiced) for k in range(SPEAKERS)]
    best = max(range(SPEAKERS), key=mass.__getitem__)
    return best if mass[best] / len(voiced) >= MIN_SHARE else None
