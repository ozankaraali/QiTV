import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ui.link_lanes import LinkLanes, Target


class Catalog(QObject):
    linkReady = Signal(int, str)
    linkFailed = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.requests = []  # (request id, item name, section, is_episode)

    def request_link(self, item, *, section, is_episode=False):
        self.requests.append((len(self.requests) + 1, item["name"], section, is_episode))
        return len(self.requests)


def target(name, section="live"):
    return Target({"name": name, "cmd": f"cmd/{name}"}, section, name)


class LinkLanesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.catalog = Catalog()
        self.lanes = LinkLanes(self.catalog)
        self.ready, self.failed, self.side_failed = [], [], []
        self.lanes.tuneReady.connect(lambda t, url: self.ready.append((t.title, url)))
        self.lanes.tuneFailed.connect(lambda t, msg: self.failed.append((t.title, msg)))
        self.lanes.sideFailed.connect(lambda t, msg: self.side_failed.append((t.title, msg)))

    def names(self):
        return [name for _, name, _, _ in self.catalog.requests]

    def test_one_tune_in_flight_and_one_queued(self):
        self.lanes.request(target("One"))
        self.lanes.request(target("Two"))
        self.lanes.request(target("Three"))
        self.assertEqual(self.names(), ["One"])
        self.assertEqual(self.lanes.queued.title, "Three")
        self.assertEqual(self.lanes.pending().title, "Three")
        self.catalog.linkReady.emit(1, "u1")
        self.assertEqual(self.ready, [("One", "u1")])  # the controller follows the queue

    def test_side_requests_go_one_at_a_time_beside_the_tune(self):
        self.lanes.request(target("One"))
        got = []
        self.lanes.request_side(target("Two"), got.append, play=False)
        self.lanes.request_side(target("Three"), got.append, play=False)
        self.assertEqual(self.names(), ["One", "Two"])
        self.catalog.linkFailed.emit(2, "No stream")
        self.assertEqual(self.side_failed, [("Two", "No stream")])
        self.assertEqual(self.names(), ["One", "Two", "Three"])
        self.catalog.linkReady.emit(3, "u3")
        self.assertEqual(got, ["u3"])
        self.catalog.linkReady.emit(1, "u1")
        self.assertEqual(self.ready, [("One", "u1")])

    def test_a_newer_choice_drops_a_side_play_but_never_a_copy(self):
        played, copied = [], []
        self.lanes.request_side(target("Two"), played.append, play=True)
        self.lanes.request_side(target("Three"), copied.append, play=False)
        self.lanes.supersede()  # e.g. a zap
        self.catalog.linkReady.emit(1, "u2")
        self.catalog.linkReady.emit(2, "u3")
        self.assertEqual((played, copied), ([], ["u3"]))

    def test_a_tune_superseded_by_a_side_play_is_dropped(self):
        self.lanes.request(target("One"))
        self.lanes.request_side(target("Two"), lambda url: None, play=True)
        self.catalog.linkReady.emit(1, "u1")
        self.catalog.linkFailed.emit(1, "late")
        self.assertEqual((self.ready, self.failed), ([], []))

    def test_a_side_play_supersedes_the_queued_tune_too(self):
        self.lanes.request(target("One"))
        self.lanes.request(target("Two"))  # queued
        self.lanes.request_side(target("Three"), lambda url: None, play=True)
        self.assertIsNone(self.lanes.queued)
        self.catalog.linkReady.emit(1, "u1")
        self.assertEqual(self.ready, [])  # neither One nor the older Two opens

    def test_a_superseded_side_play_waiting_in_line_is_never_requested(self):
        got = []
        self.lanes.request_side(target("Two"), got.append, play=False)
        self.lanes.request_side(target("Three"), got.append, play=True)
        self.lanes.request_side(target("Four"), got.append, play=False)
        self.lanes.supersede()  # e.g. a zap
        self.catalog.linkReady.emit(1, "u2")
        self.assertEqual(self.names(), ["Two", "Four"])

    def test_an_overtaken_tune_is_reported_as_dropped(self):
        dropped = []
        self.lanes.tuneDropped.connect(lambda t: dropped.append(t.title))
        self.lanes.request(target("One"))
        self.lanes.request_side(target("Two"), lambda url: None, play=True)
        self.catalog.linkFailed.emit(2, "no")
        self.catalog.linkReady.emit(1, "u1")
        self.assertEqual((dropped, self.ready), (["One"], []))

    def test_a_side_follow_up_that_raises_is_reported_and_the_queue_goes_on(self):
        def broken(url):
            raise RuntimeError("bad")

        self.lanes.request_side(target("Two"), broken, play=False)
        self.lanes.request_side(target("Three"), lambda url: None, play=False)
        with self.assertLogs("ui.link_lanes", "WARNING"):
            self.catalog.linkReady.emit(1, "u2")
        self.assertEqual(self.side_failed, [("Two", "bad")])
        self.assertEqual(self.names(), ["Two", "Three"])

    def test_drop_forgets_every_pending_request(self):
        got = []
        self.lanes.request(target("One"))
        self.lanes.request_side(target("Two"), got.append, play=False)
        self.lanes.request_side(target("Three"), got.append, play=False)
        self.lanes.drop()
        self.assertFalse(self.lanes.busy())
        self.catalog.linkReady.emit(1, "u1")
        self.catalog.linkReady.emit(2, "u2")
        self.assertEqual((self.ready, got, self.names()), ([], [], ["One", "Two"]))


if __name__ == "__main__":
    unittest.main()
