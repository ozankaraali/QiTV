"""The two-slot session manager: picture in picture and swap."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from timer_fixtures import elapse

from services.budgeted_stream import RELEASE_CAP_MS, BudgetedStream
from services.connection_budget import ConnectionBudget
from services.live.source import STREAM_LIMIT, STREAM_REFUSED
from services.sessions import MAIN_LEVEL, PIP_FAILED, PIP_LEVEL, SessionManager, Slot, limit_message

app = QApplication.instance() or QApplication([])

PROVIDER = "Provider"
A = {"id": "1", "name": "One", "cmd": "http://provider.test/1.ts"}
B = {"id": "2", "name": "Two", "cmd": "http://provider.test/2.ts"}
C = {"id": "3", "name": "Three", "cmd": "http://provider.test/3.ts"}


def provider(limit):
    return {"name": PROVIDER, "type": "M3U", "max_streams": limit}


def start_kwargs(channel):
    return {
        "url": channel["cmd"],
        "headers": {"User-Agent": "QiTV"},
        "verify_ssl": True,
        "latency": "normal",
        "user_agent": "QiTV",
        "disk": False,
        "max_bytes": 128 * 1024 * 1024,
    }


class FakePlayback:
    """A slot's player: records (slot, "player", call, *args) in the shared log."""

    def __init__(self, name, log):
        self.name, self.log = name, log

    def _record(self, *call):
        self.log.append((self.name, "player", *call))

    def play(self, url, **kwargs):
        self._record("play", url)

    def stop(self):
        self._record("stop")

    def set_muted(self, muted):
        self._record("set_muted", muted)

    def set_volume(self, value):
        self._record("set_volume", value)


class FakeLive(QObject):
    """A slot's live session: records (slot, "live", call, *args); `released` on stop."""

    released = Signal()
    timelineChanged = Signal(dict)
    modeChanged = Signal(str)
    failed = Signal(str)
    recordingChanged = Signal(bool)  # 2.2

    def __init__(self, name, log):
        super().__init__()
        self.name, self.log = name, log
        self.auto_release = True  # False: the test emits `released` itself
        self.fail_start = False
        self.fail_inside_start = ""  # a failure LiveSession reports before start() returns

    def start(self, url, **kwargs):
        if self.fail_start:
            raise RuntimeError("could not open http://user:secret@provider.test/2.ts")
        self.kwargs = kwargs  # what it started with besides its URL
        self.log.append((self.name, "live", "start", url))
        if self.fail_inside_start:
            self.failed.emit(self.fail_inside_start)  # e.g. direct play without libVLC

    def stop(self):
        self.log.append((self.name, "live", "stop"))
        if self.auto_release:
            self.released.emit()


class FakeTuner(QObject):
    """The main player's LiveTuner as the manager sees it: its stream, stop and release.

    `holding` stands for a direct play libVLC has not let go of yet (`let_go()` ends it).
    """

    released = Signal()

    def __init__(self, live, budget, cap_ms, purpose="main"):
        super().__init__()
        self.stream = BudgetedStream(live, budget, purpose, cap_ms=cap_ms, parent=self)
        self.stream.released.connect(self.released)
        self.holding = False
        self.stops = 0

    def stop(self):
        self.stops += 1
        self.stream.stop()

    def releasing(self):
        return self.holding or self.stream.waiting()

    def let_go(self):
        self.holding = False
        self.released.emit()

    def set_purpose(self, purpose):
        self.stream.set_purpose(purpose)


class _Fixture(unittest.TestCase):
    cap_ms = RELEASE_CAP_MS

    def setUp(self):
        self.log = []
        self.budget = ConnectionBudget()
        self.granted = []
        self.budget.granted.connect(self.granted.append)
        main_live = FakeLive("main", self.log)
        self.main_tuner = FakeTuner(main_live, self.budget, self.cap_ms)
        self.main = Slot("main", FakePlayback("main", self.log), main_live, tuner=self.main_tuner)
        self.pip = Slot("pip", FakePlayback("pip", self.log), FakeLive("pip", self.log))
        self.manager = SessionManager(self.main, self.pip, self.budget, release_cap_ms=self.cap_ms)
        self.denied, self.failures, self.timelines = [], [], []
        self.manager.denied.connect(self.denied.append)
        self.manager.pipFailed.connect(self.failures.append)
        self.manager.largeChanged.connect(lambda name: self.log.append(("manager", "large", name)))
        self.manager.pipStateChanged.connect(lambda on: self.log.append(("manager", "pip", on)))
        self.manager.slotTimeline.connect(lambda name, state: self.timelines.append((name, state)))

    def pump(self):
        app.processEvents()

    def hold_main(self, limit):
        """The main stream's ticket, as LiveTuner would hold it."""
        ticket = self.budget.request(PROVIDER, limit, purpose="main")
        self.pump()
        return ticket

    def run_main(self, limit=2, channel=A):
        """The main stream, tuned as LiveTuner tunes it (purpose "main")."""
        stream = self.main_tuner.stream
        stream.request(PROVIDER, limit)
        self.pump()
        stream.start(**start_kwargs(channel))
        self.main.channel = channel
        self.assertTrue(stream.running())

    def open(self, channel, limit=2):
        self.manager.open_pip(
            channel, provider=provider(limit), xtream_max=None, start_kwargs=start_kwargs(channel)
        )

    def open_granted(self, channel, limit=2):
        self.hold_main(limit)
        self.open(channel, limit)
        self.pump()

    def calls(self, slot, part=None):
        return [
            e[1:] if part is None else e[2:]
            for e in self.log
            if e[0] == slot and part in (None, e[1])
        ]

    def last_mute(self, slot):
        """The player's mute as libVLC has it: the last set_muted, else unmuted (its default)."""
        mutes = [call[1] for call in self.calls(slot, "player") if call[0] == "set_muted"]
        return mutes[-1] if mutes else False

    def pip_states(self):
        return [e[2] for e in self.log if e[:2] == ("manager", "pip")]


class SessionManagerTests(_Fixture):
    def test_main_starts_large_with_pip_closed(self):
        self.assertIs(self.manager.large(), self.main)
        self.assertIsNone(self.manager.small())
        self.assertFalse(self.manager.pip_open())
        self.assertFalse(self.manager.swap())
        # Only the main player is given its wanted level (see the next test).
        self.assertEqual(self.log, [("main", "player", "set_volume", MAIN_LEVEL)])

    def test_the_main_player_is_given_its_level_so_it_never_inherits_the_pips(self):
        # VLC 3 starts every new audio output at the last level any player set
        # (volume-save). A player with no wanted level took the PiP's 30 on its next media.
        # With a wanted level PlaybackSession applies it again on every media it opens.
        self.assertEqual(self.calls("main", "player"), [("set_volume", MAIN_LEVEL)])
        self.assertEqual(self.manager.level("main"), MAIN_LEVEL)

    def test_opening_starts_the_pip_live_session_muted_once_granted(self):
        self.hold_main(2)
        self.open(B)
        self.assertEqual(self.calls("pip"), [])  # the budget grants later
        self.pump()
        self.assertEqual(
            self.calls("pip"),
            [
                ("player", "set_volume", PIP_LEVEL),
                ("player", "set_muted", True),
                ("live", "start", B["cmd"]),
            ],
        )
        self.assertEqual(self.pip_states(), [True])
        self.assertTrue(self.manager.pip_open())
        self.assertIs(self.manager.large(), self.main)
        self.assertIs(self.manager.small(), self.pip)
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "pip"])
        self.assertEqual(self.calls("main"), [("player", "set_volume", MAIN_LEVEL)])  # only its own

    def test_a_limit_held_by_main_denies_pip_and_queues_nothing(self):
        ticket = self.hold_main(1)
        self.manager.open_pip(
            B,
            provider={"name": PROVIDER, "type": "STB"},
            xtream_max=None,
            start_kwargs=start_kwargs(B),
        )
        self.pump()
        self.assertEqual(self.denied, [1])
        self.assertEqual(self.failures, [])  # the denial is the toast
        self.assertEqual(self.calls("pip"), [])
        self.assertEqual(self.pip_states(), [])
        self.assertFalse(self.manager.pip_open())
        self.assertIsNone(self.manager.small())
        self.assertIsNone(self.manager.slot_showing(B))
        self.assertEqual(self.budget.holders(PROVIDER), ["main"])
        self.budget.release(ticket)
        self.pump()
        self.assertEqual(self.granted, [ticket])  # no PiP request waited for the freed slot
        self.assertEqual(self.calls("pip"), [])

    def test_swap_exchanges_roles_and_audio_without_restarting(self):
        self.open_granted(B)
        before = len(self.log)
        self.assertTrue(self.manager.swap())
        self.assertIs(self.manager.large(), self.pip)
        self.assertIs(self.manager.small(), self.main)
        changes = self.log[before:]
        self.assertEqual(
            changes,
            [
                ("main", "player", "set_muted", True),
                ("pip", "player", "set_muted", False),
                ("pip", "player", "set_volume", 100),  # the large picture's level
                ("main", "player", "set_volume", 30),  # the PiP's
                ("manager", "large", "pip"),
            ],
        )
        self.assertFalse([e for e in changes if e[2] in ("play", "stop", "start")])

    def test_swapping_twice_restores_the_roles(self):
        self.open_granted(B)
        self.manager.swap()
        self.manager.swap()
        self.assertIs(self.manager.large(), self.main)
        self.assertIs(self.manager.small(), self.pip)
        self.assertEqual([e[2] for e in self.log if e[:2] == ("manager", "large")], ["pip", "main"])
        self.assertEqual((self.last_mute("main"), self.last_mute("pip")), (False, True))
        self.assertEqual((self.manager.muted("main"), self.manager.muted("pip")), (False, True))

    def test_closing_while_swapped_stops_the_small_picture_and_keeps_the_large(self):
        self.run_main(2)
        self.open(B)
        self.pump()
        self.main.live.auto_release = False
        self.manager.swap()
        before = len(self.log)
        self.manager.close_pip()
        self.assertEqual(
            self.log[before:],
            [
                ("main", "player", "set_muted", True),  # silent while it stops: no blip
                ("main", "live", "stop"),
                ("manager", "pip", False),
            ],
        )
        self.assertIs(self.manager.large(), self.pip)  # the picture the user kept
        self.assertIsNone(self.manager.small())
        self.assertIs(self.manager.other(), self.main)
        self.assertFalse(self.manager.pip_open())
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "pip"])  # until released
        self.main.live.released.emit()
        self.assertEqual(self.budget.holders(PROVIDER), ["pip"])  # the large picture's slot
        self.assertIsNone(self.manager.slot_showing(A))
        self.assertIs(self.manager.slot_showing(B), self.pip)

    def test_reopening_with_another_channel_starts_it_only_after_released(self):
        self.pip.live.auto_release = False
        self.open_granted(B)
        before = len(self.log)
        self.open(C)
        self.pump()
        self.assertEqual(
            self.log[before:], [("pip", "player", "set_muted", True), ("pip", "live", "stop")]
        )
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "pip"])  # the old one
        self.pip.live.released.emit()
        self.assertEqual(self.log[before + 2 :], [])  # the new ticket is granted later
        self.pump()
        self.assertEqual(self.calls("pip", "live")[-2:], [("stop",), ("start", C["cmd"])])
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "pip"])
        self.assertEqual(self.pip_states(), [True])  # PiP stays open across the change
        self.assertIs(self.manager.slot_showing(C), self.pip)
        self.assertIsNone(self.manager.slot_showing(B))

    def test_slot_showing_matches_channels_by_identity(self):
        self.main.channel = A
        self.open_granted(B)
        self.assertIs(self.manager.slot_showing(dict(B, name="Two HD")), self.pip)
        self.assertIs(self.manager.slot_showing({"cmd": A["cmd"]}), self.main)
        self.assertIsNone(self.manager.slot_showing(C))

    def test_the_users_mute_stays_with_the_large_picture(self):
        self.open_granted(B)
        self.manager.set_muted("main", True)
        self.manager.swap()
        self.assertEqual((self.last_mute("main"), self.last_mute("pip")), (True, True))
        self.assertEqual((self.manager.muted("main"), self.manager.muted("pip")), (True, True))
        self.manager.swap()
        self.assertEqual((self.manager.muted("main"), self.manager.muted("pip")), (True, True))

    def test_timelines_are_reported_per_slot(self):
        self.main.live.timelineChanged.emit({"lag": 0.0})
        self.pip.live.timelineChanged.emit({"lag": 200.0})
        self.assertEqual(self.timelines, [("main", {"lag": 0.0}), ("pip", {"lag": 200.0})])


class RoleTests(_Fixture):
    """The PiP is whichever slot is small: its channel changes and closes."""

    def swapped(self):
        self.run_main(2)
        self.open(B)
        self.pump()
        self.assertTrue(self.manager.swap())

    def test_each_ticket_keeps_its_slots_name_across_swaps(self):
        # A slot's tickets carry its name, whatever its role: "every stream but mine" is the
        # holders under other names, with two slots or nine.
        self.run_main(2)
        self.open(B)
        self.pump()
        main_ticket, pip_ticket = self.main_tuner.stream.ticket, self.pip.ticket
        self.manager.swap()
        self.assertEqual(self.budget._active[pip_ticket][1], "pip")
        self.assertEqual(self.budget._active[main_ticket][1], "main")
        self.assertEqual(self.budget.in_use(PROVIDER), 2)  # nothing counted twice
        self.manager.swap()
        self.assertEqual(self.budget._active[main_ticket][1], "main")
        self.assertEqual(self.budget._active[pip_ticket][1], "pip")

    def test_open_after_a_swap_changes_the_small_pictures_channel(self):
        self.swapped()
        self.main.live.auto_release = False
        before = len(self.log)
        self.open(C)
        self.assertEqual(
            self.log[before:], [("main", "player", "set_muted", True), ("main", "live", "stop")]
        )
        self.assertTrue(self.manager.pip_open())
        self.main.live.released.emit()
        self.pump()
        self.assertEqual(self.calls("main", "live")[-1], ("start", C["cmd"]))
        self.assertEqual(self.calls("pip", "live"), [("start", B["cmd"])])  # never stopped
        self.assertTrue(self.manager.muted("main"))  # it stays the muted small picture
        self.assertIs(self.manager.slot_showing(C), self.main)
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "pip"])
        self.assertEqual(self.pip_states(), [True])

    def test_reopening_after_a_close_while_swapped_opens_in_the_free_slot(self):
        self.swapped()
        self.manager.close_pip()
        self.open(C)
        self.pump()
        self.assertEqual(self.calls("main", "live")[-1], ("start", C["cmd"]))
        self.assertIs(self.manager.small(), self.main)
        self.assertIs(self.manager.large(), self.pip)
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "pip"])

    def test_a_swap_waits_for_nothing_and_is_refused_while_a_change_is_on_its_way(self):
        self.swapped()
        self.main.live.auto_release = False
        self.open(C)
        self.assertFalse(self.manager.swap())
        self.assertIs(self.manager.large(), self.pip)
        self.main.live.released.emit()
        self.pump()
        self.assertTrue(self.manager.swap())

    def test_the_small_streams_start_is_said_on_open_and_after_a_change(self):
        # The shell's swap back waits for a change on its way to start.
        started = []
        self.manager.pipStarted.connect(lambda: started.append(self.manager.changing()))
        self.swapped()
        self.assertEqual(started, [False])  # it opened
        self.main.live.auto_release = False
        self.open(C)
        self.assertTrue(self.manager.changing())
        self.main.live.released.emit()
        self.pump()
        self.assertEqual(started, [False, False])  # the change started
        self.assertFalse(self.manager.changing())

    def test_a_player_still_letting_go_of_a_direct_play_delays_the_new_channel(self):
        self.swapped()
        self.main_tuner.holding = True  # e.g. a channel the main player played directly
        self.open(C)
        self.pump()
        self.assertNotIn(("start", C["cmd"]), self.calls("main", "live"))
        self.main_tuner.let_go()
        self.pump()
        self.assertEqual(self.calls("main", "live")[-1], ("start", C["cmd"]))

    def test_pip_opening_reports_a_pending_first_open_only(self):
        self.hold_main(2)
        self.open(B)
        self.assertTrue(self.manager.pip_opening())
        self.pump()
        self.assertFalse(self.manager.pip_opening())
        self.open(C)  # a change of an open PiP is not an opening
        self.assertFalse(self.manager.pip_opening())


class LevelTests(_Fixture):
    """Per-stream volume levels for the mixer."""

    def test_pip_starts_muted_at_its_own_level(self):
        self.open_granted(B)
        self.assertEqual((self.manager.level("main"), self.manager.level("pip")), (100, PIP_LEVEL))
        self.assertTrue(self.manager.muted("pip"))
        self.assertFalse(self.manager.muted("main"))

    def test_raising_the_small_slot_above_zero_unmutes_it(self):
        self.open_granted(B)
        self.manager.set_level("pip", 0)
        self.assertTrue(self.manager.muted("pip"))
        self.manager.set_level("pip", 45)
        self.assertFalse(self.manager.muted("pip"))
        self.assertEqual(
            self.calls("pip", "player")[-2:], [("set_volume", 45), ("set_muted", False)]
        )

    def test_lowering_a_muted_small_slot_keeps_it_muted(self):
        # Only raising the PiP's level above 0 makes it audible.
        self.open_granted(B)
        self.manager.set_level("pip", 25)
        self.assertTrue(self.manager.muted("pip"))
        self.manager.set_level("pip", 25)  # the same level: still muted
        self.assertTrue(self.manager.muted("pip"))
        self.manager.set_level("pip", 30)
        self.assertFalse(self.manager.muted("pip"))

    def test_a_level_for_the_large_slot_keeps_the_users_mute(self):
        self.open_granted(B)
        self.manager.set_muted("main", True)
        self.manager.set_level("main", 60)
        self.assertTrue(self.manager.muted("main"))
        self.assertEqual(self.calls("main", "player")[-1], ("set_volume", 60))

    def test_levels_are_clamped(self):
        self.manager.set_level("main", 150)
        self.manager.set_level("pip", -5)
        self.assertEqual((self.manager.level("main"), self.manager.level("pip")), (100, 0))

    def test_the_levels_stay_with_the_roles_across_a_swap(self):
        self.open_granted(B)
        self.manager.set_level("main", 80)
        self.manager.set_level("pip", 20)
        before = len(self.log)
        self.manager.swap()  # the large picture stays as loud, the small one as quiet
        self.assertEqual((self.manager.level("pip"), self.manager.level("main")), (80, 20))
        volumes = [e for e in self.log[before:] if e[2] == "set_volume"]
        self.assertEqual(
            volumes, [("pip", "player", "set_volume", 80), ("main", "player", "set_volume", 20)]
        )
        self.assertEqual(self.manager.role_levels(), (80, 20))

    def test_remembered_levels_go_to_the_roles(self):
        self.open_granted(B)
        self.manager.set_role_levels(main=55, pip=15)
        self.assertEqual((self.manager.level("main"), self.manager.level("pip")), (55, 15))
        self.manager.swap()
        self.assertEqual((self.manager.level("pip"), self.manager.level("main")), (55, 15))


class AudibleSmallTests(_Fixture):
    """The user made the small slot audible from the mixer: a swap keeps both audible."""

    def test_a_small_slot_raised_above_zero_stays_audible_across_swaps(self):
        self.open_granted(B)
        self.manager.set_level("pip", 40)
        before = len(self.log)
        self.assertTrue(self.manager.swap())
        self.assertIs(self.manager.large(), self.pip)
        mutes = [e for e in self.log[before:] if e[2] == "set_muted"]
        self.assertEqual(mutes, [])  # both stay audible
        self.assertEqual((self.manager.muted("main"), self.manager.muted("pip")), (False, False))
        self.assertEqual((self.manager.level("pip"), self.manager.level("main")), (100, 40))
        self.manager.swap()
        self.assertEqual((self.manager.muted("main"), self.manager.muted("pip")), (False, False))
        self.assertEqual((self.last_mute("main"), self.last_mute("pip")), (False, False))
        self.assertEqual((self.manager.level("main"), self.manager.level("pip")), (100, 40))

    def test_a_small_slot_unmuted_explicitly_stays_audible_across_a_swap(self):
        self.open_granted(B)
        self.manager.set_muted("pip", False)
        self.manager.swap()
        self.assertIs(self.manager.large(), self.pip)
        self.assertEqual((self.manager.muted("main"), self.manager.muted("pip")), (False, False))
        self.assertEqual((self.last_mute("main"), self.last_mute("pip")), (False, False))

    def test_muting_the_small_slot_again_lets_audio_follow_the_large_picture(self):
        self.open_granted(B)
        self.manager.set_level("pip", 40)
        self.manager.set_muted("pip", True)
        self.manager.swap()
        self.assertEqual((self.manager.muted("main"), self.manager.muted("pip")), (True, False))

    def test_closing_the_small_picture_leaves_the_large_one_audible(self):
        self.run_main(2)
        self.open(B)
        self.pump()
        self.manager.set_level("pip", 40)
        self.manager.swap()
        self.manager.close_pip()
        self.assertIs(self.manager.large(), self.pip)
        self.assertFalse(self.manager.muted("pip"))
        self.assertFalse(self.last_mute("pip"))
        self.assertTrue(self.last_mute("main"))  # the closed small picture went silent

    def test_an_audible_pip_changing_channel_is_silent_while_it_stops_then_audible(self):
        self.open_granted(B)
        self.manager.set_level("pip", 40)
        before = len(self.log)
        self.open(C)
        self.assertEqual(
            self.log[before:], [("pip", "player", "set_muted", True), ("pip", "live", "stop")]
        )
        self.pump()
        self.assertFalse(self.manager.muted("pip"))
        self.assertFalse(self.last_mute("pip"))
        self.assertEqual(self.calls("pip", "live")[-1], ("start", C["cmd"]))

    def test_a_reopened_pip_starts_muted_at_its_remembered_level(self):
        self.open_granted(B)
        self.manager.set_level("pip", 40)
        self.manager.close_pip()
        self.assertTrue(self.last_mute("pip"))  # muted before it stopped
        self.open(C)
        self.pump()
        self.assertTrue(self.manager.muted("pip"))
        self.assertEqual(
            self.calls("pip", "player")[-2:], [("set_volume", 40), ("set_muted", True)]
        )
        self.manager.swap()  # back to the default: audio follows the large picture
        self.assertEqual((self.manager.muted("main"), self.manager.muted("pip")), (True, False))


class PipFailureTests(_Fixture):
    """The PiP's LiveSession failed for good while it ran: PiP closes and says why."""

    def test_a_provider_refusal_closes_pip_with_the_limit_message(self):
        self.open_granted(B)
        self.pip.live.failed.emit(STREAM_LIMIT)
        self.assertEqual(self.failures, [limit_message(2)])
        self.assertEqual((self.denied, self.pip_states()), ([], [True, False]))
        self.assertIs(self.manager.large(), self.main)
        self.assertEqual(self.calls("pip", "live")[-1], ("stop",))
        self.assertEqual(self.budget.holders(PROVIDER), ["main"])

    def test_after_a_swap_the_small_pictures_failure_closes_pip(self):
        self.run_main(2)
        self.open(B)
        self.pump()
        self.manager.swap()
        self.pip.live.failed.emit(STREAM_LIMIT)  # the large picture's: the shell's to handle
        self.assertEqual((self.failures, self.pip_states()), ([], [True]))
        self.main.live.failed.emit(STREAM_LIMIT)
        self.assertEqual(self.failures, [limit_message(2)])
        self.assertEqual(self.pip_states(), [True, False])
        self.assertIs(self.manager.large(), self.pip)
        self.assertEqual(self.calls("main", "live")[-1], ("stop",))
        self.assertEqual(self.budget.holders(PROVIDER), ["pip"])  # the large one, by its name

    def test_a_soft_refusal_names_the_limit_too(self):
        self.open_granted(B)
        self.pip.live.failed.emit(STREAM_REFUSED)
        self.assertEqual(self.failures, [limit_message(2)])

    def test_a_refusal_with_an_unknown_limit_says_the_provider_limits_streams(self):
        self.open_granted(B, limit=None)
        self.pip.live.failed.emit(STREAM_LIMIT)
        self.assertEqual(self.failures, ["Your provider limits concurrent streams"])

    def test_a_playback_error_closes_pip_with_a_fixed_message(self):
        self.open_granted(B)
        self.pip.live.failed.emit("http://user:secret@provider.test/2.ts: 404")
        self.assertEqual(self.failures, [PIP_FAILED])
        self.assertEqual(self.pip_states(), [True, False])
        self.assertEqual(self.budget.holders(PROVIDER), ["main"])
        # What the stream said, for the shell's STB retry (never shown).
        self.assertEqual(self.manager.failure, "http://user:secret@provider.test/2.ts: 404")

    def test_a_failure_inside_start_leaves_pip_closed(self):
        self.pip.live.fail_inside_start = "Video needs VLC"
        self.open_granted(B)
        self.assertEqual(self.failures, [PIP_FAILED])
        self.assertEqual(self.pip_states(), [])
        self.assertFalse(self.manager.pip_open())
        self.assertEqual(self.budget.holders(PROVIDER), ["main"])

    def test_a_failure_once_pip_is_closed_is_ignored(self):
        self.open_granted(B)
        self.manager.close_pip()
        self.pip.live.failed.emit(STREAM_LIMIT)
        self.assertEqual((self.failures, self.pip_states()), ([], [True, False]))


class PipLifecycleTests(_Fixture):
    def test_a_change_before_the_first_grant_asks_again_and_ignores_the_stale_grant(self):
        self.hold_main(2)
        self.open(B)
        stale = self.pip.ticket
        self.open(C)  # before B's grant: B's ticket goes back, C's is asked for
        self.assertNotEqual(self.pip.ticket, stale)
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "pip"])
        self.budget.granted.emit(stale)
        self.assertEqual(self.calls("pip", "live"), [])
        self.pump()
        self.assertEqual(self.calls("pip", "live"), [("start", C["cmd"])])
        self.assertEqual(self.pip_states(), [True])

    def test_closing_before_the_grant_gives_the_ticket_back(self):
        self.hold_main(2)
        self.open(B)
        self.manager.close_pip()
        self.pump()
        self.assertEqual(self.calls("pip"), [])
        self.assertEqual(self.pip_states(), [])
        self.assertEqual(self.budget.holders(PROVIDER), ["main"])

    def test_reopening_after_a_close_waits_for_the_old_release(self):
        self.pip.live.auto_release = False
        self.open_granted(B)
        self.manager.close_pip()
        self.open(C)
        self.pump()
        self.assertEqual(self.calls("pip", "live"), [("start", B["cmd"]), ("stop",)])
        self.pip.live.released.emit()
        self.pump()
        self.assertEqual(self.calls("pip", "live")[-1], ("start", C["cmd"]))
        self.assertEqual(self.pip_states(), [True, False, True])
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "pip"])

    def test_a_change_refused_after_the_release_closes_pip(self):
        self.pip.live.auto_release = False
        self.open_granted(B)
        self.open(C)
        self.budget.request(PROVIDER, 2, purpose="main")  # a zap waits for the PiP's slot
        self.pip.live.released.emit()
        self.pump()
        self.assertEqual(self.failures, ["Your provider allows 2 streams at a time"])
        self.assertEqual(self.denied, [])  # one toast: the open PiP failed
        self.assertEqual(self.pip_states(), [True, False])
        self.assertIs(self.manager.large(), self.main)
        self.assertNotIn(("start", C["cmd"]), self.calls("pip", "live"))
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "main"])

    def test_a_start_that_raises_gives_the_ticket_back_and_reports_a_fixed_message(self):
        self.pip.live.fail_start = True
        with self.assertLogs("services.sessions", "WARNING") as logs:
            self.open_granted(B)
        self.assertEqual(self.failures, [PIP_FAILED])
        self.assertEqual(self.manager.failure, "")  # not the stream's: nothing to retry
        self.assertNotIn("secret", " ".join(logs.output))
        self.assertEqual(self.pip_states(), [])
        self.assertFalse(self.manager.pip_open())
        self.assertEqual(self.budget.holders(PROVIDER), ["main"])

    def test_a_change_that_fails_to_start_closes_pip_and_reports_it(self):
        self.open_granted(B)
        self.pip.live.fail_start = True
        with self.assertLogs("services.sessions", "WARNING"):
            self.open(C)
            self.pump()
        self.assertEqual(self.failures, [PIP_FAILED])
        self.assertEqual(self.pip_states(), [True, False])
        self.assertIs(self.manager.large(), self.main)
        self.assertEqual(self.budget.holders(PROVIDER), ["main"])

    def test_the_limit_message_counts_streams(self):
        self.assertEqual(limit_message(1), "Your provider allows 1 stream at a time")
        self.assertEqual(limit_message(3), "Your provider allows 3 streams at a time")


class ReleaseCapTests(_Fixture):
    cap_ms = 50

    def closed_past_the_cap(self):
        self.pip.live.auto_release = False
        self.open_granted(B)
        self.manager.close_pip()
        with self.assertLogs("services.budgeted_stream", "WARNING"):
            elapse(self.cap_ms * 2)
        self.assertEqual(self.budget.holders(PROVIDER), ["main"])

    def test_after_the_cap_a_reopen_starts(self):
        self.closed_past_the_cap()
        self.open(C)
        self.pump()
        self.assertEqual(self.calls("pip", "live")[-1], ("start", C["cmd"]))
        self.assertEqual(self.pip_states(), [True, False, True])
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "pip"])

    def test_a_real_released_after_the_cap_is_ignored(self):
        self.closed_past_the_cap()
        self.open(C)
        self.pump()
        before = len(self.log)
        self.pip.live.released.emit()  # B's, late
        self.assertEqual(self.log[before:], [])
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "pip"])
        self.assertTrue(self.manager.pip_open())

    def test_a_release_that_never_comes_gives_the_ticket_back_after_the_cap(self):
        self.pip.live.auto_release = False
        self.open_granted(B)
        self.manager.close_pip()
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "pip"])
        with self.assertLogs("services.budgeted_stream", "WARNING"):
            elapse(self.cap_ms * 2)
        self.assertEqual(self.budget.holders(PROVIDER), ["main"])


class _Tiles(_Fixture):
    """Two more slots, "t2" and "t3" (a multiview's tiles)."""

    def setUp(self):
        super().setUp()
        self.tiles = {}
        for name in ("t2", "t3"):
            live = FakeLive(name, self.log)
            tuner = FakeTuner(live, self.budget, self.cap_ms, purpose=name)
            self.tiles[name] = Slot(name, FakePlayback(name, self.log), live, tuner=tuner)
            self.manager.add_slot(self.tiles[name])
        self.changes = []
        self.manager.largeChanged.connect(lambda new, old: self.changes.append((new, old)))


class OneStreamTests(_Tiles):
    """2.6.2 (final review C2): a PiP's or a tile's live session knows its provider limits
    its streams, so a server part parks its live upstream there too, as the main one's does
    (`LiveSession.start`'s one_stream): the PiP swapped large, or the focused tile."""

    def test_a_pips_live_session_parks_for_a_part_on_a_limited_provider(self):
        self.open_granted(B, limit=2)
        self.assertIs(self.pip.live.kwargs["one_stream"], True)

    def test_a_tiles_live_session_parks_for_a_part_on_a_limited_provider(self):
        for name, limit, parks in (("t2", 3, True), ("t3", None, False)):
            with self.subTest(limit=limit):
                self.manager.open_slot(
                    name, channel=B, provider=PROVIDER, limit=limit, start_kwargs=start_kwargs(B)
                )
                self.pump()
                self.assertIs(self.tiles[name].live.kwargs["one_stream"], parks)


class NamedSlotTests(_Tiles):
    """More slots than two (a multiview's tiles): the large role goes to any of them."""

    def test_slots_are_listed_in_the_order_they_were_made(self):
        names = [slot.name for slot in self.manager.slots()]
        self.assertEqual(names, ["main", "pip", "t2", "t3"])
        self.assertIs(self.manager.slot("t3"), self.tiles["t3"])
        with self.assertRaises(KeyError):
            self.manager.slot("t9")
        with self.assertRaises(ValueError):
            self.manager.add_slot(self.tiles["t2"])

    def test_a_tile_starts_muted_at_the_pips_level(self):
        self.assertEqual((self.manager.muted("t2"), self.manager.level("t2")), (True, PIP_LEVEL))

    def test_set_large_gives_any_slot_the_role_and_says_which_had_it(self):
        self.assertTrue(self.manager.set_large("t2"))
        self.assertIs(self.manager.large(), self.tiles["t2"])
        self.assertIs(self.manager.other(), self.pip)  # the PiP's slot stays the PiP's
        self.assertIsNone(self.manager.small())
        self.assertTrue(self.manager.set_large("t3"))
        self.assertTrue(self.manager.set_large("main"))
        self.assertEqual(self.changes, [("t2", "main"), ("t3", "t2"), ("main", "t3")])
        self.assertFalse(self.manager.set_large("main"))  # it is large already
        self.assertEqual(len(self.changes), 3)

    def test_the_sound_follows_the_large_role_with_the_users_mute(self):
        self.manager.set_large("t2")
        self.assertEqual((self.last_mute("main"), self.last_mute("t2")), (True, False))
        self.manager.set_muted("t2", True)  # the user mutes the large picture
        self.manager.set_large("t3")
        self.assertEqual([self.manager.muted(n) for n in ("main", "t2", "t3")], [True, True, True])
        self.manager.set_muted("t3", False)
        self.manager.set_large("main")
        self.assertEqual([self.manager.muted(n) for n in ("main", "t2", "t3")], [False, True, True])

    def test_a_slot_the_user_made_audible_keeps_both_mutes(self):
        self.manager.set_muted("t2", False)  # heard beside the large picture
        self.manager.set_large("t2")
        self.assertEqual((self.manager.muted("main"), self.manager.muted("t2")), (False, False))

    def test_set_large_onto_the_pips_slot_is_a_swap(self):
        self.open_granted(B)
        self.assertTrue(self.manager.set_large("pip"))
        self.assertIs(self.manager.small(), self.main)
        self.assertEqual(self.changes, [("pip", "main")])
        self.assertTrue(self.manager.swap())
        self.assertEqual(self.changes[-1], ("main", "pip"))

    def test_set_large_waits_for_nothing_while_the_pips_change_is_on_its_way(self):
        self.pip.live.auto_release = False
        self.open_granted(B)
        self.open(C)  # B stops; C waits for its release
        self.assertFalse(self.manager.set_large("t2"))
        self.pip.live.released.emit()
        self.pump()
        self.assertTrue(self.manager.set_large("t2"))

    def test_a_tiles_streams_carry_its_name(self):
        stream = self.tiles["t2"].tuner.stream
        self.hold_main(3)
        stream.request(PROVIDER, 3)
        self.pump()
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "t2"])
        self.manager.set_large("t2")  # roles change, names stay
        self.assertEqual(sorted(self.budget.holders(PROVIDER)), ["main", "t2"])

    def test_a_tiles_failure_is_not_the_pips(self):
        self.open_granted(B)
        stream = self.tiles["t2"].tuner.stream
        stream.request(PROVIDER, 3)
        self.pump()
        stream.start(**start_kwargs(C))
        self.tiles["t2"].live.failed.emit(STREAM_LIMIT)
        self.assertEqual(self.failures, [])
        self.assertTrue(self.manager.pip_open())

    def test_a_tiles_timeline_is_reported_by_its_name(self):
        self.tiles["t3"].live.timelineChanged.emit({"lag": 3.0})
        self.assertEqual(self.timelines, [("t3", {"lag": 3.0})])


class GridMixTests(_Tiles):
    """The grid's sound: it follows the focus until the user mixes it, then stays as mixed
    until "Follow focus" or the grid is left."""

    def setUp(self):
        super().setUp()
        self.mixes = []
        self.manager.mixChanged.connect(self.mixes.append)
        self.manager.enter_grid()

    def mutes(self):
        return [self.manager.muted(name) for name in ("main", "t2", "t3")]

    def test_the_sound_follows_the_focus_until_a_tile_without_it_is_changed(self):
        self.manager.set_level("main", 60)  # the focused tile's own: the main sound
        self.manager.set_muted("main", True)
        self.manager.set_muted("main", False)
        self.assertEqual((self.manager.mixed(), self.mixes), (False, []))
        self.manager.set_large("t2")
        self.assertEqual(self.mutes(), [True, False, True])  # the focus took the sound
        self.assertEqual(self.manager.level("t2"), 60)  # at the main level

    def test_a_mix_keeps_every_tiles_mute_and_level_as_the_focus_moves(self):
        self.manager.set_level("t2", 45)  # a tile without the focus: raised, so heard
        self.assertEqual((self.manager.mixed(), self.mixes), (True, [True]))
        self.assertEqual(self.mutes(), [False, False, True])
        self.manager.set_large("t3")  # the controls move; the sound stays
        self.assertEqual(self.mutes(), [False, False, True])
        levels = [self.manager.level(name) for name in ("main", "t2", "t3")]
        self.assertEqual(levels, [MAIN_LEVEL, 45, PIP_LEVEL])
        self.manager.set_level("t3", 20)  # the focused tile's level is its own in a mix
        self.manager.set_large("main")
        levels = [self.manager.level(name) for name in ("main", "t2", "t3")]
        self.assertEqual(levels, [MAIN_LEVEL, 45, 20])
        self.assertEqual(self.mixes, [True])

    def test_a_tile_focused_untouched_in_a_mix_never_leaves_its_level_as_the_main_one(self):
        self.manager.set_level("main", 80)  # the main sound
        self.manager.set_muted("t2", False)  # a mix
        self.manager.set_large("t3")  # never touched: its own default level
        self.assertEqual(self.manager.level("t3"), PIP_LEVEL)
        self.assertEqual(self.manager.role_levels()[0], 80)  # what is remembered meanwhile
        self.manager.end_mix()
        self.assertEqual((self.manager.level("t3"), self.manager.role_levels()[0]), (80, 80))

    def test_a_level_set_on_the_focused_tile_in_a_mix_stays_as_the_main_one(self):
        self.manager.set_level("main", 80)
        self.manager.set_muted("t2", False)  # a mix
        self.manager.set_large("t3")
        self.manager.set_level("t3", 50)  # what the user hears now
        self.manager.end_mix()
        self.assertEqual((self.manager.level("t3"), self.manager.role_levels()[0]), (50, 50))

    def test_follow_focus_gives_the_main_level_back_too(self):
        self.manager.set_level("main", 70)
        self.manager.set_level("t2", 40)  # a mix
        self.manager.set_large("t2")  # set by the user in the mix: its own level stays
        self.manager.follow_focus()
        self.assertEqual(self.manager.level("t2"), 40)
        self.manager.set_large("main")  # the sound and the main level follow the focus
        self.assertEqual(self.manager.level("main"), 40)

    def test_a_tiles_speaker_mixes_too(self):
        self.manager.set_muted("t3", False)
        self.assertTrue(self.manager.mixed())
        self.manager.set_large("t2")
        self.assertEqual(self.mutes(), [False, True, False])

    def test_follow_focus_hears_the_focused_tile_alone_again(self):
        self.manager.set_muted("t2", False)
        self.manager.set_large("t3")
        self.manager.follow_focus()
        self.assertEqual((self.manager.mixed(), self.mixes), (False, [True, False]))
        self.assertEqual(self.mutes(), [True, True, False])
        self.manager.set_large("main")  # the sound follows the focus again
        self.assertEqual(self.mutes(), [False, True, True])

    def test_leaving_ends_the_mix_with_the_big_picture_heard(self):
        self.manager.set_muted("t2", False)
        self.manager.set_large("t3")  # muted in the mix
        self.manager.end_mix()  # the grid leaves, before its tiles stop
        self.assertEqual(self.mutes(), [False, False, False])  # the big picture heard
        self.assertEqual((self.manager.mixed(), self.mixes), (False, [True, False]))
        self.manager.leave_grid("t2")
        self.assertEqual(self.mixes, [True, False])  # said once
        self.assertFalse(self.manager.muted("t2"))  # the PiP stays heard, as made audible

    def test_a_mix_with_every_tile_muted_leaves_the_big_picture_muted(self):
        self.manager.set_muted("t2", True)  # no change: no mix
        self.assertFalse(self.manager.mixed())
        self.manager.set_level("t2", 10)  # lowered: a mix, still muted
        self.manager.set_muted("main", True)
        self.manager.set_large("t3")
        self.manager.leave_grid(None)
        self.assertEqual(self.mutes(), [True, True, True])
        self.assertFalse(self.manager.mixed())

    def test_the_next_grid_follows_the_focus_again(self):
        self.manager.set_muted("t2", False)
        self.manager.leave_grid(None)
        self.manager.enter_grid()
        self.assertFalse(self.manager.mixed())
        self.manager.set_large("t3")
        self.assertTrue(self.manager.muted("main"))

    def test_outside_the_grid_nothing_mixes(self):
        self.manager.leave_grid(None)
        self.manager.set_muted("t2", False)
        self.assertEqual((self.manager.mixed(), self.mixes), (False, []))


class SetAsideTests(_Fixture):
    """2.6: a recording slot leaves its role and records on in the background; another slot
    (one made for it, else an idle one) takes the role."""

    def setUp(self):
        super().setUp()
        self.made = []

        def make_slot(name):
            live = FakeLive(name, self.log)
            tuner = FakeTuner(live, self.budget, self.cap_ms, purpose=name)
            slot = Slot(name, FakePlayback(name, self.log), live, tuner=tuner)
            self.manager.add_slot(slot)
            self.made.append(name)
            return slot

        self.manager.set_slot_factory(make_slot)
        self.aside, self.back = [], []
        self.manager.setAside.connect(lambda name, fresh: self.aside.append((name, fresh)))
        self.manager.broughtBack.connect(lambda name, old: self.back.append((name, old)))

    def test_a_recording_large_slot_goes_to_the_background(self):
        self.assertEqual(self.manager.set_aside("main"), "b1")
        self.assertEqual(self.made, ["b1"])
        self.assertEqual(self.manager.large().name, "b1")
        self.assertEqual([s.name for s in self.manager.background()], ["main"])
        self.assertEqual(self.main_tuner.stops, 0)  # it streams and records on
        self.assertTrue(self.manager.muted("main"))
        self.assertFalse(self.manager.muted("b1"))  # the user's sound goes with the role
        self.assertEqual(self.manager.other().name, "pip")
        self.assertEqual(self.aside, [("main", "b1")])

    def test_it_comes_back_and_its_stand_in_is_used_again(self):
        self.manager.set_aside("main")
        self.assertEqual(self.manager.bring_back("main"), "b1")
        self.assertEqual(self.manager.large().name, "main")
        self.assertEqual(self.manager.background(), [])
        self.assertFalse(self.manager.muted("main"))
        self.assertEqual(self.back, [("main", "b1")])
        self.assertEqual(self.manager.set_aside("main"), "b1")  # no other slot is made
        self.assertEqual(self.made, ["b1"])

    def test_it_shows_small_without_a_stream(self):
        self.manager.set_aside("main")
        starts = [c for c in self.log if c[1:3] == ("live", "start")]
        self.assertTrue(self.manager.show_small("main"))
        self.assertEqual(self.manager.small().name, "main")
        self.assertTrue(self.manager.pip_open())
        self.assertEqual([c for c in self.log if c[1:3] == ("live", "start")], starts)

    def test_the_end_of_its_recording_lets_it_go(self):
        self.manager.set_aside("main")
        self.main.live.recordingChanged.emit(False)
        self.assertEqual(self.manager.background(), [])
        self.assertEqual(self.main_tuner.stops, 1)  # it leaves its channel as any player does

    def test_not_while_the_grid_shows(self):
        self.manager.enter_grid()
        self.assertIsNone(self.manager.set_aside("main"))


if __name__ == "__main__":
    unittest.main()
