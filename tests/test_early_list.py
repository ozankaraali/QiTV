"""2.13.1: the TV page's list loads while the window is built behind the splash.

TVWindow begins the start's provider setup as it is built (main() asks, unless the start opens
files or shows no splash): on a fair day the list is there as the window opens, with no
"Loading <provider>…" over it; one still loading says so as before. What that setup says before
the window's start waits for it (the last channel and the list need the window on screen), and
one that failed is said once at the start, never tried again (a portal counts each sign-in).
Fakes for the controller's catalog; a real window over the fixture's provider.
"""

import logging
import os
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_tv_controller import CHANNELS, ControllerTestCase
from timer_fixtures import elapse, wait_until

from services.catalog import WorkerFactory
from services.workers import ProviderSetupWorker
from tests import test_tv_window as base
from ui.tv_window import TVWindow

LAST = {"provider_name": "Home", "group_id": "10", "item": CHANNELS[2]}


class EarlyStartTests(ControllerTestCase):
    """The flow over a setup the window began as it was built."""

    def early(self, *, at_hand: bool) -> None:
        # The page's view: whether its list shows at once (BrowsedCatalog.at_hand).
        self.catalog.at_hand = lambda section: at_hand and section == "live"
        self.controller.wire()  # as the window wires it, before it shows
        self.controller.set_up_early()

    def hints(self) -> list:
        return [text for (text,) in self.toast.named("set_hint")]

    def test_a_list_ready_before_the_start_opens_with_no_loading_hint(self):
        self.early(at_hand=True)
        self.catalog.hub_ready("Home")  # heard before the window's start
        self.assertEqual(self.catalog.requests, [])  # nothing moves before it
        self.controller.start()
        self.assertEqual(self.catalog.selected, [None])  # the start asks for it, as ever
        self.catalog.hub_ready("Home")  # and the catalog answers at once (it is ready)
        self.catalog.sectionReady.emit("live")
        self.assertNotIn("Loading Home…", self.hints())
        self.assertIsNone(self.toast.hint)
        self.assertEqual(self.drawer.named("open_drawer"), [(None,)])  # once

    def test_the_last_channel_is_tuned_once_after_the_start(self):
        self.config.last_live_channel = dict(LAST)
        self.early(at_hand=True)
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")  # before the window's start: nothing tunes
        self.assertEqual(self.requested(), [])
        self.controller.start()
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.requested(), ["Three"])
        self.assertEqual(self.drawer.named("open_drawer"), [])

    def test_a_list_still_loading_at_the_start_says_so_as_before(self):
        self.early(at_hand=False)
        self.controller.start()
        self.assertEqual(self.toast.hint, "Loading Home…")
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.assertIsNone(self.toast.hint)
        self.assertEqual(self.hints(), ["Loading Home…", None])

    def test_a_stale_list_says_it_loads_as_before(self):
        # Set up before the start, but its channels come over the network again.
        self.early(at_hand=False)
        self.catalog.hub_ready("Home")
        self.controller.start()
        self.assertEqual(self.toast.hint, "Loading Home…")

    def test_a_setup_that_failed_before_the_start_is_said_once_and_never_tried_again(self):
        self.early(at_hand=False)
        self.catalog.hub_failed("Home", "Timed out")
        self.assertEqual(self.toast.messages(), [])  # said at the window's start
        self.controller.start()
        self.assertEqual(self.catalog.selected, [])  # no second sign-in
        errors = [text for text, kind in self.toast.messages() if kind == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Home", errors[0])
        self.assertNotIn("Loading Home…", self.hints())
        self.assertEqual(self.drawer.named("open_drawer"), [(None, "left")])  # its retry row

    def test_without_an_early_setup_a_start_is_as_before(self):
        self.catalog.at_hand = lambda section: True  # never asked
        self.controller.start()
        self.assertEqual(self.toast.hint, "Loading Home…")


class EarlyWindowCase(base.WindowCase):
    """The window as main() builds it behind the splash: its list loads as it is built."""

    setup_kind = "real"  # "real", "gated" or "failing"

    def setUp(self):
        self.setups: list[str] = []
        self.lists: list[str] = []
        self.gate = threading.Event()  # holds a "gated" setup (a slow disk, a slow portal)
        real_setup, real_m3u = WorkerFactory.provider_setup, WorkerFactory.m3u
        test = self

        class Gated(ProviderSetupWorker):
            def run(self):
                test.gate.wait(30)
                super().run()

        class Failing(ProviderSetupWorker):
            def run(self):
                self.error.emit("Timed out")

        def provider_setup(factory, provider_manager, force_refresh):
            self.setups.append(provider_manager._pinned_provider)
            if self.setup_kind == "gated":
                return Gated(provider_manager, force_refresh)
            if self.setup_kind == "failing":
                return Failing(provider_manager, force_refresh)
            return real_setup(factory, provider_manager, force_refresh)

        def m3u(factory, url, verify_ssl):
            self.lists.append(url)
            return real_m3u(factory, url, verify_ssl)

        for name, fake in (("provider_setup", provider_setup), ("m3u", m3u)):
            patcher = mock.patch.object(WorkerFactory, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()
        self.addCleanup(self.gate.set)  # first: the window's close never waits on it

    def make_window(self) -> TVWindow:
        args = (self.app, self.config, self.provider_manager, self.image_manager, self.epg_manager)
        with (
            mock.patch("ui.tv_window.create_instance", return_value=None),
            self.assertLogs("ui.tv_window", "WARNING"),
        ):
            return TVWindow(*args, load_early=True)

    def tv(self):
        return self.window.catalogs.catalog(self.window.catalog.source())

    def setup_ended(self) -> None:
        """Until the early setup's worker is done, with no Qt event handled meanwhile: main()
        runs no event loop while it builds the window (the result waits for one)."""
        deadline = time.monotonic() + 20
        while self.tv().setup_end() is None:
            self.assertLess(time.monotonic(), deadline, "the setup never ended")
            time.sleep(0.02)

    def spy_hints(self) -> list:
        toast, hints = self.window.overlays.toast, []
        set_hint = toast.set_hint

        def spy(text, **how):
            hints.append(text)
            set_hint(text, **how)

        toast.set_hint = spy
        return hints


class EarlyWindowTests(EarlyWindowCase):
    def test_the_list_starts_loading_while_the_window_is_built(self):
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.setups, [base.PROVIDER_NAME])
        self.assertTrue(self.tv().is_loading())

    def test_a_list_ready_before_the_window_shows_opens_with_no_loading_hint(self):
        self.setup_ended()
        hints = self.spy_hints()
        with self.assertLogs("ui.tv_window", logging.INFO) as said:
            self.show()
            self.wait_for_drawer()
        self.assertFalse([h for h in hints if h and h.startswith("Loading")], hints)
        self.assertEqual(self.setups, [base.PROVIDER_NAME])  # the window's start took it over
        self.assertEqual(self.lists, [])  # its saved channels: nothing loaded again
        lines = [line for line in said.output if "channel list" in line]
        self.assertEqual(len(lines), 1, said.output)
        self.assertIn("ready", lines[0])
        self.assertIn("before the window showed", lines[0])


class StillLoadingWindowTests(EarlyWindowCase):
    setup_kind = "gated"

    def test_a_list_still_loading_when_the_window_shows_says_so_as_before(self):
        hints = self.spy_hints()
        with self.assertLogs("ui.tv_window", logging.INFO) as said:
            self.show()
            name = self.provider_manager.providers[0]["name"]
            self.assertTrue(wait_until(lambda: hints[-1:] == [f"Loading {name}…"]))
        self.assertTrue(any("still loading" in line for line in said.output), said.output)
        self.gate.set()
        self.wait_for_drawer()
        self.assertTrue(wait_until(lambda: hints[-1:] == [None]))
        self.assertEqual(hints, [f"Loading {name}…", None])
        self.assertEqual(self.setups, [base.PROVIDER_NAME])


class FailedEarlyWindowTests(EarlyWindowCase):
    setup_kind = "failing"

    def test_a_setup_that_failed_before_the_window_showed_is_said_once(self):
        self.setup_ended()
        self.show()
        errors = lambda: [text for text, kind in self.toasts if kind == "error"]  # noqa: E731
        self.assertTrue(wait_until(lambda: errors() != []))
        drawer = self.wait_for_drawer()  # on the sources, with the retry row
        self.assertTrue(drawer.isVisible())
        elapse(300)  # a second sign-in would have begun by then
        self.assertEqual(len(errors()), 1)
        self.assertEqual(self.setups, [base.PROVIDER_NAME])  # never signed in again


class NoProviderEarlyWindowTests(EarlyWindowCase):
    def provider_list(self):
        return []  # a first start

    def test_a_first_start_shows_the_welcome_card_and_sets_nothing_up(self):
        self.assertEqual(self.setups, [])
        self.show()
        self.assertTrue(wait_until(self.window.overlays.welcome.isVisible))
        self.assertEqual(self.setups, [])


if __name__ == "__main__":
    unittest.main()
