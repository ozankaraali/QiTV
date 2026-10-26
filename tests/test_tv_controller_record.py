"""2.6, Record as a PVR: a recording keeps its player when its channel is left."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QWidget
from test_multiview import MIB, MultiviewCase
from test_tv_controller import (
    BACKUP_TWO,
    CHANNELS,
    FakeLiveSession,
    FakeSession,
    play_context,
)
from test_tv_pip import MOVIE, PipCase
from timer_fixtures import wait_until

from services.sessions import Slot
from ui.keymap import Action
from ui.live_tuner import LiveTuner
from ui.overlays.question_card import QuestionCard
from ui.overlays.rec_sign import RecSign
from ui.overlays.stopped_notice import StoppedNotice

QUESTION = "No stream is free: One is recording. Stop the recording?"


class _RecordCase(PipCase):
    def setUp(self):
        super().setUp()

        def make_slot(name):
            session = FakeSession()
            live = FakeLiveSession(session)
            tuner = LiveTuner(session, self.config, live, self.budget, purpose=name)
            slot = Slot(name, session, live, tuner=tuner)
            self.sessions.add_slot(slot)
            self.controller.slot_added(slot)
            self.addCleanup(live.released.emit)
            return slot

        self.sessions.set_slot_factory(make_slot)

    def record_large(self, index=0):
        self.start_large(index)
        recording = self.controller.live
        self.assertTrue(recording.start_recording("/tmp/x.ts"))
        return recording

    def leave_for(self, index):
        """Tune channel `index` while the large picture records: another player opens it."""
        self.play(index)
        stand_in = self.controller.live
        self.assertTrue(wait_until(lambda: len(stand_in.named("start")) == 1))
        stand_in.play_pipeline()
        return stand_in

    def since_recording(self, live):
        return live.calls[live.calls.index(("start_recording", "/tmp/x.ts")) :]


class RecordingAsideTests(_RecordCase):
    def test_a_zap_away_from_a_recording_keeps_it(self):
        recording = self.record_large(0)
        self.leave_for(1)
        self.assertNotIn(("stop",), self.since_recording(recording))
        self.assertIn(("set_aside",), recording.calls)
        self.assertEqual([s.name for s in self.sessions.background()], ["main"])
        self.assertEqual(self.sessions.large().name, "b1")
        self.assertIn(("set_roles", "b1", "pip"), self.frame.calls)
        self.assertEqual(self.controller.live.named("start")[-1][0], "http://stream/2")
        self.assertEqual(sorted(self.budget.holders("Home")), ["b1", "main"])

    def test_tuning_the_recording_channel_brings_it_back(self):
        recording = self.record_large(0)
        stand_in = self.leave_for(1)
        self.controller.tune(CHANNELS[0], group_key="10", channels=CHANNELS)
        self.assertIs(self.controller.live, recording)
        self.assertEqual(recording.calls[-1], ("bring_back",))
        self.assertEqual(self.sessions.background(), [])
        self.assertIn(("stop",), stand_in.calls)  # the channel it showed is left
        self.assertEqual(self.controller._channel, CHANNELS[0])

    def test_going_back_keeps_the_other_recording(self):  # Stage C review C1
        self.record_large(0)
        stand_in = self.leave_for(1)
        self.assertTrue(stand_in.start_recording("/tmp/y.ts"))
        self.controller.tune(CHANNELS[0], group_key="10", channels=CHANNELS)
        self.assertEqual(self.sessions.large().name, "main")
        self.assertTrue(stand_in.recording())
        self.assertNotIn(
            ("stop",), stand_in.calls[stand_in.calls.index(("start_recording", "/tmp/y.ts")) :]
        )
        self.assertEqual([s.name for s in self.sessions.background()], ["b1"])
        self.assertEqual(stand_in.calls[-1], ("set_aside",))
        self.controller.tune(CHANNELS[1], group_key="10", channels=CHANNELS)  # and back again
        self.assertEqual(self.sessions.large().name, "b1")
        self.assertEqual([s.name for s in self.sessions.background()], ["main"])

    def test_the_signs_go_back_keeps_the_other_recording(self):  # Stage C review C1
        self.record_large(0)
        stand_in = self.leave_for(1)
        self.assertTrue(stand_in.start_recording("/tmp/y.ts"))
        self.controller.go_back_to_recording("main")
        self.assertTrue(stand_in.recording())
        self.assertEqual([s.name for s in self.sessions.background()], ["b1"])

    def test_an_unknown_player_is_ignored(self):  # Stage C review M5: a stale phone's
        self.controller.go_back_to_recording("b9")
        self.controller.stop_background_recording("b9")

    def test_a_film_while_recording_keeps_it(self):
        recording = self.record_large(0)
        self.controller.play_vod(MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.assertNotIn(("stop",), self.since_recording(recording))
        self.assertEqual([s.name for s in self.sessions.background()], ["main"])
        stand_in = self.sessions.large()
        self.assertEqual(stand_in.name, "b1")
        self.assertEqual([url for url, _ in stand_in.playback.plays], ["http://vod/heat.mkv"])

    def test_the_library_keeps_a_recording(self):
        recording = self.record_large(0)
        self.controller.stop_for_library()
        self.assertNotIn(("stop",), self.since_recording(recording))
        self.assertEqual([s.name for s in self.sessions.background()], ["main"])

    def test_closing_a_recording_pip_keeps_it(self):
        self.start_large(0)
        small = self.open_small(CHANNELS[1])
        self.assertTrue(small.live.start_recording("/tmp/x.ts"))
        self.controller._pip.close()
        self.assertFalse(self.sessions.pip_open())
        self.assertNotIn(("stop",), self.since_recording(small.live))
        self.assertEqual([s.name for s in self.sessions.background()], ["pip"])
        self.assertEqual(small.live.calls[-1], ("set_aside",))
        self.assertIn(("set_roles", "main", "b1"), self.frame.calls)

    def test_the_recording_channel_opens_small_without_a_stream(self):
        recording = self.record_large(0)
        self.leave_for(1)
        links = len(self.catalog.links)
        self.controller.open_pip(CHANNELS[0], group_key="10", channels=CHANNELS)
        self.assertEqual(len(self.catalog.links), links)
        self.assertEqual(self.sessions.small().name, "main")
        self.assertEqual(recording.calls[-1], ("bring_back",))
        self.assertIn(("set_roles", "b1", "main"), self.frame.calls)
        self.assertEqual(self.sessions.background(), [])

    def test_a_stop_for_an_update_ends_the_recordings_before_their_players_stop(self):
        # 2.8 (review of 2.6): a self-update closed a recording's buffer; ended first (saved),
        # its player parks as any other and its disk buffer is kept, as Quit's question does.
        recording = self.record_large(0)
        self.leave_for(1)
        seen = []
        stop_background = self.sessions.stop_background

        def stopping():
            seen.append(recording.recording())
            stop_background()

        self.sessions.stop_background = stopping
        self.controller.stop_live()
        self.assertEqual(seen, [False])
        self.assertIn(("stop_recording",), self.since_recording(recording))

    def test_quitting_stops_the_recordings_in_the_background(self):
        recording = self.record_large(0)
        self.leave_for(1)
        self.controller.stop_live()
        self.assertIn(("stop",), self.since_recording(recording))
        self.assertEqual(self.sessions.background(), [])


class StreamQuestionTests(_RecordCase):
    """Task 12: with no stream free at a recording's provider, QiTV asks before it stops it."""

    def overlays(self, column):
        overlays = super().overlays(column)
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.question = QuestionCard(self.host)
        overlays.question = self.question
        return overlays

    def one_stream(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 1}

    def test_no_free_stream_asks_and_tunes_nothing(self):
        self.one_stream()
        recording = self.record_large(0)
        links = len(self.catalog.links)
        self.tune(1)
        self.assertTrue(self.question.isVisibleTo(self.host))
        self.assertEqual(self.question.question(), QUESTION)
        self.assertEqual(len(self.catalog.links), links)
        self.assertNotIn(("stop",), self.since_recording(recording))
        self.assertEqual(self.sessions.large().name, "main")

    def test_stop_recording_stops_it_and_tunes(self):
        self.one_stream()
        recording = self.record_large(0)
        self.tune(1)
        self.controller.handle(Action.SEEK_FWD)  # to Stop recording
        self.controller.handle(Action.OK)
        self.assertIn(("stop_recording",), recording.calls)
        self.assertEqual(self.requested()[-1], "Two")
        self.ready("http://stream/2")
        self.assertEqual(recording.calls[-1], ("stop",))  # a zap, as before 2.6
        recording.released.emit()
        self.assertTrue(wait_until(lambda: recording.named("start")[-1][0] == "http://stream/2"))
        self.assertEqual(self.sessions.background(), [])

    def test_keep_recording_leaves_everything(self):
        self.one_stream()
        recording = self.record_large(0)
        links = len(self.catalog.links)
        self.tune(1)
        self.controller.handle(Action.OK)  # Keep recording, focused first
        self.assertEqual(len(self.catalog.links), links)
        self.assertTrue(recording.recording())
        self.assertEqual(self.controller._channel, CHANNELS[0])

    def test_another_providers_channel_asks_nothing(self):
        self.one_stream()
        self.record_large(0)
        self.controller.tune(BACKUP_TWO, group_key="10", channels=[BACKUP_TWO])
        self.assertTrue(self.question.isHidden())
        self.assertEqual(self.requested()[-1], "Two")

    def test_a_film_asks_too(self):
        self.one_stream()
        self.record_large(0)
        links = len(self.catalog.links)
        self.controller.play_vod(MOVIE, "movie", play_context())
        self.assertTrue(self.question.isVisibleTo(self.host))
        self.assertEqual(len(self.catalog.links), links)

    def test_an_outside_player_asks_too(self):
        self.one_stream()
        recording = self.record_large(0)
        self.leave_for_backup()
        launched = []
        self.assertTrue(
            self.controller.play_elsewhere(
                lambda target, url: launched.append(url) or (True, ""), self.target(1)
            )
        )
        self.ready("http://stream/2")
        self.assertTrue(self.question.isVisibleTo(self.host))
        self.assertEqual(launched, [])
        self.controller.handle(Action.SEEK_FWD)
        self.controller.handle(Action.OK)
        self.assertIn(("stop_recording",), recording.calls)
        recording.released.emit()  # the background player let its stream go
        self.assertTrue(wait_until(lambda: launched == ["http://stream/2"]))

    def test_the_phones_watch_asks_on_the_phone(self):
        self.one_stream()
        recording = self.record_large(0)
        self.leave_for_backup()
        asked, settled, launched = [], [], []
        self.controller.phoneAsked.connect(lambda *a: asked.append(a))
        self.controller.phoneSettled.connect(settled.append)

        def launch(target, url):
            launched.append(url)
            return True, ""

        self.controller.play_elsewhere(launch, self.target(1), ask_on="s8")
        self.ready("http://stream/2")
        self.assertEqual(asked, [("s8", QUESTION, "Stop recording", "Keep recording")])
        self.assertTrue(self.question.isHidden())  # the phone asked: the TV shows nothing
        self.controller.answer_phone(True)
        self.assertEqual(settled, ["s8"])
        self.assertFalse(recording.recording())
        recording.released.emit()
        self.assertTrue(wait_until(lambda: launched == ["http://stream/2"]))

    def test_a_phones_question_takes_the_tvs_card_down(self):
        """The phone's Watch asks while the TV's card asks: the phone's question stands for
        it, the card goes, and the TV's keys answer nothing (not the phone's question)."""
        self.one_stream()
        self.record_large(0)
        self.leave_for_backup()
        launch = lambda target, url: (True, "")  # noqa: E731
        self.controller.play_elsewhere(launch, self.target(1))  # an outside player's ask
        self.ready("http://stream/2")
        self.assertTrue(self.question.isVisibleTo(self.host))
        asked = []
        self.controller.phoneAsked.connect(lambda *a: asked.append(a))
        self.controller.play_elsewhere(launch, self.target(2), ask_on="s8")
        self.ready("http://stream/3")
        self.assertEqual(len(asked), 1)
        self.assertTrue(self.question.isHidden())
        self.assertFalse(self.controller.answer_key(Action.OK))
        self.assertTrue(self.controller._question.asking())  # the phone's, still

    def test_a_phone_asked_again_is_not_told_its_question_went(self):
        """A phone's newer question replaces its own sheet: only another's says it went."""
        self.one_stream()
        self.record_large(0)
        self.leave_for_backup()
        settled = []
        self.controller.phoneSettled.connect(settled.append)
        launch = lambda target, url: (True, "")  # noqa: E731
        for index, sid in ((1, "s8"), (2, "s8"), (1, "s9")):
            self.controller.play_elsewhere(launch, self.target(index), ask_on=sid)
            self.ready(f"http://stream/{index + 1}")
        self.assertEqual(settled, ["s8"])  # when s9's came

    def test_a_page_change_takes_the_question_along(self):  # Stage C review I3
        self.one_stream()
        self.record_large(0)
        page = QWidget()
        page.resize(1280, 720)
        library_card = QuestionCard(page)
        self.controller.overlays.library_question = library_card
        self.tune(1)
        self.assertTrue(self.question.isVisibleTo(self.host))
        self.controller.page_changed("library")
        self.assertTrue(self.question.isHidden())
        self.assertTrue(library_card.isVisibleTo(page))
        self.assertEqual(library_card.question(), QUESTION)
        self.controller.page_changed("corner")  # the Library, with the player in a corner
        self.assertTrue(library_card.isVisibleTo(page))
        self.controller.page_changed("player")
        self.assertTrue(library_card.isHidden())
        self.assertEqual(self.question.question(), QUESTION)

    def test_from_the_library_the_librarys_card_asks(self):
        self.one_stream()
        self.record_large(0)
        page = QWidget()
        page.resize(1280, 720)
        library_card = QuestionCard(page)
        self.controller.overlays.library_question = library_card
        self.controller._flow.enter_library()
        self.controller.play_vod(MOVIE, "movie", play_context())
        self.assertTrue(library_card.isVisibleTo(page))
        self.assertTrue(self.question.isHidden())
        self.assertTrue(self.controller.answer_key(Action.NAV_RIGHT))
        self.assertEqual(library_card.focused(), "yes")
        self.assertFalse(self.controller.answer_key(Action.MUTE))

    def leave_for_backup(self):
        """The recording goes on in the background; another provider's channel plays large."""
        self.controller.tune(BACKUP_TWO, group_key="10", channels=[BACKUP_TWO])
        self.ready("http://backup/2")
        stand_in = self.controller.live
        self.assertTrue(wait_until(lambda: len(stand_in.named("start")) == 1))
        stand_in.play_pipeline()

    def target(self, index):
        from ui.link_lanes import Target

        channel = CHANNELS[index]
        return Target(channel, "live", channel["name"], group_key="10")

    def test_a_pip_asks_too(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 2}
        self.start_large(0)
        small = self.open_small(CHANNELS[1])
        self.assertTrue(small.live.start_recording("/tmp/x.ts"))
        links = len(self.catalog.links)
        self.controller.open_pip(CHANNELS[2], group_key="10", channels=CHANNELS)
        self.assertEqual(len(self.catalog.links), links)  # both streams held, one records
        self.assertTrue(self.question.isVisibleTo(self.host))
        self.assertEqual(self.question.question(), QUESTION.replace("One", "Two"))
        self.controller.handle(Action.SEEK_FWD)
        self.controller.handle(Action.OK)
        self.assertFalse(small.live.recording())
        self.assertEqual(self.requested()[-1], "Three")  # it opens over Two's stream


class RecSignAndQuitTests(_RecordCase):
    """Task 13: the REC sign of background recordings, and quitting asks first."""

    def overlays(self, column):
        overlays = super().overlays(column)
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.question = QuestionCard(self.host)
        self.sign = RecSign(self.host)
        overlays.question, overlays.rec_sign = self.question, self.sign
        return overlays

    def test_the_sign_shows_a_background_recording(self):
        self.record_large(0)
        self.assertTrue(self.sign.isHidden())  # the large picture's own chip says it
        self.leave_for(1)
        self.assertTrue(self.sign.isVisibleTo(self.host))
        self.assertTrue(self.sign.text().startswith("REC One"))

    def test_go_back_from_the_sign(self):
        recording = self.record_large(0)
        self.leave_for(1)
        self.sign.goBack.emit("main")
        self.assertIs(self.controller.live, recording)
        self.assertEqual(recording.calls[-1], ("bring_back",))
        self.assertTrue(self.sign.isHidden())

    def test_stop_from_the_sign(self):
        recording = self.record_large(0)
        self.leave_for(1)
        self.sign.stop.emit("main")
        self.assertIn(("stop_recording",), recording.calls)
        self.assertEqual(recording.calls[-1], ("stop",))  # its channel is left
        self.assertEqual(self.sessions.background(), [])
        self.assertTrue(self.sign.isHidden())

    def test_quitting_asks_while_recording(self):
        recording = self.record_large(0)
        self.leave_for(1)
        quits = []
        self.assertTrue(self.controller.asks_before_quit(lambda: quits.append(True)))
        self.assertEqual(self.question.question(), "One is recording. Quit and stop the recording?")
        self.controller.handle(Action.OK)  # Keep recording
        self.assertEqual(quits, [])
        self.assertTrue(recording.recording())
        self.controller.asks_before_quit(lambda: quits.append(True))
        self.controller.handle(Action.SEEK_FWD)
        self.controller.handle(Action.OK)
        self.assertFalse(recording.recording())
        self.assertEqual(quits, [True])

    def test_quitting_counts_several(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 3}
        self.record_large(0)
        stand_in = self.leave_for(1)
        self.assertTrue(stand_in.start_recording("/tmp/y.ts"))
        self.controller.asks_before_quit(lambda: None)
        self.assertEqual(
            self.question.question(), "2 channels are recording. Quit and stop the recordings?"
        )

    def test_quitting_without_a_recording_asks_nothing(self):
        self.start_large(0)
        self.assertFalse(self.controller.asks_before_quit(lambda: None))
        self.assertEqual(self.controller.quit_note(), "")


class RecordGuardWiringTests(_RecordCase):
    """Task 14: a provider with no known limit that closes the recording's stream as
    another opens: the other stops, and the provider allows one for the run."""

    def test_the_stand_in_stops_when_the_recording_breaks(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "M3U"}
        recording = self.record_large(0)
        stand_in = self.leave_for(1)
        recording.timelineChanged.emit({"reconnecting": True})
        self.assertEqual(stand_in.calls[-1], ("stop",))
        self.assertEqual(self.budget.cap("Home", None), 1)
        self.assertIn(
            ("Your provider allows no other stream while One records", "error"),
            self.toast.messages(),
        )
        self.assertTrue(recording.recording())


class StopTests(_RecordCase):
    """2.6: Stop (■, X, the phone's): every picture stops and nothing streams; a recording goes
    on in the background; Play plays what was stopped again."""

    def overlays(self, column):
        overlays = super().overlays(column)
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.notice = StoppedNotice(self.host)
        overlays.stopped_notice = self.notice
        return overlays

    def test_stop_stops_both_pictures_and_says_so(self):
        self.start_large(0)
        small = self.open_small(CHANNELS[1])
        self.controller.handle(Action.STOP)
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.assertEqual(small.live.calls[-1], ("stop",))
        self.assertFalse(self.sessions.pip_open())
        self.assertTrue(self.notice.isVisibleTo(self.host))
        self.assertEqual(self.notice.text(), "Stopped")

    def test_play_plays_the_channel_again(self):
        self.start_large(0)
        self.controller.handle(Action.STOP)
        self.live.released.emit()
        links = len(self.catalog.links)
        self.controller.handle(Action.PLAY_PAUSE)
        self.assertEqual(len(self.catalog.links), links + 1)
        self.assertEqual(self.requested()[-1], "One")
        self.assertTrue(self.notice.isHidden())

    def test_the_pills_button_plays_again(self):
        self.start_large(0)
        self.controller.handle(Action.STOP)
        self.notice.keepWatching.emit()
        self.assertEqual(self.requested()[-1], "One")

    def test_a_film_plays_again_too(self):
        self.controller.play_vod(MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.controller.handle(Action.STOP)
        self.controller.handle(Action.PLAY_PAUSE)
        self.assertEqual(self.requested()[-1], "Heat")

    def test_a_recording_goes_on_in_the_background(self):
        recording = self.record_large(0)
        self.controller.handle(Action.STOP)
        self.assertNotIn(("stop",), self.since_recording(recording))
        self.assertEqual([s.name for s in self.sessions.background()], ["main"])

    def test_with_only_the_small_picture_play_plays_its_channel_again(self):
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.STOP)
        self.assertFalse(self.sessions.pip_open())
        self.assertTrue(self.notice.isVisibleTo(self.host))
        links = len(self.catalog.links)
        self.controller.handle(Action.PLAY_PAUSE)
        self.assertEqual(len(self.catalog.links), links + 1)
        self.assertEqual(self.requested()[-1], "Two")
        self.assertTrue(self.notice.isHidden())

    def test_with_nothing_playing_stop_does_nothing(self):
        self.controller.handle(Action.STOP)
        self.assertTrue(self.notice.isHidden())

    def test_another_choice_takes_the_pill_away(self):
        self.start_large(0)
        self.controller.handle(Action.STOP)
        self.tune(1)
        self.assertTrue(self.notice.isHidden())


class StopTileTests(MultiviewCase):
    def test_stop_on_an_empty_grid_offers_nothing_to_play_again(self):
        host = QWidget()
        host.resize(1280, 720)
        notice = StoppedNotice(host)
        self.controller.overlays.stopped_notice = notice
        self.v()
        self.assertTrue(self.controller.multiview_shown())
        self.controller.handle(Action.STOP)
        self.assertFalse(self.controller.multiview_shown())
        self.assertTrue(notice.isHidden())


class RecordingTileTests(MultiviewCase):
    def setUp(self):
        super().setUp()
        self.sessions.set_slot_factory(self.make_slot)

    def test_leaving_the_grid_keeps_a_recording_tile(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        t3 = self.pick_in("t3", CHANNELS[3], "http://stream/4")
        self.assertTrue(t2.live.start_recording("/tmp/x.ts"))
        self.grid_frame.focusRequested.emit("main")
        self.v()
        self.assertIs(self.sessions.small(), t3)  # focused just before
        self.assertNotIn(
            ("stop",), t2.live.calls[t2.live.calls.index(("start_recording", "/tmp/x.ts")) :]
        )
        self.assertEqual([s.name for s in self.sessions.background()], ["t2"])
        self.assertTrue(self.sessions.muted("t2"))

    def test_a_recording_tile_left_in_the_background_takes_the_full_buffer(self):
        # 2.8 (review of 2.6): it kept its small tile buffer, also once brought back large.
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.pick_in("t3", CHANNELS[3], "http://stream/4")
        self.assertTrue(t2.live.start_recording("/tmp/x.ts"))
        self.grid_frame.focusRequested.emit("main")
        self.assertEqual(t2.live.resizes[-1], 64 * MIB)
        self.v()
        self.assertEqual([s.name for s in self.sessions.background()], ["t2"])
        self.assertEqual(t2.live.resizes[-1], 256 * MIB)

    def with_question(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.question = QuestionCard(self.host)
        self.controller.overlays.question = self.question

    def test_the_small_role_tiles_recording_survives_leaving(self):  # Stage C review I1
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.start_large(0)
        self.v()
        self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.pick_in("pip", CHANNELS[3], "http://stream/4")
        self.grid_frame.focusRequested.emit("main")
        small = self.sessions.other()
        self.assertTrue(small.live.start_recording("/tmp/x.ts"))
        self.controller.stop_for_library()  # leave_multiview(keep_pip=False)
        tail = small.live.calls[small.live.calls.index(("start_recording", "/tmp/x.ts")) :]
        self.assertNotIn(("stop",), tail)
        self.assertIn(small, self.sessions.background())
        self.assertNotIn(self.sessions.other(), self.sessions.background())
        self.assertIsNot(self.sessions.other(), self.sessions.large())

    def test_a_recording_tiles_channel_change_asks_first(self):
        self.with_question()
        self.start_large(0)
        recording = self.controller.live
        self.assertTrue(recording.start_recording("/tmp/x.ts"))
        self.v()
        links = len(self.catalog.links)
        self.tune(1)
        self.assertEqual(
            self.question.question(), "One is recording in this tile. Stop the recording?"
        )
        self.assertEqual(len(self.catalog.links), links)
        self.controller.handle(Action.SEEK_FWD)
        self.controller.handle(Action.OK)
        self.assertFalse(recording.recording())
        self.assertEqual(self.requested()[-1], "Two")

    def test_a_recording_tiles_close_asks_first(self):
        self.with_question()
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.assertTrue(t2.live.start_recording("/tmp/x.ts"))
        self.grid_frame.focusRequested.emit("main")
        self.grid.close_tile("t2")
        self.assertTrue(self.question.isVisibleTo(self.host))
        self.assertTrue(t2.live.running)
        self.controller.handle(Action.ESCAPE)  # keep recording
        self.assertTrue(t2.live.recording())
        self.grid.close_tile("t2")
        self.controller.handle(Action.SEEK_FWD)
        self.controller.handle(Action.OK)
        self.assertEqual(t2.live.calls[-1], ("stop",))

    def test_a_background_player_is_no_tile(self):
        self.start_large(0)
        self.assertTrue(self.controller.live.start_recording("/tmp/x.ts"))
        self.play(1)
        stand_in = self.controller.live
        self.assertTrue(wait_until(lambda: len(stand_in.named("start")) == 1))
        stand_in.play_pipeline()
        self.v()
        self.assertEqual(self.grid.names(), ["b1", "pip", "t2", "t3"])


if __name__ == "__main__":
    unittest.main()
