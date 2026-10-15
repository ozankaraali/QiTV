"""A guide refresh never blanks Now/Next, and a failed refresh keeps the guide it had."""

from datetime import datetime, timedelta
import os
import pickle
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from services import epg_manager as epg_manager_module
from services.epg_manager import _INDEX_TIME_FORMAT, EpgManager, _cache_key, _xtream_cache_key
from services.multikeydict import MultiKeyDict

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


class _GuideCase(unittest.TestCase):
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
        with patch("services.epg_manager.requests.get", side_effect=serving(xmltv(title))):
            manager.set_current_epg()
        self.assertEqual(self.titles(manager), [title])

    def expire(self, manager):
        old = (datetime.now() - timedelta(days=2)).strftime(_INDEX_TIME_FORMAT)
        manager.index[self.key]["date"] = old
        manager.save_index()


class EpgRefreshTests(_GuideCase):
    # --- the reload keeps the current guide until the new one is swapped in ----------------
    def test_periodic_refresh_keeps_the_guide_visible_while_it_reloads(self):
        manager = self.manager()
        self.load(manager)
        self.expire(manager)
        seen = []

        def slow_download(url, **kwargs):
            seen.append(self.titles(manager))  # what the GUI shows mid-reload
            return _Response(xmltv("New"))

        with patch("services.epg_manager.requests.get", side_effect=slow_download):
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

        with patch("services.epg_manager.requests.get", side_effect=slow_download):
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
                with patch("services.epg_manager.requests.get", side_effect=fake_get):
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
        with patch("services.epg_manager.ContentLoader", Loader):
            manager.set_current_epg()
            manager.force_refresh_current_epg()  # the portal answers with nothing
        self.assertEqual(manager.get_programs_for_channel({"id": "7"}, None, 0), [programme])
        self.assertIsNotNone(manager.index[_cache_key("http://s/c/")])

    def test_startup_refresh_failure_uses_the_saved_guide(self):
        first = self.manager()
        self.load(first)
        self.expire(first)
        restarted = self.manager()  # a new session: nothing in memory, the guide on disk
        with patch("services.epg_manager.requests.get", side_effect=failing):
            restarted.set_current_epg()
        self.assertEqual(self.titles(restarted), ["Old"])
        self.assertIsNotNone(restarted.index[self.key])

    # --- unchanged: no previous guide, or another source -----------------------------------
    def test_a_failure_without_any_guide_marks_the_source_unavailable(self):
        manager = self.manager()
        with patch("services.epg_manager.requests.get", side_effect=failing):
            manager.set_current_epg()
        self.assertIsNone(manager.index[self.key])
        self.assertEqual(self.titles(manager), [])

    def test_another_providers_guide_is_not_kept(self):
        manager = self.manager()
        self.load(manager)
        self.providers.current_provider = dict(self.providers.current_provider, url="http://o")
        with patch("services.epg_manager.requests.get", side_effect=failing):
            manager.set_current_epg()
        self.assertEqual(self.titles(manager), [])


class SavedGuideTests(_GuideCase):
    """The saved guide is read in pieces, and only when the one shown is not it already."""

    def cache_files(self):
        return sorted(os.listdir(os.path.join(self.config.get_config_dir(), "cache", "epg")))

    def reads(self):
        return patch.object(
            epg_manager_module, "read_guide", side_effect=epg_manager_module.read_guide
        )

    def test_the_guide_is_saved_in_the_piecewise_format(self):
        manager = self.manager()
        self.load(manager)
        self.assertEqual(self.cache_files(), [f"{self.key}.guide", "index.json"])
        restarted = self.manager()
        with self.reads() as read:
            restarted.set_current_epg()
        read.assert_called_once()
        self.assertEqual(self.titles(restarted), ["Old"])

    def test_an_unchanged_saved_guide_already_shown_is_not_read_again(self):
        manager = self.manager()
        self.load(manager)
        with self.reads() as read:
            manager.set_current_epg()  # a provider switch or the periodic refresh
            manager.set_current_epg()
        read.assert_not_called()
        self.assertEqual(self.titles(manager), ["Old"])

    def test_a_saved_guide_is_read_again_once_it_changed_or_another_was_shown(self):
        manager = self.manager()
        self.load(manager)
        other = self.manager()  # another QiTV saves its guide meanwhile
        with patch("services.epg_manager.requests.get", side_effect=serving(xmltv("Elsewhere"))):
            other.force_refresh_current_epg()
        with self.reads() as read:
            manager.set_current_epg()
        read.assert_called_once()
        self.assertEqual(self.titles(manager), ["Elsewhere"])
        manager.reindex_programs()  # the guide shown is no longer the saved one
        with self.reads() as read:
            manager.set_current_epg()
        read.assert_called_once()

    def test_a_guide_saved_by_an_earlier_version_is_read_and_replaced(self):
        guide = MultiKeyDict()
        guide[("news.test", "News")] = [
            {
                "@start": "20261015200000 +0000",
                "@stop": "20261015210000 +0000",
                "title": {"__text": "Legacy"},
            }
        ]
        manager = self.manager()
        manager.index[self.key] = {
            "date": datetime.now().strftime(_INDEX_TIME_FORMAT),
            "last_access": datetime.now().strftime(_INDEX_TIME_FORMAT),
        }
        legacy = os.path.join(manager._cache_dir(), f"{self.key}.pkl")
        with open(legacy, "wb") as f:
            pickle.dump(guide, f)
        manager.set_current_epg()
        self.assertEqual(self.titles(manager), ["Legacy"])
        self.expire(manager)
        self.load(manager, "New")
        self.assertEqual(self.cache_files(), [f"{self.key}.guide", "index.json"])


if __name__ == "__main__":
    unittest.main()
