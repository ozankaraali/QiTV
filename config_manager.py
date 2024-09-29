import os
import platform
import shutil

import orjson as json


class ConfigManager:
    CURRENT_VERSION = "1.4.10"  # Set your current version here

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

        selected_config = self.config["data"][self.config["selected"]]
        if "options" in selected_config:
            self.options = selected_config["options"]
            self.token = self.options["headers"]["Authorization"].split(" ")[1]
        else:
            self.options = {
                "headers": {
                    "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
                    "Accept-Charset": "UTF-8,*;q=0.8",
                    "X-User-Agent": "Model: MAG200; Link: Ethernet",
                    "Content-Type": "application/json",
                }
            }

        self.url = selected_config.get("url")
        self.mac = selected_config.get("mac")

    def update_patcher(self):
        # add favorites to the loaded config if it doesn't exist
        if "favorites" not in self.config:
            self.config["favorites"] = []
            self.save_config()

    @staticmethod
    def default_config():
        return {
            "selected": 0,
            "data": [
                {
                    "type": "M3UPLAYLIST",
                    "url": "https://iptv-org.github.io/iptv/index.m3u",
                }
            ],
            "window_positions": {
                "channel_list": {"x": 1250, "y": 100, "width": 400, "height": 800},
                "video_player": {"x": 50, "y": 100, "width": 1200, "height": 800},
            },
            "favorites": [],
        }

    def save_window_settings(self, pos, window_name):
        self.config["window_positions"][window_name] = {
            "x": pos.x(),
            "y": pos.y(),
            "width": pos.width(),
            "height": pos.height(),
        }
        self.save_config()

    def apply_window_settings(self, window_name, window):
        settings = self.config["window_positions"][window_name]
        window.setGeometry(
            settings["x"], settings["y"], settings["width"], settings["height"]
        )

    # def save_config(self):
    #     with open(self.config_path, "wb") as f:
    #         f.write(json.dumps(self.config, option=json.OPT_INDENT_2))

    def save_config(self):
        serialized_config = json.dumps(self.config, option=json.OPT_INDENT_2)
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(serialized_config.decode("utf-8"))
