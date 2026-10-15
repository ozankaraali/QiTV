"""scripts/make_demo_content.py: complete fictional content, served from 127.0.0.1 only."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock
from urllib.request import urlopen
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from scripts import make_demo_content as demo
from services.epg_time import parse_xmltv
from services.m3u import parse_m3u

BASE = "http://127.0.0.1:8765"
NOW = datetime(2026, 10, 15, 18, 40, tzinfo=timezone.utc)


def _loopback(host) -> bool:
    name = (host.decode() if isinstance(host, bytes) else str(host or "")).strip("[]").lower()
    return name in ("", "localhost", "::1") or name.startswith("127.")


class NetworkWatch:
    """Records every name lookup and connection beyond this machine (and refuses it)."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo
        watch = self

        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(address[0]):
                watch.attempts.append(f"connect {address[0]}")
                raise OSError("no network in this test")
            return real_connect(sock, address)

        def getaddrinfo(host, *args, **kwargs):
            if not _loopback(host):
                watch.attempts.append(f"lookup {host}")
                raise OSError("no network in this test")
            return real_getaddrinfo(host, *args, **kwargs)

        self.patches = [
            mock.patch.object(socket.socket, "connect", connect),
            mock.patch.object(socket, "getaddrinfo", getaddrinfo),
        ]

    def __enter__(self) -> "NetworkWatch":
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *_exc) -> None:
        for patch in reversed(self.patches):
            patch.stop()


class DemoContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.addClassCleanup(directory.cleanup)
        cls.out = Path(directory.name)
        with NetworkWatch() as watch:
            demo.generate(cls.out, BASE, now=NOW)
        cls.attempts = watch.attempts

    def test_making_it_touches_no_network(self):
        self.assertEqual(self.attempts, [])

    def test_the_playlist_has_24_channels_in_4_groups_on_this_machine(self):
        playlist = parse_m3u((self.out / demo.PLAYLIST).read_text(encoding="utf-8"), True)
        channels = playlist["contents"]
        self.assertEqual(len(channels), 24)
        groups = {k: len(v) for k, v in playlist["sorted_channels"].items()}
        self.assertEqual(groups, {"News": 6, "Sports": 6, "Entertainment": 6, "Kids": 6})
        self.assertIn("Northwind News", [c["name"] for c in channels])
        self.assertIn("Harbor Sports 1", [c["name"] for c in channels])
        for channel in channels:
            self.assertTrue(channel["cmd"].startswith(f"{BASE}/live/"), channel["cmd"])
            self.assertTrue(channel["logo"].startswith(f"{BASE}/{demo.LOGOS}/"), channel["logo"])

    def test_each_channel_has_a_square_logo(self):
        for number, _name, _group in demo.channels():
            image = QImage(str(self.out / demo.LOGOS / demo.logo_name(number)))
            self.assertFalse(image.isNull(), number)
            self.assertEqual(image.width(), image.height())

    def test_the_guide_covers_every_channel_now(self):
        root = ET.parse(self.out / demo.GUIDE).getroot()
        playlist = parse_m3u((self.out / demo.PLAYLIST).read_text(encoding="utf-8"))
        for channel in playlist:
            programmes = [
                (parse_xmltv(p.get("start")), parse_xmltv(p.get("stop")))
                for p in root.findall("programme")
                if p.get("channel") == channel["xmltv_id"]
            ]
            self.assertTrue(any(start <= NOW < stop for start, stop in programmes), channel)
            self.assertTrue(any(stop >= NOW + timedelta(hours=24) for _, stop in programmes))

    def test_the_library_has_3_genres_of_12_films_and_a_series_with_2_seasons(self):
        catalog = demo.Catalog(BASE)

        def ask(action, **extra):
            query = {"username": ["demo"], "password": ["demo"], "action": [action]}
            query.update({key: [value] for key, value in extra.items()})
            return catalog.answer(query)

        genres = ask("get_vod_categories")
        films = ask("get_vod_streams")
        self.assertEqual([g["category_name"] for g in genres], ["Drama", "Comedy", "Documentary"])
        for genre in genres:
            listed = [f for f in films if f["category_id"] == genre["category_id"]]
            self.assertEqual(len(listed), 12, genre)
        series = ask("get_series")
        self.assertEqual([s["name"] for s in series], [demo.SERIES_TITLE])
        info = ask("get_series_info", series_id=str(series[0]["series_id"]))
        self.assertEqual(sorted(info["episodes"]), ["1", "2"])
        self.assertIsNone(catalog.answer({"username": ["demo"], "password": ["wrong"]}))
        for item in films + series:
            poster = item.get("stream_icon") or item.get("cover")
            self.assertTrue(poster.startswith(f"{BASE}/{demo.POSTERS}/"), poster)
            self.assertTrue((self.out / demo.POSTERS / poster.rsplit("/", 1)[1]).is_file())

    def test_the_settings_name_only_the_demo_providers(self):
        config_dir = self.out / demo.CONFIG
        providers = json.loads((config_dir / "cache" / "provider" / "index.json").read_text())
        self.assertEqual([p["name"] for p in providers], [demo.TV_PROVIDER, demo.LIBRARY_PROVIDER])
        self.assertEqual(Path(providers[0]["url"]), (self.out / demo.PLAYLIST).resolve())
        self.assertEqual(providers[1]["url"], BASE)
        config = json.loads((config_dir / "config.json").read_text())
        self.assertFalse(config["check_updates"])  # no request to GitHub either
        self.assertEqual(config["epg_source"], "Local File")

    def test_the_server_answers_on_loopback_only(self):
        with NetworkWatch() as watch, demo.DemoServer(self.out) as server:
            self.assertTrue(server.url.startswith("http://127.0.0.1:"))
            query = "username=demo&password=demo&action=get_vod_categories"
            with urlopen(f"{server.url}/player_api.php?{query}", timeout=5) as response:
                self.assertEqual(len(json.loads(response.read())), 3)
            with urlopen(f"{server.url}/{demo.LOGOS}/{demo.logo_name(1)}", timeout=5) as response:
                self.assertEqual(response.read(8), b"\x89PNG\r\n\x1a\n")
            with urlopen(f"{server.url}/live/1.ts", timeout=5) as response:
                packets = response.read(188 * 4)
            self.assertEqual(packets[::188], b"\x47" * 4)  # MPEG-TS sync bytes
        self.assertEqual(watch.attempts, [])


if __name__ == "__main__":
    unittest.main()
