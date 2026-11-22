"""The jobs that ask plex.tv and a Plex server (2.14), off the GUI thread as the media servers'
are (services.media_workers): the sign-in's, then a section's titles, a series' seasons and
episodes, a title's stream (`PlexSectionWorker`, `PlexSeriesWorker`, `PlexLinkWorker`, which the
catalog's WorkerFactory makes for a Plex provider) and Settings' Check the connection.

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

from services import media_server as ms, media_workers, plex
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
        account: Optional[Account] = None,
        probe: Optional[Callable[[str, plex.Server], bool]] = None,
    ) -> None:
        super().__init__(client_id, account)
        self._token = token
        self._probe = probe

    def _answers(self, uri: str, server: plex.Server) -> bool:
        return plex.answers(uri, server, self.client_id, get_app_version())

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


# ---- the server's lists and streams (as services.media_workers' for Jellyfin and Emby) -------


def client_for(provider: dict, verify: bool) -> plex.PlexClient:
    """The client of a Plex provider: its server's address, its token, its client id (a
    plex.direct address's certificate always checked: plex.must_verify)."""
    url = str(provider.get("url") or "")
    return plex.PlexClient(
        url,
        token=str(provider.get("token") or ""),
        client_id=str(provider.get("device_id") or ""),
        version=get_app_version(),
        verify=plex.must_verify(url, verify),
    )


class _ServerWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, provider: dict, verify: bool, client: Optional[Callable] = None) -> None:
        super().__init__()
        self.provider = dict(provider)
        self.verify = verify
        self._client = client or client_for

    def run(self) -> None:
        client = self._client(self.provider, self.verify)
        try:
            self.finished.emit(self.work(client))
        except _Shown as shown:
            self.error.emit(str(shown))
        except Exception as e:
            logger.warning("A Plex server's request failed: %s", media_workers.logged(e))
            self.error.emit(media_workers.failure(e))
        finally:
            client.close()

    def work(self, client: Any) -> Any:
        raise NotImplementedError


class _Shown(Exception):
    """A message for the user as it is."""


class PlexSectionWorker(_ServerWorker):
    """The films ("vod") or series ("series") of every library that holds them, whole (a
    library comes at once: no delta, so a saved list's summary is not asked for)."""

    def __init__(
        self,
        provider: dict,
        content_type: str,
        verify: bool,
        client: Optional[Callable] = None,
        *,
        saved: Optional[dict] = None,
    ) -> None:
        super().__init__(provider, verify, client)
        self.content_type = content_type

    def work(self, client: Any) -> dict:
        kind = plex.SHOW if self.content_type == "series" else plex.MOVIE
        libraries = [lib for lib in client.libraries() if lib["type"] == kind]
        titles = {lib["id"]: client.items(lib["id"], kind) for lib in libraries}
        return plex.section_payload(libraries, titles, client.base, client.token)


class PlexSeriesWorker(_ServerWorker):
    """A series' seasons, each with its episodes."""

    def __init__(
        self, provider: dict, series_id: str, verify: bool, client: Optional[Callable] = None
    ) -> None:
        super().__init__(provider, verify, client)
        self.series_id = series_id

    def work(self, client: Any) -> dict:
        seasons, episodes = client.seasons_and_episodes(self.series_id)
        return plex.seasons_payload(seasons, episodes, client.base, client.token)


class PlexLinkWorker(_ServerWorker):
    """A title's stream: the version the quality menu chose, else the settings' pick, as its
    file is. Plex is never asked to convert: a conversion asked for (one carried from another
    server's title) plays the settings' pick as it is, and the answer says there are none
    (`conversions`: the ⚙ menu lists the versions only)."""

    def __init__(
        self,
        provider: dict,
        item_id: str,
        verify: bool,
        *,
        screen_4k: bool,
        four_k: str,
        quality: Optional[ms.Quality] = None,
        client: Optional[Callable] = None,
    ) -> None:
        super().__init__(provider, verify, client)
        self.item_id = item_id
        self.screen_4k = screen_4k
        self.four_k = four_k
        self.quality = quality or ms.Quality()

    def work(self, client: Any) -> dict:
        sources = plex.sources(client.media(self.item_id))
        wanted = self.quality.source if not self.quality.converted else ""
        source = next((s for s in sources if wanted and s["Id"] == wanted), None)
        if source is None:
            source, why = ms.choose_source(sources, screen_4k=self.screen_4k, setting=self.four_k)
            if source is None:
                text = ms.ONLY_4K_TEXT if why == ms.ONLY_4K else media_workers.NO_VERSION_TEXT
                raise _Shown(translate(text))
        link = plex.stream_url(client.base, source, client.token, client.client_id, self.item_id)
        return {
            "link": link,
            "versions": [ms.version_of(s) for s in sources],
            "quality": ms.Quality(source=source["Id"]),
            "conversions": False,
        }


def check_access(
    provider: dict, verify: bool, client: Optional[Callable] = None
) -> tuple[bool, str]:
    """Whether a Plex server answers its kept token (Settings' Check the connection), on the
    calling thread: (True, "") or (False, why)."""
    server = (client or client_for)(provider, verify)
    try:
        server.libraries()
        return True, ""
    except ms.SignInNeeded:
        return False, translate(ms.SIGN_IN_AGAIN)
    except Exception as e:
        logger.info("A Plex server's kept sign-in was not answered: %s", media_workers.logged(e))
        return False, media_workers.failure(e)
    finally:
        server.close()
