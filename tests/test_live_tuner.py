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
from ui.live_tuner import LiveStart, LiveTuner

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
        self.starts = []
        self.mode = ""
        self.running = False

    def is_pipeline(self):
        return False

    def start(self, url, **kwargs):
        self.starts.append(url)
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


if __name__ == "__main__":
    unittest.main()
