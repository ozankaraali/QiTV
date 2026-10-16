from datetime import datetime
from importlib import metadata as importlib_metadata
import logging
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import time
import tomllib

import orjson as json

from services.multikeydict import MultiKeyDict
from services.redact import safe_message

logger = logging.getLogger(__name__)

# Swapping config.json in can fail while Windows antivirus or the indexer holds the file
# open: retry briefly before giving up.
_REPLACE_RETRIES = 5
_REPLACE_RETRY_DELAY_S = 0.05
# After a failed save, saves try once without those retries for this long: a file that stays
# locked must not stall every save (a settled zap's among them) by the whole retry wait.
SAVE_BACKOFF_S = 30.0


def _one_of(choices, default):
    """Coerce a stored value to one of `choices`, falling back to `default`."""

    def coerce(value):
        return value if value in choices else default

    return coerce


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

    # 2) Try reading pyproject.toml from common locations (repo root, beside main.py / CWD)
    candidates = [
        Path(__file__).resolve().parent.parent / "pyproject.toml",
        Path.cwd() / "pyproject.toml",
    ]

    # 3) If running as PyInstaller bundle, check for bundled pyproject.toml
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
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
    _APP_VERSION = "0.0.0"
    return _APP_VERSION


class ConfigManager:
    DEFAULT_OPTION_CHECKUPDATE = True
    DEFAULT_OPTION_CHANNEL_EPG = True
    DEFAULT_OPTION_CHANNEL_LOGO = True
    DEFAULT_OPTION_MAX_CACHE_IMAGE_SIZE = 100
    DEFAULT_OPTION_EPG_SOURCE = "STB"  # Default EPG source
    DEFAULT_OPTION_EPG_URL = ""
    DEFAULT_OPTION_EPG_FILE = ""
    DEFAULT_OPTION_EPG_EXPIRATION_VALUE = 2
    DEFAULT_OPTION_EPG_EXPIRATION_UNIT = "Hours"
    EPG_EXPIRATION_SECONDS = {  # the guide's update period, per unit
        "Minutes": 60,  # no longer offered, still read
        "Hours": 60 * 60,
        "Days": 24 * 60 * 60,
        "Weeks": 7 * 24 * 60 * 60,
        "Months": 30 * 24 * 60 * 60,  # approximate month as 30 days
    }
    EPG_EXPIRATION_CHOICES = ("Hours", "Days", "Weeks", "Months")  # what Settings offers
    DEFAULT_OPTION_PREFER_HTTPS = False
    DEFAULT_OPTION_SSL_VERIFY = True
    # Auto (2.0.2) learns each channel's latency; a latency saved before stays as it was.
    DEFAULT_OPTION_LIVE_LATENCY = "auto"
    LIVE_LATENCY_CHOICES = ("auto", "low", "normal", "stable")
    DEFAULT_OPTION_MOUSE_WHEEL = "channel"
    MOUSE_WHEEL_CHOICES = ("channel", "volume")
    DEFAULT_OPTION_START_WITH_LAST_CHANNEL = True
    DEFAULT_OPTION_TIMESHIFT_ENABLED = False
    DEFAULT_OPTION_TIMESHIFT_MAX_MIB = 2048
    DEFAULT_OPTION_TIMESHIFT_RAM_MIB = 256  # memory per stream not on the disk ring
    TIMESHIFT_RAM_CHOICES = (128, 256, 512)
    DEFAULT_OPTION_PIP_CORNER = "br"  # picture in picture: the small picture's corner
    PIP_CORNER_CHOICES = ("br", "bl", "tr", "tl")
    DEFAULT_OPTION_PIP_VOLUME = 30  # the small picture's own level once it is made audible
    TV_DEFAULTS_VERSION = 2
    DEFAULT_TV_WINDOW = {"x": 80, "y": 80, "width": 1280, "height": 720}
    # A 1.x window smaller than this, or not landscape, does not size the TV window.
    MIN_REUSED_WINDOW = (640, 360)
    DEFAULT_OPTION_EPG_LIST_WINDOW_HOURS = 24  # 0 = unlimited
    DEFAULT_OPTION_EPG_STB_PERIOD_HOURS = 5
    DEFAULT_OPTION_PLAY_IN_VLC = False
    DEFAULT_OPTION_PLAY_IN_MPV = False
    # Auto-play settings
    DEFAULT_OPTION_AUTO_PLAY_EPISODES = True
    DEFAULT_OPTION_AUTO_PLAY_MOVIES = True
    DEFAULT_WATCH_HISTORY_LIMIT = 100
    DEFAULT_WATCHED_THRESHOLD = 0.90  # 90% = watched

    def __init__(self):
        self.config = {}
        self._save_failed_at: float | None = None  # monotonic time of the last failed save
        self.config_path = self._get_config_path()
        os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
        self._migrate_old_config()
        self.load_config()

    def _get_config_path(self):
        """Where config.json lives; resolving it creates nothing (__init__ makes the dir)."""
        override = os.environ.get("QITV_CONFIG_DIR")
        if override:
            return os.path.join(override, "config.json")
        # Check for portable mode (portable.txt file in program directory)
        # Get the directory where the script/executable is located
        if getattr(sys, "frozen", False):
            # Running as compiled executable (PyInstaller)
            program_dir = os.path.dirname(sys.executable)
        else:
            # Running as script: the program directory holds main.py, one above services/
            program_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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

        return os.path.join(config_dir, "config.json")

    def save_failed(self) -> bool:
        """True while the last save failed (the window says so when it starts)."""
        return self._save_failed_at is not None

    def _save_failed_recently(self) -> bool:
        failed_at = self._save_failed_at
        return failed_at is not None and time.monotonic() - failed_at < SAVE_BACKOFF_S

    def get_config_dir(self):
        return os.path.dirname(self.config_path)

    def _migrate_old_config(self):
        if os.environ.get("QITV_CONFIG_DIR"):
            return
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
        except FileNotFoundError, json.JSONDecodeError:
            self.config = self.default_config()
            self._save_at_start()

        if isinstance(self.xmltv_channel_map, list):
            self.xmltv_channel_map = MultiKeyDict.deserialize(self.xmltv_channel_map)

        self.update_patcher()

    def update_patcher(self):
        need_update = False

        # 1.x windows stay listed so going back to 1.x finds them (1.13.7 failed without).
        window_positions = self.config.setdefault("window_positions", {})
        if "tv_window" not in window_positions:
            window_positions["tv_window"] = self._initial_tv_window(window_positions)
            need_update = True
        for window_name, settings in self.default_config()["window_positions"].items():
            if window_name not in window_positions:
                window_positions[window_name] = settings
                need_update = True

        # Add settings introduced after the loaded config was written.
        missing_defaults: dict[str, object] = {
            "favorites": [],
            "last_watched": None,
            "check_updates": ConfigManager.DEFAULT_OPTION_CHECKUPDATE,
            "channel_logos": ConfigManager.DEFAULT_OPTION_CHANNEL_LOGO,
            "max_cache_image_size": ConfigManager.DEFAULT_OPTION_MAX_CACHE_IMAGE_SIZE,
            "epg_source": ConfigManager.DEFAULT_OPTION_EPG_SOURCE,
            "epg_url": ConfigManager.DEFAULT_OPTION_EPG_URL,
            "epg_file": ConfigManager.DEFAULT_OPTION_EPG_FILE,
            "epg_expiration_value": ConfigManager.DEFAULT_OPTION_EPG_EXPIRATION_VALUE,
            "epg_expiration_unit": ConfigManager.DEFAULT_OPTION_EPG_EXPIRATION_UNIT,
            "xmltv_channel_map": MultiKeyDict(),
            "prefer_https": ConfigManager.DEFAULT_OPTION_PREFER_HTTPS,
            "ssl_verify": ConfigManager.DEFAULT_OPTION_SSL_VERIFY,
            "epg_list_window_hours": ConfigManager.DEFAULT_OPTION_EPG_LIST_WINDOW_HOURS,
            "epg_stb_period_hours": ConfigManager.DEFAULT_OPTION_EPG_STB_PERIOD_HOURS,
            "live_latency": ConfigManager.DEFAULT_OPTION_LIVE_LATENCY,
            "mouse_wheel_action": ConfigManager.DEFAULT_OPTION_MOUSE_WHEEL,
            "start_with_last_channel": ConfigManager.DEFAULT_OPTION_START_WITH_LAST_CHANNEL,
            "last_live_channel": None,
            "timeshift_enabled": ConfigManager.DEFAULT_OPTION_TIMESHIFT_ENABLED,
            "timeshift_max_mib": ConfigManager.DEFAULT_OPTION_TIMESHIFT_MAX_MIB,
            "timeshift_ram_mib": ConfigManager.DEFAULT_OPTION_TIMESHIFT_RAM_MIB,
        }
        if "timeshift_minutes" in self.config:  # the time limit was retired for a size limit
            del self.config["timeshift_minutes"]
            need_update = True
        for key, value in missing_defaults.items():
            if key not in self.config:
                self.config[key] = value
                need_update = True

        # Each step runs once; a choice the user makes afterwards stays.
        tv_defaults = self.config.get("tv_defaults_version", 0)
        if tv_defaults < 1:
            # 2.0 is a TV: the zap banner and channel list need logos and now/next.
            self.config["channel_logos"] = True
            self.config["channel_epg"] = True
        if tv_defaults < 2:
            # 1.x's "play in VLC/MPV" would bypass the TV screen: players are picked in-app.
            self.config["play_in_vlc"] = False
            self.config["play_in_mpv"] = False
        if tv_defaults < ConfigManager.TV_DEFAULTS_VERSION:
            self.config["tv_defaults_version"] = ConfigManager.TV_DEFAULTS_VERSION
            need_update = True

        if need_update:
            self._save_at_start()

    def _save_at_start(self) -> None:
        """Save while starting: a locked or read-only config.json must not stop QiTV.

        The settings apply all the same; save_failed() tells the window to say so.
        """
        try:
            self.save_config()
        except Exception as e:
            logger.warning("Couldn't save the settings: %s", safe_message(e))

    # Simple config properties using factory
    check_updates = _config_property("check_updates", DEFAULT_OPTION_CHECKUPDATE)
    skipped_update_version = _config_property("skipped_update_version", "")
    favorites = _config_property("favorites", [])
    last_watched = _config_property("last_watched", None)
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
        "epg_expiration_unit",
        DEFAULT_OPTION_EPG_EXPIRATION_UNIT,
        # Settings spelled it "Monthes" until 2.0: read and saved as "Months".
        coerce=lambda unit: "Months" if unit == "Monthes" else unit,
    )

    @property
    def epg_expiration(self):
        """The guide's update period in seconds."""
        seconds = ConfigManager.EPG_EXPIRATION_SECONDS.get(self.epg_expiration_unit)
        if seconds is None:
            raise ValueError(f"Unsupported expiration unit: {self.epg_expiration_unit}")
        return self.epg_expiration_value * seconds

    xmltv_channel_map = _config_property("xmltv_channel_map", MultiKeyDict())

    # Boolean config properties using factory with coercion
    prefer_https = _config_property("prefer_https", DEFAULT_OPTION_PREFER_HTTPS, coerce=bool)
    ssl_verify = _config_property("ssl_verify", DEFAULT_OPTION_SSL_VERIFY, coerce=bool)
    play_in_vlc = _config_property("play_in_vlc", DEFAULT_OPTION_PLAY_IN_VLC, coerce=bool)
    play_in_mpv = _config_property("play_in_mpv", DEFAULT_OPTION_PLAY_IN_MPV, coerce=bool)

    # Auto-play settings
    auto_play_episodes = _config_property(
        "auto_play_episodes", DEFAULT_OPTION_AUTO_PLAY_EPISODES, coerce=bool
    )
    auto_play_movies = _config_property(
        "auto_play_movies", DEFAULT_OPTION_AUTO_PLAY_MOVIES, coerce=bool
    )
    live_latency = _config_property(
        "live_latency",
        DEFAULT_OPTION_LIVE_LATENCY,
        coerce=_one_of(LIVE_LATENCY_CHOICES, DEFAULT_OPTION_LIVE_LATENCY),
    )
    mouse_wheel_action = _config_property(
        "mouse_wheel_action",
        DEFAULT_OPTION_MOUSE_WHEEL,
        coerce=_one_of(MOUSE_WHEEL_CHOICES, DEFAULT_OPTION_MOUSE_WHEEL),
    )
    start_with_last_channel = _config_property(
        "start_with_last_channel", DEFAULT_OPTION_START_WITH_LAST_CHANNEL, coerce=bool
    )
    last_live_channel = _config_property("last_live_channel", None)
    timeshift_enabled = _config_property(
        "timeshift_enabled", DEFAULT_OPTION_TIMESHIFT_ENABLED, coerce=lambda value: value is True
    )
    timeshift_max_mib = _config_property(
        "timeshift_max_mib", DEFAULT_OPTION_TIMESHIFT_MAX_MIB, coerce=int, clamp=(256, 16384)
    )
    timeshift_ram_mib = _config_property(
        "timeshift_ram_mib",
        DEFAULT_OPTION_TIMESHIFT_RAM_MIB,
        coerce=lambda value: _one_of(
            ConfigManager.TIMESHIFT_RAM_CHOICES, ConfigManager.DEFAULT_OPTION_TIMESHIFT_RAM_MIB
        )(int(value)),
    )
    pip_corner = _config_property(
        "pip_corner",
        DEFAULT_OPTION_PIP_CORNER,
        coerce=_one_of(PIP_CORNER_CHOICES, DEFAULT_OPTION_PIP_CORNER),
    )
    pip_volume = _config_property(
        "pip_volume", DEFAULT_OPTION_PIP_VOLUME, coerce=int, clamp=(0, 100)
    )
    watch_history = _config_property("watch_history", [])
    playback_positions = _config_property("playback_positions", {})

    @staticmethod
    def _initial_tv_window(window_positions):
        """Place the single TV window where the larger of the two old windows was.

        Only a landscape window of at least MIN_REUSED_WINDOW counts: 1.14 kept just the tall
        channel list, which would make a portrait TV.
        """
        keys = ("x", "y", "width", "height")
        min_width, min_height = ConfigManager.MIN_REUSED_WINDOW
        sized = [
            geometry
            for geometry in (
                window_positions.get("video_player"),
                window_positions.get("channel_list"),
            )
            if isinstance(geometry, dict)
            and all(isinstance(geometry.get(k), int) for k in keys)
            and geometry["width"] >= min_width
            and geometry["height"] >= min_height
            and geometry["width"] > geometry["height"]
        ]
        if not sized:
            return dict(ConfigManager.DEFAULT_TV_WINDOW)
        best = max(sized, key=lambda geometry: geometry["width"] * geometry["height"])
        return {key: best[key] for key in keys}

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
                "tv_window": dict(ConfigManager.DEFAULT_TV_WINDOW),
            },
            "favorites": [],
            "channel_logos": ConfigManager.DEFAULT_OPTION_CHANNEL_LOGO,
            "channel_epg": ConfigManager.DEFAULT_OPTION_CHANNEL_EPG,
            "xmltv_channel_map": MultiKeyDict(),
            "max_cache_image_size": ConfigManager.DEFAULT_OPTION_MAX_CACHE_IMAGE_SIZE,
            "epg_list_window_hours": ConfigManager.DEFAULT_OPTION_EPG_LIST_WINDOW_HOURS,
            "epg_stb_period_hours": ConfigManager.DEFAULT_OPTION_EPG_STB_PERIOD_HOURS,
            "live_latency": ConfigManager.DEFAULT_OPTION_LIVE_LATENCY,
            "mouse_wheel_action": ConfigManager.DEFAULT_OPTION_MOUSE_WHEEL,
            "start_with_last_channel": ConfigManager.DEFAULT_OPTION_START_WITH_LAST_CHANNEL,
            "last_live_channel": None,
            "timeshift_enabled": ConfigManager.DEFAULT_OPTION_TIMESHIFT_ENABLED,
            "timeshift_max_mib": ConfigManager.DEFAULT_OPTION_TIMESHIFT_MAX_MIB,
            "timeshift_ram_mib": ConfigManager.DEFAULT_OPTION_TIMESHIFT_RAM_MIB,
            "tv_defaults_version": ConfigManager.TV_DEFAULTS_VERSION,
        }

    def save_window_settings(self, window, window_name):
        pos = window.geometry()
        self.config["window_positions"][window_name] = {
            "x": pos.x(),
            "y": pos.y(),
            "width": pos.width(),
            "height": pos.height(),
        }
        self.save_config()

    def apply_window_settings(self, window_name, window):
        settings = self.config["window_positions"][window_name]
        window.setGeometry(settings["x"], settings["y"], settings["width"], settings["height"])

    def save_config(self, *, retry: bool = True):
        """Write config.json; raises when it cannot (callers on the GUI thread catch it).

        A locked file is retried for up to a quarter of a second, unless `retry` is False
        (the save after a zap never waits) or a save failed within SAVE_BACKOFF_S.
        """
        retries = _REPLACE_RETRIES if retry and not self._save_failed_recently() else 0
        try:
            self._write_config(retries)
        except Exception:
            self._save_failed_at = time.monotonic()
            raise
        self._save_failed_at = None

    def _write_config(self, retries: int) -> None:
        self.xmltv_channel_map = self.xmltv_channel_map.serialize()
        try:
            serialized_config = json.dumps(self.config, option=json.OPT_INDENT_2)
        finally:
            self.xmltv_channel_map = MultiKeyDict.deserialize(self.xmltv_channel_map)

        # Write a sibling file and swap it in: a failed or interrupted write never
        # truncates the saved settings.
        directory = os.path.dirname(self.config_path) or "."
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(serialized_config)
            if os.path.exists(self.config_path):
                shutil.copymode(self.config_path, temporary)
            self._replace_config(temporary, retries)
        except BaseException:
            try:
                os.remove(temporary)
            except OSError as e:
                logger.debug("Could not remove %s: %s", temporary, e)
            raise

    def _replace_config(self, temporary, retries):
        for attempt in range(retries + 1):
            try:
                os.replace(temporary, self.config_path)
                return
            except PermissionError:
                if attempt == retries:
                    # The exception text names the paths: log only what happened.
                    logger.warning(
                        "Couldn't save the settings: config.json stayed locked (%d tries)",
                        attempt + 1,
                    )
                    raise
                time.sleep(_REPLACE_RETRY_DELAY_S)

    # Integer config properties using factory with coercion
    epg_list_window_hours = _config_property(
        "epg_list_window_hours", DEFAULT_OPTION_EPG_LIST_WINDOW_HOURS, coerce=int
    )
    epg_stb_period_hours = _config_property(
        "epg_stb_period_hours",
        DEFAULT_OPTION_EPG_STB_PERIOD_HOURS,
        coerce=int,
        clamp=(1, 168),
    )

    # --- Watch Tracking Helper Methods ---

    def _generate_content_id(self, item_data: dict, provider_name: str) -> str:
        """Generate a unique content ID for tracking playback position.

        Args:
            item_data: The item data dict (channel, movie, episode, etc.)
            provider_name: Current provider name

        Returns:
            A unique string ID for the content
        """
        # Use stream_id/id from item_data, with provider prefix for uniqueness
        item_id = item_data.get("id") or item_data.get("stream_id") or item_data.get("series_id")
        if not item_id:
            # Fallback to cmd or name
            item_id = item_data.get("cmd") or item_data.get("name") or str(hash(str(item_data)))
        return f"{provider_name}:{item_id}"

    def add_to_watch_history(
        self,
        item_data: dict,
        item_type: str,
        link: str,
        provider_name: str,
        content_id: str | None = None,
    ) -> None:
        """Add an item to watch history.

        Args:
            item_data: The item data dict
            item_type: Type of content (channel, movie, episode, etc.)
            link: The playback URL
            provider_name: Current provider name
            content_id: Identity used to de-duplicate and store (default: derived from the item)
        """
        if content_id is None:
            content_id = self._generate_content_id(item_data, provider_name)

        history_entry = {
            "content_id": content_id,
            "item_data": item_data,
            "item_type": item_type,
            "link": link,
            "provider_name": provider_name,
            "timestamp": datetime.now().isoformat(),
            "name": item_data.get("name") or item_data.get("ename") or "Unknown",
        }

        # Get current history and remove duplicate if exists
        history = self.watch_history or []
        history = [h for h in history if h.get("content_id") != content_id]

        # Add to front
        history.insert(0, history_entry)

        # Trim to limit
        if len(history) > self.DEFAULT_WATCH_HISTORY_LIMIT:
            history = history[: self.DEFAULT_WATCH_HISTORY_LIMIT]

        self.watch_history = history

    def save_playback_position(self, content_id: str, position_ms: int, duration_ms: int) -> None:
        """Save playback position for a content item.

        Args:
            content_id: Unique content identifier
            position_ms: Current position in milliseconds
            duration_ms: Total duration in milliseconds
        """
        positions = self.playback_positions or {}
        positions[content_id] = {
            "position": position_ms,
            "duration": duration_ms,
            "timestamp": datetime.now().isoformat(),
        }
        self.playback_positions = positions

    def get_playback_position(self, content_id: str) -> int | None:
        """Get saved playback position for content.

        Args:
            content_id: Unique content identifier

        Returns:
            Position in milliseconds, or None if not found
        """
        positions = self.playback_positions or {}
        entry = positions.get(content_id)
        if entry:
            pos = entry.get("position")
            return int(pos) if pos is not None else None
        return None

    def get_watched_status(self, content_id: str) -> str:
        """Get the watched status of content.

        Args:
            content_id: Unique content identifier

        Returns:
            "watched" if >90% complete, "partial" if started, "unwatched" otherwise
        """
        positions = self.playback_positions or {}
        entry = positions.get(content_id)
        if not entry:
            return "unwatched"

        position = entry.get("position", 0)
        duration = entry.get("duration", 0)

        if duration <= 0:
            return "unwatched"

        ratio = position / duration
        if ratio >= self.DEFAULT_WATCHED_THRESHOLD:
            return "watched"
        elif ratio > 0.01:  # More than 1% watched
            return "partial"
        return "unwatched"

    def clear_playback_position(self, content_id: str) -> None:
        """Clear saved playback position for content.

        Args:
            content_id: Unique content identifier
        """
        positions = self.playback_positions or {}
        if content_id in positions:
            del positions[content_id]
            self.playback_positions = positions
