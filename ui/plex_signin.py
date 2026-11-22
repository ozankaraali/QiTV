"""Plex's sign-in (2.14): a card over Settings, as the Add card is, never a system dialog.

"Plex" among the Add card's other ways (and Address and login on a Plex provider) opens it:

1. "Sign in to Plex" asks plex.tv for a code, then shows it large and spaced, with where to
   enter it (plex.tv/link, on any phone or computer) under it, readable from the couch. The
   card looks every LOOK_MS whether it was entered, one look at a time on a worker; a code
   that expired offers a new one.
2. Once entered, the account's servers that answer are found (services.plex_workers), each by
   the best of its connections (at home, then remote, then relayed). One is taken at once;
   several are a choice, a button each.
3. The server is added as a provider (its name, made unique), selected and said, as the Add
   card adds one. Signed in again from a Plex provider's card, the same server (by its id)
   takes the new access in place; another server of the account is added as a new provider.

The provider keeps the server's address and connections, its token and its id, and the client
id QiTV signed in as (`device_id`); for a server the account owns, that token is the account's
own (services.plex). Cancel (Esc, Back, the dimmed picture)
always works; nothing goes on once the card is gone, and a late answer is dropped. The keys,
the phone's buttons and the mouse reach the card as every Settings card
(SettingsController.show_card).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QWidget

from services import plex
from services.i18n import N_, translate
from services.provider_types import PLEX
from ui.overlays.settings_card import CANCEL, CANCEL_TEXT, Button, ChoiceCard
from ui.settings_controller import SettingsController
from ui.settings_providers import ADDED_TEXT, SAVE_FAILED_TEXT, ProviderRows

logger = logging.getLogger(__name__)

LOOK_MS = 2000  # between looks at whether the code was entered
RETRY, NEW_CODE = "retry", "code"  # the card's buttons (and CANCEL)
_SERVER = "server:"  # a server's button: its id after it

SIGN_IN_TITLE = N_("Sign in to Plex")  # i18n: the title of the card that signs in to Plex
ASKING_TEXT = N_("Asking plex.tv for a code…")  # i18n: Plex's sign-in card, before the code
# i18n: Plex's sign-in card, under its code (4 letters and digits, shown large above) and
# plex.tv/link (shown large too); plex.tv/link stays as it is
ENTER_TEXT = N_("Enter it at plex.tv/link, on any phone or computer. This card waits for it.")
EXPIRED_TEXT = N_("The code expired before it was entered.")  # i18n: Plex's sign-in card
NEW_CODE_TEXT = N_("New code")  # i18n: a button on Plex's sign-in card once a code expired
# i18n: Plex's sign-in card, once the code was entered
FINDING_TEXT = N_("Signed in. Looking for your Plex servers…")
CHOOSE_TITLE = N_("Choose a Plex server")  # i18n: the card listing a Plex account's servers
# i18n: under Choose a Plex server: the account has several servers, a button each
CHOOSE_TEXT = N_("Which of your Plex servers should QiTV add?")
# i18n: under Choose a Plex server, signing in again: {name} is the provider signed in again
OTHER_SERVER_TEXT = N_(
    "{name}'s server is not among them. The server chosen is added as a new provider."
)
# i18n: Plex's sign-in card: the account lists no server at all
NO_SERVER_TEXT = N_("Your Plex account has no server.")
# i18n: Plex's sign-in card: none of the account's servers answered
NONE_ANSWERED_TEXT = N_("None of your Plex servers answered. Check that one is on, then try again.")
TRY_AGAIN_TEXT = N_("Try again")  # i18n: a button on Plex's sign-in card after a failure

Clock = Callable[[], float]


class PlexSignIn(QObject):
    """Plex's sign-in cards over the picture under Settings (`drawer`'s parent). `window` is
    the TV window (its job runner, its toasts, its config); `rows` the Providers rows, whose
    changes the sign-in's are too (`ProviderRows.apply`)."""

    def __init__(
        self,
        window: Any,
        settings: SettingsController,
        drawer: QWidget,
        rows: ProviderRows,
        *,
        shown: Callable[[], None] = lambda: None,
        clock: Clock = time.monotonic,
    ) -> None:
        """`shown` runs each time the card shows (the Add card's owner raises the window's
        cards over it)."""
        super().__init__(drawer)
        self._window = window
        self._settings = settings
        self._rows = rows
        self._shown = shown
        self._clock = clock
        self.card = ChoiceCard(drawer.parentWidget())
        self.card.answered.connect(self._on_answered)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(LOOK_MS)
        self._timer.timeout.connect(self._look)
        settings.hidden.connect(self._reset)  # (its card cancelled already)
        self._request = 0
        self._reset()

    # ---- opening ------------------------------------------------------------------------------

    def start(self, editing: str = "", opened_settings: bool = False) -> None:
        """A sign-in: for a new provider, or (`editing`) provider `editing`'s again, as the
        client QiTV signed in as then. `opened_settings`: Settings opened for it, with no
        provider yet (its Cancel closes Settings too, as the Add card's does)."""
        self._reset()
        self._editing = editing
        self._alone = opened_settings
        provider = self._provider(editing) if editing else {}
        self._client_id = str(provider.get("device_id") or "") or plex.new_client_id()
        self._ask_code()

    def visible(self) -> bool:
        return not self.card.isHidden()

    # ---- the steps ----------------------------------------------------------------------------

    def _reset(self) -> None:
        """No sign-in: nothing kept, no answer awaited (one on its way is dropped)."""
        self._request += 1
        self._timer.stop()
        self._editing = ""
        self._alone = False
        self._client_id = ""
        self._pin: dict = {}
        self._ends = 0.0
        self._servers: list[dict] = []
        self._retry: Callable[[], None] = self._ask_code
        if not self.card.isHidden():
            self.card.hide()

    def _show(
        self, title: str, body: str, buttons: list[Button], default: str = "", code: str = ""
    ) -> None:
        """The card as it is now; a `code` shows large, with where to enter it."""
        cancel = Button(CANCEL, translate(CANCEL_TEXT))
        link = plex.LINK_PAGE if code else ""
        self.card.ask(
            translate(title),
            body,
            [cancel, *buttons],
            default,
            code=code,
            link=link,
            waits=bool(code),
        )
        self._settings.show_card(self.card)
        self._shown()

    def _run(
        self,
        job: QObject,
        answered: Callable[[Any], None],
        failed: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Run `job` on the window's runner; its answer goes to `answered` while it is still
        the one awaited, its failure to `failed` (the card says it, by default)."""
        self._request += 1
        request = self._request
        said = failed or self._failed

        def finished(context: int, payload: Any) -> None:
            if context == self._request and self.visible():
                answered(payload)

        def error(context: int, message: str) -> None:
            if context == self._request and self.visible():
                said(message)

        self._window.jobs.run_worker(job, finished, error, context=request)

    def _ask_code(self) -> None:
        from services.plex_workers import PinJob

        self._timer.stop()
        self._retry = self._ask_code
        self._show(SIGN_IN_TITLE, translate(ASKING_TEXT), [])
        self._run(PinJob(self._client_id), self._on_code)

    def _on_code(self, pin: dict) -> None:
        self._pin = dict(pin)
        self._ends = self._clock() + max(1, int(pin.get("expires_in") or plex.PIN_LIFETIME_S))
        code = str(pin.get("code") or "")
        self._show(SIGN_IN_TITLE, translate(ENTER_TEXT), [], CANCEL, code=code)
        self._timer.start()

    def _look(self) -> None:
        """One look at whether the code was entered (the next one LOOK_MS after its answer)."""
        from services.plex_workers import PinCheckJob

        self._timer.stop()  # (asked early: still one look at a time)
        if not self._pin or not self.visible():
            return
        if self._clock() >= self._ends:
            self._expired()
            return
        self._run(PinCheckJob(self._client_id, int(self._pin["id"])), self._on_look, self._missed)

    def _missed(self, _message: str) -> None:
        """A look that got no answer (a moment off the network): the next one in LOOK_MS."""
        self._timer.start()

    def _on_look(self, answer: dict) -> None:
        if answer.get("expired"):
            self._expired()
        elif answer.get("token"):
            self._find_servers(str(answer["token"]))
        else:
            self._timer.start()

    def _expired(self) -> None:
        self._pin = {}
        self._timer.stop()
        new = Button(NEW_CODE, translate(NEW_CODE_TEXT), accent=True)
        self._show(SIGN_IN_TITLE, translate(EXPIRED_TEXT), [new], NEW_CODE)

    def _find_servers(self, token: str) -> None:
        from services.plex_workers import ServersJob

        self._pin = {}
        self._retry = lambda: self._find_servers(token)
        self._show(SIGN_IN_TITLE, translate(FINDING_TEXT), [])
        self._run(ServersJob(self._client_id, token), self._on_servers)

    def _on_servers(self, answer: dict) -> None:
        if answer.get("refused"):  # plex.tv refused the account: only a new code helps (M8)
            from services.plex_workers import PLEX_TV_REFUSED

            self._retry = self._ask_code
            self._failed(translate(PLEX_TV_REFUSED))
            return
        servers = [s for s in answer.get("servers") or [] if s.get("machine_id")]
        servers.sort(key=lambda s: not s.get("owned"))  # the account's own first (review M9)
        if not servers:
            text = NONE_ANSWERED_TEXT if answer.get("listed") else NO_SERVER_TEXT
            self._failed(translate(text))  # (Try again: the servers again)
            return
        self._servers = servers
        own = self._provider(self._editing).get("server_id") if self._editing else None
        same = next((s for s in servers if own and s["machine_id"] == own), None)
        if same is not None:
            self._keep(same)
        elif len(servers) == 1 and not self._editing:
            self._keep(servers[0])
        else:
            buttons = [
                Button(_SERVER + s["machine_id"], s["name"] or s["machine_id"], accent=i == 0)
                for i, s in enumerate(servers)
            ]
            if self._editing:
                body = translate(OTHER_SERVER_TEXT, name=self._editing)
            else:
                body = translate(CHOOSE_TEXT)
            self._show(CHOOSE_TITLE, body, buttons, buttons[0].key)

    def _failed(self, message: str) -> None:
        self._timer.stop()
        retry = Button(RETRY, translate(TRY_AGAIN_TEXT), accent=True)
        self._show(SIGN_IN_TITLE, message, [retry], RETRY)

    # ---- the card's answers -------------------------------------------------------------------

    def _on_answered(self, key: str) -> None:
        if key == CANCEL:
            self._cancelled()
        elif key == RETRY:
            self._retry()
        elif key == NEW_CODE:
            self._ask_code()
        elif key.startswith(_SERVER):
            chosen = next((s for s in self._servers if _SERVER + s["machine_id"] == key), None)
            if chosen is not None:
                self._keep(chosen)

    def _cancelled(self) -> None:
        """Cancel: nothing kept. Settings opened for it with no provider yet closes too (the
        welcome card again)."""
        alone = self._alone
        self._reset()
        if alone and self._settings.visible() and not self._rows.actions.changes.names():
            self._settings.close()

    # ---- keeping ------------------------------------------------------------------------------

    def _keep(self, server: dict) -> None:
        """The server as a provider: the one signed in again when it is the same server, else
        a new one (selected and said). A list the disk refuses is said, and the card stays."""
        access = {
            "url": str(server["url"]),
            "token": str(server["token"]),
            "server_id": str(server["machine_id"]),
            "device_id": self._client_id,
            # Review I2: where else to look for the server when it isn't where it was.
            "connections": list(server.get("connections") or []),
        }
        editing = self._listed(str(server["machine_id"]))  # this server already: in place (M9)
        if editing:
            try:
                saved = self._rows.apply(
                    lambda: self._rows.actions.update(editing, access, refresh=True)
                )
            except KeyError:
                saved = True  # removed or renamed meanwhile: nothing to sign in again
            if not saved:
                self._failed(translate(SAVE_FAILED_TEXT))
                return
            self._reset()
            self._settings.refresh_sections()
            return
        entry = {"type": PLEX, "name": str(server["name"] or "Plex"), **access}
        added: list[str] = []
        if not self._rows.apply(lambda: added.append(self._rows.actions.add(entry))):
            self._failed(translate(SAVE_FAILED_TEXT))
            return
        self._reset()
        self._settings.refresh_sections()
        self._window.screens.notify(translate(ADDED_TEXT, names=added[0]))

    def _listed(self, server_id: str) -> str:
        """The provider that is already this server ("" for none): signed in again in place,
        never listed twice (a second device at plex.tv)."""
        changes = self._rows.actions.changes
        for name in changes.names():
            if self._provider(name).get("server_id") == server_id:
                return str(name)
        return ""

    def _provider(self, name: str) -> dict:
        try:
            return dict(self._rows.actions.changes.get(name))
        except KeyError:
            return {}
