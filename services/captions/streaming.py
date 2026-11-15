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
# line this much sooner (services.captions.worker, the growing writer's `reserve`)
TRANSLATE_LEAD_S = 5.0


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
