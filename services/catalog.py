"""Provider catalog for the TV shell: providers, sections, groups and items (spec §4.1).

All network work runs through a JobRunner; every job is tagged with the provider
generation so results that arrive after a provider switch are dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.epg_view import Programme, programmes_for_channel
from services.jobs import JobRunner
from services.provider_api import provider_prefers_https, stb_endpoint, stb_portal_base

logger = logging.getLogger(__name__)

SECTION_CONTENT_TYPES = {"live": "itv", "movies": "vod", "series": "series"}
ALL_GROUP_ID = "*"


@dataclass(frozen=True)
class Group:
    section: str
    id: str
    title: str
    count: Optional[int] = None  # None: an STB group that has not been fetched yet


class WorkerFactory:
    """Creates the real workers; tests substitute a fake with the same method names."""

    def provider_setup(self, provider_manager, force_refresh):
        from workers import ProviderSetupWorker

        return ProviderSetupWorker(provider_manager, force_refresh)

    def epg(self, epg_manager, force_refresh):
        from workers import EpgLoadWorker

        return EpgLoadWorker(epg_manager, force_refresh)

    def m3u(self, url, verify_ssl, prefer_https):
        from workers import M3ULoaderWorker

        return M3ULoaderWorker(url, verify_ssl=verify_ssl, prefer_https=prefer_https)

    def xtream(self, base_url, username, password, content_type, verify_ssl, prefer_https):
        from workers import XtreamLoaderWorker

        return XtreamLoaderWorker(
            base_url,
            username,
            password,
            content_type,
            verify_ssl=verify_ssl,
            prefer_https=prefer_https,
        )

    def stb_categories(self, url, headers, content_type, verify_ssl, prefer_https):
        from workers import STBCategoriesWorker

        return STBCategoriesWorker(
            url, headers, content_type, verify_ssl=verify_ssl, prefer_https=prefer_https
        )

    def stb_list(self, **loader_kwargs):
        from workers import StbListWorker

        return StbListWorker(**loader_kwargs)

    def xtream_series(self, **kwargs):
        from workers import XtreamSeriesInfoWorker

        return XtreamSeriesInfoWorker(**kwargs)

    def link(self, **kwargs):
        from workers import LinkCreatorWorker

        return LinkCreatorWorker(**kwargs)


class CatalogService(QObject):
    providerReady = Signal(str)
    providerFailed = Signal(str, str)
    sectionReady = Signal(str)
    sectionFailed = Signal(str, str)
    groupReady = Signal(str, str)
    groupFailed = Signal(str, str, str)
    epgReady = Signal()

    def __init__(
        self,
        config_manager,
        provider_manager,
        epg_manager,
        runner=None,
        factory: Optional[WorkerFactory] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.config = config_manager
        self.provider_manager = provider_manager
        self.epg_manager = epg_manager
        self.runner = runner if runner is not None else JobRunner(self)
        self.factory = factory or WorkerFactory()
        self._generation = 0
        self._epg_ready = False
        self._jobs: dict[QObject, tuple[str, int, Any]] = {}

    # --- providers -------------------------------------------------------------------
    def provider_names(self) -> list[str]:
        return [p.get("name", "") for p in self.provider_manager.providers]

    def current_provider(self) -> dict:
        return self.provider_manager.current_provider or {}

    def current_provider_name(self) -> str:
        return str(self.current_provider().get("name", ""))

    def provider_type(self) -> str:
        return str(self.current_provider().get("type", "")).upper()

    def select_provider(self, name: Optional[str] = None, *, force_refresh: bool = False) -> None:
        if name is not None and name != self.config.selected_provider_name:
            self.config.selected_provider_name = name
            self.config.save_config()
        self._generation += 1
        self._epg_ready = False
        worker = self.factory.provider_setup(self.provider_manager, force_refresh)
        self._start(
            worker, "provider", force_refresh, self._on_provider_ready, self._on_provider_error
        )

    def _on_provider_ready(self, payload) -> None:
        job = self._take_job()
        if job is None:
            return
        force_refresh = job[2]
        self.providerReady.emit(self.current_provider_name())
        worker = self.factory.epg(self.epg_manager, force_refresh)
        self._start(worker, "epg", None, self._on_epg_ready, self._on_epg_error)

    def _on_provider_error(self, message: str) -> None:
        if self._take_job() is not None:
            self.providerFailed.emit(self.current_provider_name(), message)

    def _on_epg_ready(self, payload) -> None:
        if self._take_job() is not None:
            self._epg_ready = True
            self.epgReady.emit()

    def _on_epg_error(self, message: str) -> None:
        if self._take_job() is not None:
            logger.info("EPG unavailable: %s", message)

    def epg_ready(self) -> bool:
        return self._epg_ready

    def programmes(self, channel: dict, now: datetime, limit: int = 8) -> list[Programme]:
        if not self._epg_ready or not self.config.channel_epg:
            return []
        return programmes_for_channel(self.epg_manager, channel, now, limit)

    def max_connections(self) -> Optional[int]:
        for ct in SECTION_CONTENT_TYPES.values():
            content = self.provider_manager.current_provider_content.get(ct)
            if isinstance(content, dict) and content.get("max_connections"):
                return int(content["max_connections"])
        return None

    # --- sections --------------------------------------------------------------------
    def sections(self) -> list[str]:
        if self.provider_type() in ("STB", "XTREAM"):
            return ["live", "movies", "series"]
        return ["live"]

    def is_live(self, section: str) -> Optional[bool]:
        if self.provider_type() in ("STB", "XTREAM"):
            return section == "live"
        return None  # M3U entries may be live or on demand

    def request_section(self, section: str, *, force: bool = False) -> None:
        ct = SECTION_CONTENT_TYPES[section]
        content = self.provider_manager.current_provider_content.get(ct)
        if not force and self._usable(content) and not self.provider_manager.is_content_stale(ct):
            QTimer.singleShot(0, lambda: self.sectionReady.emit(section))
            return
        provider, ptype = self.current_provider(), self.provider_type()
        verify = provider.get("ssl_verify", self.config.ssl_verify)
        https = provider_prefers_https(provider, self.config.prefer_https)
        if ptype == "M3USTREAM":
            item = {
                "id": 1,
                "name": provider.get("name") or "Stream",
                "cmd": provider.get("url", ""),
            }
            self._store_section(
                ct,
                {
                    "categories": [{"id": ALL_GROUP_ID, "title": "All"}],
                    "contents": [item],
                    "sorted_channels": {ALL_GROUP_ID: [0]},
                },
            )
            QTimer.singleShot(0, lambda: self.sectionReady.emit(section))
            return
        if ptype == "M3UPLAYLIST":
            worker = self.factory.m3u(provider.get("url", ""), verify, https)
        elif ptype == "XTREAM":
            worker = self.factory.xtream(
                provider.get("url", ""),
                provider.get("username", ""),
                provider.get("password", ""),
                ct,
                verify,
                https,
            )
        elif ptype == "STB":
            worker = self.factory.stb_categories(
                provider.get("url", ""), self.provider_manager.headers, ct, verify, https
            )
        else:
            QTimer.singleShot(
                0, lambda: self.sectionFailed.emit(section, f"Unsupported provider: {ptype}")
            )
            return
        self._start(worker, "section", section, self._on_section_loaded, self._on_section_error)

    def _on_section_loaded(self, payload) -> None:
        job = self._take_job()
        if job is None:
            return
        section = job[2]
        self._store_section(SECTION_CONTENT_TYPES[section], payload)
        self.sectionReady.emit(section)

    def _on_section_error(self, message: str) -> None:
        job = self._take_job()
        if job is not None:
            self.sectionFailed.emit(job[2], message)

    def _store_section(self, ct: str, payload) -> None:
        self.provider_manager.current_provider_content[ct] = payload
        self.provider_manager.mark_content_fresh(ct)
        self.provider_manager.save_provider()

    @staticmethod
    def _usable(content) -> bool:
        if isinstance(content, list):
            return bool(content)
        return isinstance(content, dict) and "categories" in content

    # --- groups and items --------------------------------------------------------------
    def _content(self, section: str):
        return self.provider_manager.current_provider_content.get(SECTION_CONTENT_TYPES[section])

    def groups(self, section: str) -> list[Group]:
        content = self._content(section)
        if isinstance(content, list):
            return [Group(section, ALL_GROUP_ID, "All", len(content))] if content else []
        if not isinstance(content, dict):
            return []
        contents = content.get("contents")
        sorted_map = content.get("sorted_channels")
        groups = []
        for category in content.get("categories", []) or []:
            group_id = str(category.get("id", ""))
            title = str(category.get("title") or category.get("name") or group_id)
            count: Optional[int]
            if isinstance(sorted_map, dict):
                if group_id == ALL_GROUP_ID:
                    count = len(contents) if isinstance(contents, list) else 0
                else:
                    count = len(sorted_map.get(group_id, []))
            elif isinstance(contents, dict):
                if group_id == ALL_GROUP_ID:
                    continue  # fetching every on-demand title at once is too heavy for a TV list
                loaded = contents.get(group_id)
                count = len(loaded) if isinstance(loaded, list) else None
            else:
                count = None
            if count == 0:
                continue
            groups.append(Group(section, group_id, title, count))
        return groups

    def items(self, section: str, group_id: str) -> Optional[list[dict]]:
        content = self._content(section)
        if isinstance(content, list):
            return list(content)
        if not isinstance(content, dict):
            return None
        contents = content.get("contents")
        sorted_map = content.get("sorted_channels")
        if isinstance(sorted_map, dict) and isinstance(contents, list):
            if group_id == ALL_GROUP_ID:
                return list(contents)
            return [contents[i] for i in sorted_map.get(group_id, []) if 0 <= i < len(contents)]
        if isinstance(contents, dict):
            loaded = contents.get(group_id)
            return list(loaded) if isinstance(loaded, list) else None
        return None

    def request_group(self, section: str, group_id: str) -> None:
        if self.items(section, group_id) is not None:
            QTimer.singleShot(0, lambda: self.groupReady.emit(section, group_id))
            return
        provider = self.current_provider()
        worker = self.factory.stb_list(
            url=stb_endpoint(
                stb_portal_base(
                    provider.get("url", ""),
                    provider_prefers_https(provider, self.config.prefer_https),
                )
            ),
            headers=self.provider_manager.headers,
            content_type=SECTION_CONTENT_TYPES[section],
            category_id=group_id,
            verify_ssl=provider.get("ssl_verify", self.config.ssl_verify),
        )
        self._start(
            worker, "group", (section, group_id), self._on_group_loaded, self._on_group_error
        )

    def _on_group_loaded(self, payload) -> None:
        job = self._take_job()
        if job is None:
            return
        section, group_id = job[2]
        content = self.provider_manager.current_provider_content.setdefault(
            SECTION_CONTENT_TYPES[section], {}
        )
        content.setdefault("contents", {})[group_id] = list(payload.get("items") or [])
        self.provider_manager.save_provider()
        self.groupReady.emit(section, group_id)

    def _on_group_error(self, message: str) -> None:
        job = self._take_job()
        if job is not None:
            section, group_id = job[2]
            self.groupFailed.emit(section, group_id, message)

    # --- job bookkeeping -------------------------------------------------------------
    def _start(self, worker, kind: str, data, on_finished, on_error) -> None:
        self._jobs[worker] = (kind, self._generation, data)
        self.runner.run_worker(worker, on_finished, on_error)

    def _take_job(self):
        """Pop the sending worker's job; None when it is unknown or from an older provider."""
        job = self._jobs.pop(self.sender(), None)
        if job is None or job[1] != self._generation:
            return None
        return job
