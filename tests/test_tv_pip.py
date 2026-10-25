"""Picture in picture and the volume mixer in the TV shell's controller.

The controller's fakes are tests/test_tv_controller.py's; here the two players are fake
sessions and live sessions with real LiveTuners, under the real SessionManager and budget.
"""

import os
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, QRect, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu

from services.connection_budget import ConnectionBudget
from services.live.live_session import CRAWLING, LOADING, RECONNECTING, UNAVAILABLE
from services.live.refusals import STREAM_LIMIT
from services.live.tuning import channel_key
from services.provider_ids import with_provider
from services.sessions import PIP_FAILED, SessionManager, Slot
from services.vlc_profile import DEFAULT_USER_AGENT
from tests.test_tv_controller import (
    CHANNELS,
    HARBOR_ONE,
    HOME_ONE,
    MIB,
    NO_MODS,
    ControllerTestCase,
    FakeLiveSession,
    FakeOverlay,
    FakeSession,
    play_context,
    wait_until,
)
from ui.autoplay import FINISHED_TEXT
from ui.keymap import Action
from ui.live_tuner import LiveTuner, one_disk_ring, wait_for_each_other
from ui.page_stack import PageStack
from ui.pip_controls import (
    ALREADY_LARGE,
    NO_PREVIOUS,
    NOTICE_MS,
    OPEN_PIP_FIRST,
    PIP_EXTERNAL,
    PIP_LIVE_ONLY,
    SWITCHING,
    SWITCHING_MS,
)
from ui.tv_pages import TVPages

MOVIE = {"id": "m7", "name": "Heat", "cmd": "http://vod/heat.mkv"}
UP, DOWN = Qt.Key.Key_Up.value, Qt.Key.Key_Down.value  # the mixer's arrows

HARBOR_TWO = {**CHANNELS[1], "_provider": "Harbor IPTV"}  # another provider, an Xtream panel
GROUP_B = [
    {"id": i, "name": name, "number": str(i), "cmd": f"ffrt http://tv/{i}"}
    for i, name in ((5, "Five"), (6, "Six"))
]
SHIFTED = {
    "start": 0.0,
    "end": 300.0,
    "position": 100.0,
    "live_edge": 300.0,
    "lag": 200.0,
    "mode": "shifted",
    "rate": 1.0,
    "paused": False,
    "target": 2.5,
}


class FakeFrame(QObject):
    swapRequested = Signal()

    def __init__(self):
        super().__init__()
        self.calls = []
        self.shown = False
        self.large = "main"

    def show_pip(self):
        self.calls.append(("show_pip",))
        self.shown = True

    def hide_pip(self):
        self.calls.append(("hide_pip",))
        self.shown = False

    def set_large(self, name):
        self.calls.append(("set_large", name))
        self.large = name

    def set_roles(self, large, small):  # the multiview leaves: these are the PiP's roles
        self.calls.append(("set_roles", large, small))
        self.large = large

    def suspend(self):  # the multiview lays the surfaces out meanwhile
        self.calls.append(("suspend",))
        self.shown = False

    def resume(self):
        self.calls.append(("resume",))

    def small_rect(self):
        return QRect(900, 500, 320, 180) if self.shown else QRect()


class FakePipLabel(FakeOverlay):
    closeRequested = Signal()
    muteRequested = Signal()  # the speaker
    swapRequested = Signal()  # the ⇄ button
    info = None
    audible = False

    def set_info(self, name, state):
        self.record("set_info", name, state)
        self.info = (name, state)

    def set_audible(self, audible):
        self.audible = audible

    def set_level(self, level):
        self.level = level

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False


class FakeMixer(FakeOverlay):
    levelRequested = Signal(int, int)
    muteRequested = Signal(int)
    rowSelected = Signal(int)
    followRequested = Signal()  # the multiview's "Follow focus"
    closed = Signal()
    rows = ()
    selected = 0

    mode = None  # the multiview's line under the rows (MixerMode), else None

    def show_rows(self, rows, selected, mode=None):
        self.record("show_rows", rows, selected)
        self.rows, self.selected, self.mode = list(rows), selected, mode
        self.visible = True

    def dismiss(self):
        if self.visible:
            self.visible = False
            self.closed.emit()

    def shown(self):
        """(title, channel, level, muted) per row, and the selected row."""
        return [(r.title, r.channel, r.level, r.muted) for r in self.rows], self.selected


class PagesForTest(TVPages):
    """TVPages with the real page moves (play, _show_page) and none of the Library wiring."""

    def __init__(self, window):
        QObject.__init__(self)
        self._window = window
        self._return_to_library = False
        self._second_click = mock.Mock()
        self._keys_level = None
        self.library_controller = mock.Mock()


class PipCase(ControllerTestCase):
    """Two players (fake sessions and live sessions, each with its real LiveTuner), the real
    SessionManager and budget, a fake PiP frame and label."""

    def setUp(self):
        super().setUp()
        self.config.ssl_verify = True
        self.config.timeshift_enabled = False
        self.config.timeshift_max_mib = 2048
        self.config.selected_provider_name = "Home"
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 2}
        self.catalog.live["20"] = list(GROUP_B)
        self.budget = ConnectionBudget()
        self.live = FakeLiveSession(self.session)
        self.pip_session = FakeSession()
        self.pip_live = FakeLiveSession(self.pip_session)
        self.main_slot = Slot(
            "main",
            self.session,
            self.live,
            tuner=LiveTuner(self.session, self.config, self.live, self.budget),
        )
        self.pip_slot = Slot(
            "pip",
            self.pip_session,
            self.pip_live,
            tuner=LiveTuner(
                self.pip_session, self.config, self.pip_live, self.budget, purpose="pip"
            ),
        )
        self.sessions = SessionManager(self.main_slot, self.pip_slot, self.budget)
        one_disk_ring(self.main_slot.tuner, self.pip_slot.tuner)  # as the window does
        wait_for_each_other(self.main_slot.tuner, self.pip_slot.tuner)
        self.frame = FakeFrame()
        self.label = FakePipLabel()
        self.controller = self.make_controller(
            self.column, sessions=self.sessions, pip_frame=self.frame
        )
        for live in (self.live, self.pip_live):
            self.addCleanup(live.released.emit)  # no capped release wait outlives the test
        self.controller.start()

    mixer = None  # MixerTests: a FakeMixer

    def overlays(self, column):
        overlays = super().overlays(column)
        overlays.pip_label = getattr(self, "label", None)
        overlays.mixer = self.mixer
        return overlays

    def start_large(self, index):
        """Tune a channel on the large picture and play it from its pipeline."""
        live = self.controller.live
        count, running = len(live.named("start")), live.running
        self.play(index)
        if running:
            self.assertEqual(live.calls[-1], ("stop",))
            live.released.emit()
        self.assertTrue(wait_until(lambda: len(live.named("start")) == count + 1))
        live.play_pipeline()

    def open_small(self, channel, group_key="10", channels=CHANNELS):
        """Open `channel` small from the drawer's menu and play it from its pipeline."""
        self.controller.open_pip(channel, group_key=group_key, channels=channels)
        small = self.sessions.other()
        count = len(small.live.named("start"))
        self.ready(f"http://stream/{channel['id']}")
        self.assertTrue(wait_until(lambda: len(small.live.named("start")) == count + 1))
        self.assertTrue(self.sessions.pip_open())
        small.live.play_pipeline()
        return small

    def counts(self):
        """What each player and live session was asked to do so far."""
        return (
            len(self.session.plays),
            self.session.stops,
            len(self.live.calls),
            len(self.pip_session.plays),
            self.pip_session.stops,
            len(self.pip_live.calls),
        )


class PipTests(PipCase):
    def test_p_opens_the_previous_channel_small_once_its_link_comes(self):
        self.start_large(0)
        self.start_large(1)  # One is the previous channel now
        self.controller.handle(Action.PIP)
        self.assertEqual(self.requested()[-1], "One")
        self.assertEqual(self.pip_live.calls, [])
        self.ready("http://stream/1")
        self.assertTrue(wait_until(lambda: self.pip_live.named("start")))
        url, kwargs = self.pip_live.named("start")[0]
        self.assertEqual(url, "http://stream/1")
        self.assertEqual(  # a main tune's start arguments
            kwargs,
            {
                "headers": {"User-Agent": DEFAULT_USER_AGENT},
                "verify_ssl": True,
                "latency": "low",
                "user_agent": DEFAULT_USER_AGENT,
                "disk": False,
                "max_bytes": 256 * MIB,  # the memory per stream
                "channel": channel_key("Home", CHANNELS[0]),
                "one_stream": True,  # 2.7: its provider limits streams (a part parks live)
                "https_first": True,  # 2.7.1: HTTPS is chosen by what answers
            },
        )
        self.assertTrue(self.sessions.pip_open())
        self.assertTrue(self.pip_session.muted)
        self.assertFalse(self.session.muted)
        self.assertEqual(self.frame.calls, [("show_pip",)])
        self.assertTrue(self.label.visible)
        self.assertEqual(self.label.info, ("One", "Connecting…"))
        self.assertEqual(self.transport.named("set_pip_state")[-1], (True, True))
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "pip"])
        self.assertEqual(len(self.live.named("start")), 2)  # the large picture plays on

    def test_the_small_picture_keeps_its_buffer_in_memory(self):
        # Never a second disk ring; the small picture's is the memory per stream.
        self.config.timeshift_enabled = True
        self.config.timeshift_max_mib = 4096
        self.config.timeshift_ram_mib = 512
        self.start_large(0)
        self.assertEqual(self.live.named("start")[0][1]["disk"], True)
        self.open_small(CHANNELS[1])
        kwargs = self.pip_live.named("start")[0][1]
        self.assertEqual((kwargs["disk"], kwargs["max_bytes"]), (False, 512 * MIB))
        self.controller.handle(Action.SWAP)  # the disk ring is small now: a zap stays in RAM
        self.controller.handle(Action.CH_NEXT)
        self.ready("http://stream/3")
        self.pip_live.released.emit()
        self.assertTrue(wait_until(lambda: len(self.pip_live.named("start")) == 2))
        kwargs = self.pip_live.named("start")[1][1]
        self.assertEqual((kwargs["disk"], kwargs["max_bytes"]), (False, 512 * MIB))

    def test_p_again_closes_pip(self):
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)
        self.ready("http://stream/1")
        self.assertTrue(wait_until(self.sessions.pip_open))
        large = self.counts()[:3]
        self.controller.handle(Action.PIP)
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.calls[-1], ("stop",))
        self.assertEqual(self.frame.calls[-2:], [("set_large", "main"), ("hide_pip",)])
        self.assertFalse(self.label.visible)
        self.assertEqual(self.transport.named("set_pip_state")[-1], (True, False))
        self.pip_live.released.emit()
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.assertEqual(self.counts()[:3], large)

    def test_p_while_its_link_is_on_the_way_cancels_it(self):
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)
        self.controller.handle(Action.PIP)  # changed one's mind
        self.ready("http://stream/1")
        QTest.qWait(20)
        self.assertEqual(self.pip_live.calls, [])
        self.assertFalse(self.sessions.pip_open())

    def test_p_without_a_previous_channel_says_so(self):
        self.start_large(0)
        self.controller.handle(Action.PIP)
        self.assertEqual(self.toast.messages()[-1], (NO_PREVIOUS, "info"))
        self.assertEqual(self.requested(), ["One"])

    def test_s_swaps_the_banner_the_zap_group_and_the_transport_follow_the_pip(self):
        self.start_large(0)
        self.open_small(GROUP_B[0], "20", GROUP_B)
        self.pip_live.timelineChanged.emit(SHIFTED)
        self.assertEqual(self.label.info, ("Five", "−03:20"))
        before = self.counts()
        self.controller.handle(Action.SWAP)
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertEqual(self.frame.calls[-1], ("set_large", "pip"))
        self.assertEqual(self.banner.info.name, "Five")
        self.assertEqual(self.zap.context.group_key, "20")
        self.assertEqual(self.zap.context.current(), GROUP_B[0])
        self.assertEqual(self.transport.named("show_timeshift")[-1], ("Five", ""))
        self.assertEqual(self.transport.named("update_timeline")[-1][0], SHIFTED)
        self.assertEqual(self.label.info, ("One", "Connecting…"))  # One is small now
        self.assertEqual(self.counts(), before)  # nothing played, stopped or started again
        self.assertTrue(self.session.muted)  # audio follows the large picture
        self.assertFalse(self.pip_session.muted)
        self.assertIs(self.controller.session, self.pip_session)
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "pip"])

    def test_a_swap_records_the_channel_now_watched(self):
        # Recent and "start with the last channel" follow the large picture.
        self.start_large(0)
        self.open_small(GROUP_B[0], "20", GROUP_B)
        plays = len(self.watch.plays)
        self.controller.handle(Action.SWAP)
        self.assertEqual(
            self.watch.plays[plays:], [("Home", GROUP_B[0], "channel", "http://stream/5", "20")]
        )
        self.controller.handle(Action.SWAP)
        self.assertEqual(
            self.watch.plays[-1], ("Home", CHANNELS[0], "channel", "http://stream/1", "10")
        )

    def test_a_zap_after_a_swap_tunes_the_large_slot_and_leaves_the_small_one_alone(self):
        self.start_large(0)
        self.open_small(GROUP_B[0], "20", GROUP_B)
        self.controller.handle(Action.SWAP)
        main = self.counts()[:3]
        self.controller.handle(Action.CH_NEXT)
        self.assertEqual(self.requested()[-1], "Six")
        self.ready("http://stream/6")
        self.assertEqual(self.pip_live.calls[-1], ("stop",))
        self.pip_live.released.emit()
        self.assertTrue(
            wait_until(lambda: self.pip_live.named("start")[-1][0] == "http://stream/6")
        )
        self.assertEqual(self.counts()[:3], main)  # the small picture got no calls
        self.assertEqual(self.zap.context.current(), GROUP_B[1])
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "pip"])
        self.assertEqual(self.label.info[0], "One")

    def test_zapping_the_large_slot_onto_the_pips_channel_swaps_instead(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        links, before = len(self.catalog.links), self.counts()
        self.controller.handle(Action.CH_NEXT)  # One -> Two, which the PiP shows
        self.assertEqual(len(self.catalog.links), links)  # no second stream of it
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertEqual(self.zap.context.current(), CHANNELS[1])
        self.assertEqual(self.counts(), before)
        self.controller.handle(Action.PREVIOUS)  # back to One: the small picture again
        self.assertIs(self.sessions.large(), self.main_slot)
        self.assertEqual(len(self.catalog.links), links)

    def test_s_without_pip_says_how_to_open_it(self):
        self.start_large(0)
        self.controller.handle(Action.SWAP)
        self.assertEqual(self.toast.messages()[-1], (OPEN_PIP_FIRST, "info"))

    def test_a_click_on_the_small_picture_swaps(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.frame.swapRequested.emit()
        self.assertIs(self.sessions.large(), self.pip_slot)

    def test_the_labels_swap_button_swaps_as_a_click_on_the_small_picture_does(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.label.swapRequested.emit()
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertEqual(self.label.info[0], "One")  # the label names the new small picture
        self.play(2)  # Three, large: waits for Two's release
        self.label.swapRequested.emit()  # refused meanwhile: it says so
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertEqual(self.toast.named("show_message")[-1], (SWITCHING, "info", SWITCHING_MS))

    def test_the_small_pictures_stream_notices_are_said_once_after_its_name(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        before = len(self.toast.named("show_message"))
        for text in (RECONNECTING, LOADING, UNAVAILABLE, RECONNECTING, CRAWLING):
            self.pip_live.notice.emit(text)
        self.assertEqual(
            self.toast.named("show_message")[before:],
            [
                ("Two: Reconnecting…", "info", NOTICE_MS),
                ("Two: Plays without rewind for smoother playback", "info", NOTICE_MS),
            ],
        )
        self.live.notice.emit(RECONNECTING)  # the large picture's, as before: unnamed
        self.assertEqual(self.toast.messages()[-1], ("Reconnecting…", "info"))
        self.open_small(CHANNELS[2])  # another channel small: its notices are said again
        self.pip_live.notice.emit(RECONNECTING)
        self.assertEqual(self.toast.messages()[-1], ("Three: Reconnecting…", "info"))

    def test_an_ended_direct_stream_of_the_small_picture_opens_again_in_its_slot(self):
        self.enterContext(mock.patch("ui.live_controls.RECONNECT_MS", (40, 120)))
        self.enterContext(mock.patch("ui.live_controls.HEALTHY_S", 0.05))
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.pip_live.play_direct()  # its live session fell back: VLC plays the stream itself
        url, kwargs = self.pip_session.plays[-1]
        holders, stops = sorted(self.budget.holders("Home")), self.pip_session.stops

        def end_and_reopen():
            plays = len(self.pip_session.plays)
            self.pip_session.stateChanged.emit("ended")
            self.assertEqual(self.toast.messages()[-1], ("Two: Reconnecting…", "info"))
            self.assertEqual(len(self.pip_session.plays), plays)  # after a back-off
            self.assertTrue(wait_until(lambda: len(self.pip_session.plays) == plays + 1))
            self.assertEqual(self.pip_session.plays[-1][0], url)
            self.assertEqual(self.pip_session.plays[-1][1]["kind"], kwargs["kind"])

        end_and_reopen()
        end_and_reopen()
        self.pip_session.stateChanged.emit("playing")
        QTest.qWait(80)  # it played a while since: twice more
        end_and_reopen()
        end_and_reopen()
        self.pip_session.stateChanged.emit("ended")
        self.assertEqual(self.toast.messages()[-1], ("Two: The stream ended", "info"))
        QTest.qWait(200)
        self.assertEqual(len(self.pip_session.plays), 6)  # the first, then four reopens
        # One slot, the same one, all along: nothing was stopped or given back.
        self.assertEqual(self.pip_session.stops, stops)
        self.assertEqual(sorted(self.budget.holders("Home")), holders)
        self.assertEqual(self.pip_live.named("stop"), [])

    def test_the_small_pictures_reopen_decodes_in_software_as_its_stream_did(self):
        self.enterContext(mock.patch("ui.live_controls.RECONNECT_MS", (40, 120)))
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.pip_live.play_direct()
        self.pip_session.software = True  # hardware decoding froze its picture: software since
        plays = len(self.pip_session.plays)
        self.pip_session.stateChanged.emit("ended")
        self.assertTrue(wait_until(lambda: len(self.pip_session.plays) == plays + 1))
        self.assertIs(self.pip_session.plays[-1][1]["software"], True)

    def test_a_swap_during_the_small_pictures_back_off_reopens_it_large(self):
        self.enterContext(mock.patch("ui.live_controls.RECONNECT_MS", (80, 120)))
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.pip_live.play_direct()
        url = self.pip_session.plays[-1][0]
        plays = len(self.pip_session.plays)
        self.pip_session.stateChanged.emit("ended")
        self.assertEqual(self.toast.messages()[-1], ("Two: Reconnecting…", "info"))
        self.controller.handle(Action.SWAP)  # its player is the large picture's now
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertTrue(wait_until(lambda: len(self.pip_session.plays) == plays + 1))
        self.assertEqual(self.pip_session.plays[-1][0], url)
        QTest.qWait(150)
        self.assertEqual(len(self.pip_session.plays), plays + 1)  # once
        self.assertNotIn(("The stream ended", "info"), self.toast.messages())

    def test_a_swap_during_the_large_pictures_back_off_reopens_it_small(self):
        self.enterContext(mock.patch("ui.live_controls.RECONNECT_MS", (80, 120)))
        self.catalog.ptype = "XTREAM"  # no link to make again: it reconnects
        self.start_large(0)
        self.live.play_direct()  # the large picture's live session fell back
        self.open_small(CHANNELS[1])
        url = self.session.plays[-1][0]
        plays = len(self.session.plays)
        self.session.ended.emit()
        self.assertEqual(self.toast.messages()[-1], ("Reconnecting…", "info"))
        self.controller.handle(Action.SWAP)  # its player is the small picture's now
        self.assertIs(self.sessions.small(), self.main_slot)
        self.assertTrue(wait_until(lambda: len(self.session.plays) == plays + 1))
        self.assertEqual(self.session.plays[-1][0], url)
        QTest.qWait(150)
        self.assertEqual(len(self.session.plays), plays + 1)  # once
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "pip"])

    def test_a_time_shifted_small_picture_is_not_reopened_here(self):
        self.enterContext(mock.patch("ui.live_controls.RECONNECT_MS", (40, 120)))
        self.start_large(0)
        self.open_small(CHANNELS[1])
        plays = len(self.pip_session.plays)
        self.pip_session.stateChanged.emit("ended")  # LiveSession reopens its buffer itself
        QTest.qWait(100)
        self.assertEqual(len(self.pip_session.plays), plays)

    def test_closing_the_small_picture_cancels_its_reopen(self):
        self.enterContext(mock.patch("ui.live_controls.RECONNECT_MS", (40, 120)))
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.pip_live.play_direct()
        self.pip_session.stateChanged.emit("ended")
        self.controller.handle(Action.PIP)  # closed during the back-off
        plays = len(self.pip_session.plays)
        QTest.qWait(100)
        self.assertEqual(len(self.pip_session.plays), plays)

    def test_a_closed_small_picture_says_nothing(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.PIP)  # closed
        before = len(self.toast.named("show_message"))
        self.pip_live.notice.emit(RECONNECTING)
        self.assertEqual(len(self.toast.named("show_message")), before)

    def test_denied_toasts_the_providers_limit(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB"}  # one stream
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)
        self.ready("http://stream/1")
        QTest.qWait(20)
        self.assertIn(("Your provider allows 1 stream at a time", "error"), self.toast.messages())
        self.assertEqual(self.pip_live.calls, [])
        self.assertEqual(self.frame.calls, [])
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_p_during_a_movie_toasts_and_opens_nothing(self):
        self.start_large(0)
        self.start_large(1)
        self.controller.play_vod(MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.live.released.emit()
        links = len(self.catalog.links)
        self.controller.handle(Action.PIP)
        self.assertEqual(self.toast.messages()[-1], (PIP_LIVE_ONLY, "info"))
        self.assertEqual(len(self.catalog.links), links)

    def test_a_movie_closes_the_small_picture_first(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.play_vod(MOVIE, "movie", play_context())
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.calls[-1], ("stop",))

    def test_the_library_stops_both_pictures(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.stop_for_library()
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.calls[-1], ("stop",))
        self.assertEqual(self.live.calls[-1], ("stop",))

    def test_shutdown_closes_the_small_picture_then_stops_the_large(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        order = []
        self.live.stop = lambda: order.append("large")
        self.pip_live.stop = lambda: order.append("small")
        self.controller.shutdown()
        self.assertEqual(order, ["small", "large"])

    def test_closing_while_swapped_keeps_the_picture_that_is_large(self):
        self.start_large(0)
        self.open_small(GROUP_B[0], "20", GROUP_B)
        self.controller.handle(Action.SWAP)
        large = self.counts()[3:]
        self.label.closeRequested.emit()  # the × on the small picture: One
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.assertEqual(self.counts()[3:], large)  # Five plays on, large
        self.assertIs(self.controller.session, self.pip_session)
        self.assertEqual(self.frame.calls[-2:], [("set_large", "pip"), ("hide_pip",)])
        self.live.released.emit()
        self.assertEqual(self.budget.holders("Home"), ["pip"])  # Five's: its player's name
        self.controller.handle(Action.CH_NEXT)  # zapping goes on in Five's group
        self.assertEqual(self.requested()[-1], "Six")

    def test_the_label_names_the_small_pictures_state(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.assertEqual(self.label.info, ("Two", "Connecting…"))
        self.pip_live.timelineChanged.emit(dict(SHIFTED, lag=3.4, target=2.5))
        self.assertEqual(self.label.info, ("Two", "LIVE"))  # within a second of its target
        self.pip_live.timelineChanged.emit(dict(SHIFTED, lag=3.6, target=2.5))
        self.assertEqual(self.label.info, ("Two", "−00:03"))
        self.live.timelineChanged.emit(SHIFTED)  # the large picture's: not the label's
        self.assertEqual(self.label.info, ("Two", "−00:03"))

    def test_a_pip_failure_closes_it_and_says_why(self):
        self.catalog.ptype = "M3UPLAYLIST"  # no link to make again
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.pip_live.failed.emit("http://user:secret@tv/2: 404")
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.toast.messages()[-1], (PIP_FAILED, "error"))

    def test_the_drawers_menu_opens_a_channel_small(self):
        self.start_large(0)
        self.nav.reset_to("live", "10")
        node = self.nav.children(self.nav.path[-1])[2]
        shown = []
        with mock.patch.object(QMenu, "popup", lambda menu, pos, *a: shown.append(menu)):
            self.drawer.contextRequested.emit(node, QPoint(40, 50))
        menu = shown[0]
        self.addCleanup(menu.deleteLater)
        action = next(a for a in menu.actions() if a.text() == "Open in picture-in-picture")
        action.trigger()
        self.assertEqual(self.requested()[-1], "Three")
        self.ready("http://stream/3")
        self.assertTrue(wait_until(self.sessions.pip_open))
        self.controller.handle(Action.SWAP)
        self.assertEqual(self.zap.context.current(), CHANNELS[2])
        self.controller.handle(Action.CH_NEXT)  # ↓ walks the drawer group from Three
        self.assertEqual(self.requested()[-1], "Four")

    def test_ctrl_p_in_the_drawer_opens_the_highlighted_channel_small(self):
        self.start_large(0)
        self.nav.reset_to("live", "10")
        self.drawer.visible = True
        self.drawer.highlight = self.nav.children(self.nav.path[-1])[2]
        self.controller.handle(Action.PIP)
        self.assertEqual(self.requested()[-1], "Three")
        self.assertEqual(self.drawer.named("close_drawer"), [()])

    def test_the_large_pictures_channel_is_not_opened_again_small(self):
        self.start_large(0)
        self.controller.open_pip(CHANNELS[0], group_key="10", channels=CHANNELS)
        self.assertEqual(self.toast.messages()[-1], (ALREADY_LARGE, "info"))
        self.assertEqual(self.requested(), ["One"])

    def test_zapping_onto_the_channel_on_its_way_to_the_pip_opens_it_large_only(self):
        self.start_large(0)
        self.controller.open_pip(CHANNELS[1], group_key="10", channels=CHANNELS)
        self.controller.handle(Action.CH_NEXT)  # Two, whose PiP link is on its way
        self.ready("http://stream/2")  # the PiP's link: it opens nothing now
        QTest.qWait(20)
        self.assertEqual(self.pip_live.calls, [])
        self.assertEqual(self.requested()[-1], "Two")
        self.ready("http://stream/2")  # the large picture's own
        self.live.released.emit()
        self.assertTrue(wait_until(lambda: self.live.named("start")[-1][0] == "http://stream/2"))
        self.assertFalse(self.sessions.pip_open())

    def test_another_provider_keeps_the_small_picture(self):
        # 2.1: browsing another provider changes the list only; the small picture plays on.
        self.catalog.hub_ready("Home")
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.catalog.hub_ready("Home")  # the same provider again: it stays open
        self.assertTrue(self.sessions.pip_open())
        self.catalog.provider = "Backup"
        self.catalog.hub_ready("Backup")
        self.assertTrue(self.sessions.pip_open())
        self.assertNotIn(("stop",), self.pip_live.calls)

    def test_an_external_play_closes_the_small_picture_first(self):
        # External players are outside the budget; the PiP gives its slot back.
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.config.play_in_vlc = True
        self.play(2)
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.calls[-1], ("stop",))

    def test_with_an_external_player_picture_in_picture_says_no(self):
        # The external player holds the provider's stream; a PiP would be a second.
        self.start_large(0)
        self.config.play_in_vlc = True
        self.play(1)  # Two plays in VLC once the in-app stream let go
        self.live.released.emit()
        self.assertEqual(self.external.calls, [("vlc", "http://stream/2")])
        links = len(self.catalog.links)
        asks = {
            "P": lambda: self.controller.handle(Action.PIP),
            "the menu": lambda: self.controller.open_pip(
                CHANNELS[2], group_key="10", channels=CHANNELS
            ),
            "the transport's button": self.transport.pipRequested.emit,
        }
        for name, ask in asks.items():
            with self.subTest(name):
                ask()
                self.assertEqual(self.toast.messages()[-1], (PIP_EXTERNAL, "info"))
                self.assertEqual(len(self.catalog.links), links)  # no link: nothing opens
                self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.calls, [])

    def test_play_in_vlc_from_the_menu_closes_the_small_picture_first(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.nav.reset_to("live", "10")
        node = self.nav.children(self.nav.path[-1])[2]
        shown = []
        with mock.patch.object(QMenu, "popup", lambda menu, pos, *a: shown.append(menu)):
            self.drawer.contextRequested.emit(node, QPoint(40, 50))
        self.addCleanup(shown[0].deleteLater)
        next(a for a in shown[0].actions() if a.text() == "Play in VLC").trigger()
        self.ready("http://stream/3")
        self.assertFalse(self.sessions.pip_open())

    def test_a_full_provider_says_so_before_asking_for_a_link(self):
        # An STB portal mints a link per request; its one stream is taken.
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB"}
        self.start_large(0)
        self.start_large(1)
        links = len(self.catalog.links)
        self.controller.handle(Action.PIP)
        self.assertEqual(len(self.catalog.links), links)
        self.assertEqual(
            self.toast.messages()[-1], ("Your provider allows 1 stream at a time", "error")
        )

    def test_a_provider_setup_while_the_pips_link_is_on_its_way_asks_again(self):
        # A setup drops every link request; the PiP's must not stay stuck.
        self.catalog.hub_ready("Home")
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)
        self.assertEqual(self.requested()[-1], "One")
        links = len(self.catalog.links)
        self.catalog.hub_ready("Home")  # a refresh: the same provider
        self.assertEqual(len(self.catalog.links), links + 1)  # asked again
        self.assertEqual(self.requested()[-1], "One")
        self.ready("http://stream/1")
        self.assertTrue(wait_until(self.sessions.pip_open))

    def test_after_another_provider_pip_still_opens(self):
        # 2.1: another provider's setup leaves the PiP's link alone; it still arrives.
        self.catalog.hub_ready("Home")
        home = [with_provider(c, "Home") for c in CHANNELS]  # items are always stamped
        self.tune = lambda i: self.controller.tune(home[i], group_key="10", channels=home)
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)  # its link is on its way…
        links = len(self.catalog.links)
        self.catalog.provider = "Backup"
        self.catalog.hub_ready("Backup")  # …and stays: it is Home's, not Backup's
        self.assertEqual(len(self.catalog.links), links)  # nothing is asked again
        self.assertEqual(self.requested()[-1], "One")
        self.ready("http://stream/1")
        self.assertTrue(wait_until(self.sessions.pip_open))

    def test_a_provider_failure_forgets_the_pips_link_and_p_works_again(self):
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)
        self.catalog.hub_failed("Home", "Portal down")
        links = len(self.catalog.links)
        self.controller.handle(Action.PIP)  # nothing was open: P asks again
        self.assertEqual(len(self.catalog.links), links + 1)

    def test_a_swap_waits_for_nothing_while_the_large_pictures_stream_opens(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.play(2)  # Three: waits for One's release
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.controller.handle(Action.SWAP)
        self.assertIs(self.sessions.large(), self.main_slot)  # nothing swapped
        self.assertEqual(self.toast.messages()[-1], (SWITCHING, "info"))
        self.frame.swapRequested.emit()  # a click on the PiP says so too
        self.assertEqual(self.toast.named("show_message")[-1], (SWITCHING, "info", SWITCHING_MS))
        self.live.released.emit()
        self.assertTrue(wait_until(lambda: self.live.named("start")[-1][0] == "http://stream/3"))
        self.controller.handle(Action.SWAP)
        self.assertIs(self.sessions.large(), self.pip_slot)


class BrowsingProviderPipTests(PipCase):
    """Choosing another provider in the list changes the list only."""

    def test_browsing_another_provider_stops_nothing(self):
        self.start_large(0)
        self.open_small(CHANNELS[2])
        before = self.counts()
        self.controller.select_provider("Backup")
        self.catalog.provider = "Backup"
        self.catalog.hub_ready("Backup")
        self.assertTrue(self.sessions.pip_open())
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.zap.context.group_key, "10")
        self.controller.handle(Action.CH_NEXT)  # the zap list is still the playing provider's
        self.assertEqual(self.requested()[-1], "Two")  # (One's next; Three is the small one)

    def test_a_link_on_its_way_still_arrives_after_browsing(self):
        self.controller.tune(HOME_ONE, group_key="10", channels=[HOME_ONE])
        self.controller.select_provider("Backup")
        self.catalog.provider = "Backup"
        self.catalog.hub_ready("Backup")
        self.assertEqual(self.requested(), ["One"])
        self.ready("http://stream/1")
        self.assertTrue(wait_until(lambda: self.live.named("start")))
        self.assertEqual(self.live.named("start")[0][0], "http://stream/1")


class LibraryPlayTests(PipCase):
    """A title from the Library while pictures run (QiTV 2.1): it takes the large picture."""

    def play_from_library(self, movie=MOVIE):
        self.controller.enter_library_playing()  # the Library is open, the player in its corner
        self.controller.play_vod(movie, "movie", play_context())
        self.ready("http://vod/heat.mkv")

    def small_title(self):
        return self.label.info[0]

    def test_a_movie_takes_the_big_picture_and_the_channel_goes_small(self):
        self.start_large(0)
        before = (len(self.live.calls), self.session.stops, len(self.session.plays))
        self.play_from_library()
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertEqual([url for url, _ in self.pip_session.plays], ["http://vod/heat.mkv"])
        self.assertTrue(self.sessions.pip_open())
        self.assertEqual(self.small_title(), "One")
        self.assertIs(self.controller.session, self.pip_session)
        # The channel was not restarted, stopped or re-pointed.
        self.assertEqual(
            (len(self.live.calls), self.session.stops, len(self.session.plays)), before
        )
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "pip"])

    def test_a_movie_through_the_pages_keeps_the_channel_small(self):
        """The real call order: TVPages.play switches the page (which leaves the corner)
        before play_vod runs."""
        self.start_large(0)
        before = (len(self.live.calls), self.session.stops, len(self.session.plays))
        self.controller.enter_library_playing()
        library_page, player_page = object(), object()
        stack = mock.Mock()
        stack.mode.return_value = PageStack.CORNER
        stack.currentWidget.return_value = library_page

        def show_player():
            stack.mode.return_value = PageStack.PLAYER
            stack.currentWidget.return_value = player_page

        stack.show_player.side_effect = show_player
        window = mock.Mock(
            pages=stack,
            library_page=library_page,
            player_page=player_page,
            controller=self.controller,
        )
        window.closing.return_value = False
        pages = PagesForTest(window)
        pages.play(MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.assertTrue(self.sessions.pip_open())
        self.assertEqual([url for url, _ in self.pip_session.plays], ["http://vod/heat.mkv"])
        self.assertEqual(self.small_title(), "One")
        self.assertEqual(
            (len(self.live.calls), self.session.stops, len(self.session.plays)), before
        )

    def test_a_previous_small_picture_closes(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.play_from_library()
        self.assertEqual(self.pip_live.calls[-1], ("stop",))  # Two closed first
        self.assertNotIn("http://vod/heat.mkv", [url for url, _ in self.pip_session.plays])
        self.pip_live.released.emit()
        self.assertEqual(self.pip_session.plays[-1][0], "http://vod/heat.mkv")
        self.assertEqual(self.small_title(), "One")
        self.assertNotIn(("stop",), self.live.calls)
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "pip"])

    def test_the_movies_provider_limit_closes_its_small_picture(self):
        self.controller.tune(HARBOR_ONE, group_key="10", channels=[HARBOR_ONE])
        self.ready("http://stream/1")
        self.assertTrue(wait_until(lambda: self.live.named("start")))
        self.live.play_pipeline()
        movie = {**MOVIE, "_provider": "Harbor IPTV"}  # Harbor's panel allows one stream
        self.play_from_library(movie)
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(
            self.toast.messages()[-1],
            ("Closed One to play Heat: Harbor IPTV allows 1 stream", "info"),
        )
        self.assertLessEqual(len(self.budget.holders("Harbor IPTV")), 1)
        self.assertEqual(self.live.calls[-1], ("stop",))  # as before: the channel stops
        self.live.released.emit()
        self.assertEqual([url for url, _ in self.session.plays][-1], "http://vod/heat.mkv")
        self.assertEqual(self.budget.holders("Harbor IPTV"), ["main"])

    def test_the_movies_limit_says_the_channel_closes_only_once_its_link_comes(self):
        # The close was said before the link came; when that failed, One played on.
        self.controller.tune(HARBOR_ONE, group_key="10", channels=[HARBOR_ONE])
        self.ready("http://stream/1")
        self.assertTrue(wait_until(lambda: self.live.named("start")))
        self.live.play_pipeline()
        self.controller.enter_library_playing()
        self.controller.play_vod({**MOVIE, "_provider": "Harbor IPTV"}, "movie", play_context())
        self.assertEqual(self.toast.messages(), [])
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Not found")
        self.assertEqual(self.toast.messages(), [("Heat: Not found", "error")])
        self.assertNotIn(("stop",), self.live.calls)  # One plays on, large

    def test_the_movies_limit_keeps_another_providers_small_picture(self):
        # The small picture closed before the limit was looked at, for nothing.
        self.controller.tune(HARBOR_ONE, group_key="10", channels=[HARBOR_ONE])
        self.ready("http://stream/1")
        self.assertTrue(wait_until(lambda: self.live.named("start")))
        self.live.play_pipeline()
        small = self.open_small(CHANNELS[1])  # Home's Two
        self.play_from_library({**MOVIE, "_provider": "Harbor IPTV"})  # one Harbor stream
        self.assertEqual(
            self.toast.messages()[-1],
            ("Closed One to play Heat: Harbor IPTV allows 1 stream", "info"),
        )
        self.assertTrue(self.sessions.pip_open())
        self.assertNotIn(("stop",), small.live.calls)  # Two plays on, small
        self.assertEqual(self.label.info[0], "Two")
        self.assertEqual(self.live.calls[-1], ("stop",))  # One gives the film its stream
        self.live.released.emit()
        self.assertEqual([url for url, _ in self.session.plays][-1], "http://vod/heat.mkv")
        self.assertEqual(self.budget.holders("Harbor IPTV"), ["main"])
        self.assertEqual(self.budget.holders("Home"), ["pip"])

    def test_a_failed_link_gives_the_channel_the_big_picture_back(self):
        # The channel stayed small beside an empty big picture.
        self.start_large(0)
        self.controller.enter_library_playing()
        self.controller.play_vod(MOVIE, "movie", play_context())
        self.assertIs(self.sessions.large(), self.pip_slot)  # One went small for it
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Not found")
        self.assertIs(self.sessions.large(), self.main_slot)
        self.assertFalse(self.sessions.pip_open())
        self.assertIs(self.controller.session, self.session)
        self.assertFalse(self.session.muted)  # heard again
        self.assertNotIn(("stop",), self.live.calls)  # One never stopped
        self.assertEqual(self.controller._channel, CHANNELS[0])  # INFO, ★, the guide
        self.assertEqual(self.toast.messages()[-1], ("Heat: Not found", "error"))
        self.controller.handle(Action.CH_NEXT)  # zapping goes on from One
        self.assertEqual(self.requested()[-1], "Two")

    def change_small_while_the_film_comes(self):
        """One large; Heat from the Library sends it small; while Heat's link is on its way,
        Three is opened small instead: that change waits for One's stream to let go."""
        self.start_large(0)
        self.controller.enter_library_playing()
        self.controller.play_vod(MOVIE, "movie", play_context())
        film = self.catalog.links[-1][0]
        self.controller.open_pip(CHANNELS[2], group_key="10", channels=CHANNELS)
        self.ready("http://stream/3")
        self.assertEqual(self.live.calls[-1], ("stop",))  # One gives way to Three
        return film

    def test_a_failed_link_while_the_small_picture_changes_waits_for_it(self):
        # The swap is refused while the change is on its way, and the big picture
        # stayed empty.
        film = self.change_small_while_the_film_comes()
        self.catalog.linkFailed.emit(film, "Not found")
        self.assertEqual(self.toast.messages()[-1], ("Heat: Not found", "error"))
        self.assertIs(self.sessions.large(), self.pip_slot)  # still empty, for now
        self.three_opens()
        self.assertIs(self.sessions.large(), self.main_slot)  # ...and takes the big picture
        self.assertFalse(self.sessions.pip_open())  # the empty one closed
        self.assertEqual(self.controller._channel, CHANNELS[2])

    def test_back_to_the_library_while_the_small_picture_changes_drops_the_film(self):
        film = self.change_small_while_the_film_comes()
        self.controller.enter_library_playing()
        self.catalog.linkReady.emit(film, "http://vod/heat.mkv")  # late: it opens nothing
        self.assertEqual(self.pip_session.plays, [])
        self.three_opens()
        self.assertIs(self.sessions.large(), self.main_slot)  # Three, large in the corner
        self.assertFalse(self.sessions.pip_open())

    def three_opens(self):
        self.live.released.emit()  # One let go: Three opens small
        self.assertTrue(wait_until(lambda: self.live.named("start")[-1][0] == "http://stream/3"))

    def test_a_choice_made_meanwhile_is_not_swapped_small_once_the_change_settles(self):
        film = self.change_small_while_the_film_comes()
        self.catalog.linkFailed.emit(film, "Not found")
        self.tune(3)  # Four, chosen since for the big picture
        self.three_opens()
        self.assertIs(self.sessions.large(), self.pip_slot)  # Four's link is on its way there
        self.assertTrue(self.sessions.pip_open())
        self.assertEqual(self.requested()[-1], "Four")

    def test_a_film_whose_open_fails_gives_the_channel_the_big_picture_back(self):
        # The open raising (no player, say) left the big picture black.
        self.start_large(0)
        self.controller.enter_library_playing()
        self.controller.play_vod(MOVIE, "movie", play_context())
        with mock.patch.object(self.pip_session, "play", side_effect=RuntimeError("No player")):
            with self.assertLogs("ui.tv_controller", "WARNING"):
                self.ready("http://vod/heat.mkv")
        self.assertIs(self.sessions.large(), self.main_slot)
        self.assertFalse(self.sessions.pip_open())
        self.assertNotIn(("stop",), self.live.calls)
        self.assertEqual(self.toast.messages()[-1], ("Heat: No player", "error"))

    def test_a_failed_sign_in_of_the_movies_provider_gives_the_channel_back_too(self):
        self.start_large(0)
        self.controller.enter_library_playing()
        self.controller.play_vod({**MOVIE, "_provider": "Harbor IPTV"}, "movie", play_context())
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.catalog.catalogs.providerFailed.emit("Harbor IPTV", "Portal down")
        self.assertIs(self.sessions.large(), self.main_slot)
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.toast.messages()[-1], ("Harbor IPTV: Portal down", "error"))

    def pages(self):
        """TVPages over a window whose stack shows the Library with the player in its corner."""
        library_page, player_page = object(), object()
        stack = mock.Mock()
        stack.mode.return_value = PageStack.CORNER
        stack.currentWidget.return_value = library_page

        def show(mode, page):
            stack.mode.return_value = mode
            stack.currentWidget.return_value = page

        stack.show_player.side_effect = lambda: show(PageStack.PLAYER, player_page)
        stack.show_library.side_effect = lambda corner: show(
            PageStack.CORNER if corner else PageStack.LIBRARY, library_page
        )
        window = mock.Mock(
            pages=stack,
            library_page=library_page,
            player_page=player_page,
            controller=self.controller,
        )
        window.closing.return_value = False
        return PagesForTest(window), stack

    def test_back_to_the_library_before_the_films_link_came_keeps_the_channel_playing(self):
        # ‹ Library (or Esc) then found nothing playing and stopped both.
        self.start_large(0)
        self.controller.enter_library_playing()
        pages, stack = self.pages()
        pages.play(MOVIE, "movie", play_context())  # One goes small; Heat's link is on its way
        self.assertTrue(self.sessions.pip_open())
        pages.back_to_library()
        stack.show_library.assert_called_with(corner=True)  # One plays on, in the corner
        self.assertIs(self.sessions.large(), self.main_slot)  # large again: the film is dropped
        self.assertFalse(self.sessions.pip_open())
        self.assertNotIn(("stop",), self.live.calls)
        self.assertFalse(self.session.muted)
        self.ready("http://vod/heat.mkv")  # late: it opens nothing
        self.assertEqual(self.pip_session.plays, [])

    def test_a_swap_while_the_films_link_comes_leaves_nothing_to_give_back_later(self):
        self.start_large(1)
        self.start_large(0)  # One, after Two
        self.controller.enter_library_playing()
        self.controller.play_vod(MOVIE, "movie", play_context())  # One small, Heat on its way
        self.controller.handle(Action.SWAP)  # One large again, the empty picture closes
        self.assertFalse(self.sessions.pip_open())
        self.controller.handle(Action.PIP)  # Two, small
        self.ready("http://stream/2")
        self.assertTrue(wait_until(self.sessions.pip_open))
        self.controller.enter_library_playing()
        self.assertIs(self.sessions.large(), self.main_slot)  # nothing swapped
        self.assertTrue(self.sessions.pip_open())

    def test_a_movie_of_another_provider_keeps_the_channel_small_whatever_its_limit(self):
        self.start_large(0)  # Home, two streams
        self.play_from_library({**MOVIE, "_provider": "Harbor IPTV"})
        self.assertTrue(self.sessions.pip_open())
        # Each stream carries its player's name: the channel stays the main player's, small.
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.assertEqual(self.budget.holders("Harbor IPTV"), ["pip"])

    def test_swapping_twice_keeps_the_movies_resume_point_and_controls(self):
        # S, then S again, while a film from the Library plays large.
        cid = "Home:movie:m7"
        self.start_large(0)
        self.play_from_library()
        self.pip_session.positionChanged.emit(60_000, 600_000)
        self.assertEqual(self.watch.positions[-1], (cid, 60_000, 600_000))
        self.controller.handle(Action.SWAP)  # the channel large, the film small
        self.assertIs(self.sessions.large(), self.main_slot)
        self.assertEqual(self.shown_controls(), "show_timeshift")  # the channel's
        self.pip_session.positionChanged.emit(61_000, 600_000)  # small, it still counts
        self.assertEqual(self.watch.positions[-1], (cid, 61_000, 600_000))
        self.assertNotIn(("update_position", 61_000, 600_000), self.transport.calls)
        self.controller.handle(Action.SWAP)  # the film large again
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertEqual(self.shown_controls(), "show_vod")
        self.assertEqual(self.transport.named("show_vod")[-1], ("Heat", ""))
        self.pip_session.positionChanged.emit(590_000, 600_000)  # its end: finished
        self.assertEqual(self.watch.positions[-1], (cid, 590_000, 600_000))
        self.assertEqual(self.transport.calls[-1], ("update_position", 590_000, 600_000))

    def test_a_movie_swapped_small_shows_its_title_without_live_wording(self):
        # Polish: the label used live wording ("Connecting…") for the film playing small.
        self.start_large(0)
        self.play_from_library()
        self.assertEqual(self.small_title(), "One")  # the channel kept small
        self.controller.handle(Action.SWAP)  # the channel large, the film small
        self.assertEqual(self.label.info, (MOVIE["name"], ""))
        self.pip_session.positionChanged.emit(61_000, 600_000)
        self.assertEqual(self.label.info, (MOVIE["name"], ""))
        self.controller.handle(Action.SWAP)  # the film large again
        self.assertEqual(self.small_title(), "One")
        self.assertNotEqual(self.label.info[1], "")  # the channel's state again

    def shown_controls(self):
        shows = ("show_vod", "show_live", "show_timeshift")
        return [call[0] for call in self.transport.calls if call[0] in shows][-1]

    def test_without_the_library_a_movie_still_closes_the_small_picture(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.play_vod(MOVIE, "movie", play_context())
        self.assertFalse(self.sessions.pip_open())

    def test_a_channel_from_the_librarys_search_tunes_the_big_picture_only(self):
        self.start_large(0)
        small = self.open_small(CHANNELS[1])
        self.controller.enter_library_playing()
        self.controller.tune_live(CHANNELS[2])
        self.assertTrue(self.sessions.pip_open())
        self.assertNotIn(("stop",), small.live.calls)
        self.assertEqual(self.requested()[-1], "Three")


class TitleBesideChannelTests(PipCase):
    """A film from the Library large and the channel kept small: what follows it, and its end
    in the small picture (nothing said it ended there and nothing offered Next; Next
    and autoplay closed the channel kept small)."""

    FERRY = {"id": "m8", "name": "Night Ferry", "cmd": "http://vod/night-ferry.mkv"}

    def setUp(self):
        super().setUp()
        self.start_large(0)  # One, Home's
        self.controller.enter_library_playing()
        self.films = [MOVIE, self.FERRY]
        self.controller.play_vod(MOVIE, "movie", play_context(genre_items=self.films))
        self.ready("http://vod/heat.mkv")
        self.assertIs(self.sessions.large(), self.pip_slot)  # Heat large, One small

    @staticmethod
    def end(session):
        """As PlaybackSession: the state is "ended" before ended() is emitted."""
        session.stateChanged.emit("ended")
        session.ended.emit()

    def assert_one_plays_small_beside(self, url):
        self.assertTrue(self.sessions.pip_open())
        self.assertIs(self.sessions.small(), self.main_slot)
        self.assertEqual(self.label.info[0], "One")
        self.assertNotIn(("stop",), self.live.calls)
        self.assertEqual(self.pip_session.plays[-1][0], url)

    def test_next_after_the_film_keeps_the_channel_small(self):
        self.end(self.pip_session)  # Heat ends large
        self.assertEqual(self.next_card.named("show_next"), [("Night Ferry", "", "movie")])
        self.next_card.activate()
        self.ready("http://vod/night-ferry.mkv")
        self.assert_one_plays_small_beside("http://vod/night-ferry.mkv")
        self.transport.nextRequested.emit()  # the transport's Next: the same
        self.assertEqual(self.requested()[-1], "Night Ferry")

    def test_another_title_from_the_library_keeps_the_channel_small_too(self):
        self.controller.enter_library_playing()
        self.controller.play_vod(self.FERRY, "movie", play_context())
        self.ready("http://vod/night-ferry.mkv")
        self.assert_one_plays_small_beside("http://vod/night-ferry.mkv")

    def test_a_title_of_the_channels_provider_without_a_second_stream_closes_it(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 1}
        self.controller.play_vod(self.FERRY, "movie", play_context())  # Home's, as One
        self.assertTrue(self.sessions.pip_open())  # until its link comes
        self.assertEqual(self.toast.messages(), [])
        self.ready("http://vod/night-ferry.mkv")
        self.assertEqual(
            self.toast.messages()[-1],
            ("Closed One to play Night Ferry: Home allows 1 stream", "info"),
        )
        self.assertFalse(self.sessions.pip_open())

    def test_a_title_whose_link_fails_closes_nothing_and_says_only_that(self):
        # "Closed One to play …" was said before the link came; One played on.
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 1}
        self.controller.play_vod(self.FERRY, "movie", play_context())
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Not found")
        self.assertEqual(self.toast.messages(), [("Night Ferry: Not found", "error")])
        self.assertTrue(self.sessions.pip_open())  # One plays on, small
        self.assertNotIn(("stop",), self.live.calls)

    def test_a_film_that_ends_small_says_so_and_offers_next_once_large_again(self):
        self.controller.handle(Action.SWAP)  # One large, Heat small
        self.assertEqual(self.label.info, ("Heat", ""))
        self.end(self.pip_session)
        self.assertEqual(self.label.info, ("Heat", FINISHED_TEXT))
        self.assertEqual(self.next_card.named("show_next"), [])  # One plays large: no card
        self.controller.handle(Action.SWAP)  # ⇄, S or a click: Heat large again
        self.assertEqual(self.next_card.named("show_next"), [("Night Ferry", "", "movie")])
        self.next_card.activate()
        self.ready("http://vod/night-ferry.mkv")
        self.assert_one_plays_small_beside("http://vod/night-ferry.mkv")

    def test_a_film_that_fails_large_gives_the_channel_the_big_picture_back(self):
        # Only a failed link or sign-in did; a film whose playback failed left the
        # big picture black with the channel small.
        self.pip_session.errorOccurred.emit("The stream could not be opened")  # Heat, large
        self.assertIs(self.sessions.large(), self.main_slot)
        self.assertFalse(self.sessions.pip_open())  # the failed film closed
        self.assertFalse(self.session.muted)
        self.assertNotIn(("stop",), self.live.calls)
        self.assertEqual(self.controller._channel, CHANNELS[0])  # INFO, ★, the guide
        self.assertEqual(self.toast.messages(), [("Heat: The stream could not be opened", "error")])

    def test_the_channel_kept_small_that_fails_opens_again_beside_the_film(self):
        # The STB retry's fresh link was dropped because a movie plays, and
        # nothing was said (before the retry: "Couldn't open this channel in PiP").
        self.live.failed.emit("The stream could not be opened")  # One, Home's portal, small
        self.assertFalse(self.sessions.pip_open())
        self.live.released.emit()
        self.ready("http://stream/1?play_token=new")
        self.assertTrue(wait_until(lambda: len(self.live.named("start")) == 2))
        self.assertEqual(self.live.named("start")[-1][0], "http://stream/1?play_token=new")
        self.assertTrue(self.sessions.pip_open())
        self.assertIs(self.sessions.small(), self.main_slot)
        self.assertEqual(self.label.info[0], "One")
        self.assertEqual(self.toast.messages(), [])
        self.live.play_pipeline()
        self.live.failed.emit("The stream could not be opened")  # once: said now
        self.assertEqual(self.toast.messages(), [(PIP_FAILED, "error")])

    def test_a_film_that_fails_small_says_why_once(self):
        # The small player's errors had no listener.
        self.session.errorOccurred.emit("Broken")  # One's, small: its live session's to recover
        self.assertEqual(self.toast.messages(), [])
        self.controller.handle(Action.SWAP)  # One large, Heat small
        self.pip_session.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.toast.messages(), [("Heat: The stream could not be opened", "error")])
        self.assertTrue(self.sessions.pip_open())  # its title stays: × closes it, ⇄ swaps
        self.controller.handle(Action.SWAP)  # Heat large: it says it as the big picture
        self.pip_session.errorOccurred.emit("Broken")
        self.assertEqual(self.toast.messages()[-1], ("Heat: Broken", "error"))
        self.assertEqual(len(self.toast.messages()), 2)

    def test_a_swap_drops_the_next_card(self):
        self.end(self.pip_session)  # Heat ends large: the card offers Night Ferry
        links = len(self.catalog.links)
        self.controller.handle(Action.SWAP)  # One large: the card was Heat's
        self.assertFalse(self.next_card.visible)
        self.next_card.activate()
        self.assertEqual(len(self.catalog.links), links)
        self.assertTrue(self.sessions.pip_open())


class PipLinkSettlingTests(PipCase):
    """The small picture's link settles with its own channel's provider."""

    def setUp(self):
        super().setUp()
        self.start_large(0)
        self.home_two = {**CHANNELS[1], "_provider": "Home"}

    def test_a_browsed_providers_failure_keeps_the_small_pictures_link(self):
        self.controller.open_pip(self.home_two, group_key="10", channels=[self.home_two])
        self.catalog.provider = "Backup"
        self.catalog.hub_failed("Backup", "down")
        self.ready("http://stream/2")
        self.assertTrue(wait_until(lambda: self.pip_live.named("start")))
        self.assertTrue(self.sessions.pip_open())

    def test_its_own_providers_failure_forgets_the_small_picture(self):
        self.controller.open_pip(self.home_two, group_key="10", channels=[self.home_two])
        self.catalog.hub_failed("Home", "down")
        self.ready("http://stream/2")  # a late result is ignored
        QTest.qWait(20)
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.named("start"), [])

    def test_another_providers_failed_sign_in_says_why_the_small_picture_never_opens(self):
        # The list shows Home; Harbor's channel was opened small.
        self.controller.open_pip(HARBOR_TWO, group_key="10", channels=[HARBOR_TWO])
        self.catalog.catalogs.providerFailed.emit("Harbor IPTV", "Portal down")
        self.assertEqual(self.toast.messages()[-1], ("Harbor IPTV: Portal down", "error"))
        self.assertEqual(self.controller._channel, CHANNELS[0])  # the large picture's
        self.ready("http://stream/2")  # a late result is ignored
        QTest.qWait(20)
        self.assertFalse(self.sessions.pip_open())

    def test_its_own_providers_setup_asks_the_link_again(self):
        self.controller.open_pip(self.home_two, group_key="10", channels=[self.home_two])
        count = len(self.catalog.links)
        self.catalog.hub_ready("Harbor IPTV")  # another provider's: untouched
        self.assertEqual(len(self.catalog.links), count)
        self.catalog.hub_ready("Home")
        self.assertEqual(len(self.catalog.links), count + 1)
        self.ready("http://stream/2")
        self.assertTrue(wait_until(lambda: self.pip_live.named("start")))


class PipRetryTests(PipCase):
    """An STB channel that fails small gets a fresh link once, as the large picture does
    (the PiP toasted at once, so an expired portal session of a provider shown only small
    needed the channel opened again by hand)."""

    PORTAL_TWO = {**CHANNELS[1], "_provider": "Portal B"}  # an STB portal, not browsed

    def setUp(self):
        super().setUp()
        self.catalog.catalogs.ready.add("Portal B")  # set up: its links were made
        self.start_large(0)

    def fail_small(self, small, message="The stream could not be opened"):
        small.live.failed.emit(message)
        small.live.released.emit()  # its stream let go: a new open may start

    def test_its_own_portal_signs_in_again_and_the_channel_opens_small_again(self):
        small = self.open_small(self.PORTAL_TWO)
        links = len(self.catalog.links)
        self.fail_small(small)
        self.assertEqual(self.toast.messages(), [])
        self.assertEqual(self.catalog.setups, ["Portal B"])  # signed in again, then...
        self.assertEqual(len(self.catalog.links), links + 1)  # ...a fresh link
        self.assertEqual(self.catalog.links[-1][1], self.PORTAL_TWO)
        self.ready("http://stream/2?play_token=new")
        self.assertTrue(wait_until(lambda: len(small.live.named("start")) == 2))
        self.assertEqual(small.live.named("start")[-1][0], "http://stream/2?play_token=new")
        self.assertTrue(self.sessions.pip_open())
        self.assertEqual(self.label.info[0], "Two")
        small.live.play_pipeline()
        self.fail_small(small)  # once per channel opened small
        self.assertEqual(self.toast.messages(), [(PIP_FAILED, "error")])
        self.assertEqual(len(self.catalog.links), links + 1)
        self.assertFalse(self.sessions.pip_open())

    def test_both_pictures_failing_on_one_portal_sign_it_in_once(self):
        # Each retry signed the portal in again: two handshakes, one of them wasted.
        self.catalog.specs["Portal B"]["max_streams"] = 2
        three = {**CHANNELS[2], "_provider": "Portal B"}
        self.controller.tune(three, group_key="10", channels=[three])
        self.ready("http://stream/3")
        self.live.released.emit()
        self.assertTrue(wait_until(lambda: len(self.live.named("start")) == 2))
        self.live.play_pipeline()
        small = self.open_small(self.PORTAL_TWO)
        links = len(self.catalog.links)
        self.live.failed.emit("The stream could not be opened")  # the large picture's
        self.fail_small(small)
        self.assertEqual(self.catalog.setups, ["Portal B"])  # one sign-in serves both links
        self.assertEqual(len(self.catalog.links), links + 2)

    def test_the_browsed_portal_makes_the_link_again_without_signing_in(self):
        small = self.open_small(CHANNELS[1])
        links = len(self.catalog.links)
        self.fail_small(small)
        self.assertEqual(self.catalog.setups, [])
        self.assertEqual(len(self.catalog.links), links + 1)

    def test_a_refusal_for_the_limit_is_never_retried(self):
        small = self.open_small(self.PORTAL_TWO)
        links = len(self.catalog.links)
        self.fail_small(small, STREAM_LIMIT)
        self.assertEqual(len(self.catalog.links), links)
        self.assertEqual(self.toast.messages()[-1][1], "error")

    def test_another_providers_type_toasts_at_once(self):
        small = self.open_small(HARBOR_TWO)  # an Xtream panel: no token to renew
        links = len(self.catalog.links)
        self.fail_small(small)
        self.assertEqual(len(self.catalog.links), links)
        self.assertEqual(self.toast.messages(), [(PIP_FAILED, "error")])

    def test_a_channel_chosen_small_since_is_not_replaced_by_the_retry(self):
        small = self.open_small(self.PORTAL_TWO)
        self.controller.open_pip(CHANNELS[2], group_key="10", channels=CHANNELS)  # its link...
        links = len(self.catalog.links)
        self.fail_small(small)  # ...is on its way: Three opens, not Two again
        self.assertEqual(len(self.catalog.links), links)
        self.ready("http://stream/3")
        self.assertTrue(wait_until(lambda: small.live.named("start")[-1][0] == "http://stream/3"))


class MixedProviderPipTests(PipCase):
    """The small picture's limit and slot are those of its channel's own provider."""

    def setUp(self):
        super().setUp()
        # Home (browsed) allows one stream and the large picture holds it; Harbor's panel
        # reported one for itself.
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 1}

    def test_the_limit_is_the_small_channels_provider(self):
        self.start_large(0)
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.open_small(HARBOR_TWO)  # Home is full, Harbor is not: both play
        self.assertTrue(self.sessions.pip_open())
        self.assertEqual(self.budget.holders("Harbor IPTV"), ["pip"])
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_a_full_providers_channel_is_refused_whatever_the_browsed_one_allows(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 2}
        self.budget.request("Harbor IPTV", 1, purpose="main")  # Harbor's only stream is taken
        self.controller.open_pip(HARBOR_TWO, group_key="10", channels=[HARBOR_TWO])
        QTest.qWait(20)
        self.assertIn(("Your provider allows 1 stream at a time", "error"), self.toast.messages())
        self.assertEqual(self.catalog.links, [])  # no link was asked for
        self.assertEqual(self.pip_live.calls, [])


class PreviousChannelSourceTests(PipCase):
    def test_previous_into_the_pip_after_browsing_keeps_the_list_it_was_picked_from(self):
        self.catalog.names.append("Harbor IPTV")
        self.catalog.limits_of["Harbor IPTV"] = 2
        harbor = [{**c, "_provider": "Harbor IPTV"} for c in CHANNELS]
        self.catalog.select_provider("Harbor IPTV")
        live = self.controller.live
        for channel in harbor[:2]:  # One, then Two on the large picture
            count, running = len(live.named("start")), live.running
            self.controller.tune(channel, group_key="10", channels=harbor)
            self.ready(f"http://stream/{channel['id']}")
            if running:
                live.released.emit()
            self.assertTrue(wait_until(lambda: len(live.named("start")) == count + 1))
            live.play_pipeline()
        self.catalog.select_provider("Home")  # browsing elsewhere
        self.controller.handle(Action.PIP)  # One, small
        wanted = self.controller._pip._wanted[0]
        self.assertEqual((wanted.item["name"], wanted.group_key), ("One", "10"))
        self.assertEqual(wanted.source, "Harbor IPTV")


class OneStreamPipTests(PipCase):
    """A provider allowing one stream: the small picture holds it while the large one is idle
    (its channel failed). What the large picture opens next waits for the PiP's release."""

    def setUp(self):
        super().setUp()
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 1}

    def test_a_movie_opens_only_once_the_closing_pip_gave_back_the_only_slot(self):
        self.open_small(CHANNELS[1])
        self.assertEqual(self.budget.holders("Home"), ["pip"])
        self.controller.play_vod(MOVIE, "movie", play_context())  # closes the PiP first
        self.ready("http://vod/heat.mkv")
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.budget.holders("Home"), ["pip"])  # its upstream is still open
        self.assertEqual(self.session.plays, [])
        self.pip_live.released.emit()
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.assertEqual([url for url, _ in self.session.plays], ["http://vod/heat.mkv"])

    def test_a_direct_channel_waits_for_the_closing_pip_too(self):
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.PIP)  # P closes it
        self.tune(2)
        self.ready("rtmp://tv/3")  # the pipeline reads http(s) only: VLC plays it direct
        self.assertEqual(self.budget.holders("Home"), ["pip"])
        self.assertEqual(self.session.plays, [])
        self.pip_live.released.emit()
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.assertEqual([url for url, _ in self.session.plays], ["rtmp://tv/3"])

    def test_a_direct_channel_is_refused_while_the_small_picture_holds_the_only_stream(self):
        self.open_small(CHANNELS[1])
        self.tune(2)
        self.ready("rtmp://tv/3")  # played directly: the same limit as a time-shifted tune
        self.assertEqual(
            self.toast.messages()[-1], ("Your provider allows 1 stream at a time", "error")
        )
        self.assertEqual(self.session.plays, [])
        self.assertEqual(self.budget.holders("Home"), ["pip"])
        self.assertTrue(self.sessions.pip_open())  # the small picture plays on


class MixerTests(PipCase):
    """The volume keys on the mixer: a row per picture."""

    def setUp(self):
        self.mixer = FakeMixer()
        super().setUp()
        self.config.pip_volume = 30

    def test_the_pips_level_comes_from_the_config_at_startup(self):
        self.assertEqual(self.sessions.level("pip"), 30)
        self.config.pip_volume = 45
        controller = self.make_controller(self.column, sessions=self.sessions)
        controller.start()
        self.assertEqual(self.sessions.level("pip"), 45)

    def test_a_volume_key_without_pip_shows_one_row(self):
        self.start_large(0)
        self.controller.handle(Action.VOL_DOWN)
        self.assertEqual(self.mixer.shown(), ([("Main", "One", 95, False)], 0))
        self.assertEqual(self.sessions.level("main"), 95)
        self.assertEqual(self.session.level, 95)
        self.assertEqual(self.toast.messages(), [])  # the mixer says it

    def test_with_pip_it_shows_two_rows_and_the_keys_act_on_the_selected_one(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.VOL_DOWN)  # the large picture's row first
        self.assertEqual(
            self.mixer.shown(), ([("Main", "One", 95, False), ("PiP", "Two", 30, True)], 0)
        )
        self.controller.handle(Action.SEEK_FWD)  # → the PiP's row (not a seek)
        self.assertEqual(self.mixer.selected, 1)
        self.assertEqual(self.live.named("seek_relative"), [])
        self.controller.handle(Action.CH_PREV, key=UP)  # ↑: louder, and audible now
        self.assertEqual(self.mixer.shown()[0][1], ("PiP", "Two", 35, False))
        self.assertFalse(self.pip_session.muted)
        self.assertEqual(self.pip_session.level, 35)
        self.assertTrue(self.label.audible)
        self.controller.handle(Action.CH_NEXT, key=DOWN)  # ↓ (not a zap)
        self.controller.handle(Action.CH_NEXT, key=DOWN)
        self.assertEqual(self.sessions.level("pip"), 25)
        self.assertEqual(self.requested(), ["One", "Two"])
        self.controller.handle(Action.TAB)  # back to Main
        self.assertEqual(self.mixer.selected, 0)
        self.controller.handle(Action.MUTE)  # M mutes the selected row
        self.assertTrue(self.sessions.muted("main"))
        self.assertFalse(self.sessions.muted("pip"))
        self.controller.handle(Action.ESCAPE)  # hides it first
        self.assertFalse(self.mixer.visible)
        self.assertEqual(self.window.calls, [])

    def test_m_mutes_the_large_picture_and_shows_the_mixer(self):
        self.start_large(0)
        self.controller.handle(Action.MUTE)
        self.assertTrue(self.sessions.muted("main"))
        self.assertTrue(self.session.muted)
        self.assertEqual(self.mixer.shown(), ([("Main", "One", 100, True)], 0))
        self.mixer.dismiss()
        self.controller.handle(Action.MUTE)
        self.assertFalse(self.sessions.muted("main"))

    def test_a_swap_keeps_each_rows_level_with_its_role(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.VOL_DOWN)
        self.mixer.levelRequested.emit(0, 80)
        self.mixer.levelRequested.emit(1, 20)  # lower: the PiP stays muted
        self.assertEqual(
            self.mixer.shown(), ([("Main", "One", 80, False), ("PiP", "Two", 20, True)], 1)
        )
        self.mixer.levelRequested.emit(1, 40)  # raised: audible
        self.assertEqual(self.mixer.shown()[0][1], ("PiP", "Two", 40, False))
        self.controller.handle(Action.SWAP)  # as loud as before: the levels are the roles'
        self.assertEqual(
            self.mixer.shown()[0], [("Main", "Two", 80, False), ("PiP", "One", 40, False)]
        )
        self.assertEqual((self.session.level, self.pip_session.level), (40, 80))

    def open_pip_row(self):
        """Show the mixer on the PiP's row (+ at full volume changes nothing, then Tab)."""
        self.controller.handle(Action.VOL_UP)
        self.controller.handle(Action.TAB)

    def test_the_labels_speaker_mutes_and_unmutes_the_small_picture(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.assertFalse(self.label.audible)  # it opens muted
        self.label.muteRequested.emit()
        self.assertFalse(self.sessions.muted("pip"))
        self.assertFalse(self.pip_session.muted)
        self.assertTrue(self.label.audible)
        self.assertFalse(self.mixer.visible)  # a click on the speaker opens nothing
        self.assertFalse(self.sessions.muted("main"))
        self.open_pip_row()
        self.label.muteRequested.emit()  # the mixer, showing, follows
        self.assertTrue(self.sessions.muted("pip"))
        self.assertTrue(self.pip_session.muted)
        self.assertFalse(self.label.audible)
        self.assertEqual(self.mixer.shown()[0][1], ("PiP", "Two", 30, True))
        self.assertEqual((self.sessions.level("pip"), self.config.saves), (30, 0))

    def test_after_a_swap_the_speaker_still_mutes_the_small_picture(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.SWAP)  # the "main" player is small now
        self.label.muteRequested.emit()
        self.assertFalse(self.sessions.muted("main"))
        self.assertTrue(self.label.audible)
        self.assertFalse(self.sessions.muted("pip"))  # the large picture is heard too

    def test_the_transports_speaker_shows_the_mixer_on_the_main_row(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.transport.volumeRequested.emit()
        self.assertEqual(
            self.mixer.shown(), ([("Main", "One", 100, False), ("PiP", "Two", 30, True)], 0)
        )
        self.controller.handle(Action.CH_NEXT, key=DOWN)  # its keys: ↓ is quieter now
        self.assertEqual(self.sessions.level("main"), 95)

    def test_the_transports_speaker_shows_whether_the_large_picture_is_heard(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.MUTE)
        self.assertEqual(self.transport.named("set_muted")[-1], (True,))
        self.label.muteRequested.emit()  # the small picture is heard: a swap keeps mutes
        self.controller.handle(Action.SWAP)
        self.assertEqual(self.transport.named("set_muted")[-1], (False,))  # Two, heard

    def test_the_speakers_show_each_picture_s_level(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.assertEqual(self.label.level, 30)  # the small picture's
        self.controller.handle(Action.VOL_DOWN)  # the large picture's 100 → 95
        self.assertEqual(self.transport.named("set_level")[-1], (95,))
        self.controller.handle(Action.SWAP)  # the levels stay with the roles
        self.assertEqual(self.transport.named("set_level")[-1], (95,))
        self.assertEqual(self.label.level, 30)

    def test_raising_the_pip_row_from_zero_unmutes_the_small_picture(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.open_pip_row()
        self.assertEqual(self.mixer.selected, 1)
        self.mixer.levelRequested.emit(1, 0)
        self.assertTrue(self.sessions.muted("pip"))
        self.controller.handle(Action.CH_PREV, key=UP)
        self.assertEqual(self.sessions.level("pip"), 5)
        self.assertFalse(self.sessions.muted("pip"))
        self.assertFalse(self.pip_session.muted)

    def test_a_click_on_a_speaker_toggles_that_rows_mute(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.VOL_UP)
        self.mixer.muteRequested.emit(1)
        self.assertFalse(self.sessions.muted("pip"))
        self.mixer.muteRequested.emit(1)
        self.assertTrue(self.sessions.muted("pip"))
        self.mixer.muteRequested.emit(0)
        self.assertTrue(self.sessions.muted("main"))

    def test_the_pips_level_is_remembered_once_the_mixer_hides(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.open_pip_row()
        self.controller.handle(Action.CH_PREV, key=UP)
        self.assertEqual(self.config.saves, 0)
        self.mixer.dismiss()  # 3 s after the last change
        self.assertEqual((self.config.pip_volume, self.config.saves), (35, 1))
        self.mixer.dismiss()
        self.assertEqual(self.config.saves, 1)

    def test_the_wheel_during_a_movie_shows_the_mixer(self):
        self.controller.play_vod(MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.controller.wheel(1, NO_MODS)
        self.assertEqual(self.mixer.shown(), ([("Main", "Heat", 95, False)], 0))

    def test_with_one_row_left_and_right_still_seek(self):
        # ←/→ switch rows only when there are two.
        self.controller.play_vod(MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.session.seekable = True
        self.controller.handle(Action.CH_PREV, key=UP)  # ↑ in a movie: louder, the mixer
        self.assertTrue(self.mixer.visible)
        self.controller.handle(Action.SEEK_FWD)
        self.assertEqual(self.session.relative, [10_000])

    def test_channel_keys_zap_while_the_mixer_shows(self):
        # PageUp/PageDown and the remote's Ch± are never the mixer's arrows.
        self.start_large(0)
        self.controller.handle(Action.VOL_DOWN)
        self.assertTrue(self.mixer.visible)
        self.controller.handle(Action.CH_NEXT, key=Qt.Key.Key_PageDown.value)
        self.assertEqual(self.requested()[-1], "Two")
        self.assertEqual(self.sessions.level("main"), 95)

    def test_a_swap_and_a_main_row_change_within_the_delay_do_not_replace_the_remembered_level(
        self,
    ):
        # The level was read when the mixer hid, by which time the "pip" player
        # could be large and changed from the Main row.
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.open_pip_row()
        self.controller.handle(Action.CH_PREV, key=UP)  # the small picture: 30 -> 35
        self.controller.handle(Action.SWAP)  # S: the "pip" player is large
        self.mixer.levelRequested.emit(0, 30)  # the Main row: that same player, 35 -> 30
        self.assertEqual(self.sessions.level("pip"), 30)
        self.mixer.dismiss()  # within the 3 s
        self.assertEqual((self.config.pip_volume, self.config.saves), (35, 1))

    def test_the_main_and_the_pip_levels_are_remembered_by_role(self):
        # volume is the large picture's level and pip_volume the small one's, whichever
        # player has the role.
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.SWAP)  # the "pip" player is large now
        self.controller.handle(Action.VOL_DOWN)  # Main: 100 -> 95
        self.mixer.dismiss()
        self.assertEqual(
            (self.config.volume, self.config.pip_volume, self.config.saves), (95, 30, 1)
        )
        self.controller.handle(Action.SWAP)
        self.assertEqual(self.sessions.level("main"), 95)  # large again, as loud
        self.open_pip_row()
        self.controller.handle(Action.CH_PREV, key=UP)
        self.mixer.dismiss()
        self.assertEqual(self.config.saves, 2)
        self.assertEqual(self.config.pip_volume, self.sessions.level("pip"))

    def test_the_main_level_comes_back_at_startup(self):
        self.config.volume = 45
        self.config.pip_volume = 12
        controller = self.make_controller(self.column, sessions=self.sessions)
        controller.start()
        self.assertEqual((self.sessions.level("main"), self.sessions.level("pip")), (45, 12))
        self.assertEqual(self.session.level, 45)
        self.assertEqual(self.transport.named("set_level")[-1], (45,))  # its speaker too
