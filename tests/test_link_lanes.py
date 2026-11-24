import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from timer_fixtures import pause, wait_until

from ui.link_lanes import LinkLanes, Target

PAGE = "https://kick.com/examplechannel"


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

    def test_a_link_answered_but_never_played_is_said_dropped(self):  # 2.14 review I2
        # (A Plex conversion's session the server started for it is stopped then.)
        dropped = []
        self.lanes.linkDropped.connect(dropped.append)
        self.lanes.request(target("One"))
        self.lanes.request_side(target("Two"), lambda url: None, play=True)
        self.catalog.linkFailed.emit(2, "no")
        self.catalog.linkReady.emit(1, "u1")  # overtaken by a side play
        self.lanes.request(target("Three"))
        self.lanes.drop_tune()  # Stop
        self.catalog.linkReady.emit(3, "u3")
        got = []
        self.lanes.request_side(target("Four"), got.append, play=True)
        self.lanes.supersede()  # a newer choice
        self.catalog.linkReady.emit(4, "u4")
        self.lanes.request(target("Five"))
        self.catalog.linkReady.emit(5, "u5")  # played: not dropped
        self.assertEqual(dropped, ["u1", "u3", "u4"])
        self.assertEqual((got, self.ready[-1]), ([], ("Five", "u5")))

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

    def site(self):
        """A Kick or Twitch channel's page (a link of Network streams) and its resolver's asks."""
        asked = []
        self.lanes.known = lambda item: item.get("page")
        self.lanes.resolver = lambda url, ok, failed: asked.append((url, ok, failed))
        return Target({"name": "Kick one", "page": PAGE}, "live", "Kick one"), asked

    def test_the_lane_resolves_a_site_page_each_time(self):
        page, asked = self.site()
        self.lanes.request(page)
        self.assertEqual(([url for url, _, _ in asked], self.names()), ([PAGE], []))
        pause(20)
        self.assertEqual(self.ready, [])  # no answer of its own: the site's is awaited
        asked[0][1]("https://edge.example/hls/master.m3u8?token=one")
        self.assertTrue(wait_until(lambda: self.ready))
        self.assertEqual(
            self.ready, [("Kick one", "https://edge.example/hls/master.m3u8?token=one")]
        )
        self.lanes.request(page)  # played again: the site is asked afresh
        self.assertEqual(len(asked), 2)
        asked[1][1]("https://edge.example/hls/master.m3u8?token=two")
        self.assertTrue(wait_until(lambda: len(self.ready) == 2))
        self.assertEqual(self.ready[-1][1], "https://edge.example/hls/master.m3u8?token=two")
        # Any other link of Network streams is answered as it is, a turn later.
        other = Target({"name": "Coast", "page": "https://cdn.example/coast.m3u8"}, "live", "Coast")
        self.lanes.request(other)
        self.assertTrue(wait_until(lambda: len(self.ready) == 3))
        self.assertEqual(self.ready[-1], ("Coast", "https://cdn.example/coast.m3u8"))
        self.assertEqual(len(asked), 2)

    def test_a_site_answer_given_at_once_still_lands(self):
        # A resolver that answers inside its call: the lane takes it once its request is noted.
        self.lanes.known = lambda item: item.get("page")
        self.lanes.resolver = lambda url, ok, failed: ok("https://edge.example/now.m3u8")
        self.lanes.request(Target({"name": "Kick one", "page": PAGE}, "live", "Kick one"))
        self.assertTrue(wait_until(lambda: self.ready))
        self.assertEqual(self.ready, [("Kick one", "https://edge.example/now.m3u8")])
        self.assertFalse(self.lanes.busy())

    def test_a_site_page_that_fails_says_why_on_its_lane(self):
        page, asked = self.site()
        self.lanes.request(page)
        asked[0][2]("examplechannel isn't live now")
        self.assertTrue(wait_until(lambda: self.failed))
        self.assertEqual(self.failed, [("Kick one", "examplechannel isn't live now")])
        # The side lane's too: a link asked beside the tune, its failure said there.
        got = []
        self.lanes.request_side(page, got.append, play=False)
        self.lanes.request_side(page, got.append, play=False)  # (the same: asked once)
        self.assertEqual(len(asked), 2)
        asked[1][1]("https://edge.example/side.m3u8")
        self.assertTrue(wait_until(lambda: got))
        self.assertEqual(got, ["https://edge.example/side.m3u8"])
        self.lanes.request_side(page, got.append, play=False)
        asked[2][2]("No channel named examplechannel on Kick")
        self.assertTrue(wait_until(lambda: self.side_failed))
        self.assertEqual(
            self.side_failed, [("Kick one", "No channel named examplechannel on Kick")]
        )


if __name__ == "__main__":
    unittest.main()
