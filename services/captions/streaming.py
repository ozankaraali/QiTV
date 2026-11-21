"""Where a caption line ends, and the picture's pace (services.captions.growing writes its lines
by these). A line ends at a sentence end (. ? ! …), or where it would get too long to read (two
rows, six seconds: cut at its last comma, else at a word, with no mark added; `line_end`).

2026-11-08, the owner's live test: under dialogue with music beneath it nobody pauses, so a line
waited for its sentence's end, and came after the picture had reached its words, or never
showed. So a line is also written at the last moment: once the picture is about to reach its
first word (MARGIN_S), with the words heard so far but those of the last HOLDBACK_S the model
may still change (`time_left` says when the picture gets there).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional

SENTENCE_ENDS = (".", "?", "!", "…")
MAX_LINE_CHARS = 84  # two caption rows
MAX_LINE_S = 6.0
# The picture's pace (services.captions.growing reads these)
MARGIN_S = 0.8  # a line is out this long before the picture reaches its first word
HOLDBACK_S = 0.5  # a line on the last moment leaves the words from this near what is heard
HELD_AFTER_S = 0.75  # the picture has not moved for this long: it waits
HOLD_FLOOR_S = 7.5  # a waiting picture plays on once this far behind the sound read
FIRST_WORD_S = 0.3  # the captions show a line this long after its start (ui.captions_controller)
# 2.10: while translating, a line's translation takes this long before the line shows: the
# picture is held this much further back (ui.captions_controller) and the speech side writes each
# line this much sooner (services.captions.worker, the growing writer's `reserve`). 2.13.1: the
# lead until the translator's speed is known, and the most a measured one is (`TranslationLead`)
TRANSLATE_LEAD_S = 5.0
TRANSLATE_LEAD_MIN_S = 2.0
TRANSLATE_LEAD_STEP_S = 1.0  # a translation came late: the lead grows by this ...
TRANSLATE_LEAD_MAX_S = 7.0  # ... to this at most


def measured_lead(seconds: float) -> float:
    """2.13.1: the lead for a translator whose medium line takes `seconds` (the faster of two
    warm-up translations): four times that and half a second, rounded up to a half second, 2 to
    5 seconds. 2026-11-21's queue simulation of the deadline rule on measured line times: 2.5 s
    on a 7950X, 4.0 s on a 4-core ARM box and 4.5 s on the reference i7 keep 95-97 % of the
    lines in time beside the speech model."""
    raw = math.ceil((4.0 * max(0.0, seconds) + 0.5) * 2.0 - 1e-9) / 2.0
    return min(TRANSLATE_LEAD_S, max(TRANSLATE_LEAD_MIN_S, raw))


class TranslationLead:
    """2.13.1: how long before a line shows its translation has (the GUI keeps it; the captions
    process is told it, services.captions.worker). TRANSLATE_LEAD_S until the translator's
    warm-up is timed (`measure`); TRANSLATE_LEAD_STEP_S more each time a translation comes late
    (`late`), to TRANSLATE_LEAD_MAX_S. Never lowered mid-playback (the picture would be steered
    toward live, which the viewer sees): only at a natural point (`natural`: a zap or a tune, a
    seek, play after a pause, translation turned on), and then to the measured lead."""

    def __init__(self) -> None:
        self.value = TRANSLATE_LEAD_S
        self.measured: Optional[float] = None
        self._fastest: Optional[float] = None  # the quickest warm-up timed while QiTV runs

    def measure(self, seconds: float) -> bool:
        """The translator's medium line took `seconds` (its warm-up): the measured lead, by the
        quickest such time while QiTV runs (a slower one had the speech model beside it); taken
        at once only when it is higher. True when the lead changed."""
        if self._fastest is None or seconds < self._fastest:
            self._fastest = seconds
        self.measured = measured_lead(self._fastest)
        if self.measured > self.value:
            self.value = self.measured
            return True
        return False

    def late(self) -> bool:
        """A translation came late for its line: a step longer, to the cap. True when it grew."""
        if self.value >= TRANSLATE_LEAD_MAX_S:
            return False
        self.value = min(TRANSLATE_LEAD_MAX_S, self.value + TRANSLATE_LEAD_STEP_S)
        return True

    def natural(self) -> bool:
        """A natural point: the measured lead, when it is lower. True when the lead changed."""
        if self.measured is None or self.measured >= self.value:
            return False
        self.value = self.measured
        return True


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
    start: float  # seconds of sound, its first token
    end: float  # its last token


def line_end(settled: list[Word], all_of_it: bool) -> Optional[int]:
    """The index of the settled word a line ends with, else None (wait for more); with
    `all_of_it`, the last one when no rule ends the line sooner."""
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
