"""The workers that ask a Jellyfin or Emby server (2.8), run off the GUI thread as the other
providers' workers are (`services.catalog.WorkerFactory`): a section's titles, a series' seasons
and episodes, a title's stream, and a sign-in (Settings → Verify).

Each emits plain data (`finished`) or a message to show (`error`), never an address or a token.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional
import uuid

from PySide6.QtCore import QObject, Signal

from services import media_server as ms
from services.config_manager import get_app_version
from services.i18n import N_, translate

logger = logging.getLogger(__name__)

# i18n: a Jellyfin or Emby title had no file to play
NO_VERSION_TEXT = N_("This title has no file to play.")
# i18n: a Jellyfin or Emby server could not be reached or answered with an error
SERVER_FAILED = N_("The server could not be reached ({reason}).")

MOVIE_LIBRARIES = ("movies", "mixed", "homevideos", "")
SERIES_LIBRARIES = ("tvshows", "mixed", "")


def client_for(provider: dict, verify: bool) -> ms.MediaServerClient:
    """The client of a server provider (its kept token and user)."""
    return ms.MediaServerClient(
        str(provider.get("url") or ""),
        str(provider.get("type") or ms.JELLYFIN),
        device_id=str(provider.get("device_id") or "") or ms.new_device_id(),
        version=get_app_version(),
        token=str(provider.get("token") or ""),
        user_id=str(provider.get("user_id") or ""),
        verify=verify,
    )


def _failure(error: Exception) -> str:
    if isinstance(error, ms.SignInNeeded):
        return translate(ms.SIGN_IN_AGAIN)
    reason = str(error) if isinstance(error, ms.MediaServerError) else type(error).__name__
    return translate(SERVER_FAILED, reason=reason)


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
        except Exception as e:
            logger.warning("A media server's request failed: %s", type(e).__name__)
            self.error.emit(_failure(e))
        finally:
            client.close()

    def work(self, client: ms.MediaServerClient) -> Any:
        raise NotImplementedError


class MediaSectionWorker(_ServerWorker):
    """The films ("vod") or series ("series") of every library that holds them."""

    def __init__(self, provider: dict, content_type: str, verify: bool, client=None) -> None:
        super().__init__(provider, verify, client)
        self.content_type = content_type

    def work(self, client: ms.MediaServerClient) -> dict:
        series = self.content_type == "series"
        wanted = SERIES_LIBRARIES if series else MOVIE_LIBRARIES
        libraries = [lib for lib in client.libraries() if lib["type"] in wanted]
        item_type = "Series" if series else "Movie"
        titles = {lib["id"]: client.items(item_type, lib["id"]) for lib in libraries}
        return ms.section_payload(libraries, titles, client.base)


class MediaSeriesWorker(_ServerWorker):
    """A series' seasons, each with its episodes."""

    def __init__(self, provider: dict, series_id: str, verify: bool, client=None) -> None:
        super().__init__(provider, verify, client)
        self.series_id = series_id

    def work(self, client: ms.MediaServerClient) -> dict:
        seasons, episodes = client.seasons_and_episodes(self.series_id)
        return ms.seasons_payload(seasons, episodes, client.base)


class MediaLinkWorker(_ServerWorker):
    """A title's stream: the version the settings pick, as its file is (or, over the bitrate
    cap, the server's conversion)."""

    def __init__(
        self,
        provider: dict,
        item_id: str,
        verify: bool,
        *,
        screen_4k: bool,
        four_k: str,
        max_bitrate: int,
        client=None,
    ) -> None:
        super().__init__(provider, verify, client)
        self.item_id = item_id
        self.screen_4k = screen_4k
        self.four_k = four_k
        self.max_bitrate = max_bitrate

    def work(self, client: ms.MediaServerClient) -> dict:
        sources = client.media_sources(self.item_id)
        source, why = ms.choose_source(sources, screen_4k=self.screen_4k, setting=self.four_k)
        if source is None:
            text = ms.ONLY_4K_TEXT if why == ms.ONLY_4K else NO_VERSION_TEXT
            raise _Shown(translate(text))
        session = uuid.uuid4().hex
        bitrate = int(source.get("Bitrate") or 0)
        if self.max_bitrate > 0 and bitrate > self.max_bitrate:
            link = ms.converted_url(
                client.base,
                self.item_id,
                source,
                client.token,
                session,
                self.max_bitrate,
                client.device_id,
            )
        else:
            link = ms.stream_url(client.base, self.item_id, source, client.token, session)
        return {"link": link}

    def run(self) -> None:
        client = self._client(self.provider, self.verify)
        try:
            self.finished.emit(self.work(client))
        except _Shown as shown:
            self.error.emit(str(shown))
        except Exception as e:
            logger.warning("A media server's stream failed: %s", type(e).__name__)
            self.error.emit(_failure(e))
        finally:
            client.close()


class _Shown(Exception):
    """A message for the user as it is."""


class MediaSignInWorker(QObject):
    """Settings → Verify: sign in with the password; `finished` gives {token, user_id,
    device_id, server, version} for the provider (the password is not kept)."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        url: str,
        kind: str,
        username: str,
        password: str,
        verify: bool,
        device_id: str = "",
        client=None,
    ) -> None:
        super().__init__()
        self.url, self.kind = url, kind
        self.username, self.password = username, password
        self.verify = verify
        self.device_id = device_id or ms.new_device_id()
        self._client = client

    def run(self) -> None:
        ok, value = sign_in(
            self.url,
            self.kind,
            self.username,
            self.password,
            self.verify,
            device_id=self.device_id,
            client=self._client,
        )
        if ok:
            self.finished.emit(value)
        else:
            self.error.emit(str(value))


def sign_in(
    url: str,
    kind: str,
    username: str,
    password: str,
    verify: bool,
    device_id: str = "",
    client=None,
) -> tuple[bool, Any]:
    """A server's sign-in with the password, on the calling thread (no Qt object: Settings'
    verification thread calls it): (True, {token, user_id, device_id}) or (False, why)."""
    device_id = device_id or ms.new_device_id()
    client = client or ms.MediaServerClient(
        url, kind, device_id=device_id, version=get_app_version(), verify=verify
    )
    try:
        token, user_id = client.sign_in(username, password)
        return True, {"token": token, "user_id": user_id, "device_id": device_id}
    except ms.SignInNeeded:
        # i18n: Settings → Verify: the server refused the user name or the password
        return False, translate(N_("The user name or the password is not right."))
    except Exception as e:
        logger.info("A media server's sign-in failed: %s", type(e).__name__)
        return False, _failure(e)
    finally:
        client.close()
