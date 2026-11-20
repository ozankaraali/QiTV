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
from tests.test_provider_changes import PANEL, PLAYLIST, temp_manager
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
        self.assertEqual(self.run_action("update", "Home", {"token": "t3"}, True), [])  # the same
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


if __name__ == "__main__":
    unittest.main()
