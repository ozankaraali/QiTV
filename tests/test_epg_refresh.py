"""A guide refresh never blanks Now/Next, and a failed refresh keeps the guide it had."""

from datetime import datetime, timedelta
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from epg_manager import _INDEX_TIME_FORMAT, EpgManager, _cache_key, _xtream_cache_key
from multikeydict import MultiKeyDict

CHANNEL = {"xmltv_id": "news.test", "name": "News"}


def xmltv(title):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="news.test"><display-name>News</display-name></channel>
  <programme channel="news.test" start="20261015200000 +0000" stop="20261015210000 +0000">
    <title>{title}</title>
  </programme>
</tv>
""".encode()


class _Response:
    def __init__(self, body=b"", status_code=200):
        self.status_code = status_code
        self.content = body
        self.headers = {}

    def iter_content(self, size):
        yield self.content


def serving(body):
    return lambda url, **kwargs: _Response(body)


def failing(url, **kwargs):
    raise requests.ConnectionError("Failed to establish a new connection")


class _Config(SimpleNamespace):
    def __init__(self, directory, **overrides):
        super().__init__(
            channel_epg=True,
            epg_source="STB",
            epg_expiration=3600,
            epg_stb_period_hours=5,
            epg_list_window_hours=0,
            prefer_https=False,
            ssl_verify=True,
            xmltv_channel_map=MultiKeyDict(),
            **overrides,
        )
        self._directory = directory

    def get_config_dir(self):
        return self._directory


class EpgRefreshTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-epg-")
        self.addCleanup(directory.cleanup)
        self.config = _Config(directory.name)
        self.providers = SimpleNamespace(
            current_provider={
                "type": "XTREAM",
                "name": "X",
                "url": "http://h",
                "username": "u",
                "password": "p",
            },
            headers={},
        )
        self.key = _xtream_cache_key("http://h", "u")

    def manager(self):
        return EpgManager(self.config, self.providers)

    def titles(self, manager):
        programmes = manager.get_programs_for_channel(CHANNEL, None, 0)
        return [p["title"]["__text"] for p in programmes]

    def load(self, manager, title="Old"):
        with patch("epg_manager.requests.get", side_effect=serving(xmltv(title))):
            manager.set_current_epg()
        self.assertEqual(self.titles(manager), [title])

    def expire(self, manager):
        old = (datetime.now() - timedelta(days=2)).strftime(_INDEX_TIME_FORMAT)
        manager.index[self.key]["date"] = old
        manager.save_index()

    # --- the reload keeps the current guide until the new one is swapped in ----------------
    def test_periodic_refresh_keeps_the_guide_visible_while_it_reloads(self):
        manager = self.manager()
        self.load(manager)
        self.expire(manager)
        seen = []

        def slow_download(url, **kwargs):
            seen.append(self.titles(manager))  # what the GUI shows mid-reload
            return _Response(xmltv("New"))

        with patch("epg_manager.requests.get", side_effect=slow_download):
            manager.set_current_epg()
        self.assertEqual(seen, [["Old"]])
        self.assertEqual(self.titles(manager), ["New"])

    def test_forced_refresh_keeps_the_guide_visible_while_it_reloads(self):
        manager = self.manager()
        self.load(manager)
        seen = []

        def slow_download(url, **kwargs):
            seen.append(self.titles(manager))
            return _Response(xmltv("New"))

        with patch("epg_manager.requests.get", side_effect=slow_download):
            manager.force_refresh_current_epg()
        self.assertEqual(seen, [["Old"]])
        self.assertEqual(self.titles(manager), ["New"])

    def test_cached_reload_keeps_the_guide_visible(self):
        manager = self.manager()
        self.load(manager)
        seen = []
        real_load = manager._load_cached_epg

        def load_cached(cache_key):
            seen.append(self.titles(manager))
            return real_load(cache_key)

        with patch.object(manager, "_load_cached_epg", side_effect=load_cached):
            manager.set_current_epg()  # not expired: reloads the saved guide
        self.assertEqual(seen, [["Old"]])
        self.assertEqual(self.titles(manager), ["Old"])

    def test_a_reader_survives_a_swap_in_the_middle_of_a_lookup(self):
        manager = self.manager()
        self.load(manager)

        class SwappedMidLookup(MultiKeyDict):
            def __contains__(self, key):
                found = super().__contains__(key)
                manager.epg = MultiKeyDict()  # a refresh swaps in another guide meanwhile
                return found

        guide = SwappedMidLookup()
        for keys, programmes in manager.epg.items():
            guide[keys] = programmes
        manager.epg = guide
        self.assertEqual(self.titles(manager), ["Old"])

    # --- a failed refresh keeps the guide ------------------------------------------------
    def test_failed_refreshes_keep_the_guide_and_try_again_later(self):
        manager = self.manager()
        self.load(manager)
        cases = {
            "unreachable, forced": (failing, manager.force_refresh_current_epg),
            "server error, forced": (
                lambda url, **kwargs: _Response(b"busy", status_code=503),
                manager.force_refresh_current_epg,
            ),
            "unreachable, periodic": (failing, manager.set_current_epg),
        }
        for name, (fake_get, refresh) in cases.items():
            with self.subTest(name):
                self.expire(manager)
                with patch("epg_manager.requests.get", side_effect=fake_get):
                    refresh()
                self.assertEqual(self.titles(manager), ["Old"])
                self.assertIsNotNone(manager.index[self.key])  # not marked unavailable
        self.load(manager, "New")  # the next periodic refresh tries again
        self.assertEqual(self.titles(manager), ["New"])

    def test_failed_stb_refresh_keeps_the_guide(self):
        self.providers.current_provider = {"type": "STB", "name": "S", "url": "http://s/c/"}
        programme = {"name": "Old", "time": "2026-10-15 20:00:00", "time_to": "2026-10-15 21:00:00"}
        guides = [{"7": [programme]}, None]

        class Loader:
            def __init__(self, **kwargs):
                self.items = []

            def run(self):
                guide = guides.pop(0)
                self.items = [guide] if guide else []

        manager = self.manager()
        with patch("epg_manager.ContentLoader", Loader):
            manager.set_current_epg()
            manager.force_refresh_current_epg()  # the portal answers with nothing
        self.assertEqual(manager.get_programs_for_channel({"id": "7"}, None, 0), [programme])
        self.assertIsNotNone(manager.index[_cache_key("http://s/c/")])

    def test_startup_refresh_failure_uses_the_saved_guide(self):
        first = self.manager()
        self.load(first)
        self.expire(first)
        restarted = self.manager()  # a new session: nothing in memory, the guide on disk
        with patch("epg_manager.requests.get", side_effect=failing):
            restarted.set_current_epg()
        self.assertEqual(self.titles(restarted), ["Old"])
        self.assertIsNotNone(restarted.index[self.key])

    # --- unchanged: no previous guide, or another source -----------------------------------
    def test_a_failure_without_any_guide_marks_the_source_unavailable(self):
        manager = self.manager()
        with patch("epg_manager.requests.get", side_effect=failing):
            manager.set_current_epg()
        self.assertIsNone(manager.index[self.key])
        self.assertEqual(self.titles(manager), [])

    def test_another_providers_guide_is_not_kept(self):
        manager = self.manager()
        self.load(manager)
        self.providers.current_provider = dict(self.providers.current_provider, url="http://o")
        with patch("epg_manager.requests.get", side_effect=failing):
            manager.set_current_epg()
        self.assertEqual(self.titles(manager), [])


if __name__ == "__main__":
    unittest.main()
