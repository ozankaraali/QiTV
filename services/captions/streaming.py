"""A streaming speech model's text as caption lines (a dev trial: our own Turkish streaming
FastConformer, sherpa-onnx's OnlineRecognizer). The sound goes in continuously, with no pieces
and so no forced cuts; the model's growing text becomes whole lines, never word by word: a line
ends at a sentence end (. ? ! …) once the words before it are settled, at a pause (the
recognizer's endpoint: a full stop is added when the model wrote none, as ufak's lines end), or
when it would get too long to read (cut at its last comma, else at a word, with no mark added).
Each line is placed on the timeline from the model's own token times.

`Lines` is the line logic alone (no model, testable); `Anchors` maps seconds of sound fed to the
recognizer onto the timeline (the sound comes in placed blocks, with gaps at times);
`StreamWriter` puts them together around a recognizer stream.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

SENTENCE_ENDS = (".", "?", "!", "…")
SETTLED_S = 1.2  # words older than this behind the decoded sound no longer change (beam search)
MAX_LINE_CHARS = 84  # two caption rows
MAX_LINE_S = 6.0
TAIL_S = 0.3  # a line shows a little past its last word's start


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
        self, tokens: list[str], times: list[float], offset: float, frontier: float, endpoint: bool
    ) -> list[tuple[str, float, float]]:
        """New lines (text, start, end in seconds of sound fed): `offset` is the segment's start,
        `frontier` how far the sound is decoded, `endpoint` a pause ended the segment (all of it
        is written then)."""
        lines: list[tuple[str, float, float]] = []
        while True:
            words = words_of(tokens, times, offset, self.done_s)
            if not words:
                return lines
            settled = [w for w in words if endpoint or w.end <= frontier - SETTLED_S]
            cut = self._cut(settled, endpoint and len(settled) == len(words))
            if cut is None:
                return lines
            line = words[: cut + 1]
            text = " ".join(w.text for w in line)
            if endpoint and cut == len(words) - 1 and not text.endswith(SENTENCE_ENDS + (",",)):
                text += "."  # a pause ended what was said: its line ends a sentence, as ufak's do
            lines.append((text, line[0].start, line[-1].end + TAIL_S))
            self.done_s = line[-1].end

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

    def forget_before(self, fed: float) -> None:
        """Anchors no line can reach any more (before the current segment) go."""
        i = max(0, bisect_right(self._fed, fed) - 1)
        if i > 0:
            del self._fed[:i], self._at[:i]


class StreamWriter:
    """One run's recognizer stream: `feed` placed sound in, `lines` placed lines out."""

    def __init__(self, recognizer: Any) -> None:
        self.recognizer = recognizer
        self.stream = recognizer.create_stream()
        self.anchors = Anchors()
        self.lines = Lines()

    def feed(self, at: float, samples: Any) -> list[tuple[str, float, float]]:
        """16 kHz mono floats from timeline second `at`; the lines it completes (timeline)."""
        self.anchors.add(at, len(samples) / 16000)
        self.stream.accept_waveform(16000, samples)
        return self._decode()

    def frontier(self) -> float:
        """The timeline second the decoding has reached (about one chunk behind what was fed)."""
        return self.anchors.timeline(max(0.0, self.anchors.fed_s - 1.2))

    def _decode(self) -> list[tuple[str, float, float]]:
        recognizer, stream = self.recognizer, self.stream
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        result = recognizer.get_result_all(stream)
        endpoint = bool(recognizer.is_endpoint(stream))
        offset = float(result.start_time)
        found = self.lines.take(
            list(result.tokens),
            [float(t) for t in result.timestamps],
            offset,
            max(0.0, self.anchors.fed_s - 1.2),
            endpoint,
        )
        if endpoint:
            recognizer.reset(stream)
            self.lines.reset()
            self.anchors.forget_before(offset)
        return [(text, self.anchors.timeline(a), self.anchors.timeline(b)) for text, a, b in found]
