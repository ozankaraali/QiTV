"""Export service: writes cached content, all live channels or complete STB series to M3U.

Replaces the export flows of the old UI mixin. The worker runs on a ``JobRunner`` thread;
error texts are fixed strings and logs go through ``safe_exc`` so URLs, MACs and
credentials never reach either.
"""

from __future__ import annotations

from enum import Enum
import logging
import threading
from typing import Any, Optional

from PySide6.QtCore import QObject, Signal
import requests

from services.export import save_m3u_content, save_stb_content
from services.provider_api import provider_prefers_https, stb_endpoint, stb_portal_base
from services.redact import safe_exc
from services.search import iter_items

logger = logging.getLogger(__name__)

SECTION_KEYS = {"live": "itv", "movies": "vod", "series": "series"}
REQUEST_TIMEOUT = (10, 30)

MSG_STB_SERIES_ONLY = (
    "Complete export is only available for STB series. Use “Cached content” instead."
)
MSG_LOAD_LIVE = "Load Live TV first."
MSG_NOTHING_LOADED = "Nothing is loaded in this section yet."
MSG_CANCELLED = "Export cancelled"
MSG_NO_EPISODES = "No episodes found to export."
MSG_UNREACHABLE = "Couldn\u2019t reach the provider to fetch episodes."
MSG_WRITE_FAILED = "Export failed: couldn\u2019t write the file"
POLL_SECONDS = 0.05


class ExportKind(str, Enum):
    CACHED = "cached"
    ALL_LIVE = "all_live"
    COMPLETE_SERIES = "complete_series"


def collect_items(content: dict, section: str) -> list[dict]:
    """The section's loaded items, de-duplicated by id (falling back to cmd, then name)."""
    key = SECTION_KEYS.get(section)
    if key is None or not isinstance(content, dict):
        return []
    items: list[dict] = []
    seen: set[str] = set()
    for item in iter_items(content.get(key)):
        ident = item.get("id")
        if ident in (None, ""):
            ident = item.get("cmd") or item.get("name")
        if ident not in (None, ""):
            marker = str(ident)
            if marker in seen:
                continue
            seen.add(marker)
        items.append(item)
    return items


def has_items(content: Optional[dict], section: str) -> bool:
    """Whether the section has a loaded item: stops at the first one (cheap on the GUI thread)."""
    key = SECTION_KEYS.get(section)
    if key is None or not isinstance(content, dict):
        return False
    return next(iter_items(content.get(key)), None) is not None


def export_available(
    kind: ExportKind, provider: dict, section: str, content: Optional[dict] = None
) -> Optional[str]:
    """None when the export is allowed, else the user-facing reason.

    ``content`` is the provider's loaded content (``current_provider_content``); the brief's
    three-argument form cannot see what is loaded, so it is an optional fourth argument. It
    runs on the GUI thread, so it only looks for a first item (the worker collects them).
    """
    if kind == ExportKind.COMPLETE_SERIES:
        if provider.get("type") != "STB" or section != "series":
            return MSG_STB_SERIES_ONLY
        return None
    if kind == ExportKind.ALL_LIVE:
        return None if has_items(content, "live") else MSG_LOAD_LIVE
    return None if has_items(content, section) else MSG_NOTHING_LOADED


def write_items(provider: dict, items: list[dict], path: str) -> int:
    """Write items to an M3U file at ``path``; returns the entries written; OSError if it fails."""
    if provider.get("type") == "STB":
        return save_stb_content(
            stb_portal_base(provider.get("url", "")), items, provider.get("mac", ""), path
        )
    return save_m3u_content(items, path)


class _Cancelled(Exception):
    pass


def _dicts(js: Optional[dict]) -> list[dict]:
    """The dict entries of a listing's ``data`` (none when missing or malformed)."""
    data = js.get("data") if isinstance(js, dict) else None
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


class ExportWorker(QObject):
    """Runs one export on a worker thread; ``finished`` carries the number of items written."""

    progress = Signal(int, int, str)  # done, total, label
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        kind: ExportKind,
        provider: dict,
        content: dict,
        section: str,
        path: str,
        *,
        verify_ssl: bool,
        prefer_https: bool,
        headers: dict,
        cancel: threading.Event,
    ):
        super().__init__()
        self._kind = kind
        self._provider = provider
        self._content = content
        self._section = section
        self._path = path
        self._verify_ssl = verify_ssl
        self._prefer_https = prefer_https
        self._headers = headers
        self._cancel = cancel

    def run(self) -> None:
        try:
            if self._kind == ExportKind.COMPLETE_SERIES:
                self._run_complete()
            else:
                self._run_simple()
        except _Cancelled:
            self.error.emit(MSG_CANCELLED)
        except OSError as e:
            logger.error("Export write failed: %s", safe_exc(e))
            self.error.emit(MSG_WRITE_FAILED)
        except Exception as e:
            logger.error("Export failed: %s", safe_exc(e))
            self.error.emit(f"Export failed: {type(e).__name__}")

    def _run_simple(self) -> None:
        section = "live" if self._kind == ExportKind.ALL_LIVE else self._section
        items = collect_items(self._content, section)
        self.progress.emit(1, 1, "Writing\u2026")
        self.finished.emit(write_items(self._provider, items, self._path))

    def _run_complete(self) -> None:
        reason = export_available(self._kind, self._provider, self._section)
        if reason:
            self.error.emit(reason)
            return
        series_list = collect_items(self._content, "series")
        total = len(series_list)
        episodes: list[dict] = []
        reached = 0
        with requests.Session() as session:
            for done, series in enumerate(series_list, start=1):
                self._check_cancel()
                name = series.get("name", "Unknown")
                self.progress.emit(done - 1, total, str(name))
                seasons = self._fetch(session, series, 0, "name")
                if seasons is not None:
                    reached += 1
                for season in _dicts(seasons):
                    self._check_cancel()
                    data = self._fetch(session, series, season.get("id"), "added")
                    for episode in _dicts(data):
                        episode["series_name"] = name
                        episode["season_name"] = season.get("name", "")
                        episodes.append(episode)
                self.progress.emit(done, total, str(name))
        self._check_cancel()
        if not episodes:
            self.error.emit(MSG_NO_EPISODES if reached or not total else MSG_UNREACHABLE)
            return
        count = write_items(self._provider, episodes, self._path)
        self.finished.emit(count)

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled

    def _fetch(
        self, session: requests.Session, series: dict, season_id: Any, sortby: str
    ) -> Optional[dict]:
        """One STB series listing (season_id 0 lists the seasons); None on failure.

        The request runs on a helper thread so cancelling returns at once even while the
        portal stalls; the abandoned request is aborted by closing the session.
        """
        base = stb_endpoint(
            stb_portal_base(
                self._provider.get("url", ""),
                provider_prefers_https(self._provider, self._prefer_https),
            )
        )
        params = {
            "type": "series",
            "action": "get_ordered_list",
            "category_id": series.get("category_id"),
            "movie_id": series.get("id"),
            "season_id": season_id,
            "sortby": sortby,
            "JsHttpRequest": "1-xml",
        }
        box: dict[str, Any] = {}
        done = threading.Event()

        def request() -> None:
            try:
                response = session.get(
                    base,
                    headers=self._headers,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                    verify=self._verify_ssl,
                )
                if response.status_code == 200:
                    js = response.json().get("js", {})
                    box["result"] = js if isinstance(js, dict) else None
                else:
                    logger.warning("Series listing returned HTTP %s", response.status_code)
            except (requests.RequestException, ValueError, AttributeError) as e:
                logger.warning("Series listing failed: %s", safe_exc(e))
            finally:
                done.set()

        threading.Thread(target=request, daemon=True).start()
        while not done.wait(POLL_SECONDS):
            if self._cancel.is_set():
                session.close()
                raise _Cancelled
        return box.get("result")
