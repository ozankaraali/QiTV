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


def export_available(
    kind: ExportKind, provider: dict, section: str, content: Optional[dict] = None
) -> Optional[str]:
    """None when the export is allowed, else the user-facing reason.

    ``content`` is the provider's loaded content (``current_provider_content``); the brief's
    three-argument form cannot see what is loaded, so it is an optional fourth argument.
    """
    if kind == ExportKind.COMPLETE_SERIES:
        if provider.get("type") != "STB" or section != "series":
            return MSG_STB_SERIES_ONLY
        return None
    if kind == ExportKind.ALL_LIVE:
        return None if collect_items(content or {}, "live") else MSG_LOAD_LIVE
    return None if collect_items(content or {}, section) else MSG_NOTHING_LOADED


def write_items(provider: dict, items: list[dict], path: str) -> int:
    """Write items to an M3U file at ``path``; returns the number of items given."""
    if provider.get("type") == "STB":
        save_stb_content(
            stb_portal_base(provider.get("url", "")), items, provider.get("mac", ""), path
        )
    else:
        save_m3u_content(items, path)
    return len(items)


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
        for done, series in enumerate(series_list, start=1):
            if self._cancel.is_set():
                self.error.emit(MSG_CANCELLED)
                return
            name = series.get("name", "Unknown")
            self.progress.emit(done - 1, total, str(name))
            seasons = self._fetch(series, 0, "name")
            for season in (seasons or {}).get("data", []):
                if self._cancel.is_set():
                    self.error.emit(MSG_CANCELLED)
                    return
                data = self._fetch(series, season.get("id"), "added")
                for episode in (data or {}).get("data", []):
                    episode["series_name"] = name
                    episode["season_name"] = season.get("name", "")
                    episodes.append(episode)
            self.progress.emit(done, total, str(name))
        if self._cancel.is_set():
            self.error.emit(MSG_CANCELLED)
            return
        if not episodes:
            self.error.emit(MSG_NO_EPISODES)
            return
        self.finished.emit(write_items(self._provider, episodes, self._path))

    def _fetch(self, series: dict, season_id: Any, sortby: str) -> Optional[dict]:
        """One STB series listing (season_id 0 lists the seasons); None on failure."""
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
        try:
            response = requests.get(
                base,
                headers=self._headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
                verify=self._verify_ssl,
            )
            if response.status_code == 200:
                js = response.json().get("js", {})
                return js if isinstance(js, dict) else None
            logger.warning("Series listing returned HTTP %s", response.status_code)
        except (requests.RequestException, ValueError) as e:
            logger.warning("Series listing failed: %s", safe_exc(e))
        return None
