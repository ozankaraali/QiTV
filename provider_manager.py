import hashlib
import logging
import os
import random
import string
from urllib.parse import urlencode

from PySide6.QtCore import QObject, Signal
import orjson as json
import requests
import tzlocal
from urlobject import URLObject

logger = logging.getLogger(__name__)


class ProviderManager(QObject):
    progress = Signal(str)

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
        self._load_providers()

    def _current_provider_cache_name(self):
        hashed_name = hashlib.sha256(self.current_provider["name"].encode("utf-8")).hexdigest()
        return os.path.join(self.provider_dir, f"{hashed_name}.json")

    def _load_providers(self):
        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                self.providers = json.loads(f.read())
            if self.providers is None:
                self.providers = self.default_providers()
        except (FileNotFoundError, json.JSONDecodeError):
            self.providers = self.default_providers()
            self.save_providers()

    def clear_current_provider_cache(self):
        try:
            os.remove(self._current_provider_cache_name())
        except FileNotFoundError:
            pass
        self.current_provider_content = {}

    def set_current_provider(self, progress_callback):
        progress_callback.emit("Searching for provider...")
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
        try:
            with open(self._current_provider_cache_name(), "r", encoding="utf-8") as f:
                self.current_provider_content = json.loads(f.read())
        except (FileNotFoundError, json.JSONDecodeError):
            self.current_provider_content = {}

        if self.current_provider["type"] == "STB":
            progress_callback.emit("Performing handshake...")
            self.token = ""
            self.do_handshake(self.current_provider["url"], self.current_provider["mac"])

        progress_callback.emit("Provider setup complete.")

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

        for entry in os.listdir(self.provider_dir):
            if entry == "index.json":
                continue
            # Only consider json cache files for pruning
            if not entry.endswith(".json"):
                continue
            if entry not in expected_files:
                try:
                    os.remove(os.path.join(self.provider_dir, entry))
                except FileNotFoundError:
                    pass

    def save_provider(self):
        serialized = json.dumps(self.current_provider_content, option=json.OPT_INDENT_2)
        with open(self._current_provider_cache_name(), "w", encoding="utf-8") as f:
            f.write(serialized.decode("utf-8"))

    def do_handshake(self, url, mac, serverload="/portal.php"):
        self.token = self.token if self.token else self.random_token()
        self.headers = self.create_headers(url, mac, self.token)
        try:
            prehash = "2614ddf9829ba9d284f389d88e8c669d81f6a5c2"
            fetchurl = f"{url}{serverload}?type=stb&action=handshake&prehash={prehash}&token=&JsHttpRequest=1-xml"
            handshake = requests.get(fetchurl, timeout=5, headers=self.headers)
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
            profile = requests.get(fetchurl, timeout=5, headers=self.headers)
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
                return self.do_handshake(url, mac, serverload)
            logger.warning("Error in handshake: %s", e)
            return False

    @staticmethod
    def default_providers():
        return [
            {
                "type": "M3UPLAYLIST",
                "name": "iptv-org.github.io",
                "url": "https://iptv-org.github.io/iptv/index.m3u",
            }
        ]

    @staticmethod
    def random_token():
        return "".join(random.choices(string.ascii_letters + string.digits, k=32))

    @staticmethod
    def create_headers(url, mac, token):
        url = URLObject(url)
        # Use a robust string representation of local timezone
        try:
            timezone = str(tzlocal.get_localzone())
        except Exception:
            timezone = "UTC"
        headers = {
            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
            "Accept-Charset": "UTF-8,*;q=0.8",
            "X-User-Agent": "Model: MAG200; Link: Ethernet",
            "Host": f"{url.netloc}",
            "Range": "bytes=0-",
            "Accept": "*/*",
            "Referer": f"{url}/c/" if not url.path else f"{url}/",
            "Cookie": f"mac={mac}; stb_lang=en; timezone={timezone}; PHPSESSID=null;",
            "Authorization": f"Bearer {token}",
        }
        return headers
