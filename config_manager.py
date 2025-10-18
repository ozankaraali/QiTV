import logging
import os
from pathlib import Path
import platform
import shutil
import sys
import tomllib

import orjson as json

from multikeydict import MultiKeyDict

try:
    from importlib import metadata as importlib_metadata
except Exception:  # pragma: no cover
    import importlib_metadata  # type: ignore

logger = logging.getLogger(__name__)


_APP_VERSION: str | None = None


def get_app_version() -> str:
    global _APP_VERSION
    if _APP_VERSION:
        return _APP_VERSION

    # 1) Try installed package metadata
    try:
        _APP_VERSION = importlib_metadata.version("qitv")
        return _APP_VERSION
    except Exception:
        pass

    # 2) Try reading pyproject.toml from common locations (repo root / CWD)
    candidates = [
        Path(__file__).resolve().parent.parent / "pyproject.toml",
        Path(__file__).resolve().parent / "pyproject.toml",
        Path.cwd() / "pyproject.toml",
    ]
    for p in candidates:
        try:
            if p.exists():
                with p.open("rb") as f:
                    data = tomllib.load(f)
                v = data.get("project", {}).get("version")
                if isinstance(v, str):
                    _APP_VERSION = v
                    return v
        except Exception as e:
            logger.debug(f"Unable to read version from {p}: {e}")

    # 3) Fallback
    if _APP_VERSION is None:
        _APP_VERSION = "0.0.0"
    return _APP_VERSION


class ConfigManager:

    DEFAULT_OPTION_CHECKUPDATE = True
    DEFAULT_OPTION_STB_CONTENT_INFO = False
    DEFAULT_OPTION_CHANNEL_EPG = False
    DEFAULT_OPTION_CHANNEL_LOGO = False
    DEFAULT_OPTION_MAX_CACHE_IMAGE_SIZE = 100
    DEFAULT_OPTION_EPG_SOURCE = "STB"  # Default EPG source
    DEFAULT_OPTION_EPG_URL = ""
    DEFAULT_OPTION_EPG_FILE = ""
    DEFAULT_OPTION_EPG_EXPIRATION_VALUE = 2
    DEFAULT_OPTION_EPG_EXPIRATION_UNIT = "Hours"

    def __init__(self):
        self.config = {}
        self.config_path = self._get_config_path()
        self._migrate_old_config()
        self.load_config()

    def _get_config_path(self):
        app_name = "qitv"
        if platform.system() == "Linux":
            config_dir = os.path.join(os.getenv("HOME", ""), f".config/{app_name}")
        elif platform.system() == "Darwin":  # macOS
            config_dir = os.path.join(
                os.getenv("HOME", ""), f"Library/Application Support/{app_name}"
            )
        elif platform.system() == "Windows":
            config_dir = os.path.join(os.getenv("APPDATA", ""), app_name)
        else:
            raise RuntimeError("Unsupported operating system")

        os.makedirs(config_dir, exist_ok=True)
        return os.path.join(config_dir, "config.json")

    def get_config_dir(self):
        return os.path.dirname(self.config_path)

    def _migrate_old_config(self):
        try:
            old_config_path = "config.json"
            if os.path.isfile(old_config_path) and not os.path.isfile(self.config_path):
                shutil.copy(old_config_path, self.config_path)
                os.remove(old_config_path)
        except Exception as e:
            logger.warning(f"Error during config migration: {e}")

    def load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = json.loads(f.read())
            if self.config is None:
                self.config = self.default_config()
        except (FileNotFoundError, json.JSONDecodeError):
            self.config = self.default_config()
            self.save_config()

        if isinstance(self.xmltv_channel_map, list):
            self.xmltv_channel_map = MultiKeyDict.deserialize(self.xmltv_channel_map)

        self.update_patcher()

    def update_patcher(self):

        need_update = False

        # add favorites to the loaded config if it doesn't exist
        if "favorites" not in self.config:
            self.favorites = []
            need_update = True

        # add check_updates to the loaded config if it doesn't exist
        if "check_updates" not in self.config:
            self.check_updates = ConfigManager.DEFAULT_OPTION_CHECKUPDATE
            need_update = True

        # add show_stb_content_info to the loaded config if it doesn't exist
        if "show_stb_content_info" not in self.config:
            self.show_stb_content_info = ConfigManager.DEFAULT_OPTION_STB_CONTENT_INFO
            need_update = True

        # add channel logo to the loaded config if it doesn't exist
        if "channel_logos" not in self.config:
            self.channel_logos = ConfigManager.DEFAULT_OPTION_CHANNEL_LOGO
            need_update = True

        # add max_cache_image_size to the loaded config if it doesn't exist
        if "max_cache_image_size" not in self.config:
            self.max_cache_image_size = ConfigManager.DEFAULT_OPTION_MAX_CACHE_IMAGE_SIZE
            need_update = True

        # add epg_source to the loaded config if it doesn't exist
        if "epg_source" not in self.config:
            self.epg_source = ConfigManager.DEFAULT_OPTION_EPG_SOURCE
            need_update = True

        # add epg_url to the loaded config if it doesn't exist
        if "epg_url" not in self.config:
            self.epg_url = ConfigManager.DEFAULT_OPTION_EPG_URL
            need_update = True

        # add epg_file to the loaded config if it doesn't exist
        if "epg_file" not in self.config:
            self.epg_file = ConfigManager.DEFAULT_OPTION_EPG_FILE
            need_update = True

        # add epg_expiration_value to the loaded config if it doesn't exist
        if "epg_expiration_value" not in self.config:
            self.epg_expiration_value = ConfigManager.DEFAULT_OPTION_EPG_EXPIRATION_VALUE
            need_update = True

        # add epg_expiration_unit to the loaded config if it doesn't exist
        if "epg_expiration_unit" not in self.config:
            self.epg_expiration_unit = ConfigManager.DEFAULT_OPTION_EPG_EXPIRATION_UNIT
            need_update = True

        # add xmltv_channel_map to the loaded config if it doesn't exist
        if "xmltv_channel_map" not in self.config:
            self.config["xmltv_channel_map"] = MultiKeyDict()
            need_update = True

        if need_update:
            self.save_config()

    @property
    def check_updates(self):
        return self.config.get("check_updates", ConfigManager.DEFAULT_OPTION_CHECKUPDATE)

    @check_updates.setter
    def check_updates(self, value):
        self.config["check_updates"] = value

    @property
    def favorites(self):
        return self.config.get("favorites", [])

    @favorites.setter
    def favorites(self, value):
        self.config["favorites"] = value

    @property
    def show_stb_content_info(self):
        return self.config.get(
            "show_stb_content_info", ConfigManager.DEFAULT_OPTION_STB_CONTENT_INFO
        )

    @show_stb_content_info.setter
    def show_stb_content_info(self, value):
        self.config["show_stb_content_info"] = value

    @property
    def selected_provider_name(self):
        return self.config.get("selected_provider_name", "iptv-org.github.io")

    @selected_provider_name.setter
    def selected_provider_name(self, value):
        self.config["selected_provider_name"] = value

    @property
    def channel_epg(self):
        return self.config.get("channel_epg", ConfigManager.DEFAULT_OPTION_CHANNEL_EPG)

    @channel_epg.setter
    def channel_epg(self, value):
        self.config["channel_epg"] = value

    @property
    def channel_logos(self):
        return self.config.get("channel_logos", ConfigManager.DEFAULT_OPTION_CHANNEL_LOGO)

    @channel_logos.setter
    def channel_logos(self, value):
        self.config["channel_logos"] = value

    @property
    def max_cache_image_size(self):
        return self.config.get(
            "max_cache_image_size", ConfigManager.DEFAULT_OPTION_MAX_CACHE_IMAGE_SIZE
        )

    @max_cache_image_size.setter
    def max_cache_image_size(self, value):
        self.config["max_cache_image_size"] = value

    @property
    def epg_source(self):
        return self.config.get("epg_source", ConfigManager.DEFAULT_OPTION_EPG_SOURCE)

    @epg_source.setter
    def epg_source(self, value):
        self.config["epg_source"] = value

    @property
    def epg_url(self):
        return self.config.get("epg_url", ConfigManager.DEFAULT_OPTION_EPG_URL)

    @epg_url.setter
    def epg_url(self, value):
        self.config["epg_url"] = value

    @property
    def epg_file(self):
        return self.config.get("epg_file", ConfigManager.DEFAULT_OPTION_EPG_FILE)

    @epg_file.setter
    def epg_file(self, value):
        self.config["epg_file"] = value

    @property
    def epg_expiration_value(self):
        return self.config.get(
            "epg_expiration_value", ConfigManager.DEFAULT_OPTION_EPG_EXPIRATION_VALUE
        )

    @epg_expiration_value.setter
    def epg_expiration_value(self, value):
        self.config["epg_expiration_value"] = value

    @property
    def epg_expiration_unit(self):
        return self.config.get(
            "epg_expiration_unit", ConfigManager.DEFAULT_OPTION_EPG_EXPIRATION_UNIT
        )

    @epg_expiration_unit.setter
    def epg_expiration_unit(self, value):
        self.config["epg_expiration_unit"] = value

    @property
    def epg_expiration(self):
        # Get expiration in seconds
        if self.epg_expiration_unit == "Months":
            return self.epg_expiration_value * 30 * 24 * 60 * 60  # Approximate month as 30 days
        elif self.epg_expiration_unit == "Days":
            return self.epg_expiration_value * 24 * 60 * 60
        elif self.epg_expiration_unit == "Hours":
            return self.epg_expiration_value * 60 * 60
        elif self.epg_expiration_unit == "Minutes":
            return self.epg_expiration_value * 60
        else:
            raise ValueError(f"Unsupported expiration unit: {self.epg_expiration_unit}")

    @property
    def xmltv_channel_map(self):
        return self.config.get("xmltv_channel_map", MultiKeyDict())

    @xmltv_channel_map.setter
    def xmltv_channel_map(self, value):
        self.config["xmltv_channel_map"] = value

    @staticmethod
    def default_config():
        return {
            "selected_provider_name": "iptv-org.github.io",
            "check_updates": ConfigManager.DEFAULT_OPTION_CHECKUPDATE,
            "data": [
                {
                    "type": "M3UPLAYLIST",
                    "name": "iptv-org.github.io",
                    "url": "https://iptv-org.github.io/iptv/index.m3u",
                }
            ],
            "window_positions": {
                "channel_list": {
                    "x": 1250,
                    "y": 100,
                    "width": 400,
                    "height": 800,
                    "splitter_ratio": 0.75,
                    "splitter_content_info_ratio": 0.33,
                },
                "video_player": {"x": 50, "y": 100, "width": 1200, "height": 800},
            },
            "favorites": [],
            "show_stb_content_info": ConfigManager.DEFAULT_OPTION_STB_CONTENT_INFO,
            "channel_logos": ConfigManager.DEFAULT_OPTION_CHANNEL_LOGO,
            "channel_epg": ConfigManager.DEFAULT_OPTION_CHANNEL_EPG,
            "xmltv_channel_map": MultiKeyDict(),
            "max_cache_image_size": ConfigManager.DEFAULT_OPTION_MAX_CACHE_IMAGE_SIZE,
        }

    def save_window_settings(self, window, window_name):
        pos = window.geometry()
        self.config["window_positions"][window_name] = {
            "x": pos.x(),
            "y": pos.y(),
            "width": pos.width(),
            "height": pos.height(),
        }
        if window_name == "channel_list":
            self.config["window_positions"][window_name]["splitter_ratio"] = window.splitter_ratio
            self.config["window_positions"][window_name][
                "splitter_content_info_ratio"
            ] = window.splitter_content_info_ratio

        self.save_config()

    def apply_window_settings(self, window_name, window):
        settings = self.config["window_positions"][window_name]
        window.setGeometry(settings["x"], settings["y"], settings["width"], settings["height"])
        if window_name == "channel_list":
            window.splitter_ratio = settings.get("splitter_ratio", 0.75)
            window.splitter_content_info_ratio = settings.get("splitter_content_info_ratio", 0.33)

    def save_config(self):
        self.xmltv_channel_map = self.xmltv_channel_map.serialize()

        serialized_config = json.dumps(self.config, option=json.OPT_INDENT_2)
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(serialized_config.decode("utf-8"))

        self.xmltv_channel_map = MultiKeyDict.deserialize(self.xmltv_channel_map)
