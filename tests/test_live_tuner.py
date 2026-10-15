"""LiveTuner's budget slots: direct channels and movies hold the main slot too (spec §6.9)."""

import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from timer_fixtures import elapse

from services.connection_budget import ConnectionBudget
from ui.live_tuner import LiveStart, LiveTuner, start_kwargs, wait_for_each_other

app = QApplication.instance() or QApplication([])

PROVIDER = "Provider"
URL = "http://provider.example/live/user/secret/42.ts"


class FakeSession(QObject):
    """The player: a stop leaves libVLC's input open while `holding`, until `let_go()`."""

    inputEnded = Signal()

    def __init__(self):
        super().__init__()
        self.holding = False
        self.stops = 0

    def stop(self):
        self.stops += 1

    def holds_input(self):
        return self.holding

    def let_go(self):
        self.holding = False
        self.inputEnded.emit()


class FakeLive(QObject):
    """LiveSession's surface for BudgetedStream: released() follows stop() at once."""

    released = Signal()
    timelineChanged = Signal(dict)

    def __init__(self):
        super().__init__()
        self.starts, self.kwargs = [], []
        self.mode = ""
        self.running = False

    def is_pipeline(self):
        return False

    def start(self, url, **kwargs):
        self.starts.append(url)
        self.kwargs.append(kwargs)
        self.running = True

    def stop(self):
        if self.running:
            self.running = False
            self.released.emit()


class DirectSlotTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.live = FakeLive()
        self.budget = ConnectionBudget()
        config = SimpleNamespace(
            selected_provider_name=PROVIDER,
            live_latency="normal",
            timeshift_enabled=False,
            timeshift_max_mib=256,
        )
        self.tuner = LiveTuner(self.session, config, self.live, self.budget)
        self.played = []

    def play(self, name):
        return lambda: self.played.append(name)

    def holders(self):
        return self.budget.holders(PROVIDER)

    def test_a_direct_play_waits_while_the_other_pictures_stream_closes(self):
        # M3: a closing PiP may still hold the provider's last slot.
        peer_session = FakeSession()
        peer = LiveTuner(peer_session, self.tuner.config, FakeLive(), self.budget, purpose="pip")
        wait_for_each_other(self.tuner, peer)
        peer.open(lambda: None, direct=True, provider=PROVIDER)
        peer_session.holding = True
        peer.stop()
        self.assertTrue(peer.releasing())
        self.tuner.open(self.play("file"), local=True)  # no provider's stream: no wait
        self.assertEqual(self.played, ["file"])
        self.tuner.open(self.play("movie"), direct=True, provider=PROVIDER)
        self.assertEqual((self.played, self.holders()), (["file"], ["pip"]))
        peer_session.let_go()
        self.assertEqual((self.played, self.holders()), (["file", "movie"], ["main"]))

    def test_a_direct_play_holds_the_main_slot_until_vlc_let_go(self):
        self.tuner.open(self.play("movie"), direct=True)
        self.assertEqual(self.played, ["movie"])
        self.assertEqual(self.holders(), ["main"])  # a PiP would see it taken
        self.session.holding = True
        self.tuner.stop()
        self.assertEqual(self.holders(), ["main"])  # the provider stream is still open
        self.session.let_go()
        self.assertEqual(self.holders(), [])

    def test_a_stop_gives_the_slot_back_at_once_when_nothing_is_held(self):
        self.tuner.open(self.play("channel"), direct=True)
        self.tuner.stop()
        self.assertEqual(self.holders(), [])

    def test_the_wait_for_vlc_is_capped(self):
        self.tuner.open(self.play("movie"), direct=True)
        self.session.holding = True
        self.tuner._input_cap.setInterval(20)
        with self.assertLogs("ui.live_tuner", "WARNING"):
            self.tuner.stop()
            elapse(40)  # runs due timers in order: a stalled runner can't skip the cap
        self.assertEqual(self.holders(), [])

    def test_one_direct_play_after_another_never_holds_two_slots(self):
        self.tuner.open(self.play("first"), direct=True)
        self.session.holding = True
        self.tuner.open(self.play("second"), direct=True)
        self.assertEqual((self.played, self.holders()), (["first"], ["main"]))
        self.session.let_go()
        self.assertEqual((self.played, self.holders()), (["first", "second"], ["main"]))

    def test_an_external_launch_holds_nothing(self):
        self.tuner.open(self.play("vlc"))
        self.assertEqual(self.played, ["vlc"])
        self.assertEqual(self.holders(), [])

    def test_a_shutdown_gives_the_slot_back(self):
        self.tuner.open(self.play("movie"), direct=True)
        self.tuner.shutdown()
        self.assertEqual(self.holders(), [])

    def test_a_pipeline_tune_waits_for_the_direct_plays_slot(self):
        self.tuner.open(self.play("movie"), direct=True)
        self.session.holding = True
        self.tuner.tune(LiveStart(URL, "QiTV", True), PROVIDER, 1)
        QTest.qWait(10)
        self.assertEqual(self.live.starts, [])
        self.assertEqual(self.holders(), ["main"])  # the movie's, until VLC let go
        self.session.let_go()
        QTest.qWait(10)
        self.assertEqual(self.live.starts, [URL])
        self.assertEqual(self.holders(), ["main"])  # now the channel's

    def test_without_a_pipeline_a_direct_play_holds_the_slot_too(self):
        tuner = LiveTuner(self.session, self.tuner.config, None, self.budget)
        tuner.open(self.play("movie"), direct=True)
        self.assertEqual(self.holders(), ["main"])
        tuner.stop()
        self.assertEqual(self.holders(), [])

    def test_a_direct_play_books_its_slot_under_the_provider_it_is_given(self):
        # During a provider setup the selected provider is already the new one while the
        # catalog (and the pipeline tunes) still use the old: both must book under one name.
        self.tuner.config.selected_provider_name = "Next provider"
        self.tuner.open(self.play("channel"), direct=True, provider=PROVIDER)
        self.assertEqual(self.holders(), ["main"])
        self.assertEqual(self.budget.holders("Next provider"), [])
        self.tuner.stop()
        self.assertEqual(self.holders(), [])


class SlotRoleTests(unittest.TestCase):
    """Phase 4: one tuner per player; a swap gives each another role (its purpose)."""

    def setUp(self):
        self.session = FakeSession()
        self.live = FakeLive()
        self.budget = ConnectionBudget()
        self.config = SimpleNamespace(
            selected_provider_name=PROVIDER,
            live_latency="low",
            timeshift_enabled=True,
            timeshift_max_mib=4096,
        )
        self.tuner = LiveTuner(self.session, self.config, self.live, self.budget, purpose="pip")
        self.released = []
        self.tuner.released.connect(lambda: self.released.append(True))

    def holders(self):
        return self.budget.holders(PROVIDER)

    def start_pipeline(self):
        self.tuner.tune(LiveStart(URL, "QiTV", True), PROVIDER, 2)
        QTest.qWait(10)
        self.assertEqual(self.live.starts, [URL])

    def test_its_purpose_names_its_slots_and_follows_a_new_role(self):
        self.start_pipeline()
        self.assertEqual(self.holders(), ["pip"])
        self.tuner.set_purpose("main")
        self.assertEqual(self.holders(), ["main"])
        self.assertEqual(self.tuner.purpose, "main")
        self.tuner.open(lambda: None, direct=True, provider=PROVIDER)
        self.assertEqual(self.holders(), ["main"])  # the direct play's, as the large picture
        self.tuner.set_purpose("pip")
        self.assertEqual(self.holders(), ["pip"])

    def test_released_follows_once_nothing_of_its_player_is_open(self):
        self.tuner.open(lambda: None, direct=True, provider=PROVIDER)
        self.session.holding = True
        self.tuner.stop()
        self.assertTrue(self.tuner.releasing())
        self.assertEqual(self.released, [])
        self.session.let_go()
        self.assertFalse(self.tuner.releasing())
        self.assertEqual(self.released, [True])
        self.start_pipeline()
        self.tuner.stop()  # FakeLive releases at once
        self.assertFalse(self.tuner.releasing())
        self.assertEqual(self.released, [True, True])

    def test_its_stream_is_the_budgeted_stream_of_its_live_session(self):
        self.assertIs(self.tuner.stream.live, self.live)
        self.assertEqual(self.tuner.stream.purpose, "pip")
        self.assertIsNone(LiveTuner(self.session, self.config, None, self.budget).stream)

    def test_start_kwargs_are_a_main_tunes(self):
        self.start_pipeline()
        kwargs = start_kwargs(LiveStart(URL, "QiTV", True), self.config)
        self.assertEqual(kwargs.pop("url"), URL)
        self.assertEqual(
            kwargs,
            {
                "headers": {"User-Agent": "QiTV"},
                "verify_ssl": True,
                "latency": "low",
                "user_agent": "QiTV",
                "disk": True,
                "max_bytes": 4096 * 1024 * 1024,
                # What the helper falls back to if the disk fails: the memory per stream (H12).
                "ram_bytes": 256 * 1024 * 1024,
            },
        )
        self.config.timeshift_enabled = False
        self.assertEqual(  # without consent: the memory per stream (default 256 MiB)
            start_kwargs(LiveStart(URL, "QiTV", False), self.config)["max_bytes"],
            256 * 1024 * 1024,
        )
        self.config.timeshift_ram_mib = 512
        self.assertEqual(
            start_kwargs(LiveStart(URL, "QiTV", False), self.config)["max_bytes"],
            512 * 1024 * 1024,
        )
        self.config.timeshift_enabled = True
        forced = start_kwargs(LiveStart(URL, "QiTV", False), self.config, disk=False)
        self.assertEqual((forced["disk"], forced["max_bytes"]), (False, 512 * 1024 * 1024))
        self.assertNotIn("ram_bytes", forced)  # in memory already
        on_disk = start_kwargs(LiveStart(URL, "QiTV", False), self.config)
        self.assertEqual(on_disk["ram_bytes"], 512 * 1024 * 1024)

    def test_one_disk_ring_at_most_the_other_player_keeps_it(self):
        # Ruling (M5): the user's disk cap is the total: a second stream goes to RAM.
        self.config.timeshift_ram_mib = 128
        other = SimpleNamespace(disk=True)
        self.tuner.set_disk_allowed(lambda: not other.disk)
        self.start_pipeline()
        kwargs = self.live.kwargs[-1]
        self.assertEqual((kwargs["disk"], kwargs["max_bytes"]), (False, 128 * 1024 * 1024))
        self.assertFalse(self.tuner.on_disk())
        other.disk = False
        self.tuner.stop()
        self.tuner.tune(LiveStart(URL, "QiTV", True), PROVIDER, 2)
        QTest.qWait(10)
        self.assertEqual(self.live.kwargs[-1]["disk"], True)
        self.assertTrue(self.tuner.on_disk())


if __name__ == "__main__":
    unittest.main()
