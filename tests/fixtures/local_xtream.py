"""A fake Xtream-shaped provider on 127.0.0.1 for the Library window tests and manual runs.

`XtreamFixture` answers the Xtream player API the catalog uses: 2 VOD categories with 3 movies
each, 1 series with 1 season of 2 episodes, no live channels and an empty guide. Every movie
and episode URL the catalog builds plays tests/fixtures/smoke.mp4, read from the file (with
byte ranges, so libVLC can seek). Titles are fictional; there are no credentials and no
network beyond the loopback address.

    with XtreamFixture() as xtream:
        providers = [..., xtream.provider()]
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
import threading
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from services.live.server import ThreadingLoopbackHTTPServer

FIXTURE_DIR = Path(__file__).resolve().parent
SMOKE_MP4 = FIXTURE_DIR / "smoke.mp4"
PROVIDER_NAME = "Fixture Xtream"
USERNAME = "demo"
PASSWORD = "demo"
VOD_CATEGORIES = [
    {"category_id": "11", "category_name": "Harbour Dramas"},
    {"category_id": "12", "category_name": "Lighthouse Comedies"},
]
MOVIE_TITLES = {
    "11": ["Northwind Crossing", "Quiet Pier", "Saltwater Letters"],
    "12": ["The Foghorn Affair", "Gull Patrol", "Tidewater Picnic"],
}
MOVIES = [
    {
        "num": index,
        "stream_id": 100 + index,
        "name": title,
        "category_id": category,
        "container_extension": "mp4",
        "stream_icon": "",
        "plot": f"A fictional film for QiTV's tests: {title}.",
        "releasedate": "2026",
    }
    for index, (category, title) in enumerate(
        ((c, t) for c, titles in MOVIE_TITLES.items() for t in titles), start=1
    )
]
SERIES_CATEGORIES = [{"category_id": "21", "category_name": "Coastal Mysteries"}]
SERIES = [
    {
        "num": 1,
        "series_id": 201,
        "name": "Lantern Street",
        "category_id": "21",
        "cover": "",
        "plot": "A fictional series for QiTV's tests.",
        "year": "2026",
    }
]
EPISODES = [
    {"id": 3001, "episode_num": 1, "title": "Arrival", "season": 1, "container_extension": "mp4"},
    {"id": 3002, "episode_num": 2, "title": "Low Tide", "season": 1, "container_extension": "mp4"},
]
SERIES_INFO = {
    "seasons": [{"season_number": 1, "name": "Season 1"}],
    "episodes": {"1": EPISODES},
    "info": {"name": "Lantern Street"},
}
_ACTIONS: dict[Optional[str], Any] = {
    None: {
        "user_info": {"auth": 1, "status": "Active", "allowed_output_formats": ["ts"]},
        "server_info": {},  # no advertised host: media URLs use the provider URL
    },
    "get_live_categories": [],
    "get_live_streams": [],
    "get_vod_categories": VOD_CATEGORIES,
    "get_vod_streams": MOVIES,
    "get_series_categories": SERIES_CATEGORIES,
    "get_series": SERIES,
}
EMPTY_GUIDE = b'<?xml version="1.0" encoding="UTF-8"?><tv></tv>'
_MEDIA = re.compile(r"^/(movie|series)/[^/]+/[^/]+/\d+\.mp4$")
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def movie_titles() -> list[str]:
    return [movie["name"] for movie in MOVIES]


class XtreamFixture:
    def __init__(self) -> None:
        self.requests: list[str] = []  # request paths, queries included
        media = SMOKE_MP4.read_bytes()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                pass

            def do_GET(self) -> None:
                owner.requests.append(self.path)
                parts = urlsplit(self.path)
                if parts.path == "/player_api.php":
                    self._json(owner.answer(parse_qs(parts.query)))
                elif parts.path == "/xmltv.php":
                    self._send(200, "application/xml", EMPTY_GUIDE)  # no programmes
                elif _MEDIA.match(parts.path):
                    self._media(media)
                else:
                    self._send(404, "text/plain", b"")

            def _json(self, value: Any) -> None:
                if value is None:
                    self._send(404, "application/json", b"{}")
                else:
                    self._send(200, "application/json", json.dumps(value).encode())

            def _media(self, body: bytes) -> None:
                match = _RANGE.fullmatch(self.headers.get("Range", ""))
                if match is None:
                    self._send(200, "video/mp4", body)
                    return
                first = int(match.group(1) or 0)
                last = int(match.group(2)) if match.group(2) else len(body) - 1
                last = min(last, len(body) - 1)
                headers = {"Content-Range": f"bytes {first}-{last}/{len(body)}"}
                self._send(206, "video/mp4", body[first : last + 1], headers)

            def _send(
                self, status: int, kind: str, body: bytes, headers: Optional[dict] = None
            ) -> None:
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Accept-Ranges", "bytes")
                    for name, value in (headers or {}).items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # the player closed the connection (a seek, a stop)

        self.server = ThreadingLoopbackHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    def answer(self, query: dict[str, list[str]]) -> Any:
        if query.get("username") != [USERNAME] or query.get("password") != [PASSWORD]:
            return None
        action = (query.get("action") or [None])[0]
        if action == "get_series_info":
            series = (query.get("series_id") or [""])[0]
            return SERIES_INFO if series == str(SERIES[0]["series_id"]) else {}
        return _ACTIONS.get(action)

    def provider(self) -> dict:
        return {
            "type": "XTREAM",
            "name": PROVIDER_NAME,
            "url": self.url,
            "username": USERNAME,
            "password": PASSWORD,
        }

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(3)

    def __enter__(self) -> "XtreamFixture":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
