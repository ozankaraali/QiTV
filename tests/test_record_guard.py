"""2.6: a provider that keeps only the newest stream: the recording wins (ui/record_guard.py)."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from services.connection_budget import ConnectionBudget
from services.sessions import Slot
from ui.record_guard import DROP_WINDOW_S, RecordGuard


class FakeLive(QObject):
    timelineChanged = Signal(dict)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.recorded = False

    def recording(self):
        return self.recorded


class FakeStream:
    def __init__(self):
        self.ticket = None


class FakeTuner:
    def __init__(self, purpose):
        self.purpose = purpose
        self.stream = FakeStream()


class RecordGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.budget = ConnectionBudget()
        self.now = [1000.0]
        self.slots = [self.slot("main", "Seven"), self.slot("b1", "Eight")]
        self.limits = {"P": None}
        self.stopped, self.toasts = [], []
        self.guard = RecordGuard(
            self.budget,
            lambda: list(self.slots),
            limit_of=lambda slot: self.limits["P"],
            stop=self.stopped.append,
            toast=self.toasts.append,
            clock=lambda: self.now[0],
        )
        for slot in self.slots:
            self.guard.watch_slot(slot)

    def slot(self, name, channel):
        slot = Slot(name, object(), FakeLive(), tuner=FakeTuner(name))
        slot.channel = {"name": channel}
        return slot

    def open(self, slot, provider="P"):
        """`slot`'s stream is granted (it starts)."""
        ticket = self.budget.request(provider, None, purpose=slot.tuner.purpose)
        slot.tuner.stream.ticket = ticket
        self.app.processEvents()  # granted is emitted deferred
        return ticket

    def recording_then_another(self):
        main, b1 = self.slots
        self.open(main)
        main.live.recorded = True
        self.now[0] += 60.0
        self.open(b1)
        self.now[0] += 3.0
        return main, b1

    def test_the_newer_stream_stops_and_the_limit_is_learned(self):
        main, b1 = self.recording_then_another()
        main.live.timelineChanged.emit({"reconnecting": True})
        self.assertEqual(self.stopped, [b1])
        self.assertEqual(self.budget.cap("P", None), 1)
        self.assertEqual(self.toasts, ["Your provider allows no other stream while Seven records"])
        main.live.timelineChanged.emit({"reconnecting": True})  # once
        self.assertEqual(len(self.stopped), 1)

    def test_a_failure_is_not_watched(self):
        # 2.8 (review of 2.6): LiveSession ends a recording before it says `failed`
        # (test_live_session_recording), so a failure never finds one to save; the path that
        # could only fire with a fake is gone.
        main, _b1 = self.recording_then_another()
        main.live.failed.emit("Connection refused")
        self.assertEqual(self.stopped, [])

    def test_a_known_limit_is_left_to_the_budget(self):
        self.limits["P"] = 2
        main, _b1 = self.recording_then_another()
        main.live.timelineChanged.emit({"reconnecting": True})
        self.assertEqual(self.stopped, [])
        self.assertIsNone(self.budget.cap("P", None))

    def test_a_late_reconnect_is_not_blamed(self):
        main, _b1 = self.recording_then_another()
        self.now[0] += DROP_WINDOW_S + 20.0
        main.live.timelineChanged.emit({"reconnecting": True})
        self.assertEqual(self.stopped, [])

    def test_another_providers_stream_is_not_blamed(self):
        main, b1 = self.slots
        self.open(main)
        main.live.recorded = True
        self.open(b1, provider="Q")
        main.live.timelineChanged.emit({"reconnecting": True})
        self.assertEqual(self.stopped, [])

    def test_a_channel_that_does_not_record_is_not_guarded(self):
        main, b1 = self.slots
        self.open(main)
        self.open(b1)
        main.live.timelineChanged.emit({"reconnecting": True})
        self.assertEqual(self.stopped, [])

    def test_a_newer_recording_is_never_stopped_for_an_older_one(self):
        main, b1 = self.recording_then_another()
        b1.live.recorded = True
        main.live.timelineChanged.emit({"reconnecting": True})
        self.assertEqual(self.stopped, [])


if __name__ == "__main__":
    unittest.main()
