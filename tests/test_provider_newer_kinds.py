"""Providers of a kind older 2.x builds don't know (2.14: Plex) are kept beside index.json, in
index.more: an older build reads index.json whole and would list one it can't open ("PLEX"),
so it never sees them; this build lists every provider in its order. Fictional hosts."""

import os
import stat
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import orjson

from services import newer_kinds, private_files
from services.config_manager import ConfigManager
from services.provider_manager import ProviderManager
from tests.fixtures.local_provider import write_local_provider

PLAYLIST = {"type": "M3UPLAYLIST", "name": "Lists", "url": "http://lists.test/tv.m3u"}
PANEL = {"type": "XTREAM", "name": "Panel", "url": "http://panel.test", "username": "a"}
SERVER = {"type": "JELLYFIN", "name": "Films", "url": "http://films.test:8096", "token": "J"}
PLEX = {
    "type": "PLEX",
    "name": "Home Media",
    "url": "https://10-0-0-5.example.plex.direct:32400",
    "token": "plex-token",
    "server_id": "machine-home",
    "device_id": "client-1",
}
PLEX_2 = {**PLEX, "name": "A Friend's Films", "server_id": "machine-friend"}


class SplitAndMergeTest(unittest.TestCase):
    def test_a_list_splits_into_what_older_builds_read_and_the_rest_with_its_place(self):
        listed = [PLAYLIST, PLEX, PANEL, PLEX_2]
        older, newer = newer_kinds.split(listed)
        self.assertEqual(older, [PLAYLIST, PANEL])
        self.assertEqual(
            newer, [{"after": "Lists", "provider": PLEX}, {"after": "Panel", "provider": PLEX_2}]
        )
        self.assertEqual(newer_kinds.merged(older, newer), listed)

    def test_one_first_in_the_list_stays_first(self):
        older, newer = newer_kinds.split([PLEX, PLEX_2, PANEL])
        self.assertEqual(newer[0]["after"], "")
        self.assertEqual(newer[1]["after"], "Home Media")
        self.assertEqual(newer_kinds.merged(older, newer), [PLEX, PLEX_2, PANEL])

    def test_jellyfin_and_emby_stay_in_index_json(self):  # 2.8 builds read them
        older, newer = newer_kinds.split([SERVER, PLEX])
        self.assertEqual((older, len(newer)), ([SERVER], 1))

    def test_an_older_builds_change_meanwhile_keeps_each_after_its_neighbour_or_last(self):
        _, newer = newer_kinds.split([PLAYLIST, PLEX, PANEL, PLEX_2])
        # An older build removed Panel and added another playlist since.
        other = {"type": "M3UPLAYLIST", "name": "More", "url": "http://more.test/a.m3u"}
        merged = newer_kinds.merged([PLAYLIST, other], newer)
        names = [p["name"] for p in merged]
        self.assertEqual(names, ["Lists", "Home Media", "More", PLEX_2["name"]])

    def test_a_name_an_older_build_gave_meanwhile_wins(self):
        _, newer = newer_kinds.split([PLEX])
        taken = {"type": "M3UPLAYLIST", "name": "home media", "url": "http://x.test/a.m3u"}
        merged = newer_kinds.merged([taken], newer)
        self.assertEqual([p["name"] for p in merged], ["Home Media (2)", "home media"])

    def test_damaged_entries_are_left_out(self):
        whole = {"after": "Panel", "provider": PLEX}
        newer = [{"after": "x"}, "odd", {"provider": {"type": "PLEX"}}, whole]
        self.assertEqual(newer_kinds.merged([PANEL], newer), [PANEL, PLEX])
        self.assertEqual(newer_kinds.merged([PANEL], {"not": "a list"}), [PANEL])


class ProviderManagerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        env = mock.patch.dict(os.environ, {"QITV_CONFIG_DIR": directory.name})
        env.start()
        self.addCleanup(env.stop)
        self.dir = directory.name
        write_local_provider(self.dir, [dict(PLAYLIST), dict(PANEL)])
        self.config = ConfigManager()
        self.manager = self.new_manager()

    def new_manager(self):
        manager = ProviderManager(self.config)
        self.addCleanup(manager._cache_writer.shutdown, wait=True)
        return manager

    @property
    def more_file(self):
        return os.path.join(self.manager.provider_dir, newer_kinds.FILE_NAME)

    def older_builds_list(self):
        """What a 2.13 build lists: index.json, read whole."""
        with open(self.manager.index_file, "rb") as f:
            return orjson.loads(f.read())

    def save(self, providers):
        self.manager.providers[:] = [dict(p) for p in providers]
        self.manager.save_providers()

    def test_an_older_build_never_sees_a_plex_provider(self):
        self.save([PLAYLIST, PLEX, PANEL])
        self.assertEqual([p["type"] for p in self.older_builds_list()], ["M3UPLAYLIST", "XTREAM"])
        self.assertTrue(os.path.exists(self.more_file))
        self.assertEqual(self.new_manager().providers, [PLAYLIST, PLEX, PANEL])

    def test_its_file_is_this_users_only_and_named_so_no_cache_pruning_takes_it(self):
        self.save([PLAYLIST, PLEX])
        if os.name == "posix":
            mode = stat.S_IMODE(os.stat(self.more_file).st_mode)
            self.assertEqual(mode & 0o077, 0)
        for suffix in (".json", ".jsonl"):  # what any build's pruning treats as a cache
            self.assertFalse(newer_kinds.FILE_NAME.endswith(suffix))

    def test_an_older_builds_save_leaves_the_plex_providers_for_this_build(self):
        self.save([PLAYLIST, PLEX, PANEL])
        # The older build saves the list it read, a provider added at its end.
        added = {"type": "M3UPLAYLIST", "name": "News", "url": "http://news.test/a.m3u"}
        read = self.older_builds_list()
        with open(self.manager.index_file, "wb") as f:
            f.write(orjson.dumps(read + [added]))
        names = [p["name"] for p in self.new_manager().providers]
        self.assertEqual(names, ["Lists", "Home Media", "Panel", "News"])

    def test_the_file_goes_once_no_plex_provider_is_left(self):
        self.save([PLAYLIST, PLEX])
        self.save([PLAYLIST])
        self.assertFalse(os.path.exists(self.more_file))
        self.assertEqual(self.new_manager().providers, [PLAYLIST])

    def test_no_file_while_no_provider_needs_one(self):
        self.save([PLAYLIST, SERVER])
        self.assertFalse(os.path.exists(self.more_file))

    def test_a_plex_providers_cache_is_kept_by_this_builds_pruning(self):
        self.save([PLAYLIST, PLEX])
        cache = self.manager._provider_cache_name(PLEX["name"])
        with open(cache, "wb") as f:
            f.write(b"{}\n")
        self.save([PLAYLIST, PLEX])
        self.assertTrue(os.path.exists(cache))

    def test_an_unreadable_index_json_keeps_the_plex_providers(self):
        self.save([PLAYLIST, PLEX])
        with open(self.manager.index_file, "wb") as f:
            f.write(b"[{")  # cut short
        listed = self.new_manager().providers
        self.assertIn(PLEX, listed)
        self.assertIn(PLEX, self.new_manager().providers)  # saved again with the free list

    def test_an_unreadable_index_more_is_set_aside_and_the_rest_loads(self):
        with open(self.more_file, "wb") as f:
            f.write(b"{not json")
        with self.assertLogs("services.newer_kinds", "WARNING"):
            listed = self.new_manager().providers
        self.assertEqual(listed, [PLAYLIST, PANEL])
        aside = [n for n in os.listdir(self.manager.provider_dir) if ".broken-" in n]
        self.assertEqual(len(aside), 1)

    def test_a_newer_format_is_left_as_it_is(self):
        with open(self.more_file, "wb") as f:
            f.write(orjson.dumps({"version": 99, "providers": [{"provider": PLEX}]}))
        self.assertEqual(self.new_manager().providers, [PLAYLIST, PANEL])
        self.assertTrue(os.path.exists(self.more_file))

    def test_the_privacy_sweep_covers_it(self):
        self.save([PLAYLIST, PLEX])
        if os.name != "posix":
            return
        os.chmod(self.more_file, 0o644)
        private_files.tighten(self.dir, None, system="Linux")
        self.assertEqual(stat.S_IMODE(os.stat(self.more_file).st_mode) & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
