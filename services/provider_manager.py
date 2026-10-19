from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
import os
import random
import string
import threading
import time
from typing import Optional
from urllib.parse import urlencode, urlsplit

from PySide6.QtCore import QObject, Signal
import orjson as json
import tzlocal

from services.atomic_write import atomic_write
from services.lazy_import import lazy_module
from services.m3u import split_cached_groups
from services.provider_api import provider_prefers_https
from services.redact import safe_exc

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

# Why an STB handshake failed (StbHandshake.failure); the setup worker words it for the user.
HANDSHAKE_UNREACHABLE = "unreachable"  # DNS, connection, timeout or a server error
HANDSHAKE_REJECTED = "rejected"  # an explicit refusal (401/403, blocked) or no token
HANDSHAKE_UNEXPECTED = "unexpected"  # any other answer: a wrong path, not JSON
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
        self.current_provider = {}
        self.current_provider_content = {}
        self.token = ""
        self.headers = {}
        self.handshake_failure = None  # a HANDSHAKE_* reason after a failed STB setup
        self._cache_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="provider-cache")
        self._cache_lock = threading.Lock()
        self._cache_generations = {}
        self._pending_cache_snapshots = {}
        if providers is not None:
            self.providers = providers
        else:
            self._load_providers()

    def _provider_cache_name(self, provider_name):
        hashed_name = hashlib.sha256(provider_name.encode("utf-8")).hexdigest()
        return os.path.join(self.provider_dir, f"{hashed_name}.json")

    def _current_provider_cache_name(self):
        return self._provider_cache_name(self.current_provider["name"])

    def provider_cache_identity(self, provider, *, prefer_https=None, ssl_verify=None):
        """Identify the effective connection, without persisting credentials in metadata."""
        connection = {
            key: provider.get(key, "")
            for key in ("type", "url", "mac", "username", "password", "serial_number", "device_id")
        }
        connection["prefer_https"] = provider_prefers_https(
            provider,
            self.config_manager.prefer_https if prefer_https is None else prefer_https,
        )
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
        if cache is None:
            try:
                with open(cache_path, "rb") as f:
                    cache = json.loads(f.read())
            except OSError, json.JSONDecodeError:
                return {}
        if not isinstance(cache, dict):
            return {}
        metadata = cache.get("_cache")
        if metadata is not None:
            if not isinstance(metadata, dict):
                return {}
            if metadata.get("identity") != self.provider_cache_identity(provider):
                return {}
        if str(provider.get("type", "")).upper() == "M3UPLAYLIST" and "itv" in cache:
            # Cached before "A;B" group titles were split: split now, from the cache.
            cache["itv"] = split_cached_groups(cache["itv"])
        # Raw legacy payloads are readable, but have no trusted freshness timestamp.
        return cache

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
            with open(self.index_file, "r", encoding="utf-8") as f:
                self.providers = json.loads(f.read())
            if self.providers is None:
                self.providers = self.default_providers()
        except FileNotFoundError, json.JSONDecodeError:
            self.providers = self.default_providers()
            self.save_providers()

    @staticmethod
    def default_providers():
        return [
            {
                "type": "M3UPLAYLIST",
                "name": "iptv-org.github.io",
                "url": "https://iptv-org.github.io/iptv/index.m3u",
            }
        ]

    def _invalidate_cache_path(self, cache_path) -> None:
        """Supersede queued writes for this cache file, then delete it."""
        with self._cache_lock:
            self._cache_generations[cache_path] = self._cache_generations.get(cache_path, 0) + 1
            self._pending_cache_snapshots.pop(cache_path, None)
            try:
                os.remove(cache_path)
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
            playlist = str(provider.get("type", "")).upper() not in ("STB", "XTREAM")
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
        with open(self.index_file, "w", encoding="utf-8") as f:
            f.write(serialized.decode("utf-8"))

        # Delete stale cache files not matching any known provider name hash
        # (malformed provider entries without a string name are ignored).
        expected_files = {
            os.path.basename(self._provider_cache_name(p["name"]))
            for p in self.providers
            if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"]
        }

        with self._cache_lock:
            queued_files = {os.path.basename(path) for path in self._cache_generations}
        for entry in set(os.listdir(self.provider_dir)) | queued_files:
            # Only consider json cache files for pruning
            if entry == "index.json" or not entry.endswith(".json"):
                continue
            if entry not in expected_files:
                self._invalidate_cache_path(os.path.join(self.provider_dir, entry))

    @staticmethod
    def _snapshot_cache(value):
        # Copy mutable index maps, not potentially enormous channel/episode payloads.
        if isinstance(value, dict):
            return {key: ProviderManager._snapshot_cache(item) for key, item in value.items()}
        return value

    def save_provider(self):
        self._cache_metadata()
        snapshot = self._snapshot_cache(self.current_provider_content)
        cache_path = self._current_provider_cache_name()
        with self._cache_lock:
            generation = self._cache_generations.get(cache_path, 0) + 1
            self._cache_generations[cache_path] = generation
            self._pending_cache_snapshots[cache_path] = (generation, snapshot)
        self._cache_writer.submit(self._write_provider_cache, cache_path, generation, snapshot)

    def _write_provider_cache(self, cache_path, generation, snapshot):
        def replace_if_newest(temporary, path):  # a newer snapshot queued meanwhile wins
            with self._cache_lock:
                if self._cache_generations.get(cache_path, 0) == generation:
                    os.replace(temporary, path)

        try:
            with self._cache_lock:
                if self._cache_generations.get(cache_path, 0) != generation:
                    return
            serialized = json.dumps(snapshot)
            with atomic_write(cache_path, replace_if_newest) as f:
                f.write(serialized)
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
            prefer_https=provider_prefers_https(
                self.current_provider, self.config_manager.prefer_https
            ),
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
            if prefer_https and not _prefer_attempted and url.startswith("http://"):
                url = "https://" + url[len("http://") :]

            fetchurl = f"{url}{serverload}?type=stb&action=handshake&prehash={_PREHASH}&token=&JsHttpRequest=1-xml"
            handshake = requests.get(fetchurl, timeout=5, headers=self.headers, verify=ssl_verify)
            if handshake.status_code == 200:
                body = handshake.json()
            else:
                raise _PortalStatus("handshake", handshake.status_code)
            self.token = body["js"]["token"]
            self.headers["Authorization"] = f"Bearer {self.token}"

            # Use get_profile request to detect blocked providers
            fetchurl = self.profile_url(url, serverload, mac)
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
            http_retry = (
                prefer_https and not _prefer_attempted and original_url.startswith("http://")
            )
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
                return self.run(
                    original_url,
                    mac,
                    serverload,
                    serial_number,
                    device_id,
                    prefer_https_override=prefer_https_override,
                    ssl_verify_override=ssl_verify_override,
                    _prefer_attempted=True,
                )
            if not load_retry and not _quiet:
                logger.warning("STB handshake failed (%s): %s", self.failure, safe_exc(e))
            return False

    @staticmethod
    def profile_url(url, serverload, mac):
        """The portal's get_profile request, as a set-top box sends it after its handshake."""
        params = {
            "ver": "ImageDescription: 2.20.02-pub-424; ImageDate: Fri May 8 15:39:55 UTC 2020; PORTAL version: 5.3.0; API Version: JS API version: 343; STB API version: 146; Player Engine version: 0x588",
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
            "metrics": f'{{"mac":"{mac}", "sn":"062014N067770","model":"MAG424","type":"STB","uid":"","random":""}}',
            "hw_version_2": "bb8b74cdcaa19c7f6a6bdfecc8e91b7e4b5ea556",
            "timestamp": "1729441259",
            "api_signature": "262",
            "prehash": _PREHASH,
        }
        encoded_params = urlencode(params)
        return f"{url}{serverload}?type=stb&action=get_profile&hd=1&{encoded_params}&JsHttpRequest=1-xml"

    def check_profile(self, url, mac, headers):
        """A profile request on a session that is signed in already (`headers`, with its
        token): no handshake, so the portal keeps the token that session plays with (portals
        bind one token to a MAC). True when the portal knows the device; `statuses`,
        `timed_out` and `profile` record what came, as run() records them."""
        self.headers = dict(headers)
        bases = [url]
        if self.prefer_https and url.startswith("http://"):
            bases.insert(0, "https://" + url[len("http://") :])
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
            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
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
