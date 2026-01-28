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


def _config_property(key: str, default, *, coerce=None, clamp=None):
    """
    Factory to generate config property getter/setter pairs.

    Args:
        key: The config dictionary key
        default: Default value if key is missing
        coerce: Optional callable to coerce values (e.g., bool, int)
        clamp: Optional (min, max) tuple for clamping numeric values
    """

    def getter(self):
        value = self.config.get(key, default)
        if coerce is not None:
            try:
                value = coerce(value)
            except Exception:
                value = default
        if clamp is not None:
            value = max(clamp[0], min(clamp[1], value))
        return value

    def setter(self, value):
        if coerce is not None:
            try:
                value = coerce(value)
            except Exception:
                value = default
        self.config[key] = value

    return property(getter, setter)


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

    # 3) If running as PyInstaller bundle, check for bundled pyproject.toml
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # Running in a PyInstaller bundle
        bundled_pyproject = Path(sys._MEIPASS) / "pyproject.toml"
        candidates.insert(0, bundled_pyproject)

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

    # 4) Fallback
    if _APP_VERSION is None:
        _APP_VERSION = "0.0.0"
    return _APP_VERSION


class ConfigManager:

    DEFAULT_OPTION_CHECKUPDATE = True
    DEFAULT_OPTION_STB_CONTENT_INFO = True
    DEFAULT_OPTION_CHANNEL_EPG = False
    DEFAULT_OPTION_CHANNEL_LOGO = False
    DEFAULT_OPTION_MAX_CACHE_IMAGE_SIZE = 100
    DEFAULT_OPTION_EPG_SOURCE = "STB"  # Default EPG source
    DEFAULT_OPTION_EPG_URL = ""
    DEFAULT_OPTION_EPG_FILE = ""
    DEFAULT_OPTION_EPG_EXPIRATION_VALUE = 2
    DEFAULT_OPTION_EPG_EXPIRATION_UNIT = "Hours"
    DEFAULT_OPTION_PREFER_HTTPS = False
    DEFAULT_OPTION_SSL_VERIFY = True
    DEFAULT_OPTION_KEYBOARD_REMOTE_MODE = False
    DEFAULT_OPTION_EPG_LIST_WINDOW_HOURS = 24  # 0 = unlimited
    DEFAULT_OPTION_EPG_STB_PERIOD_HOURS = 5
    DEFAULT_OPTION_SMOOTH_PAUSED_SEEK = True
    DEFAULT_OPTION_PLAY_IN_VLC = False
    DEFAULT_OPTION_PLAY_IN_MPV = False

    def __init__(self):
        self.config = {}
        self.config_path = self._get_config_path()
        self._migrate_old_config()
        self.load_config()

    def _get_config_path(self):
        # Check for portable mode (portable.txt file in program directory)
        # Get the directory where the script/executable is located
        if getattr(sys, 'frozen', False):
            # Running as compiled executable (PyInstaller)
            program_dir = os.path.dirname(sys.executable)
        else:
            # Running as script
            program_dir = os.path.dirname(os.path.abspath(__file__))

        portable_flag = os.path.join(program_dir, "portable.txt")

        if os.path.exists(portable_flag):
            # Portable mode: use program directory
            config_dir = program_dir
        else:
            # Normal mode: use system-specific directories
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

        # add last_watched to the loaded config if it doesn't exist
        if "last_watched" not in self.config:
            self.last_watched = None
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

        # add network security options if missing
        if "prefer_https" not in self.config:
            self.prefer_https = ConfigManager.DEFAULT_OPTION_PREFER_HTTPS
            need_update = True
        if "ssl_verify" not in self.config:
            self.ssl_verify = ConfigManager.DEFAULT_OPTION_SSL_VERIFY
            need_update = True

        # add keyboard_remote_mode if missing
        if "keyboard_remote_mode" not in self.config:
            self.keyboard_remote_mode = ConfigManager.DEFAULT_OPTION_KEYBOARD_REMOTE_MODE
            need_update = True
        # add smooth_paused_seek if missing
        if "smooth_paused_seek" not in self.config:
            self.smooth_paused_seek = ConfigManager.DEFAULT_OPTION_SMOOTH_PAUSED_SEEK
            need_update = True
        # add epg_list_window_hours if missing
        if "epg_list_window_hours" not in self.config:
            self.epg_list_window_hours = ConfigManager.DEFAULT_OPTION_EPG_LIST_WINDOW_HOURS
            need_update = True
        # add epg_stb_period_hours if missing
        if "epg_stb_period_hours" not in self.config:
            self.epg_stb_period_hours = ConfigManager.DEFAULT_OPTION_EPG_STB_PERIOD_HOURS
            need_update = True

        if need_update:
            self.save_config()

    # Simple config properties using factory
    check_updates = _config_property("check_updates", DEFAULT_OPTION_CHECKUPDATE)
    favorites = _config_property("favorites", [])
    last_watched = _config_property("last_watched", None)
    show_stb_content_info = _config_property(
        "show_stb_content_info", DEFAULT_OPTION_STB_CONTENT_INFO
    )
    selected_provider_name = _config_property("selected_provider_name", "iptv-org.github.io")
    channel_epg = _config_property("channel_epg", DEFAULT_OPTION_CHANNEL_EPG)
    channel_logos = _config_property("channel_logos", DEFAULT_OPTION_CHANNEL_LOGO)
    max_cache_image_size = _config_property(
        "max_cache_image_size", DEFAULT_OPTION_MAX_CACHE_IMAGE_SIZE
    )
    epg_source = _config_property("epg_source", DEFAULT_OPTION_EPG_SOURCE)
    epg_url = _config_property("epg_url", DEFAULT_OPTION_EPG_URL)
    epg_file = _config_property("epg_file", DEFAULT_OPTION_EPG_FILE)
    epg_expiration_value = _config_property(
        "epg_expiration_value", DEFAULT_OPTION_EPG_EXPIRATION_VALUE
    )
    epg_expiration_unit = _config_property(
        "epg_expiration_unit", DEFAULT_OPTION_EPG_EXPIRATION_UNIT
    )

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

    xmltv_channel_map = _config_property("xmltv_channel_map", MultiKeyDict())

    # Boolean config properties using factory with coercion
    prefer_https = _config_property("prefer_https", DEFAULT_OPTION_PREFER_HTTPS, coerce=bool)
    ssl_verify = _config_property("ssl_verify", DEFAULT_OPTION_SSL_VERIFY, coerce=bool)
    keyboard_remote_mode = _config_property(
        "keyboard_remote_mode", DEFAULT_OPTION_KEYBOARD_REMOTE_MODE, coerce=bool
    )
    smooth_paused_seek = _config_property(
        "smooth_paused_seek", DEFAULT_OPTION_SMOOTH_PAUSED_SEEK, coerce=bool
    )
    play_in_vlc = _config_property("play_in_vlc", DEFAULT_OPTION_PLAY_IN_VLC, coerce=bool)
    play_in_mpv = _config_property("play_in_mpv", DEFAULT_OPTION_PLAY_IN_MPV, coerce=bool)

    @staticmethod
    def default_config():
        return {
            "selected_provider_name": "iptv-org.github.io",
            "check_updates": ConfigManager.DEFAULT_OPTION_CHECKUPDATE,
            "prefer_https": ConfigManager.DEFAULT_OPTION_PREFER_HTTPS,
            "ssl_verify": ConfigManager.DEFAULT_OPTION_SSL_VERIFY,
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
            "keyboard_remote_mode": ConfigManager.DEFAULT_OPTION_KEYBOARD_REMOTE_MODE,
            "smooth_paused_seek": ConfigManager.DEFAULT_OPTION_SMOOTH_PAUSED_SEEK,
            "epg_list_window_hours": ConfigManager.DEFAULT_OPTION_EPG_LIST_WINDOW_HOURS,
            "epg_stb_period_hours": ConfigManager.DEFAULT_OPTION_EPG_STB_PERIOD_HOURS,
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

    # Integer config properties using factory with coercion
    epg_list_window_hours = _config_property(
        "epg_list_window_hours", DEFAULT_OPTION_EPG_LIST_WINDOW_HOURS, coerce=int
    )
    epg_stb_period_hours = _config_property(
        "epg_stb_period_hours", DEFAULT_OPTION_EPG_STB_PERIOD_HOURS, coerce=int, clamp=(1, 168)
    )
