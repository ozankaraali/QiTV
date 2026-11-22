"""The jobs that ask plex.tv and a Plex server (2.14), off the GUI thread as the media servers'
are (services.media_workers).

The sign-in is three one-shot jobs, so the GUI thread's timer paces the looks and nothing runs
on once the card is gone: a code (`PinJob`), one look at whether it was entered (`PinCheckJob`),
and the account's servers that answer, each by the best of its connections (`ServersJob`).
Each emits plain data (`finished`) or words to show (`error`), never an address or a token in
words or in the log.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal

from services import media_server as ms, plex
from services.config_manager import get_app_version
from services.i18n import N_, translate

logger = logging.getLogger(__name__)

# i18n: Plex's sign-in: plex.tv could not be reached ({reason}: what went wrong, in English)
PLEX_TV_FAILED = N_("plex.tv could not be reached ({reason}).")
# i18n: Plex's sign-in: plex.tv refused the account it had just signed in
PLEX_TV_REFUSED = N_("plex.tv refused the sign-in. Try again with a new code.")

Account = Callable[[str], Any]  # a client id -> a PlexAccount (tests: a fake)


def _account(client_id: str) -> plex.PlexAccount:
    return plex.PlexAccount(client_id, get_app_version())


def _plex_tv_failure(error: Exception) -> str:
    if isinstance(error, ms.SignInNeeded):
        return translate(PLEX_TV_REFUSED)
    reason = str(error) if isinstance(error, ms.MediaServerError) else type(error).__name__
    return translate(PLEX_TV_FAILED, reason=reason)


class _AccountJob(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, client_id: str, account: Optional[Account] = None) -> None:
        super().__init__()
        self.client_id = client_id
        self._account = account or _account

    def run(self) -> None:
        account = self._account(self.client_id)
        try:
            self.finished.emit(self.work(account))
        except Exception as e:
            logger.info("Plex's sign-in: plex.tv's answer failed: %s", type(e).__name__)
            self.error.emit(_plex_tv_failure(e))
        finally:
            account.close()

    def work(self, account: Any) -> Any:
        raise NotImplementedError


class PinJob(_AccountJob):
    """A new code: {id, code, expires_in}."""

    def work(self, account: Any) -> dict:
        pin = account.create_pin()
        return {"id": pin.id, "code": pin.code, "expires_in": pin.expires_in}


class PinCheckJob(_AccountJob):
    """One look at the code: {token} (the account's, "" while not entered yet), with
    `expired` once plex.tv no longer knows it."""

    def __init__(self, client_id: str, pin_id: int, account: Optional[Account] = None) -> None:
        super().__init__(client_id, account)
        self.pin_id = pin_id

    def work(self, account: Any) -> dict:
        try:
            return {"token": account.check_pin(self.pin_id)}
        except ms.SignInNeeded:
            return {"token": "", "expired": True}


class ServersJob(_AccountJob):
    """The account's servers that answer: {listed: how many it has, servers: [{name,
    machine_id, token (the server's own), owned, url (the best connection that answered)}]}."""

    def __init__(
        self,
        client_id: str,
        token: str,
        verify: bool,
        account: Optional[Account] = None,
        probe: Optional[Callable[[str, plex.Server], bool]] = None,
    ) -> None:
        super().__init__(client_id, account)
        self._token = token
        self.verify = verify
        self._probe = probe

    def _answers(self, uri: str, server: plex.Server) -> bool:
        return plex.answers(uri, server, self.client_id, get_app_version(), self.verify)

    def work(self, account: Any) -> dict:
        servers = account.servers(self._token)
        found = plex.reachable(servers, self._probe or self._answers)
        return {
            "listed": len(servers),
            "servers": [
                {
                    "name": server.name,
                    "machine_id": server.machine_id,
                    "token": server.access_token,
                    "owned": server.owned,
                    "url": uri,
                }
                for server, uri in found
            ],
        }
