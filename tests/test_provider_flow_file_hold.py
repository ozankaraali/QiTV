"""A start with a file plays only the file (spec §3).

While a file opened in QiTV is on its way or plays, a provider that becomes ready tunes nothing
and opens no drawer, the welcome card waits, and what waits for QiTV to be idle (the startup
update question) runs once the file ended, stopped, failed, turned out missing or gave way to
another title. Fakes for the controller's catalog and player; a real window over no provider.
"""

import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_tv_controller import CHANNELS, HARBOR_ONE, ControllerTestCase, FakeOwnCatalog
from timer_fixtures import wait_until

from tests import test_tv_window as base
from ui.keymap import Action

LAST = {"provider_name": "Home", "group_id": "10", "item": CHANNELS[2]}
FILM = "/videos/Dizi Bölüm 1.mkv"


class FileHoldTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.runs: list[str] = []  # the when_idle callbacks that ran

    def when_idle(self, name="update question"):
        self.controller.when_idle(lambda: self.runs.append(name))

    def film(self, path=FILM):
        """A file opened in QiTV plays (TVWindow.open_paths holds first)."""
        self.controller.hold_for_file(True)
        self.controller.play_file(path, size=1234)
        self.session.stateChanged.emit("playing")

    def test_provider_ready_while_held_tunes_nothing(self):
        self.config.last_live_channel = dict(LAST)
        self.controller.hold_for_file(True)  # main(), before the window shows
        self.controller.start()
        self.catalog.hub_ready("Home")
        self.assertIsNone(self.controller._flow._autotune)
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.catalog.links, [])
        self.assertEqual(self.drawer.named("open_drawer"), [])

    def test_a_last_channel_already_on_its_way_is_dropped(self):
        self.config.last_live_channel = dict(LAST)
        self.controller.start()
        self.catalog.hub_ready("Home")  # tuned once the live section loads
        self.controller.hold_for_file(True)  # a hand-over meanwhile
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.catalog.links, [])
        self.assertEqual(self.drawer.named("open_drawer"), [])

    def test_another_providers_last_channel_is_not_waited_for(self):
        self.catalog.names = ["Home", "Harbor IPTV"]
        self.config.last_live_channel = dict(LAST, provider_name="Harbor IPTV", group_id="41")
        hub = self.catalog.catalogs
        hub.own["Harbor IPTV"] = FakeOwnCatalog([HARBOR_ONE])
        self.controller.hold_for_file(True)
        self.controller.start()
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(hub.ensured, [])  # its provider is not even set up for it
        hub.ready.add("Harbor IPTV")
        hub.own["Harbor IPTV"].loaded = True
        hub.contentChanged.emit("Harbor IPTV")
        self.assertEqual(self.catalog.links, [])
        self.assertEqual(self.drawer.named("open_drawer"), [])

    def test_a_wait_for_another_providers_channel_ends_with_the_hold(self):
        self.catalog.names = ["Home", "Harbor IPTV"]
        self.config.last_live_channel = dict(LAST, provider_name="Harbor IPTV", group_id="41")
        hub = self.catalog.catalogs
        hub.own["Harbor IPTV"] = FakeOwnCatalog([HARBOR_ONE])
        self.controller.start()
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(hub.ensured, ["Harbor IPTV"])  # waited for
        self.controller.hold_for_file(True)
        self.assertFalse(self.controller._flow._foreign_timer.isActive())
        hub.ready.add("Harbor IPTV")
        hub.own["Harbor IPTV"].loaded = True
        hub.contentChanged.emit("Harbor IPTV")
        self.assertEqual(self.catalog.links, [])

    def test_welcome_waits_for_the_film(self):
        self.catalog.names = []
        self.controller.hold_for_file(True)
        self.controller.start()
        self.assertFalse(self.welcome.visible)
        self.assertEqual(self.welcome.named("show_card"), [])
        self.controller.hold_for_file(False)
        self.assertTrue(self.welcome.visible)

    def test_a_card_already_up_is_withdrawn_until_the_film_ends(self):
        self.catalog.names = []
        self.controller.start()
        self.assertTrue(self.welcome.visible)
        self.film()
        self.assertFalse(self.welcome.visible)
        self.session.stateChanged.emit("ended")
        self.session.ended.emit()
        self.assertTrue(self.welcome.visible)

    def test_the_list_key_shows_the_card_over_the_film(self):
        # Asked for: the list's key (or button) with no provider shows the card at once.
        self.catalog.names = []
        self.controller.start()
        self.film()
        self.controller.handle(Action.OPEN_DRAWER)
        self.assertTrue(self.welcome.visible)

    def test_when_idle_runs_after_release(self):
        self.controller.hold_for_file(True)
        self.when_idle()
        self.assertEqual(self.runs, [])
        self.controller.hold_for_file(False)
        self.assertEqual(self.runs, ["update question"])
        self.controller.hold_for_file(False)
        self.controller.hold_for_file(True)
        self.controller.hold_for_file(False)
        self.assertEqual(self.runs, ["update question"])  # once
        self.when_idle("now")
        self.assertEqual(self.runs, ["update question", "now"])  # not held: at once

    def test_film_end_releases(self):
        self.catalog.names = []
        self.controller.start()
        self.film()
        self.when_idle()
        self.assertEqual(self.runs, [])
        self.session.stateChanged.emit("ended")
        self.session.ended.emit()
        self.assertEqual(self.runs, ["update question"])
        self.assertTrue(self.welcome.visible)

    def test_failures_release(self):
        # A local target whose player fails (the missing file at launch: WindowHoldTests).
        self.catalog.names = []
        self.controller.start()
        self.film()
        self.when_idle()
        self.assertEqual(self.runs, [])
        self.session.stateChanged.emit("error")
        self.session.errorOccurred.emit("The stream could not be played")
        self.assertEqual(self.runs, ["update question"])
        self.assertTrue(self.welcome.visible)

    def test_an_open_that_raises_releases(self):
        self.catalog.names = []
        self.controller.start()
        self.controller.hold_for_file(True)
        self.when_idle()
        self.session.play = lambda url, **kwargs: (_ for _ in ()).throw(OSError("no input"))
        with self.assertLogs("ui.tv_controller", "WARNING"):
            self.controller.play_file(FILM, size=1234)
        self.assertEqual(self.runs, ["update question"])
        self.assertTrue(self.welcome.visible)

    def test_stop_releases(self):
        self.catalog.names = []
        self.controller.start()
        self.film()
        self.when_idle()
        self.assertEqual(self.runs, [])
        self.controller.stop()
        self.assertEqual(self.runs, ["update question"])
        self.assertTrue(self.welcome.visible)

    def test_a_stop_before_the_file_opens_keeps_the_hold(self):
        # Its check runs on a thread: the Library opened meanwhile stops nothing of it.
        self.catalog.names = []
        self.controller.start()
        self.controller.hold_for_file(True)
        self.when_idle()
        self.controller.stop_for_library()
        self.assertEqual(self.runs, [])
        self.assertFalse(self.welcome.visible)

    def test_a_channel_replacing_the_film_releases_and_another_file_keeps_it(self):
        self.controller.start()
        self.film()
        self.when_idle()
        self.film("/videos/Dizi Bölüm 2.mkv")
        self.assertEqual(self.runs, [])
        self.tune(0)
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Not found")
        self.assertEqual(self.runs, [])  # the film plays on: its hold too
        self.tune(1)
        self.ready("http://stream/2")
        self.assertEqual(self.runs, ["update question"])

    def test_after_the_release_nothing_tunes_by_itself(self):
        # A file that turned out missing at launch released the hold before the provider
        # was ready: the start was the file's, and the last channel stays untuned.
        self.config.last_live_channel = dict(LAST)
        self.controller.hold_for_file(True)
        self.controller.start()
        self.controller.hold_for_file(False)
        self.assertEqual(self.drawer.named("open_drawer"), [])
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.catalog.links, [])

    def test_a_provider_failure_while_held_opens_no_drawer(self):
        self.controller.hold_for_file(True)
        self.controller.start()
        self.catalog.hub_failed("Home", "Handshake failed")
        self.assertIn(("Home: Handshake failed", "error"), self.toast.messages())
        self.assertEqual(self.drawer.calls, [])


class WindowHoldTests(base.WindowCase):
    """A launch with a file that is gone, over no provider: the toast names it, then the
    welcome card and what waited for the film come."""

    def provider_list(self):
        return []

    def test_a_missing_file_at_launch_releases(self):
        window, runs = self.window, []
        window.controller.hold_for_file(True)  # main(), before the window shows
        window.when_idle(lambda: runs.append(True))
        card = window.overlays.welcome
        self.show()
        self.assertFalse(card.isVisible())
        self.assertEqual(runs, [])
        gone = Path(self.config_dir) / "Dizi Bölüm 9.mkv"
        window.open_paths([str(gone)], source="launch")
        text = "Couldn't open “Dizi Bölüm 9.mkv”"
        self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
        self.assertTrue(wait_until(card.isVisible))
        self.assertEqual(runs, [True])


if __name__ == "__main__":
    unittest.main()
