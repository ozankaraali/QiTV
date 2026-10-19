"""A guide refresh never blanks Now/Next, and a failed refresh keeps the guide it had."""

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import pickle
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from services import epg_manager as epg_manager_module, epg_store as epg_store_module
from services.epg_manager import (
    _INDEX_TIME_FORMAT,
    EpgManager,
    _cache_key,
    _http_date,
    _xtream_cache_key,
)
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

    def close(self):
        self.closed = True


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
        programmes = manager.get_programs_for_channel(CHANNEL)
        return [p["title"] for p in programmes]

    def load(self, manager, title="Old"):
        with patch("services.epg_manager.requests.get", side_effect=serving(xmltv(title))):
            manager.set_current_epg()
        self.assertEqual(self.titles(manager), [title])

    def expire(self, manager):
        old = (datetime.now() - timedelta(days=2)).strftime(_INDEX_TIME_FORMAT)
        manager.index[self.key]["date"] = old
        manager.save_index()


class IfModifiedSinceTests(unittest.TestCase):
    def test_the_date_is_an_english_http_date(self):
        self.assertEqual(
            _http_date(datetime(1994, 11, 6, 8, 49, 37)), "Sun, 06 Nov 1994 08:49:37 GMT"
        )
        self.assertEqual(
            _http_date(datetime(2026, 3, 4, 23, 5, 9)), "Wed, 04 Mar 2026 23:05:09 GMT"
        )

    def test_the_date_does_not_follow_the_locale(self):
        # Qt switches the process to the system locale on Unix: strftime then names the day
        # and month in that language, which servers ignore.
        import locale

        previous = locale.setlocale(locale.LC_TIME)
        self.addCleanup(locale.setlocale, locale.LC_TIME, previous)
        for name in ("tr_TR.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8", "tr_TR", "de_DE"):
            try:
                locale.setlocale(locale.LC_TIME, name)
                break
            except locale.Error:
                continue
        else:
            self.skipTest("no non-English locale installed")
        self.assertEqual(
            _http_date(datetime(2026, 3, 4, 23, 5, 9)), "Wed, 04 Mar 2026 23:05:09 GMT"
        )


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
        self.assertEqual(manager.get_programs_for_channel({"id": "7"}), [programme])
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


class SourceTests(_GuideCase):
    """Each configured source is fetched by its kind's fetch, with its own arguments."""

    def setUp(self):
        super().setUp()
        self.config.epg_file = os.path.join(self.config.get_config_dir(), "guide.xml")
        self.config.epg_url = "http://g.test/guide.xml"
        self.fetched = []
        for kind in ("stb", "xtream", "file", "url"):
            fetch = self.recorder(kind)
            self.enterContext(patch.object(EpgManager, f"_fetch_epg_from_{kind}", fetch))

    def recorder(self, kind):
        return lambda manager, *args: self.fetched.append((kind, args))

    def cases(self):
        xtream = self.providers.current_provider
        portal = {"type": "STB", "name": "S", "url": "http://s/c/"}
        path, url = self.config.epg_file, self.config.epg_url
        return [
            ("STB", portal, "stb", _cache_key("http://s/c/"), ("http://s/c/",)),
            ("STB", xtream, "xtream", self.key, ("http://h", "u", "p")),
            ("Local File", xtream, "file", _cache_key(path), (path,)),
            ("URL", xtream, "url", _cache_key(url), (url,)),
        ]

    def test_each_source_is_fetched_by_its_kind(self):
        for epg_source, provider, kind, key, args in self.cases():
            with self.subTest(kind):
                self.config.epg_source, self.providers.current_provider = epg_source, provider
                manager = self.manager()
                self.assertEqual(manager._source(), (kind, key, args))
                manager.set_current_epg()
                manager.index[key] = None  # a source without a guide is not fetched again
                manager.set_current_epg()
                manager.force_refresh_current_epg()  # unless asked to
                # A URL's fetch also gets how many guides of it were stored when the load began
                # (None: when the fetch begins).
                seen, now = ((0,), (None,)) if kind == "url" else ((), ())
                self.assertEqual(self.fetched, [(kind, args + seen), (kind, args + now)])
                self.fetched.clear()

    def test_without_a_source_nothing_is_fetched(self):
        self.providers.current_provider = {"type": "M3UPLAYLIST", "name": "M", "url": "http://m"}
        manager = self.manager()
        self.assertIsNone(manager._source())
        manager.set_current_epg()
        manager.force_refresh_current_epg()
        self.assertEqual(self.fetched, [])


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

    def guide_path(self):
        return os.path.join(self.config.get_config_dir(), "cache", "epg", f"{self.key}.guide")

    def assert_unreadable_guide_is_fetched_again(self, damage):
        self.load(self.manager())
        path = self.guide_path()
        damage(path)
        restarted = self.manager()  # a new session reads the damaged file
        get = patch("services.epg_manager.requests.get", side_effect=serving(xmltv("New")))
        with get as fetch, self.assertLogs("services.epg_manager", "WARNING") as logged:
            restarted.set_current_epg()
        fetch.assert_called_once()  # not an empty guide with no fetch
        self.assertEqual(len(logged.records), 1)  # logged once, without the path or contents
        self.assertNotIn(self.config.get_config_dir(), logged.output[0])
        self.assertEqual(self.titles(restarted), ["New"])
        self.assertEqual(sorted(epg_store_module.read_guide(path).items())[0][1][0]["title"], "New")
        with get as fetch:  # and the next start reads the new file
            self.manager().set_current_epg()
        fetch.assert_not_called()

    def test_a_truncated_saved_guide_is_removed_and_fetched_again(self):
        # It stayed unreadable, and the guide stayed empty, until the index expired.
        def truncate(path):
            with open(path, "rb+") as f:
                f.truncate(os.path.getsize(path) // 2)

        self.assert_unreadable_guide_is_fetched_again(truncate)

    def test_a_saved_guide_of_a_future_version_is_removed_and_fetched_again(self):
        def future(path):
            with open(path, "wb") as f:
                pickle.dump((epg_store_module._MAGIC, epg_store_module._VERSION + 1, "packed"), f)

        self.assert_unreadable_guide_is_fetched_again(future)

    def test_reindexing_the_guide_unpickles_no_channel(self):
        # Re-keying a saved guide on the GUI thread read every channel back.
        self.load(self.manager())
        manager = self.manager()  # a new session: the guide comes off the disk, still packed
        manager.set_current_epg()
        self.assertEqual(manager.epg.packed(), 1)
        self.config.xmltv_channel_map[("news.test", "extra.id")] = None
        decodes, real_loads = [], pickle.loads

        def count(blob, *args, **kwargs):
            decodes.append(len(blob))
            return real_loads(blob, *args, **kwargs)

        with patch.object(epg_store_module.pickle, "loads", side_effect=count):
            manager.reindex_programs()
        self.assertEqual(decodes, [])
        self.assertEqual(manager.epg.packed(), 1)  # still pickled
        extra = manager.get_programs_for_channel({"xmltv_id": "extra.id", "name": "Other"})
        self.assertEqual([p["title"] for p in extra], ["Old"])  # under its new keys
        self.assertEqual(self.titles(manager), ["Old"])

    def test_reindexing_publishes_the_new_guide_like_every_other_change(self):
        # Every guide change is one swap through _publish (it records the guide's source).
        self.load(self.manager())
        manager = self.manager()
        manager.set_current_epg()
        source, old = manager._epg_source, manager.epg
        self.config.xmltv_channel_map[("news.test", "extra.id")] = None
        with patch.object(manager, "_publish", wraps=manager._publish) as publish:
            manager.reindex_programs()
        publish.assert_called_once_with(source, manager.epg)
        self.assertIsNot(manager.epg, old)
        self.assertEqual(manager._epg_source, source)

    def test_a_1x_pickled_guide_is_ignored_and_the_guide_is_fetched(self):
        # Only 1.x saved the guide as <key>.pkl; every 2.x saves <key>.guide.
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
        get = patch("services.epg_manager.requests.get", side_effect=serving(xmltv("New")))
        with get as fetch, patch.object(pickle, "load") as unpickle:
            manager.set_current_epg()
        fetch.assert_called_once()
        unpickle.assert_not_called()
        self.assertEqual(self.titles(manager), ["New"])
        self.assertIn(f"{self.key}.guide", self.cache_files())


class SharedIndexTests(_GuideCase):
    """Every provider's manager shares one index file: saving never wipes another's entries."""

    def test_two_managers_saving_in_turn_keep_each_others_entries(self):
        first, second = self.manager(), self.manager()  # both read the empty index
        first.index["a"] = {"date": "2026-10-15 20:00:00", "last_access": "2026-10-15 20:00:00"}
        second.index["b"] = None
        first.save_index()
        second.save_index()  # its copy never saw "a"
        third = self.manager()
        self.assertEqual(set(third.index), {"a", "b"})
        self.assertIsNone(third.index["b"])

    def test_a_removed_entry_is_removed_from_the_file(self):
        first = self.manager()
        first.index["a"] = None
        first.save_index()
        second = self.manager()
        del second.index["a"]
        second.index["b"] = None
        second.save_index()
        self.assertEqual(set(self.manager().index), {"b"})

    def test_a_single_managers_existing_index_reads_unchanged(self):
        first = self.manager()
        first.index["a"] = None
        first.save_index()
        self.assertEqual(self.manager().index, {"a": None})

    def test_concurrent_saves_keep_every_managers_entries(self):
        managers = [self.manager() for _ in range(6)]
        barrier = threading.Barrier(len(managers))
        errors = []

        def save(n, manager):
            manager.index[f"k{n}"] = None
            barrier.wait()
            try:
                manager.save_index()
            except Exception as e:  # noqa: BLE001 - the test reports any failure
                errors.append(e)

        threads = [threading.Thread(target=save, args=(n, m)) for n, m in enumerate(managers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(set(self.manager().index), {f"k{n}" for n in range(6)})
        leftovers = [
            f for f in os.listdir(os.path.dirname(managers[0]._index_file())) if f.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])


class SharedUrlGuideTests(_GuideCase):
    """The URL guide is global: each provider in use loads it with its own manager."""

    def test_two_managers_download_a_shared_url_guide_once(self):
        # Both wrote the same <hash>.xml.download (a forced refresh), and the one that
        # waited downloaded and parsed it again; it reads the guide the other just stored.
        self.config.epg_source = "URL"
        self.config.epg_url = "http://127.0.0.1:9/guide.xml"
        managers = [self.manager(), self.manager()]
        guard, active, overlaps, errors = threading.Lock(), [0], [], []
        barrier = threading.Barrier(len(managers))

        class Slow(_Response):
            def __init__(self, body):
                super().__init__(body)
                self.headers = {"Last-Modified": "Sun, 18 Oct 2026 02:00:00 GMT"}

            def iter_content(self, size):
                with guard:
                    active[0] += 1
                    overlaps.append(active[0])
                time.sleep(0.05)
                yield self.content
                with guard:
                    active[0] -= 1

        def refresh(manager):
            barrier.wait()
            try:
                manager.force_refresh_current_epg()
            except Exception as e:  # noqa: BLE001 - the test reports any failure
                errors.append(e)

        get = lambda url, **kwargs: Slow(xmltv("New"))  # noqa: E731
        with patch("services.epg_manager.requests.get", side_effect=get):
            threads = [threading.Thread(target=refresh, args=(m,)) for m in managers]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(overlaps, [1])  # one download, never two at once
        self.assertEqual([self.titles(m) for m in managers], [["New"], ["New"]])
        cache = os.path.dirname(managers[0]._index_file())
        self.assertEqual([f for f in os.listdir(cache) if f.endswith((".xml", ".download"))], [])

    def test_a_manager_reads_the_url_guide_another_stored_since_it_read_the_index(self):
        # Each provider's manager read the shared index when its catalog was made; the
        # guide another provider downloaded later was downloaded and parsed again.
        self.config.epg_source = "URL"
        self.config.epg_url = "http://127.0.0.1:9/guide.xml"
        first, second = self.manager(), self.manager()
        with patch("services.epg_manager.requests.get", side_effect=serving(xmltv("New"))) as get:
            first.set_current_epg()
            second.set_current_epg()
        self.assertEqual(get.call_count, 1)
        self.assertEqual(self.titles(second), ["New"])

    def test_a_waiter_downloads_when_nothing_was_stored_while_it_waited(self):
        # The waiter took any change of the shared index entry while it waited (here a
        # third manager saving when it last read the guide, while the download it waited
        # for failed) for a guide just stored, and downloaded nothing.
        self.config.epg_source = "URL"
        self.config.epg_url = "http://127.0.0.1:9/guide.xml"
        with patch("services.epg_manager.requests.get", side_effect=serving(xmltv("Old"))):
            self.manager().set_current_epg()
        waiter, third = self.manager(), self.manager()
        key, lock = _cache_key(self.config.epg_url), epg_manager_module._source_lock

        def meanwhile(cache_key):
            third.index[key]["last_access"] = "2099-01-01 00:00:00"
            third.save_index()
            return lock(cache_key)

        get = patch("services.epg_manager.requests.get", side_effect=serving(xmltv("New")))
        with patch.object(epg_manager_module, "_source_lock", side_effect=meanwhile), get as got:
            waiter.force_refresh_current_epg()
        self.assertEqual(got.call_count, 1)
        self.assertEqual(self.titles(waiter), ["New"])

    def test_a_local_file_guide_is_parsed_once_for_two_managers(self):
        path = os.path.join(self.config.get_config_dir(), "guide.xml")
        with open(path, "wb") as f:
            f.write(xmltv("Local"))
        self.config.epg_source = "Local File"
        self.config.epg_file = path
        first, second = self.manager(), self.manager()
        parse, parsed = EpgManager._index_programs, []

        def counting(manager, xmltv_file):
            parsed.append(xmltv_file)
            return parse(manager, xmltv_file)

        with patch.object(EpgManager, "_index_programs", counting):
            first.set_current_epg()
            second.set_current_epg()
        self.assertEqual(parsed, [path])
        self.assertEqual(self.titles(second), ["Local"])

    def test_a_shared_url_guide_that_failed_for_one_manager_is_tried_by_the_next(self):
        self.config.epg_source = "URL"
        self.config.epg_url = "http://127.0.0.1:9/guide.xml"
        first, second = self.manager(), self.manager()
        with patch("services.epg_manager.requests.get", side_effect=serving(b"<tv></tv>")):
            first.set_current_epg()  # an answer without programmes: no guide
        with patch("services.epg_manager.requests.get", side_effect=serving(xmltv("New"))) as get:
            second.set_current_epg()
        self.assertEqual(get.call_count, 1)
        self.assertEqual(self.titles(second), ["New"])


class UrlGuideDateTests(_GuideCase):
    """A URL guide is stored whatever its server says of its date (pre-existing: without a
    Last-Modified header the store raised ValueError, and the guide was never kept)."""

    def serve(self, last_modified):
        """Serve one guide on 127.0.0.1 with `last_modified` (None: no such header) as the
        URL guide; returns the requests it gets."""
        body, seen = xmltv("Served"), []

        class Guide(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(body)))
                if last_modified is not None:
                    self.send_header("Last-Modified", last_modified)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Guide)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.config.epg_source = "URL"
        self.config.epg_url = f"http://127.0.0.1:{server.server_port}/guide.xml"
        return seen

    def stored_date(self, manager):
        manager_key = _cache_key(self.config.epg_url)
        self.assertTrue(os.path.exists(manager._guide_path(manager_key)))
        return datetime.strptime(manager.index[manager_key]["date"], _INDEX_TIME_FORMAT)

    def assert_download_time(self, date):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertLess(abs(date - now), timedelta(minutes=1))

    def test_a_guide_from_a_server_without_last_modified_is_stored(self):
        seen = self.serve(None)
        manager = self.manager()
        manager.set_current_epg()
        self.assertEqual(self.titles(manager), ["Served"])
        self.assert_download_time(self.stored_date(manager))  # in UTC, as Last-Modified is
        again = self.manager()  # the next start reads the saved guide
        again.set_current_epg()
        self.assertEqual(self.titles(again), ["Served"])
        self.assertEqual(seen, ["/guide.xml"])

    def test_an_unreadable_last_modified_is_taken_as_the_download_time(self):
        self.serve("yesterday")
        manager = self.manager()
        manager.set_current_epg()
        self.assertEqual(self.titles(manager), ["Served"])
        self.assert_download_time(self.stored_date(manager))

    def test_a_stale_guide_is_checked_by_its_status_alone_then_fetched(self):
        seen = self.serve(None)  # this server ignores If-Modified-Since: always the guide
        manager = self.manager()
        manager.set_current_epg()
        self.config.epg_expiration = -1  # the saved guide is out of date at once
        get, asked = requests.get, []

        def recording(url, **kwargs):
            asked.append(kwargs)
            return get(url, **kwargs)

        with patch("services.epg_manager.requests.get", side_effect=recording):
            manager.set_current_epg()
        self.assertEqual(seen, ["/guide.xml"] * 3)  # the first load, the check, the fetch
        # Neither answer is read into memory whole (a guide can take hundreds of MB).
        self.assertEqual([kwargs.get("stream") for kwargs in asked], [True, True])
        self.assertEqual(self.titles(manager), ["Served"])

    def test_the_servers_last_modified_is_the_guides_date(self):
        self.serve("Sun, 18 Oct 2026 02:00:00 GMT")
        manager = self.manager()
        manager.set_current_epg()
        self.assertEqual(self.stored_date(manager), datetime(2026, 10, 18, 2, 0, 0))


if __name__ == "__main__":
    unittest.main()
