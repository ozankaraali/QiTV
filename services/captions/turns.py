"""Where the speaker changes inside a stretch of sound (2026-11-08, the owner: Speaker marks):
pyannote's segmentation 3.0 (MIT; sherpa-onnx's ONNX export, models.SEGMENTATION_FILE) says, 17 ms
at a time, which of up to three speakers of the stretch is talking (a powerset: none, one of
three, or two at once). It runs through the onnxruntime library the sherpa-onnx runtime carries
(services.captions.onnx_c: no numpy, no onnxruntime wheel), about 10 ms for each second of sound.

`Turns.changes` reads a line's sound, with some of the sound before it: the times inside the line
where one speaker gives way to another, where the line is split (services.captions.speakers.
parts_of). Its speakers are the stretch's own (A, B, C: nobody's identity), so each part is still
told by the speaker model. (Measured: its word on who began a line, against the one just before,
made the speakers worse than the speaker model's prints alone; it is not asked.)
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
import threading
from typing import Any, Optional

RATE = 16_000
FRAME = 270  # samples between the model's outputs (its receptive field's shift)
FIELD = 721  # samples one output hears (its receptive field)
CLASSES = 7  # none, A, B, C, A+B, A+C, B+C
_ONE = {1: 1, 2: 2, 3: 3}  # a class that is one speaker alone: which
MIN_RUN_S = 0.3  # a speaker heard alone for less than this is not a turn of its own
CONTEXT_S = 2.0  # the sound before a line it is read with


@dataclass
class Found:
    """What the segmentation found in a line."""

    changes: list[float] = field(default_factory=list)  # timeline seconds of each change inside


def runs(labels: list[int], shortest: int) -> list[tuple[int, int, int]]:
    """Each speaker's turns in per-frame labels (0 none, 1-3 a speaker alone, -1 two at once):
    (speaker, first frame, last frame + 1), a speaker heard for fewer than `shortest` frames left
    out, and the same speaker's turns across a gap joined."""
    found: list[tuple[int, int, int]] = []
    i = 0
    while i < len(labels):
        speaker = labels[i]
        j = i
        while j < len(labels) and labels[j] == speaker:
            j += 1
        if speaker > 0 and j - i >= shortest:
            if found and found[-1][0] == speaker:
                found[-1] = (speaker, found[-1][1], j)
            else:
                found.append((speaker, i, j))
        i = j
    return found


def frame_time(frame: float) -> float:
    """Seconds from the stretch's start to the middle of what output `frame` hears."""
    return (FIELD / 2 + frame * FRAME) / RATE


def found_in(labels: list[int], at: float, since: float) -> Found:
    """The changes in per-frame `labels` of the sound from timeline second `at`, inside the line
    that begins at `since` (the sound before it lets a turn going on into it be heard whole)."""
    turns = runs(labels, max(1, round(MIN_RUN_S * RATE / FRAME)))
    out = Found()
    inside = [t for t in turns if at + frame_time(t[2]) > since + 0.15]  # going on in the line
    for (a, _f, end), (b, start, _l) in zip(inside, inside[1:]):
        if a != b and at + frame_time((end + start) / 2) > since:
            out.changes.append(at + frame_time((end + start) / 2))
    return out


class Turns:
    """The segmentation model, through the runtime's onnxruntime library."""

    def __init__(self, model: str, threads: int = 1, library: Optional[str] = None) -> None:
        from services.captions import onnx_c

        self._onnx = onnx_c
        self._api: Any = onnx_c.Api(onnx_c.find_library(library))
        self._session: Any = onnx_c.Session(self._api, model, threads)
        self._lock = threading.Lock()

    def labels(self, samples: array) -> list[int]:
        """Per output frame: 0 nobody, 1-3 one speaker alone, -1 two at once."""
        if len(samples) < FIELD:
            return []
        with self._lock:
            tensor = self._api.tensor(array("f", samples), (1, 1, len(samples)), self._onnx.FLOAT)
            output = self._session.run([tensor])[0]
            scores = output.floats()
            frames = output.shape[1]
        labels = []
        for f in range(frames):
            row = scores[f * CLASSES : (f + 1) * CLASSES]
            best = max(range(CLASSES), key=row.__getitem__)
            labels.append(_ONE.get(best, 0 if best == 0 else -1))
        return labels

    def changes(self, samples: array, at: float, since: float) -> Found:
        """The changes of speaker in the sound of `samples` from timeline second `at`, inside
        the line that begins at `since`."""
        return found_in(self.labels(samples), at, since)
