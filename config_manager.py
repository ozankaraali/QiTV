import os
import platform
import shutil

import orjson as json


class ConfigManager:
    CURRENT_VERSION = "1.5.8"  # Set your current version here

    DEFAULT_OPTION_CHECKUPDATE = True
    DEFAULT_OPTION_STB_CONTENT_INFO = False

    def __init__(self):
        self.config = {}
        self.options = {}
        self.token = ""
        self.url = ""
        self.mac = ""
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
            print(f"Error during config migration: {e}")

    def load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = json.loads(f.read())
            if self.config is None:
                self.config = self.default_config()
        except (FileNotFoundError, json.JSONDecodeError):
            self.config = self.default_config()
            self.save_config()

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
        return self.config.get("show_stb_content_info", ConfigManager.DEFAULT_OPTION_STB_CONTENT_INFO)

    @show_stb_content_info.setter
    def show_stb_content_info(self, value):
        self.config["show_stb_content_info"] = value

    @property
    def selected_provider_name(self):
        return self.config.get("selected_provider_name", "iptv-org.github.io")

    @selected_provider_name.setter
    def selected_provider_name(self, value):
        self.config["selected_provider_name"] = value

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
                "channel_list": {"x": 1250, "y": 100, "width": 400, "height": 800, "splitter_ratio": 0.75},
                "video_player": {"x": 50, "y": 100, "width": 1200, "height": 800},
            },
            "favorites": [],
            "show_stb_content_info": ConfigManager.DEFAULT_OPTION_STB_CONTENT_INFO,
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

        self.save_config()

    def apply_window_settings(self, window_name, window):
        settings = self.config["window_positions"][window_name]
        window.setGeometry(
            settings["x"], settings["y"], settings["width"], settings["height"]
        )
        if window_name == "channel_list":
            window.splitter_ratio = settings.get("splitter_ratio", 0.75)

    def save_config(self):
        serialized_config = json.dumps(self.config, option=json.OPT_INDENT_2)
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(serialized_config.decode("utf-8"))
