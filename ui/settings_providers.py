"""Settings › Providers in the drawer (2.12): the providers, and each one's own rows.

Providers lists each provider by its name (as the viewer gave it: never translated), its kind
under it ("Xtream · in use" for the one the TV shows), then ＋ Add provider and Free channels
(services.settings_model). A provider opens a layer of its own:

- Name, edited in place: Enter or a click elsewhere renames it, never a keystroke; another
  provider's name, in any case, is refused and said, and the name stays;
- Kind, only shown: another kind is another provider;
- Address and login ›, the card that changes them after a check (`ProviderCards.edit`);
- Verify certificates, Streams at once and Guide time offset, each step written at once by the
  old Save's rules (services.provider_changes);
- Hidden groups ›, this provider's groups hidden to tidy the lists, each shown again at once;
- Check the connection, its last answer at its end (a check on a worker: ui.provider_check);
- Remove provider…, asked first in a card, its scheduled recordings said.

Renaming and removing ask the parental PIN while parental controls are locked, once per visit of
Settings. Each change runs the TV window's follow-ups (ui.provider_actions), and the rows are
built again once it is done; a rename keeps the viewer in the renamed provider's layer. A change
made elsewhere (a drop, Free channels, the old window, the TV moving to another provider) builds
them again at once (`providers_changed`), the place kept by key. A list that cannot be saved is
said, and the change holds until QiTV closes.

Keys: `provider/<name>` for a provider's layer and `provider/<name>/<field>` for its rows, the
name with "%", "/" and a leading "@" escaped (an unnamed provider is named after its address;
the list's own rows are `provider/@add` and `provider/@free`).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional, Protocol, TypeVar
from urllib.parse import quote, unquote, urlsplit

from PySide6.QtCore import QObject

from services.epg_view import (
    GUIDE_OFFSET_KEY,
    GUIDE_OFFSET_MAX,
    GUIDE_OFFSET_STEP,
    guide_offset_minutes,
)
from services.i18n import N_, tr, translate
from services.provider_changes import NameTaken
from services.provider_check import Outcome
from services.provider_types import EMBY, JELLYFIN
from services.settings_model import (
    ACTION,
    ADD_PROVIDER,
    DANGER,
    FREE_CHANNELS,
    INFO,
    ON_OFF,
    OPENS,
    PROVIDER_PREFIX,
    TEXT,
    Choice,
    Row,
    Section,
    step as next_choice,
)
from ui.hidden_groups_settings import INTRO as HIDDEN_INTRO, SECTIONS as GROUP_KINDS
from ui.overlays.settings_card import BUSY_TEXT, CANCEL, CANCEL_TEXT, Button, ChoiceCard
from ui.parental_settings import pin_allows
from ui.provider_actions import ProviderActions
from ui.provider_check import ProviderCheck
from ui.settings_controller import SettingsController
from ui.settings_dialog import (
    HTTP_ONLY_NOTE,
    NAME_TAKEN_TEXT,
    REMOVE_QUESTION,
    REMOVE_RECORDINGS_NOTE,
    offset_text,
)
from ui.tv_controller import ERROR_MS

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# A provider's rows, by the last part of their keys.
NAME, KIND, LOGIN, SSL, STREAMS, OFFSET, HIDDEN, CHECK, REMOVE = (
    "name",
    "kind",
    "login",
    "ssl",
    "streams",
    "offset",
    "hidden",
    "check",
    "remove",
)
# The stepper rows' provider keys (what ProviderChanges writes by the old Save's rules).
_STORED = {SSL: "ssl_verify", STREAMS: "max_streams", OFFSET: GUIDE_OFFSET_KEY}
STREAMS_MOST = 10  # as the old window's spin box

# A provider's kind as Settings names it (the old window's names; the products' as they are).
_KINDS = {
    "XTREAM": "Xtream",
    "M3UPLAYLIST": N_("M3U playlist"),  # i18n: a provider's kind
    "STB": N_("STB portal"),  # i18n: a provider's kind
    "M3USTREAM": N_("M3U Stream"),  # i18n: a provider's kind (one stream, from older QiTVs)
    JELLYFIN: "Jellyfin",
    EMBY: "Emby",
}
# i18n: under a provider's name in Settings → Providers: its kind ("Xtream"), and that the TV
# shows it now
IN_USE_TEXT = N_("{kind} · in use")
# i18n: the Max simultaneous streams setting's value for "as the provider reports"
AUTOMATIC_TEXT = N_("Automatic")
_STREAM_CHOICES = (Choice(0, AUTOMATIC_TEXT),) + tuple(
    Choice(n, str(n), literal=True) for n in range(1, STREAMS_MOST + 1)
)
SHOW_AGAIN_TEXT = N_("Show again")  # i18n: Settings → Providers → Hidden groups: a group's action
REMOVE_TEXT = N_("Remove")  # i18n: a button of "Remove the provider …?" (verb); its key: REMOVE


class ProviderCards(Protocol):
    """The cards over the picture that add a provider and change one's address and login
    (plan 2, Task 8)."""

    def add(self) -> None: ...

    def edit(self, name: str) -> None: ...


class WindowCards:
    """Until the drawer's own cards (plan 2, Tasks 8 and 9): ＋ Add provider and Address and
    login open the Settings window's provider form, as Add provider does today."""

    def __init__(self, window: Any) -> None:
        self._window = window

    def add(self) -> None:
        self._window.add_provider()

    def edit(self, name: str) -> None:
        self._window.options_dialog("providers")


def _escaped(name: str) -> str:
    """A name as a key's part: no "/" in it, and never one of the list's own rows ("@add")."""
    text = name.replace("%", "%25").replace("/", "%2F")
    return "%40" + text[1:] if text.startswith("@") else text


def layer_key(name: str) -> str:
    """Provider `name`'s row in Providers, and its layer's key."""
    return PROVIDER_PREFIX + _escaped(name)


def row_key(name: str, field: str) -> str:
    """Provider `name`'s row `field` (NAME, KIND …)."""
    return f"{layer_key(name)}/{field}"


def _parsed(key: str) -> Optional[tuple[str, str, str]]:
    """(provider name, field, what follows: a hidden group's key) of a provider's row key;
    None for a key of no provider (the list's own rows)."""
    if not key.startswith(PROVIDER_PREFIX) or key in (ADD_PROVIDER, FREE_CHANNELS):
        return None
    parts = key[len(PROVIDER_PREFIX) :].split("/", 2) + ["", ""]
    return unquote(parts[0]), parts[1], unquote(parts[2])


def kind_text(kind: Any) -> str:
    """A provider's kind as Settings names it ("Xtream", "M3U playlist"); a kind it does not
    know as it is stored."""
    text = _KINDS.get(str(kind or "").upper())
    return translate(text) if text is not None else str(kind or "")


def answer_text(outcome: Outcome) -> str:
    """A check's answer as the old window's Verify Provider said it."""
    why = outcome.why
    if outcome.ok:
        if why:
            # i18n: why is a warning, as "The portal says your subscription ended on 3 Oct 2026"
            text = tr("Provider verified successfully. Note: {why}.", why=why)
        else:
            text = tr("Provider verified successfully.")
        if outcome.http_only:  # the only place QiTV says so (2.7.1)
            text = f"{text} {translate(HTTP_ONLY_NOTE)}"
        return text
    if why:
        # i18n: why is the reason, as "The username or password is wrong"
        return tr("Failed to verify provider: {why}.", why=why)
    return tr("Failed to verify provider.")


def _where(provider: dict) -> str:
    """Where a provider is, without its login: a web address's host (and port), a playlist
    file's name."""
    url = str(provider.get("url") or "").strip()
    if "://" not in url:
        return os.path.basename(url)
    try:
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port
    except ValueError:
        return ""
    return f"{host}:{port}" if host and port else host


def _streams(value: Any) -> int:
    """A stream limit as the row shows it: 0 (Automatic) to STREAMS_MOST."""
    try:
        streams = int(value or 0)
    except TypeError, ValueError, OverflowError:
        return 0  # a hand-edited bad value means Automatic
    return max(0, min(STREAMS_MOST, streams))


def _offsets() -> tuple[Choice, ...]:
    """The guide time offsets, −12 h … +12 h in 30-minute steps, as the old window lists them."""
    return tuple(
        Choice(minutes, offset_text(minutes), literal=True)
        for minutes in range(-GUIDE_OFFSET_MAX, GUIDE_OFFSET_MAX + 1, GUIDE_OFFSET_STEP)
    )


class ProviderRows(QObject):
    """Settings' Providers rows (`rows`, for the sections) and the handler of their keys
    (`provider/…`, registered with `settings`). `window` is the TV window; `drawer` the
    Settings layer the rows show in; `cards` the Add and the address cards (the Settings
    window's form until plan 2's cards: WindowCards)."""

    def __init__(
        self,
        window: Any,
        settings: SettingsController,
        drawer: Any,
        cards: Optional[ProviderCards] = None,
    ) -> None:
        super().__init__(drawer)
        self._window = window
        self._settings = settings
        self._drawer = drawer
        self.cards: ProviderCards = cards if cards is not None else WindowCards(window)
        self._actions = ProviderActions(window)
        self._changing = False  # a change of ours runs: the rows are built once it is done
        self._pin_granted = False  # this visit of Settings
        self._notes: dict[str, str] = {}  # provider -> its last check's answer ("Checking…")
        self._checks: dict[ProviderCheck, str] = {}  # a check on its way -> its provider
        self._question: Optional[ChoiceCard] = None
        self._removing = ""  # the provider the question asks about
        settings.register(PROVIDER_PREFIX, self)
        settings.hidden.connect(self._on_settings_hidden)

    @property
    def question(self) -> Optional[ChoiceCard]:
        """The card that asks before a provider is removed (None until it first asked)."""
        return self._question

    # ---- the rows ---------------------------------------------------------------------------

    def rows(self) -> tuple[Row, ...]:
        """A row per provider, in the list's order, each opening its own layer."""
        selected = str(self._window.config_manager.selected_provider_name or "")
        offsets = _offsets()
        groups: dict[str, list[dict]] = {}
        tidy = getattr(self._window, "hidden_groups", None)
        for entry in tidy.entries() if tidy is not None else []:
            groups.setdefault(str(entry.get("provider") or ""), []).append(entry)
        rows = []
        for provider in self._providers():
            name = str(provider.get("name") or "")
            if not name:
                continue  # (hand-edited) nothing to know it by
            kind = kind_text(provider.get("type"))
            layer = Section(
                layer_key(name),
                name,
                rows=self._layer(name, provider, offsets, groups.get(name, [])),
                literal=True,
            )
            sub = translate(IN_USE_TEXT, kind=kind) if name == selected else kind
            rows.append(Row(layer_key(name), name, section=layer, sub=sub, literal=True))
        return tuple(rows)

    def _layer(
        self, name: str, provider: dict, offsets: tuple[Choice, ...], groups: list[dict]
    ) -> tuple[Row, ...]:
        hidden_key = row_key(name, HIDDEN)
        hidden = Section(
            hidden_key,
            N_("Hidden groups"),  # i18n: a row of a provider's Settings; opens its list
            rows=tuple(self._group_row(hidden_key, entry) for entry in groups),
            empty_text=N_("No hidden groups"),  # i18n: Settings → a provider → Hidden groups
        )
        return (
            Row(
                row_key(name, NAME),
                N_("Name"),  # i18n: a provider's name in its Settings, edited in place
                # i18n: help under a provider's Name in Settings
                N_("What QiTV lists it as. Enter keeps a new name, Esc puts the old one back."),
                kind=TEXT,
            ),
            Row(row_key(name, KIND), N_("Kind"), kind=INFO),  # i18n: a provider's kind (Xtream…)
            Row(
                row_key(name, LOGIN),
                N_("Address and login"),  # i18n: a row of a provider's Settings; opens a card
                # i18n: help under a provider's Address and login in Settings
                N_("Changed in a card, and checked before anything is kept."),
                kind=OPENS,
                note=_where(provider),
            ),
            Row(
                row_key(name, SSL),
                N_("Verify certificates"),  # i18n: a provider's setting (its HTTPS certificates)
                # i18n: help under a provider's Verify certificates in Settings
                N_("Whether QiTV checks this provider's HTTPS certificates."),
                ON_OFF,
                self._global_ssl(),
            ),
            Row(
                row_key(name, STREAMS),
                N_("Streams at once"),  # i18n: a provider's setting: how many streams it allows
                N_(
                    "How many streams this provider allows at once. Automatic uses what the"
                    " provider reports."
                ),
                _STREAM_CHOICES,
                0,
            ),
            Row(
                row_key(name, OFFSET),
                N_("Guide time offset"),  # i18n: a provider's setting (its guide's times)
                # i18n: help under a provider's Guide time offset in Settings
                N_("Moves this provider's guide when its times are off by whole hours."),
                offsets,
                0,
            ),
            Row(
                hidden_key,
                hidden.text,
                HIDDEN_INTRO,
                section=hidden,
                note=str(len(groups)) if groups else "",
            ),
            Row(
                row_key(name, CHECK),
                N_("Check the connection"),  # i18n: a provider's action: does it answer?
                # i18n: help under a provider's Check the connection in Settings
                N_("Asks the provider whether it answers, without opening a stream."),
                kind=ACTION,
                note=self._notes.get(name, ""),
            ),
            Row(
                row_key(name, REMOVE),
                N_("Remove provider"),  # i18n: a provider's action (it asks first)
                # i18n: help under a provider's Remove provider in Settings
                N_("Takes it off the list, with its scheduled recordings. Asked first."),
                kind=DANGER,
            ),
        )

    @staticmethod
    def _group_row(hidden_key: str, entry: dict) -> Row:
        """A hidden group: its title as the provider gave it, its kind under it."""
        kind = GROUP_KINDS.get(str(entry.get("section")))
        return Row(
            f"{hidden_key}/{quote(str(entry.get('key')), safe='')}",
            str(entry.get("title") or entry.get("key")),
            # i18n: help under a hidden group in a provider's Settings
            N_("Shows the group in the lists again, at once."),
            kind=ACTION,
            note=translate(SHOW_AGAIN_TEXT),
            sub=translate(kind) if kind else "",
            literal=True,
        )

    # ---- RowHandler -------------------------------------------------------------------------

    def value(self, row: Row) -> Any:
        parsed = _parsed(row.key)
        provider = self._find(parsed[0]) if parsed is not None else None
        if parsed is None or provider is None:
            return None
        name, field, _rest = parsed
        if field == NAME:
            return name
        if field == KIND:
            return kind_text(provider.get("type"))
        if field == SSL:
            return bool(provider.get("ssl_verify", self._global_ssl()))
        if field == STREAMS:
            return _streams(provider.get("max_streams"))
        if field == OFFSET:
            return guide_offset_minutes(provider)
        return None

    def step(self, row: Row, delta: int) -> None:
        parsed = _parsed(row.key)
        if parsed is None or parsed[1] not in _STORED or self._find(parsed[0]) is None:
            return
        name, field, _rest = parsed
        value = next_choice(row, self.value(row), delta)
        self._apply(lambda: self._actions.update(name, {_STORED[field]: value}))
        self._settings.refresh_sections()

    def edited(self, row: Row, text: str) -> None:
        parsed = _parsed(row.key)
        if parsed is not None and parsed[1] == NAME and self._find(parsed[0]) is not None:
            self._rename(parsed[0], text)

    def chosen(self, row: Row) -> None:
        if row.key == ADD_PROVIDER:
            self.cards.add()
            return
        if row.key == FREE_CHANNELS:  # the window's menu, under the row
            self._window.show_free_channels(self._drawer.row_anchor(row.key))
            return
        parsed = _parsed(row.key)
        if parsed is None or self._find(parsed[0]) is None:
            return
        name, field, rest = parsed
        if field == LOGIN:
            self.cards.edit(name)
        elif field == CHECK:
            self._check(name)
        elif field == REMOVE:
            self._ask_remove(name)
        elif field == HIDDEN and rest:
            self._show_again(rest)

    # ---- the changes ------------------------------------------------------------------------

    def providers_changed(self, *_args: object) -> None:
        """The provider list, or the provider the TV shows, changed (the catalogs' reload, a
        provider ready): the rows are built again, the place kept by key. One of our own
        changes builds them once it is done instead (a rename with its new keys)."""
        if not self._changing:
            self._settings.refresh_sections()

    def _apply(self, change: Callable[[], _T]) -> Optional[_T]:
        """Run one change of ours (ProviderActions'). The window's news of it, which comes
        inside it (the catalogs' providersChanged), waits for the caller's one build of the
        rows. A provider list that cannot be saved is said (None): the change holds in memory,
        and the window's follow-ups after the save did not run."""
        self._changing = True
        try:
            return change()
        except OSError as e:
            logger.warning("Couldn't save the provider list: %s", type(e).__name__)
            from ui.tv_window import SAVE_FAILED_TEXT  # (the window imports this module)

            self._notify(translate(SAVE_FAILED_TEXT))
            return None
        finally:
            self._changing = False

    def _rename(self, name: str, text: str) -> None:
        """Provider `name` renamed to `text` (blank: after its address), the PIN asked first
        while parental controls are locked; another provider's name is said and refused. The
        viewer stays in its layer, under the new name."""
        if not self._parental_allows():
            return
        index = self._actions.changes.names().index(name)
        try:
            self._apply(lambda: self._actions.rename(name, text))
        except NameTaken as taken:
            self._notify(translate(NAME_TAKEN_TEXT, name=taken.name))
            return
        names = self._actions.changes.names()
        new = names[index] if index < len(names) else name  # (its save may have failed)
        if new == name:
            self._settings.refresh_sections()
            return
        if name in self._notes:
            self._notes[new] = self._notes.pop(name)
        self._checks = {check: new if n == name else n for check, n in self._checks.items()}
        self._settings.refresh_sections(renamed={layer_key(name): layer_key(new)})

    def _show_again(self, group: str) -> None:
        """A hidden group back in the lists, at once (as the old box's Show again)."""
        tidy = getattr(self._window, "hidden_groups", None)
        if tidy is not None and tidy.show_again(group):
            self._window.controller.lists_changed()
        self._settings.refresh_sections()

    def _check(self, name: str) -> None:
        """Check the connection: on a worker; its answer is noted at the row's end."""
        if name in self._checks.values():
            return  # on its way already
        check = ProviderCheck(runner=self._window.jobs, parent=self)
        check.answered.connect(lambda _id, outcome, c=check: self._answered(c, outcome))
        self._checks[check] = name  # (a runner may answer inside start)
        self._notes[name] = translate(BUSY_TEXT)
        self._settings.refresh_sections()
        check.start(dict(self._find(name) or {}), self._global_ssl())

    def _answered(self, check: ProviderCheck, outcome: Outcome) -> None:
        name = self._checks.pop(check, None)
        check.deleteLater()
        if name is None or self._find(name) is None:
            return  # its provider went meanwhile
        self._notes[name] = answer_text(outcome)
        if outcome.access:  # a sign-in's (a media server's): kept in place of a password
            self._apply(lambda: self._actions.update(name, dict(outcome.access)))
        self._settings.refresh_sections()

    def _ask_remove(self, name: str) -> None:
        """Remove provider…: the PIN while locked, then asked in a card (Cancel first)."""
        if not self._parental_allows():
            return
        schedule = getattr(self._window, "recording_schedule", None)
        scheduled = schedule is not None and name in schedule.providers()
        if self._question is None:
            self._question = ChoiceCard(self._drawer.parentWidget())
            self._question.answered.connect(self._on_remove_answered)
        self._removing = name
        self._question.ask(
            translate(REMOVE_QUESTION, name=name),
            translate(REMOVE_RECORDINGS_NOTE) if scheduled else "",
            [
                Button(CANCEL, translate(CANCEL_TEXT)),
                Button(REMOVE, translate(REMOVE_TEXT), danger=True),
            ],
            CANCEL,
        )
        self._settings.show_card(self._question)

    def _on_remove_answered(self, key: str) -> None:
        name, self._removing = self._removing, ""
        if key != REMOVE or not name or self._find(name) is None:
            return
        self._notes.pop(name, None)
        self._checks = {check: n for check, n in self._checks.items() if n != name}
        self._apply(lambda: self._actions.remove(name))
        self._settings.refresh_sections()

    # ---- what they need ---------------------------------------------------------------------

    def _parental_allows(self) -> bool:
        """Renaming or removing while parental controls are locked asks the PIN, once per
        visit of Settings (what they hide is kept by provider name)."""
        guard = getattr(self._window, "parental", None)
        if guard is None or not guard.locked() or self._pin_granted:
            return True
        self._pin_granted = pin_allows(self._drawer.window(), guard)
        return self._pin_granted

    def _on_settings_hidden(self) -> None:
        self._pin_granted = False

    def _notify(self, text: str) -> None:
        self._window.screens.notify(text, "error", ERROR_MS)

    def _global_ssl(self) -> bool:
        return bool(self._window.config_manager.ssl_verify)

    def _providers(self) -> list[dict]:
        return [p for p in self._window.provider_manager.providers if isinstance(p, dict)]

    def _find(self, name: str) -> Optional[dict]:
        """Provider `name`'s entry as listed (read only); None when none has the name."""
        return next((p for p in self._providers() if p.get("name") == name), None)
