"""Settings › Parental controls in the drawer (plan 3): the old Settings window's tab, each
action at once, through the window's services.parental guard as the old tab used it.

- The first line says the state, in the old tab's words: off (no PIN), locked, or unlocked for
  now;
- Set PIN… (Change PIN… once one is set) opens a card: the current PIN first when one is set,
  then the new one twice. Remove PIN… asks the current one; Unlock for an hour…, only while
  locked, asks it too; Lock now, only while unlocked, ends the hour. The old rules and
  messages: a PIN is 4 digits or more, typed the same twice, and a wrong one is refused (said in
  the card, which stays for another try). The fields are secret and take digits only;
- Hide groups whose names look adult is a switch only with a PIN, and while locked with it on
  it only says so (turning it off needs the PIN: unlocked);
- Hidden while locked › lists what is hidden, each shown again at once with a press; while
  locked the line only says to unlock first, and nothing of it is listed.

After every change TVWindow.parental_changed, which builds these rows again too (the hour
running out while Settings shows as well). The same card asks the PIN before a change parental
controls guard elsewhere in Settings (`ask_pin`: a provider renamed or removed), answered
through a callback (2.13.1: no system window). No PIN is ever logged, shown or said. Keys:
`parental/…`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote

from PySide6.QtCore import QObject

from services.i18n import N_, number, translate
from services.parental import Parental
from services.provider_ids import provider_title
from services.settings_model import (
    ACTION,
    DANGER,
    INFO,
    ON_OFF,
    STEP,
    Row,
    Section,
    step as stepped,
)
from ui.overlays.settings_card import CANCEL, CANCEL_TEXT, OK, OK_TEXT, Button, Field, FormCard
from ui.settings_controller import SettingsController

logger = logging.getLogger(__name__)

MIN_DIGITS = 4  # a PIN's
PARENTAL_PREFIX = "parental/"
STATE = PARENTAL_PREFIX + "state"
PIN = PARENTAL_PREFIX + "pin"  # Set PIN… / Change PIN…
REMOVE_PIN = PARENTAL_PREFIX + "remove_pin"
UNLOCK = PARENTAL_PREFIX + "unlock"
LOCK = PARENTAL_PREFIX + "lock"
ADULT = PARENTAL_PREFIX + "adult"
HIDDEN = PARENTAL_PREFIX + "hidden"
HIDDEN_PREFIX = HIDDEN + "/"  # + a hidden channel's or group's key, quoted

# The card's fields, and what it is for.
CURRENT = "current"
NEW = "new"
AGAIN = "again"
YOURS = "pin"
_SET, _CHANGE, _REMOVE, _UNLOCK, _CHECK = "set", "change", "remove", "unlock", "check"
_COLONS = " :：  "  # after the old window's labels ("Your PIN:")
_ELLIPSIS = "….  "  # after a row's title, as a card's ("Set PIN…" → "Set PIN")

OFF_TEXT = N_("Off: no PIN is set.")  # i18n: Settings → Parental controls: no PIN, none hidden
LOCKED_TEXT = N_("On: locked.")  # i18n: Settings → Parental controls
UNLOCKED_TEXT = N_("On: unlocked for now.")  # i18n: Settings → Parental controls
# i18n: Settings → Parental controls: the row (no PIN yet / one already set); opens a card
SET_PIN_TEXT = N_("Set PIN…")
CHANGE_PIN_TEXT = N_("Change PIN…")  # i18n: Settings → Parental controls; opens a card
REMOVE_PIN_TEXT = N_("Remove PIN…")  # i18n: Settings → Parental controls; opens a card
UNLOCK_TEXT = N_("Unlock for an hour…")  # i18n: Settings → Parental controls; opens a card
LOCK_TEXT = N_("Lock now")  # i18n: Settings → Parental controls
# i18n: Settings → Parental controls (2.8): a lasting rule, the groups a provider adds later too
ADULT_TEXT = N_("Hide groups whose names look adult")
# i18n: Settings → Parental controls: the list of what is hidden (a row; opens the list)
HIDDEN_TEXT = N_("Hidden while locked:")
# While locked, the list of what is hidden shows only once unlocked with the PIN.
# i18n: Settings → Parental controls, while locked: in place of the list of what is hidden
LOCKED_LIST_TEXT = N_("Unlock to see or change what is hidden.")
# i18n: Settings → Parental controls → Hidden while locked, with nothing hidden
NOTHING_HIDDEN_TEXT = N_("Nothing is hidden")
SHOW_AGAIN_TEXT = N_("Show again")  # i18n: Settings → Parental controls: a hidden one's action
CURRENT_PIN_TEXT = N_("Your current PIN:")  # i18n: asking for the PIN
NEW_PIN_TEXT = N_("New PIN")  # i18n: a field of Settings → Parental controls' PIN card
# i18n: what the empty New PIN field shows (Settings → Parental controls)
NEW_PIN_HINT = N_("4 digits or more")
AGAIN_TEXT = N_("The same PIN again:")  # i18n: confirming the PIN
YOUR_PIN_TEXT = N_("Your PIN:")  # i18n: asking for the PIN to unlock
# i18n: said in the PIN card: the PIN typed is too short or has letters
SHORT_TEXT = N_("A PIN is 4 digits or more.")
# i18n: said in the PIN card: the PIN was typed differently the second time
DIFFERENT_TEXT = N_("The PINs were not the same.")
# i18n: said in the PIN card: the PIN typed is not the parental controls' PIN
WRONG_TEXT = N_("That PIN is not right.")
# i18n: the title of the window that asks for the parental controls' PIN
PARENTAL_TITLE = N_("Parental controls")


def _digits(text: str) -> str:
    return "".join(c for c in text if c.isdecimal())


def _on_off(on: bool) -> str:
    return next(choice for choice in ON_OFF if choice.value is on).shown()


def ask_pin(window: Any, guard: Optional[Parental], then: Callable[[bool], None]) -> None:
    """Whether a change parental controls guard may go ahead (Settings › Providers: a provider
    renamed or removed, 2.8.12), answered through `then`: at once (True) with no PIN set or
    unlocked; else once the PIN is typed in the PIN's card over Settings (`window`'s
    parental_rows: it unlocks nothing; a wrong one is said in the card, which stays for another
    try), False when the card goes without it (Cancel, Esc, Back, Settings going)."""
    if guard is None or not guard.locked():
        then(True)
        return
    rows = getattr(window, "parental_rows", None)
    if rows is None:
        logger.warning("No card to ask the parental PIN in: the change is refused")
        then(False)
        return
    rows.check_pin(guard, then)


class ParentalRows(QObject):
    """What Parental controls lists (`rows`) and the handler of its rows (`parental/…`,
    registered with `settings`), with the card that asks for the PIN. `window` is the TV window
    (its `parental` the guard); `drawer` the Settings layer the rows show in."""

    def __init__(self, window: Any, settings: SettingsController, drawer: Any) -> None:
        super().__init__(drawer)
        self._window = window
        self._settings = settings
        self._drawer = drawer
        self._card: Optional[FormCard] = None
        self._for = ""  # what the card is for (_SET, _CHANGE, _REMOVE, _UNLOCK, _CHECK)
        self._title = ""  # and its title
        # _CHECK's: the guard the PIN is checked against, and what waits for the answer
        self._checking: Optional[tuple[Parental, Callable[[bool], None]]] = None
        settings.register(PARENTAL_PREFIX, self)

    @property
    def card(self) -> Optional[FormCard]:
        """The card that asks for the PIN (None until it first showed)."""
        return self._card

    def _guard(self) -> Parental:
        guard: Parental = self._window.parental
        return guard

    def _changed(self) -> None:
        """A change: the channel list, the guide and the Library show what may show now, and
        these rows follow (TVWindow.parental_changed builds them again)."""
        self._window.parental_changed()

    # ---- what the section shows -------------------------------------------------------------

    def rows(self) -> tuple[Row, ...]:
        guard = self._guard()
        has_pin = guard.has_pin()
        locked = has_pin and guard.locked()
        adult = guard.adult_rule()
        state = OFF_TEXT if not has_pin else LOCKED_TEXT if locked else UNLOCKED_TEXT
        rows = [
            # i18n: help under the state of Settings → Parental controls
            Row(STATE, state, N_("A PIN hides what you choose while QiTV is locked."), kind=INFO),
            Row(
                PIN,
                CHANGE_PIN_TEXT if has_pin else SET_PIN_TEXT,
                (
                    # i18n: help under Settings → Parental controls → Change PIN…
                    N_("Asks for the current PIN, then the new one twice.")
                    if has_pin
                    # i18n: help under Settings → Parental controls → Set PIN…
                    else N_("4 digits or more; then hide channels and groups from their menu.")
                ),
                kind=ACTION,
            ),
        ]
        if has_pin:
            rows.append(
                Row(
                    REMOVE_PIN,
                    REMOVE_PIN_TEXT,
                    # i18n: help under Settings → Parental controls → Remove PIN…
                    N_("Shows everything again and forgets what was hidden. Asks for the PIN."),
                    kind=DANGER,
                )
            )
        if locked:
            rows.append(
                Row(
                    UNLOCK,
                    UNLOCK_TEXT,
                    # i18n: help under Settings → Parental controls → Unlock for an hour…
                    N_("Shows what is hidden for an hour, with the PIN."),
                    kind=ACTION,
                )
            )
        elif has_pin:
            rows.append(
                Row(
                    LOCK,
                    LOCK_TEXT,
                    # i18n: help under Settings → Parental controls → Lock now
                    N_("Hides it again before the hour is up."),
                    kind=ACTION,
                )
            )
        rows.append(self._adult_row(has_pin, locked, adult))
        rows.append(self._hidden_row(locked))
        return tuple(rows)

    @staticmethod
    def _adult_row(has_pin: bool, locked: bool, adult: bool) -> Row:
        """A switch only with a PIN; while locked with it on, only what it is (turning it off
        needs the PIN)."""
        if not has_pin:
            # i18n: help under Settings → Parental controls → Hide groups … without a PIN
            return Row(ADULT, ADULT_TEXT, N_("Set a PIN first."), kind=INFO)
        if locked and adult:
            # i18n: help under Settings → Parental controls → Hide groups … while locked
            return Row(ADULT, ADULT_TEXT, N_("Unlock with the PIN to turn it off."), kind=INFO)
        return Row(
            ADULT,
            ADULT_TEXT,
            # "XXX", "Adult", "18+" as group names say it.
            # i18n: help under Settings → Parental controls → Hide groups whose names look adult
            N_("XXX, Adult, 18+…: the groups a provider adds later too."),
            ON_OFF,
            False,
        )

    def _hidden_row(self, locked: bool) -> Row:
        """Hidden while locked ›, or, while locked, a line that says to unlock first."""
        # i18n: help under Settings → Parental controls → Hidden while locked
        help = N_("The channels and groups hidden while locked, each shown again with a press.")
        if locked:
            return Row(HIDDEN, LOCKED_LIST_TEXT, help, kind=INFO)
        entries = [e for e in self._guard().hidden() if isinstance(e.get("key"), str) and e["key"]]
        title = translate(HIDDEN_TEXT).rstrip(_COLONS)
        layer = Section(
            HIDDEN,
            title,
            rows=tuple(self._hidden_entry(entry) for entry in entries),
            empty_text=NOTHING_HIDDEN_TEXT,
            literal=True,
        )
        return Row(
            HIDDEN,
            title,
            help,
            section=layer,
            note=number(len(entries)) if entries else "",
            literal=True,
        )

    @staticmethod
    def _hidden_entry(entry: dict) -> Row:
        """A hidden group or channel: its name as the provider gave it, the provider under it."""
        return Row(
            HIDDEN_PREFIX + quote(entry["key"], safe=""),
            str(entry.get("title") or entry["key"]),
            # i18n: help under a hidden one in Settings → Parental controls → Hidden while locked
            N_("Shows it in the lists again, at once."),
            kind=ACTION,
            note=translate(SHOW_AGAIN_TEXT),
            sub=provider_title(str(entry.get("provider") or "")),  # a link's: Network streams
            literal=True,
        )

    # ---- RowHandler -------------------------------------------------------------------------

    def value(self, row: Row) -> Any:
        if row.key == ADULT:
            on = self._guard().adult_rule()
            return on if row.kind == STEP else _on_off(on)
        return None

    def step(self, row: Row, delta: int) -> None:
        if row.key != ADULT or row.kind != STEP:
            return
        guard = self._guard()
        # Refused without a PIN, and turned off while locked (the rows offer neither).
        if guard.set_adult_rule(bool(stepped(row, guard.adult_rule(), delta))):
            self._changed()

    def edited(self, row: Row, text: str) -> None:
        pass

    def chosen(self, row: Row) -> None:
        guard = self._guard()
        has_pin = guard.has_pin()
        if row.key == PIN:
            self._ask(_CHANGE if has_pin else _SET, row.text)
        elif row.key == REMOVE_PIN and has_pin:
            self._ask(_REMOVE, row.text)
        elif row.key == UNLOCK and guard.locked():
            self._ask(_UNLOCK, row.text)
        elif row.key == LOCK:
            guard.lock()
            self._changed()
        elif row.key.startswith(HIDDEN_PREFIX):
            # Only while unlocked (with the PIN), so it can't be undone without it.
            if guard.show_again(unquote(row.key[len(HIDDEN_PREFIX) :])):
                self._changed()

    # ---- the PIN's card ---------------------------------------------------------------------

    def check_pin(self, guard: Parental, then: Callable[[bool], None]) -> None:
        """The PIN asked in the card, titled Parental controls, for a change `guard` guards
        (ask_pin): `then(True)` once the right one is typed (nothing unlocks), `then(False)`
        once the card goes without it."""
        self._ask(_CHECK, PARENTAL_TITLE)
        self._checking = (guard, then)

    def _ask(self, purpose: str, title: str) -> None:
        """The card for `purpose`, its fields empty, the keys on the first; `title` the row's."""
        if self._card is None:
            self._card = FormCard(self._drawer.parentWidget())
            self._card.pressed.connect(self._on_pressed)
            self._card.changed.connect(self._on_changed)
            self._card.cancelled.connect(self._forget)
        self._checked(False)  # a check still asked is over, unanswered
        self._for = purpose
        self._title = translate(title).rstrip(_ELLIPSIS)
        self._render({})
        self._settings.show_card(self._card)

    def _fields(self) -> list[tuple[str, str]]:
        """The card's fields for what it is for: (key, the old window's label)."""
        if self._for == _SET:
            return [(NEW, NEW_PIN_TEXT), (AGAIN, AGAIN_TEXT)]
        if self._for == _CHANGE:
            return [(CURRENT, CURRENT_PIN_TEXT), (NEW, NEW_PIN_TEXT), (AGAIN, AGAIN_TEXT)]
        if self._for == _REMOVE:
            return [(CURRENT, CURRENT_PIN_TEXT)]
        return [(YOURS, YOUR_PIN_TEXT)]

    def _render(self, values: dict[str, str], said: str = "") -> None:
        """The card with `values` in its fields (shown again: the focus and the carets stay),
        and `said` in the warning colour."""
        card = self._card
        if card is None:
            return
        fields = [
            Field(
                key,
                translate(label).rstrip(_COLONS),
                values.get(key, ""),
                secret=True,
                placeholder=translate(NEW_PIN_HINT) if key == NEW else "",
            )
            for key, label in self._fields()
        ]
        buttons = [
            Button(CANCEL, translate(CANCEL_TEXT)),
            Button(OK, translate(OK_TEXT), accent=True),
        ]
        card.set_form(self._title, fields, said, buttons, [])
        if said:
            card.set_found(said, False)

    def _on_changed(self, key: str, text: str) -> None:
        """Digits only: anything else typed (or pasted) is no part of a PIN."""
        card = self._card
        if card is None or _digits(text) == text:
            return
        values = card.values()
        values[key] = _digits(text)
        said, ok = card.found()
        self._render(values, "" if ok else said)

    def _refuse(self, text: str, *emptied: str) -> None:
        """`text` said in the card, the fields `emptied` cleared, the keys on the first."""
        card = self._card
        if card is None:
            return
        values = card.values()
        for key in emptied:
            values[key] = ""
        self._render(values, translate(text))
        card.focus_field(emptied[0])

    def _on_pressed(self, key: str) -> None:
        card = self._card
        if card is None or key != OK:
            return
        guard = self._guard()
        values = card.values()
        if self._for in (_SET, _CHANGE):
            if self._for == _CHANGE and not guard.check(values.get(CURRENT, "")):
                self._refuse(WRONG_TEXT, CURRENT)
                return
            pin = values.get(NEW, "")
            if len(pin) < MIN_DIGITS or not pin.isdigit():
                self._refuse(SHORT_TEXT, NEW, AGAIN)
                return
            if values.get(AGAIN, "") != pin:
                self._refuse(DIFFERENT_TEXT, NEW, AGAIN)
                return
            guard.set_pin(pin)
        elif self._for == _REMOVE:
            if not guard.clear_pin(values.get(CURRENT, "")):
                self._refuse(WRONG_TEXT, CURRENT)
                return
        elif self._for == _UNLOCK:
            if not guard.unlock(values.get(YOURS, "")):
                self._refuse(WRONG_TEXT, YOURS)
                return
        elif self._for == _CHECK and self._checking is not None:
            if not self._checking[0].check(values.get(YOURS, "")):
                self._refuse(WRONG_TEXT, YOURS)
                return
            card.hide()
            self._for = ""
            self._checked(True)  # nothing changed here: the change it guards goes on
            return
        else:
            return
        card.hide()
        self._forget()
        self._changed()

    def _forget(self) -> None:
        """The card went (done, or cancelled: Esc, Back, Settings going)."""
        self._for = ""
        self._checked(False)

    def _checked(self, right: bool) -> None:
        """A check's answer, once (none while none is asked)."""
        checking, self._checking = self._checking, None
        if checking is not None:
            checking[1](right)
