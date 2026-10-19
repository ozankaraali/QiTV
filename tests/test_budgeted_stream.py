"""One live stream's budget slot and the wait for its release (shared by main and PiP)."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from timer_fixtures import elapse

from services.budgeted_stream import RELEASE_CAP_MS, BudgetedStream, HeldSlot
from services.connection_budget import DENIED, ConnectionBudget

app = QApplication.instance() or QApplication([])

P = "Provider"


class FakeLive(QObject):
    """LiveSession's start/stop and `released`: at once on stop unless `auto_release` is False."""

    released = Signal()

    def __init__(self):
        super().__init__()
        self.calls = []
        self.auto_release = True
        self.fail_start = False

    def start(self, url, **kwargs):
        if self.fail_start:
            raise RuntimeError("cannot open")
        self.calls.append(("start", url, kwargs))

    def stop(self):
        self.calls.append(("stop",))
        if self.auto_release:
            self.released.emit()


class _Fixture(unittest.TestCase):
    cap_ms = RELEASE_CAP_MS

    def setUp(self):
        self.budget = ConnectionBudget()
        self.live = FakeLive()
        self.stream = BudgetedStream(self.live, self.budget, "main", cap_ms=self.cap_ms)
        self.grants, self.releases = [], []
        self.stream.granted.connect(lambda: self.grants.append(self.stream.ticket))
        self.stream.released.connect(lambda: self.releases.append(True))

    def pump(self):
        app.processEvents()

    def running_stream(self):
        self.stream.request(P, 2)
        self.pump()
        self.stream.start("http://provider.test/1.ts", latency="normal")
        self.assertTrue(self.stream.running())


class BudgetedStreamTests(_Fixture):
    def test_a_grant_is_reported_for_the_latest_request_only(self):
        first = self.stream.request(P, 2)
        second = self.stream.request(P, 2)  # replaces the first: its ticket goes back
        self.budget.request(P, 2, purpose="pip")  # another stream's grant
        self.assertFalse(self.stream.is_granted())  # the budget grants later
        self.pump()
        self.assertEqual(self.grants, [second])
        self.assertTrue(self.stream.is_granted())
        self.assertNotEqual(first, second)
        self.assertEqual(sorted(self.budget.holders(P)), ["main", "pip"])

    def test_start_passes_its_arguments_to_the_live_session(self):
        self.running_stream()
        self.assertEqual(
            self.live.calls, [("start", "http://provider.test/1.ts", {"latency": "normal"})]
        )

    def test_a_ticket_that_never_started_goes_back_at_once(self):
        self.stream.request(P, 2)
        self.stream.stop()
        self.pump()
        self.assertEqual((self.budget.holders(P), self.grants), ([], []))
        self.assertEqual(self.live.calls, [])
        self.assertFalse(self.stream.waiting())

    def test_a_running_stream_gives_its_ticket_back_on_released(self):
        self.live.auto_release = False
        self.running_stream()
        self.stream.stop()
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.assertTrue(self.stream.waiting())
        self.assertFalse(self.stream.running())
        self.assertEqual(self.budget.holders(P), ["main"])  # until its upstream is closed
        self.live.released.emit()
        self.assertEqual((self.budget.holders(P), self.releases), ([], [True]))
        self.assertFalse(self.stream.waiting())

    def test_a_release_inside_stop_is_handled_at_once(self):
        self.running_stream()
        self.stream.stop()
        self.assertEqual((self.budget.holders(P), self.releases), ([], [True]))
        self.assertFalse(self.stream.waiting())

    def test_a_released_it_did_not_ask_for_is_ignored(self):
        self.running_stream()
        self.live.released.emit()  # someone else stopped the live session
        self.assertEqual((self.budget.holders(P), self.releases), (["main"], []))
        self.assertTrue(self.stream.running())

    def test_a_denied_request_holds_no_ticket(self):
        self.budget.request(P, 1, purpose="pip")
        self.assertEqual(self.stream.request(P, 1, queue=False), DENIED)
        self.assertIsNone(self.stream.ticket)
        self.assertEqual(self.budget.holders(P), ["pip"])

    def test_a_start_that_raises_gives_the_ticket_back_and_propagates(self):
        self.live.fail_start = True
        self.stream.request(P, 2)
        self.pump()
        with self.assertRaises(RuntimeError):
            self.stream.start("http://provider.test/1.ts")
        self.assertEqual(self.budget.holders(P), [])
        self.assertIsNone(self.stream.ticket)
        self.assertFalse(self.stream.running())
        self.assertEqual(self.live.calls, [("stop",)])  # whatever it had begun is stopped
        self.assertEqual(self.releases, [])  # that released() was not asked for


class PurposeTests(_Fixture):
    """A swap gives the stream another role: its tickets follow."""

    def test_set_purpose_relabels_the_running_and_the_releasing_tickets(self):
        self.running_stream()
        self.live.auto_release = False
        self.stream.stop()  # its ticket waits for the upstream's close
        self.stream.request(P, 2)  # the next one, granted while the old one still counts
        self.stream.set_purpose("pip")
        self.assertEqual(sorted(self.budget.holders(P)), ["pip", "pip"])
        self.assertEqual(self.stream.purpose, "pip")
        self.live.released.emit()
        self.assertEqual(self.budget.holders(P), ["pip"])
        self.stream.request(P, 2)
        self.assertEqual(self.budget.holders(P), ["pip"])  # new requests use the new purpose

    def test_a_held_slot_follows_its_new_purpose(self):
        held = HeldSlot(self.budget, "main")
        held.take(P)
        held.set_purpose("pip")
        self.assertEqual(self.budget.holders(P), ["pip"])
        held.take(P)
        self.assertEqual(self.budget.holders(P), ["pip"])
        held.give_back()
        self.assertEqual(self.budget.holders(P), [])


class TryTakeTests(_Fixture):
    """A multiview tile's direct play takes a slot only within the provider's limit."""

    def test_it_takes_a_free_slot_in_place_of_its_own(self):
        held = HeldSlot(self.budget, "t2")
        self.assertTrue(held.try_take(P, 2))
        self.assertTrue(held.try_take(P, 2))  # its own goes back first: still one
        self.assertEqual(self.budget.holders(P), ["t2"])

    def test_a_full_provider_denies_it_and_queues_nothing(self):
        self.budget.request(P, 2, purpose="main")
        self.budget.request(P, 2, purpose="pip")
        held = HeldSlot(self.budget, "t2")
        self.assertFalse(held.try_take(P, 2))
        self.assertIsNone(held.ticket)
        self.assertEqual(sorted(self.budget.holders(P)), ["main", "pip"])
        self.assertEqual(self.budget._waiting, {})

    def test_without_a_limit_it_always_takes_one(self):
        held = HeldSlot(self.budget, "t3")
        self.budget.request(P, None, purpose="main")
        self.assertTrue(held.try_take(P, None))
        self.assertEqual(sorted(self.budget.holders(P)), ["main", "t3"])


class ReleaseCapTests(_Fixture):
    cap_ms = 50

    def test_the_cap_gives_the_ticket_back_and_a_late_released_is_ignored(self):
        self.live.auto_release = False
        self.running_stream()
        self.stream.stop()
        with self.assertLogs("services.budgeted_stream", "WARNING") as logs:
            elapse(self.cap_ms * 2)
        self.assertIn("The main stream", logs.output[0])  # its purpose
        self.assertEqual((self.budget.holders(P), self.releases), ([], [True]))
        self.running_stream()  # the next stream opens on a new ticket
        self.live.released.emit()  # the first one's, late
        self.assertEqual((self.budget.holders(P), self.releases), (["main"], [True]))
        self.assertTrue(self.stream.running())


if __name__ == "__main__":
    unittest.main()
