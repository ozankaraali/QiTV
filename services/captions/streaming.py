"""A streaming speech model's text as caption lines (a dev trial: our own Turkish streaming
FastConformer, sherpa-onnx's OnlineRecognizer). The sound goes in continuously, with no pieces
and so no forced cuts; the model's growing text becomes whole lines, never word by word: a line
ends at a sentence end (. ? ! …) once the words before it are settled, at a pause (the
recognizer's endpoint: a full stop is added when the model wrote none, as ufak's lines end), or
when it would get too long to read (cut at its last comma, else at a word, with no mark added).
Each line is placed on the timeline from the model's own token times.

2026-11-08, the owner's live test: under dialogue with music beneath it nobody pauses, so a line
waited for its sentence's end to settle, or for a pause, and came after the picture had reached
its words, or never showed. Now, as services.captions.growing does, a line is also written at the
last moment: once the picture is about to reach its first word (MARGIN_S), with the words heard so
far but those of the last HOLDBACK_S the model may still change, cut by the same rules, no mark
added; a scrap of a word or two waits a moment for more (SHORTEST_S, LATE_S).

`Lines` is the line logic alone (no model, testable); `Anchors` maps seconds of sound fed to the
recognizer onto the timeline (the sound comes in placed blocks, with gaps at times);
`StreamWriter` puts them together around a recognizer stream.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

SENTENCE_ENDS = (".", "?", "!", "…")
SETTLED_S = 1.2  # words older than this behind the decoded sound no longer change (beam search)
MAX_LINE_CHARS = 84  # two caption rows
MAX_LINE_S = 6.0
TAIL_S = 0.3  # a line shows a little past its last word's start
# The picture's pace (services.captions.growing reads these too)
MARGIN_S = 0.8  # a line is out this long before the picture reaches its first word
HOLDBACK_S = 0.5  # a line on the last moment leaves the words from this near what is heard
# ... and is no scrap of a word or two: shorter than SHORTEST_S and with no sentence's end, it
# waits for more words until the picture is LATE_S past its first word (a picture near the live
# edge: lines of a word each, 244 for 75, on a drama stretch 4 s behind live)
SHORTEST_S = 1.2
LATE_S = 1.0
HELD_AFTER_S = 0.75  # the picture has not moved for this long: it waits
HOLD_FLOOR_S = 7.5  # a waiting picture plays on once this far behind the sound read
FIRST_WORD_S = 0.3  # the captions show a line this long after its start (ui.captions_controller)
NEVER = float("-inf")


def time_left(t: float, position: float, still: float, newest: float) -> float:
    """Seconds until the picture, at timeline second `position` and still for `still` seconds,
    gets to second `t`: it plays at 1x; one that has not moved for HELD_AFTER_S waits (a run
    starts with it waiting for the captions' distance, or it was paused) and plays on once the
    sound read (to `newest`) is HOLD_FLOOR_S ahead of it."""
    if still >= HELD_AFTER_S:
        return max(0.0, HOLD_FLOOR_S - (newest - position)) + (t - position)
    return t - position - still


@dataclass
class Word:
    text: str
    first: int  # its first token's index in the segment
    last: int  # its last token's
    start: float  # seconds of sound fed, its first token
    end: float  # its last token


def words_of(tokens: list[str], times: list[float], offset: float, after: float) -> list[Word]:
    """The segment's words that start after second `after` (seconds of sound fed): a token that
    begins with a space begins a word; marks (",", ".") join the word before them."""
    out: list[Word] = []
    for i, token in enumerate(tokens):
        at = offset + times[i]
        if token.startswith(" ") or not out:
            out.append(Word(token.strip(), i, i, at, at))
        else:
            word = out[-1]
            word.text += token
            word.last, word.end = i, at
    return [w for w in out if w.text and w.start > after]


class Lines:
    """Lines out of the recognizer's segments (`take`). What is written is remembered by time,
    not by token count: beam search may split a word it already gave another way ("futbol",
    "fut" + "bol"), and a count would then write a word twice (the owner, 2026-11-05: "futbol
    futbol severler"); the sound's time of a word does not change."""

    def __init__(self) -> None:
        self.done_s = -1.0  # seconds of sound fed up to which lines are written (the last word's)

    def reset(self) -> None:
        """A new segment: its words come later in the sound, so the time mark stays."""

    def take(
        self,
        tokens: list[str],
        times: list[float],
        offset: float,
        frontier: float,
        endpoint: bool,
        due: float = NEVER,
    ) -> list[tuple[str, float, float]]:
        """New lines (text, start, end in seconds of sound fed): `offset` is the segment's start,
        `frontier` how far the sound is decoded, `endpoint` a pause ended the segment (all of it
        is written then), `due` the second whose words must be out now (the picture is about to
        get there): a line that begins by then is written with the words heard so far but the
        last one and those of the last HOLDBACK_S (the model may still change them), unless it
        would be a scrap and the picture is not yet LATE_S past it."""
        lines: list[tuple[str, float, float]] = []
        while True:
            words = words_of(tokens, times, offset, self.done_s)
            if not words:
                return lines
            settled = [w for w in words if endpoint or w.end <= frontier - SETTLED_S]
            cut = self._cut(settled, endpoint and len(settled) == len(words))
            if cut is None and words[0].start <= due:
                heard = [w for w in words[:-1] if w.end <= frontier - HOLDBACK_S]
                cut = self._cut(heard if len(heard) > len(settled) else settled, True)
                scrap = cut is not None and not self._whole(words[: cut + 1])
                if scrap and words[0].start > due - MARGIN_S - LATE_S:
                    cut = None  # more words first, a moment later
            if cut is None:
                return lines
            line = words[: cut + 1]
            text = " ".join(w.text for w in line)
            if endpoint and cut == len(words) - 1 and not text.endswith(SENTENCE_ENDS + (",",)):
                text += "."  # a pause ended what was said: its line ends a sentence, as ufak's do
            lines.append((text, line[0].start, line[-1].end + TAIL_S))
            self.done_s = line[-1].end

    @staticmethod
    def _whole(line: list[Word]) -> bool:
        """A line long enough to read on its own, or a sentence."""
        return line[-1].end - line[0].start >= SHORTEST_S or line[-1].text.endswith(SENTENCE_ENDS)

    @staticmethod
    def _cut(settled: list[Word], all_of_it: bool) -> Optional[int]:
        """The index of the settled word a line ends with, else None (wait for more)."""
        if not settled:
            return None
        length, cut = 0, None
        for i, word in enumerate(settled):
            length += len(word.text) + (1 if i else 0)
            if word.text.endswith(SENTENCE_ENDS):
                return i
            too_long = length > MAX_LINE_CHARS or word.end - settled[0].start > MAX_LINE_S
            if too_long and i > 0:
                commas = [j for j in range(i) if settled[j].text.endswith(",")]
                return commas[-1] if commas else i - 1
        if all_of_it:
            cut = len(settled) - 1
        return cut


class Anchors:
    """Seconds of sound fed to the recognizer -> timeline seconds: each placed block is an anchor
    (fed second where it begins, its timeline second); a time maps through the block it fell in."""

    def __init__(self) -> None:
        self._fed: list[float] = []
        self._at: list[float] = []
        self.fed_s = 0.0

    def add(self, at: float, seconds: float) -> None:
        self._fed.append(self.fed_s)
        self._at.append(at)
        self.fed_s += seconds

    def timeline(self, fed: float) -> float:
        i = max(0, bisect_right(self._fed, fed) - 1)
        return self._at[i] + (fed - self._fed[i]) if self._fed else fed

    def fed_at(self, at: float) -> float:
        """Timeline second `at` -> seconds of sound fed (in a gap between blocks: where the next
        block begins)."""
        if not self._fed:
            return at
        i = max(0, bisect_right(self._at, at) - 1)
        fed = self._fed[i] + (at - self._at[i])
        return min(fed, self._fed[i + 1]) if i + 1 < len(self._fed) else fed

    def forget_before(self, fed: float) -> None:
        """Anchors no line can reach any more (before the current segment) go."""
        i = max(0, bisect_right(self._fed, fed) - 1)
        if i > 0:
            del self._fed[:i], self._at[:i]


class StreamWriter:
    """One run's recognizer stream: `push` placed sound in, `work` placed lines out (`feed`:
    both). With the picture's place (`viewer`), a line is out before the picture gets to it."""

    def __init__(
        self,
        recognizer: Any,
        viewer: Optional[list] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.recognizer = recognizer
        self.stream = recognizer.create_stream()
        self.anchors = Anchors()
        self.lines = Lines()
        self.viewer = viewer  # [position], the picture's timeline second
        self.clock = clock
        self._seen = (float(viewer[0]), clock() - HELD_AFTER_S) if viewer is not None else None
        self._heard: tuple[list[str], list[float], float] = ([], [], 0.0)  # the segment so far
        self._found: list[tuple[str, float, float]] = []

    def push(self, at: float, samples: Any) -> None:
        """16 kHz mono floats from timeline second `at`."""
        self.anchors.add(at, len(samples) / 16000)
        self.stream.accept_waveform(16000, samples)
        self._found += self._decode()

    def work(self) -> list[tuple[str, float, float]]:
        """The lines completed since (timeline), and those the picture is about to get to."""
        tokens, times, offset = self._heard
        found, self._found = self._found, []
        due = self.lines.take(tokens, times, offset, self._decoded(), False, self._due())
        return found + [self._placed(line) for line in due]

    def feed(self, at: float, samples: Any) -> list[tuple[str, float, float]]:
        self.push(at, samples)
        return self.work()

    def frontier(self) -> float:
        """The timeline second the decoding has reached (about one chunk behind what was fed)."""
        return self.anchors.timeline(self._decoded())

    def _decoded(self) -> float:
        return max(0.0, self.anchors.fed_s - 1.2)

    def _due(self) -> float:
        """The second of sound fed whose words must be out now: the picture gets there within
        MARGIN_S (NEVER without the picture's place)."""
        if self.viewer is None or self._seen is None or not self.anchors.fed_s:
            return NEVER
        now, position = self.clock(), float(self.viewer[0])
        if position != self._seen[0]:
            self._seen = (position, now)
        still = now - self._seen[1]
        newest = self.anchors.timeline(self.anchors.fed_s)
        # time_left(t) == MARGIN_S, for t: it falls by one second a second of t
        at = position + MARGIN_S - time_left(position, position, still, newest)
        return self.anchors.fed_at(at)

    def _placed(self, line: tuple[str, float, float]) -> tuple[str, float, float]:
        text, a, b = line
        return text, self.anchors.timeline(a), self.anchors.timeline(b)

    def _decode(self) -> list[tuple[str, float, float]]:
        recognizer, stream = self.recognizer, self.stream
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        result = recognizer.get_result_all(stream)
        endpoint = bool(recognizer.is_endpoint(stream))
        offset = float(result.start_time)
        tokens, times = list(result.tokens), [float(t) for t in result.timestamps]
        found = self.lines.take(tokens, times, offset, self._decoded(), endpoint, self._due())
        placed = [self._placed(line) for line in found]  # before the anchors go
        if endpoint:
            recognizer.reset(stream)
            self.lines.reset()
            self.anchors.forget_before(offset)
            tokens, times = [], []
        self._heard = (tokens, times, offset)
        return placed
