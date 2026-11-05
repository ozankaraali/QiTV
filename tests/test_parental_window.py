"""Parental controls in the window (2.8 review): hiding takes what was watched out of the
history and the last channel; locking (Lock now, or the hour running out) stops a hidden
channel that plays and takes the hidden ones out of zapping."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from test_tv_window import WindowCase, wait_until

from services.provider_ids import with_provider
from tests.fixtures import local_provider
from tests.fixtures.local_provider import PROVIDER_NAME


def stamped(index: int) -> dict:
    return with_provider(local_provider.channels()[index], PROVIDER_NAME)


class ParentalWindowTests(WindowCase):
    def guard(self):
        guard = self.window.parental
        guard.set_pin("1234")  # locked from here
        return guard

    def playing(self, name):
        controller = self.window.controller
        return wait_until(lambda: (controller._channel or {}).get("name") == name)

    def zap_names(self):
        return [c.get("name") for c in self.window.controller.zap.context.channels]

    def test_hiding_a_watched_channel_takes_it_out_of_the_history(self):
        config = self.window.config_manager
        watched = local_provider.channels()[1]
        config.watch_history = [
            {"item_data": watched, "item_type": "channel", "provider_name": PROVIDER_NAME}
        ]
        config.last_live_channel = {"provider_name": PROVIDER_NAME, "group_id": "", "item": watched}
        self.guard().hide_item("live", stamped(1))
        self.window.parental_changed()
        self.assertEqual(config.watch_history, [])
        self.assertIsNone(config.last_live_channel)  # QiTV doesn't start on it

    def test_lists_from_the_history_leave_the_hidden_out_while_locked(self):
        # An entry still in the history (an adult group named only once its list loads).
        config = self.window.config_manager
        watched = local_provider.channels()[1]
        entry = {"item_data": watched, "item_type": "channel", "provider_name": PROVIDER_NAME}
        config.watch_history = [entry]
        nav = self.window.controller.nav
        self.assertEqual(nav.recent_count(), 1)
        guard = self.guard()
        guard.hide_item("live", stamped(1))  # no parental_changed: the history keeps it
        self.assertEqual(nav.recent_count(), 0)
        guard.unlock("1234")
        self.assertEqual(nav.recent_count(), 1)
        guard.lock()
        flow = self.window.controller._flow
        config.last_live_channel = {"provider_name": PROVIDER_NAME, "group_id": "", "item": watched}
        self.assertIsNone(flow._last_channel(PROVIDER_NAME))

    def test_recordings_of_a_hidden_channel_are_not_listed_while_locked(self):  # review M9
        from services import recordings_library as rl

        pages = self.window.screens
        files = [
            {"id": "r1", "name": "Evening news", "path": "/r/1.ts", "_channel": "Channel 2"},
            {"id": "r2", "name": "Channel 3", "path": "/r/2.ts", "_channel": "Channel 3"},
        ]
        pages.recordings_library._files = lambda: files
        guard = self.guard()
        guard.hide_item("live", stamped(1))  # Channel 2
        self.assertEqual([r["id"] for r in pages.recordings_library.items(rl.ALL)], ["r2"])
        refreshed = []
        pages.library_controller.recordings_changed = lambda: refreshed.append(True)
        guard.unlock("1234")
        self.window.parental_changed()  # 2.9.7 review: the tab follows the lock
        self.assertEqual(refreshed, [True])
        self.assertEqual([r["id"] for r in pages.recordings_library.items(rl.ALL)], ["r1", "r2"])

    def test_locking_stops_a_hidden_channel_and_zapping_leaves_the_hidden_out(self):
        self.open_first_channel()
        self.assertTrue(self.playing("Channel 1"))
        guard = self.guard()
        guard.unlock("1234")
        guard.hide_item("live", stamped(1))  # Channel 2
        self.window.parental_changed()
        self.assertIn("Channel 2", self.zap_names())  # unlocked: still there
        guard.lock()
        self.window.parental_changed()
        self.assertNotIn("Channel 2", self.zap_names())
        self.assertEqual(self.window.controller._channel["name"], "Channel 1")  # plays on
        guard.unlock("1234")
        guard.hide_item("live", stamped(0))  # what plays
        guard.lock()
        self.window.parental_changed()
        self.assertIsNotNone(self.window.controller._stopped)

    def test_the_unlock_running_out_stops_a_hidden_channel(self):
        self.open_first_channel()
        self.assertTrue(self.playing("Channel 1"))
        guard = self.guard()
        guard.unlock("1234", seconds=0.3)
        guard.hide_item("live", stamped(0))
        self.window.parental_changed()
        self.assertIsNone(self.window.controller._stopped)  # unlocked for now
        self.assertTrue(wait_until(lambda: self.window.controller._stopped is not None))


if __name__ == "__main__":
    unittest.main()
