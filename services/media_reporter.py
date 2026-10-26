"""Telling a Jellyfin or Emby server where its title plays (2.8), so its own "continue
watching" and watched marks follow what is watched in QiTV.

The shell says what plays (`playing(url)`: any stream; only a server's own is told), where it is
(`position`) and when it stops (`stopped`, or another stream playing). The server hears of a
start, of where it is every PROGRESS_S seconds, and of the stop with its last position (a title
stopped near its end is marked watched by the server). The requests go out on a thread of their
own, one after another; a failure is only logged.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional

from services import media_server as ms
from services.config_manager import get_app_version

logger = logging.getLogger(__name__)

PROGRESS_S = 10.0

Send = Callable[[dict, str, ms.StreamRef, int, bool], None]


def _send_over_http(
    server: dict, event: str, ref: ms.StreamRef, position: int, paused: bool
) -> None:
    client = ms.MediaServerClient(
        ref.base,
        str(server.get("type") or ms.JELLYFIN),
        device_id=str(server.get("device_id") or ""),
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
    ) -> None:
        self._servers = servers
        self._clock = clock
        self._send_now = send  # tests: synchronous; else the reporting thread
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._active: Optional[tuple[dict, ms.StreamRef]] = None
        self._position = 0
        self._paused = False
        self._told = 0.0

    def _server_of(self, url: str) -> Optional[tuple[dict, ms.StreamRef]]:
        servers = [s for s in self._servers() if ms.is_media_server(s.get("type"))]
        ref = ms.parse_stream_url(url, [str(s.get("url") or "") for s in servers])
        if ref is None:
            return None
        server = next(s for s in servers if ms.normalized_base(str(s.get("url") or "")) == ref.base)
        return server, ref

    def playing(self, url: str) -> None:
        """`url` plays now (whatever it is): a server's title starts; the one before stops."""
        found = self._server_of(url or "")
        if found is not None and self._active is not None:
            same = self._active[1]
            if (same.base, same.item_id) == (found[1].base, found[1].item_id):
                return  # the same title opened again
        self.stopped()
        if found is None:
            return
        self._active, self._position, self._paused = found, 0, False
        self._told = self._clock()
        self._send(found[0], "start", found[1], 0, False)

    def position(self, position_ms: int, paused: bool = False) -> None:
        if self._active is None:
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

    def shutdown(self) -> None:
        """QiTV closes: the stop is told (the thread finishes what it has, then ends)."""
        self.stopped()
        if self._thread is not None:
            self._queue.put(None)

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
