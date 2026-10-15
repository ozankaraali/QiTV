"""Provider catalog for the TV shell: providers, sections, groups and items (spec §4.1).

All network work runs through a JobRunner; every job is tagged with the provider
generation so results that arrive after a provider switch are dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import itertools
import logging
import re
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.epg_view import Programme, programmes_for_channel
from services.jobs import JobRunner
from services.provider_api import provider_prefers_https, stb_endpoint, stb_portal_base
from workers import find_roman_token

logger = logging.getLogger(__name__)

SECTION_CONTENT_TYPES = {"live": "itv", "movies": "vod", "series": "series"}
ALL_GROUP_ID = "*"
NO_SERIES_MESSAGE = "This provider has no series"


def normalize_seasons(series_name: str, items: list[dict]) -> list[dict]:
    """Number and name a series' seasons for display (ported from the legacy list UI)."""
    for i, item in enumerate(items):
        try:
            # Store original name before modification
            original_name = item.get("o_name") or item.get("name", f"Season {i + 1}")
            item["o_name"] = original_name

            # Derive a numeric season index for sorting/display
            number_str = None
            # Prefer explicit id/season_number if numeric
            sid = str(item.get("id", ""))
            if sid.isdigit():
                number_str = str(int(sid))
            else:
                s_num = str(item.get("season_number", ""))
                if s_num.isdigit():
                    number_str = str(int(s_num))
            # Fallback: first integer found in the name (e.g., "Season 1", "1. Sezon", "S02")
            if not number_str:
                m = re.search(r"\d+", original_name)
                if m:
                    number_str = str(int(m.group(0)))
            # Fallback: Roman numeral detection (e.g., "Season IV", "Rocky II")
            if not number_str:
                val = find_roman_token(original_name)
                if val is not None:
                    number_str = str(val)
            # Final fallback: position index
            if not number_str:
                number_str = str(i + 1)
            item["number"] = number_str

            # Create combined name
            item["name"] = f"{series_name}.{original_name}"

            # Add "added" field if not present (use air_date or empty)
            if "added" not in item:
                item["added"] = item.get("air_date", "")

        except Exception as e:
            logger.error(f"Error processing season {i}: {e}", exc_info=True)
            # Set defaults if processing fails
            item["o_name"] = item.get("name", f"Season {i + 1}")
            item["number"] = str(i + 1)
            item["name"] = f"{series_name}.Season {i + 1}"
            item["added"] = ""
    return items


def format_xtream_episodes(season, stream_base, stream_ext, username, password) -> list[dict]:
    """Build playable episode entries from an Xtream season (ported from the legacy list UI)."""
    episodes = season.get("episodes", []) or []
    formatted_episodes: list[dict] = []
    for idx, ep in enumerate(episodes, start=1):
        episode_id = ep.get("id")
        # Use container_extension from episode data, fallback to stream_ext
        container_ext = ep.get("container_extension") or stream_ext
        cmd = f"{stream_base}/series/{username}/{password}/{episode_id}.{container_ext}"
        # Robust episode number extraction
        number_str = None
        for key in ("episode_num", "episode", "num"):
            val = ep.get(key)
            if val is not None:
                s = str(val)
                if s.isdigit():
                    number_str = str(int(s))
                    break
        if not number_str:
            title_text = ep.get("title") or ep.get("name") or ""
            m = re.search(r"\d+", str(title_text))
            if m:
                number_str = str(int(m.group(0)))
        if not number_str:
            # Try Roman numerals in title/name with conservative rules
            val = find_roman_token(title_text)
            if val is not None:
                number_str = str(val)
        if not number_str and episode_id is not None:
            s = str(episode_id)
            if s.isdigit():
                number_str = str(int(s))
        if not number_str:
            number_str = str(idx)

        formatted_ep = {
            "id": episode_id,
            "number": number_str,
            "ename": ep.get("title") or f"Episode {number_str}",
            "name": ep.get("title") or f"Episode {number_str}",
            "description": ep.get("info") or "",
            "cmd": cmd,
            "logo": (
                ep.get("info", {}).get("movie_image") if isinstance(ep.get("info"), dict) else ""
            ),
            "season": ep.get("season"),
            "episode_num": number_str,
            "container_extension": container_ext,
        }
        formatted_episodes.append(formatted_ep)
    return formatted_episodes


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
    seasonsReady = Signal(str)
    seasonsFailed = Signal(str, str)
    episodesReady = Signal(str, str)
    episodesFailed = Signal(str, str, str)
    linkReady = Signal(int, str)
    linkFailed = Signal(int, str)

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
        self._setup_running = False
        self._pending_setup: Optional[tuple[Optional[str], bool]] = None
        self._pending_groups: set[tuple[int, str, str]] = set()
        self._pending_seasons: set[tuple[int, str]] = set()
        self._pending_episodes: set[tuple[int, str, str]] = set()
        self._link_ids = itertools.count(1)

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
        # Setups mutate the shared ProviderManager, so only one runs at a time; the
        # generation bumps now so a superseded setup's results are ignored.
        self._generation += 1
        self._epg_ready = False
        self._pending_groups.clear()
        self._pending_seasons.clear()
        self._pending_episodes.clear()
        if self._setup_running:
            self._pending_setup = (name, force_refresh)
            return
        self._start_setup(name, force_refresh)

    def _start_setup(self, name: Optional[str], force_refresh: bool) -> None:
        if name is not None and name != self.config.selected_provider_name:
            self.config.selected_provider_name = name
            self.config.save_config()
        target = name if name is not None else self.config.selected_provider_name
        worker = self.factory.provider_setup(self.provider_manager, force_refresh)
        self._setup_running = True
        self._start(
            worker,
            "provider",
            (target, force_refresh),
            self._on_provider_ready,
            self._on_provider_error,
        )

    def _finish_setup(self) -> bool:
        """Mark the running setup done; True when a queued request took over."""
        self._setup_running = False
        pending, self._pending_setup = self._pending_setup, None
        if pending is None:
            return False
        self._start_setup(*pending)
        return True

    def _on_provider_ready(self, job, payload) -> None:
        if self._finish_setup() or not self._current(job):
            return
        self.providerReady.emit(self.current_provider_name())
        worker = self.factory.epg(self.epg_manager, job[2][1])
        self._start(worker, "epg", None, self._on_epg_ready, self._on_epg_error)

    def _on_provider_error(self, job, message: str) -> None:
        if self._finish_setup() or not self._current(job):
            return
        self.providerFailed.emit(job[2][0] or self.current_provider_name(), message)

    def _on_epg_ready(self, job, payload) -> None:
        if self._current(job):
            self._epg_ready = True
            self.epgReady.emit()

    def _on_epg_error(self, job, message: str) -> None:
        if self._current(job):
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
            self._emit_later(self.sectionReady, section)
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
            self._emit_later(self.sectionReady, section)
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
            self._emit_later(self.sectionFailed, section, f"Unsupported provider: {ptype}")
            return
        self._start(worker, "section", section, self._on_section_loaded, self._on_section_error)

    def _on_section_loaded(self, job, payload) -> None:
        if not self._current(job):
            return
        section = job[2]
        self._store_section(SECTION_CONTENT_TYPES[section], payload)
        self.sectionReady.emit(section)

    def _on_section_error(self, job, message: str) -> None:
        if self._current(job):
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
            self._emit_later(self.groupReady, section, group_id)
            return
        key = (self._generation, section, group_id)
        if key in self._pending_groups:
            return
        self._pending_groups.add(key)
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

    def _on_group_loaded(self, job, payload) -> None:
        section, group_id = job[2]
        self._pending_groups.discard((job[1], section, group_id))
        if not self._current(job):
            return
        content = self.provider_manager.current_provider_content.get(SECTION_CONTENT_TYPES[section])
        if not isinstance(content, dict):
            logger.warning("Dropping %s group %s: section content is not loaded", section, group_id)
            return
        content.setdefault("contents", {})[group_id] = list(payload.get("items") or [])
        self.provider_manager.save_provider()
        self.groupReady.emit(section, group_id)

    def _on_group_error(self, job, message: str) -> None:
        section, group_id = job[2]
        self._pending_groups.discard((job[1], section, group_id))
        if self._current(job):
            self.groupFailed.emit(section, group_id, message)

    # --- series -----------------------------------------------------------------------
    def _series_cache(self) -> dict:
        contents = self.provider_manager.current_provider_content
        content = contents.setdefault("series", {})
        if not isinstance(content, dict):
            logger.debug("Replacing non-dict series content %r", type(content))
            content = contents["series"] = {}
        return content

    def seasons(self, series: dict) -> Optional[list[dict]]:
        cached = self._series_cache().get("seasons", {}).get(str(series.get("id")))
        return list(cached) if isinstance(cached, list) else None

    def request_seasons(self, series: dict) -> None:
        if self.seasons(series) is not None:
            self._emit_later(self.seasonsReady, str(series.get("id")))
            return
        series_id = str(series.get("id"))
        if self.provider_type() not in ("XTREAM", "STB"):
            self._emit_later(self.seasonsFailed, series_id, NO_SERIES_MESSAGE)
            return
        key = (self._generation, series_id)
        if key in self._pending_seasons:
            return
        self._pending_seasons.add(key)
        provider = self.current_provider()
        verify = provider.get("ssl_verify", self.config.ssl_verify)
        https = provider_prefers_https(provider, self.config.prefer_https)
        if self.provider_type() == "XTREAM":
            worker = self.factory.xtream_series(
                base_url=provider.get("url", ""),
                username=provider.get("username", ""),
                password=provider.get("password", ""),
                series_id=str(series.get("id")),
                resolved_base=self._series_cache().get("resolved_base", ""),
                verify_ssl=verify,
                prefer_https=https,
            )
        else:
            worker = self.factory.stb_list(
                url=stb_endpoint(stb_portal_base(provider.get("url", ""), https)),
                headers=self.provider_manager.headers,
                content_type="series",
                category_id=series.get("category_id") or series.get("tv_genre_id", ""),
                movie_id=series.get("id"),
                season_id=0,
                action="get_ordered_list",
                sortby="name",
                verify_ssl=verify,
            )
        self._start(worker, "seasons", series, self._on_seasons_loaded, self._on_seasons_error)

    def _on_seasons_loaded(self, job, payload) -> None:
        self._pending_seasons.discard((job[1], str(job[2].get("id"))))
        if not self._current(job):
            return
        series = job[2]
        cache = self._series_cache()
        if payload.get("resolved_base"):
            cache["resolved_base"] = payload["resolved_base"]
        raw = payload.get("seasons") if "seasons" in payload else payload.get("items")
        items = normalize_seasons(series.get("name", "Unknown Series"), list(raw or []))
        cache.setdefault("seasons", {})[str(series.get("id"))] = items
        self.provider_manager.save_provider()
        self.seasonsReady.emit(str(series.get("id")))

    def _on_seasons_error(self, job, message: str) -> None:
        self._pending_seasons.discard((job[1], str(job[2].get("id"))))
        if self._current(job):
            self.seasonsFailed.emit(str(job[2].get("id")), message)

    def episodes(self, series: dict, season: dict) -> Optional[list[dict]]:
        if self.provider_type() == "XTREAM":
            provider, cache = self.current_provider(), self._series_cache()
            return format_xtream_episodes(
                season,
                cache.get("stream_base", "") or cache.get("resolved_base", ""),
                cache.get("stream_ext", "ts"),
                provider.get("username", ""),
                provider.get("password", ""),
            )
        cached = (
            self._series_cache()
            .get("episodes", {})
            .get(str(series.get("id")), {})
            .get(str(season.get("id")))
        )
        return list(cached) if isinstance(cached, list) else None

    def request_episodes(self, series: dict, season: dict) -> None:
        if self.episodes(series, season) is not None:
            self._emit_later(self.episodesReady, str(series.get("id")), str(season.get("id")))
            return
        series_id, season_id = str(series.get("id")), str(season.get("id"))
        if self.provider_type() != "STB":
            self._emit_later(self.episodesFailed, series_id, season_id, NO_SERIES_MESSAGE)
            return
        key = (self._generation, series_id, season_id)
        if key in self._pending_episodes:
            return
        self._pending_episodes.add(key)
        provider = self.current_provider()
        worker = self.factory.stb_list(
            url=stb_endpoint(
                stb_portal_base(
                    provider.get("url", ""),
                    provider_prefers_https(provider, self.config.prefer_https),
                )
            ),
            headers=self.provider_manager.headers,
            content_type="series",
            category_id=series.get("category_id") or series.get("tv_genre_id", ""),
            movie_id=series.get("id"),
            season_id=season.get("id"),
            action="get_ordered_list",
            sortby="added",
            verify_ssl=provider.get("ssl_verify", self.config.ssl_verify),
        )
        self._start(
            worker, "episodes", (series, season), self._on_episodes_loaded, self._on_episodes_error
        )

    def _on_episodes_loaded(self, job, payload) -> None:
        series, season = job[2]
        self._pending_episodes.discard((job[1], str(series.get("id")), str(season.get("id"))))
        if not self._current(job):
            return
        season_id = str(season.get("id"))
        selected = next(
            (item for item in payload.get("items") or [] if str(item.get("id")) == season_id),
            None,
        )
        episodes = []
        for number in (selected or {}).get("series", []) or []:
            episode = dict(series)
            episode.update(
                {
                    "number": f"{number}",
                    "ename": f"Episode {number}",
                    "cmd": selected.get("cmd") if selected else None,
                    "series": number,
                }
            )
            episodes.append(episode)
        by_season = self._series_cache().setdefault("episodes", {})
        by_season.setdefault(str(series.get("id")), {})[season_id] = episodes
        self.provider_manager.save_provider()
        self.episodesReady.emit(str(series.get("id")), season_id)

    def _on_episodes_error(self, job, message: str) -> None:
        series, season = job[2]
        self._pending_episodes.discard((job[1], str(series.get("id")), str(season.get("id"))))
        if self._current(job):
            self.episodesFailed.emit(str(series.get("id")), str(season.get("id")), message)

    # --- links ------------------------------------------------------------------------
    def request_link(self, item: dict, *, section: str, is_episode: bool = False) -> int:
        """Resolve a playable URL. STB portals mint a fresh link; others play `cmd` directly."""
        request_id = next(self._link_ids)
        if section not in SECTION_CONTENT_TYPES:
            self._emit_later(self.linkFailed, request_id, f"Unknown section: {section}")
            return request_id
        if self.provider_type() != "STB":
            url = str(item.get("cmd") or "").strip()
            if url:
                self._emit_later(self.linkReady, request_id, url)
            else:
                self._emit_later(self.linkFailed, request_id, "This item has no stream address")
            return request_id
        provider = self.current_provider()
        worker = self.factory.link(
            base_url=provider.get("url", ""),
            headers=self.provider_manager.headers,
            content_type=SECTION_CONTENT_TYPES[section],
            cmd=item.get("cmd", ""),
            is_episode=is_episode,
            series_param=item.get("series") if is_episode else None,
            verify_ssl=provider.get("ssl_verify", self.config.ssl_verify),
            prefer_https=provider_prefers_https(provider, self.config.prefer_https),
        )
        self._start(worker, "link", request_id, self._on_link_ready, self._on_link_error)
        return request_id

    def _on_link_ready(self, job, payload) -> None:
        if not self._current(job):
            return
        url = str(payload.get("link") or "").strip()
        if url:
            self.linkReady.emit(job[2], url)
        else:
            self.linkFailed.emit(job[2], "The portal returned an empty link")

    def _on_link_error(self, job, message: str) -> None:
        if self._current(job):
            self.linkFailed.emit(job[2], message)

    # --- job bookkeeping -------------------------------------------------------------
    def _start(self, worker, kind: str, data, on_finished, on_error) -> None:
        self.runner.run_worker(
            worker, on_finished, on_error, context=(kind, self._generation, data)
        )

    def _current(self, job) -> bool:
        """True while the job belongs to the current provider generation."""
        return bool(job[1] == self._generation)

    def _emit_later(self, signal, *args) -> None:
        generation = self._generation
        QTimer.singleShot(0, lambda: self._emit_if_current(generation, signal, *args))

    def _emit_if_current(self, generation: int, signal, *args) -> None:
        if generation == self._generation:
            signal.emit(*args)
