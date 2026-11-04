"""Speech cut into pieces for the speech model (2.9, AI captions).

A voice detector (Silero, through sherpa-onnx's `VadModel.is_speech`) says per 32 ms window
whether someone speaks. A piece starts a little before the first speech, ends a little after
it once a pause of `END_PAUSE_S` follows, and is cut sooner once it reaches `max_s`: at the
latest short pause between words (`WORD_PAUSE_S`) in its second half, else at its quietest
window. `max_s` follows the lead the captions have over the picture (the caller sets it): a
small lead right after a tune wants short pieces that are ready in time; a large one gives
the model more to hear at once, and more accuracy.

Each piece says how much of it was speech: a piece that was mostly not (music the detector
caught now and then) is where models write what nobody said. Times are seconds on the
time-shift timeline; samples are 16 kHz mono floats in an `array("f")`. Only the standard
library is used (no numpy in the captions runtime).
"""

from __future__ import annotations

from array import array
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

RATE = 16_000
WINDOW = 512  # Silero's window at 16 kHz: 32 ms
# 2.9.3: pieces were cut too short, and the models wrote them worse than whole sentences
# (FLEURS, joined into a stream: ufakzeka-kulak's Turkish 15.5 % of words wrong in QiTV's
# pieces, 8.5 % in whole sentences; Parakeet's French 26.5 %, Qwen's Russian 37 %). A pause of
# 0.6 s ends a piece (sherpa-onnx's detector: 0.5 s; faster-whisper: 2 s), 0.3 s of sound
# before the speech and 0.25 s after it go with it (faster-whisper: 0.4 s), and a piece may
# be 10 s long when the lead allows: Turkish 8.5 %, French 10.2 %, Russian 14.5 %.
END_PAUSE_S = 0.6
WORD_PAUSE_S = 0.08
LEAD_IN_S = 0.3
TAIL_S = 0.25
MIN_PIECE_S = 0.4
MAX_PIECE_S = 10.0  # the longest a piece may ever be (the models' token caps follow its length)
MIN_MAX_S = 2.0


@dataclass
class Piece:
    start: float
    end: float
    samples: array
    speech_share: float
    split: bool = False  # 2.9.4: cut mid-speech (it reached its length), not at a pause


class Cutter:
    """Push placed sound in order (`push`); take the pieces it gives back."""

    def __init__(self, is_speech: Callable[[array], bool]) -> None:
        self._is_speech = is_speech
        self.max_s = MAX_PIECE_S
        self.reset()

    def reset(self) -> None:
        self._buf = array("f")
        self._buf_start: Optional[float] = None  # the timeline second of _buf[0]
        self._flags: list[bool] = []  # per window of _buf, from its start
        self._loud: list[float] = []
        self._piece: Optional[int] = None  # the window the open piece starts at
        self._last_speech = -1
        self._pause = 0

    def set_max(self, seconds: float) -> None:
        self.max_s = min(MAX_PIECE_S, max(MIN_MAX_S, seconds))

    # ---- in --------------------------------------------------------------------------
    def push(self, start: float, samples: array) -> list[Piece]:
        out: list[Piece] = []
        if self._buf_start is not None:
            expected = self._buf_start + len(self._buf) / RATE
            if abs(start - expected) > 0.05:  # a gap or an overlap: a new run of sound
                out += self.flush()
        if self._buf_start is None:
            self._buf_start = start
        self._buf.extend(samples)
        while (len(self._flags) + 1) * WINDOW <= len(self._buf):
            i = len(self._flags)
            window = self._buf[i * WINDOW : (i + 1) * WINDOW]
            self._flags.append(bool(self._is_speech(window)))
            self._loud.append(sum(v * v for v in window[::8]) / (WINDOW // 8))
            piece = self._step(i)
            if piece is not None:
                out.append(piece)
        self._trim()
        return out

    def flush(self) -> list[Piece]:
        """The open piece, if long enough, and a fresh start."""
        out: list[Piece] = []
        if self._piece is not None and self._last_speech >= self._piece:
            piece = self._cut(self._piece, self._last_speech + 1 + _windows(TAIL_S))
            if piece is not None:
                out.append(piece)
        self.reset()
        return out

    # ---- the state machine, per window -------------------------------------------------
    def _step(self, i: int) -> Optional[Piece]:
        speech = self._flags[i]
        if self._piece is None:
            if speech:
                self._piece = max(0, i - _windows(LEAD_IN_S))
                self._last_speech = i
                self._pause = 0
            return None
        if speech:
            self._last_speech = i
            self._pause = 0
        else:
            self._pause += 1
            if self._pause >= _windows(END_PAUSE_S):
                start = self._piece
                self._piece = None
                return self._cut(start, self._last_speech + 1 + _windows(TAIL_S))
        if (i + 1 - self._piece) >= _windows(self.max_s):
            start = self._piece
            at = self._cut_point(start, i + 1)
            self._piece = at
            return self._cut(start, at, split=True)
        return None

    def _cut_point(self, start: int, end: int) -> int:
        """Where to cut a piece that reached its length: the latest pause between words in
        its second half, else its quietest window there."""
        lo = start + (end - start) // 2
        run, best = 0, None
        for i in range(lo, end):
            if not self._flags[i]:
                run += 1
                if run >= _windows(WORD_PAUSE_S):
                    best = i - run // 2  # inside the pause
            else:
                run = 0
        if best is not None:
            return best + 1
        quietest = min(range(lo, end), key=lambda i: self._loud[i])
        return quietest + 1

    def _cut(self, start: int, end: int, *, split: bool = False) -> Optional[Piece]:
        end = min(end, len(self._flags))
        if end - start < _windows(MIN_PIECE_S) or self._buf_start is None:
            return None
        samples = self._buf[start * WINDOW : end * WINDOW]
        speech = sum(self._flags[start:end])
        return Piece(
            start=self._buf_start + start * WINDOW / RATE,
            end=self._buf_start + end * WINDOW / RATE,
            samples=samples,
            speech_share=speech / max(1, end - start),
            split=split,
        )

    def _trim(self) -> None:
        """Forget what no piece can still need."""
        keep = self._piece if self._piece is not None else len(self._flags) - _windows(LEAD_IN_S)
        keep = max(0, min(keep, len(self._flags)))
        if keep < _windows(10.0) or self._buf_start is None:
            return  # trim in steps, not per window
        del self._buf[: keep * WINDOW]
        del self._flags[:keep]
        del self._loud[:keep]
        self._buf_start += keep * WINDOW / RATE
        if self._piece is not None:
            self._piece -= keep
        self._last_speech -= keep


def _windows(seconds: float) -> int:
    return max(1, round(seconds * RATE / WINDOW))
