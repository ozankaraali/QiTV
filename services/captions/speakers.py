"""Who speaks each caption line (2026-11-08, the owner: "we need diarization otherwise dialogs
write single"): in a dialogue, two people's lines came one after the other as if one person
spoke them.

A small speaker model (sherpa-onnx's SpeakerEmbeddingExtractor, models.SPEAKER_FILE) turns a
line's voiced sound into a voice print (`Voices`), whichever caption model wrote the line.
`Speakers` keeps the last few voices heard (KEEP, each a running average of its prints that
forgets as time goes by) and says whose a line is: the voice its print is most like (the
speaker before's while within PREFER of it), if it is KNOWN_ABOVE like it, so a dialogue going
back and forth (A, B, then A again) finds each voice again; like none (NEW_BELOW), a new
speaker's. A line with less than MIN_VOICED_S of voiced sound gives no print, and goes with the
speaker before. `Recent` keeps the run's last seconds of placed sound and which of its windows
are voiced, so a line (start, end on the timeline) of any writer finds its own sound.

Tuned (2026-11-08) on 8.8 minutes of two-person Turkish drama dialogue, its changes of speaker
labelled from the subtitles, read live-like by the captions process with each writer (Moonshine's
growing writer, the streaming FastConformer, ufak's pieces), 568 lines: of the changes marked,
81 % are changes, and 75 % of the changes are marked. A speaker keeps one voice (one colour) for
90 % of their lines; a colour flips about 3.5 times a minute of dialogue. Short lines are the
hard part: a line like no voice heard starts a new one only from MIN_NEW_S of voiced sound (from
0.3 s, half of the changes marked were wrong). A line that holds two speakers (a piece cut across
a turn: a third of ufak's) gets one voice.

How a change shows (the Subtitles panel's Speaker marks, a run from source): DASHES, as Turkish
subtitles write dialogue, the new speaker's line starting with "- " on a line of its own, and
both speakers' lines marked when they show together (`dashed`, `joined`); or COLOURS, each
speaker's lines in a colour of their own, as teletext subtitles do (white, yellow, cyan and
green, in the order the speakers come: `Colours`, the colour marked with the line, `coloured`).
Nothing changes while one speaker goes on.

No Qt, no numpy (the captions process has neither).
"""

from __future__ import annotations

from array import array
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import math
import threading
from typing import Any, Optional

RATE = 16_000
WINDOW = 512  # the voice detector's window (services.captions.pieces.WINDOW): 32 ms
KEEP_S = 40.0  # the sound kept for the lines still to come (a line is out within seconds)
MIN_VOICED_S = 0.3  # a line with less voiced sound than this is too short to tell
MIN_PRINT_S = 0.3  # ... and with less than this, no print is taken at all
MIN_NEW_S = 1.25  # a line like no voice heard is a new speaker's from this much voiced sound
MIN_LEARN_S = 0.3  # a voice's average learns from lines with this much voiced sound
# A print this like a voice heard lately (its running average) is that voice's (the most alike
# one's; the one before's while it is within PREFER of it), and one less like every voice than
# NEW_BELOW, long enough, is a new speaker's. Tuned on the caption lines (see above).
KNOWN_ABOVE = 0.35
NEW_BELOW = 0.35
PREFER = 0.0
KEEP = 4  # voices kept: a scene's speakers
HALF_LIFE_S = 30.0  # a voice's average forgets its older lines at this rate
FORGET_S = 180.0  # a voice not heard for this long is forgotten
MOST_WEIGHT = 8.0  # a voice's average never weighs more lines than this (it follows the voice)
OFF, DASHES, COLOURS = "off", "dashes", "colours"  # how a change of speaker shows
MARKS = (OFF, DASHES, COLOURS)
DASH = "- "
COLOUR_COUNT = 4  # white, yellow, cyan, green (ui.overlays.captions paints them)
# a line's colour, marked before its words (Unicode's private use: never in a model's text)
_COLOUR_MARKS = tuple(chr(0xE000 + k) for k in range(COLOUR_COUNT))


def unit(vector: Sequence[float]) -> list[float]:
    """`vector` at length 1 (a zero vector as it is)."""
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm > 0 else list(vector)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """How alike two prints are, both of length 1: 1 the same, 0 nothing alike."""
    return sum(x * y for x, y in zip(a, b))


@dataclass
class Turn:
    """Who speaks a line: speaker 1, 2, ... in the run (S1, S2 in the log)."""

    speaker: int
    similarity: Optional[float]  # to the speaker before's voice (None: none to compare with)
    changed: bool  # another speaker than the line before's
    trusted: bool = True  # enough voiced sound to tell

    def tag(self) -> str:
        """The log's words for it: "S2 0.31 new", "S1 0.82", "S1 short"."""
        words = [f"S{self.speaker}"]
        if self.similarity is not None:
            words.append(f"{self.similarity:.2f}")
        if not self.trusted:
            words.append("short")
        elif self.changed:
            words.append("new")
        return " ".join(words)


@dataclass
class _Voice:
    number: int
    centre: Optional[list[float]]  # the running average of its prints, length 1
    weight: float
    heard: float  # the timeline second of its last line


class Speakers:
    """The voices heard in a run, and whose each line is (`hear`)."""

    def __init__(
        self,
        known_above: float = KNOWN_ABOVE,
        prefer: float = PREFER,
        *,
        new_below: float = NEW_BELOW,
        keep: int = KEEP,
        min_voiced: float = MIN_VOICED_S,
        min_new: float = MIN_NEW_S,
        min_learn: float = MIN_LEARN_S,
        half_life: float = HALF_LIFE_S,
        forget: float = FORGET_S,
    ) -> None:
        self.known_above = known_above
        self.prefer = prefer
        self.new_below = new_below
        self.keep = keep
        self.min_voiced = min_voiced
        self.min_new = min_new
        self.min_learn = min_learn
        self.half_life = half_life
        self.forget = forget
        self._voices: list[_Voice] = []
        self._last: Optional[_Voice] = None
        self._count = 0

    def hear(self, print_: Optional[Sequence[float]], voiced_s: float, at: float) -> Turn:
        """The line at timeline second `at` with this voice print (None: none taken) of
        `voiced_s` seconds of voiced sound: the speaker before's, one heard lately, or new."""
        self._voices = [v for v in self._voices if v is self._last or at - v.heard < self.forget]
        vector = unit(print_) if print_ else None
        last = self._last
        similar = None
        if vector is not None and last is not None and last.centre is not None:
            similar = cosine(vector, last.centre)
        if vector is None or voiced_s < self.min_voiced:
            if last is None:
                last = self._last = self._add(None, at)
            return Turn(last.number, similar, False, trusted=False)
        heard = [(cosine(vector, v.centre), v) for v in self._voices if v.centre is not None]
        if not heard:
            if last is None:
                last = self._last = self._add(vector, at)
            else:
                self._update(last, vector, at)
            return Turn(last.number, None, False)
        alike, voice = max(heard, key=lambda pair: pair[0])
        sure = voiced_s >= self.min_new  # long enough to start a new voice
        if last is not None and similar is not None and similar >= alike - self.prefer:
            # the speaker before is (about) the most alike: they go on, unless their voice is
            # nothing like this one
            if similar >= self.new_below:
                alike, voice = similar, last
            elif sure:
                voice = self._add(vector, at)
                alike = 1.0
            else:  # like nobody, but too short to be sure of a new voice
                return Turn(last.number, similar, False, trusted=False)
        elif alike < self.known_above:  # another voice is more alike, but not alike enough
            if alike < self.new_below and sure:
                voice = self._add(vector, at)  # nobody heard lately: a new speaker
                alike = 1.0
            elif last is not None:
                return Turn(last.number, similar, False, trusted=False)
        if alike >= self.known_above and voiced_s >= self.min_learn and voice.centre is not vector:
            self._update(voice, vector, at)  # its average learns from lines it is sure of
        voice.heard = max(voice.heard, at)
        self._last = voice
        return Turn(voice.number, similar, voice is not last)

    def _add(self, vector: Optional[list[float]], at: float) -> _Voice:
        self._count += 1
        voice = _Voice(self._count, vector, 1.0, at)
        self._voices.append(voice)
        if len(self._voices) > self.keep:  # the one not heard for longest goes
            oldest = min((v for v in self._voices if v is not voice), key=lambda v: v.heard)
            self._voices.remove(oldest)
        return voice

    def _update(self, voice: _Voice, vector: list[float], at: float) -> None:
        if voice.centre is None:
            voice.centre, voice.weight, voice.heard = vector, 1.0, at
            return
        weight = voice.weight * 0.5 ** (max(0.0, at - voice.heard) / self.half_life)
        voice.centre = unit([weight * c + x for c, x in zip(voice.centre, vector)])
        voice.weight = min(MOST_WEIGHT, weight + 1.0)
        voice.heard = max(voice.heard, at)


class Recent:
    """The run's last KEEP_S seconds of placed sound, in voice detector windows, each with
    whether it is voiced: a line's voiced sound (`voiced`). `push` from the thread that places
    the sound, `voiced` from the ones that write the lines."""

    def __init__(self, is_voiced: Optional[Callable[[array], bool]], keep_s: float = KEEP_S):
        self._is_voiced = is_voiced
        self._keep_s = keep_s
        self._windows: deque[tuple[float, array, bool]] = deque()
        self._rest = array("f")  # the start of the next window
        self._rest_at: Optional[float] = None
        self._lock = threading.Lock()

    def push(self, at: float, samples: Sequence[float]) -> None:
        """Placed sound from timeline second `at`; a jump starts the windows afresh there."""
        if self._rest_at is None or abs(at - (self._rest_at + len(self._rest) / RATE)) > 0.02:
            self._rest, self._rest_at = array("f"), at
        self._rest.extend(samples)
        done: list[tuple[float, array, bool]] = []
        while len(self._rest) >= WINDOW:
            window = self._rest[:WINDOW]
            del self._rest[:WINDOW]
            voiced = True
            if self._is_voiced is not None:
                try:
                    voiced = bool(self._is_voiced(window))
                except Exception:
                    voiced = False
            done.append((self._rest_at, window, voiced))
            self._rest_at += WINDOW / RATE
        if not done:
            return
        with self._lock:
            self._windows.extend(done)
            newest = done[-1][0]
            while self._windows and self._windows[0][0] < newest - self._keep_s:
                self._windows.popleft()

    def voiced(self, start: float, end: float) -> array:
        """The voiced sound of timeline seconds [start, end) (each window whose middle is in)."""
        half = WINDOW / RATE / 2
        out = array("f")
        with self._lock:
            for at, window, voiced in self._windows:
                if voiced and start <= at + half < end:
                    out.extend(window)
        return out


class Voices:
    """The speaker model (sherpa-onnx's SpeakerEmbeddingExtractor, from the runtime given):
    a voice print of a stretch of voiced sound, or None when it is too short for one."""

    def __init__(self, sherpa_onnx: Any, model: str, threads: int = 1) -> None:
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model, num_threads=threads)
        if not config.validate():
            raise ValueError("the speaker model's files are not all there")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self._lock = threading.Lock()

    def embed(self, samples: array) -> Optional[list[float]]:
        if len(samples) < MIN_PRINT_S * RATE:
            return None
        with self._lock:
            stream = self._extractor.create_stream()
            stream.accept_waveform(RATE, samples)
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                return None
            return list(self._extractor.compute(stream))


class Colours:
    """Each speaker's colour, in the order the speakers come (S1 white, S2 yellow, ...); a fifth
    takes the colour of the one heard longest ago, so the four latest are told apart."""

    def __init__(self, count: int = COLOUR_COUNT) -> None:
        self._count = count
        self._of: dict[int, int] = {}  # speaker: colour, the latest heard last
        self._order: list[int] = []

    def of(self, speaker: int) -> int:
        if speaker in self._of:
            self._order.remove(speaker)
        else:
            taken = set(self._of.values())
            free = [k for k in range(self._count) if k not in taken]
            if free:
                self._of[speaker] = free[0]
            else:
                oldest = self._order.pop(0)
                self._of[speaker] = self._of.pop(oldest)
        self._order.append(speaker)
        return self._of[speaker]


def coloured(text: str, colour: int) -> str:
    """A line in colour `colour` (0: white ... 3: green): its mark before its words."""
    return _COLOUR_MARKS[colour] + plain(text)


def colour_of(line: str) -> tuple[Optional[int], str]:
    """A line's colour (None: the captions' own) and its words."""
    if line[:1] in _COLOUR_MARKS:
        return _COLOUR_MARKS.index(line[0]), line[1:]
    return None, line


def plain(line: str) -> str:
    """A line's words without its colour."""
    return colour_of(line)[1]


def dashed(text: str) -> str:
    """One speaker's line marked as dialogue: "- " first (once)."""
    return text if text.startswith(DASH) else DASH + text


def joined(before: str, line: str, turn: bool, mark: str = DASHES) -> str:
    """`line` joined to the caption `before` it: on the same line while the speaker goes on;
    another speaker's on a line of its own, both speakers' lines dashed (DASHES) or each in its
    speaker's colour (COLOURS, `line` coloured already)."""
    if not turn:
        return f"{before} {plain(line)}"
    if mark == COLOURS:
        return f"{before}\n{line}"
    return f"{dashed(before)}\n{dashed(line)}"
