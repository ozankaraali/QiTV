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
import time
from typing import Callable, Optional

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
TRANSLATE_LEAD_QUIET_S = 300.0  # grown, a step lower only after this long with none late


def measured_lead(seconds: float) -> float:
    """2.13.1: the lead for a translator whose medium line takes `seconds` (the faster of two
    warm-up translations): four times that and half a second, rounded up to a half second, 2 to
    5 seconds. 2026-11-21's queue simulation of the deadline rule on measured line times: 2.5 s
    on a 7950X, 4.0 s on a 4-core ARM box and 4.5 s on the reference i7 keep 95-97 % of the
    lines in time beside the speech model."""
    raw = math.ceil((4.0 * max(0.0, seconds) + 0.5) * 2.0 - 1e-9) / 2.0
    return min(TRANSLATE_LEAD_S, max(TRANSLATE_LEAD_MIN_S, raw))


def translate_times(value: object) -> dict[str, float]:
    """2.13.1: the translator's timed medium line as the config keeps it (optional key
    ai_captions_translate_t, older builds ignore it): {its threads, as text: seconds}; anything
    else gives nothing (a count of 1 to 64, more than 0 and under a minute)."""
    if not isinstance(value, dict):
        return {}
    kept: dict[str, float] = {}
    for threads, seconds in value.items():
        if not isinstance(threads, str) or not threads.isdigit() or not 1 <= int(threads) <= 64:
            continue
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            continue
        if 0.0 < float(seconds) < 60.0:
            kept[threads] = round(float(seconds), 3)
    return kept


class TranslationLead:
    """2.13.1: how long before a line shows its translation has (the GUI keeps it; the captions
    process is told it, services.captions.worker). TRANSLATE_LEAD_S until the translator's
    warm-up is timed (`measure`; a time an earlier run saved starts it, `seed`);
    TRANSLATE_LEAD_STEP_S more each time a translation comes late (`late`), to
    TRANSLATE_LEAD_MAX_S. Never lowered mid-playback (the picture would be steered toward live,
    which the viewer sees): only at a natural point (`natural`: a zap or a tune, a seek, play
    after a pause, translation turned on), and then to the measured lead; once grown, never
    below what it grew to while QiTV runs (no wait for a second and a late line after each
    zap), but for a step down after TRANSLATE_LEAD_QUIET_S with no late line, and again after
    as long again. `clock` is time.monotonic (tests give their own)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.value = TRANSLATE_LEAD_S
        self.measured: Optional[float] = None
        self.fastest: Optional[float] = None  # the quickest warm-up this run timed
        self._clock = clock
        self._earned: Optional[float] = None  # what late translations grew it to
        self._quiet_since = 0.0  # the last late translation (or step down since)

    def seed(self, seconds: float) -> None:
        """A medium line's time an earlier run measured (its config): the measured lead until
        this run times its own."""
        if self.fastest is None:
            self.measured = measured_lead(seconds)

    def measure(self, seconds: float) -> bool:
        """The translator's medium line took `seconds` (its warm-up): the measured lead, by the
        quickest such time this run timed (a slower one had the speech model beside it; a saved
        one is replaced); taken at once only when it is higher. True when the lead changed."""
        if self.fastest is None or seconds < self.fastest:
            self.fastest = seconds
        self.measured = measured_lead(self.fastest)
        if self.measured > self.value:
            self.value = self.measured
            return True
        return False

    def late(self) -> bool:
        """A translation came late for its line: a step longer, to the cap, and what it grew to
        kept. True when it grew."""
        self._quiet_since = self._clock()
        if self.value >= TRANSLATE_LEAD_MAX_S:
            return False
        self.value = min(TRANSLATE_LEAD_MAX_S, self.value + TRANSLATE_LEAD_STEP_S)
        self._earned = self.value
        return True

    def natural(self) -> bool:
        """A natural point: the measured lead (TRANSLATE_LEAD_S unmeasured) when it is lower,
        never below what it grew to; that a step lower after TRANSLATE_LEAD_QUIET_S with no
        late translation. True when the lead changed."""
        base = self.measured if self.measured is not None else TRANSLATE_LEAD_S
        now = self._clock()
        if self._earned is not None and now - self._quiet_since >= TRANSLATE_LEAD_QUIET_S:
            self._earned -= TRANSLATE_LEAD_STEP_S
            self._quiet_since = now  # the next step down after as long again
            if self._earned <= base:
                self._earned = None
        target = base if self._earned is None else max(base, self._earned)
        if target >= self.value - 1e-9:
            return False
        self.value = target
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
