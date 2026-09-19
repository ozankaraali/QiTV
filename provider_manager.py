from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
import os
import random
import string
import tempfile
import threading
import time
from urllib.parse import urlencode

from PySide6.QtCore import QObject, Signal
import orjson as json
import requests
import tzlocal
from urlobject import URLObject

logger = logging.getLogger(__name__)


class ProviderManager(QObject):
    progress = Signal(str)

    CONTENT_CACHE_TTL = 6 * 60 * 60

    def __init__(self, config_manager):
        super().__init__()
        self.config_manager = config_manager
        self.provider_dir = os.path.join(config_manager.get_config_dir(), "cache", "provider")
        os.makedirs(self.provider_dir, exist_ok=True)
        self.index_file = os.path.join(self.provider_dir, "index.json")
        self.providers = []
        self.current_provider = {}
        self.current_provider_content = {}
        self.token = ""
        self.headers = {}
        self._cache_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="provider-cache")
        self._cache_lock = threading.Lock()
        self._cache_generations = {}
        self._pending_cache_snapshots = {}
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
        connection["prefer_https"] = provider.get(
            "prefer_https",
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
            except (OSError, json.JSONDecodeError):
                return {}
        if not isinstance(cache, dict):
            return {}
        metadata = cache.get("_cache")
        if metadata is not None:
            if not isinstance(metadata, dict):
                return {}
            if metadata.get("identity") != self.provider_cache_identity(provider):
                return {}
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
        except (FileNotFoundError, json.JSONDecodeError):
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

    def invalidate_provider_cache(self, provider_name) -> None:
        """Invalidate queued writes as well as the on-disk cache for this name."""
        cache_path = self._provider_cache_name(provider_name)
        with self._cache_lock:
            self._cache_generations[cache_path] = self._cache_generations.get(cache_path, 0) + 1
            self._pending_cache_snapshots.pop(cache_path, None)
            try:
                os.remove(cache_path)
            except FileNotFoundError:
                pass
        if self.current_provider.get("name") == provider_name:
            self.current_provider_content = {}

    def clear_current_provider_cache(self):
        self.invalidate_provider_cache(self.current_provider["name"])

    def set_current_provider(self, progress_callback):
        progress_callback.emit("Searching for provider...")
        self.current_provider = {}
        # search for provider in the list
        if self.config_manager.selected_provider_name:
            for provider in self.providers:
                if provider["name"] == self.config_manager.selected_provider_name:
                    self.current_provider = provider
                    break

        # if provider not found, set the first one
        if not self.current_provider:
            self.current_provider = self.providers[0]

        progress_callback.emit("Loading provider content...")
        self.current_provider_content = self._load_provider_cache(self.current_provider)

        if self.current_provider["type"] == "STB":
            progress_callback.emit("Performing handshake...")
            self.token = ""
            self.do_handshake(
                self.current_provider["url"],
                self.current_provider["mac"],
                serial_number=self.current_provider.get("serial_number", ""),
                device_id=self.current_provider.get("device_id", ""),
            )

        progress_callback.emit("Provider setup complete.")

    def get_all_providers_cached_content(self):
        """Return cached content for all configured providers.

        Returns list of (provider_name, content_dict) tuples.
        Only includes providers that have been loaded/cached.
        """
        results = []
        for provider in self.providers:
            name = provider.get("name", "Unknown")
            cache = self._load_provider_cache(provider)
            content = {key: value for key, value in cache.items() if key != "_cache"}
            if content:
                results.append((name, content))
        return results

    def save_providers(self):
        serialized = json.dumps(self.providers, option=json.OPT_INDENT_2)
        with open(self.index_file, "w", encoding="utf-8") as f:
            f.write(serialized.decode("utf-8"))

        # Delete stale cache files not matching any known provider name hash
        expected_files = set()
        for p in self.providers:
            try:
                name = p.get("name") if isinstance(p, dict) else None
                if name:
                    expected_files.add(f"{hashlib.sha256(name.encode('utf-8')).hexdigest()}.json")
            except Exception:
                # ignore malformed entries
                pass

        with self._cache_lock:
            queued_files = {os.path.basename(path) for path in self._cache_generations}
        for entry in set(os.listdir(self.provider_dir)) | queued_files:
            if entry == "index.json":
                continue
            # Only consider json cache files for pruning
            if not entry.endswith(".json"):
                continue
            if entry not in expected_files:
                cache_path = os.path.join(self.provider_dir, entry)
                with self._cache_lock:
                    self._cache_generations[cache_path] = (
                        self._cache_generations.get(cache_path, 0) + 1
                    )
                    self._pending_cache_snapshots.pop(cache_path, None)
                    try:
                        os.remove(cache_path)
                    except FileNotFoundError:
                        pass

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
        temporary_path = None
        try:
            with self._cache_lock:
                if self._cache_generations.get(cache_path, 0) != generation:
                    return
            serialized = json.dumps(snapshot)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.provider_dir, suffix=".tmp", delete=False
            ) as f:
                temporary_path = f.name
                f.write(serialized)
            with self._cache_lock:
                if self._cache_generations.get(cache_path, 0) == generation:
                    os.replace(temporary_path, cache_path)
                    temporary_path = None
        except (OSError, TypeError):
            logger.exception("Failed to save provider cache")
        finally:
            with self._cache_lock:
                pending = self._pending_cache_snapshots.get(cache_path)
                if pending is not None and pending[0] == generation:
                    del self._pending_cache_snapshots[cache_path]
            if temporary_path is not None:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass

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
            prefer_https=self.current_provider.get(
                "prefer_https", self.config_manager.prefer_https
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


class StbHandshake:
    """An isolated STB session, usable for setup or background provider verification."""

    def __init__(self, *, prefer_https=False, ssl_verify=True, token=""):
        self.prefer_https = prefer_https
        self.ssl_verify = ssl_verify
        self.token = token
        self.headers = {}

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

            prehash = "2614ddf9829ba9d284f389d88e8c669d81f6a5c2"
            fetchurl = f"{url}{serverload}?type=stb&action=handshake&prehash={prehash}&token=&JsHttpRequest=1-xml"
            handshake = requests.get(fetchurl, timeout=5, headers=self.headers, verify=ssl_verify)
            if handshake.status_code == 200:
                body = handshake.json()
            else:
                raise Exception(f"Failed to fetch handshake: {handshake.status_code}")
            self.token = body["js"]["token"]
            self.headers["Authorization"] = f"Bearer {self.token}"

            # Use get_profile request to detect blocked providers

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
                "prehash": {prehash},
            }
            encoded_params = urlencode(params)

            fetchurl = f"{url}{serverload}?type=stb&action=get_profile&hd=1&{encoded_params}&JsHttpRequest=1-xml"
            profile = requests.get(fetchurl, timeout=5, headers=self.headers, verify=ssl_verify)
            if profile.status_code == 200:
                body = profile.json()
            else:
                raise Exception(f"Failed to fetch profile: {profile.status_code}")

            theId = body["js"]["id"]
            theName = body["js"]["name"]
            if not theId and not theName:
                raise Exception("Provider is blocked")

            return True
        except Exception as e:
            if serverload != "/server/load.php" and "handshake" in fetchurl:
                serverload = "/server/load.php"
                return self.run(
                    url,
                    mac,
                    serverload,
                    serial_number,
                    device_id,
                    prefer_https_override=prefer_https_override,
                    ssl_verify_override=ssl_verify_override,
                    _prefer_attempted=_prefer_attempted,
                )
            # If HTTPS attempt failed and we preferred HTTPS, fall back to HTTP once
            if prefer_https and not _prefer_attempted and original_url.startswith("http://"):
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
            logger.warning("Error in handshake: %s", e)
            return False

    @staticmethod
    def random_token():
        return "".join(random.choices(string.ascii_letters + string.digits, k=32))

    @staticmethod
    def create_headers(url, mac, token, serial_number="", device_id=""):
        url = URLObject(url)
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
            "Host": f"{url.netloc}",
            "Range": "bytes=0-",
            "Accept": "*/*",
            "Referer": f"{url}/c/" if not url.path else f"{url}/",
            "Cookie": "; ".join(cookie_parts) + ";",
            "Authorization": f"Bearer {token}",
        }
        return headers
