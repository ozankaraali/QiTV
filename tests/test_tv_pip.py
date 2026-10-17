"""Picture in picture and the volume mixer in the TV shell's controller (Phase 4 Tasks 3, 3b).

The controller's fakes are tests/test_tv_controller.py's; here the two players are fake
sessions and live sessions with real LiveTuners, under the real SessionManager and budget.
"""

import os
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu

from services.connection_budget import ConnectionBudget
from services.live.tuning import channel_key
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
from ui.keymap import Action
from ui.live_tuner import LiveTuner, one_disk_ring, wait_for_each_other
from ui.pip_controls import (
    ALREADY_LARGE,
    NO_PREVIOUS,
    OPEN_PIP_FIRST,
    PIP_EXTERNAL,
    PIP_LIVE_ONLY,
    SWITCHING,
    SWITCHING_MS,
)

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

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False


class FakeMixer(FakeOverlay):
    levelRequested = Signal(int, int)
    muteRequested = Signal(int)
    rowSelected = Signal(int)
    closed = Signal()
    rows = ()
    selected = 0

    def show_rows(self, rows, selected):
        self.record("show_rows", rows, selected)
        self.rows, self.selected = list(rows), selected
        self.visible = True

    def dismiss(self):
        if self.visible:
            self.visible = False
            self.closed.emit()

    def shown(self):
        """(title, channel, level, muted) per row, and the selected row."""
        return [(r.title, r.channel, r.level, r.muted) for r in self.rows], self.selected


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
        # Ruling (M5): never a second disk ring; the small picture's is the memory per stream.
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
        # Review I4: Recent and "start with the last channel" follow the large picture.
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
        self.assertEqual(self.budget.holders("Home"), ["main"])  # Five's, as the large one
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

    def test_another_provider_closes_the_small_picture(self):
        self.catalog.providerReady.emit("Home")
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.catalog.providerReady.emit("Home")  # the same provider again: it stays open
        self.assertTrue(self.sessions.pip_open())
        self.catalog.provider = "Backup"
        self.catalog.providerReady.emit("Backup")
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.calls[-1], ("stop",))

    def test_an_external_play_closes_the_small_picture_first(self):
        # Review M6: external players are outside the budget; the PiP gives its slot back.
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.config.play_in_vlc = True
        self.play(2)
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.calls[-1], ("stop",))

    def test_with_an_external_player_picture_in_picture_says_no(self):
        # Review M4: the external player holds the provider's stream; a PiP would be a second.
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
        # Review M7: an STB portal mints a link per request; its one stream is taken.
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
        # Review I1: a setup drops every link request; the PiP's must not stay stuck.
        self.catalog.providerReady.emit("Home")
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)
        self.assertEqual(self.requested()[-1], "One")
        links = len(self.catalog.links)
        self.catalog.providerReady.emit("Home")  # a refresh: the same provider
        self.assertEqual(len(self.catalog.links), links + 1)  # asked again
        self.assertEqual(self.requested()[-1], "One")
        self.ready("http://stream/1")
        self.assertTrue(wait_until(self.sessions.pip_open))

    def test_after_another_provider_pip_still_opens(self):
        self.catalog.providerReady.emit("Home")
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)  # its link is on its way…
        self.catalog.provider = "Backup"
        self.catalog.providerReady.emit("Backup")  # …and dropped with the old provider
        links = len(self.catalog.links)
        self.assertEqual(len(self.catalog.links), links)  # the old channel is not asked again
        self.start_large(2)
        self.controller.open_pip(CHANNELS[3], group_key="10", channels=CHANNELS)
        self.assertEqual(self.requested()[-1], "Four")
        self.ready("http://stream/4")
        self.assertTrue(wait_until(self.sessions.pip_open))

    def test_a_provider_failure_forgets_the_pips_link_and_p_works_again(self):
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)
        self.catalog.providerFailed.emit("Home", "Portal down")
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
        self.assertEqual(self.toast.messages()[-1], (SWITCHING, "info"))  # review M1
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
        self.open_small(CHANNELS[1])
        before = self.counts()
        self.controller.select_provider("Backup")
        self.catalog.provider = "Backup"
        self.catalog.providerReady.emit("Backup")
        self.assertTrue(self.sessions.pip_open())
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.zap.context.group_key, "10")
        self.controller.handle(Action.CH_NEXT)  # the zap list is still the playing provider's
        self.assertEqual(self.requested()[-1], "Three")

    def test_a_link_on_its_way_still_arrives_after_browsing(self):
        self.controller.tune(HOME_ONE, group_key="10", channels=[HOME_ONE])
        self.controller.select_provider("Backup")
        self.catalog.provider = "Backup"
        self.catalog.providerReady.emit("Backup")
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

    def test_a_movie_of_another_provider_keeps_the_channel_small_whatever_its_limit(self):
        self.start_large(0)  # Home, two streams
        self.play_from_library({**MOVIE, "_provider": "Harbor IPTV"})
        self.assertTrue(self.sessions.pip_open())
        self.assertEqual(self.budget.holders("Home"), ["pip"])
        self.assertEqual(self.budget.holders("Harbor IPTV"), ["main"])

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


class PipLinkSettlingTests(PipCase):
    """The small picture's link settles with its own channel's provider."""

    def setUp(self):
        super().setUp()
        self.start_large(0)
        self.home_two = {**CHANNELS[1], "_provider": "Home"}

    def test_a_browsed_providers_failure_keeps_the_small_pictures_link(self):
        self.controller.open_pip(self.home_two, group_key="10", channels=[self.home_two])
        self.catalog.provider = "Backup"
        self.catalog.providerFailed.emit("Backup", "down")
        self.ready("http://stream/2")
        self.assertTrue(wait_until(lambda: self.pip_live.named("start")))
        self.assertTrue(self.sessions.pip_open())

    def test_its_own_providers_failure_forgets_the_small_picture(self):
        self.controller.open_pip(self.home_two, group_key="10", channels=[self.home_two])
        self.catalog.providerFailed.emit("Home", "down")
        self.ready("http://stream/2")  # a late result is ignored
        QTest.qWait(20)
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.named("start"), [])

    def test_its_own_providers_setup_asks_the_link_again(self):
        self.controller.open_pip(self.home_two, group_key="10", channels=[self.home_two])
        count = len(self.catalog.links)
        self.catalog.providerReady.emit("Harbor IPTV")  # another provider's: untouched
        self.assertEqual(len(self.catalog.links), count)
        self.catalog.providerReady.emit("Home")
        self.assertEqual(len(self.catalog.links), count + 1)
        self.ready("http://stream/2")
        self.assertTrue(wait_until(lambda: self.pip_live.named("start")))


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


class MixerTests(PipCase):
    """The volume keys on the mixer: a row per picture (Phase 4 Task 3b)."""

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

    def test_a_swap_moves_each_level_with_its_stream(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.VOL_DOWN)
        self.mixer.levelRequested.emit(0, 80)
        self.mixer.levelRequested.emit(1, 20)  # lower: the PiP stays muted (review M3)
        self.assertEqual(
            self.mixer.shown(), ([("Main", "One", 80, False), ("PiP", "Two", 20, True)], 1)
        )
        self.mixer.levelRequested.emit(1, 40)  # raised: audible
        self.assertEqual(self.mixer.shown()[0][1], ("PiP", "Two", 40, False))
        self.controller.handle(Action.SWAP)
        self.assertEqual(
            self.mixer.shown()[0], [("Main", "Two", 40, False), ("PiP", "One", 80, False)]
        )
        self.assertEqual((self.session.level, self.pip_session.level), (80, 40))

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
        # Review M2: ←/→ switch rows only when there are two.
        self.controller.play_vod(MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.session.seekable = True
        self.controller.handle(Action.CH_PREV, key=UP)  # ↑ in a movie: louder, the mixer
        self.assertTrue(self.mixer.visible)
        self.controller.handle(Action.SEEK_FWD)
        self.assertEqual(self.session.relative, [10_000])

    def test_channel_keys_zap_while_the_mixer_shows(self):
        # Review M2: PageUp/PageDown and the remote's Ch± are never the mixer's arrows.
        self.start_large(0)
        self.controller.handle(Action.VOL_DOWN)
        self.assertTrue(self.mixer.visible)
        self.controller.handle(Action.CH_NEXT, key=Qt.Key.Key_PageDown.value)
        self.assertEqual(self.requested()[-1], "Two")
        self.assertEqual(self.sessions.level("main"), 95)

    def test_a_swap_and_a_main_row_change_within_the_delay_do_not_replace_the_remembered_level(
        self,
    ):
        # Review M7: the level was read when the mixer hid, by which time the "pip" player
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

    def test_the_pip_level_is_remembered_only_while_the_pip_player_is_small(self):
        # Review M4 (ruling): pip_volume is the small picture's level.
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.SWAP)  # the "pip" player is large now
        self.controller.handle(Action.VOL_DOWN)  # Main: the "pip" player's level
        self.mixer.dismiss()
        self.assertEqual((self.config.pip_volume, self.config.saves), (30, 0))
        self.controller.handle(Action.SWAP)
        self.open_pip_row()
        self.controller.handle(Action.CH_PREV, key=UP)
        self.mixer.dismiss()
        self.assertEqual(self.config.saves, 1)
        self.assertEqual(self.config.pip_volume, self.sessions.level("pip"))
