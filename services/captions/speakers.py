"""Who speaks each caption line (2026-11-08, the owner: "we need diarization otherwise dialogs
write single"): in a dialogue, two people's lines came one after the other as if one person
spoke them.

A line is told in parts (`parts_of`, the owner: "when a sentence ends, still even if it changes
talker, it does not change"): split where pyannote's segmentation hears the speaker change
(services.captions.turns) and after each sentence's end, or with no segmentation at its longest
pauses; each part's sound found by the voice detector's windows. A small speaker model
(sherpa-onnx's SpeakerEmbeddingExtractor, models.SPEAKER_FILE) turns each part's voiced sound into
a voice print (`Voices`), whichever caption model wrote the line. `Speakers` keeps the last few
voices heard (KEEP, each a running average of its prints that forgets slowly) and says whose a
part is: the kept voice its print is most like, if alike enough; a new voice only from a long
part like none kept. A mistaken new voice that turns out an older one goes into it, and a voice's
colour never changes once given (the owner: "woman was yellow man was blue then man got yellow").
A part with less than MIN_VOICED_S of voiced sound gives no print. `Recent` keeps the run's last
seconds of placed sound and which of its windows are voiced, so a line of any writer finds its
sound.

Measured (2026-11-08) on 8.8 minutes of two-person Turkish drama dialogue, each subtitle cue's
speaker labelled, read live-like through the captions process by Moonshine's growing writer, the
streaming FastConformer and ufak's pieces, the speaker each cue is shown with: of the changes of
speaker shown, 88 % are changes and 63 % of the changes show (364a0f8, a speaker per line: 79 %
and 49 %); a speaker's colour flips 2.8 times a minute (3.8), and stays the same for 91 % of
their cues (87 %); 36 voices for 30 people (38). ufak's pieces, which hold several speakers'
sentences, gain most from the segmentation's splits: 93 % and 47 % (67 % and 14 %), their colours
flipping once a minute (2.4).

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
MIN_VOICED_S = 0.3  # a part with less voiced sound than this is too short to tell
MIN_PRINT_S = 0.3  # ... and with less than this, no print is taken at all
MIN_NEW_S = 1.25  # a part like no voice kept is a new speaker's from this much voiced sound
MIN_LEARN_S = 0.3  # a voice's average learns from parts with this much voiced sound
# A part goes to the kept voice its print is most like when it is at least KNOWN_ABOVE like it
# (and MARGIN more than like the second); a new speaker's when less than NEW_BELOW like every
# voice (and long, MIN_NEW_S). Away from the speaker before at once when STRONG more alike than
# its voice, else once the next part agrees. Two voices that turn out MERGE_ABOVE alike are one
# (the newer goes into the older). Tuned on the caption lines (see above): a margin and a wait
# for the next part made fewer changes show, no steadier colours, so neither is asked for.
KNOWN_ABOVE = 0.3
MARGIN = 0.0
NEW_BELOW = 0.3
STRONG = 0.0
MERGE_ABOVE = 0.6
KEEP = 6  # voices kept: a scene's speakers and the last scene's
HALF_LIFE_S = 90.0  # a voice's average forgets its older parts at this rate
FORGET_S = 300.0  # a voice not heard for this long is forgotten
MOST_WEIGHT = 12.0  # a voice's average never weighs more parts than this (it follows the voice)
# A line is told in parts: split after each sentence's end and at each pause of LONG_PAUSE_S
# inside it (a sentence's end goes to the pause within SNAP_S of where its letters put it); a
# part with less than MIN_PART_S of voiced sound goes with a neighbour
SENTENCE_ENDS = (".", "?", "!", "…")
_CLOSERS = "'\"”’»)"
LONG_PAUSE_S = 0.4
MOST_PAUSES = 3  # the longest pauses of a line it is split at, at most
SNAP_S = 0.6
MIN_PART_S = 0.7
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
    heard: float  # the timeline second of its last part


class Speakers:
    """The voices heard in a run, and whose each part of a line is (`hear`)."""

    def __init__(
        self,
        known_above: float = KNOWN_ABOVE,
        *,
        margin: float = MARGIN,
        new_below: float = NEW_BELOW,
        strong: float = STRONG,
        merge_above: float = MERGE_ABOVE,
        keep: int = KEEP,
        min_voiced: float = MIN_VOICED_S,
        min_new: float = MIN_NEW_S,
        min_learn: float = MIN_LEARN_S,
        half_life: float = HALF_LIFE_S,
        forget: float = FORGET_S,
    ) -> None:
        self.known_above = known_above
        self.margin = margin
        self.new_below = new_below
        self.strong = strong
        self.merge_above = merge_above
        self.keep = keep
        self.min_voiced = min_voiced
        self.min_new = min_new
        self.min_learn = min_learn
        self.half_life = half_life
        self.forget = forget
        self._voices: list[_Voice] = []
        self._last: Optional[_Voice] = None
        self._leaning: Optional[_Voice] = None  # another voice the part before leaned to
        self._count = 0

    def hear(self, print_: Optional[Sequence[float]], voiced_s: float, at: float) -> Turn:
        """The part at timeline second `at` with this voice print (None: none taken) of
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
        heard = sorted(
            ((cosine(vector, v.centre), v) for v in self._voices if v.centre is not None),
            key=lambda pair: -pair[0],
        )
        if not heard or last is None:
            voice = last if last is not None else self._add(vector, at)
            self._update(voice, vector, at)
            self._last = voice
            return Turn(voice.number, None, False)
        alike, voice = heard[0]
        second = heard[1][0] if len(heard) > 1 else -1.0
        sure = voiced_s >= self.min_new  # long enough to start a new voice
        if voice is last:  # the speaker before goes on, unless their voice is nothing like it
            self._leaning = None
            if alike < self.new_below and sure:
                voice = self._add(vector, at)
            elif alike < self.new_below:
                return Turn(last.number, similar, False, trusted=False)
        elif alike >= self.known_above and alike - second >= self.margin:
            # another kept voice, clearly the most alike: on strong evidence at once, else once
            # the next part agrees
            if alike - (similar or 0.0) < self.strong and self._leaning is not voice:
                self._leaning = voice
                return Turn(last.number, similar, False, trusted=False)
            self._leaning = None
        elif alike < self.new_below and sure:  # like none of them: a new speaker
            self._leaning = None
            voice = self._add(vector, at)
        else:  # no voice clearly: the speaker before goes on
            return Turn(last.number, similar, False, trusted=False)
        if voice.centre is not vector and voiced_s >= self.min_learn:
            if cosine(vector, voice.centre or vector) >= self.known_above:
                self._update(voice, vector, at)  # its average learns from parts it is sure of
        voice.heard = max(voice.heard, at)
        voice, last = self._merged(voice, last)
        self._last = voice
        return Turn(voice.number, similar, voice is not last)

    def _merged(self, voice: _Voice, last: _Voice) -> tuple[_Voice, _Voice]:
        """`voice` and a voice as alike as one (a mistaken new one) are one from now on: the
        newer goes into the older, keeping its number (and so its colour)."""
        if voice.centre is None:
            return voice, last
        for other in self._voices:
            if other is voice or other.centre is None:
                continue
            if cosine(voice.centre, other.centre) < self.merge_above:
                continue
            older, newer = (voice, other) if voice.number < other.number else (other, voice)
            total = older.weight + newer.weight
            pairs = zip(older.centre or [], newer.centre or [])
            older.centre = unit([older.weight * a + newer.weight * b for a, b in pairs])
            older.weight = min(MOST_WEIGHT, total)
            older.heard = max(older.heard, newer.heard)
            self._voices.remove(newer)
            if self._leaning is newer:
                self._leaning = older
            voice = older if voice is newer else voice
            last = older if last is newer else last
            break  # (one at a time: the next part may find another)
        return voice, last

    def _add(self, vector: Optional[list[float]], at: float) -> _Voice:
        self._count += 1
        voice = _Voice(self._count, vector, 1.0, at)
        self._voices.append(voice)
        if len(self._voices) > self.keep:  # the one not heard for longest goes
            others = [v for v in self._voices if v is not voice and v is not self._last]
            self._voices.remove(min(others, key=lambda v: v.heard))
        return voice

    def _update(self, voice: _Voice, vector: list[float], at: float) -> None:
        if voice.centre is None:
            voice.centre, voice.weight, voice.heard = vector, 1.0, at
            return
        weight = voice.weight * 0.5 ** (max(0.0, at - voice.heard) / self.half_life)
        voice.centre = unit([weight * c + x for c, x in zip(voice.centre, vector)])
        voice.weight = min(MOST_WEIGHT, weight + 1.0)
        voice.heard = max(voice.heard, at)


@dataclass(frozen=True)
class Part:
    """A part of a line that may be another speaker's (`parts_of`): its words and its sound."""

    text: str
    start: float
    end: float
    # the segmentation (services.captions.turns) heard another speaker begin with it (True), or
    # the one before go on (False); None: it said nothing
    new: Optional[bool] = None


def _ends_sentence(word: str) -> bool:
    return word.rstrip(_CLOSERS).endswith(SENTENCE_ENDS)


def parts_of(
    text: str,
    start: float,
    end: float,
    voiced: Optional[Sequence[bool]] = None,
    at: Optional[float] = None,
    *,
    pause: float = LONG_PAUSE_S,
    most: int = MOST_PAUSES,
    min_voiced: float = MIN_PART_S,
    changes: Optional[Sequence[float]] = None,
) -> list[Part]:
    """`text`, a line of timeline seconds [start, end), in parts that may each be another
    speaker's: split after each sentence's end and where the segmentation heard the speaker
    change (`changes`, timeline seconds; services.captions.turns), else (none ran) at the `most`
    longest pauses of `pause` or more. The words are spread over the voiced windows by their
    letters (`voiced`: the voice detector's verdict per WINDOW from timeline second `at`, `start`
    by default), a sentence's end going to the longest pause within SNAP_S of where they put it;
    without them, over [start, end). A part with less than `min_voiced` of voiced sound (of time,
    without them) goes with a neighbour: the next when it begins another speaker's turn the
    next goes on, else the one before (the first, the next). Each part says whether the
    segmentation heard another speaker begin with it (`Part.new`)."""
    words = text.split()
    if len(words) < 2 or end <= start:
        return [Part(text, start, end)]
    step = WINDOW / RATE
    origin = start if at is None else at
    flags = list(voiced or ())
    first = max(0, int((start - origin) / step + 1e-6))
    last = max(first, min(len(flags), math.ceil((end - origin) / step - 1e-6)))
    on = [i for i in range(first, last) if flags[i]]
    after, count = [], 0
    for word in words:
        count += len(word) + 1
        after.append(count)

    def when(share: float) -> float:
        if not on:
            return start + share * (end - start)
        return origin + on[min(len(on) - 1, int(share * len(on)))] * step

    pauses = []  # (first window, last window + 1, voiced windows before it) inside the line
    quiet, before = None, 0
    for i in range(first, last):
        if flags[i]:
            if quiet is not None and before:
                pauses.append((quiet, i, before))
            quiet, before = None, before + 1
        elif quiet is None:
            quiet = i
    cuts: dict[int, float] = {}
    for i in range(len(words) - 1):
        if _ends_sentence(words[i]):
            t = when(after[i] / count)
            near = [p for p in pauses if abs(origin + (p[0] + p[1]) / 2 * step - t) <= SNAP_S]
            if near:
                quiet_from, quiet_to, _ = max(near, key=lambda p: p[1] - p[0])
                t = origin + (quiet_from + quiet_to) / 2 * step
            cuts[i] = t
    turned: set[int] = set()  # the cuts where the segmentation heard another speaker begin

    def nearest(t: float) -> int:
        """The word boundary at timeline second `t`, by the voiced sound before it."""
        if on:
            share = sum(1 for i in on if origin + (i + 0.5) * step < t) / len(on)
        else:
            share = (t - start) / (end - start)
        return min(range(len(words) - 1), key=lambda j: abs(after[j] / count - share))

    if changes is not None:
        ends = list(cuts)  # a speaker changes at a sentence's end more often than not
        for t in changes:
            if start < t < end:
                close = [i for i in ends if abs(cuts[i] - t) <= SNAP_S]
                i = min(close, key=lambda j: abs(cuts[j] - t)) if close else nearest(t)
                cuts[i] = t
                turned.add(i)
    else:
        long = [p for p in pauses if (p[1] - p[0]) * step >= pause - 1e-6]
        for quiet_from, quiet_to, _before in sorted(long, key=lambda p: p[0] - p[1])[:most]:
            t = origin + (quiet_from + quiet_to) / 2 * step
            cuts.setdefault(nearest(t), t)
    told = changes is not None  # (the segmentation ran: a cut with no change is the same voice)
    parts: list[Part] = []
    begun, since, new = 0, start, None
    for i in sorted(cuts):
        if since < cuts[i] < end:
            parts.append(Part(" ".join(words[begun : i + 1]), since, cuts[i], new))
            begun, since = i + 1, cuts[i]
            new = (i in turned) if told else None
    parts.append(Part(" ".join(words[begun:]), since, end, new))

    def sound(part: Part) -> float:
        if not flags:
            return part.end - part.start
        lo = max(0, int((part.start - origin) / step + 1e-6))
        hi = min(len(flags), math.ceil((part.end - origin) / step - 1e-6))
        return sum(flags[lo:hi]) * step

    while len(parts) > 1:
        short = next((k for k, part in enumerate(parts) if sound(part) < min_voiced), None)
        if short is None:
            break
        ahead = short + 1 < len(parts) and parts[short + 1].new is not True
        a = short if short == 0 or (parts[short].new and ahead) else short - 1
        parts[a : a + 2] = [
            Part(
                f"{parts[a].text} {parts[a + 1].text}",
                parts[a].start,
                parts[a + 1].end,
                parts[a].new,
            )
        ]
    return parts


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

    def flags(self, start: float, end: float) -> tuple[list[bool], float]:
        """The voice detector's verdicts of the windows of timeline seconds [start, end), and
        the first one's second (`start` when none is kept)."""
        half = WINDOW / RATE / 2
        with self._lock:
            kept = [(at, voiced) for at, _w, voiced in self._windows if start <= at + half < end]
        return [voiced for _at, voiced in kept], (kept[0][0] if kept else start)

    def sound(self, start: float, end: float) -> tuple[array, float]:
        """All the sound of timeline seconds [start, end) kept (each window whose middle is
        in), and the first window's second (`start` when none is kept)."""
        half = WINDOW / RATE / 2
        out = array("f")
        first: Optional[float] = None
        with self._lock:
            for at, window, _voiced in self._windows:
                if start <= at + half < end:
                    if first is None:
                        first = at
                    out.extend(window)
        return out, (start if first is None else first)

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
    """Each speaker's colour, in the order the speakers come (S1 white, S2 yellow, ...), never
    changed once given; past four, a new speaker shares the colour of the one heard longest
    ago."""

    def __init__(self, count: int = COLOUR_COUNT) -> None:
        self._count = count
        self._of: dict[int, int] = {}  # speaker: colour
        self._heard: list[int] = []  # the speakers, the one heard longest ago first

    def of(self, speaker: int) -> int:
        if speaker in self._heard:
            self._heard.remove(speaker)
        if speaker not in self._of:
            taken = {self._of[s] for s in self._heard}
            free = [k for k in range(self._count) if k not in taken]
            self._of[speaker] = free[0] if free else self._of[self._heard[0]]
        self._heard.append(speaker)
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
