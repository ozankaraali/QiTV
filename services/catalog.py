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
from typing import Any, Callable, Iterator, Optional, cast

from PySide6.QtCore import QObject, QTimer, Signal

from services.epg_view import Programme, programmes_for_channel
from services.jobs import FAILED_MESSAGE, NO_RESULT_MESSAGE, JobRunner
from services.provider_api import provider_prefers_https, stb_endpoint, stb_portal_base
from services.provider_ids import stamp
from services.redact import safe_message
from services.workers import NO_PORTAL_DATA_MESSAGE, STB_DEVICE_REJECTED_MESSAGE, find_roman_token

logger = logging.getLogger(__name__)

SECTION_CONTENT_TYPES = {"live": "itv", "movies": "vod", "series": "series"}
ALL_GROUP_ID = "*"
NO_SERIES_MESSAGE = "This provider has no series"
PROVIDER_LOADING_MESSAGE = "The provider is still loading"
GROUP_UNAVAILABLE_MESSAGE = "This group isn't available"
SECTION_UNAVAILABLE_MESSAGE = "This provider doesn't offer this section"
NO_STREAM_MESSAGE = "This item has no stream address"
EMPTY_LINK_MESSAGE = "The portal returned an empty link"
# Catalog network jobs (sections, groups, seasons, episodes) running at once per service:
# portals rate-limit, and some ban a MAC that opens many connections.
MAX_CATALOG_JOBS = 2

# --- failure messages shown to the user ---------------------------------------------------
# Worker errors are exception texts that can carry URLs, query strings and credentials
# (Xtream puts the username and password in every URL). Only these fixed strings, and the
# service's own messages, ever leave the service.
UNREACHABLE_MESSAGE = "Couldn't reach the provider"
LOGIN_REFUSED_MESSAGE = "The provider refused the login"
TOO_SLOW_MESSAGE = "The provider took too long to answer"
UNEXPECTED_ANSWER_MESSAGE = "The provider sent an unexpected answer"
_SAFE_MESSAGES = frozenset(
    {
        PROVIDER_LOADING_MESSAGE,
        GROUP_UNAVAILABLE_MESSAGE,
        SECTION_UNAVAILABLE_MESSAGE,
        NO_SERIES_MESSAGE,
        NO_STREAM_MESSAGE,
        EMPTY_LINK_MESSAGE,
        NO_PORTAL_DATA_MESSAGE,
        STB_DEVICE_REJECTED_MESSAGE,
        FAILED_MESSAGE,
        NO_RESULT_MESSAGE,
        UNREACHABLE_MESSAGE,
        LOGIN_REFUSED_MESSAGE,
        TOO_SLOW_MESSAGE,
        UNEXPECTED_ANSWER_MESSAGE,
    }
)
_SAFE_PREFIXES = ("Unknown section: ", "Unsupported provider: ")
_TOO_SLOW = re.compile(r"timed? ?out|timeout", re.IGNORECASE)
_LOGIN_REFUSED = re.compile(
    r"\b(?:401|403)\s+(?:client\s+)?error\b|unauthori[sz]ed|forbidden"
    r"|status(?:\s*code)?\W+(?:401|403)\b|:\s*(?:401|403)\s*$",
    re.IGNORECASE,
)
_UNREACHABLE = re.compile(
    r"max retries exceeded|failed to establish|connection (?:refused|reset|aborted|error)"
    r"|connectionerror|failed to resolve|name or service not known|nodename nor servname"
    r"|getaddrinfo|name resolution|cannot connect to host|network is unreachable"
    r"|no route to host|remote ?end closed|remotedisconnected|server ?disconnected"
    r"|ssl|certificate|no such file|errno"
    # requests' InvalidURL, MissingSchema and InvalidSchema: an address it cannot use.
    r"|invalid url|no (?:scheme|host) supplied|no connection adapters|^failed to parse:",
    re.IGNORECASE,
)


def friendly_error(message: object) -> str:
    """Map a worker's error text (or exception) to a fixed message without URLs or credentials.

    The service's own messages pass through. Everything else is classified and logged at
    INFO with the exception's type when an exception object is given. The text is never
    logged, nor searched for a type name: a credential could look like one.
    """
    text = "" if message is None else str(message)
    if text in _SAFE_MESSAGES or text.startswith(_SAFE_PREFIXES):
        return text
    if not text.strip() or _TOO_SLOW.search(text):
        friendly = TOO_SLOW_MESSAGE  # an empty text is almost always a timeout
    elif _LOGIN_REFUSED.search(text):
        friendly = LOGIN_REFUSED_MESSAGE
    elif _UNREACHABLE.search(text):
        friendly = UNREACHABLE_MESSAGE
    else:
        friendly = UNEXPECTED_ANSWER_MESSAGE
    if isinstance(message, BaseException):
        logger.info("Provider request failed: %s (%s)", friendly, type(message).__name__)
    else:
        logger.info("Provider request failed: %s", friendly)
    return friendly


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
    provider: str = ""  # the provider of a pinned catalog's group; "" for the selected provider
    header: bool = False  # a provider's heading row in a list that mixes providers


@dataclass(frozen=True, eq=False)
class _Job:
    """A running job's context: results are kept only for the same generation and provider."""

    kind: str
    generation: int
    provider: str
    data: Any = None
    key: Optional[tuple] = None  # catalog jobs: the de-duplication key


@dataclass(eq=False)
class _CatalogRequest:
    """A catalog network request waiting for a free job slot."""

    key: tuple
    kind: str
    data: Any
    build: Callable[[], QObject]


class WorkerFactory:
    """Creates the real workers; tests substitute a fake with the same method names."""

    def provider_setup(self, provider_manager, force_refresh) -> QObject:
        from services.workers import ProviderSetupWorker

        return ProviderSetupWorker(provider_manager, force_refresh)

    def epg(self, epg_manager, force_refresh) -> QObject:
        from services.workers import EpgLoadWorker

        return EpgLoadWorker(epg_manager, force_refresh)

    def m3u(self, url, verify_ssl, prefer_https) -> QObject:
        from services.workers import M3ULoaderWorker

        return M3ULoaderWorker(url, verify_ssl=verify_ssl, prefer_https=prefer_https)

    def xtream(
        self, base_url, username, password, content_type, verify_ssl, prefer_https
    ) -> QObject:
        from services.workers import XtreamLoaderWorker

        return XtreamLoaderWorker(
            base_url,
            username,
            password,
            content_type,
            verify_ssl=verify_ssl,
            prefer_https=prefer_https,
        )

    def stb_categories(self, url, headers, content_type, verify_ssl, prefer_https) -> QObject:
        from services.workers import STBCategoriesWorker

        return STBCategoriesWorker(
            url, headers, content_type, verify_ssl=verify_ssl, prefer_https=prefer_https
        )

    def stb_list(self, **loader_kwargs) -> QObject:
        from services.workers import StbListWorker

        return StbListWorker(**loader_kwargs)

    def xtream_series(self, **kwargs) -> QObject:
        from services.workers import XtreamSeriesInfoWorker

        return XtreamSeriesInfoWorker(**kwargs)

    def link(self, **kwargs) -> QObject:
        from services.workers import LinkCreatorWorker

        return LinkCreatorWorker(**kwargs)


class CatalogService(QObject):
    """The TV shell's view of the current provider.

    Provider setups and EPG loads mutate the shared ProviderManager/EpgManager, so they
    run one at a time in a small serial queue. While a setup is running or queued, catalog
    and link requests fail fast instead of reading half-switched provider state. Catalog
    network jobs run at most MAX_CATALOG_JOBS at a time for the current provider (those of
    its earlier generations included: they still hit the same portal); the rest wait
    newest-first.
    """

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
        provider_name: Optional[str] = None,
        link_ids: Optional[Iterator[int]] = None,
    ):
        """`provider_name` pins this catalog to one provider: it never follows (or writes)
        `config.selected_provider_name`. `link_ids` is shared by the catalogs of one app so
        link request ids are unique across them.
        """
        super().__init__(parent)
        self._pinned = provider_name
        self.config = config_manager
        self.provider_manager = provider_manager
        self.epg_manager = epg_manager
        self.runner = runner if runner is not None else JobRunner(self)
        self.factory = factory or WorkerFactory()
        self._generation = 0
        self._content_version = 0  # bumped whenever the catalog content may have changed
        self._epg_ready = False
        self._ready_provider: Optional[str] = None
        # Serial provider-state queue: setups and EPG loads never overlap.
        self._state_job: Optional[str] = None  # "provider" or "epg" while one runs
        self._pending_setup: Optional[tuple[Optional[str], bool]] = None
        self._setup_target: Optional[str] = None  # the provider the running setup loads
        self._pending_epg: Optional[bool] = None  # a queued EPG load (its force flag)
        # Catalog network jobs: keys are (generation, kind, ...) for queued and running jobs;
        # a running job's key maps to the provider whose portal it asks.
        self._catalog_queue: list[_CatalogRequest] = []
        self._catalog_inflight: dict[tuple, str] = {}
        self._catalog_handlers: dict[str, tuple[Callable, Callable]] = {
            "section": (self._on_section_loaded, self._on_section_error),
            "group": (self._on_group_loaded, self._on_group_error),
            "seasons": (self._on_seasons_loaded, self._on_seasons_error),
            "episodes": (self._on_episodes_loaded, self._on_episodes_error),
        }
        self._link_ids = link_ids if link_ids is not None else itertools.count(1)
        self._closed = False  # shutdown(): nothing starts or reports any more

    # --- providers -------------------------------------------------------------------
    def provider_names(self) -> list[str]:
        return [p.get("name", "") for p in self.provider_manager.providers]

    def current_provider(self) -> dict:
        return self.provider_manager.current_provider or {}

    def current_provider_name(self) -> str:
        return str(self.current_provider().get("name", ""))

    def provider_type(self) -> str:
        return str(self.current_provider().get("type", "")).upper()

    def content_version(self) -> int:
        """Changes whenever groups, items, seasons or episodes may have changed.

        Views memoize what they build from the catalog against it.
        """
        return self._content_version

    def select_provider(self, name: Optional[str] = None, *, force_refresh: bool = False) -> None:
        if self._closed:
            return
        # Only one setup runs at a time; the generation bumps now so results of anything
        # issued for the previous provider are ignored, and queued catalog requests and a
        # queued EPG load (both for the previous provider) are dropped.
        self._generation += 1
        self._content_version += 1  # the setup replaces the provider's content
        self._epg_ready = False
        self._ready_provider = None
        self._catalog_queue.clear()
        self._pending_epg = None
        if self._state_job is not None:
            queued = self._pending_setup
            if queued is not None and queued[1] and self._target(queued[0]) == self._target(name):
                force_refresh = True  # a queued forced refresh (Settings) must still clear
            self._pending_setup = (name, force_refresh)
            return
        self._start_setup(name, force_refresh)

    def _target(self, name: Optional[str]) -> str:
        """The provider a setup for `name` loads: None means the selected provider."""
        if self._pinned is not None:
            return self._pinned
        return name if name is not None else str(self.config.selected_provider_name or "")

    def loading_provider(self) -> Optional[str]:
        """The provider whose setup will report next (queued, else running), else None."""
        if self._pending_setup is not None:
            return self._target(self._pending_setup[0])
        if self._state_job == "provider":
            return self._setup_target
        return None

    def is_loading(self) -> bool:
        """True while a provider setup is running or queued.

        Requests made meanwhile fail with PROVIDER_LOADING_MESSAGE; ask again on
        providerReady.
        """
        return self._setup_active()

    def shutdown(self) -> None:
        """The window is closing: start no more jobs and report nothing more.

        Running jobs are cancelled (an XMLTV download or an STB crawl stops at its next step);
        what they still report is dropped.
        """
        self._closed = True
        cancel_all = getattr(self.runner, "cancel_all", None)
        if cancel_all is not None:
            cancel_all()
        self._generation += 1  # drops results and deferred emits of everything issued so far
        self._catalog_queue.clear()
        self._pending_setup = None
        self._pending_epg = None

    def _setup_active(self) -> bool:
        """True while a provider setup is running or waiting to run."""
        return self._state_job == "provider" or self._pending_setup is not None

    def _start_setup(self, name: Optional[str], force_refresh: bool) -> None:
        if self._pinned is None and name is not None and name != self.config.selected_provider_name:
            self.config.selected_provider_name = name
            try:
                self.config.save_config()
            except Exception as e:
                # The setup still runs; the choice is saved with the next settings write.
                logger.warning("Couldn't save the provider choice: %s", safe_message(e))
        target = self._target(name)
        worker = self.factory.provider_setup(self.provider_manager, force_refresh)
        self._state_job = "provider"
        self._setup_target = target
        self._start(
            worker,
            "provider",
            (target, force_refresh),
            self._on_provider_ready,
            self._on_provider_error,
        )

    def _next_state_job(self) -> bool:
        """The running setup or EPG load is done: start the next queued one.

        True when a queued provider setup took over (the finished job is superseded).
        """
        if self._state_job == "provider":
            self._content_version += 1  # the setup has finished replacing the content
        self._state_job = None
        if self._closed:
            return True  # nothing starts, and the finished job's result is not wanted
        if self._pending_setup is not None:
            pending, self._pending_setup = self._pending_setup, None
            self._start_setup(*pending)
            return True
        if self._pending_epg is not None:
            force, self._pending_epg = self._pending_epg, None
            self._start_epg(force)
        return False

    def _on_provider_ready(self, job: _Job, payload) -> None:
        if self._next_state_job() or job.generation != self._generation:
            return
        # A new generation for the ready provider: nothing issued before or during the
        # setup can store its result now.
        self._generation += 1
        self._stamp_loaded_content()
        self._ready_provider = self.current_provider_name()
        self.providerReady.emit(self._ready_provider)
        self._request_epg(job.data[1])
        self._pump_catalog()

    def _on_provider_error(self, job: _Job, message: str) -> None:
        if self._next_state_job() or job.generation != self._generation:
            return
        self._fail(self.providerFailed, job.data[0] or self.current_provider_name(), message)
        self._pump_catalog()

    # --- EPG --------------------------------------------------------------------------
    def refresh_epg(self, *, force: bool = False) -> None:
        """Reload the ready provider's guide (the TV shell calls this periodically).

        Runs behind any setup or EPG load already running; ignored while a setup is
        pending, since a finished setup loads its own guide.
        """
        if self._ready_provider is None or self._ready_provider != self.current_provider_name():
            return
        self._request_epg(force)

    def _request_epg(self, force: bool) -> None:
        if self._closed or self._setup_active():
            return
        if self._state_job is not None:  # an EPG load is running: load once more after it
            self._pending_epg = bool(force or self._pending_epg)
            return
        self._start_epg(force)

    def _start_epg(self, force: bool) -> None:
        self._state_job = "epg"
        worker = self.factory.epg(self.epg_manager, force)
        self._start(worker, "epg", force, self._on_epg_ready, self._on_epg_error)

    def _on_epg_ready(self, job: _Job, payload) -> None:
        if self._next_state_job() or not self._current(job):
            return
        self._epg_ready = True
        self.epgReady.emit()

    def _on_epg_error(self, job: _Job, message: str) -> None:
        if self._next_state_job() or not self._current(job):
            return
        logger.info("EPG unavailable: %s", friendly_error(message))

    def release_epg(self) -> None:
        """Give the guide's memory back (the saved copy stays); refresh_epg() loads it again."""
        self._pending_epg = None
        self._epg_ready = False
        if self.epg_manager is not None:
            self.epg_manager.release()

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
        if self._closed:
            return
        if self._setup_active():
            self._fail_later(self.sectionFailed, section, PROVIDER_LOADING_MESSAGE)
            return
        if section not in self.sections():
            # An M3U playlist has no movies or series: loading it as one would store its
            # channels under that section (and Search would list them there).
            self._fail_later(self.sectionFailed, section, SECTION_UNAVAILABLE_MESSAGE)
            return
        ct = SECTION_CONTENT_TYPES[section]
        content = self.provider_manager.current_provider_content.get(ct)
        if not force and self._usable(content) and not self.provider_manager.is_content_stale(ct):
            self._emit_later(self.sectionReady, section)
            return
        provider, ptype = self.current_provider(), self.provider_type()
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
        if ptype not in ("M3UPLAYLIST", "XTREAM", "STB"):
            self._fail_later(self.sectionFailed, section, f"Unsupported provider: {ptype}")
            return
        self._request_catalog(("section", section), "section", section, self._section_worker)

    def _section_worker(self, section: str) -> QObject:
        provider, ptype = self.current_provider(), self.provider_type()
        ct = SECTION_CONTENT_TYPES[section]
        verify = provider.get("ssl_verify", self.config.ssl_verify)
        https = provider_prefers_https(provider, self.config.prefer_https)
        if ptype == "M3UPLAYLIST":
            return self.factory.m3u(provider.get("url", ""), verify, https)
        if ptype == "XTREAM":
            return self.factory.xtream(
                provider.get("url", ""),
                provider.get("username", ""),
                provider.get("password", ""),
                ct,
                verify,
                https,
            )
        return self.factory.stb_categories(
            provider.get("url", ""), self.provider_manager.headers, ct, verify, https
        )

    def _on_section_loaded(self, job: _Job, payload) -> None:
        section = job.data
        self._store_section(SECTION_CONTENT_TYPES[section], payload)
        self.sectionReady.emit(section)

    def _on_section_error(self, job: _Job, message: str) -> None:
        self._fail(self.sectionFailed, job.data, message)

    def _store_section(self, ct: str, payload) -> None:
        self._stamp_content(payload)
        self._content_version += 1
        self.provider_manager.current_provider_content[ct] = payload
        self.provider_manager.mark_content_fresh(ct)
        self.provider_manager.save_provider()

    def has_content(self, section: str) -> bool:
        """True when the section's content is loaded (it may be stale)."""
        return self._usable(self._content(section))

    # --- provider stamps ---------------------------------------------------------------
    # Stamped once when content is stored or loaded from the cache, not on every read:
    # a catalog can hold 50,000 channels.
    def _stamp_loaded_content(self) -> None:
        for key, content in self.provider_manager.current_provider_content.items():
            if key != "_cache":
                self._stamp_content(content)

    def _stamp_content(self, content) -> None:
        """Stamp every item in a section's content (the lists, the per-group lists, the
        cached seasons and episodes) with the current provider."""
        provider = self.current_provider_name()
        if isinstance(content, list):
            stamp(content, provider)
            return
        if not isinstance(content, dict):
            return
        for key in ("contents", "seasons"):
            part = content.get(key)
            if isinstance(part, list):
                stamp(part, provider)
            elif isinstance(part, dict):
                for items in part.values():
                    if isinstance(items, list):
                        stamp(items, provider)
        episodes = content.get("episodes")
        if isinstance(episodes, dict):
            for by_season in episodes.values():
                if isinstance(by_season, dict):
                    for items in by_season.values():
                        if isinstance(items, list):
                            stamp(items, provider)

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
            if not content:
                return []
            return [Group(section, ALL_GROUP_ID, "All", len(content), self._pinned or "")]
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
            groups.append(Group(section, group_id, title, count, self._pinned or ""))
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
        if self._closed:
            return
        if self._setup_active():
            self._fail_later(self.groupFailed, section, group_id, PROVIDER_LOADING_MESSAGE)
            return
        if self.items(section, group_id) is not None:
            self._emit_later(self.groupReady, section, group_id)
            return
        if self.provider_type() != "STB":
            # Only STB portals load groups on demand; elsewhere the section holds them all.
            self._fail_later(self.groupFailed, section, group_id, GROUP_UNAVAILABLE_MESSAGE)
            return
        self._request_catalog(
            ("group", section, group_id), "group", (section, group_id), self._group_worker
        )

    def _group_worker(self, data: tuple[str, str]) -> QObject:
        section, group_id = data
        provider = self.current_provider()
        return self.factory.stb_list(
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

    def _on_group_loaded(self, job: _Job, payload) -> None:
        section, group_id = job.data
        content = self.provider_manager.current_provider_content.get(SECTION_CONTENT_TYPES[section])
        if not isinstance(content, dict):
            logger.warning("Dropping %s group %s: section content is not loaded", section, group_id)
            self._fail(self.groupFailed, section, group_id, GROUP_UNAVAILABLE_MESSAGE)
            return
        loaded = list(payload.get("items") or [])
        stamp(loaded, self.current_provider_name())
        content.setdefault("contents", {})[group_id] = loaded
        self._content_version += 1
        self.provider_manager.save_provider()
        self.groupReady.emit(section, group_id)

    def _on_group_error(self, job: _Job, message: str) -> None:
        section, group_id = job.data
        self._fail(self.groupFailed, section, group_id, message)

    # --- series -----------------------------------------------------------------------
    def _series_cache(self) -> dict:
        contents = self.provider_manager.current_provider_content
        content = contents.setdefault("series", {})
        if not isinstance(content, dict):
            logger.debug("Replacing non-dict series content %r", type(content))
            content = contents["series"] = {}
            self._content_version += 1
        return cast(dict, content)

    def seasons(self, series: dict) -> Optional[list[dict]]:
        cached = self._series_cache().get("seasons", {}).get(str(series.get("id")))
        return list(cached) if isinstance(cached, list) else None

    def request_seasons(self, series: dict) -> None:
        if self._closed:
            return
        series_id = str(series.get("id"))
        if self._setup_active():
            self._fail_later(self.seasonsFailed, series_id, PROVIDER_LOADING_MESSAGE)
            return
        if self.seasons(series) is not None:
            self._emit_later(self.seasonsReady, series_id)
            return
        if self.provider_type() not in ("XTREAM", "STB"):
            self._fail_later(self.seasonsFailed, series_id, NO_SERIES_MESSAGE)
            return
        self._request_catalog(("seasons", series_id), "seasons", series, self._seasons_worker)

    def _seasons_worker(self, series: dict) -> QObject:
        provider = self.current_provider()
        verify = provider.get("ssl_verify", self.config.ssl_verify)
        https = provider_prefers_https(provider, self.config.prefer_https)
        if self.provider_type() == "XTREAM":
            return self.factory.xtream_series(
                base_url=provider.get("url", ""),
                username=provider.get("username", ""),
                password=provider.get("password", ""),
                series_id=str(series.get("id")),
                resolved_base=self._series_cache().get("resolved_base", ""),
                verify_ssl=verify,
                prefer_https=https,
            )
        return self.factory.stb_list(
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

    def _on_seasons_loaded(self, job: _Job, payload) -> None:
        series = job.data
        cache = self._series_cache()
        if payload.get("resolved_base"):
            cache["resolved_base"] = payload["resolved_base"]
        raw = payload.get("seasons") if "seasons" in payload else payload.get("items")
        items = normalize_seasons(series.get("name", "Unknown Series"), list(raw or []))
        stamp(items, self.current_provider_name())
        cache.setdefault("seasons", {})[str(series.get("id"))] = items
        self._content_version += 1
        self.provider_manager.save_provider()
        self.seasonsReady.emit(str(series.get("id")))

    def _on_seasons_error(self, job: _Job, message: str) -> None:
        self._fail(self.seasonsFailed, str(job.data.get("id")), message)

    def episodes(self, series: dict, season: dict) -> Optional[list[dict]]:
        if self.provider_type() == "XTREAM":
            provider, cache = self.current_provider(), self._series_cache()
            formatted = format_xtream_episodes(
                season,
                cache.get("stream_base", "") or cache.get("resolved_base", ""),
                cache.get("stream_ext", "ts"),
                provider.get("username", ""),
                provider.get("password", ""),
            )
            stamp(formatted, self.current_provider_name())  # made on the fly, never stored
            return formatted
        cached = (
            self._series_cache()
            .get("episodes", {})
            .get(str(series.get("id")), {})
            .get(str(season.get("id")))
        )
        return list(cached) if isinstance(cached, list) else None

    def request_episodes(self, series: dict, season: dict) -> None:
        if self._closed:
            return
        series_id, season_id = str(series.get("id")), str(season.get("id"))
        if self._setup_active():
            self._fail_later(self.episodesFailed, series_id, season_id, PROVIDER_LOADING_MESSAGE)
            return
        if self.episodes(series, season) is not None:
            self._emit_later(self.episodesReady, series_id, season_id)
            return
        if self.provider_type() != "STB":
            self._fail_later(self.episodesFailed, series_id, season_id, NO_SERIES_MESSAGE)
            return
        self._request_catalog(
            ("episodes", series_id, season_id), "episodes", (series, season), self._episodes_worker
        )

    def _episodes_worker(self, data: tuple[dict, dict]) -> QObject:
        series, season = data
        provider = self.current_provider()
        return self.factory.stb_list(
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

    def _on_episodes_loaded(self, job: _Job, payload) -> None:
        series, season = job.data
        season_id = str(season.get("id"))
        selected = next(
            (item for item in payload.get("items") or [] if str(item.get("id")) == season_id),
            None,
        )
        # Episodes copy the series dict, so its id is the series id: the season keeps
        # S1E1 and S2E1 apart for watch state and navigation.
        season_number = season.get("number")
        episodes = []
        for number in (selected or {}).get("series", []) or []:
            episode = dict(series)
            episode.update(
                {
                    "number": f"{number}",
                    "ename": f"Episode {number}",
                    "cmd": selected.get("cmd") if selected else None,
                    "series": number,
                    "season_id": season_id,
                }
            )
            if season_number not in (None, ""):
                episode["season"] = str(season_number)
            episodes.append(episode)
        stamp(episodes, self.current_provider_name())
        by_season = self._series_cache().setdefault("episodes", {})
        by_season.setdefault(str(series.get("id")), {})[season_id] = episodes
        self._content_version += 1
        self.provider_manager.save_provider()
        self.episodesReady.emit(str(series.get("id")), season_id)

    def _on_episodes_error(self, job: _Job, message: str) -> None:
        series, season = job.data
        self._fail(self.episodesFailed, str(series.get("id")), str(season.get("id")), message)

    # --- links ------------------------------------------------------------------------
    def request_link(self, item: dict, *, section: str, is_episode: bool = False) -> int:
        """Resolve a playable URL. STB portals mint a fresh link; others play `cmd` directly."""
        request_id = next(self._link_ids)
        if self._closed:
            return request_id
        if self._setup_active():
            self._fail_later(self.linkFailed, request_id, PROVIDER_LOADING_MESSAGE)
            return request_id
        if section not in SECTION_CONTENT_TYPES:
            self._fail_later(self.linkFailed, request_id, f"Unknown section: {section}")
            return request_id
        if self.provider_type() != "STB":
            url = str(item.get("cmd") or "").strip()
            if url:
                self._emit_later(self.linkReady, request_id, url)
            else:
                self._fail_later(self.linkFailed, request_id, NO_STREAM_MESSAGE)
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

    def _on_link_ready(self, job: _Job, payload) -> None:
        if not self._current(job):
            return
        url = str(payload.get("link") or "").strip()
        if url:
            self.linkReady.emit(job.data, url)
        else:
            self._fail(self.linkFailed, job.data, EMPTY_LINK_MESSAGE)

    def _on_link_error(self, job: _Job, message: str) -> None:
        if self._current(job):
            self._fail(self.linkFailed, job.data, message)

    # --- catalog job queue ------------------------------------------------------------
    def _request_catalog(
        self, key: tuple, kind: str, data, build: Callable[[Any], QObject]
    ) -> None:
        """Run a catalog network job once per key, at most MAX_CATALOG_JOBS at a time."""
        key = (self._generation, *key)
        if key in self._catalog_inflight:
            return
        for index, queued in enumerate(self._catalog_queue):
            if queued.key == key:
                # Asked again: it is the user's latest focus, so it goes next.
                self._catalog_queue.append(self._catalog_queue.pop(index))
                return
        self._catalog_queue.append(_CatalogRequest(key, kind, data, lambda: build(data)))
        self._pump_catalog()

    def _pump_catalog(self) -> None:
        while (
            not self._closed
            and self._catalog_queue
            and self._running_catalog_jobs() < MAX_CATALOG_JOBS
            and not self._setup_active()
        ):
            request = self._catalog_queue.pop()  # newest first: the user's latest focus
            provider = self.current_provider_name()
            self._catalog_inflight[request.key] = provider
            job = _Job(request.kind, self._generation, provider, request.data, request.key)
            self.runner.run_worker(
                request.build(), self._on_catalog_loaded, self._on_catalog_error, context=job
            )

    def _running_catalog_jobs(self) -> int:
        """Catalog jobs in flight against the current provider's portal.

        Those of an earlier generation count too (a forced refresh, the same provider
        selected again): their results are dropped, but they still hit the portal. A previous
        provider's jobs finish on their own and hold none of the current provider's slots.
        """
        current = self.current_provider_name()
        return sum(1 for provider in self._catalog_inflight.values() if provider == current)

    def _finish_catalog_job(self, job: _Job) -> bool:
        """Free the job's slot; True when its result still belongs to the current provider."""
        if job.key is not None:
            self._catalog_inflight.pop(job.key, None)
        return self._current(job)

    def _on_catalog_loaded(self, job: _Job, payload) -> None:
        try:
            if self._finish_catalog_job(job):
                self._catalog_handlers[job.kind][0](job, payload)
        finally:
            self._pump_catalog()

    def _on_catalog_error(self, job: _Job, message: str) -> None:
        try:
            if self._finish_catalog_job(job):
                self._catalog_handlers[job.kind][1](job, message)
        finally:
            self._pump_catalog()

    # --- job bookkeeping -------------------------------------------------------------
    def _start(self, worker, kind: str, data, on_finished, on_error) -> None:
        job = _Job(kind, self._generation, self.current_provider_name(), data)
        self.runner.run_worker(worker, on_finished, on_error, context=job)

    def _current(self, job: _Job) -> bool:
        """True while the job belongs to the current provider generation and provider."""
        return job.generation == self._generation and job.provider == self.current_provider_name()

    def _fail(self, signal, *args) -> None:
        """Emit a *Failed signal; its last argument, the message, is made user-safe."""
        *head, message = args
        signal.emit(*head, friendly_error(message))

    def _fail_later(self, signal, *args) -> None:
        *head, message = args
        self._emit_later(signal, *head, friendly_error(message))

    def _emit_later(self, signal, *args) -> None:
        generation = self._generation
        QTimer.singleShot(0, self, lambda: self._emit_if_current(generation, signal, *args))

    def _emit_if_current(self, generation: int, signal, *args) -> None:
        if generation == self._generation:
            signal.emit(*args)
