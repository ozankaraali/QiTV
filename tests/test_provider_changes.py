"""Settings in the drawer changes one provider at a time: each change is written as the old
Settings window's Save wrote the provider list (the same fields, older 2.x builds read them).
Fictional hosts."""

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.config_manager import ConfigManager
from services.epg_view import GUIDE_OFFSET_KEY
from services.provider_changes import NameTaken, ProviderChanges
from services.provider_manager import ProviderManager
from tests.fixtures.local_provider import write_local_provider

PLAYLIST = {"type": "M3UPLAYLIST", "name": "Lists", "url": "http://lists.test/tv.m3u"}
PANEL = {
    "type": "XTREAM",
    "name": "Panel",
    "url": "http://panel.test:8080",
    "username": "viewer",
    "password": "secret",
}


def temp_manager(case, providers):
    """A ConfigManager and a ProviderManager over `providers` in a temporary config dir."""
    directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    case.addCleanup(directory.cleanup)
    env = mock.patch.dict(os.environ, {"QITV_CONFIG_DIR": directory.name})
    env.start()
    case.addCleanup(env.stop)
    write_local_provider(directory.name, [dict(p) for p in providers])
    config = ConfigManager()
    return config, manager_of(case, config)


def manager_of(case, config):
    manager = ProviderManager(config)
    case.addCleanup(manager._cache_writer.shutdown, wait=True)
    return manager


class ProviderChangesTests(unittest.TestCase):
    def setUp(self):
        self.config, self.manager = temp_manager(self, [PLAYLIST, PANEL])
        self.config.ssl_verify = True
        self.changes = ProviderChanges(self.manager, lambda: bool(self.config.ssl_verify))

    def saved(self):
        """The provider list as index.json holds it now (read anew)."""
        return manager_of(self, self.config).providers

    def saved_entry(self, name):
        return next(p for p in self.saved() if p["name"] == name)

    def test_add_makes_the_name_unique_and_saves(self):
        name = self.changes.add({"type": "M3UPLAYLIST", "name": "lists", "url": "http://b.test/"})
        self.assertEqual(name, "lists (2)")
        self.assertEqual([p["name"] for p in self.saved()], ["Lists", "Panel", "lists (2)"])
        self.assertEqual(self.changes.names(), ["Lists", "Panel", "lists (2)"])
        # Unnamed: its address, a playlist file's name without its folder (made unique too).
        path = os.path.join(self.config.get_config_dir(), "Sports.m3u")
        self.assertEqual(self.changes.add({"type": "M3UPLAYLIST", "url": path}), "Sports")
        self.assertEqual(self.changes.add({"type": "M3UPLAYLIST", "url": path}), "Sports (2)")
        self.assertEqual(
            self.changes.add({"type": "M3UPLAYLIST", "name": " ", "url": "http://c.test/x"}),
            "http://c.test/x",
        )
        with self.assertRaises(ValueError):
            self.changes.add({"type": "M3UPLAYLIST", "name": "", "url": ""})  # no name at all
        # A new one keeps none of the defaults: Automatic streams, no offset, the global check.
        entry = {**PLAYLIST, "name": "New", "max_streams": 0, GUIDE_OFFSET_KEY: 0}
        entry["ssl_verify"] = True
        self.changes.add(entry)
        self.assertEqual(
            self.saved_entry("New"),
            {"type": "M3UPLAYLIST", "name": "New", "url": "http://lists.test/tv.m3u"},
        )
        self.assertEqual(entry["name"], "New")  # the caller's dict is not the one listed
        self.assertIsNot(self.changes.get("New"), self.manager.providers[-1])

    def test_update_reports_an_identity_change_only_for_identity_fields(self):
        change = self.changes.update("Panel", {"url": "http://panel.test:9090"})
        self.assertTrue(change.identity_changed)
        self.assertEqual(change.fields, {"url"})
        change = self.changes.update("Panel", {"username": "other"})
        self.assertTrue(change.identity_changed)
        self.assertEqual(change.fields, {"username"})
        change = self.changes.update("Panel", {"max_streams": 2})
        self.assertFalse(change.identity_changed)
        self.assertEqual(change.fields, {"max_streams"})
        change = self.changes.update("Panel", {GUIDE_OFFSET_KEY: 90})
        self.assertFalse(change.identity_changed)
        self.assertEqual(change.fields, {GUIDE_OFFSET_KEY})
        change = self.changes.update("Panel", {"username": "other"})  # as it is already
        self.assertEqual((change.identity_changed, change.fields), (False, set()))
        self.assertEqual(self.saved_entry("Panel")["url"], "http://panel.test:9090")
        self.assertEqual(self.saved_entry("Panel")["username"], "other")
        # ssl_verify counts as the global one when the provider has none of its own.
        change = self.changes.update("Panel", {"ssl_verify": False})
        self.assertTrue(change.identity_changed)
        with self.assertRaises(ValueError):
            self.changes.update("Panel", {"name": "Renamed"})  # rename() renames
        with self.assertRaises(KeyError):
            self.changes.update("Nobody", {"url": "http://x.test"})

    def test_offset_zero_pops_the_key_and_max_streams_is_kept_only_if_set(self):
        self.assertEqual(self.changes.update("Lists", {"max_streams": 0}).fields, set())
        self.assertNotIn("max_streams", self.saved_entry("Lists"))  # Automatic: no key
        self.changes.update("Lists", {"max_streams": 3})
        self.assertEqual(self.saved_entry("Lists")["max_streams"], 3)
        self.changes.update("Lists", {"max_streams": 0})
        self.assertEqual(self.saved_entry("Lists")["max_streams"], 0)  # the key keeps its 0
        self.changes.update("Lists", {GUIDE_OFFSET_KEY: -120})
        self.assertEqual(self.saved_entry("Lists")[GUIDE_OFFSET_KEY], -120)
        change = self.changes.update("Lists", {GUIDE_OFFSET_KEY: 0})
        self.assertEqual(change.fields, {GUIDE_OFFSET_KEY})
        self.assertNotIn(GUIDE_OFFSET_KEY, self.saved_entry("Lists"))

    def test_ssl_verify_is_written_only_when_it_differs_or_was_there(self):
        self.assertEqual(self.changes.update("Lists", {"ssl_verify": True}).fields, set())
        self.assertNotIn("ssl_verify", self.saved_entry("Lists"))  # as the global one
        self.changes.update("Lists", {"ssl_verify": False})
        self.assertIs(self.saved_entry("Lists")["ssl_verify"], False)
        change = self.changes.update("Lists", {"ssl_verify": True})
        self.assertEqual(change.fields, {"ssl_verify"})
        self.assertIs(self.saved_entry("Lists")["ssl_verify"], True)  # there: kept explicit
        self.config.ssl_verify = False  # the global setting off: True now differs from it
        self.changes.update("Panel", {"ssl_verify": True})
        self.assertIs(self.saved_entry("Panel")["ssl_verify"], True)

    def test_rename_refuses_a_taken_name_case_insensitively(self):
        with self.assertRaises(NameTaken) as refused:
            self.changes.rename("Lists", "PANEL")
        self.assertEqual(refused.exception.name, "PANEL")
        self.assertEqual([p["name"] for p in self.saved()], ["Lists", "Panel"])
        self.assertEqual(self.changes.rename("Lists", "LISTS"), "LISTS")  # its own, in capitals
        self.assertEqual(self.changes.rename("LISTS", "News"), "News")
        self.assertEqual([p["name"] for p in self.saved()], ["News", "Panel"])
        self.assertEqual(self.saved_entry("News")["url"], PLAYLIST["url"])
        self.assertEqual(self.changes.rename("News", " "), PLAYLIST["url"])  # blank: its address
        with self.assertRaises(KeyError):
            self.changes.rename("Nobody", "Somebody")

    def test_a_media_servers_password_is_never_stored(self):
        server = {
            "type": "JELLYFIN",
            "name": "Home",
            "url": "http://media.test:8096",
            "username": "viewer",
            "password": "secret",
            "token": "t1",
        }
        self.changes.add(server)
        self.assertNotIn("password", self.saved_entry("Home"))
        self.assertEqual(self.saved_entry("Home")["token"], "t1")
        self.changes.update("Home", {"password": "again", "token": "t2"})
        self.assertNotIn("password", self.saved_entry("Home"))
        self.assertEqual(self.saved_entry("Home")["token"], "t2")
        self.changes.update("Panel", {"type": "EMBY"})  # a login made a media server's
        self.assertNotIn("password", self.saved_entry("Panel"))

    def test_remove_saves_and_forgets(self):
        cache = self.manager._provider_cache_name("Panel")
        with open(cache, "w", encoding="utf-8") as f:
            f.write("{}")
        self.changes.remove("Panel")
        self.assertEqual(self.changes.names(), ["Lists"])
        self.assertEqual([p["name"] for p in self.saved()], ["Lists"])
        self.assertFalse(os.path.exists(cache))  # its lists went with it
        with self.assertRaises(KeyError):
            self.changes.get("Panel")
        self.changes.remove("Lists")
        self.assertEqual(self.saved(), [])

    def test_a_change_keeps_the_shared_list_and_lists_a_new_entry(self):
        listed = self.manager.providers
        before = listed[1]
        self.changes.update("Panel", {"url": "http://panel.test:9090"})
        self.assertIs(self.manager.providers, listed)  # pinned managers share it
        self.assertIsNot(listed[1], before)  # one holding the old entry keeps it as it was
        self.assertEqual(before["url"], PANEL["url"])
        self.assertEqual(listed[1]["url"], "http://panel.test:9090")

    def test_a_list_that_cannot_be_saved_is_left_as_the_disk_holds_it(self):
        # Every change, its write refused: the list in memory is what the disk holds (the
        # shared list object too), and the error goes on to the caller.
        listed = self.manager.providers
        on_disk = [dict(p) for p in self.saved()]
        changes = (
            ("add", ({"type": "M3UPLAYLIST", "name": "New", "url": "http://n.test/"},)),
            ("update", ("Panel", {"url": "http://panel.test:9090", "max_streams": 2})),
            ("rename", ("Panel", "Films")),
            ("remove", ("Lists",)),
        )
        for name, args in changes:
            with self.subTest(name):
                with mock.patch.object(
                    self.manager, "save_providers", side_effect=PermissionError("index.json")
                ):
                    with self.assertRaises(PermissionError):
                        getattr(self.changes, name)(*args)
                self.assertIs(self.manager.providers, listed)
                self.assertEqual(listed, on_disk)
                self.assertEqual(self.saved(), on_disk)

    def test_a_stale_cache_that_cannot_go_does_not_fail_the_save(self):
        # The list is written first; a cache file left of a name no longer listed that cannot
        # be removed is only logged (the save stands, memory and disk agree).
        cache = self.manager._provider_cache_name("Panel")
        with open(cache, "w", encoding="utf-8") as f:
            f.write("{}")
        with (
            mock.patch("services.provider_manager.os.remove", side_effect=PermissionError()),
            self.assertLogs("services.provider_manager", "WARNING"),
        ):
            self.changes.rename("Panel", "Films")
        self.assertEqual([p["name"] for p in self.saved()], ["Lists", "Films"])
        self.assertEqual(self.changes.names(), ["Lists", "Films"])


if __name__ == "__main__":
    unittest.main()
