"""Who speaks each caption line (2026-11-08, the owner: "we need diarization otherwise dialogs
write single"): in a dialogue, two people's lines came one after the other as if one person
spoke them.

A line is told in parts (`parts_of`, the owner: "when a sentence ends, still even if it changes
talker, it does not change"): split where the diarizer hears the speaker change (2026-11-09:
NVIDIA's streaming Sortformer, services.captions.sortformer) and after each sentence's end, or
with no diarizer at its longest pauses; each part's sound found by the voice detector's windows
(`Recent` keeps the run's last seconds of them, so a line of any writer finds its own). The
diarizer tells each part's speaker, numbered in the order they came, which its speaker cache
keeps through the run.

How a change shows (the Subtitles panel's Speaker marks, a run from source): DASHES, as Turkish
subtitles write dialogue, the new speaker's line starting with "- " on a line of its own, and
both speakers' lines marked when they show together (`dashed`, `joined`); or COLOURS, each
speaker's lines in a colour of their own, as teletext subtitles do (white, yellow, cyan and
green, in the order the speakers come: `Colours`, the colour marked with the line, `coloured`),
a speaker's colour never changing once given. Nothing changes while one speaker goes on.

No Qt, no numpy (the captions process has neither).
"""

from __future__ import annotations

from array import array
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import math
import threading
from typing import Optional

RATE = 16_000
WINDOW = 512  # the voice detector's window (services.captions.pieces.WINDOW): 32 ms
KEEP_S = 40.0  # the sound kept for the lines still to come (a line is out within seconds)
# A line is told in parts: split after each sentence's end and at each pause of LONG_PAUSE_S
# inside it (a sentence's end goes to the pause within SNAP_S of where its letters put it); a
# part with less than MIN_PART_S of voiced sound goes with a neighbour
SENTENCE_ENDS = (".", "?", "!", "…")
_CLOSERS = "'\"”’»)"
LONG_PAUSE_S = 0.4
MOST_PAUSES = 3  # the longest pauses of a line it is split at, at most
SNAP_S = 0.6
# 2026-11-09, the owner's live test ("not from the sentence beginning"): a line's word times are
# estimates (by its letters, or a pause near them), so a change of speaker the diarizer hears
# goes to the nearest sentence end or end of the line within this; a sentence is split only with
# neither near (and a change nearest an end of the line is that line's own)
CHANGE_SNAP_S = 1.5
MIN_PART_S = 0.7
OFF, DASHES, COLOURS = "off", "dashes", "colours"  # how a change of speaker shows
MARKS = (OFF, DASHES, COLOURS)
DASH = "- "
COLOUR_COUNT = 4  # white, yellow, cyan, green (ui.overlays.captions paints them)
# a line's colour, marked before its words (Unicode's private use: never in a model's text)
_COLOUR_MARKS = tuple(chr(0xE000 + k) for k in range(COLOUR_COUNT))


@dataclass
class Turn:
    """Who speaks a part of a line: speaker 1, 2, ... in the run (S1, S2 in the log)."""

    speaker: int
    changed: bool  # another speaker than the part before's
    heard: bool = True  # the diarizer heard this speaker in it (False: the one before goes on)
    before: Optional[int] = None  # the part before's speaker

    def tag(self) -> str:
        """The log's words for it: "S2 after S1" (a change), "S1", "S1 untold"."""
        if not self.heard:
            return f"S{self.speaker} untold"
        if self.changed:
            return f"S{self.speaker} after S{self.before}"
        return f"S{self.speaker}"


@dataclass(frozen=True)
class Part:
    """A part of a line that may be another speaker's (`parts_of`): its words and its sound."""

    text: str
    start: float
    end: float
    # the diarizer (services.captions.sortformer) heard another speaker begin with it (True), or
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
    change_snap: float = CHANGE_SNAP_S,
) -> list[Part]:
    """`text`, a line of timeline seconds [start, end), in parts that may each be another
    speaker's: split after each sentence's end and where the diarizer heard the speaker
    change (`changes`, timeline seconds; services.captions.sortformer), else (none) at the `most`
    longest pauses of `pause` or more. The words are spread over the voiced windows by their
    letters (`voiced`: the voice detector's verdict per WINDOW from timeline second `at`, `start`
    by default), a sentence's end going to the longest pause within SNAP_S of where they put it;
    without them, over [start, end). A change goes to the nearest sentence end or end of the line
    within `change_snap` (one nearest an end of the line splits nothing), else between the
    words nearest it. A part with less than `min_voiced` of voiced sound (of time,
    without them) goes with a neighbour: the next when it begins another speaker's turn the
    next goes on, else the one before (the first, the next). Each part says whether the
    diarizer heard another speaker begin with it (`Part.new`)."""
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
    turned: set[int] = set()  # the cuts where the diarizer heard another speaker begin

    def nearest(t: float) -> int:
        """The word boundary at timeline second `t`, by the voiced sound before it."""
        if on:
            share = sum(1 for i in on if origin + (i + 0.5) * step < t) / len(on)
        else:
            share = (t - start) / (end - start)
        return min(range(len(words) - 1), key=lambda j: abs(after[j] / count - share))

    if changes is not None:
        ends = dict(cuts)  # a speaker changes at a sentence's end more often than not
        for t in changes:
            if not start < t < end:
                continue
            edge = min(t - start, end - t)
            close = [j for j in ends if abs(ends[j] - t) <= change_snap]
            end_near = min(close, key=lambda j: abs(ends[j] - t)) if close else None
            if edge <= change_snap and (end_near is None or edge < abs(ends[end_near] - t)):
                continue  # the line's own start or end
            at_word = nearest(t) if end_near is None else end_near
            cuts[at_word] = t
            turned.add(at_word)
    else:
        long = [p for p in pauses if (p[1] - p[0]) * step >= pause - 1e-6]
        for quiet_from, quiet_to, _before in sorted(long, key=lambda p: p[0] - p[1])[:most]:
            t = origin + (quiet_from + quiet_to) / 2 * step
            cuts.setdefault(nearest(t), t)
    told = changes is not None  # (the diarizer told: a cut with no change is the same voice)
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
    """The voice detector's verdicts on the run's last KEEP_S seconds of placed sound, a window
    at a time (`flags`: a line's words are spread over its voiced windows). `push` from the
    thread that places the sound, `flags` from the ones that write the lines."""

    def __init__(self, is_voiced: Optional[Callable[[array], bool]], keep_s: float = KEEP_S):
        self._is_voiced = is_voiced
        self._keep_s = keep_s
        self._windows: deque[tuple[float, bool]] = deque()  # (timeline second, voiced)
        self._rest = array("f")  # the start of the next window
        self._rest_at: Optional[float] = None
        self._lock = threading.Lock()

    def push(self, at: float, samples: Sequence[float]) -> None:
        """Placed sound from timeline second `at`; a jump starts the windows afresh there."""
        if self._rest_at is None or abs(at - (self._rest_at + len(self._rest) / RATE)) > 0.02:
            self._rest, self._rest_at = array("f"), at
        self._rest.extend(samples)
        done: list[tuple[float, bool]] = []
        while len(self._rest) >= WINDOW:
            window = self._rest[:WINDOW]
            del self._rest[:WINDOW]
            voiced = True
            if self._is_voiced is not None:
                try:
                    voiced = bool(self._is_voiced(window))
                except Exception:
                    voiced = False
            done.append((self._rest_at, voiced))
            self._rest_at += WINDOW / RATE
        if not done:
            return
        with self._lock:
            self._windows.extend(done)
            newest = done[-1][0]
            while self._windows and self._windows[0][0] < newest - self._keep_s:
                self._windows.popleft()

    def flags(self, start: float, end: float) -> tuple[list[bool], float]:
        """The voice detector's verdicts of the windows of timeline seconds [start, end) (each
        window whose middle is in), and the first one's second (`start` when none is kept)."""
        half = WINDOW / RATE / 2
        with self._lock:
            kept = [(at, voiced) for at, voiced in self._windows if start <= at + half < end]
        return [voiced for _at, voiced in kept], (kept[0][0] if kept else start)


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
