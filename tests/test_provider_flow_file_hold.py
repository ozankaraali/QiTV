"""A start with a file plays only the file (spec §3).

While a file opened in QiTV is on its way or plays, a provider that becomes ready tunes nothing
and opens no drawer, the welcome card waits, and what waits for QiTV to be idle (the startup
update question) runs once the file ended, stopped, failed, turned out missing or gave way to
another title. Fakes for the controller's catalog and player; a real window over no provider.
"""

import os
from pathlib import Path
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_tv_controller import CHANNELS, HARBOR_ONE, ControllerTestCase, FakeOwnCatalog
from timer_fixtures import elapse, wait_until

from services.open_paths import holds_the_start
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

    def test_a_providers_loading_hint_waits_for_the_film(self):
        # "Loading <provider>…" showed over a film opened at start: the provider loads behind
        # it, and its hint shows once the film is over, while it still loads.
        self.controller.hold_for_file(True)  # main(), before the window shows
        self.controller.start()
        self.assertEqual(self.catalog.selected, [None])  # it loads all the same
        self.assertEqual(self.toast.named("set_hint"), [])  # not even a moment
        self.controller.hold_for_file(False)
        self.assertEqual(self.toast.hint, "Loading Home…")
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.assertIsNone(self.toast.hint)

    def test_a_loading_hint_up_goes_for_the_film_and_never_comes_back_once_loaded(self):
        self.controller.start()
        self.assertEqual(self.toast.hint, "Loading Home…")
        self.film()  # a double-click while the provider loads
        self.assertIsNone(self.toast.hint)
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.session.stateChanged.emit("ended")
        self.session.ended.emit()
        self.assertIsNone(self.toast.hint)
        self.assertNotIn(("Loading Home…",), self.toast.named("set_hint")[1:])  # never again

    def test_a_failed_setup_still_says_so_over_the_film(self):
        self.controller.hold_for_file(True)
        self.controller.start()
        self.catalog.hub_failed("Home", "Portal down")
        self.assertEqual(self.toast.messages(), [("Home: Portal down", "error")])
        self.controller.hold_for_file(False)
        self.assertIsNone(self.toast.hint)  # nothing loads any more

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

    def test_play_again_holds_as_the_first_play_did(self):
        # Review: Play again after a Stop (the card up again) would play under the card.
        self.catalog.names = []
        self.controller.start()
        self.film()
        self.controller.stop()
        self.assertTrue(self.welcome.visible)
        self.controller.play_again()
        self.assertEqual(self.session.plays[-1][0], Path(FILM).absolute().as_uri())
        self.assertFalse(self.welcome.visible)
        self.session.stateChanged.emit("playing")
        self.session.stateChanged.emit("ended")
        self.session.ended.emit()
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

    def test_a_file_still_looked_at_gives_way_only_to_a_choice_made_since(self):
        # Review M5: TVWindow drops a file whose check is still on its way (file_gives_way)
        # once something else is chosen; a link that comes for an earlier choice doesn't.
        gave: list[str] = []
        self.window.file_gives_way = lambda: gave.append("gave way")
        self.controller.start()
        self.tune(0)
        self.assertEqual(len(gave), 1)  # a channel chosen now
        self.ready()
        self.assertEqual(len(gave), 1)  # its link came: chosen before, no newer choice
        self.tune(1)
        self.tune(2)  # while 1's link is on its way: queued, chosen now all the same
        self.assertEqual(len(gave), 3)
        self.ready("http://stream/2")  # 1's link: the queued choice is asked for now
        self.assertEqual(self.requested()[-1], CHANNELS[2]["name"])
        self.assertEqual(len(gave), 3)  # asked again, chosen before
        self.ready("http://stream/3")
        self.assertEqual(len(gave), 3)
        self.controller.play_file(FILM, size=1234)  # a file is no other choice
        self.assertEqual(len(gave), 3)
        self.controller._before_new_target(None)  # a phone's Watch or an outside player
        self.assertEqual(len(gave), 4)

    def test_a_file_on_its_way_drops_the_next_cards_episode(self):
        # The Next card's countdown ending while a file is looked at would ask for a provider's
        # episode: a choice made since (it would drop the file), and a provider stream opened
        # over a double-click. The hold drops the card's offer first; the resume after a
        # failed self-update is no newer choice either.
        gave: list[str] = []
        self.window.file_gives_way = lambda: gave.append("gave way")
        with mock.patch.object(self.controller._autoplay, "drop") as drop:
            self.controller.hold_for_file(False)
            drop.assert_not_called()
            self.controller.hold_for_file(True)
            drop.assert_called_once_with()
        self.controller.hold_for_file(False)
        self.controller.start()
        self.play(0)
        gave.clear()
        self.controller.stop_live()  # a self-update that couldn't launch: it plays again
        asked = len(self.catalog.links)
        self.controller.resume_stopped()
        self.assertEqual(len(self.catalog.links), asked + 1)  # asked for again
        self.assertEqual(self.requested()[-1], CHANNELS[0]["name"])
        self.assertEqual(gave, [])

    def startup_check(self):
        """main()'s startup check, its network answering now that an update exists: the
        questions shown are recorded."""
        from ui import update_dialogs

        shown: list[tuple] = []
        mocks = []
        for patch in (
            mock.patch("services.update_checker.check_for_updates"),
            mock.patch.object(
                update_dialogs, "show_update_dialog", side_effect=lambda *a: shown.append(a)
            ),
        ):
            mocks.append(patch.start())
            self.addCleanup(patch.stop)
        update_dialogs.check_for_updates(self.config, when_idle=self.controller.when_idle)
        ui = mocks[0].call_args.args[2]  # what the service reports to
        self.assertIsNot(ui, update_dialogs.DIALOGS)
        return ui, shown

    def test_a_startup_check_that_answers_during_the_film_asks_after_it(self):
        # Review I2: only the check's start waited for the film; its question opened over a
        # film that came after it (a Finder double-click's open event comes after the window).
        self.controller.start()
        ui, shown = self.startup_check()
        self.film()  # after the check started
        ui.update_available({"version": "9.9.9"}, self.config, False)  # the network answers
        elapse(50)
        self.assertEqual(shown, [])  # nothing over the film
        self.tune(0)
        self.ready()  # a channel replaces the film: released inside the tune
        self.assertEqual(shown, [])  # never inside the release itself
        elapse(50)
        self.assertEqual(shown, [({"version": "9.9.9"}, self.config, False)])
        elapse(50)
        self.assertEqual(len(shown), 1)  # once

    def test_a_file_opened_as_the_question_comes_holds_it_again(self):
        self.controller.start()
        ui, shown = self.startup_check()
        self.film()
        ui.update_available({"version": "9.9.9"}, self.config, False)
        self.session.stateChanged.emit("ended")
        self.session.ended.emit()  # over: the question is on its way from the event loop
        self.film("/videos/Dizi Bölüm 2.mkv")  # the next episode, double-clicked at once
        elapse(50)
        self.assertEqual(shown, [])
        self.controller.stop()
        elapse(50)
        self.assertEqual(len(shown), 1)

    def test_with_nothing_held_the_startup_question_comes_at_once(self):
        self.controller.start()
        ui, shown = self.startup_check()
        ui.update_available({"version": "9.9.9"}, self.config, False)
        self.assertEqual(len(shown), 1)

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
        self.assertEqual(self.drawer.calls, [])  # nor does the list open by itself

    def test_a_start_with_a_file_qitv_wont_play_tunes_nothing(self):
        # main() holds for it as for a film; open_paths says so and lets go
        self.config.last_live_channel = dict(LAST)
        if holds_the_start(["/x/notes.txt"]):
            self.controller.hold_for_file(True)
        self.controller.start()
        self.controller.hold_for_file(False)
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.catalog.links, [])
        self.assertEqual(self.drawer.calls, [])

    def test_after_the_release_back_to_live_tv_still_opens_the_list(self):
        # The viewer's own way back from the Library during the setup is not "by itself".
        self.controller.hold_for_file(True)
        self.controller.start()
        self.controller.hold_for_file(False)
        self.catalog.loading = True
        self.controller.resume_live()  # tuned or listed once the provider is ready
        self.catalog.loading = False
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.drawer.named("open_drawer"), [(None,)])

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

    def launch(self, *paths):
        """As main() starts: held when the start carries a film or a file QiTV won't play."""
        opened = [str(path) for path in paths]
        if holds_the_start(opened):
            self.window.controller.hold_for_file(True)
        runs = []
        self.window.when_idle(lambda: runs.append(True))  # at once unless held
        held = not runs
        self.show()
        self.window.open_paths(opened, source="launch")
        return held, runs

    def test_a_file_qitv_wont_play_at_launch_says_so_and_lets_go(self):
        notes = Path(self.config_dir) / "Notlar.txt"
        notes.write_text("x", encoding="utf-8")
        srt = Path(self.config_dir) / "Dizi Bölüm 1.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHallo\n", encoding="utf-8")
        held, runs = self.launch(notes, srt)
        self.assertTrue(held)  # nothing opened over the message, from the first moment
        text = "QiTV can't play “Notlar.txt”"
        self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
        self.assertTrue(wait_until(self.window.overlays.welcome.isVisible))
        self.assertEqual(runs, [True])

    def test_subtitles_or_a_playlist_alone_at_launch_leave_no_hold(self):
        srt = Path(self.config_dir) / "Dizi Bölüm 1.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHallo\n", encoding="utf-8")
        held, runs = self.launch(srt)
        self.assertFalse(held)
        self.assertEqual(runs, [True])
        playlist = Path(self.config_dir) / "Kanallar.m3u"
        playlist.write_text("#EXTM3U\n", encoding="utf-8")
        with mock.patch.object(self.window, "add_dropped_providers") as added:
            self.assertFalse(holds_the_start([str(playlist)]))
            self.window.open_paths([str(playlist)], source="launch")
            self.assertTrue(wait_until(lambda: added.called))
        after = []
        self.window.when_idle(lambda: after.append(True))
        self.assertEqual(after, [True])

    def test_a_hold_taken_before_subtitles_alone_is_let_go(self):
        srt = Path(self.config_dir) / "Dizi Bölüm 1.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHallo\n", encoding="utf-8")
        self.window.controller.hold_for_file(True)
        runs = []
        self.window.when_idle(lambda: runs.append(True))
        self.show()
        self.window.open_paths([str(srt)], source="launch")
        self.assertEqual(runs, [True])

    def test_a_check_that_raises_still_lets_go(self):
        # Review: an unexpected error on the check's thread said nothing, and the hold stayed.
        window, runs = self.window, []
        self.show()
        film = Path(self.config_dir) / "Dizi Bölüm 1.mkv"
        film.write_bytes(b"film")
        card = window.overlays.welcome
        self.assertTrue(wait_until(card.isVisible))
        with (
            mock.patch("ui.tv_window._readable_size", side_effect=RuntimeError("odd disk")),
            self.assertLogs("ui.tv_window", "WARNING") as logs,
        ):
            window.open_paths([str(film)], source="handover")
            self.assertFalse(card.isVisible())
            window.when_idle(lambda: runs.append(True))
            text = "Couldn't open “Dizi Bölüm 1.mkv”"
            self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
        self.assertIn(
            "WARNING:ui.tv_window:A file opened in QiTV could not be looked at (RuntimeError)",
            logs.output,
        )
        self.assertTrue(card.isVisible())
        self.assertEqual(runs, [True])


if __name__ == "__main__":
    unittest.main()
