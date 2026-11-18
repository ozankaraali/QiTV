"""Local files resume where they stopped (File → Open File…), and an episode's folder names
the next one (spec §8): listed on a thread as it opens, offered by the Next card and the
transport's Next, played as a file. Fictional files in a temporary folder."""

from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication
from test_tv_controller import ControllerTestCase
from timer_fixtures import wait_until

from services.local_episodes import list_folder, next_in_folder
from services.next_episode import LOCAL_NEXT
from services.watch_state import WatchState, local_key
from ui.keymap import Action

FINISHED = ("Finished", "info")


class LocalResumeTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.store = SimpleNamespace(playback_positions={}, save_config=lambda retry=True: None)
        self.store.save_playback_position = lambda cid, pos, dur: (
            self.store.playback_positions.__setitem__(
                cid, {"position": pos, "duration": dur, "timestamp": "2026-11-17T10:00:00"}
            )
        )
        self.controller.start()
        self.controller.watch = WatchState(self.store)

    def test_play_file_resumes_from_saved_point(self):
        key = local_key("ep.mkv", 1234)
        self.store.playback_positions[key] = {"position": 600_000, "duration": 2_400_000}
        self.controller.play_file("/videos/ep.mkv", size=1234)
        url, kwargs = self.session.plays[-1]
        self.assertEqual(url, Path("/videos/ep.mkv").absolute().as_uri())
        self.assertEqual(kwargs["start_ms"], 600_000)
        self.session.positionChanged.emit(700_000, 2_400_000)
        self.assertEqual(self.store.playback_positions[key]["position"], 700_000)

    def test_play_file_without_size_keeps_no_point(self):
        self.controller.play_file("/videos/ep.mkv")
        self.assertEqual(self.session.plays[-1][1]["start_ms"], 0)
        self.session.positionChanged.emit(700_000, 2_400_000)
        self.assertEqual(self.store.playback_positions, {})

    def test_a_short_song_keeps_no_point(self):
        self.controller.play_file("/music/song.mp3", size=99)
        self.session.positionChanged.emit(60_000, 180_000)
        self.assertEqual(self.store.playback_positions, {})


class OpeningMarkTests(ControllerTestCase):
    """Where a file's opening ends for QiTV's mark over it (TVWindow.opening_over): never as
    the file itself is played, at once on a stop, an error, its end or another title."""

    def setUp(self):
        super().setUp()
        self.controller.start()
        self.over = mock.Mock()
        self.window.opening_over = self.over

    def test_playing_a_file_leaves_the_mark(self):
        self.controller.play_file("/videos/ep.mkv", size=1234)
        self.controller.play_file("/videos/next.mkv", size=99)  # a file over a file
        self.over.assert_not_called()

    def test_a_stop_an_error_or_an_end_ends_it(self):
        for end in (
            self.controller.stop,
            lambda: self.session.errorOccurred.emit("The stream could not be opened"),
            self.session.ended.emit,
        ):
            self.controller.play_file("/videos/ep.mkv", size=1234)
            self.over.reset_mock()
            end()
            self.over.assert_called()


class FolderCase(ControllerTestCase):
    """An episode's folder in a temporary directory; the hold's release is heard (when_idle)."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.runs: list[str] = []  # the when_idle callbacks that ran

    def episode(self, name, data=b"episode"):
        path = self.folder / name
        path.write_bytes(data)
        return path

    def open(self, path):
        """A file opened in QiTV plays (TVWindow.open_paths holds first)."""
        self.controller.hold_for_file(True)
        self.controller.play_file(str(path), len(path.read_bytes()))
        self.session.stateChanged.emit("playing")

    def end(self):
        self.session.stateChanged.emit("ended")
        self.session.ended.emit()

    def when_idle(self):
        self.controller.when_idle(lambda: self.runs.append("update question"))

    def context(self):
        return self.controller.now_playing().context or {}

    def next_states(self):
        return [available for (available,) in self.transport.named("set_next_available")]


class FolderEpisodeTests(FolderCase):
    """The listing's thread is the test's: FolderListing.start is captured, and the test hands
    the controller what list_folder finds."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(self.controller._listing, "start")
        self.listings = patch.start()
        self.addCleanup(patch.stop)

    def list_now(self):
        """The newest listing answers, as its thread would."""
        generation, folder = self.listings.call_args.args
        self.controller._on_folder_listed(generation, list_folder(folder))

    def test_an_episode_lists_its_folder_when_it_opens(self):
        self.controller.start()
        self.open(self.episode("Show.S01E01.mkv"))
        self.assertEqual(self.listings.call_count, 1)
        self.assertEqual(self.listings.call_args.args[1], str(self.folder))
        self.listings.reset_mock()
        self.open(self.episode("Holiday.mkv"))
        with mock.patch.object(self.controller.watch, "mark_audio", create=True):
            self.open(self.episode("Show.S01E02.mp3"))  # a song named like one
        recording = self.episode("Show.S01E03.ts")
        self.controller.play_recording({"path": str(recording), "id": "rec-1", "name": "Show"})
        self.listings.assert_not_called()

    def test_the_listing_gives_the_next_and_the_button(self):
        self.controller.start()
        first, second = self.episode("Show.S01E01.mkv"), self.episode("Show.S01E02.mkv", b"x" * 12)
        self.episode("Show.S01E02.srt")
        self.episode("Other.S01E02.mkv")
        self.open(first)
        self.assertNotIn(True, self.next_states())
        self.list_now()
        following = self.context()[LOCAL_NEXT]
        self.assertEqual(following["local_path"], str(second))
        self.assertEqual(following["size"], 12)
        self.assertEqual(self.next_states()[-1], True)

    def test_a_late_listing_never_reaches_a_newer_title(self):
        self.controller.start()
        first = self.episode("Show.S01E01.mkv")
        self.episode("Show.S01E02.mkv")
        self.episode("Show.S01E03.mkv")
        self.open(first)
        late = self.listings.call_args.args
        self.open(self.episode("Holiday.mkv"))  # another file before the listing came
        holiday = self.controller.now_playing()
        self.controller._on_folder_listed(late[0], list_folder(late[1]))
        self.assertIsNone(holiday.context)
        self.open(self.folder / "Show.S01E02.mkv")  # a newer episode of the same show
        newer = self.controller.now_playing()
        self.controller._on_folder_listed(late[0], list_folder(late[1]))
        self.assertIsNone(newer.context)  # never S01E01's next
        self.assertNotIn(True, self.next_states())
        # A channel chosen while the file plays: its link is on its way, the file still current.
        self.open(first)
        again = self.controller.now_playing()
        late = self.listings.call_args.args
        self.tune(0)
        self.assertIs(self.controller.now_playing(), again)
        self.controller._on_folder_listed(late[0], list_folder(late[1]))
        self.assertIsNone(again.context)
        self.assertNotIn(True, self.next_states())

    def test_the_card_plays_the_next_file(self):
        self.controller.start()
        first, second = self.episode("Show.S01E01.mkv"), self.episode("Show.S01E02.mkv", b"x" * 12)
        self.open(first)
        self.list_now()
        self.end()
        self.assertEqual(self.next_card.named("show_next"), [("Show", "S1 · E2", "episode")])
        self.assertNotIn(FINISHED, self.toast.messages())
        with mock.patch.object(
            self.controller, "play_file", wraps=self.controller.play_file
        ) as play_file:
            self.next_card.activate()  # Play now
        play_file.assert_called_once_with(str(second), 12)
        self.assertEqual(self.session.plays[-1][0], second.absolute().as_uri())

    def test_each_file_gets_a_fresh_context(self):
        # Task 2's note: what follows keeps the old context; the file played is a new target
        # whose context is only its own listing's.
        self.controller.start()
        first = self.episode("Show.S01E01.mkv")
        self.episode("Show.S01E02.mkv")
        third = self.episode("Show.S01E03.mkv")
        self.open(first)
        self.list_now()
        before = self.controller.now_playing()
        self.end()
        self.next_card.activate()
        second = self.controller.now_playing()
        self.assertIsNot(second, before)
        self.assertIsNone(second.context)  # nothing carried over, before its own listing
        self.assertEqual(self.next_states()[-1], False)
        self.session.stateChanged.emit("playing")
        self.list_now()
        self.assertEqual(self.context()[LOCAL_NEXT]["local_path"], str(third))
        self.end()
        self.next_card.activate()
        last = self.controller.now_playing()
        self.assertIsNone(last.context)
        self.session.stateChanged.emit("playing")
        self.list_now()  # the end of the folder: nothing follows
        self.assertNotIn(LOCAL_NEXT, self.context())
        self.assertEqual(self.next_states()[-1], False)
        self.end()
        self.assertEqual(len(self.next_card.named("show_next")), 2)  # no card for the last
        self.assertIn(FINISHED, self.toast.messages())

    def test_the_hold_waits_for_the_card(self):
        self.catalog.names = []  # no provider: the hold keeps the welcome card off the film
        self.controller.start()
        first, second = self.episode("Show.S01E01.mkv"), self.episode("Show.S01E02.mkv")
        self.open(first)
        self.list_now()
        self.when_idle()
        self.end()
        self.assertTrue(self.next_card.visible)
        self.assertEqual(self.runs, [])  # the card takes the end: the hold stays
        self.assertFalse(self.welcome.visible)
        self.controller.handle(Action.ESCAPE)  # Esc on the card
        self.assertFalse(self.next_card.visible)
        self.assertEqual(self.runs, ["update question"])
        self.assertTrue(self.welcome.visible)
        # The transport's Next after Esc plays the next file held, as any file opened in QiTV.
        self.transport.nextRequested.emit()
        self.assertEqual(self.session.plays[-1][0], second.absolute().as_uri())
        self.assertFalse(self.welcome.visible)
        self.session.stateChanged.emit("playing")
        self.list_now()  # the last one in its folder
        self.when_idle()
        self.end()  # an end the card doesn't take lets go at once, "Finished" as today
        self.assertEqual(self.runs, ["update question"] * 2)
        self.assertTrue(self.welcome.visible)
        self.assertIn(FINISHED, self.toast.messages())
        # With the setting off there is no card either.
        self.config.auto_play_episodes = False
        self.open(first)
        self.list_now()
        self.when_idle()
        self.toast.calls.clear()
        self.end()
        self.assertEqual(self.runs, ["update question"] * 3)
        self.assertEqual(len(self.next_card.named("show_next")), 1)
        self.assertIn(FINISHED, self.toast.messages())

    def card_over_the_end(self):
        """An episode with a next one in its folder ended: the card offers it, and the file's
        hold waits for the card (the update question too)."""
        first = self.episode("Show.S01E01.mkv")
        self.episode("Show.S01E02.mkv")
        self.open(first)
        self.list_now()
        self.when_idle()
        self.end()
        self.assertTrue(self.next_card.visible)
        self.assertEqual(self.runs, [])

    def test_a_channel_chosen_over_the_card_lets_the_hold_go(self):
        # The card dropped for a channel whose link then fails: the ended file keeps no hold.
        self.controller.start()
        self.card_over_the_end()
        self.tune(0)
        self.assertFalse(self.next_card.visible)
        self.assertEqual(self.runs, ["update question"])
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Not found")
        self.assertEqual(self.runs, ["update question"])

    def test_the_list_asked_for_with_no_provider_over_the_card_lets_the_hold_go(self):
        # Review I1: the welcome card asked for drops the Next card (ProviderFlow.welcome),
        # never as an Esc: the hold kept for the card went with it all the same.
        self.catalog.names = []
        self.controller.start()
        self.card_over_the_end()
        self.controller.handle(Action.OPEN_DRAWER)
        self.assertTrue(self.welcome.visible)
        self.assertFalse(self.next_card.visible)
        self.assertEqual(self.runs, ["update question"])
        self.when_idle()  # nothing held any more: at once
        self.assertEqual(self.runs, ["update question"] * 2)

    def test_a_provider_chosen_over_the_card_lets_the_hold_go(self):
        # Review I1: a setup the viewer asks for drops the Next card (its "Loading" hint).
        self.controller.start()
        self.card_over_the_end()
        self.controller.select_provider("Backup")
        self.assertFalse(self.next_card.visible)
        self.assertEqual(self.runs, ["update question"])
        self.drawer.calls.clear()
        # Not held: a setup that fails opens the list on its retry row (a held film's waits).
        self.catalog.hub_failed("Backup", "Portal down")
        self.assertEqual(self.drawer.named("open_drawer"), [(None, "left")])

    def test_a_card_offered_under_a_hidden_host_still_counts(self):
        # The card offered while its host doesn't show (isVisible False): a channel chosen
        # then lets go of the file's hold as over a card that shows.
        self.controller.start()
        self.card_over_the_end()
        self.next_card.visible = False  # offered, not on screen
        self.tune(0)
        self.assertEqual(self.runs, ["update question"])

    def test_a_next_file_gone_fails_like_any_open(self):
        # Review focus 3: deleted or moved after the listing. libVLC can't open it and says so
        # (the fake session is told, as PlaybackSession reports it).
        self.controller.start()
        for card in (False, True):  # the transport's Next, then the card's Play now
            with self.subTest(card=card):
                self.runs.clear()
                first = self.episode("Show.S01E01.mkv")
                second = self.episode("Show.S01E02.mkv")
                self.open(first)
                self.list_now()
                self.when_idle()
                if card:
                    self.end()
                second.unlink()
                with self.assertNoLogs("ui.tv_controller", "WARNING"):
                    if card:
                        self.next_card.activate()
                    else:
                        self.transport.nextRequested.emit()
                    self.assertEqual(self.session.plays[-1][0], second.absolute().as_uri())
                    self.assertEqual(self.runs, [])  # held as it opens
                    self.session.stateChanged.emit("error")
                    self.session.errorOccurred.emit("The stream could not be opened")
                self.assertEqual(self.runs, ["update question"])
                errors = [text for text, kind in self.toast.messages() if kind == "error"]
                self.assertTrue(errors and "Show.S01E02.mkv" in errors[-1], errors)


class ListingThreadTests(FolderCase):
    """The listing on its own thread; a folder on a slow or unreachable share (review focus 1)."""

    def test_a_listing_that_fails_leaves_no_next(self):
        self.controller.start()
        gate = threading.Event()

        def slow(folder):
            gate.wait(10)
            return []  # the share could not be read

        first = self.episode("Show.S01E01.mkv")
        self.episode("Show.S01E02.mkv")
        with mock.patch("ui.episode_listing.list_folder", side_effect=slow) as listed:
            self.open(first)
            self.assertEqual(self.session.plays[-1][0], first.absolute().as_uri())  # not held
            threads = [t for t in threading.enumerate() if t.name == "qitv-episodes"]
            self.assertEqual(len(threads), 1)
            self.assertTrue(threads[0].daemon)
            gate.set()
            threads[0].join(10)
            self.assertFalse(threads[0].is_alive())
        listed.assert_called_once_with(str(self.folder))
        QCoreApplication.processEvents()  # its answer, queued to the GUI thread
        self.assertNotIn(LOCAL_NEXT, self.context())
        self.assertNotIn(True, self.next_states())
        self.end()
        self.assertEqual(self.next_card.named("show_next"), [])
        self.assertIn(FINISHED, self.toast.messages())

    def test_the_listing_comes_back_on_the_gui_thread(self):
        self.controller.start()
        first, second = self.episode("Show.S01E01.mkv"), self.episode("Show.S01E02.mkv")
        threads: list[int] = []  # where the listing is read

        def picked(current_path, listing):
            threads.append(threading.get_ident())
            return next_in_folder(current_path, listing)

        with mock.patch("ui.tv_controller.next_in_folder", side_effect=picked):
            self.open(first)
            self.assertTrue(wait_until(lambda: LOCAL_NEXT in self.context()))
        self.assertEqual(threads, [threading.get_ident()])  # the GUI thread's
        self.assertEqual(self.context()[LOCAL_NEXT]["local_path"], str(second))
        self.assertEqual(self.next_states()[-1], True)


if __name__ == "__main__":
    unittest.main()
