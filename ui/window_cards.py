"""QiTV's own questions and messages outside Settings (2.13.1): what the system's message boxes
asked or said before (an update's messages, "Quit while recording?" before an update, a
recording's deletion) is Settings' ChoiceCard (ui/overlays/settings_card.py), over the page that
shows: no window of the system's, and nothing waits in an exec().

One question at a time: one asked while another shows, or before the window first shows (an
update's at the start), waits its turn, and comes from the event loop once the one before it was
answered. Over live TV the card is one of the TV's own (TVController.add_card: it has every key
but the sound's and the window's, before Settings, the guide and the channel list, which wait
for it, and the drawer's keys reach it, so the arrows go between its answers); the mini player
becomes the window first and the audio dock's ⋯ menu closes. What QiTV says unasked (`unasked`:
an update found at the start, a download's result) waits while the mini player shows (`held`),
the small window left as it is, and comes once it ends (`release`); a question asked for
meanwhile comes first. Over the Library (the player in its corner or not) its twin on the
Library page has the keys before the Library (`library_key`). QiTV's own questions (2.6's,
about a recording) stay above it.

Each question answers through its `then(key)`: the key of the button or the other way pressed,
or its `back` answer for Esc, Backspace, the phone's Back, the mouse's Back and a click on the
dimmed picture. A question goes with its page, answered as its Back: live TV's card cancels as
Open Link's does when the Library, Settings or a stop comes; the Library's when live TV comes.
An answer while the window closes is its Back too, and nothing is asked once it closes.

A progress (`progress`: an update's download) is such a question with a bar (ProgressCard):
`Progress.set` moves it, `Progress.close` takes it away unanswered once its work is done, and
what waited for it comes then.

`install` makes a window's cards the ones `ask`, `tell` and `progress` use (held weakly: a
window that went asks nothing); with none, nothing is asked (logged).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import partial
import logging
from typing import Callable, Iterable, Optional, Sequence
import weakref

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from services.i18n import translate
from ui.keymap import Action
from ui.overlays.progress_card import ProgressCard
from ui.overlays.settings_card import CANCEL, OK, OK_TEXT, Button

logger = logging.getLogger(__name__)

Answer = Callable[[str], None]  # a question's: the key answered


@dataclass(eq=False)
class _Question:
    title: str
    body: str  # (a progress's changes)
    buttons: tuple[Button, ...]
    default: str
    then: Answer
    alternates: tuple[Button, ...]
    back: str
    progress: Optional[float] = None  # a progress's bar (None: a question's, none)
    closed: bool = False  # a progress whose work is done
    unasked: bool = False  # QiTV says it by itself: it waits while the mini player shows


class Progress:
    """A progress card's handle (WindowCards.progress): `set` moves its bar and changes its
    words (shown now, or once it shows), `close` takes it away unanswered. Once it was answered
    or closed, neither changes anything."""

    def __init__(self, cards: Optional[WindowCards], question: Optional[_Question]) -> None:
        self._cards = cards
        self._question = question

    def set(self, fraction: float, text: str) -> None:
        cards, question = self._cards, self._question
        if cards is not None and question is not None and isValid(cards):
            cards._moved(question, fraction, text)

    def close(self) -> None:
        cards, question = self._cards, self._question
        if cards is not None and question is not None and isValid(cards):
            cards._close(question)


class WindowCards(QObject):
    """The window's two cards, `tv_card` (on the TV's overlay host: the caller adds it to the
    host's overlays and to TVController.add_card) and `library_card` (on the Library page), and
    the questions waiting for them. `library_shown()` says whether the Library is the page in
    front, `closing()` whether the window closes; `before_tv()` makes way for the TV's card
    (the mini player, the ⋯ menu); `held()` whether what QiTV says unasked waits (the mini
    player shows: `release` once it ends); `gives_way()` whether the
    window is hidden for the small window (the macOS mini player: a question asked for brings
    it back through `before_tv`); `above()` the cards that stay over this one."""

    def __init__(
        self,
        tv_card: ProgressCard,
        library_card: ProgressCard,
        *,
        window: QWidget,
        library_shown: Callable[[], bool],
        closing: Callable[[], bool],
        before_tv: Callable[[], None] = lambda: None,
        held: Callable[[], bool] = lambda: False,
        gives_way: Callable[[], bool] = lambda: False,
        above: Callable[[], Iterable[QWidget]] = tuple,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.tv_card = tv_card
        self.library_card = library_card
        self._window = window
        self._library_shown = library_shown
        self._closing = closing
        self._before_tv = before_tv
        self._held = held
        self._gives_way = gives_way
        self._above = above
        self._waiting: deque[_Question] = deque()
        self._asked: Optional[_Question] = None  # the one shown
        self._card: Optional[ProgressCard] = None  # where it shows
        for card in (tv_card, library_card):
            card.answered.connect(partial(self._answered, card))
        window.installEventFilter(self)  # its first show: what waited for it

    # ---- asking -------------------------------------------------------------------------------

    def ask(
        self,
        title: str,
        body: str,
        buttons: Sequence[Button],
        default: str,
        then: Answer,
        *,
        alternates: Sequence[Button] = (),
        back: str = CANCEL,
        unasked: bool = False,
    ) -> None:
        """Ask now, or once the question before it was answered and the window shows (one
        `unasked`, that QiTV says by itself, once the mini player ends too). The texts are the
        app's language's; the keys start on `default`."""
        if self._closing():
            return
        question = _Question(title, body, tuple(buttons), default, then, tuple(alternates), back)
        question.unasked = unasked
        self._waiting.append(question)
        self._next()

    def release(self) -> None:
        """The mini player ended (`held` no longer): what QiTV said unasked meanwhile comes,
        from the event loop."""
        QTimer.singleShot(0, self, self._next)

    def progress(
        self,
        title: str,
        text: str,
        buttons: Sequence[Button],
        default: str,
        then: Answer,
        *,
        back: str = CANCEL,
    ) -> Progress:
        """A question with a bar, at 0 (`text` under the title), asked as `ask` asks: its
        handle moves it, and closes it once its work is done (unanswered)."""
        question = _Question(title, text, tuple(buttons), default, then, (), back, progress=0.0)
        if self._closing():
            return Progress(None, None)
        self._waiting.append(question)
        self._next()
        return Progress(self, question)

    def shown(self) -> bool:
        """Whether a question shows (it has the keys)."""
        return self._asked is not None

    def library_key(self, action: Action, payload: object = "") -> bool:
        """A key while the Library shows: True when it is the question's there (every key but
        the sound's and the window's)."""
        card = self._card
        if self._asked is None or card is not self.library_card or card.isHidden():
            return False
        return card.handle(action, payload)

    def page_changed(self, _mode: str = "") -> None:
        """The window shows another page (PageStack.modeChanged): a question on the page that
        went is answered as its Back."""
        card = self._card
        if self._asked is None or card is None or card is self._front():
            return
        card.hide()  # (a ChoiceCard hidden by its caller answers nothing itself)
        self._answered(card, CANCEL)

    # ---- showing ------------------------------------------------------------------------------

    def _front(self) -> ProgressCard:
        return self.library_card if self._library_shown() else self.tv_card

    def _next(self) -> None:
        if self._asked is not None or not self._waiting:
            return
        if self._closing():
            self._waiting.clear()
            return
        if not self._window.isVisible() and not self._gives_way():
            return  # its show asks (`eventFilter`)
        held = self._held()  # (what QiTV says unasked waits: `release` asks again)
        question = next((q for q in self._waiting if not (held and q.unasked)), None)
        if question is None:
            return
        if not self._window.isVisible():
            self._before_tv()  # hidden for the macOS mini player: asked for, the window comes
            if not self._window.isVisible():
                return
        self._waiting.remove(question)
        card = self._front()
        if card is self.tv_card:
            self._before_tv()
        self._asked, self._card = question, card
        card.ask(
            question.title,
            question.body,
            list(question.buttons),
            question.default,
            alternates=list(question.alternates),
            progress=question.progress,
        )
        for widget in self._above():
            if widget is not None and isValid(widget) and widget.isVisible():
                widget.raise_()

    def _moved(self, question: _Question, fraction: float, text: str) -> None:
        if question.closed or question.progress is None:
            return
        question.progress, question.body = fraction, text
        if question is self._asked and self._card is not None:
            self._card.set_progress(fraction, text)

    def _close(self, question: _Question) -> None:
        """A progress's work is done: it goes unanswered (shown: what waited comes next)."""
        if question.closed:
            return
        question.closed = True
        if question in self._waiting:
            self._waiting.remove(question)
        if question is not self._asked:
            return
        card, self._asked, self._card = self._card, None, None
        if card is not None:
            card.hide()  # (a card hidden by its caller answers nothing)
        QTimer.singleShot(0, self, self._next)

    def _answered(self, card: ProgressCard, key: str) -> None:
        if card is not self._card or self._asked is None:
            return
        question, self._asked, self._card = self._asked, None, None
        question.closed = True  # answered: its handle changes nothing more
        if key == CANCEL or self._closing():
            key = question.back
        try:
            question.then(key)
        except Exception:
            logger.exception("A question's answer failed")
        QTimer.singleShot(0, self, self._next)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._window and event.type() == QEvent.Type.Show:
            QTimer.singleShot(0, self, self._next)
        return False


def _nothing(_key: str) -> None:
    pass


# The TV window's cards, once it built them (TVWindow: `install`).
_installed: Optional[weakref.ReferenceType[WindowCards]] = None


def install(cards: Optional[WindowCards]) -> None:
    """`cards` are the ones `ask` and `tell` use from now on (None: none)."""
    global _installed
    _installed = weakref.ref(cards) if cards is not None else None


def installed() -> Optional[WindowCards]:
    ref = _installed
    cards = ref() if ref is not None else None
    return cards if cards is not None and isValid(cards) else None


def ask(
    title: str,
    body: str,
    buttons: Sequence[Button],
    default: str,
    then: Answer,
    *,
    alternates: Sequence[Button] = (),
    back: str = CANCEL,
    unasked: bool = False,
) -> None:
    """WindowCards.ask on the installed window's cards; with none, nothing is asked."""
    cards = installed()
    if cards is None:
        logger.info("A question with no window to ask it on: not asked (%s)", title)
        return
    cards.ask(
        title, body, buttons, default, then, alternates=alternates, back=back, unasked=unasked
    )


def progress(
    title: str,
    text: str,
    buttons: Sequence[Button],
    default: str,
    then: Answer,
    *,
    back: str = CANCEL,
) -> Progress:
    """WindowCards.progress on the installed window's cards; with none, a handle that moves
    nothing (the work goes on unseen)."""
    cards = installed()
    if cards is None:
        logger.info("A progress with no window to show it on: not shown (%s)", title)
        return Progress(None, None)
    return cards.progress(title, text, buttons, default, then, back=back)


def tell(title: str, body: str, then: Optional[Answer] = None, *, unasked: bool = False) -> None:
    """A message with OK (the accent: its only answer, Back too) on the installed window's
    cards (through `ask`); `then` once it is read."""
    ok = Button(OK, translate(OK_TEXT), accent=True)
    ask(title, body, [ok], OK, then or _nothing, back=OK, unasked=unasked)
