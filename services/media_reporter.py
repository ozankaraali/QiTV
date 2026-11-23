"""Telling a Jellyfin or Emby server where its title plays (2.8), and a Plex server (2.14,
through its timeline: services.plex), so its own "continue watching" and watched marks follow
what is watched in QiTV.

The shell says what plays (`playing(url)`: any stream; only a server's own is told), where it is
(`position`) and when it stops (`stopped`, or another stream playing). The server hears of a
start, of where it is every PROGRESS_S seconds, and of the stop with its last position (a title
stopped near its end is marked watched by the server). The same title opened again with another
stream (its quality changed: another play session, converted or not) is the old play's stop and
the new one's start. The requests go out on a thread of their own, one after another; a failure
is only logged.

A Plex server's conversion of a title (2.14, the ⚙'s) is ended on the server with its play's
stop, as a Jellyfin server ends its own when told the play stopped: so a conversion replaced (a
new quality, another title) or ended (Stop, the Library, the sleep timer, QiTV closing) stops
using the server's processor at once.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional

from services import media_server as ms, plex
from services.config_manager import get_app_version
from services.provider_types import is_plex

logger = logging.getLogger(__name__)

PROGRESS_S = 10.0
CLOSE_WAIT_S = 1.0  # QiTV closing waits at most this for the last requests (review M3)

Send = Callable[[dict, str, ms.StreamRef, int, bool], None]


def _send_over_http(
    server: dict, event: str, ref: ms.StreamRef, position: int, paused: bool
) -> None:
    if is_plex(server.get("type")):
        told = plex.PlexClient(
            ref.base,
            token=ref.token,
            client_id=str(server.get("device_id") or ""),
            version=get_app_version(),
            verify=plex.must_verify(ref.base, bool(server.get("ssl_verify", True))),
            timeout=10,
        )
        try:
            if event == "abandoned":  # a conversion that never played: its session ends
                told.stop_conversion(ref.transcode)
                return
            if event == "stopped" and ref.transcode:
                try:  # the conversion's session first (review N2: QiTV's close waits 1 s)
                    told.stop_conversion(ref.transcode)
                finally:  # stopped or not, where it stopped is told
                    told.report(event, ref, position, paused)
            else:
                told.report(event, ref, position, paused)
        finally:
            told.close()
        return
    client = ms.MediaServerClient(
        ref.base,
        str(server.get("type") or ms.JELLYFIN),
        device_id=ms.device_id_of(server),
        version=get_app_version(),
        token=ref.token,
        user_id=str(server.get("user_id") or ""),
        verify=bool(server.get("ssl_verify", True)),
        timeout=10,
    )
    try:
        client.report(event, ref, position, paused)
    finally:
        client.close()


class MediaReporter:
    """`servers()` lists the media server providers (their entries)."""

    def __init__(
        self,
        servers: Callable[[], list[dict]],
        send: Optional[Send] = None,
        clock: Callable[[], float] = time.monotonic,
        verify: Callable[[], bool] = lambda: True,
    ) -> None:
        """`verify()`: Settings' certificate check, for a server without its own."""
        self._servers = servers
        self._verify = verify
        self._clock = clock
        self._send_now = send  # tests: synchronous; else the reporting thread
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._active: Optional[tuple[dict, ms.StreamRef]] = None
        self._url = ""  # the active stream's address
        self._position = 0
        self._paused = False
        self._told = 0.0

    def _server_of(self, url: str) -> Optional[tuple[dict, ms.StreamRef]]:
        found = None
        for server in self._servers():
            kind = server.get("type")
            if is_plex(kind):  # at any of its addresses (one found again since: review N1)
                ref = plex.parse_stream_url(url, plex.bases_of(server))
            elif ms.is_media_server(kind):
                ref = ms.parse_stream_url(url, [str(server.get("url") or "")])
            else:
                continue
            if ref is not None:
                found = server, ref
                break
        if found is None:
            return None
        server, ref = found
        if "ssl_verify" not in server:
            server = dict(server, ssl_verify=self._verify())  # Settings' check (2.8)
        return server, ref

    def playing(self, url: str) -> None:
        """`url` plays now (whatever it is): a server's title starts; the one before stops."""
        found = self._server_of(url or "")
        if found is not None and self._active is not None:
            same, now = self._active[1], found[1]
            if (same.base, same.item_id, same.play_session, same.converted) == (
                now.base,
                now.item_id,
                now.play_session,
                now.converted,
            ):
                return  # the same stream opened again (a retry, a cast)
        self.stopped()
        if found is None:
            return
        self._active, self._position, self._paused = found, 0, False
        self._url = url
        self._told = self._clock()
        self._send(found[0], "start", found[1], 0, False)

    def active_url(self) -> str:
        """The address of the play the server heard start ("" for none)."""
        return self._url if self._active is not None else ""

    def position(self, position_ms: int, paused: bool = False, url: Optional[str] = None) -> None:
        """Where the title plays; `url`: only while that stream is the one the server heard
        start (a title swapped small, a phone's Watch: the large picture may play another)."""
        if self._active is None or (url is not None and url != self._url):
            return
        self._position, self._paused = max(0, int(position_ms)), paused
        if self._clock() - self._told >= PROGRESS_S:
            self._told = self._clock()
            self._send(self._active[0], "progress", self._active[1], self._position, paused)

    def stopped(self) -> None:
        if self._active is None:
            return
        server, ref = self._active
        self._active = None
        self._send(server, "stopped", ref, self._position, self._paused)

    def abandoned(self, url: str) -> None:
        """2.14 review I2: QiTV made the link `url` but will not play it (a newer choice came
        before it opened, or Stop): a Plex conversion's session the server started for it is
        stopped there. Another link is nothing to its server (it was never told)."""
        if not url or (self._active is not None and url == self._url):
            return
        found = self._server_of(url)
        if found is not None and found[1].transcode:
            self._send(found[0], "abandoned", found[1], 0, False)

    def shutdown(self) -> None:
        """QiTV closes: the stop is told, and QiTV waits for it CLOSE_WAIT_S at most (2.14
        review M3: a Plex conversion's session ends with it; the thread finishes what it
        has, then ends)."""
        self.stopped()
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(CLOSE_WAIT_S)

    def _send(self, server: dict, event: str, ref: ms.StreamRef, position: int, paused: bool):
        if self._send_now is not None:
            self._send_now(server, event, ref, position, paused)
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="media-reporter", daemon=True)
            self._thread.start()
        self._queue.put((server, event, ref, position, paused))

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                _send_over_http(*job)
            except Exception as e:  # never more than a log line
                logger.info(
                    "A media server did not take where its title plays: %s", type(e).__name__
                )
