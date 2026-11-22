from datetime import datetime
from importlib import metadata as importlib_metadata
import logging
import os
from pathlib import Path
import platform
import sys
import time
import tomllib

import orjson as json

from services.atomic_write import atomic_write, set_aside
from services.captions import translation_rules
from services.captions.languages import CAPTION_LANGUAGES
from services.captions.mt_languages import MT_LANGUAGES
from services.captions.streaming import translate_times
from services.config_writer import ConfigWriter
from services.free_channels import ALL_NAME, ALL_URL
from services.multikeydict import MultiKeyDict
from services.redact import safe_message
from services.surrogates import mended

logger = logging.getLogger(__name__)

# Swapping config.json in can fail while Windows antivirus or the indexer holds the file
# open: retry briefly before giving up.
_REPLACE_RETRIES = 5
_REPLACE_RETRY_DELAY_S = 0.05
# After a failed save, saves try once without those retries for this long: a file that stays
# locked must not stall every save (a settled zap's among them) by the whole retry wait.
SAVE_BACKOFF_S = 30.0
QUIT_SAVE_WAIT_S = 5.0  # quitting waits this long at most for config.json to be written
_UNREAD_TEXT = "config.json couldn't be read when QiTV started: it is left as it is"
# A new config's keys that update_patcher does not add to a loaded one: its providers (a loaded
# config has its own), its windows (added one by one) and what the one-time steps set.
_SET_ELSEWHERE = frozenset(
    "selected_provider_name data window_positions channel_epg tv_defaults_version".split()
)


class ConfigUnreadable(PermissionError):
    """A save refused: config.json was there but could not be read at start (an antivirus or
    another program holding it, its permissions), so this run never writes over it."""


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
            logger.debug("Unable to read version from %s: %s", p, safe_message(e))

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
    DEFAULT_OPTION_SSL_VERIFY = True
    # Auto learns each channel's latency; every saved latency is moved to it once.
    DEFAULT_OPTION_LIVE_LATENCY = "auto"
    LIVE_LATENCY_CHOICES = ("auto", "low", "normal", "stable")
    # Bumped by each one-time change of the latency default (`update_patcher`).
    LATENCY_DEFAULTS_VERSION = 1
    # 2.9.7: the wheel over the picture changes the volume (over the channel list, channels);
    # every saved choice is moved to it once (`update_patcher`), a choice made later stays.
    DEFAULT_OPTION_MOUSE_WHEEL = "volume"
    MOUSE_WHEEL_CHOICES = ("channel", "volume")
    WHEEL_DEFAULTS_VERSION = 1
    DEFAULT_OPTION_START_WITH_LAST_CHANNEL = True
    DEFAULT_OPTION_TIMESHIFT_ENABLED = False
    # 2.1: hardware again (a frozen picture falls back to software: services.picture_watch);
    # VLC itself takes software where the graphics chip can't decode a stream.
    DEFAULT_OPTION_HARDWARE_DECODING = True
    # Bumped by each one-time change of the decoding default (`update_patcher`).
    DECODING_DEFAULTS_VERSION = 1
    DEINTERLACE_DEFAULTS_VERSION = 1
    DEFAULT_OPTION_TIMESHIFT_MAX_MIB = 2048
    DEFAULT_OPTION_TIMESHIFT_RAM_MIB = 256  # memory per stream not on the disk ring
    TIMESHIFT_RAM_CHOICES = (128, 256, 512)
    DEFAULT_OPTION_PIP_CORNER = "br"  # picture in picture: the small picture's corner
    PIP_CORNER_CHOICES = ("br", "bl", "tr", "tl")
    DEFAULT_OPTION_CORNER_PLAYER_CORNER = "bottom-right"  # what plays, small, over the Library
    CORNER_PLAYER_CORNER_CHOICES = ("top-left", "top-right", "bottom-left", "bottom-right")
    DEFAULT_OPTION_PIP_VOLUME = 30  # the small picture's own level once it is made audible
    DEFAULT_OPTION_VOLUME = 100  # the large picture's level: libVLC's default
    DEFAULT_OPTION_DEINTERLACE = "auto"  # 2.3: libVLC's own, or "off" (Settings)
    # 2.11.2 and older also stored "on" (every picture deinterlaced): it reads as "auto" now.
    DEINTERLACE_CHOICES = ("auto", "off")
    AI_CAPTIONS_MODELS = ("auto", "small", "large")  # 2.9: AI captions' speech model tier
    # 2.2, the multiview: a tile's time-shift buffer while it has no focus (an optional key,
    # never written by default). 48 MiB holds one keyframe interval of a 4K channel.
    DEFAULT_OPTION_MULTIVIEW_TILE_RAM_MIB = 64
    MULTIVIEW_TILE_RAM_RANGE = (48, 512)
    TV_DEFAULTS_VERSION = 2
    # 2.12: bumped by each one-time clean-up of config["data"] (`update_patcher`).
    LEGACY_LISTS_VERSION = 1
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

    def __init__(self):
        self.config = {}
        self._save_failed_at: float | None = None  # monotonic time of the last failed save
        self._writer: ConfigWriter | None = None  # write_in_background()'s
        self._unread = False  # config.json there but unreadable: never saved over (this run)
        self.config_path = self._get_config_path()
        os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
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

    def subtitles_dir(self) -> str:
        """Where downloaded subtitles are kept (made by the first download)."""
        return os.path.join(self.get_config_dir(), "subtitles")

    def load_config(self):
        try:
            self.config = json.loads(self._read_config())
        except FileNotFoundError:
            self.config = self.default_config()
            self._save_at_start()
        except json.JSONDecodeError:  # cut short, or damaged
            self._replace_unreadable_config()
        except OSError as e:  # there, but it can't be opened or read: not damaged
            self._keep_unread_config(e)
        else:
            if self.config is None:
                self.config = self.default_config()
            elif not isinstance(self.config, dict):
                self._replace_unreadable_config()

        if isinstance(self.xmltv_channel_map, list):
            self.xmltv_channel_map = MultiKeyDict.deserialize(self.xmltv_channel_map)

        self.update_patcher()

    def _read_config(self) -> bytes:
        """config.json's bytes. One held open for a moment (Windows' antivirus or indexer, as
        QiTV starts) is tried again, as a save is: _REPLACE_RETRIES times, _REPLACE_RETRY_DELAY_S
        apart."""
        attempt = 0
        while True:
            try:
                with open(self.config_path, "rb") as f:
                    return f.read()
            except PermissionError:
                if attempt == _REPLACE_RETRIES:
                    raise  # still held: the defaults this run (_keep_unread_config)
                attempt += 1
                time.sleep(_REPLACE_RETRY_DELAY_S)

    def _keep_unread_config(self, error: OSError) -> None:
        """The defaults this run, and the user's config.json kept as it is: nothing is saved
        over it (its settings are only out of reach, maybe for a moment). save_failed() tells
        the window to say the settings aren't saved."""
        self.config = self.default_config()
        self._unread = True
        self._save_failed_at = time.monotonic()
        logger.warning(
            "The settings can't be read (%s: %s): the defaults are used this time, and "
            "config.json is left as it is",
            type(error).__name__,
            error.strerror or "",  # (its text names the path)
        )

    def _replace_unreadable_config(self) -> None:
        """Start from the defaults; the config.json that cannot be read is kept beside the
        new one as config.json.broken-<time>, so nothing in it is lost. (Where it cannot be
        moved, the defaults cannot replace it either.)"""
        self.config = self.default_config()
        aside = set_aside(self.config_path)
        if aside is not None:
            logger.warning(
                "The settings can't be read: they were kept as %s, and the defaults are used",
                os.path.basename(aside),
            )
        self._save_at_start()

    def update_patcher(self):
        need_update = False

        # 1.x windows stay listed so going back to 1.x finds them (1.13.7 failed without).
        window_positions = self.config.setdefault("window_positions", {})
        if "tv_window" not in window_positions:
            window_positions["tv_window"] = self._initial_tv_window(window_positions)
            need_update = True
        defaults = self.default_config()
        for window_name, settings in defaults["window_positions"].items():
            if window_name not in window_positions:
                window_positions[window_name] = settings
                need_update = True

        # Add settings introduced after the loaded config was written: a new config's (see
        # _SET_ELSEWHERE), and these.
        missing_defaults: dict[str, object] = {
            **{key: value for key, value in defaults.items() if key not in _SET_ELSEWHERE},
            "last_watched": None,
            "epg_source": ConfigManager.DEFAULT_OPTION_EPG_SOURCE,
            "epg_url": ConfigManager.DEFAULT_OPTION_EPG_URL,
            "epg_file": ConfigManager.DEFAULT_OPTION_EPG_FILE,
            "epg_expiration_value": ConfigManager.DEFAULT_OPTION_EPG_EXPIRATION_VALUE,
            "epg_expiration_unit": ConfigManager.DEFAULT_OPTION_EPG_EXPIRATION_UNIT,
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

        # 2.1: hardware decoding is the default again. Since 2.0.5 (2.0.6 on Windows and
        # Linux) every config saved `false`, so a choice and the old default look alike: it
        # is switched on once, and a choice made afterwards stays. 2.0.x keeps the key and
        # reads `hardware_decoding` as it is.
        decoding_defaults = self._defaults_version("decoding_defaults_version")
        if decoding_defaults < 1:
            self.config["hardware_decoding"] = True
        if decoding_defaults < ConfigManager.DECODING_DEFAULTS_VERSION:
            self.config["decoding_defaults_version"] = ConfigManager.DECODING_DEFAULTS_VERSION
            need_update = True

        # 2.1's Auto is new enough that everyone is moved to it once, whatever was saved
        # (2.0.0 and 2.0.1 saved Normal for everyone, and a fixed distance skips where Auto
        # plays further behind). A choice made afterwards stays. 2.x before this ignores the key.
        latency_defaults = self._defaults_version("latency_defaults_version")
        if latency_defaults < 1:
            self.config["live_latency"] = "auto"
        if latency_defaults < ConfigManager.LATENCY_DEFAULTS_VERSION:
            self.config["latency_defaults_version"] = ConfigManager.LATENCY_DEFAULTS_VERSION
            need_update = True

        # 2.12: deinterlacing is Auto or Off (On is gone: it deinterlaced progressive pictures
        # too, for three times the CPU). Everyone is moved to Auto once, whatever was saved, and
        # an Off chosen afterwards stays. Older 2.x builds ignore the key and read "deinterlace"
        # as one of their choices.
        deinterlace_defaults = self._defaults_version("deinterlace_defaults_version")
        if deinterlace_defaults < 1 and "deinterlace" in self.config:
            self.config["deinterlace"] = "auto"
        if deinterlace_defaults < ConfigManager.DEINTERLACE_DEFAULTS_VERSION:
            self.config["deinterlace_defaults_version"] = ConfigManager.DEINTERLACE_DEFAULTS_VERSION
            need_update = True

        # 2.9.7: the wheel over the picture changes the volume. Every config saved "channel"
        # as the default, so a choice and the old default look alike: everyone is moved
        # once, and a choice made afterwards stays. Older 2.x builds ignore the key and read
        # "volume" as one of their choices.
        wheel_defaults = self._defaults_version("wheel_defaults_version")
        if wheel_defaults < 1:
            self.config["mouse_wheel_action"] = "volume"
        if wheel_defaults < ConfigManager.WHEEL_DEFAULTS_VERSION:
            self.config["wheel_defaults_version"] = ConfigManager.WHEEL_DEFAULTS_VERSION
            need_update = True

        # 2.12: early 1.x builds kept their channel lists in the providers of "data" ("itv",
        # and "channels" again): megabytes nothing reads, parsed at every start, held in
        # memory and written at every save (a film saves every 30 s). No 2.x build reads them;
        # they go once, and a 1.x build gone back to loads its list again. Everything else in
        # "data" stays.
        legacy_lists = self._defaults_version("legacy_lists_version")
        if legacy_lists < 1:
            entries = self.config.get("data")
            for entry in entries if isinstance(entries, list) else ():
                if isinstance(entry, dict):
                    entry.pop("itv", None)
                    entry.pop("channels", None)
        if legacy_lists < ConfigManager.LEGACY_LISTS_VERSION:
            self.config["legacy_lists_version"] = ConfigManager.LEGACY_LISTS_VERSION
            need_update = True

        if need_update:
            self._save_at_start()

    def _defaults_version(self, key: str) -> int:
        """Which one-time steps under `key` already ran: 0 when missing or not a number."""
        version = self.config.get(key, 0)
        return version if isinstance(version, int) and not isinstance(version, bool) else 0

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
    provider_favorites = _config_property("provider_favorites", None)
    last_watched = _config_property("last_watched", None)
    selected_provider_name = _config_property("selected_provider_name", "iptv-org.github.io")
    # Mixed providers (2.1): what the TV and the Library show. An empty library_provider_name
    # means the selected provider.
    tv_all_providers = _config_property("tv_all_providers", False, coerce=bool)
    library_provider_name = _config_property("library_provider_name", "", coerce=str)
    library_all_providers = _config_property("library_all_providers", False, coerce=bool)
    # 2.8, optional (older builds ignore it and mix every provider): the providers the
    # Library mixes, by name; empty: every one.
    library_mix = _config_property(
        "library_mix",
        [],
        coerce=lambda value: (
            [n for n in value if isinstance(n, str)] if isinstance(value, list) else []
        ),
    )
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
    hardware_decoding = _config_property(
        "hardware_decoding", DEFAULT_OPTION_HARDWARE_DECODING, coerce=lambda value: value is True
    )
    # 2.9.1, optional (older builds ignore it): the providers' names looked up over HTTPS
    # (services.secure_dns); off by default.
    secure_dns = _config_property("secure_dns", False, coerce=lambda value: value is True)
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
    corner_player_corner = _config_property(
        "corner_player_corner",
        DEFAULT_OPTION_CORNER_PLAYER_CORNER,
        coerce=_one_of(CORNER_PLAYER_CORNER_CHOICES, DEFAULT_OPTION_CORNER_PLAYER_CORNER),
    )
    pip_volume = _config_property(
        "pip_volume", DEFAULT_OPTION_PIP_VOLUME, coerce=int, clamp=(0, 100)
    )
    # The large picture's level (2.2.1, optional; older builds ignore it and start at 100).
    volume = _config_property("volume", DEFAULT_OPTION_VOLUME, coerce=int, clamp=(0, 100))
    # The last multiview grid (2.2, optional; older builds ignore it): {"version": 1, "cells",
    # "focus", "tiles": [a last_live_channel-style record, or None per tile]}.
    multiview = _config_property("multiview", None)
    # The phone remote (2.5, optional; older builds ignore it): services/remote/pairing.py's
    # {"version": 1, "enabled", "relay", "relay_url", "port", "room", "room_secret", "identity",
    # "devices"}. The remote's thread owns it; the GUI only writes what that thread hands over.
    remote = _config_property("remote", None)
    # The multiview's layouts showed once by themselves, as a hint (optional; older builds
    # ignore it).
    multiview_hinted = _config_property(
        "multiview_hinted", False, coerce=lambda value: value is True
    )
    # The detailed log (2.8.2, optional; older builds ignore it): QiTV's own debug lines too.
    detailed_log = _config_property("detailed_log", False, coerce=lambda value: value is True)
    multiview_tile_ram_mib = _config_property(
        "multiview_tile_ram_mib",
        DEFAULT_OPTION_MULTIVIEW_TILE_RAM_MIB,
        coerce=int,
        clamp=MULTIVIEW_TILE_RAM_RANGE,
    )
    subtitle_folder = _config_property("subtitle_folder", "", coerce=str)  # 2.2: Load a file…
    # 2.4, optional (older builds ignore it): on macOS, the fullscreen picture floats in a small
    # window while another desktop shows (ui/desktop_pip.py).
    desktop_pip = _config_property("desktop_pip", True, coerce=lambda value: value is not False)
    # 2.4.1, optional: where the user moved or resized that window, [x, y, width, height].
    desktop_pip_rect = _config_property("desktop_pip_rect", None)
    # Optional (older builds ignore it): on macOS, the picture floats in that window too
    # while the TV window (not fullscreen) is covered by other apps' windows or minimized
    # (ui/desktop_pip.py, the covered reason). Off by default: only true turns it on.
    covered_pip = _config_property("covered_pip", False, coerce=lambda value: value is True)
    # 2.8.5, optional (older builds ignore it): the window stays above other apps' windows
    # (View → Keep on Top).
    keep_on_top = _config_property("keep_on_top", False, coerce=lambda value: value is True)
    # 2.3, optional (older builds ignore it): the deinterlacing (Settings), every player's.
    deinterlace = _config_property(
        "deinterlace",
        DEFAULT_OPTION_DEINTERLACE,
        coerce=_one_of(DEINTERLACE_CHOICES, DEFAULT_OPTION_DEINTERLACE),
    )
    # 2.2, optional (older builds ignore it): the Recordings folder Settings chose; "" is the
    # system's Movies (Videos) folder's QiTV (services.recording).
    recordings_dir = _config_property(
        "recordings_dir", "", coerce=lambda value: value if isinstance(value, str) else ""
    )
    # 2.9.7, optional (older builds ignore it): more folders whose recordings the Library and
    # the Recordings page list (where new recordings go is `recordings_dir`'s).
    recording_places = _config_property(
        "recording_places",
        [],
        coerce=lambda value: (
            [v for v in value if isinstance(v, str) and v.strip()]
            if isinstance(value, list)
            else []
        ),
    )
    # 2.8, optional (older builds ignore it): Jellyfin and Emby. Which version of a title plays
    # ("screen": the 4K one on a 4K screen, or when it's the only one; "always", "never"); the
    # file plays as it is (the player's ⚙ quality menu asks the server to convert).
    media_4k_versions = _config_property(
        "media_4k_versions", "screen", coerce=_one_of(("screen", "always", "never"), "screen")
    )
    # 2.9.1, optional: groups hidden to tidy the lists ({"version": 1, "groups"});
    # services.hidden_groups reads and writes it.
    hidden_groups = _config_property(
        "hidden_groups", None, coerce=lambda value: value if isinstance(value, dict) else None
    )
    # 2.8, optional: parental controls ({"version": 1, "salt", "pin" (a PBKDF2 hash), "hidden"});
    # services.parental reads and writes it.
    parental = _config_property(
        "parental", None, coerce=lambda value: value if isinstance(value, dict) else None
    )
    # 2.2: OpenSubtitles. Optional keys (older builds ignore them); the key and the password
    # are never logged. The language is the one downloaded last; "hearing impaired" whether
    # that file was for the hearing impaired (such files then rank first).
    opensubtitles_api_key = _config_property("opensubtitles_api_key", "", coerce=str)
    opensubtitles_username = _config_property("opensubtitles_username", "", coerce=str)
    opensubtitles_password = _config_property("opensubtitles_password", "", coerce=str)
    opensubtitles_language = _config_property("opensubtitles_language", "", coerce=str)
    opensubtitles_hearing_impaired = _config_property(
        "opensubtitles_hearing_impaired", False, coerce=lambda value: value is True
    )
    # 2.8, optional (older builds ignore them): TMDB fills in what the Library's titles lack
    # (services/tmdb.py); the token is the user's own (a source build has none built in) and is
    # never logged.
    tmdb_details = _config_property("tmdb_details", True, coerce=lambda value: value is not False)
    tmdb_token = _config_property("tmdb_token", "", coerce=str)
    # 2.9, optional (older builds ignore them): AI captions. The speech model's tier ("auto":
    # chosen by the computer's speed; "small": faster, 0.6B; "large": more accurate, 1.7B).
    ai_captions_model = _config_property(
        "ai_captions_model", "auto", coerce=_one_of(AI_CAPTIONS_MODELS, "auto")
    )
    # 2.11.2: no longer read; kept so older builds read what they wrote (the language captions
    # heard when nothing named it: "auto", else one of services.captions.languages.
    # CAPTION_LANGUAGES). What captions hear now comes from the content only.
    ai_captions_language = _config_property(
        "ai_captions_language", "auto", coerce=_one_of(("auto", *CAPTION_LANGUAGES), "auto")
    )
    # 2.9: the AI captions language the viewer chose per channel ("<source>|<channel>": a
    # Qwen language name); optional, so older builds keep reading the config.
    ai_captions_picks = _config_property(
        "ai_captions_picks",
        {},
        coerce=lambda v: (
            {str(k): str(n) for k, n in v.items() if str(n) in CAPTION_LANGUAGES}
            if isinstance(v, dict)
            else {}
        ),
    )
    # 2.9.6, optional (older builds ignore it): the viewer said yes to downloading the speech models
    # for the languages they watch (the first question). Undeclared until 2026-11-08, so it was set
    # on the object only, never written, and the question came back after every restart.
    ai_captions_language_models = _config_property(
        "ai_captions_language_models", False, coerce=lambda value: value is True
    )
    # 2.10, optional (older builds ignore it): the language AI captions are translated into
    # ("": off; else one of services.captions.mt_languages.MT_LANGUAGES, MiLMMT's names).
    # 2.11.2: no longer read (nor carried over); kept so older builds read what they wrote.
    ai_captions_translate_to = _config_property(
        "ai_captions_translate_to", "", coerce=_one_of(("", *MT_LANGUAGES), "")
    )
    # 2.11.2, optional (older builds ignore it): the translation per spoken language
    # ({Qwen's name: MiLMMT's name, or "" for none}; services.captions.translation_rules)
    ai_captions_translations = _config_property(
        "ai_captions_translations", {}, coerce=translation_rules.clean
    )
    # 2.13.1, optional (older builds ignore it): the caption translator's timed medium line per
    # thread count ({"2": seconds}): the next run's translation lead starts from it
    ai_captions_translate_t = _config_property(
        "ai_captions_translate_t", {}, coerce=translate_times
    )
    # Optional (older builds ignore it): AI captions were turned on (true) or off during the
    # last video file played; the next one comes on with them when nothing has to be asked,
    # downloaded or measured first (CaptionsController.local_video_playing).
    ai_captions_local_on = _config_property(
        "ai_captions_local_on", False, coerce=lambda v: v is True
    )
    # Optional (older builds ignore them): the audio screen's view picked by hand ("": none, the
    # song's own: Album with an album's list, else Cover), and the dock's shuffle and repeat.
    audio_view = _config_property(
        "audio_view", "", coerce=_one_of(("", "cover", "album", "stage"), "")
    )
    audio_shuffle = _config_property("audio_shuffle", False, coerce=lambda v: v is True)
    audio_repeat = _config_property(
        "audio_repeat", "off", coerce=_one_of(("off", "all", "one"), "off")
    )
    # Optional (older builds ignore it): the kinds of files QiTV registered itself for with the
    # system, and the executable registered ({"videos": "<path>"}); services.file_types reads
    # it and assigns a new dict (the default is shared: never changed in place).
    file_types = _config_property(
        "file_types", {}, coerce=lambda v: v if isinstance(v, dict) else {}
    )
    # 2.11.1, optional (older builds ignore it): the file types (extensions) each kind was
    # registered with ({"music": ["aac", "aif", …]}); a kind registered with others (2.11.0's
    # music had no AIFF) registers again at start (services.file_types.refresh).
    file_types_registered = _config_property(
        "file_types_registered", {}, coerce=lambda v: v if isinstance(v, dict) else {}
    )
    watch_history = _config_property("watch_history", [])
    playback_positions = _config_property("playback_positions", {})

    @property
    def language(self) -> str | None:
        """The language chosen in Settings ("tr"), or None to follow the system's.

        2.2, optional: no key (what older builds write, and ignore) is the system's language.
        """
        code = self.config.get("language")
        return code if isinstance(code, str) and code else None

    @language.setter
    def language(self, code: str | None) -> None:
        if code:
            self.config["language"] = code
        else:
            self.config.pop("language", None)

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
        """A new config.json (update_patcher adds its settings to an older one)."""
        return {
            "selected_provider_name": ALL_NAME,
            "check_updates": ConfigManager.DEFAULT_OPTION_CHECKUPDATE,
            "ssl_verify": ConfigManager.DEFAULT_OPTION_SSL_VERIFY,
            "data": [
                {
                    "type": "M3UPLAYLIST",
                    "name": ALL_NAME,
                    "url": ALL_URL,
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
            "hardware_decoding": ConfigManager.DEFAULT_OPTION_HARDWARE_DECODING,
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

    def save_config(self, *, retry: bool = True, wait: bool = False):
        """Write config.json; raises when it cannot (callers on the GUI thread catch it).

        A locked file is retried for up to a quarter of a second, unless `retry` is False
        (the save after a zap never waits) or a save failed within SAVE_BACKOFF_S.

        Once `write_in_background()` was called (QiTV does), the settings are serialised here
        and written by a thread of their own: a save returns before the disk is written and a
        failure is logged there (`save_failed()` tells); `wait` waits for that write and
        raises its failure, as a save did before (TVWindow.save_config, after a change to the
        providers, says so).
        """
        if self._unread:  # a failed save, as one that couldn't write
            self._save_failed_at = time.monotonic()
            if self._writer is None or wait:
                raise ConfigUnreadable(_UNREAD_TEXT)
            return
        retries = _REPLACE_RETRIES if retry and not self._save_failed_recently() else 0
        writer = self._writer
        if writer is not None:
            written = writer.submit(self._serialized_config(), retries)
            if wait:
                writer.wait(written)
            return
        try:
            self._write_config(retries)
        except Exception:
            self._save_failed_at = time.monotonic()
            raise
        self._save_failed_at = None

    def write_in_background(self) -> None:
        """Saves from now on are written by a thread of their own (`ConfigWriter`), in order,
        the newest of those waiting only: the GUI thread never waits for the disk, nor for a
        locked file. QiTV turns this on once its config is loaded; tests and scripts save at
        once."""
        if self._writer is None:
            self._writer = ConfigWriter(self._write_in_background)

    def flush_saves(self, timeout: float = QUIT_SAVE_WAIT_S) -> bool:
        """Waits, `timeout` seconds at most, until the saves handed to the writer are written
        (QiTV quitting); False when one still was not."""
        writer = self._writer
        return True if writer is None else writer.flush(timeout)

    def _write_in_background(self, serialized: bytes, retries: int) -> None:
        """On the writer's thread: the snapshot written, a failure noted and logged."""
        try:
            self._write_config(retries, serialized)
        except Exception as e:
            self._save_failed_at = time.monotonic()
            logger.warning("Couldn't save the settings: %s", safe_message(e))
            raise
        self._save_failed_at = None

    def _serialized_config(self) -> bytes:
        """config.json's bytes, the settings as they are now. Text no UTF-8 file can hold (half
        an emoji: a lone surrogate) stopped every save; a provider's lists are mended as they
        are read (services.surrogates), and what came in another way is mended here, where it
        is kept too."""
        try:
            return json.dumps(self._config_to_write(), option=json.OPT_INDENT_2)
        except TypeError:  # orjson's JSONEncodeError is one
            mended(self.config)
            channel_map = self.xmltv_channel_map
            if isinstance(channel_map, MultiKeyDict):  # keyed by tuples: mended as it is kept
                self.xmltv_channel_map = MultiKeyDict.deserialize(mended(channel_map.serialize()))
            serialized = json.dumps(self._config_to_write(), option=json.OPT_INDENT_2)
        logger.warning("The settings held text cut mid-character: saved with U+FFFD in its place")
        return serialized

    def _config_to_write(self) -> dict:
        """The config as config.json keeps it: its channel map a list (a copy of the top level;
        the map in memory stays as it is)."""
        config = dict(self.config)
        channel_map = self.xmltv_channel_map
        if isinstance(channel_map, MultiKeyDict):
            channel_map = channel_map.serialize()
        config["xmltv_channel_map"] = channel_map
        return config

    def _write_config(self, retries: int, serialized: bytes | None = None) -> None:
        """config.json written whole (`serialized`: a snapshot taken before; now if None)."""
        if self._unread:
            raise ConfigUnreadable(_UNREAD_TEXT)
        serialized_config = self._serialized_config() if serialized is None else serialized
        with atomic_write(
            self.config_path, lambda temporary, path: self._replace_config(temporary, retries)
        ) as f:
            f.write(serialized_config)

    def _replace_config(self, temporary, retries):
        """Swap the written settings in, readable by the user alone (they hold sign-ins and
        keys): an older file's looser mode is not carried over."""
        os.chmod(temporary, 0o600)
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
