"""Kept buffers (2.6): the registry of parked sessions, one per channel."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from services.live.kept import KeptBuffers, KeptRing, Where

app = QApplication.instance() or QApplication([])

LIVE = Where(None, False)


class FakeEngine(QObject):
    sessionUpdated = Signal(str, dict)
    keptFound = Signal(str, dict, dict)  # 2.6: a later helper took a kept folder back

    def __init__(self):
        super().__init__()
        self.closed = []

    def close_session(self, sid):
        self.closed.append(sid)


class KeptBuffersTests(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.kept = KeptBuffers(self.engine)

    def test_take_hands_it_back_once(self):
        ring = KeptRing("a", "k1", True, Where(12.0, True))
        self.kept.put(ring)
        self.assertIsNone(self.kept.take("k2"))
        self.assertEqual(self.kept.take("k1"), ring)
        self.assertIsNone(self.kept.take("k1"))
        self.assertEqual(self.engine.closed, [])

    def test_one_memory_ring_is_kept(self):
        self.kept.put(KeptRing("a", "k1", False, LIVE))
        self.kept.put(KeptRing("b", "k2", False, LIVE))
        self.assertEqual(self.engine.closed, ["a"])
        self.assertIsNone(self.kept.take("k1"))
        self.assertEqual(self.kept.take("k2").sid, "b")

    def test_a_ring_coming_right_back_leaves_the_others(self):  # 2.8
        # Its channel is tuned again at once (a new picture size): the memory ring kept
        # before stays; the next ring put as usual keeps only itself again.
        self.kept.put(KeptRing("a", "k1", False, LIVE))
        self.kept.put(KeptRing("b", "k2", False, LIVE), coming_back=True)
        self.assertEqual(self.engine.closed, [])
        self.assertEqual(self.kept.take("k2").sid, "b")
        self.assertEqual([r.sid for r in self.kept.rings()], ["a"])
        self.kept.put(KeptRing("c", "k3", False, LIVE))
        self.assertEqual(self.engine.closed, ["a"])

    def test_disk_rings_are_all_kept(self):
        for sid, channel in (("a", "k1"), ("b", "k2"), ("c", "k3")):
            self.kept.put(KeptRing(sid, channel, True, LIVE))
        self.kept.put(KeptRing("m", "k4", False, LIVE))  # a memory one beside them
        self.assertEqual(self.engine.closed, [])
        self.assertEqual([r.sid for r in self.kept.rings()], ["a", "b", "c", "m"])

    def test_the_same_channel_replaces(self):
        self.kept.put(KeptRing("a", "k1", True, LIVE))
        self.kept.put(KeptRing("b", "k1", True, LIVE))
        self.assertEqual(self.engine.closed, ["a"])
        self.assertEqual(self.kept.take("k1").sid, "b")

    def test_turning_the_disk_off_closes_the_disk_rings(self):
        self.kept.put(KeptRing("a", "k1", True, LIVE))
        self.kept.put(KeptRing("m", "k2", False, LIVE))
        self.kept.set_disk(False)
        self.assertEqual(self.engine.closed, ["a"])
        self.assertEqual([r.sid for r in self.kept.rings()], ["m"])

    def test_with_the_disk_off_a_disk_ring_left_later_goes(self):
        # The channel playing on disk when the disk was turned off, left afterwards.
        self.kept.set_disk(False)
        self.kept.put(KeptRing("a", "k1", True, LIVE))
        self.kept.put(KeptRing("m", "k2", False, LIVE))
        self.assertEqual(self.engine.closed, ["a"])
        self.assertEqual([r.sid for r in self.kept.rings()], ["m"])

    def test_with_the_disk_on_again_disk_rings_are_kept(self):
        self.kept.set_disk(False)
        self.kept.set_disk(True)
        self.kept.put(KeptRing("a", "k1", True, LIVE))
        self.assertEqual(self.engine.closed, [])
        self.assertEqual(self.kept.take("k1").sid, "a")

    def test_drop_one(self):
        self.kept.put(KeptRing("a", "k1", True, LIVE))
        self.kept.drop("a")
        self.assertEqual(self.engine.closed, ["a"])
        self.assertEqual(self.kept.rings(), [])

    def test_a_kept_buffer_left_empty_goes(self):
        # Review I5: the disk amount or the day's limit took all of it.
        self.kept.put(KeptRing("a", "k1", True, LIVE))
        self.kept.put(KeptRing("b", "k2", True, LIVE))
        self.engine.sessionUpdated.emit("a", {"oldest": 3})
        self.engine.sessionUpdated.emit("x", {"oldest": None})  # not a kept one
        self.engine.sessionUpdated.emit("a", {"oldest": None})
        self.assertEqual(self.engine.closed, ["a"])
        self.assertEqual([r.sid for r in self.kept.rings()], ["b"])

    def test_what_a_new_helper_took_back_is_kept(self):
        self.engine.keptFound.emit("z", {"channel": "k1"}, {"position": 12.0, "paused": True})
        self.engine.keptFound.emit("y", {"channel": "k2"}, {})
        self.engine.keptFound.emit("x", {}, {})  # no channel: let go
        self.assertEqual(self.kept.take("k1"), KeptRing("z", "k1", True, Where(12.0, True)))
        self.assertEqual(self.kept.take("k2"), KeptRing("y", "k2", True, Where(None, False)))
        self.assertEqual(self.engine.closed, ["x"])

    def test_a_new_engine_forgets_the_old_ones_sessions(self):
        self.kept.put(KeptRing("a", "k1", True, LIVE))
        other = FakeEngine()
        self.kept.use_engine(other)
        self.assertEqual(self.kept.rings(), [])
        self.assertEqual((self.engine.closed, other.closed), ([], []))


if __name__ == "__main__":
    unittest.main()
