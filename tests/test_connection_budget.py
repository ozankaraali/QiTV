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


if __name__ == "__main__":
    unittest.main()
