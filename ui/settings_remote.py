"""Settings › Phone remote in the drawer (plan 3): the old Settings window's Phone remote tab,
each change applied at once.

- Phone remote turns the remote on or off (RemoteController.apply_config starts, changes or
  stops it), and Reach QiTV from any network its relay; while the remote is off, the relay's
  row only says how it is set, as the old tab's box was greyed;
- Home address and Relay say where the remote is now, as the remote tells it (its
  statusChanged builds the rows again while Settings shows);
- Paired phones › lists the phones, when each was last used under it; one asks before it goes
  (RemoteController.remove_device: at once while the remote is off, else once its hub has
  removed it, devicesChanged building the rows again);
- Show the pairing code on the TV closes Settings, then shows it (TVWindow.open_phone_remote,
  which turns the remote on first, as the menu's Phone Remote… does).

The switches are written into the config's `remote` as the old tab's Save wrote them (its
version, the relay's address kept as set), the phones and the identity in it as they are.
Settings a newer QiTV stored are never written: under the old tab's note, every row only says
how it is. Keys: `remote/…`.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Optional
from urllib.parse import quote, unquote

from PySide6.QtCore import QObject

from services.i18n import N_, number, translate
from services.remote.protocol import RELAY_URL, STORE_VERSION
from services.settings_model import ACTION, INFO, ON_OFF, OPENS, STEP, Row, Section, step as stepped
from ui.overlays.settings_card import CANCEL, CANCEL_TEXT, Button, ChoiceCard
from ui.settings_controller import SettingsController

logger = logging.getLogger(__name__)

REMOTE_PREFIX = "remote/"
NEWER = REMOTE_PREFIX + "newer"
ENABLED = REMOTE_PREFIX + "enabled"
RELAY = REMOTE_PREFIX + "relay"
HOME = REMOTE_PREFIX + "home"
RELAY_STATE = REMOTE_PREFIX + "relay_state"
PHONES = REMOTE_PREFIX + "phones"
PHONE_PREFIX = PHONES + "/"  # + the phone's id, quoted
PAIR = REMOTE_PREFIX + "pair"
REMOVE = "remove"  # the key of the question's Remove
_FIELDS = {ENABLED: "enabled", RELAY: "relay"}  # a switch's row -> its key in `remote`

# i18n: Settings → Phone remote: the switch that turns the remote on
REMOTE_TEXT = N_("Phone remote")
RELAY_TEXT = N_("Reach QiTV from any network")  # i18n: Settings → Phone remote: the relay's
HOME_TEXT = N_("Home address")  # i18n: Settings → Phone remote: QiTV's address at home
RELAY_STATE_TEXT = N_("Relay")  # i18n: Settings → Phone remote: the relay's state now
PHONES_TEXT = N_("Paired phones")  # i18n: Settings → Phone remote: opens their list
# i18n: Settings → Phone remote: closes Settings and shows the pairing QR over the TV
PAIR_TEXT = N_("Show the pairing code on the TV")
# i18n: under a paired phone in Settings → Phone remote → Paired phones ({when}: a date)
LAST_USED_TEXT = N_("Last used {when}")
# i18n: Settings → Phone remote → Paired phones, with none paired yet
NO_PHONES_TEXT = N_("No paired phones")
# i18n: asked before Settings → Phone remote → Paired phones removes one ({name}: the phone)
REMOVE_QUESTION = N_("Remove the phone “{name}”?")
# i18n: under "Remove the phone …?": what it takes to use that phone again
REMOVE_BODY = N_("It can be paired again with the code on the TV.")
REMOVE_TEXT = N_("Remove")  # i18n: a button: unpairs the phone (verb)
# i18n: Settings → Phone remote: the settings were written by a newer QiTV
NEWER_SETTINGS = N_("These phone remote settings come from a newer QiTV; it keeps them.")
# The relay's state, as Settings → Phone remote says it.
RELAY_STATES = {
    "off": N_("Off"),  # i18n: the relay's state in Settings → Phone remote
    "connecting": N_("Connecting…"),  # i18n: the relay's state in Settings → Phone remote
    "online": N_("Connected"),  # i18n: the relay's state in Settings → Phone remote
    # i18n: the relay's state in Settings → Phone remote: it couldn't be reached, QiTV retries
    "away": N_("Can't connect; trying again"),
    # i18n: the relay's state in Settings → Phone remote
    "resting": N_("Resting until 00:00 UTC (its daily limit)"),
    # i18n: the relay's state: a copy of this QiTV's settings runs elsewhere
    "replaced": N_("Another QiTV is using this relay room"),
    # i18n: the phone remote's state: it couldn't start
    "error": N_("The phone remote couldn't start"),
}


def _known(data: dict) -> bool:
    """Whether the stored settings are this QiTV's (or none yet), not a newer one's."""
    return data.get("version") in (None, STORE_VERSION)


def _on_off(on: bool) -> str:
    return next(choice for choice in ON_OFF if choice.value is on).shown()


def _last_used(timestamp: object) -> str:
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return "—"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


class RemoteRows(QObject):
    """What Phone remote lists (`rows`) and the handler of its rows (`remote/…`, registered
    with `settings`). `window` is the TV window (its `remote` the RemoteController); `drawer`
    the Settings layer the rows show in."""

    def __init__(self, window: Any, settings: SettingsController, drawer: Any) -> None:
        super().__init__(drawer)
        self._window = window
        self._settings = settings
        self._drawer = drawer
        self._question: Optional[ChoiceCard] = None
        self._asked = ""  # the phone the question is about
        settings.register(REMOTE_PREFIX, self)
        window.remote.statusChanged.connect(self._follow)
        window.remote.devicesChanged.connect(self._follow)

    @property
    def question(self) -> Optional[ChoiceCard]:
        """The card that asks before a phone is removed (None until it first asked)."""
        return self._question

    def _data(self) -> dict:
        data = self._window.config_manager.remote
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _devices(data: dict) -> list[dict]:
        devices = data.get("devices")
        if not isinstance(devices, list):
            return []
        return [d for d in devices if isinstance(d, dict) and d.get("id")]

    def _follow(self, *_args: object) -> None:
        """The remote's state or its phones changed: the rows show it (built as Settings opens
        while it is hidden)."""
        if self._settings.visible():
            self._settings.refresh_sections()

    # ---- what the section shows -------------------------------------------------------------

    def rows(self) -> tuple[Row, ...]:
        data = self._data()
        known = _known(data)
        on = data.get("enabled") is True
        rows: list[Row] = []
        if not known:
            # i18n: help under the note that a newer QiTV stored the phone remote's settings
            rows.append(Row(NEWER, NEWER_SETTINGS, N_("Only that QiTV changes them."), kind=INFO))
        rows += [
            self._switch(
                ENABLED,
                REMOTE_TEXT,
                # i18n: help under Settings → Phone remote → Phone remote
                N_("Your phone's browser as a remote, with a guide and search, once paired."),
                False,
                info=not known,
            ),
            self._switch(
                RELAY,
                RELAY_TEXT,
                # i18n: help under Settings → Phone remote → Reach QiTV from any network
                N_("Through the QiTV relay, which passes on scrambled messages it can't read."),
                True,
                info=not known or not on,
            ),
            # i18n: help under Settings → Phone remote → Home address
            Row(HOME, HOME_TEXT, N_("Where phones on this network reach QiTV."), kind=INFO),
            Row(
                RELAY_STATE,
                RELAY_STATE_TEXT,
                # i18n: help under Settings → Phone remote → Relay (its state)
                N_("Whether phones on other networks reach QiTV through the relay now."),
                kind=INFO,
            ),
        ]
        # i18n: help under Settings → Phone remote → Paired phones
        phones_help = N_("The phones that can control QiTV; each can be removed.")
        # i18n: help under Settings → Phone remote → Show the pairing code on the TV
        pair_help = N_("A phone scans it once to become a remote.")
        if not known:
            rows += [
                Row(PHONES, PHONES_TEXT, phones_help, kind=INFO),
                Row(PAIR, PAIR_TEXT, pair_help, kind=INFO),
            ]
            return tuple(rows)
        devices = self._devices(data)
        phones = Section(
            PHONES,
            PHONES_TEXT,
            rows=tuple(self._phone_row(device) for device in devices),
            empty_text=NO_PHONES_TEXT,
        )
        rows += [
            Row(
                PHONES,
                PHONES_TEXT,
                phones_help,
                section=phones,
                note=number(len(devices)) if devices else "",
            ),
            Row(PAIR, PAIR_TEXT, pair_help, kind=ACTION),
        ]
        return tuple(rows)

    @staticmethod
    def _switch(key: str, text: str, help: str, default: bool, *, info: bool) -> Row:
        """An On/Off row, or one that only says it (the remote off, a newer QiTV's)."""
        if info:
            return Row(key, text, help, kind=INFO)
        return Row(key, text, help, ON_OFF, default)

    @staticmethod
    def _phone_row(device: dict) -> Row:
        """A paired phone: its name as it gave it, when it was last used under it."""
        return Row(
            PHONE_PREFIX + quote(str(device["id"]), safe=""),
            str(device.get("name") or "") or "—",
            # i18n: help under a paired phone in Settings → Phone remote → Paired phones
            N_("Asks before it removes this phone."),
            kind=OPENS,
            sub=translate(LAST_USED_TEXT, when=_last_used(device.get("last_used"))),
            literal=True,
        )

    # ---- RowHandler -------------------------------------------------------------------------

    def value(self, row: Row) -> Any:
        data = self._data()
        if row.key in _FIELDS:
            if row.key == ENABLED:
                on = data.get("enabled") is True
            else:
                on = data.get("relay", True) is not False
            return on if row.kind == STEP else _on_off(on)
        status = self._window.remote.status
        if row.key == HOME:
            return status.get("home_url") or "—"
        if row.key == RELAY_STATE:
            state = status.get("relay") or "off"
            return translate(RELAY_STATES.get(state, RELAY_STATES["off"]))
        if row.key == PHONES and row.kind == INFO:
            return number(len(self._devices(data)))
        return None

    def step(self, row: Row, delta: int) -> None:
        field = _FIELDS.get(row.key)
        if field is None or row.kind != STEP or not _known(self._data()):
            return  # a newer QiTV's settings are never written
        self._write(field, bool(stepped(row, self.value(row), delta)))

    def edited(self, row: Row, text: str) -> None:
        pass

    def chosen(self, row: Row) -> None:
        if not _known(self._data()) or row.kind == INFO:
            return
        if row.key == PAIR:
            self._settings.close()  # the code shows over the TV
            self._window.open_phone_remote()
        elif row.key.startswith(PHONE_PREFIX):
            self._ask(unquote(row.key[len(PHONE_PREFIX) :]), row.text)

    # ---- the changes ------------------------------------------------------------------------

    def _write(self, field: str, value: bool) -> None:
        """A switch written as the old tab's Save wrote them, and the remote following it."""
        config = self._window.config_manager
        data = dict(self._data())
        if not _known(data):
            return
        data["version"] = STORE_VERSION
        data["enabled"] = data.get("enabled") is True
        data["relay"] = data.get("relay", True) is not False
        data[field] = value
        data.setdefault("relay_url", RELAY_URL)  # set in config.json only (testing a relay)
        config.remote = data
        try:
            config.save_config()
        except Exception as e:  # only a config without its background writer raises
            logger.warning("Couldn't save the phone remote's settings: %s", type(e).__name__)
        self._window.remote.apply_config()  # started, changed or stopped
        self._settings.refresh_sections()  # the relay's row: a switch only while it is on

    def _ask(self, device_id: str, name: str) -> None:
        """Remove the phone…?: asked first, Cancel the answer the keys start on."""
        if self._question is None:
            self._question = ChoiceCard(self._drawer.parentWidget())
            self._question.answered.connect(self._on_answered)
        self._asked = device_id
        self._question.ask(
            translate(REMOVE_QUESTION, name=name),
            translate(REMOVE_BODY),
            [
                Button(CANCEL, translate(CANCEL_TEXT)),
                Button(REMOVE, translate(REMOVE_TEXT), danger=True),
            ],
            CANCEL,
        )
        self._settings.show_card(self._question)

    def _on_answered(self, key: str) -> None:
        device_id, self._asked = self._asked, ""
        if key == REMOVE and device_id and _known(self._data()):
            self._window.remote.remove_device(device_id)  # its devicesChanged: the rows follow
