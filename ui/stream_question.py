"""Before a recording is stopped for a stream (2.6, Record as a PVR): the question.

A recording holds its own stream at its provider. Whatever else wants one there (a zap, a film,
the picture in picture, an outside player, the phone's Watch) gets it when one is free; when
none is, `needs` asks "No stream is free: {channel} is recording. Stop the recording?" on the
card of the page that shows (the player's, the Library's), or on the phone that asked (Task 15:
`asked`). Stop recording stops as many recordings of that provider as the open needs, oldest
first, waits for their streams to go back, then runs what was asked; Keep recording changes
nothing. A recording only counts when stopping it gives its stream back: the large picture's
only when the open replaces it (the picture in picture never takes the big picture's stream).

`confirm` asks the same of one recording that would stop anyway (a multiview tile's channel
change or ✕: a tile has no background to go to).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.i18n import N_, translate
from services.sessions import Slot, records
from ui.keymap import Action

logger = logging.getLogger(__name__)

# i18n: a question when the provider's streams are all in use and one of them records;
# {channel} is the recording channel's name
STREAM_QUESTION = N_("No stream is free: {channel} is recording. Stop the recording?")
# i18n: a question before a multiview tile that records changes channel or closes; {channel}
# is the recording channel's name
TILE_QUESTION = N_("{channel} is recording in this tile. Stop the recording?")
STOP_RECORDING = N_("Stop recording")  # i18n: the question's button: stop it and go on
KEEP_RECORDING = N_("Keep recording")  # i18n: the question's button: nothing changes
RELEASE_WAIT_MS = 8000  # a stopped recording's stream not back by then: go on anyway


@dataclass
class _Question:
    provider: str
    then: Callable[[], None]
    stop: list[Slot]
    replaced: str  # the purpose whose stream the open takes over (none to wait for)
    ask_on: Optional[str] = None
    waiting: set[str] = field(default_factory=set)  # purposes whose streams must go back


class StreamQuestion(QObject):
    # Task 15: a question for phone `ask_on` (its text, the answers' words)
    asked = Signal(str, str, str, str)
    settled = Signal(str)  # the phone's question is gone (answered, or replaced)

    def __init__(
        self,
        *,
        budget,
        recordings: Callable[[], list[Slot]],
        large: Callable[[], Optional[Slot]],
        card: Callable[[], Any],
        stop: Callable[[Slot, bool], None],
        closed: Callable[[], bool],
        parent: Optional[QObject] = None,
    ) -> None:
        """`recordings()` every player that records, `large()` the big picture's, `card()` the
        QuestionCard of the page that shows (None: none to ask on), `stop(slot, replaced)`
        stops a recording and, unless `replaced` (the open takes its stream over), lets its
        stream go; `closed()` whether the shell closes."""
        super().__init__(parent)
        self._budget = budget
        self._recordings = recordings
        self._large = large
        self._card = card
        self._stop = stop
        self._closed = closed
        self._question: Optional[_Question] = None
        self._waited: Optional[_Question] = None
        self._shown: Any = None  # the card that asks now
        self._cards: list[Any] = []  # the cards whose answers are followed
        self._going = False  # what was asked runs: it asks nothing again
        self._wait = QTimer(self, singleShot=True)
        self._wait.timeout.connect(self._go_on)
        budget.released.connect(self._on_released)

    def needs(
        self,
        provider: str,
        limit: Optional[int],
        purpose: str,
        then: Callable[[], None],
        *,
        gives_way: bool = False,
        ask_on: Optional[str] = None,
    ) -> bool:
        """True (and nothing runs now) when a recording holds a stream at `provider` and none
        is free for an open that takes over `purpose`'s stream (`gives_way`: every stream in
        the app that does not record goes, as for an outside player): asked. False: the
        caller goes on at once (a stream is free, or stopping recordings would not free one
        and the caller's own refusal says why)."""
        if self._going or self._closed():
            return False
        capped = self._budget.cap(provider, limit)
        if capped is None:
            return False
        holders = self._budget.holders(provider)
        large = self._large()
        recording = [slot for slot in self._recordings() if _purpose(slot) in holders]
        if not recording:
            return False
        recorded = {_purpose(slot) for slot in recording}
        if gives_way:
            others = [p for p in holders if p in recorded]
        else:
            others = [p for p in holders if p != purpose or p in recorded]
        if len(others) < capped:
            return False
        stoppable = [s for s in recording if s is not large or _purpose(s) == purpose]
        needed = len(others) - capped + 1
        if needed > len(stoppable):
            return False
        stop = stoppable[:needed]
        text = translate(STREAM_QUESTION, channel=_name(stop[0]))
        return self._ask(_Question(provider, then, stop, purpose, ask_on), text)

    def confirm(
        self, slot: Slot, then: Callable[[], None], *, ask_on: Optional[str] = None
    ) -> bool:
        """`slot` records and would stop (a multiview tile): True, asked; False when it does
        not record (the caller goes on)."""
        if self._going or self._closed() or not records(slot):
            return False
        question = _Question("", then, [slot], _purpose(slot), ask_on)
        return self._ask(question, translate(TILE_QUESTION, channel=_name(slot)))

    def answer(self, yes: bool) -> None:
        """The answer, from the card or the phone."""
        question, self._question = self._question, None
        card, self._shown = self._shown, None
        if card is not None and card.isVisible():
            card.hide()
        if question is None:
            return
        if question.ask_on is not None:
            self.settled.emit(question.ask_on)
        if not yes or self._closed():
            logger.info("A recording kept its stream: nothing else opened")
            return
        for slot in question.stop:
            replaced = _purpose(slot) == question.replaced
            if not replaced and question.provider:
                question.waiting.add(_purpose(slot))
            self._stop(slot, replaced)
        self._waited = question
        self._wait.start(RELEASE_WAIT_MS)
        self._on_released(question.provider)

    def handle(self, action: Action) -> bool:
        """A key while the card asks: True when it is the card's."""
        card = self._shown
        return card is not None and card.handle(action)

    def asking(self) -> bool:
        return self._question is not None

    # ---- internals ----------------------------------------------------------------------

    def _ask(self, question: _Question, text: str) -> bool:
        old = self._question
        if old is not None and old.ask_on is not None:
            self.settled.emit(old.ask_on)  # a newer question stands for it
        yes, no = translate(STOP_RECORDING), translate(KEEP_RECORDING)
        if question.ask_on is not None:
            self._question = question
            self.asked.emit(question.ask_on, text, yes, no)
            return True
        card = self._card()
        if card is None:
            return False
        if not any(known is card for known in self._cards):
            self._cards.append(card)
            card.answered.connect(partial(self._on_card, card))
        if self._shown is not None and self._shown is not card:
            self._shown.hide()  # the other page's card: no answer
        self._question = question
        self._shown = card
        card.ask(text, yes, no)
        return True

    def _on_card(self, card: Any, yes: bool) -> None:
        if card is self._shown:
            self._shown = None
            self.answer(yes)

    def _on_released(self, provider: str) -> None:
        question = self._waited
        if question is None or provider != question.provider:
            return
        holders = set(self._budget.holders(provider))
        if not question.waiting & holders:
            self._go_on()

    def _go_on(self) -> None:
        question, self._waited = self._waited, None
        self._wait.stop()
        if question is None or self._closed():
            return
        self._going = True
        try:
            question.then()
        finally:
            self._going = False


def _purpose(slot: Slot) -> str:
    return str(getattr(slot.tuner, "purpose", slot.name))


def _name(slot: Slot) -> str:
    channel = slot.channel or {}
    return str(channel.get("name") or "")
