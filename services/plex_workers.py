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

from services import media_server as ms, media_workers, plex, plex_addresses
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
        try:
            servers = account.servers(self._token)
        except ms.SignInNeeded:
            return {"refused": True}  # the card asks for a new code (review M8)
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
                    "connections": plex.kept_connections(server.connections),
                }
                for server, uri in found
            ],
        }


# ---- the server's lists and streams (as services.media_workers' for Jellyfin and Emby) -------


def client_for(provider: dict, verify: bool) -> plex.PlexClient:
    """The client of a Plex provider: its server where it was last reached
    (services.plex_addresses), its token, its client id (a plex.direct address's certificate
    always checked: plex.must_verify)."""
    url = plex_addresses.base_of(provider)
    return plex.PlexClient(
        url,
        token=str(provider.get("token") or ""),
        client_id=str(provider.get("device_id") or ""),
        version=get_app_version(),
        verify=plex.must_verify(url, verify),
    )


Finder = Callable[[dict], str]  # a provider -> the best of its connections that answers, or ""


def refind(provider: dict) -> str:
    """Where provider `provider`'s server answers now: the best of the connections it keeps
    (at home, remote, relayed), each asked who it is without the token (plex.answers); "" for
    none (or none kept)."""
    connections = plex.connections_of(provider)
    server_id = str(provider.get("server_id") or "")
    if not connections or not server_id:
        return ""
    server = plex.Server(
        str(provider.get("name") or ""),
        server_id,
        str(provider.get("token") or ""),
        connections=connections,
    )
    client_id, version = str(provider.get("device_id") or ""), get_app_version()
    found = plex.reachable([server], lambda uri, s: plex.answers(uri, s, client_id, version))
    return found[0][1] if found else ""


def _at_best(provider: dict) -> bool:
    connections = plex.connections_of(provider)
    return not connections or plex_addresses.base_of(provider) == connections[0].uri


def asked(
    provider: dict,
    verify: bool,
    work: Callable[[Any], Any],
    client: Optional[Callable] = None,
    finder: Optional[Finder] = None,
    best: bool = False,
) -> Any:
    """`work(client)` with provider `provider`'s server where it was last reached; not
    reached there, the server is looked for among its connections and asked again where it
    answers, from now on (review I2). `best`: looked for first when not at its best (a
    list's load: at home again, not through the relay)."""
    make, find = client or client_for, finder or refind
    server_id = str(provider.get("server_id") or "")
    if best and not _at_best(provider):
        found = find(provider)
        if found:
            plex_addresses.moved(server_id, found)
    server = make(provider, verify)
    try:
        try:
            return work(server)
        except Exception as error:
            if not plex.unreachable(error):
                raise
            found = find(provider)
            if not found or found == server.base:
                raise
            logger.info("A Plex server answers at another of its connections now")
            plex_addresses.moved(server_id, found)
            server.close()
            server = make(provider, verify)
            return work(server)
    finally:
        server.close()


class _ServerWorker(QObject):
    finished = Signal(object)
    error = Signal(str)
    best = False  # a list's load: its server looked for first when not at its best

    def __init__(
        self,
        provider: dict,
        verify: bool,
        client: Optional[Callable] = None,
        finder: Optional[Finder] = None,
    ) -> None:
        super().__init__()
        self.provider = dict(provider)
        self.verify = verify
        self._client = client
        self._finder = finder

    def run(self) -> None:
        try:
            self.finished.emit(
                asked(self.provider, self.verify, self.work, self._client, self._finder, self.best)
            )
        except _Shown as shown:
            self.error.emit(str(shown))
        except Exception as e:
            logger.warning("A Plex server's request failed: %s", media_workers.logged(e))
            self.error.emit(media_workers.failure(e))

    def work(self, client: Any) -> Any:
        raise NotImplementedError


class _Shown(Exception):
    """A message for the user as it is."""


class PlexSectionWorker(_ServerWorker):
    """The films ("vod") or series ("series") of every library that holds them, whole (a
    library comes at once: no delta, so a saved list's summary is not asked for)."""

    best = True

    def __init__(
        self,
        provider: dict,
        content_type: str,
        verify: bool,
        client: Optional[Callable] = None,
        *,
        saved: Optional[dict] = None,
        finder: Optional[Finder] = None,
    ) -> None:
        super().__init__(provider, verify, client, finder)
        self.content_type = content_type

    def work(self, client: Any) -> dict:
        kind = plex.SHOW if self.content_type == "series" else plex.MOVIE
        libraries = [lib for lib in client.libraries() if lib["type"] == kind]
        titles = {lib["id"]: client.items(lib["id"], kind) for lib in libraries}
        server_id = str(self.provider.get("server_id") or "")
        return plex.section_payload(
            libraries, titles, client.base, client.token, server_id=server_id
        )


class PlexSeriesWorker(_ServerWorker):
    """A series' seasons, each with its episodes."""

    def __init__(
        self,
        provider: dict,
        series_id: str,
        verify: bool,
        client: Optional[Callable] = None,
        finder: Optional[Finder] = None,
    ) -> None:
        super().__init__(provider, verify, client, finder)
        self.series_id = series_id

    def work(self, client: Any) -> dict:
        seasons, episodes = client.seasons_and_episodes(self.series_id)
        server_id = str(self.provider.get("server_id") or "")
        return plex.seasons_payload(
            seasons, episodes, client.base, client.token, server_id=server_id
        )


class PlexLinkWorker(_ServerWorker):
    """A title's stream: the version the quality menu chose, else the settings' pick, as its
    file is, or the server's conversion of it when the menu chose one (2.14, as Jellyfin's:
    services.media_workers.MediaLinkWorker). A conversion the server would not make plays
    the version as it is, and the answer says so (`refused`; its `quality`: the ⚙ ticks the
    file)."""

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
        finder: Optional[Finder] = None,
    ) -> None:
        super().__init__(provider, verify, client, finder)
        self.item_id = item_id
        self.screen_4k = screen_4k
        self.four_k = four_k
        self.quality = quality or ms.Quality()

    def work(self, client: Any) -> dict:
        sources = plex.sources(client.media(self.item_id))
        wanted = self.quality
        source = next((s for s in sources if wanted.source and s["Id"] == wanted.source), None)
        if source is None:  # the settings' pick (a version chosen before may be gone)
            source, why = ms.choose_source(sources, screen_4k=self.screen_4k, setting=self.four_k)
            if source is None:
                text = ms.ONLY_4K_TEXT if why == ms.ONLY_4K else media_workers.NO_VERSION_TEXT
                raise _Shown(translate(text))
        link, playing, refused = "", ms.Quality(source=source["Id"]), False
        if wanted.converted and not ms.within(source, wanted):
            link = self._converted(client, source, wanted)
            if link:  # the conversion, of the version it converts (the ⚙ offers those below)
                playing = ms.Quality(source["Id"], wanted.height, wanted.bitrate)
            refused = not link
        file = plex.stream_url(client.base, source, client.token, client.client_id, self.item_id)
        answer = {
            "link": link or file,
            "versions": [ms.version_of(s) for s in sources],
            "quality": playing,
        }
        if link:
            answer["file"] = file  # the history keeps it (review M5: the session's ends)
        if refused:
            answer["refused"] = True  # the conversion asked for: the TV says the server didn't
        return answer

    def _converted(self, client: Any, source: dict, wanted: ms.Quality) -> str:
        """The server's conversion's address, or "" (it would not: the file plays as it is)."""
        params = plex.conversion_params(
            self.item_id, source, wanted, client.client_id, client.version
        )
        try:
            return str(client.convert(params) or "")
        except ms.SignInNeeded:
            raise
        except Exception as error:  # never the address: it may carry the token (review N1)
            logger.info("A Plex server did not convert the title: %s", media_workers.logged(error))
            return ""


def check_access(
    provider: dict,
    verify: bool,
    client: Optional[Callable] = None,
    finder: Optional[Finder] = None,
) -> tuple[bool, str]:
    """Whether a Plex server answers its kept token (Settings' Check the connection), on the
    calling thread, looked for among its connections when not where it was: (True, "") or
    (False, why)."""
    try:
        asked(provider, verify, lambda server: server.libraries(), client, finder, best=True)
        return True, ""
    except ms.SignInNeeded:
        return False, translate(ms.SIGN_IN_AGAIN)
    except Exception as e:
        logger.info("A Plex server's kept sign-in was not answered: %s", media_workers.logged(e))
        return False, media_workers.failure(e)
