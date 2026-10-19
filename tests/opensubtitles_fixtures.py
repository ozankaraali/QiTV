"""A stand-in for `requests` that answers like OpenSubtitles' REST API (not a test module).

Every title, release and key here is made up.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Union
from urllib.parse import urlsplit

API = "https://api.opensubtitles.com/api/v1"
LINK = "https://dl.opensubtitles.example/file/1"  # a download's temporary link
SRT = b"1\n00:00:01,000 --> 00:00:03,000\nHello from the harbour\n"


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        json: Any = None,
        content: bytes = b"",
        headers: Optional[dict] = None,
    ) -> None:
        self.status_code = status
        self._json = json
        self.content = content
        self.headers = headers or {}

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("not JSON")
        return self._json


Answer = Union[FakeResponse, BaseException, Callable[[dict], FakeResponse]]


class FakeHttp:
    """Answers each (method, URL without its query) from a list, the last one repeating;
    `calls` keeps (method, url, the request's keywords)."""

    def __init__(self, routes: Optional[dict[tuple[str, str], list[Answer]]] = None) -> None:
        self.routes: dict[tuple[str, str], list[Answer]] = {}
        for key, answers in (routes or {}).items():
            self.routes[key] = list(answers)
        self.calls: list[tuple[str, str, dict]] = []

    def answer(self, method: str, url: str, *answers: Answer) -> None:
        self.routes[(method, url)] = list(answers)

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        parts = urlsplit(url)
        key = (method, f"{parts.scheme}://{parts.netloc}{parts.path}")
        answers = self.routes.get(key)
        if not answers:
            return FakeResponse(404, {"message": "not here"})
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer) and not isinstance(answer, FakeResponse):
            return answer(kwargs)
        return answer

    def made(self, method: str, path: str) -> list[dict]:
        """The keywords of each request made to `path` (under the API, or a whole URL)."""
        url = path if path.startswith("https://") else API + path
        return [kwargs for m, u, kwargs in self.calls if m == method and u == url]


def entry(
    file_id: int,
    language: str,
    release: str,
    *,
    downloads: int = 0,
    hearing_impaired: bool = False,
    machine: bool = False,
    ai: bool = False,
    trusted: bool = False,
    rank: str = "",
    file_name: str = "",
) -> dict:
    """One subtitle of a search's answer, as the API sends it."""
    return {
        "id": str(file_id * 10),
        "type": "subtitle",
        "attributes": {
            "language": language,
            "download_count": downloads,
            "hearing_impaired": hearing_impaired,
            "machine_translated": machine,
            "ai_translated": ai,
            "from_trusted": trusted,
            "release": release,
            "uploader": {"uploader_id": 1, "name": "someone", "rank": rank},
            "files": [{"file_id": file_id, "cd_number": 1, "file_name": file_name or release}],
        },
    }


def page(*entries: dict, total_pages: int = 1, number: int = 1) -> dict:
    return {
        "total_pages": total_pages,
        "total_count": len(entries),
        "per_page": 50,
        "page": number,
        "data": list(entries),
    }


def download_answer(name: str = "Harbour.Lights.2019.en.srt", remaining: int = 4) -> dict:
    return {
        "link": LINK,
        "file_name": name,
        "requests": 1,
        "remaining": remaining,
        "message": "Your quota will be renewed in 23 hours and 52 minutes",
        "reset_time": "23 hours and 52 minutes",
        "reset_time_utc": "2026-10-20T13:03:16.000Z",
    }


def quota_answer() -> FakeResponse:
    return FakeResponse(
        406,
        {
            "requests": 6,
            "remaining": -1,
            "message": "You have downloaded your allowed 5 subtitles for 24h.",
            "reset_time": "08 hours and 38 minutes",
            "reset_time_utc": "2026-10-20T06:00:00.000Z",
        },
    )


def login_answer(token: str = "token-1", base_url: str = "api.opensubtitles.com") -> dict:
    return {
        "user": {"allowed_downloads": 20, "level": "Sub leecher", "user_id": 7, "vip": False},
        "base_url": base_url,
        "token": token,
        "status": 200,
    }


def serving(http: FakeHttp, *entries: dict, name: str = "Harbour.Lights.2019.en.srt") -> None:
    """A search finding `entries`, and their download."""
    http.answer("GET", API + "/subtitles", FakeResponse(200, page(*entries)))
    http.answer("POST", API + "/download", FakeResponse(200, download_answer(name)))
    http.answer("GET", LINK, FakeResponse(200, content=SRT))
