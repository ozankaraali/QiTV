import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services.connection_budget import DENIED, ConnectionBudget

app = QApplication.instance() or QApplication([])


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.b = ConnectionBudget()
        self.got = []
        self.b.granted.connect(self.got.append)

    def pump(self):
        app.processEvents()

    def test_immediate_grant_is_deferred_and_counted(self):
        t = self.b.request("p", 2, purpose="main")
        self.assertEqual(self.b.in_use("p"), 1)
        self.assertEqual(self.got, [])
        self.pump()
        self.assertEqual(self.got, [t])

    def test_queue_at_limit_then_grant_on_release(self):
        a = self.b.request("p", 1, purpose="main")
        self.pump()
        c = self.b.request("p", 1, purpose="pip")
        self.pump()
        self.assertEqual(self.got, [a])
        self.assertEqual(self.b.in_use("p"), 1)
        self.b.release(a)
        self.pump()
        self.assertEqual(self.got, [a, c])
        self.assertEqual(self.b.holders("p"), ["pip"])

    def test_newest_queued_per_purpose_wins(self):
        a = self.b.request("p", 1, purpose="main")
        self.pump()
        z1 = self.b.request("p", 1, purpose="zap")
        z2 = self.b.request("p", 1, purpose="zap")
        z3 = self.b.request("p", 1, purpose="zap")
        self.b.release(a)
        self.pump()
        self.assertEqual(self.got, [a, z3])
        self.assertNotIn(z1, self.got)
        self.assertNotIn(z2, self.got)

    def test_unlimited(self):
        for _ in range(5):
            self.b.request("p", None, purpose="main")
        self.assertEqual(self.b.in_use("p"), 5)

    def test_providers_are_independent(self):
        self.b.request("a", 1, purpose="main")
        self.b.request("b", 1, purpose="main")
        self.assertEqual((self.b.in_use("a"), self.b.in_use("b")), (1, 1))

    def test_queue_false_is_denied_and_not_queued(self):
        a = self.b.request("p", 1, purpose="main")
        self.pump()
        r = self.b.request("p", 1, purpose="pip", queue=False)
        self.assertEqual(r, DENIED)
        self.b.release(a)
        self.pump()
        self.assertEqual(self.got, [a])
        self.assertEqual(self.b.in_use("p"), 0)

    def test_queue_false_granted_when_free(self):
        a = self.b.request("p", 2, purpose="main")
        r = self.b.request("p", 2, purpose="pip", queue=False)
        self.assertNotEqual(r, DENIED)
        self.assertNotEqual(r, a)

    def test_cancel_queued_and_held(self):
        a = self.b.request("p", 1, purpose="main")
        self.pump()
        q = self.b.request("p", 1, purpose="pip")
        self.b.cancel(q)
        self.b.release(a)
        self.pump()
        self.assertEqual(self.got, [a])
        h = self.b.request("p", 1, purpose="main")
        self.b.cancel(h)
        self.assertEqual(self.b.in_use("p"), 0)

    def test_release_before_deferred_emit_never_fires(self):
        t = self.b.request("p", 1, purpose="main")
        self.b.release(t)
        self.pump()
        self.assertEqual(self.got, [])

    def test_unknown_release_and_cancel_are_noops(self):
        self.b.release(999)
        self.b.cancel(999)
        self.assertEqual(self.b.in_use("p"), 0)

    def test_limit_for(self):
        lf = self.b.limit_for
        self.assertEqual(lf({"type": "XTREAM"}, 3), 3)
        self.assertIsNone(lf({"type": "XTREAM"}, None))
        self.assertEqual(lf({"type": "STB"}, None), 1)
        self.assertIsNone(lf({"type": "M3UPLAYLIST"}, None))
        self.assertEqual(lf({"type": "STB", "max_streams": 2}, None), 2)
        self.assertEqual(lf({"type": "XTREAM", "max_streams": 4}, 1), 4)
        self.assertEqual(lf({"type": "M3UPLAYLIST", "max_streams": 2}, None), 2)
        self.assertEqual(lf({"type": "STB", "max_streams": 0}, None), 1)

    def test_release_of_queued_ticket_drops_it(self):
        a = self.b.request("p", 1, purpose="main")
        self.pump()
        q = self.b.request("p", 1, purpose="pip")
        self.b.release(q)
        self.b.release(a)
        self.pump()
        self.assertEqual(self.got, [a])
        self.assertEqual(self.b.in_use("p"), 0)

    def test_double_release_is_noop(self):
        a = self.b.request("p", 1, purpose="main")
        self.pump()
        self.b.release(a)
        self.b.release(a)
        self.assertEqual(self.b.in_use("p"), 0)

    def test_grant_order_is_fifo_by_newest_request(self):
        a = self.b.request("p", 1, purpose="main")
        self.pump()
        x = self.b.request("p", 1, purpose="x")
        y = self.b.request("p", 1, purpose="y")
        x2 = self.b.request("p", 1, purpose="x")  # replaces x, moves behind y
        self.b.release(a)
        self.pump()
        self.assertEqual(self.got, [a, y])
        self.b.release(y)
        self.pump()
        self.assertEqual(self.got, [a, y, x2])
        self.assertNotIn(x, self.got)

    def test_replacement_grant_counts_one(self):
        a = self.b.request("p", 1, purpose="main")
        self.pump()
        self.b.request("p", 1, purpose="zap")
        z = self.b.request("p", 1, purpose="zap")
        self.b.release(a)
        self.pump()
        self.assertEqual(self.got[-1], z)
        self.assertEqual(self.b.in_use("p"), 1)

    def test_other_provider_queue_not_promoted(self):
        a = self.b.request("a", 1, purpose="main")
        b = self.b.request("b", 1, purpose="main")
        self.pump()
        qb = self.b.request("b", 1, purpose="zap")
        self.b.release(a)
        self.pump()
        self.assertNotIn(qb, self.got)
        self.assertEqual(self.b.in_use("b"), 1)
        self.b.release(b)
        self.pump()
        self.assertIn(qb, self.got)

    def test_denied_release_cancel_ignored(self):
        self.b.release(DENIED)
        self.b.cancel(DENIED)
        self.assertEqual(self.b.in_use("p"), 0)

    def test_relabel_changes_a_held_or_queued_tickets_purpose(self):
        # A swap hands the large picture's slot to the tuner and the small one's to the PiP;
        # their tickets take the new roles' purposes, and nothing is counted twice.
        held = self.b.request("p", 1, purpose="pip")
        self.pump()
        queued = self.b.request("p", 1, purpose="main")
        self.b.relabel(held, "main")
        self.b.relabel(queued, "pip")
        self.assertEqual(self.b.holders("p"), ["main"])
        self.assertEqual(self.b.in_use("p"), 1)
        replacing = self.b.request("p", 1, purpose="pip")  # the newest per purpose wins
        self.b.release(held)
        self.pump()
        self.assertEqual(self.got, [held, replacing])
        self.assertEqual(self.b.holders("p"), ["pip"])
        self.b.relabel(queued, "main")  # gone: ignored, like DENIED and unknown tickets
        self.b.relabel(DENIED, "main")
        self.b.relabel(999, "main")
        self.assertEqual(self.b.holders("p"), ["pip"])

    def test_limit_for_edge_cases(self):
        lf = self.b.limit_for
        self.assertIsNone(lf({"type": "XTREAM"}, 0))
        self.assertIsNone(lf({"type": "XTREAM"}, -2))
        self.assertEqual(lf({"type": "STB", "max_streams": "2"}, None), 2)
        self.assertEqual(lf({"type": "STB", "max_streams": "abc"}, None), 1)
        self.assertEqual(lf({"type": "STB", "max_streams": -1}, None), 1)
        self.assertEqual(lf({"type": "STB", "max_streams": None}, None), 1)


if __name__ == "__main__":
    unittest.main()


class LearnedLimitTests(unittest.TestCase):
    """2.6: a limit learned for the run (a provider that keeps only the newest stream while a
    recording holds one), and whether a stream is free for a purpose."""

    def setUp(self):
        self.b = ConnectionBudget()

    def test_a_learned_limit_caps_requests(self):
        self.b.learn("P", 1)
        self.assertNotEqual(self.b.request("P", None, purpose="rec"), DENIED)
        self.assertEqual(self.b.request("P", None, purpose="main", queue=False), DENIED)

    def test_it_caps_every_providers_limit(self):
        self.b.learn("P", 1)
        self.assertEqual(self.b.limit_for({"name": "P", "type": "M3U"}, None), 1)
        self.assertEqual(self.b.limit_for({"name": "P", "type": "XTREAM"}, 3), 1)
        self.assertIsNone(self.b.limit_for({"name": "Q", "type": "M3U"}, None))
        self.assertEqual((self.b.cap("P", 2), self.b.cap("Q", 2)), (1, 2))

    def test_learning_never_raises_a_limit(self):
        self.b.learn("P", 1)
        self.b.learn("P", 3)
        self.assertEqual(self.b.cap("P", None), 1)

    def test_free_counts_the_other_purposes(self):
        self.b.request("P", 2, purpose="rec")
        self.assertTrue(self.b.free("P", 2, purpose="main"))
        self.b.request("P", 2, purpose="pip")
        self.assertFalse(self.b.free("P", 2, purpose="main"))
        self.assertTrue(self.b.free("P", 2, purpose="pip"))  # its own does not count
        self.assertTrue(self.b.free("P", None, purpose="main"))
