from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
import os
import random
import string
import threading
import time
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit

from PySide6.QtCore import QObject, Signal
import orjson as json
import tzlocal

from services import free_channels, provider_cache
from services.atomic_write import atomic_write, set_aside
from services.catchup import mark_stb_archives
from services.lazy_import import lazy_module
from services.m3u import order_cached_groups, split_cached_groups
from services.provider_api import (
    https_fallback_candidates,
    known_http_only,
    remember_http_only,
    stb_api_base,
)
from services.provider_types import is_media_server
from services.redact import safe_exc, safe_message
from services.vlc_profile import STB_USER_AGENT

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

# Why an STB handshake failed (StbHandshake.failure); the setup worker words it for the user.
HANDSHAKE_UNREACHABLE = "unreachable"  # DNS, connection, timeout or a server error
HANDSHAKE_REJECTED = "rejected"  # an explicit refusal (401/403, blocked) or no token
HANDSHAKE_UNEXPECTED = "unexpected"  # any other answer: a wrong path, not JSON
# 2.12: a provider's cache is "<hash>.v2.jsonl", in pieces (services.provider_cache). Before,
# "<hash>.json", one document: read once when there is no new file, removed by the first save
# (older builds then find no cache and load the lists again).
CACHE_SUFFIX = ".v2.jsonl"
LEGACY_CACHE_SUFFIX = ".json"
# Beside it, what opened one title or group at a time (seasons, episodes, a portal's group
# pages): opening one writes this small file, not the lists again (save_provider_pages).
PAGES_SUFFIX = ".v2.pages.jsonl"
# One cache file is read or written at a time, whichever provider's: four read at once left the
# GUI thread a fifth of the interpreter (its work took 4.7 times as long), one at a time half.
_CACHE_FILE_TURN = threading.Lock()
_PREHASH = "2614ddf9829ba9d284f389d88e8c669d81f6a5c2"
# When attempts (portal paths, HTTPS then HTTP) fail differently, the stronger reason wins.
_HANDSHAKE_FAILURE_RANK = {HANDSHAKE_UNREACHABLE: 0, HANDSHAKE_UNEXPECTED: 1, HANDSHAKE_REJECTED: 2}


class _PortalStatus(Exception):
    """A portal answered a handshake step with a non-200 status."""

    def __init__(self, step, status_code):
        super().__init__(f"Failed to fetch {step}: {status_code}")
        self.status_code = status_code


class _PortalRefused(Exception):
    """A portal answered, but refused this device."""


def classify_handshake_error(error):
    """The HANDSHAKE_* reason for an exception raised during an STB handshake."""
    if isinstance(error, _PortalStatus):
        if error.status_code >= 500:
            return HANDSHAKE_UNREACHABLE
        if error.status_code in (401, 403):
            return HANDSHAKE_REJECTED
        return HANDSHAKE_UNEXPECTED
    if isinstance(error, (_PortalRefused, KeyError, TypeError)):
        return HANDSHAKE_REJECTED  # refused, or an answer without a token or profile
    if isinstance(error, (requests.exceptions.InvalidJSONError, ValueError)):
        return HANDSHAKE_UNEXPECTED  # not JSON (requests' JSON error is a RequestException)
    if isinstance(error, requests.RequestException):
        return HANDSHAKE_UNREACHABLE  # DNS, connection, SSL and timeouts
    return HANDSHAKE_UNEXPECTED


def _modified(path) -> int:
    """When `path` was last written (ns); -1 when there is no such file."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return -1


class ProviderManager(QObject):
    progress = Signal(str)

    CONTENT_CACHE_TTL = 6 * 60 * 60

    def __init__(
        self,
        config_manager,
        provider_name: Optional[str] = None,
        providers: Optional[list] = None,
    ):
        """`provider_name` pins this manager to one provider whatever the config selects;
        `providers` shares that list (so pinned managers agree) instead of reading index.json.
        """
        super().__init__()
        self.config_manager = config_manager
        self._pinned_provider = provider_name
        self.provider_dir = os.path.join(config_manager.get_config_dir(), "cache", "provider")
        os.makedirs(self.provider_dir, exist_ok=True)
        self.index_file = os.path.join(self.provider_dir, "index.json")
        self.providers = []
        self.current_provider: dict[str, Any] = {}
        self.current_provider_content: dict[str, Any] = {}
        self.token = ""
        self.headers: dict[str, str] = {}
        self.handshake_failure = None  # a HANDSHAKE_* reason after a failed STB setup
        self._cache_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="provider-cache")
        self._cache_lock = threading.Lock()
        self._cache_generations: dict[str, int] = {}
        # (generation, snapshot, whether its lists are written too: save_provider_pages's not)
        self._pending_cache_snapshots: dict[str, tuple[int, Any, bool]] = {}
        # Set when index.json could not be read: the caches of the providers it listed stay.
        self._keep_unlisted_caches = False
        if providers is not None:
            self.providers = providers
        else:
            self._load_providers()

    def _provider_cache_name(self, provider_name):
        hashed_name = hashlib.sha256(provider_name.encode("utf-8")).hexdigest()
        return os.path.join(self.provider_dir, f"{hashed_name}{CACHE_SUFFIX}")

    @staticmethod
    def _legacy_cache_name(cache_path):
        """The older builds' file for the cache at `cache_path` (one JSON document)."""
        return cache_path[: -len(CACHE_SUFFIX)] + LEGACY_CACHE_SUFFIX

    @staticmethod
    def _pages_name(cache_path):
        """The pages beside the cache at `cache_path` (PAGES_SUFFIX)."""
        return cache_path[: -len(CACHE_SUFFIX)] + PAGES_SUFFIX

    def _current_provider_cache_name(self):
        return self._provider_cache_name(self.current_provider["name"])

    def provider_cache_identity(self, provider, *, ssl_verify=None):
        """Identify the effective connection, without persisting credentials in metadata.
        (2.7.1: without the retired "Prefer HTTPS", so a list saved under it, its addresses
        HTTPS whether the provider answered there or not, is loaded again once.)"""
        connection = {
            key: provider.get(key, "")
            for key in ("type", "url", "mac", "username", "password", "serial_number", "device_id")
        }
        connection["ssl_verify"] = provider.get(
            "ssl_verify",
            self.config_manager.ssl_verify if ssl_verify is None else ssl_verify,
        )
        return hashlib.sha256(json.dumps(connection, option=json.OPT_SORT_KEYS)).hexdigest()

    def _load_provider_cache(self, provider):
        cache_path = self._provider_cache_name(provider["name"])
        with self._cache_lock:
            pending = self._pending_cache_snapshots.get(cache_path)
            cache = self._snapshot_cache(pending[1]) if pending is not None else None
            generation = self._cache_generations.get(cache_path, 0)
        legacy = False
        if cache is None:
            cache, legacy = self._read_cache_file(cache_path)
        if not isinstance(cache, dict):
            return {}
        metadata = cache.get("_cache")
        if metadata is not None:
            if not isinstance(metadata, dict):
                return {}
            if metadata.get("identity") != self.provider_cache_identity(provider):
                return {}
        ptype = str(provider.get("type", "")).upper()
        if ptype == "M3UPLAYLIST" and "itv" in cache:
            # Cached before "A;B" group titles were split: split now, from the cache.
            cache["itv"] = split_cached_groups(cache["itv"])
            order_cached_groups(cache["itv"])  # in the language's alphabet, case aside
        live = cache.get("itv")
        if ptype == "STB" and isinstance(live, dict):
            # Cached before STB catch-up: the channels the portal marks archived get their
            # archive keys now, from the cache.
            mark_stb_archives(live.get("contents"))
        if legacy and provider.get("name") == self.current_provider.get("name"):
            self._rewrite_legacy_cache(cache_path, generation, cache)  # (the content it saves)
        # Raw legacy payloads are readable, but have no trusted freshness timestamp.
        return cache

    def _read_cache_file(self, cache_path):
        """(The cache saved at `cache_path`, whether it is an older build's): {} when it cannot
        be read. The older builds' single document instead when it is the newer file: none
        here, or one an older build wrote since (gone back to, then this build again; this
        build removes it with each lists' file it writes, an older one never sees that)."""
        legacy = self._legacy_cache_name(cache_path)
        with _CACHE_FILE_TURN:
            if _modified(legacy) > _modified(cache_path):
                try:
                    with open(legacy, "rb") as f:
                        return json.loads(f.read()), True
                except OSError, json.JSONDecodeError:
                    return {}, False
            try:
                cache = provider_cache.read_cache(cache_path)
            except OSError, ValueError, TypeError:  # (none, or damaged: loaded again)
                return {}, False
            try:  # the seasons and group pages opened, as they were saved with these lists
                provider_cache.read_pages(self._pages_name(cache_path), cache)
            except FileNotFoundError:
                pass
            except OSError, ValueError, TypeError:
                logger.info("A provider's opened series and groups can't be read: dropped")
            return cache, False

    def _rewrite_legacy_cache(self, cache_path, generation, cache):
        """An older build's cache, just read, is written as the new file (and goes): it is
        read in one call once. Not when a save or a removal came meanwhile (`generation`)."""
        snapshot = self._snapshot_cache(cache)
        with self._cache_lock:
            if self._cache_generations.get(cache_path, 0) != generation:
                return
            generation += 1
            self._cache_generations[cache_path] = generation
            self._pending_cache_snapshots[cache_path] = (generation, snapshot, True)
        try:
            self._cache_writer.submit(self._write_provider_cache, cache_path, generation, snapshot)
        except RuntimeError:  # the writer has stopped (its provider went): left to the next start
            with self._cache_lock:
                if self._pending_cache_snapshots.get(cache_path, (0,))[0] == generation:
                    del self._pending_cache_snapshots[cache_path]

    def is_content_stale(self, content_type) -> bool:
        if content_type not in self.current_provider_content:
            return True
        metadata = self.current_provider_content.get("_cache")
        if not isinstance(metadata, dict):
            return True
        if metadata.get("identity") != self.provider_cache_identity(self.current_provider):
            return True
        timestamps = metadata.get("refreshed_at")
        if not isinstance(timestamps, dict):
            return True
        refreshed_at = timestamps.get(content_type)
        if isinstance(refreshed_at, bool) or not isinstance(refreshed_at, (int, float)):
            return True
        age = time.time() - refreshed_at
        return not 0 <= age < self.CONTENT_CACHE_TTL

    def content_expired(self, content_type) -> bool:
        """The saved content is the current provider's as set up now, only older than
        CONTENT_CACHE_TTL (2.8: a film or series list then shows while it loads again)."""
        if content_type not in self.current_provider_content:
            return False
        metadata = self.current_provider_content.get("_cache")
        if not isinstance(metadata, dict):
            return False
        if metadata.get("identity") != self.provider_cache_identity(self.current_provider):
            return False
        timestamps = metadata.get("refreshed_at")
        refreshed_at = timestamps.get(content_type) if isinstance(timestamps, dict) else None
        if isinstance(refreshed_at, bool) or not isinstance(refreshed_at, (int, float)):
            return False
        return self.is_content_stale(content_type)

    def mark_content_fresh(self, content_type) -> None:
        """Mark only a successful root fetch; category persistence must not call this."""
        metadata = self._cache_metadata()
        metadata["refreshed_at"][content_type] = time.time()

    def _cache_metadata(self):
        identity = self.provider_cache_identity(self.current_provider)
        metadata = self.current_provider_content.get("_cache")
        if not isinstance(metadata, dict) or metadata.get("identity") != identity:
            metadata = {"identity": identity, "refreshed_at": {}}
            self.current_provider_content["_cache"] = metadata
        elif not isinstance(metadata.get("refreshed_at"), dict):
            metadata["refreshed_at"] = {}
        return metadata

    def _load_providers(self):
        try:
            with open(self.index_file, "rb") as f:
                self.providers = json.loads(f.read())
        except FileNotFoundError:
            self.providers = self.default_providers()
            self.save_providers()
        except json.JSONDecodeError:  # cut short (an older build crashed mid-save), or damaged
            self._replace_unreadable_index()
        else:
            if self.providers is None:
                self.providers = self.default_providers()
            elif not isinstance(self.providers, list):
                self._replace_unreadable_index()

    def _replace_unreadable_index(self):
        """Start from the free list. The index.json that cannot be read holds every provider
        and its sign-in: it is kept beside the new one (index.json.broken-<time>) and so are
        the providers' caches, so putting that file back brings everything back."""
        self.providers = self.default_providers()
        self._keep_unlisted_caches = True  # no save this session prunes a cache
        aside = set_aside(self.index_file)  # where it cannot be moved, nor can it be replaced
        if aside is not None:
            logger.warning(
                "The provider list can't be read: it was kept as %s, and the free list is "
                "used meanwhile",
                os.path.basename(aside),
            )
        try:
            self.save_providers()
        except OSError as e:
            logger.warning("Couldn't save the provider list: %s", safe_message(e))

    @staticmethod
    def default_providers():
        """No providers file (or an unreadable one): the default config's free list."""
        return [free_channels.provider_entry(None)]

    def _invalidate_cache_path(self, cache_path) -> None:
        """Supersede queued writes for this cache file, then delete it (its pages, and an older
        build's file too)."""
        with self._cache_lock:
            self._cache_generations[cache_path] = self._cache_generations.get(cache_path, 0) + 1
            self._pending_cache_snapshots.pop(cache_path, None)
            for path in (
                cache_path,
                self._pages_name(cache_path),
                self._legacy_cache_name(cache_path),
            ):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

    def invalidate_provider_cache(self, provider_name) -> None:
        """Invalidate queued writes as well as the on-disk cache for this name."""
        self._invalidate_cache_path(self._provider_cache_name(provider_name))
        if self.current_provider.get("name") == provider_name:
            self.current_provider_content = {}

    def clear_current_provider_cache(self):
        self.invalidate_provider_cache(self.current_provider["name"])

    def set_current_provider(self, progress_callback):
        """Select the configured provider.

        False when an STB handshake fails; ``handshake_failure`` then says why.
        """
        # A setup that fails part-way (e.g. an STB entry without a MAC) must not leave the
        # previous portal's session (MAC cookie, token) for the next requests.
        self.headers = {}
        self.token = ""
        self.handshake_failure = None
        progress_callback.emit("Searching for provider...")
        self.current_provider = {}
        # search for provider in the list
        wanted = self._pinned_provider or self.config_manager.selected_provider_name
        if wanted:
            for provider in self.providers:
                if provider["name"] == wanted:
                    self.current_provider = provider
                    break

        if not self.current_provider and self._pinned_provider:
            # A provider deleted in Settings must not be served as another one.
            logger.warning("The pinned provider is no longer in the list")
            raise LookupError("This provider is no longer in your list.")

        # if provider not found, set the first one
        if not self.current_provider:
            self.current_provider = self.providers[0]

        progress_callback.emit("Loading provider content...")
        self.current_provider_content = self._load_provider_cache(self.current_provider)

        accepted = True
        if self.current_provider["type"] == "STB":
            progress_callback.emit("Performing handshake...")
            accepted = bool(
                self.do_handshake(
                    self.current_provider["url"],
                    self.current_provider["mac"],
                    serial_number=self.current_provider.get("serial_number", ""),
                    device_id=self.current_provider.get("device_id", ""),
                )
            )

        progress_callback.emit("Provider setup complete.")
        return accepted

    def get_all_providers_cached_content(self, loaded=None):
        """Return cached content for all configured providers.

        Returns list of (provider_name, content_dict) tuples.
        Only includes providers that have been loaded/cached. `loaded` maps a provider's name
        to the content its catalog holds in memory: that is used instead of reading (and
        parsing) the provider's cache file again, which for 200,000 channels takes seconds.
        """
        results = []
        for provider in self.providers:
            name = provider.get("name", "Unknown")
            cache = loaded.get(name) if loaded else None
            if cache is None:
                cache = self._load_provider_cache(provider)
            # Playlists offer live channels only; an older catalog may have stored the
            # playlist again under "vod" or "series".
            kind = str(provider.get("type", "")).upper()
            playlist = kind not in ("STB", "XTREAM") and not is_media_server(kind)
            content = {
                key: value
                for key, value in list(cache.items())
                if key != "_cache" and not (playlist and key in ("vod", "series"))
            }
            if content:
                results.append((name, content))
        return results

    def save_providers(self):
        serialized = json.dumps(self.providers, option=json.OPT_INDENT_2)
        # Written whole or not at all, and (as atomic_write writes) readable by the user alone:
        # it holds every sign-in.
        with atomic_write(self.index_file) as f:
            f.write(serialized)
        if self._keep_unlisted_caches:
            return
        try:
            self._prune_unlisted_caches()
        except OSError as e:
            # The list is written: a raise now would say it was not (Settings puts back a
            # change whose write failed). A stale cache only takes room until the next save.
            logger.warning("Couldn't remove a stale provider cache: %s", type(e).__name__)

    def _prune_unlisted_caches(self) -> None:
        # Delete stale cache files not matching any known provider name hash
        # (malformed provider entries without a string name are ignored).
        expected_files = {
            os.path.basename(self._provider_cache_name(p["name"]))
            for p in self.providers
            if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"]
        }

        with self._cache_lock:
            queued_files = {os.path.basename(path) for path in self._cache_generations}
        caches = set()
        for entry in set(os.listdir(self.provider_dir)) | queued_files:
            # Only a cache's files are considered for pruning (an older build's "<hash>.json" too)
            if entry == "index.json":
                continue
            for suffix in (CACHE_SUFFIX, PAGES_SUFFIX, LEGACY_CACHE_SUFFIX):
                if entry.endswith(suffix):
                    caches.add(entry[: -len(suffix)] + CACHE_SUFFIX)
                    break
        for cache in sorted(caches - expected_files):
            self._invalidate_cache_path(os.path.join(self.provider_dir, cache))

    @staticmethod
    def _snapshot_cache(value):
        # Copy mutable index maps, not potentially enormous channel/episode payloads.
        if isinstance(value, dict):
            return {key: ProviderManager._snapshot_cache(item) for key, item in value.items()}
        return value

    def save_provider(self):
        """Save the current provider's content: its lists and its pages."""
        self._save_cache(lists=True)

    def save_provider_pages(self):
        """Save what opened since (2.12): a series' seasons, a season's episodes, a portal's
        group pages. The lists' file stays as it is: the small pages file is written, not the
        whole catalog again (a hundred MB or more on a large account, at each series)."""
        self._save_cache(lists=False)

    def _save_cache(self, lists):
        metadata = self.current_provider_content.get("_cache")
        if self._cache_metadata() is not metadata:  # new (another identity): the lists' too
            lists = True
        snapshot = self._snapshot_cache(self.current_provider_content)
        cache_path = self._current_provider_cache_name()
        with self._cache_lock:
            generation = self._cache_generations.get(cache_path, 0) + 1
            self._cache_generations[cache_path] = generation
            pending = self._pending_cache_snapshots.get(cache_path)
            lists = lists or (pending is not None and pending[2])  # (that write is superseded)
            self._pending_cache_snapshots[cache_path] = (generation, snapshot, lists)
        self._cache_writer.submit(
            self._write_provider_cache, cache_path, generation, snapshot, lists
        )

    def _write_provider_cache(self, cache_path, generation, snapshot, lists=True):
        def newest():
            return self._cache_generations.get(cache_path, 0) == generation

        def replace_if_newest(temporary, path):  # a newer snapshot queued meanwhile wins
            with self._cache_lock:
                if newest():
                    os.replace(temporary, path)

        try:
            with self._cache_lock:
                if not newest():
                    return
            if lists or not os.path.exists(cache_path):
                # In pieces: one orjson call held the GIL (the GUI thread) for the whole cache.
                with _CACHE_FILE_TURN:  # (removals too: Windows refuses a file being read)
                    provider_cache.write_cache(cache_path, snapshot, replace_if_newest)
                    if os.path.exists(cache_path):  # the older builds' file is read no more
                        try:
                            os.remove(self._legacy_cache_name(cache_path))
                        except FileNotFoundError:
                            pass
            pages = self._pages_name(cache_path)
            if provider_cache.has_pages(snapshot):
                with _CACHE_FILE_TURN:
                    provider_cache.write_pages(pages, snapshot, replace_if_newest)
            else:
                with _CACHE_FILE_TURN, self._cache_lock:
                    if newest() and os.path.exists(pages):
                        os.remove(pages)
        except OSError, TypeError:
            logger.exception("Failed to save provider cache")
        finally:
            with self._cache_lock:
                pending = self._pending_cache_snapshots.get(cache_path)
                if pending is not None and pending[0] == generation:
                    del self._pending_cache_snapshots[cache_path]

    def do_handshake(
        self,
        url,
        mac,
        serverload="/portal.php",
        serial_number="",
        device_id="",
        prefer_https_override=None,
        ssl_verify_override=None,
    ):
        handshake = StbHandshake(
            prefer_https=True,
            ssl_verify=self.current_provider.get("ssl_verify", self.config_manager.ssl_verify),
            token=self.token,
        )
        try:
            return handshake.run(
                url,
                mac,
                serverload,
                serial_number,
                device_id,
                prefer_https_override=prefer_https_override,
                ssl_verify_override=ssl_verify_override,
            )
        finally:
            self.token = handshake.token
            self.headers = handshake.headers
            self.handshake_failure = handshake.failure


class StbHandshake:
    """An isolated STB session, usable for setup or background provider verification."""

    def __init__(self, *, prefer_https=False, ssl_verify=True, token=""):
        self.prefer_https = prefer_https
        self.ssl_verify = ssl_verify
        self.token = token
        self.headers = {}
        self.failure = None  # a HANDSHAKE_* reason once run() has returned False
        # What the attempts met, for an account check (services.account_check): each error
        # status, whether one timed out, and the profile's answer (its "js") once one came.
        self.statuses: list[int] = []
        self.timed_out = False
        self.profile = None

    def run(
        self,
        url,
        mac,
        serverload="/portal.php",
        serial_number="",
        device_id="",
        prefer_https_override: bool | None = None,
        ssl_verify_override: bool | None = None,
        _prefer_attempted: bool = False,
        _quiet: bool = False,
    ):
        self.token = self.token if self.token else self.random_token()
        self.headers = self.create_headers(url, mac, self.token, serial_number, device_id)
        try:
            # Optionally prefer HTTPS but allow fallback to HTTP on failure
            original_url = url
            prefer_https = (
                prefer_https_override if prefer_https_override is not None else self.prefer_https
            )
            ssl_verify = ssl_verify_override if ssl_verify_override is not None else self.ssl_verify
            # HTTPS first, unless the portal answered only over HTTP this run
            try_https = (
                prefer_https
                and not _prefer_attempted
                and url.startswith("http://")
                and not known_http_only(url)
            )
            if try_https:
                url = "https://" + url[len("http://") :]
            api = stb_api_base(url)  # where its lists, links and guide are asked too (2.8.12)

            fetchurl = (
                f"{api}{serverload}?type=stb&action=handshake&prehash={_PREHASH}"
                "&token=&JsHttpRequest=1-xml"
            )
            handshake = requests.get(fetchurl, timeout=5, headers=self.headers, verify=ssl_verify)
            if handshake.status_code == 200:
                body = handshake.json()
            else:
                raise _PortalStatus("handshake", handshake.status_code)
            self.token = body["js"]["token"]
            self.headers["Authorization"] = f"Bearer {self.token}"

            # Use get_profile request to detect blocked providers
            fetchurl = self.profile_url(api, serverload, mac)
            profile = requests.get(fetchurl, timeout=5, headers=self.headers, verify=ssl_verify)
            if profile.status_code == 200:
                body = profile.json()
            else:
                raise _PortalStatus("profile", profile.status_code)

            if isinstance(body, dict) and isinstance(body.get("js"), dict):
                self.profile = body["js"]
            theId = body["js"]["id"]
            theName = body["js"]["name"]
            if not theId and not theName:
                raise _PortalRefused("Provider is blocked")

            self.failure = None
            return True
        except Exception as e:
            if isinstance(e, _PortalStatus):
                self.statuses.append(e.status_code)
            elif isinstance(e, requests.exceptions.Timeout):
                self.timed_out = True
            reason = classify_handshake_error(e)
            if self.failure is None or (
                _HANDSHAKE_FAILURE_RANK[reason] > _HANDSHAKE_FAILURE_RANK[self.failure]
            ):
                self.failure = reason
            http_retry = try_https
            load_retry = serverload != "/server/load.php" and "handshake" in fetchurl
            # The other portal path on the same scheme first; when that fails too, the HTTP
            # fallback below still runs (both paths again, over HTTP).
            if load_retry and self.run(
                url,
                mac,
                "/server/load.php",
                serial_number,
                device_id,
                prefer_https_override=prefer_https_override,
                ssl_verify_override=ssl_verify_override,
                _prefer_attempted=_prefer_attempted,
                _quiet=http_retry,
            ):
                return True
            # If HTTPS attempt failed and we preferred HTTPS, fall back to HTTP once
            if http_retry:
                logger.info("HTTPS handshake failed; retrying over HTTP")
                signed_in = self.run(
                    original_url,
                    mac,
                    serverload,
                    serial_number,
                    device_id,
                    prefer_https_override=prefer_https_override,
                    ssl_verify_override=ssl_verify_override,
                    _prefer_attempted=True,
                )
                if signed_in:  # its lists, links and guide are asked over HTTP too
                    remember_http_only(original_url)
                return signed_in
            if not load_retry and not _quiet:
                logger.warning("STB handshake failed (%s): %s", self.failure, safe_exc(e))
            return False

    @staticmethod
    def profile_url(url, serverload, mac):
        """The portal's get_profile request, as a set-top box sends it after its handshake."""
        params = {
            "ver": (
                "ImageDescription: 2.20.02-pub-424; ImageDate: Fri May 8 15:39:55 UTC 2020; "
                "PORTAL version: 5.3.0; API Version: JS API version: 343; "
                "STB API version: 146; Player Engine version: 0x588"
            ),
            "num_banks": "2",
            "sn": "062014N067770",
            "stb_type": "MAG424",
            "client_type": "STB",
            "image_version": "220",
            "video_out": "hdmi",
            "device_id": "",
            "device_id2": "",
            "signature": "",
            "auth_second_step": "1",
            "hw_version": "1.7-BD-00",
            "not_valid_token": "0",
            "metrics": (
                f'{{"mac":"{mac}", "sn":"062014N067770","model":"MAG424",'
                '"type":"STB","uid":"","random":""}'
            ),
            "hw_version_2": "bb8b74cdcaa19c7f6a6bdfecc8e91b7e4b5ea556",
            "timestamp": "1729441259",
            "api_signature": "262",
            "prehash": _PREHASH,
        }
        encoded_params = urlencode(params)
        return (
            f"{url}{serverload}?type=stb&action=get_profile&hd=1&{encoded_params}"
            "&JsHttpRequest=1-xml"
        )

    def check_profile(self, url, mac, headers):
        """A profile request on a session that is signed in already (`headers`, with its
        token): no handshake, so the portal keeps the token that session plays with (portals
        bind one token to a MAC). True when the portal knows the device; `statuses`,
        `timed_out` and `profile` record what came, as run() records them."""
        self.headers = dict(headers)
        bases = https_fallback_candidates(stb_api_base(url), self.prefer_https)
        for base in bases:
            for serverload in ("/portal.php", "/server/load.php"):
                try:
                    response = requests.get(
                        self.profile_url(base, serverload, mac),
                        timeout=5,
                        headers=self.headers,
                        verify=self.ssl_verify,
                    )
                except requests.exceptions.Timeout:
                    self.timed_out = True
                    continue
                except requests.RequestException:
                    continue
                if response.status_code != 200:
                    self.statuses.append(response.status_code)
                    continue
                try:
                    body = response.json()
                except ValueError:
                    continue
                js = body.get("js") if isinstance(body, dict) else None
                if isinstance(js, dict):
                    self.profile = js
                    return bool(js.get("id") or js.get("name"))
        return False

    @staticmethod
    def random_token():
        return "".join(random.choices(string.ascii_letters + string.digits, k=32))

    @staticmethod
    def create_headers(url, mac, token, serial_number="", device_id=""):
        parts = urlsplit(url)
        # Use a robust string representation of local timezone
        try:
            timezone = str(tzlocal.get_localzone())
        except Exception:
            timezone = "UTC"

        # Build cookie string with optional serial_number and device_id
        cookie_parts = [
            f"mac={mac}",
            "stb_lang=en",
            f"timezone={timezone}",
            "PHPSESSID=null",
        ]
        if serial_number:
            cookie_parts.append(f"sn={serial_number}")
        if device_id:
            cookie_parts.append(f"device_id={device_id}")

        headers = {
            "User-Agent": STB_USER_AGENT,
            "Accept-Charset": "UTF-8,*;q=0.8",
            "X-User-Agent": "Model: MAG200; Link: Ethernet",
            "Host": parts.netloc,
            "Range": "bytes=0-",
            "Accept": "*/*",
            "Referer": f"{url}/c/" if not parts.path else f"{url}/",  # the URL as given
            "Cookie": "; ".join(cookie_parts) + ";",
            "Authorization": f"Bearer {token}",
        }
        return headers
