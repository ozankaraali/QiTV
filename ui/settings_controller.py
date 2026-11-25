"""Binds the Settings layer of the drawer to the config and to the key actions.

A step is applied at once (no Save): the row's next choice is written through the values and
the overlay paints again; so is a text row's text once it is kept. The controller keeps the
key actions it uses and hands every other one back, so volume, mute and fullscreen still reach
the player. While a text is edited the field has the keys first (typing, Backspace, the caret,
the clipboard; Enter and Esc); else typing and Paste find a setting, and while a find shows
Backspace edits its text (Esc, ← or Enter on a match end it: SettingsDrawer's find).

The sections are built by the caller's function, again whenever what they show changed
(`refresh_sections`: a provider added, removed or renamed, here or elsewhere), and the drawer
keeps its place in them by key. A row whose key starts with a registered prefix is that
handler's (`register`: Providers' rows are `provider/<name>/<field>`), its value, steps,
edits and actions (the rows paint again after each; a row's own data, its note or sub line,
changes only when the handler builds the sections again); every other key is a config setting,
through the values. A card over the picture (`show_card`: a form, a question) has every key
before the rows while it shows.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
import logging
from typing import Any, Optional, Protocol

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtWidgets import QWidget

from services.settings_model import INFO, TEXT, Row, Section, choice_of, is_lit, step
from services.settings_values import SettingsValues
from ui.keymap import Action
from ui.overlays.base import Overlay
from ui.overlays.settings_drawer import SettingsDrawer
from ui.remote_typing import PhoneField, field_of

logger = logging.getLogger(__name__)

# NAV_RIGHT's payload from the mouse's Forward button: a layer down only, never a step.
MOUSE_FORWARD = "mouse forward"
# A stepper whose change loads something again (the guide's source, Verify SSL) waits this long
# after its last step: passing through its values loads once, for the one the steps stop on.
STEPS_SETTLE_MS = 600


class Settled(QObject):
    """`act()` once the steps stop, if they changed what `value()` gives: each `start()` waits
    STEPS_SETTLE_MS again, and then `act` runs only when the value differs from `before`, what
    it was before the first of these steps (steps that end where they began, as ← meant as
    "back" then →, do nothing). `before` is the value as the Settled is made, then as each stop
    of the steps leaves it: only the steps it hears change the value. Never while `closing()`
    holds (the window closes). Made on the GUI thread; `act` runs there."""

    def __init__(
        self,
        act: Callable[[], None],
        closing: Callable[[], bool],
        parent: QObject,
        value: Callable[[], Any],
    ):
        super().__init__(parent)
        self._act = act
        self._closing = closing
        self._value = value
        self._before = value()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(STEPS_SETTLE_MS)
        self._timer.timeout.connect(self._settled)

    @property
    def before(self) -> Any:
        """The value before the steps (as the last stop of them left it)."""
        return self._before

    def waiting(self) -> bool:
        """Whether steps are on their way: their stop is yet to come."""
        return self._timer.isActive()

    def start(self) -> None:
        self._timer.start()

    def _settled(self) -> None:
        if self._closing():
            return
        now = self._value()
        if now != self._before:
            self._before = now
            self._act()


class RowHandler(Protocol):
    """The rows of a key namespace (`SettingsController.register`): what they show and what
    a step, a text kept or a choice on one does (it saves, and says what failed)."""

    def value(self, row: Row) -> Any:
        """A stepper's current choice's value, or a text or info row's text."""

    def step(self, row: Row, delta: int) -> None:
        """The stepper `row` one choice on (+1) or back (-1)."""

    def edited(self, row: Row, text: str) -> None:
        """The text row `row`'s new text, kept."""

    def chosen(self, row: Row) -> None:
        """The row `row` acts, or asks for the card it opens."""


class Card(Protocol):
    """A card over the picture (ui/overlays/settings_card.py's FormCard and ChoiceCard): it
    takes a key while it shows (False for one that is not its own: the sound's, the window's),
    and its Esc is its Cancel (it hides)."""

    def handle(self, action: Action, payload: object = "") -> bool: ...

    def isHidden(self) -> bool: ...

    def isVisible(self) -> bool: ...

    def show(self) -> None: ...

    def hide(self) -> None: ...

    def raise_(self) -> None: ...


def _walk(rows: tuple[Row, ...]) -> Iterator[Row]:
    """Each row, then the rows of the layer it opens, and so on."""
    for row in rows:
        yield row
        if row.section is not None:
            yield from _walk(row.section.rows)


class SettingsController(QObject):
    hidden = Signal()  # Settings went: backed out of, or closed

    def __init__(
        self,
        drawer: SettingsDrawer,
        values: SettingsValues,
        sections: Callable[[], tuple[Section, ...]],
        reopen_drawer: Callable[[], None],
    ) -> None:
        super().__init__(drawer)
        self._drawer = drawer
        # Where the cards are made, and Settings is at home: the TV window's overlay host. It
        # lends Settings and its cards to the Library's layer while the Library is in front
        # (2.14: lend_overlay, take_back_overlay).
        self._home = drawer.parentWidget()
        self._values = values
        self._make_sections = sections  # the sections as they are now
        self._reopen_drawer = reopen_drawer
        self._from_drawer = False
        self._sections: tuple[Section, ...] = ()
        self._handlers: list[tuple[str, RowHandler]] = []  # the longest prefix first
        self._cards: list[Card] = []  # shown over Settings, the last on top
        drawer.stepped.connect(self._on_stepped)
        drawer.chosen.connect(self._on_chosen)
        drawer.edited.connect(self._on_edited)
        drawer.closed.connect(self._on_closed)
        drawer.installEventFilter(self)  # its own hide, however it came (`hidden`)

    # ---- the rows' namespaces ---------------------------------------------------------------

    def register(self, prefix: str, handler: RowHandler) -> None:
        """The rows whose keys start with `prefix` ("provider/") are `handler`'s: their values,
        steps, texts and actions (a longer prefix registered goes first; the same one
        again replaces it). The other keys are the config's, through the values."""
        handlers = [(p, h) for p, h in self._handlers if p != prefix] + [(prefix, handler)]
        self._handlers = sorted(handlers, key=lambda item: len(item[0]), reverse=True)

    def _handler(self, key: str) -> Optional[RowHandler]:
        return next((h for prefix, h in self._handlers if key.startswith(prefix)), None)

    def _value(self, row: Row) -> Any:
        """The row's value, from its handler or the config (KeyError: a key it has not)."""
        handler = self._handler(row.key)
        return handler.value(row) if handler is not None else self._values.get(row.key)

    # ---- what the overlay shows -------------------------------------------------------------

    def value_text(self, row: Row) -> str:
        """The translated text of a stepper's current choice, or a text or info row's value as
        it is ("" for a row that only opens or acts: it has no value to ask, and for a key the
        values do not know)."""
        if row.choices:
            return choice_of(row, self._value(row)).shown()
        if row.kind not in (TEXT, INFO):
            return ""
        try:
            value = self._value(row)
        except KeyError:
            return ""
        return "" if value is None else str(value)

    def lit(self, row: Row) -> bool:
        """Whether the row's value differs from its default."""
        if not row.choices:
            return False
        return is_lit(row, self._value(row))

    # ---- opening and closing ----------------------------------------------------------------

    def open(self, section_key: str = "", from_drawer: bool = False, row: str = "") -> None:
        """Show Settings at `section_key` (else the first section), on its `row` when one is
        given (any layer's: Free channels' countries, 2.13.1). `from_drawer`: backing out of
        the sections gives the TV drawer back."""
        self._from_drawer = from_drawer
        self._sections = self._make_sections()
        self._drawer.open_at(self._sections, section_key)
        if row:
            self._drawer.show_row(row)

    def refresh_sections(self, renamed: Optional[Mapping[str, str]] = None) -> None:
        """The sections built again: what they show changed (a provider added, removed or
        renamed, here or elsewhere). While they show, the drawer keeps its place in them by
        key (SettingsDrawer.set_sections); `renamed` (old key → new) says which keys are the
        same lines under a new name, so that a provider renamed here keeps the viewer in it."""
        self._sections = self._make_sections()
        if self._drawer.isVisible():
            self._drawer.set_sections(self._sections, renamed=renamed)

    def show_section(self, section_key: str, row: str = "") -> None:
        """While Settings shows elsewhere: at `section_key`, from its top (a card of that
        section asked for from outside it, as the menu bar's Add provider), or on its `row`
        (in it already too: the Add card's Free channels…). Where it was opened from stays:
        backing out of it gives back what it would have, and a card over it stays over it."""
        if not self._drawer.isVisible():
            return
        if self._drawer.path()[:1] != (section_key,):
            self._sections = self._make_sections()
            self._drawer.open_at(self._sections, section_key)
        elif not row:
            return
        if row:
            self._drawer.show_row(row)
        for card in self._cards:
            if card.isVisible():
                card.raise_()  # the drawer raised itself over it

    def show_row(self, key: str) -> bool:
        """While Settings shows: the keys on row `key`, wherever it is (a provider just added,
        among the providers); False when there is no such row."""
        return self._drawer.isVisible() and self._drawer.show_row(key)

    def visible(self) -> bool:
        return self._drawer.isVisible()

    # ---- cards over the picture -------------------------------------------------------------

    def card_parent(self) -> Optional[QWidget]:
        """What a card over Settings is made over: Settings' home (the TV window's overlay
        host, whose overlays a card made over it joins), wherever Settings shows now; it shows
        where Settings is (show_card)."""
        return self._home

    def _place(self, card: Card) -> None:
        """2.14: `card` where Settings is. Settings lent to the Library's layer, its home lends
        the card there too, out of its overlays (none of what the host hides or masks reaches
        it: the corner player's show_only); Settings at home, the card is taken back to its
        place among them. A home that lends nothing (a plain widget) moves the card."""
        home, place = self._home, self._drawer.parentWidget()
        if not isinstance(card, Overlay) or home is None or place is None:
            return
        if place is not home and hasattr(home, "lend_overlay"):
            home.lend_overlay(card, place)
        elif place is home and hasattr(home, "take_back_overlay"):
            home.take_back_overlay(card)
        if card.parentWidget() is not place:  # (one never the home's overlay, or no host)
            card.move_to(place)

    def show_card(self, card: Card) -> None:
        """`card` over Settings (filled first: FormCard.set_form and ChoiceCard.ask show
        it): while it shows, every key is its own before the rows, its Esc its Cancel. A card
        shown over another has the keys until it goes. Settings going cancels them. 2.14: it
        shows where Settings is, over the picture or over the Library (_place); the fill's
        show, over a host that hid it meanwhile (the corner player's show_only), is made good
        here, once it is placed."""
        self._place(card)
        if card in self._cards:
            self._cards.remove(card)
        self._cards.append(card)
        if card.isHidden():
            card.show()
        card.raise_()

    def card_shown(self) -> bool:
        """Whether a card shows over Settings (it has the keys)."""
        return self._card() is not None

    def phone_field(self) -> Optional[PhoneField]:
        """2.15: the text box Settings' keys type into, for a phone's typing sheet: the field
        of the card over it (none while it asks), else the text edited in place (none while the
        rows have the keys: typing there finds a setting)."""
        card = self._card()
        if card is not None:
            return field_of(card)
        return self._drawer.phone_field() if self._drawer.editing() else None

    def _card(self) -> Optional[Card]:
        """The card on top, of those in sight (a card hides itself when it is done). One whose
        page is out of sight, not hidden itself, has no keys until it shows again."""
        self._cards = [card for card in self._cards if not card.isHidden()]
        shown = [card for card in self._cards if card.isVisible()]
        return shown[-1] if shown else None

    def _cancel_cards(self) -> None:
        """Settings went: each card still showing cancels, as its Esc cancels it."""
        cards, self._cards = self._cards, []
        for card in reversed(cards):
            if not card.isHidden():
                card.handle(Action.ESCAPE)
            if not card.isHidden():
                card.hide()

    def keep_edit(self) -> None:
        """A text being edited is kept, as Enter keeps it (a click on the picture closes
        Settings: it is a click elsewhere). Any other hide puts the text back."""
        if self._drawer.editing():
            self._drawer.edit_key(Action.NAV_ACTIVATE)

    def close(self) -> None:
        """Hide Settings; the TV drawer is not reopened."""
        self._from_drawer = False
        self._drawer.hide()

    def back_out(self) -> None:
        """Hide Settings as backing out of its sections does: opened from the TV drawer, it
        gives the drawer back (a choice made in Settings opened for it: a free list's
        country, 2.13.1)."""
        if self._drawer.isVisible():
            self._drawer.hide()
            self._on_closed()

    def handle(self, action: Action, payload: object = None) -> bool:
        """Do a key action on the overlay; False for one it does not use. A card shown over it
        takes every key first (False for the ones it gives back: the sound's, the window's),
        and the rows hear none. While a text is edited its field takes its keys first (Esc
        there puts the text back, Backspace deletes); else typing and Paste find a setting,
        Backspace editing the find's text while it shows. NAV_RIGHT with MOUSE_FORWARD is the
        mouse's Forward: a layer down only (none over a card)."""
        card = self._card()
        if card is not None:
            if action is Action.NAV_RIGHT and payload == MOUSE_FORWARD:
                return True  # a card has no layer to go down to
            return card.handle(action, payload)
        drawer = self._drawer
        if action is Action.NAV_RIGHT and payload == MOUSE_FORWARD:
            drawer.forward()
            return True
        text = payload if isinstance(payload, str) else ""
        if drawer.editing() and drawer.edit_key(action, text):
            return True
        if action is Action.NAV_TEXT:
            drawer.find_type(text)  # (a space alone starts no find: it reaches nothing)
        elif action is Action.PASTE:
            drawer.find_paste()  # as into the channel list's find
        elif action is Action.NAV_BACKSPACE and drawer.finding():
            drawer.find_backspace()
        elif action is Action.NAV_UP:
            drawer.move_by(-1)
        elif action is Action.NAV_DOWN:
            drawer.move_by(1)
        elif action is Action.NAV_PAGE_UP:
            drawer.page(-1)
        elif action is Action.NAV_PAGE_DOWN:
            drawer.page(1)
        elif action is Action.NAV_LEFT:
            drawer.left()
        elif action is Action.NAV_RIGHT:
            drawer.right()
        elif action in (Action.NAV_ACTIVATE, Action.OK):
            drawer.activate()
        elif action in (Action.ESCAPE, Action.NAV_BACKSPACE):
            drawer.back()
        else:
            return False
        return True

    # ---- the overlay's signals --------------------------------------------------------------

    def _find(self, key: str) -> Optional[Row]:
        for section in self._sections:
            for row in _walk(section.rows):
                if row.key == key:
                    return row
        return None

    def _on_stepped(self, key: str, delta: int) -> None:
        row = self._find(key)
        if row is None or not row.choices:
            logger.warning("A step for %r, which is no stepper row", key)
            return
        handler = self._handler(key)
        if handler is not None:
            handler.step(row, delta)
        else:
            self._values.set(key, step(row, self._values.get(key), delta))
        self._drawer.refresh()

    def _on_edited(self, key: str, text: str) -> None:
        row = self._find(key)
        if row is None or row.kind != TEXT:
            logger.warning("An edit for %r, which is no text row", key)
            return
        handler = self._handler(key)
        if handler is not None:
            handler.edited(row, text)  # a rename may build the sections again
        else:
            try:
                self._values.set(key, text)
            except KeyError:
                logger.warning("An edit for %r, which the config has no setting for", key)
                return
        self._drawer.refresh()

    def _on_chosen(self, key: str) -> None:
        handler = self._handler(key)
        if handler is None:
            return
        row = self._find(key)
        if row is None:
            logger.warning("A choice of %r, which is no row", key)
            return
        handler.chosen(row)
        # A value it changed shows and is said (what it changed in the rows' own data, a note,
        # it shows by building the sections again).
        self._drawer.refresh()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Hidden itself, not only with the page or window it is on.
        if watched is self._drawer and event.type() == QEvent.Type.Hide:
            if self._drawer.isHidden():
                self._cancel_cards()
                self.hidden.emit()
        return False

    def _on_closed(self) -> None:
        from_drawer, self._from_drawer = self._from_drawer, False
        if from_drawer:
            self._reopen_drawer()
