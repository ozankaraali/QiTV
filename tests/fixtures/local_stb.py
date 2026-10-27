"""A fake STB portal on 127.0.0.1 for the catch-up tests.

`StbFixture` answers what QiTV asks of a portal's `load.php` (also as `portal.php`), shaped as
such portals commonly answer:
- `stb`: `handshake` (a new token each time) and `get_profile`;
- `itv`: `get_genres`; `get_all_channels`, where channel 101 is marked archived (`tv_archive`
  "1", `tv_archive_duration` "168": hours) and channel 102 is not; `get_epg_info`, half-hour
  programmes from six hours ago to three hours ahead for each, ids from 5000 (101) and 6000
  (102), with `start_timestamp`/`stop_timestamp`, and channel 101's `mark_archive` 1 for the
  programmes that have ended, 0 for the others; `create_link` for a channel's live link;
- `tv_archive`: `create_link` with `cmd` "auto /media/<programme id>.mpg": "ffmpeg
  http://127.0.0.1:…/archive/<id>.ts?token=…" for a recorded programme of channel 101, else a
  `js` with an empty `cmd` and the `error` "nothing_to_play", as such portals answer when there
  is nothing to play (`errors` makes a programme's answer another error).
A request whose bearer token is not the last handshake's is refused as such portals refuse it
(a 200 whose body is the text "Authorization failed."); `expire()` makes the token stale.
`/archive/<id>.ts` and `/live/<id>.ts` answer a few MPEG-TS packets.

Everything is fictional; there are no credentials and no network beyond the loopback address.

    with StbFixture() as portal:
        provider = portal.provider()
"""

from __future__ import annotations

from datetime import datetime
from http.server import BaseHTTPRequestHandler
import json
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from services.live.server import ThreadingLoopbackHTTPServer

PROVIDER_NAME = "Fixture Portal"
MAC = "00:1A:79:00:00:01"
ARCHIVED_ID = "101"
PLAIN_ID = "102"
SLOT_S = 30 * 60
SLOTS_BEFORE = 12  # six hours of programmes before now
SLOTS_AFTER = 6  # three hours after
_MEDIA_PATH = re.compile(r"^/(archive|live)/(\d+)\.ts$")
_ARCHIVE_CMD = re.compile(r"^auto /media/(\d+)\.mpg$")
_TS = bytes([0x47, 0x40, 0x00, 0x10]) + bytes(184)  # one MPEG-TS packet (PID 0)


def channels() -> list[dict]:
    """The portal's live channels as `get_all_channels` lists them."""
    base = {"tv_genre_id": "1", "logo": "", "status": 1, "allow_pvr": "1"}
    return [
        dict(
            base,
            id=ARCHIVED_ID,
            name="Harbour News",
            number="1",
            cmd=f"ffrt http://localhost/ch/{ARCHIVED_ID}_",
            tv_archive="1",
            tv_archive_duration="168",
        ),
        dict(
            base,
            id=PLAIN_ID,
            name="Quiet Pier",
            number="2",
            cmd=f"ffrt http://localhost/ch/{PLAIN_ID}_",
            tv_archive="0",
            tv_archive_duration="0",
        ),
    ]


def _local(stamp: float) -> str:
    """A portal's programme time: local wall-clock time (QiTV names its time zone)."""
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")


class StbFixture:
    def __init__(self, now: Optional[float] = None) -> None:
        self.now = time.time() if now is None else now
        self.token = ""
        self.handshakes = 0
        # A programme's create_link answers this `js.error` instead ("": `{"js": []}`, as a
        # portal without an archive to ask answers).
        self.errors: dict[str, str] = {}
        self.requests: list[tuple[str, dict[str, str], str]] = []  # path, query, bearer
        self.play_links = False  # a channel's link as /play/live.php?…&stream=<its number>
        self.guide = self._guide()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                pass

            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                query = {key: values[0] for key, values in parse_qs(parts.query).items()}
                bearer = self.headers.get("Authorization", "")
                owner.requests.append((parts.path, query, bearer))
                media = _MEDIA_PATH.match(parts.path)
                if media is not None:
                    self._send(200, "video/mp2t", _TS * 64)
                elif parts.path in ("/portal.php", "/server/load.php"):
                    answer = owner.answer(query, bearer)
                    if answer is None:
                        self._send(200, "text/html", b"Authorization failed.")
                    else:
                        self._send(200, "application/json", json.dumps(answer).encode())
                else:
                    self._send(404, "text/plain", b"")

            def _send(self, status: int, kind: str, body: bytes) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # the client closed the connection

        self.server = ThreadingLoopbackHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    # ---- what it lists -----------------------------------------------------------------------

    def _guide(self) -> dict[str, list[dict]]:
        first = (int(self.now) // SLOT_S - SLOTS_BEFORE) * SLOT_S
        guide: dict[str, list[dict]] = {}
        for channel, ids in ((ARCHIVED_ID, 5000), (PLAIN_ID, 6000)):
            programmes = []
            for i in range(SLOTS_BEFORE + SLOTS_AFTER):
                start, stop = first + i * SLOT_S, first + (i + 1) * SLOT_S
                programme = {
                    "id": str(ids + i),
                    "ch_id": channel,
                    "name": f"Show {i}",
                    "descr": "",
                    "time": _local(start),
                    "time_to": _local(stop),
                    "start_timestamp": start,
                    "stop_timestamp": stop,
                    "duration": SLOT_S,
                }
                if channel == ARCHIVED_ID:
                    programme["mark_archive"] = 1 if stop <= self.now else 0
                programmes.append(programme)
            guide[channel] = programmes
        return guide

    def programme(self, channel: str, at: float) -> dict:
        """The guide's programme of `channel` on at `at` (Unix seconds)."""
        for programme in self.guide[channel]:
            if programme["start_timestamp"] <= at < programme["stop_timestamp"]:
                return programme
        raise LookupError(at)

    def recorded(self, programme_id: str) -> bool:
        return any(
            p["id"] == programme_id and p.get("mark_archive") == 1 for p in self.guide[ARCHIVED_ID]
        )

    def archive_link(self, programme_id: str) -> str:
        return f"{self.url}/archive/{programme_id}.ts?token=fixture"

    # ---- its answers -------------------------------------------------------------------------

    def answer(self, query: dict[str, str], bearer: str) -> Any:
        """The `load.php` answer to `query`; None: the session is refused."""
        kind, action = query.get("type"), query.get("action")
        if (kind, action) == ("stb", "handshake"):
            self.handshakes += 1
            self.token = f"token{self.handshakes}"
            return {"js": {"token": self.token}}
        if not self.token or bearer != f"Bearer {self.token}":
            return None
        if (kind, action) == ("stb", "get_profile"):
            return {"js": {"id": 1, "name": "Fixture box"}}
        if (kind, action) == ("itv", "get_genres"):
            return {"js": [{"id": "1", "title": "News"}]}
        if (kind, action) == ("itv", "get_all_channels"):
            return {"js": {"data": channels(), "total_items": 2, "max_page_items": 2}}
        if (kind, action) == ("itv", "get_epg_info"):
            return {"js": self.guide}
        if (kind, action) == ("itv", "create_link"):
            if self.play_links:  # as some portals answer: the channel's number in `stream`
                channel = re.sub(r"\D", "", query.get("cmd", ""))
                return {
                    "js": {
                        "cmd": f"ffmpeg {self.url}/play/live.php?mac=00:1A:79:00:00:01"
                        f"&stream={channel}&extension=ts&play_token=fixture"
                    }
                }
            channel = re.sub(r"\D", "", query.get("cmd", "")) or "0"
            return {"js": {"cmd": f"ffmpeg {self.url}/live/{channel}.ts?play_token=fixture"}}
        if (kind, action) == ("tv_archive", "create_link"):
            found = _ARCHIVE_CMD.match(query.get("cmd", ""))
            programme = found.group(1) if found else ""
            if programme in self.errors:
                error = self.errors[programme]
                return {"js": {"id": programme, "cmd": "", "error": error}} if error else {"js": []}
            if programme and self.recorded(programme):
                return {"js": {"id": programme, "cmd": f"ffmpeg {self.archive_link(programme)}"}}
            return {"js": {"id": programme, "cmd": "", "error": "nothing_to_play"}}
        return {"js": []}

    def expire(self) -> None:
        """The session's token is no longer accepted (until the next handshake)."""
        self.token = "expired"

    def asked(self, kind: str, action: str) -> list[tuple[dict[str, str], str]]:
        """The queries (and bearer headers) of the requests of `kind` and `action` so far."""
        return [
            (query, bearer)
            for _path, query, bearer in self.requests
            if query.get("type") == kind and query.get("action") == action
        ]

    def provider(self) -> dict:
        return {"type": "STB", "name": PROVIDER_NAME, "url": self.url, "mac": MAC}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(3)

    def __enter__(self) -> "StbFixture":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
