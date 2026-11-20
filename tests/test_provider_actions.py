"""One provider change in the drawer runs the TV window's follow-ups the old Settings window's
Save ran for it (the catalogs, the menus, what is kept under a name, the provider shown).
A fake window records them in order. Fictional hosts."""

import os
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.epg_view import GUIDE_OFFSET_KEY
from services.provider_changes import NameTaken
from tests.test_provider_changes import PANEL, PLAYLIST, manager_of, temp_manager
from ui.provider_actions import ProviderActions

REBUILT = ("menus.rebuild_providers",)
SAVED = ("providers_saved",)


class FakeWindow:
    """What ProviderActions calls on the TV window, as events in the order called."""

    def __init__(self, config, manager):
        self.config_manager = config
        self.provider_manager = manager
        self.events: list[tuple] = []
        self.created: dict[str, SimpleNamespace] = {}  # name -> a catalog made for it
        self.catalogs = SimpleNamespace(
            created=lambda: list(self.created), catalog=lambda name: self.created[name]
        )
        self.menus = SimpleNamespace(rebuild_providers=lambda: self.events.append(REBUILT))

    def catalog_for(self, name):
        manager = SimpleNamespace(
            invalidate_provider_cache=lambda n: self.events.append(("invalidate", "own", n))
        )
        self.created[name] = SimpleNamespace(provider_manager=manager)

    def providers_saved(self):
        self.events.append(SAVED)

    def providers_renamed(self, renames, removed=()):
        self.events.append(
            ("providers_renamed", dict(renames.names), sorted(renames.before), list(removed))
        )

    def set_provider(self, force_update=False):
        self.events.append(("set_provider", force_update))

    def _show_provider(self, name):
        self.events.append(("_show_provider", name))

    def save_config(self):
        self.events.append(("save_config", self.config_manager.selected_provider_name))


class ProviderActionsTests(unittest.TestCase):
    def setUp(self):
        self.config, self.manager = temp_manager(self, [PLAYLIST, PANEL])
        self.config.ssl_verify = True
        self.config.selected_provider_name = "Lists"
        self.window = FakeWindow(self.config, self.manager)
        listed = mock.patch.object(
            self.manager,
            "invalidate_provider_cache",
            side_effect=lambda n: self.window.events.append(("invalidate", "list", n)),
        )
        listed.start()
        self.addCleanup(listed.stop)
        self.actions = ProviderActions(self.window)

    def run_action(self, action, *args):
        """The events one action caused (its result is checked by the caller's assertEqual)."""
        self.window.events.clear()
        self.result = getattr(self.actions, action)(*args)
        return list(self.window.events)

    def test_actions_mirror_the_old_save_for_each_change(self):
        extra = {"type": "M3UPLAYLIST", "name": "Extra", "url": "http://extra.test/tv.m3u"}
        self.assertEqual(
            self.run_action("add", extra), [SAVED, ("_show_provider", "Extra"), REBUILT]
        )
        self.assertEqual(self.result, "Extra")
        self.config.selected_provider_name = "Lists"  # the real window's shows it: back
        # A new connection: a provider not shown loses its lists before the catalogs reload,
        # through its own catalog's manager when it has one (it holds the queued writes) ...
        self.assertEqual(
            self.run_action("update", "Panel", {"url": "http://panel.test:9090"}),
            [("invalidate", "list", "Panel"), SAVED, REBUILT],
        )
        self.assertTrue(self.result.identity_changed)
        self.window.catalog_for("Extra")
        self.assertEqual(
            self.run_action("update", "Extra", {"url": "http://extra.test/b.m3u"}),
            [("invalidate", "own", "Extra"), SAVED, REBUILT],
        )
        # ... and the one shown is set up again with it once they did.
        self.assertEqual(
            self.run_action("update", "Lists", {"url": "http://lists.test/b.m3u"}),
            [SAVED, ("set_provider", True), REBUILT],
        )
        # The same connection (the stream limit, the guide's offset): the catalogs only.
        self.assertEqual(self.run_action("update", "Lists", {"max_streams": 2}), [SAVED, REBUILT])
        self.assertFalse(self.result.identity_changed)
        self.assertEqual(
            self.run_action("update", "Panel", {GUIDE_OFFSET_KEY: 60}), [SAVED, REBUILT]
        )
        self.assertEqual(self.run_action("update", "Panel", {GUIDE_OFFSET_KEY: 60}), [])
        # A rename: what is kept under the old name moves, before the catalogs reload ...
        # (its lists under the old name go, through its catalog's manager when it has one).
        names = ["Extra", "Lists", "Panel"]
        self.assertEqual(
            self.run_action("rename", "Panel", "Films"),
            [
                ("providers_renamed", {"Panel": "Films"}, names, []),
                ("invalidate", "list", "Panel"),
                SAVED,
                REBUILT,
            ],
        )
        self.assertEqual(self.result, "Films")
        # ... and the one shown is set up again under its new name.
        self.window.catalog_for("Lists")
        self.assertEqual(
            self.run_action("rename", "Lists", "News"),
            [
                ("providers_renamed", {"Lists": "News"}, ["Extra", "Films", "Lists"], []),
                ("invalidate", "own", "Lists"),
                SAVED,
                ("set_provider", True),
                REBUILT,
            ],
        )
        self.config.selected_provider_name = "News"  # as the real window's carry_over moves it
        with self.assertRaises(NameTaken):
            self.run_action("rename", "Films", "news")
        self.assertEqual(self.window.events, [])
        self.assertEqual(self.run_action("rename", "Films", "Films"), [])  # nothing to follow
        # Removed (not the one shown): its recordings and its lists go, then the catalogs reload.
        self.assertEqual(
            self.run_action("remove", "Films"),
            [
                ("providers_renamed", {}, [], ["Films"]),
                ("invalidate", "list", "Films"),
                SAVED,
                REBUILT,
            ],
        )
        self.assertEqual(self.actions.changes.names(), ["News", "Extra"])

    def test_a_new_sign_in_brings_the_lists_again(self):
        # A media server signed in again keeps its address, user and device: the connection's
        # identity is the same, yet its lists were fetched with the token it replaces.
        home = {"type": "JELLYFIN", "name": "Home", "url": "http://media.test:8096"}
        self.actions.changes.add({**home, "username": "me", "token": "t1", "device_id": "d1"})
        self.assertEqual(
            self.run_action("update", "Home", {"token": "t2"}, True),
            [("invalidate", "list", "Home"), SAVED, REBUILT],
        )
        self.assertFalse(self.result.identity_changed)
        self.config.selected_provider_name = "Home"
        self.assertEqual(
            self.run_action("update", "Home", {"token": "t3"}, True),
            [SAVED, ("set_provider", True), REBUILT],
        )
        # Signed in again with the same token: its lists come again all the same (the old
        # window refreshed every provider verified), though nothing is written.
        self.assertEqual(
            self.run_action("update", "Home", {"token": "t3"}, True),
            [SAVED, ("set_provider", True), REBUILT],
        )
        self.assertEqual(self.actions.changes.get("Home")["token"], "t3")
        self.config.selected_provider_name = "Lists"
        self.assertEqual(
            self.run_action("update", "Home", {"token": "t3"}, True),
            [("invalidate", "list", "Home"), SAVED, REBUILT],
        )
        self.assertEqual(self.run_action("update", "Home", {"token": "t3"}), [])  # no sign-in
        self.assertEqual(self.run_action("update", "Home", {"token": "t4"}), [SAVED, REBUILT])

    def test_a_change_that_cannot_be_saved_has_no_follow_ups(self):
        # The list is as the disk holds it again: nothing to carry over, reload or hand over.
        extra = {"type": "M3UPLAYLIST", "name": "Extra", "url": "http://extra.test/tv.m3u"}
        cases = (
            ("add", (extra,)),
            ("update", ("Lists", {"url": "http://lists.test/b.m3u"})),
            ("rename", ("Lists", "News")),
            ("remove", ("Lists",)),
        )
        for action, args in cases:
            with self.subTest(action):
                self.window.events.clear()
                with mock.patch.object(
                    self.manager, "save_providers", side_effect=PermissionError()
                ):
                    with self.assertRaises(PermissionError):
                        getattr(self.actions, action)(*args)
                self.assertEqual(self.window.events, [])
                self.assertEqual(self.actions.changes.names(), ["Lists", "Panel"])
                self.assertEqual(self.config.selected_provider_name, "Lists")

    def test_removing_the_provider_shown_shows_the_next_or_none(self):
        self.manager.providers.append(
            {"type": "M3UPLAYLIST", "name": "Extra", "url": "http://extra.test/tv.m3u"}
        )
        self.manager.save_providers()
        self.window.catalog_for("Lists")
        removed = [("providers_renamed", {}, [], ["Lists"]), ("invalidate", "own", "Lists"), SAVED]
        self.assertEqual(
            self.run_action("remove", "Lists"),
            [*removed, ("save_config", "Panel"), ("set_provider", False), REBUILT],
        )
        self.assertEqual(self.config.selected_provider_name, "Panel")  # the one after it
        self.config.selected_provider_name = "Extra"  # the last one: the one before it
        self.assertEqual(
            self.run_action("remove", "Extra")[-3:],
            [("save_config", "Panel"), ("set_provider", False), REBUILT],
        )
        self.assertEqual(
            self.run_action("remove", "Panel"),  # none left: the TV shows its welcome card
            [
                ("providers_renamed", {}, [], ["Panel"]),
                ("invalidate", "list", "Panel"),
                SAVED,
                ("save_config", ""),
                ("set_provider", False),
                REBUILT,
            ],
        )
        self.assertEqual(self.config.selected_provider_name, "")


class CacheFileTests(unittest.TestCase):
    """2.12: a provider's cache is three files (its lists in pieces, the pages opened beside
    them, an older build's single file): what a change lets go of, on disk."""

    def setUp(self):
        self.config, self.manager = temp_manager(self, [PLAYLIST, PANEL])
        self.config.ssl_verify = True
        self.config.selected_provider_name = "Lists"
        self.window = FakeWindow(self.config, self.manager)
        self.actions = ProviderActions(self.window)

    def cache_files(self, name):
        """Provider `name`'s cache files, each written (what they hold is never read here)."""
        lists = self.manager._provider_cache_name(name)
        files = [lists, self.manager._pages_name(lists), self.manager._legacy_cache_name(lists)]
        for path in files:
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")
        return files

    def assert_gone(self, files):
        for path in files:
            self.assertFalse(os.path.exists(path), os.path.basename(path))

    def test_each_change_that_lets_go_of_the_lists_drops_every_file(self):
        own = manager_of(self, self.config)  # Panel's catalog's own (its queued writes)
        own.current_provider = own.providers[1]
        own.current_provider_content = {"vod": {"categories": []}}
        self.window.created["Panel"] = SimpleNamespace(provider_manager=own)
        files = self.cache_files("Panel")
        self.actions.update("Panel", {"url": "http://panel.test:9090"})  # a new connection
        self.assert_gone(files)
        self.assertEqual(own.current_provider_content, {})
        files = self.cache_files("Panel")
        self.config.ssl_verify = False  # Panel follows Verify SSL (Lists is the TV's)
        self.actions.global_ssl_changed()
        self.assert_gone(files)
        files = self.cache_files("Panel")
        self.actions.rename("Panel", "Films")
        self.assert_gone(files)
        files = self.cache_files("Films")
        self.actions.remove("Films")
        self.assert_gone(files)

    def test_a_file_the_system_refuses_is_logged_and_the_change_stands(self):
        # Windows refuses to remove a file being read (a search reading the lists): the list
        # is written by then, so the change is not said to have failed, its follow-ups run and
        # the other files go.
        for action, args, follow_ups in (
            ("update", ("Panel", {"url": "http://panel.test:9090"}), [SAVED, REBUILT]),
            ("rename", ("Panel", "Films"), [SAVED, REBUILT]),
        ):
            with self.subTest(action):
                files = self.cache_files("Panel")
                remove = os.remove

                def refusing(path, refused=files[0]):
                    if path == refused:
                        raise PermissionError(path)
                    remove(path)

                self.window.events.clear()
                with (
                    mock.patch("services.provider_manager.os.remove", refusing),
                    self.assertLogs(level="WARNING") as logs,  # (a rename's prune says it too)
                ):
                    getattr(self.actions, action)(*args)
                self.assertIn("ui.provider_actions", {record.name for record in logs.records})
                events = [e for e in self.window.events if e[0] != "providers_renamed"]
                self.assertEqual(events, follow_ups)
                self.assertTrue(os.path.exists(files[0]))  # refused
                self.assertFalse(os.path.exists(files[1]))
                self.assertFalse(os.path.exists(files[2]))
                os.remove(files[0])
        self.assertEqual(self.actions.changes.names(), ["Lists", "Films"])
        self.assertEqual(self.actions.changes.get("Films")["url"], "http://panel.test:9090")


if __name__ == "__main__":
    unittest.main()
