"""An offline speech model on live sound with no forced cuts (a dev trial: our Turkish Moonshine,
sherpa-onnx's Moonshine v2 files, which read whole pieces only and give no word times).

Pieces cut to the captions' lead lost words at the cuts (the owner, 2026-11-07: right after a
tune, "Burada her | yeni bir mücadele var" lost "gün"; "geldik | bir hafta sonu" lost "iyi"):
read with the whole sentence, both words come out right. So here a piece is an utterance, from
the voice detector's speech to a pause (pieces.Cutter, long enough for any sentence), read whole
once it ends. Only when the picture is about to reach its words and it has not ended does
anything else happen: it ends at its longest pause between words of SPLIT_PAUSE_S or more (read
whole up to there); with none (music under the words holds the detectors), it is read again as
it grows and the line two readings in a row agree on (LocalAgreement) is written at the last
moment, never a reading's last word nor the words of its last half second: the word cut in two
at the end of what was heard so far is never written. Once words are written, a reading begins
CONTEXT_S before their end, at a pause between words, so what is read stays short however long
the utterance runs; its words continue the written ones (`continues`: the model may write those
a little differently, only what follows is new). They are matched against the written words
from well before where the reading begins (HELD_S), the words' guessed times being no more than
guesses, and the same word said again is told apart by where they put it.

2026-11-07, the owner's first live test: a line came again without its first word (a reading
held more written words than their guessed times said, and those were taken for new), and
"odada bunu konuşuyorlar" came as "odada bu." and "konuşuyorlar." (a cut at an 80 ms hush inside
"bunu", the pause it was cut at being any of 80 ms or more). 2026-11-08, a drama: a phrase came
as its first word and its third ("Kara", then "yolunu kesti." for "kara kedi yolunu kesti"): the
last written word, read again run together with the next one ("Karakedi"), was taken for itself.
Now what follows it in such a word is new when an earlier reading heard it apart
(`joined_rest`), and a written word read again in two (or two run together) is no new word.

Measured (2026-11-07, held-out drama and read speech joined into live-like streams, the
captions' 8 s distance, a tune at each start): 24.8 % of words wrong against 26.5 % for pieces
of up to 6 s and 35 % for the 2 s pieces right after a tune; under a music bed 25.6 % against
28.6 % and 39.6 %; about 1.7 times the reading of 6 s pieces.

Without word times, the words are spread over the voiced windows of the sound they came from,
by their letters (`place`). Lines follow services.captions.streaming's rules (a sentence's end,
two rows at most, six seconds at most).

- An utterance leads in from the first voiced window up to LOOKBACK_S before the detector's
  first speech window (the detector says speech late, and the word that started the sound was
  lost; the owner, 2026-11-07: "İhtiyar, hayırlı akşamlar" shown as "Hayırlı akşamlar"), not
  from further back: sound before the voice changed the first words (0.8 s more before each
  utterance: 3 words in 100 more wrong on a drama stream).
- One that runs very long ends at a pause between words after its written words (`LONG_S`);
  with none, at its quietest window (`LONGEST_HELD_S`), the next part reading its last
  OVERLAP_S again (the word cut there is left to it, the words it reads again are not new).
- The picture's pace (`time_left`): a picture that has not moved for a moment waits for the
  captions' distance (the live picture's "Starting captions…") and plays on at 1x from there.
"""

from __future__ import annotations

from array import array
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import logging
import time
from typing import Optional
import unicodedata

from services.captions.pieces import RATE, WINDOW, Cutter, Piece
from services.captions.streaming import (  # the picture's pace, as the streaming lines'
    FIRST_WORD_S,
    HELD_AFTER_S,
    HOLDBACK_S,
    MARGIN_S,
    SENTENCE_ENDS,
    Lines,
    Word,
    time_left,
)

logger = logging.getLogger(__name__)

LOOKBACK_S = 0.6  # an utterance leads in from the first voiced window this long before speech
LONGEST_S = 30.0  # the cutter's own length: never reached, LONGEST_HELD_S ends one first
LONG_S = 14.0  # an utterance this long ends at a pause between words after its written words
LONGEST_HELD_S = 24.0  # ... and at this length with none: at its quietest, read again past it
FIRST_READ_S = 1.0  # an utterance is read again only once it is this long
CONTEXT_S = 2.0  # a reading past written words begins about this long before their end
STEP_S = 0.8  # the least sound between two readings of an utterance going on...
STEP_SHARE = 1.5  # ... and at least this many times the last reading's time
MIN_SPEECH_SHARE = 0.3  # less of an utterance was speech: music the detector caught
PASSED_S = 1.0  # an utterance the picture passed by this much is not read
JOIN_GAP_S = 0.3  # a line this near the one before goes on from it (ui.captions_controller)
_COST_START = 0.15  # seconds a reading takes per second of sound, until measured
SPLIT_PAUSE_S = 0.2  # a pause between words an utterance going on may end at (a word's own
# stops and hushes are shorter: the owner, 2026-11-07, "odada bu|nu konuşuyorlar" at 80 ms)
SPLIT_SLACK_S = 0.3  # it ends this much (and a reading) before its first words would be late
OVERLAP_S = 1.5  # the next part reads this much of a part again: its last word comes whole there
HELD_S = 3.0  # a reading's words are matched against the written words up to this much before
# where it begins as their guessed times tell (the guesses can be a second or two off)
CARRIED_WORDS = 12  # the words of a split part the next part's readings are matched against


def norm(word: str) -> str:
    """A word as two readings are compared: no case, no marks, Turkish's dotted and dotless i."""
    word = unicodedata.normalize("NFKC", word).replace("İ", "i").replace("I", "ı").casefold()
    return "".join(c for c in word if unicodedata.category(c)[0] not in "PS")


def continues(written: Sequence[str], heard: Sequence[str], expect: Optional[int] = None) -> int:
    """Where the words `heard` (a reading of the sound the words `written` came from, and more)
    go on past them: the j for which heard[:j] is nearest `written`. A word changed costs less
    the more alike the two are (a word read a little differently is the same word), two words
    not alike as much as one missed and one added (a reading that holds none of the written
    words never takes its new words for them); a word read again in two ("Karakedi", "Kara
    kedi"), or two read again run together, costs nothing; of two
    places as near, the one nearest `expect` (how many written words the reading should hold,
    by their guessed times: the same word said again is told apart by where it is), else the
    earlier, so that a written word the reading left out costs it no new word."""
    a = [norm(w) for w in written]
    b = [norm(w) for w in heard]
    if not a:
        return 0
    previous = [float(j) for j in range(len(b) + 1)]  # distances of a[:0] to b[:j]
    before = previous  # (of a[:i - 2])
    for i in range(1, len(a) + 1):
        row = [float(i)] + [0.0] * len(b)
        for j in range(1, len(b) + 1):
            x, y = a[i - 1], b[j - 1]
            alike = 1.0 if x == y else SequenceMatcher(None, x, y).ratio()
            # a word read a little differently costs less the more alike; two words that are
            # not alike are no change of one word but two (one gone, one come)
            change = 2.0 * (1.0 - alike) if alike >= 0.6 else 2.0
            row[j] = min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + change)
            if j >= 2 and x == b[j - 2] + y:
                row[j] = min(row[j], previous[j - 2])  # the written word read in two
            if i >= 2 and y == a[i - 2] + x:
                row[j] = min(row[j], before[j - 1])  # two written words read as one
        before, previous = previous, row
    best = min(previous)
    near = [j for j, d in enumerate(previous) if d <= best + 1e-9]
    return min(near, key=lambda j: (0 if expect is None else abs(j - expect), j))


def joined_rest(written: str, heard: str, said_next: Sequence[str]) -> Optional[str]:
    """The word after `written` when a reading wrote the two run together (`heard`, the word
    `continues` matched to `written`, "Karakedi" for "Kara"): what follows `written` in it,
    but only when an earlier reading heard that word on its own right after `written`
    (`said_next`, its words past the written ones: "kedi", or the "ke" its end cut). Else None:
    `heard` is `written` read a little differently ("seviyor", "seviyorum")."""
    a, b = norm(written), norm(heard)
    rest = b[len(a) :]
    nxt = norm(said_next[0]) if said_next else ""
    if not a or not b.startswith(a) or len(rest) < 2 or not nxt:
        return None
    short = min(len(rest), len(nxt))
    if rest != nxt and (len(rest) < 3 or short < 2 or rest[:short] != nxt[:short]):
        return None
    count = 0
    for i, letter in enumerate(heard):  # `heard` past the letters of `written`
        count += len(norm(letter))
        if count >= len(a):
            tail = heard[i + 1 :]
            while tail and not tail[0].isalnum():
                tail = tail[1:]
            return tail or None
    return None


def words_of(text: str) -> list[str]:
    """A reading's words; a mark on its own (" .", " ''") goes with the word before it."""
    out: list[str] = []
    for token in text.split():
        if out and not any(c.isalnum() for c in token):
            out[-1] += token
        elif any(c.isalnum() for c in token):
            out.append(token)
    return out


def ends_sentence(text: str) -> bool:
    """A line that ends with a sentence's mark or a comma (before closing quotes)."""
    return text.rstrip("'\"”’»)").endswith(SENTENCE_ENDS + (",",))


def agreed(first: Sequence[str], second: Sequence[str]) -> int:
    """How many words two readings begin with alike."""
    n = 0
    for x, y in zip(first, second):
        if norm(x) != norm(y):
            break
        n += 1
    return n


def pause_in(voiced: Sequence[bool], lo: int, hi: int) -> int:
    """The window inside the longest run of unvoiced windows in [lo, hi), else `hi`."""
    best, longest, run = hi, 0, 0
    for i in range(max(0, lo), min(hi, len(voiced))):
        run = 0 if voiced[i] else run + 1
        if run and run >= longest:
            best, longest = i - run // 2, run
    return best


def place(words: Sequence[str], start: float, voiced: Sequence[bool]) -> list[Word]:
    """`words` spread over the voiced windows from timeline second `start` (`voiced`: per
    window from there), each by its letters; over all of it when none is voiced."""
    step = WINDOW / RATE
    spans = [i for i, v in enumerate(voiced) if v] or list(range(len(voiced)))
    if not words:
        return []
    if not spans:
        return [Word(w, 0, 0, start, start) for w in words]
    weights = [len(w) + 1 for w in words]
    total = float(sum(weights))
    out, done = [], 0.0
    for text, weight in zip(words, weights):
        first = spans[min(len(spans) - 1, int(done / total * len(spans)))]
        done += weight
        last = spans[max(0, min(len(spans) - 1, int(done / total * len(spans)) - 1))]
        out.append(Word(text, 0, 0, start + first * step, start + (last + 1) * step))
    return out


@dataclass
class _Utterance:
    start: float  # timeline, the piece's (with its lead-in)
    # what of the part before it reads again (a split: OVERLAP_S): those words, and where the
    # words not told yet begin (the word left to it)
    carried: list[Word] = field(default_factory=list)
    told_to: float = float("-inf")
    left: list[str] = field(default_factory=list)  # the word left to it, as that part heard it
    written: list[Word] = field(default_factory=list)  # its words written so far
    heard: list[str] = field(default_factory=list)  # the last reading's words past them
    read_to: float = float("-inf")  # the end of the sound the last reading had
    ready: list[Word] = field(default_factory=list)  # the line to write when its time comes
    sure: bool = False  # two readings agree on `ready` (else one alone, its end held back)
    clear: list[Word] = field(default_factory=list)  # the last reading's words but its end

    def unwritten(self, piece: Piece) -> tuple[float, list[bool]]:
        """Where its words not written yet begin, and the voiced windows from there."""
        at = self.written[-1].end if self.written else max(piece.start, self.told_to)
        skip = max(0, round((at - piece.start) * RATE / WINDOW))
        return piece.start + skip * WINDOW / RATE, piece.voiced[skip:]


class GrowingWriter:
    """One run's utterances: `push` placed sound in (the voice detector only), `work` reads
    what is due and gives back the lines it completes (text, start, end on the timeline)."""

    def __init__(
        self,
        decode: Callable[[array], str],
        is_speech: Callable[[array], bool],
        is_voiced: Callable[[array], bool],
        viewer: list,
        *,
        clock: Callable[[], float] = time.monotonic,
        keep: Optional[Callable[[float, float, array, str], None]] = None,
    ) -> None:
        self.decode = decode  # whole sound -> the model's text, already cleaned (guards)
        self.cutter = Cutter(is_speech, is_voiced, longest=LONGEST_S, lookback=LOOKBACK_S)
        self.viewer = viewer  # [position], the picture's timeline second
        self.clock = clock
        self.keep = keep  # (start, end, sound, text) of each utterance read whole
        # a split part's end read again by the next part: where that begins, where its words
        # not told yet begin, the words told so far, the word left to it (as heard)
        self._carry: tuple[float, float, list[Word], list[str]] = (
            float("nan"),
            float("-inf"),
            [],
            [],
        )
        self.newest = float("-inf")
        self.cost = _COST_START
        self.readings = 0
        self.partial_readings = 0
        self.read_s = 0.0  # seconds of sound read, all readings
        self._ended: deque[Piece] = deque()
        self._utterance: Optional[_Utterance] = None
        self._pending: list[Word] = []  # written, not yet in a line
        self._pending_final = False  # they end an utterance: the last line takes a full stop
        # where the picture was, when it last moved: waiting until it is seen to move (a run
        # starts with it waiting for the captions' distance, or there already)
        self._seen = (float(viewer[0]), clock() - HELD_AFTER_S)
        self._last = (float("-inf"), float("-inf"))  # the last line's start and end

    # ---- in ------------------------------------------------------------------------------
    def push(self, at: float, samples: array) -> None:
        self.newest = at + len(samples) / RATE
        self._ended.extend(self.cutter.push(at, samples))

    def feed(self, at: float, samples: array) -> list[tuple[str, float, float]]:
        self.push(at, samples)
        return self.work()

    def flush(self) -> list[tuple[str, float, float]]:
        """The end of the sound: what is going on is read whole."""
        self._ended.extend(self.cutter.flush())
        lines = self.work()
        return lines + self._lines(everything=True)

    # ---- the picture ---------------------------------------------------------------------
    def time_left(self, t: float) -> float:
        """Seconds until the picture gets to timeline second `t`."""
        now, position = self.clock(), float(self.viewer[0])
        if position != self._seen[0]:
            self._seen = (position, now)
        return time_left(t, position, now - self._seen[1], self.newest)

    def frontier(self) -> float:
        """The timeline second up to which lines are out (what is not, is still to come)."""
        if self._pending:
            return self._pending[0].start
        if self._ended:
            return self._ended[0].start
        piece = self.cutter.open_piece()
        if piece is not None:
            utterance = self._utterance
            if utterance is not None and utterance.start == piece.start and utterance.written:
                return utterance.written[-1].end
            return piece.start
        return self.newest

    # ---- reading -------------------------------------------------------------------------
    def work(self) -> list[tuple[str, float, float]]:
        lines: list[tuple[str, float, float]] = []
        while self._ended:
            lines += self._finish(self._ended.popleft())
        return lines + self._grow()

    def _read(self, samples: array) -> list[str]:
        started = self.clock()
        try:
            text = self.decode(samples)
        except Exception as error:
            logger.warning("AI captions: a reading failed: %s", type(error).__name__)
            text = ""
        took = self.clock() - started
        length = max(1.0, len(samples) / RATE)
        self.cost = 0.7 * self.cost + 0.3 * (took / length)
        self.readings += 1
        self.read_s += len(samples) / RATE
        return words_of(text)

    def _estimate(self, seconds: float) -> float:
        return self.cost * max(1.0, seconds)

    def _finish(self, piece: Piece) -> list[tuple[str, float, float]]:
        """An utterance ended (a pause, or cut long): read whole, its words past the written."""
        utterance = self._utterance
        if utterance is None or utterance.start != piece.start:
            utterance = self._new_utterance(piece)
        self._utterance = None
        passed = piece.end + PASSED_S < float(self.viewer[0])
        if piece.speech_share < MIN_SPEECH_SHARE or passed:
            return self._close(final=not piece.split)
        new = self._new_words(utterance, piece)
        if self.keep is not None:
            said = " ".join([w.text for w in utterance.written] + new)
            try:
                self.keep(piece.start, piece.end, piece.samples, said)
            except Exception as error:  # a full disk never stops the captions
                logger.warning("AI captions: a trial piece was not kept: %s", error)
        at, voiced = utterance.unwritten(piece)
        placed = place(new, at, voiced)
        if piece.overlap:
            # Split with no pause between words: its last word may be the one cut in two, so it
            # is left to the next part, which reads OVERLAP_S of this one again; the words it
            # has again are not new to it.
            again = piece.end - piece.overlap
            left_over = placed.pop() if placed else None
            told = utterance.carried + utterance.written + placed
            if left_over is not None:
                told_to = left_over.start
            else:
                told_to = utterance.written[-1].end if utterance.written else utterance.told_to
            left = [left_over.text] if left_over is not None else []
            self._carry = (again, max(told_to, utterance.told_to), told[-CARRIED_WORDS:], left)
        self._pending += placed
        return self._close(final=not piece.split)

    def _close(self, final: bool) -> list[tuple[str, float, float]]:
        self._pending_final = final
        return self._lines(everything=True)

    def _grow(self) -> list[tuple[str, float, float]]:
        """The utterance going on: read again as the picture nears its words not written yet,
        and the line two readings agree on written once the picture is about to get there (if
        the utterance has not ended by then: it is read whole)."""
        piece = self.cutter.open_piece()
        if piece is None:
            return []
        utterance = self._utterance
        if utterance is None or utterance.start != piece.start:
            utterance = self._utterance = self._new_utterance(piece)
        length = piece.end - piece.start
        if length >= LONG_S:
            ended = self._split(utterance, piece, length >= LONGEST_HELD_S)
            if ended is not None:
                return ended
        if piece.end + PASSED_S < float(self.viewer[0]):
            return []  # the picture is past it (a seek): the time goes to what comes next
        at, voiced = utterance.unwritten(piece)
        # where the line of the first word not written yet shows
        shows = at + next((i for i, v in enumerate(voiced) if v), 0) * WINDOW / RATE - FIRST_WORD_S
        # what a reading takes: of the whole so far, or from CONTEXT_S before the written words
        reading = self._estimate(min(length, piece.end - at + CONTEXT_S + 0.6))
        step = max(STEP_S, STEP_SHARE * reading)
        left = self.time_left(shows)
        last = left <= reading + MARGIN_S  # no time for another reading after the next
        # two readings a step apart before that (unless the utterance ends first: read whole)
        due = left <= 2 * reading + step + MARGIN_S
        fresh = piece.end - utterance.read_to >= (STEP_S / 2 if last else step)
        pause = None
        if due:
            # the longest pause between words long enough to be one (a sentence's end, more
            # often than a comma's), past its first second and the written words: it ends there
            # once its first words would otherwise come late
            lo = max(at, piece.start) + FIRST_READ_S
            pause = self.cutter.pause_between(lo, piece.end - 0.2, SPLIT_PAUSE_S)
            # (a reading's time more: one going on when the time comes delays it that much)
            if pause is not None and left <= 2 * reading + MARGIN_S + SPLIT_SLACK_S:
                part = self.cutter.split_at(pause)
                return [] if part is None else self._finish(part)
        if length >= FIRST_READ_S and fresh and due and pause is None:
            if not utterance.sure or not last:
                self._read_again(utterance, piece, at, voiced)
        if utterance.ready and self.time_left(shows) <= reading + MARGIN_S:
            taken, utterance.ready, utterance.sure = utterance.ready, [], False
            # and the last reading's words after it that would come late by the next time
            # (readings that agree on a word at a time fell behind the picture on a shouted
            # drama scene): never past its end, which `clear` leaves out
            for word in utterance.clear[len(taken) :]:
                if self.time_left(word.start - FIRST_WORD_S) > reading + step + MARGIN_S:
                    break
                taken.append(word)
            utterance.clear = []
            utterance.written += taken
            utterance.heard = utterance.heard[len(taken) :]
            self._pending += taken
            return self._lines()
        return []

    def _read_again(
        self, utterance: _Utterance, piece: Piece, at: float, voiced: list[bool]
    ) -> None:
        """A reading of the utterance so far: the first line of what it agrees on with the
        reading before is ready to write (or, alone, of what it heard but its last half second)."""
        self.partial_readings += 1
        new = self._new_words(utterance, piece)
        before, utterance.heard, utterance.read_to = utterance.heard, new, piece.end
        same = agreed(before, new)
        placed = place(new, at, voiced)
        utterance.sure = same > 0
        # never a reading's last word, nor the words of its last half second (as far as their
        # guessed times tell), agreed on or not: a word both readings' ends cut in two comes out
        # alike in both ("geçirmeniz", "geçirmenizi diliyorum")
        timed = len([w for w in placed if w.end <= piece.end - HOLDBACK_S])
        clear = max(0, min(len(new) - 1, timed))
        same = min(same, clear) if same else clear
        # the first line of it: the words after it may still be read whole
        cut = Lines._cut(placed[:same], True) if same else None
        utterance.ready = placed[: same if cut is None else cut + 1]
        utterance.clear = placed[:clear]
        if utterance.ready and len(utterance.ready) == len(placed):
            # its last word: a full stop there is where the reading ended
            utterance.ready[-1].text = utterance.ready[-1].text.rstrip(".")

    def _new_words(self, utterance: _Utterance, piece: Piece) -> list[str]:
        """A reading of the utterance's sound so far, its words past the ones written. Once some
        are written it begins CONTEXT_S before their end (at a pause between words there): what
        is read stays short however long the utterance runs, and the model still hears the
        words that lead to the new ones."""
        skip = 0
        if utterance.written:
            step = WINDOW / RATE
            aim = round((utterance.written[-1].end - CONTEXT_S - piece.start) / step)
            if aim > round(1.0 / step):
                skip = pause_in(piece.voiced, aim - round(0.6 / step), aim + round(0.4 / step))
        begins = piece.start + skip * WINDOW / RATE
        heard = self._read(piece.samples[skip * WINDOW :])
        # Matched against the written words from HELD_S before where it begins, not only the
        # ones their guessed times put in it: the guesses can be a second off, and a written
        # word the reading had again was then new (the owner, 2026-11-07: a line written again
        # without its first word).
        told = utterance.carried + utterance.written
        held = [w.text for w in utterance.carried] + [
            w.text for w in utterance.written if w.end > begins - HELD_S
        ]
        expect = len([w for w in told if w.end > begins + 0.15])
        j = continues(held, heard, expect)
        new = heard[j:]
        if held and j:
            # The last written word read again run together with the next one: "Kara" was
            # written and this reading has "Karakedi" (the owner, 2026-11-08: the phrase came
            # as "Kara" and "yolunu kesti.").
            rest = joined_rest(held[-1], heard[j - 1], utterance.heard or utterance.left)
            if rest is not None:
                new = [rest] + new
        return new

    def _new_utterance(self, piece: Piece) -> _Utterance:
        """The utterance of a piece that has begun; one that reads the end of a split part
        again carries that part's words."""
        again, told_to, words, left = self._carry
        if abs(again - piece.start) < 1e-3:
            return _Utterance(piece.start, carried=words, told_to=told_to, left=list(left))
        return _Utterance(piece.start)

    def _split(
        self, utterance: _Utterance, piece: Piece, anyway: bool
    ) -> Optional[list[tuple[str, float, float]]]:
        """A long utterance ends at its longest pause between words after its written words
        (they stay in it), read whole as one that ended; with no such pause it goes on
        (`anyway`: it ends at its quietest window there, and the next part reads OVERLAP_S of
        it again, so a word cut in two comes whole). None: it goes on."""
        lo = (utterance.written[-1].end if utterance.written else piece.start + LONG_S / 2) + 0.1
        at = self.cutter.pause_between(lo, piece.end - 0.3, SPLIT_PAUSE_S)
        back = 0.0
        if at is None and anyway:
            at, back = self.cutter.quietest_between(lo, piece.end - 0.3), OVERLAP_S
        part = None if at is None else self.cutter.split_at(at, back=back)
        return None if part is None else self._finish(part)

    # ---- lines ---------------------------------------------------------------------------
    def _lines(self, everything: bool = False) -> list[tuple[str, float, float]]:
        """Lines out of the written words (they are written once their time has come, or the
        utterance ended: `everything`)."""
        out: list[tuple[str, float, float]] = []
        while self._pending:
            words = self._pending
            cut = Lines._cut(words, True)
            if cut is None:
                break
            line = words[: cut + 1]
            del words[: cut + 1]
            text = " ".join(w.text for w in line)
            if not words and everything and self._pending_final:
                if not ends_sentence(text):
                    text += "."  # a pause ended what was said: its line ends a sentence
            # The captions show a line from FIRST_WORD_S past its start: one that begins after
            # a pause starts that much before its first word; one that goes on from the line
            # before starts at its word, right after that line, so the captions may join them
            # (a short end, cut off on its own). Two lines never start or end alike (the
            # captions take a line with another's start or end for the same line again).
            first, previous_end = line[0].start, self._last[1]
            goes_on = first - previous_end <= JOIN_GAP_S
            start = max(first if goes_on else first - FIRST_WORD_S, previous_end)
            start = max(start, self._last[0] + 0.05)
            end = max(line[-1].end, start + 0.5, previous_end + 0.05)
            self._last = (start, end)
            out.append((text, start, end))
        if not self._pending:
            self._pending_final = False
        return out
