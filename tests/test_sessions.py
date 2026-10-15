"""The two-slot session manager: picture in picture and swap (Phase 4 Task 1)."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from timer_fixtures import elapse

from services.budgeted_stream import RELEASE_CAP_MS, BudgetedStream
from services.connection_budget import ConnectionBudget
from services.live.source import STREAM_LIMIT, STREAM_REFUSED
from services.sessions import PIP_FAILED, PIP_LEVEL, SessionManager, Slot, limit_message

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

    def __init__(self, name, log):
        super().__init__()
        self.name, self.log = name, log
        self.auto_release = True  # False: the test emits `released` itself
        self.fail_start = False
        self.fail_inside_start = ""  # a failure LiveSession reports before start() returns

    def start(self, url, **kwargs):
        if self.fail_start:
            raise RuntimeError("could not open http://user:secret@provider.test/2.ts")
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

    def __init__(self, live, budget, cap_ms):
        super().__init__()
        self.stream = BudgetedStream(live, budget, "main", cap_ms=cap_ms, parent=self)
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
        self.assertEqual(self.log, [])

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
        self.assertEqual(self.calls("main"), [])

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
        self.assertEqual(self.budget.holders(PROVIDER), ["main"])  # the large picture's
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
    """The PiP is whichever slot is small: its channel changes and closes (owner's ruling)."""

    def swapped(self):
        self.run_main(2)
        self.open(B)
        self.pump()
        self.assertTrue(self.manager.swap())

    def test_a_swap_hands_each_ticket_to_its_new_role(self):
        self.run_main(2)
        self.open(B)
        self.pump()
        main_ticket, pip_ticket = self.main_tuner.stream.ticket, self.pip.ticket
        self.manager.swap()
        self.assertEqual(self.budget._active[pip_ticket][1], "main")  # the large picture's
        self.assertEqual(self.budget._active[main_ticket][1], "pip")
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
    """Per-stream volume levels for the mixer (owner amendment)."""

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

    def test_a_swap_carries_each_level_with_its_stream(self):
        self.open_granted(B)
        self.manager.set_level("main", 80)
        self.manager.set_level("pip", 20)
        before = len(self.log)
        self.manager.swap()
        self.assertEqual((self.manager.level("main"), self.manager.level("pip")), (80, 20))
        self.assertFalse([e for e in self.log[before:] if e[2] == "set_volume"])


class AudibleSmallTests(_Fixture):
    """The user made the small slot audible from the mixer: a swap keeps both audible."""

    def test_a_small_slot_raised_above_zero_stays_audible_across_swaps(self):
        self.open_granted(B)
        self.manager.set_level("pip", 40)
        before = len(self.log)
        self.assertTrue(self.manager.swap())
        self.assertIs(self.manager.large(), self.pip)
        self.assertEqual(self.log[before:], [("manager", "large", "pip")])
        self.assertEqual((self.manager.muted("main"), self.manager.muted("pip")), (False, False))
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
        self.assertEqual(self.budget.holders(PROVIDER), ["main"])

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


if __name__ == "__main__":
    unittest.main()
