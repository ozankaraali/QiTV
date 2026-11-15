"""A caption line in parts (2026-11-08, the owner: "the sentences got too long, more than two
lines, after the dot we can split and show the other part later"): split after each sentence's
end and at its longest pauses (`parts_of`), each part's sound found by the voice detector's
windows. The captions show a long line a part at a time, each as it is said
(ui.captions_controller's `_page_at`).

No Qt, no numpy (the captions process has neither).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math
from typing import Optional

RATE = 16_000
WINDOW = 512  # the voice detector's window (services.captions.pieces.WINDOW): 32 ms
# A line is told in parts: split after each sentence's end and at each pause of LONG_PAUSE_S
# inside it (a sentence's end goes to the pause within SNAP_S of where its letters put it); a
# part with less than MIN_PART_S of voiced sound goes with a neighbour
SENTENCE_ENDS = (".", "?", "!", "…")
_CLOSERS = "'\"”’»)"
LONG_PAUSE_S = 0.4
MOST_PAUSES = 3  # the longest pauses of a line it is split at, at most
SNAP_S = 0.6
MIN_PART_S = 0.7


@dataclass(frozen=True)
class Part:
    """A part of a line (`parts_of`): its words and its sound."""

    text: str
    start: float
    end: float


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
) -> list[Part]:
    """`text`, a line of timeline seconds [start, end), in parts: split after each sentence's end
    and at the `most` longest pauses of `pause` or more. The words are spread over the voiced
    windows by their letters (`voiced`: the voice detector's verdict per WINDOW from timeline
    second `at`, `start` by default), a sentence's end going to the longest pause within SNAP_S
    of where they put it; without them, over [start, end). A part with less than `min_voiced` of
    voiced sound (of time, without them) goes with a neighbour: the one before (the first, the
    next)."""
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

    def nearest(t: float) -> int:
        """The word boundary at timeline second `t`, by the voiced sound before it."""
        if on:
            share = sum(1 for i in on if origin + (i + 0.5) * step < t) / len(on)
        else:
            share = (t - start) / (end - start)
        return min(range(len(words) - 1), key=lambda j: abs(after[j] / count - share))

    long = [p for p in pauses if (p[1] - p[0]) * step >= pause - 1e-6]
    for quiet_from, quiet_to, _before in sorted(long, key=lambda p: p[0] - p[1])[:most]:
        t = origin + (quiet_from + quiet_to) / 2 * step
        cuts.setdefault(nearest(t), t)
    parts: list[Part] = []
    begun, since = 0, start
    for i in sorted(cuts):
        if since < cuts[i] < end:
            parts.append(Part(" ".join(words[begun : i + 1]), since, cuts[i]))
            begun, since = i + 1, cuts[i]
    parts.append(Part(" ".join(words[begun:]), since, end))

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
        a = short if short == 0 else short - 1
        parts[a : a + 2] = [
            Part(f"{parts[a].text} {parts[a + 1].text}", parts[a].start, parts[a + 1].end)
        ]
    return parts
