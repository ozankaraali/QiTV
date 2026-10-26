"""The phone remote's second screen, QiTV's side: what's on, the guide, search results and the
Library, as plain dicts with opaque handles (ui/remote_snapshots.py), and what a phone's tap on a
handle does (ui/remote_controller.py)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import time
import unittest
from unittest import mock

from PySide6.QtTest import QTest
from test_tv_window import LIBRARY_WAIT_MS, XTREAM, TwoProviderCase, WindowCase, wait_until

from services.remote.protocol import Closed, Get, Opened, OpenItem, Press, Query
from tests.fixtures import local_xtream
from ui.remote_snapshots import Handles, state


class SnapshotTests(WindowCase):
    def test_state_has_no_urls_or_credentials(self):
        self.open_first_channel()
        self.assertTrue(wait_until(lambda: state(self.window)["now"] is not None))
        snapshot = state(self.window)
        text = json.dumps(snapshot)
        for secret in ("http", "cmd", "link", "password"):
            self.assertNotIn(secret, text)
        self.assertEqual(snapshot["t"], "state")
        self.assertEqual(snapshot["now"]["name"], "Channel 1")
        self.assertEqual(snapshot["now"]["kind"], "live")
        self.assertIn(snapshot["context"], ("player", "drawer", "guide", "library", "subtitles"))
        for key in ("volume", "muted", "recording", "pip", "multiview", "section"):
            self.assertIn(key, snapshot)

    def test_behind_live_only_once_the_viewer_left_live(self):
        controller = self.window.controller
        self.addCleanup(setattr, controller._tuner, "timeline", None)
        with mock.patch.object(controller, "_live_lag", return_value=4.2):
            controller._tuner.timeline = {"lag": 4.2, "mode": "live"}  # its buffer: still live
            self.assertEqual(controller.behind_live(), 0.0)
            controller._tuner.timeline = {"lag": 4.2, "mode": "shifted"}  # paused or rewound
            self.assertEqual(controller.behind_live(), 4.2)
        self.open_first_channel()
        self.assertTrue(wait_until(lambda: state(self.window)["now"] is not None))
        with mock.patch.object(controller, "behind_live", return_value=38.4):
            self.assertEqual(state(self.window)["now"]["behind_s"], 38)
        with mock.patch.object(controller, "behind_live", return_value=0.0):
            self.assertEqual(state(self.window)["now"]["behind_s"], 0)

    def test_where_it_was_left_reaches_the_phone(self):
        controller = self.window.controller
        self.assertIsNone(state(self.window).get("back"))
        with mock.patch.object(controller, "back_label", return_value="20:10"):
            self.assertEqual(state(self.window)["back"], "20:10")

    def test_what_records_in_the_background_reaches_the_phone(self):
        controller = self.window.controller
        self.assertEqual(state(self.window)["rec"], [])
        since = time.monotonic() - 90.0  # recording for a minute and a half
        with mock.patch.object(controller, "rec_list", return_value=[("b1", "News", since)]):
            [entry] = state(self.window)["rec"]
        self.assertEqual((entry["id"], entry["name"]), ("b1", "News"))
        self.assertAlmostEqual(entry["since"], time.time() - 90.0, delta=2.0)
        self.assertIsInstance(entry["since"], int)  # whole seconds: a snapshot stays the same

    def test_state_before_anything_plays(self):
        snapshot = state(self.window)
        self.assertIsNone(snapshot["now"])

    def test_handles_are_capped(self):
        handles = Handles(limit=2000)
        ids = [handles.issue("channel", {"n": i}) for i in range(2100)]
        self.assertIsNone(handles.get(ids[0]))
        self.assertIsNone(handles.get(ids[99]))
        self.assertEqual(handles.get(ids[100]), ("channel", {"n": 100}))
        self.assertEqual(handles.get(ids[-1]), ("channel", {"n": 2099}))
        self.assertIsNone(handles.get("made-up"))


class PhoneLibraryWindowTests(TwoProviderCase):
    """The phone's Library over a provider's films: QiTV answers that phone, a tap plays the film,
    and the TV's own Library stays as it was."""

    selected = XTREAM

    def setUp(self):
        super().setUp()
        self.hub = mock.Mock()
        self.window.remote.hub = self.hub
        self.window.remote.apply_config = lambda: None
        self.addCleanup(setattr, self.window.remote, "hub", None)

    def libraries(self, sid="s1"):
        return [c.args[1] for c in self.hub.send.call_args_list if c.args[0] == sid]

    def test_a_film_tapped_on_the_phone_plays(self):
        plays = self.play_spy()
        self.show()
        self.wait_ready(XTREAM)
        remote = self.window.remote
        remote.bridge.post(Get("s1", "library", "", 0, "movies", ""))

        def titles():
            found = [m for m in self.libraries() if m["t"] == "library" and m["items"]]
            return found[-1] if found else None

        self.assertTrue(wait_until(lambda: titles() is not None, LIBRARY_WAIT_MS))
        message = titles()
        self.assertTrue(message["title"])  # the genre shown: the first one
        self.assertNotIn("http", json.dumps(message))
        self.assertFalse(self.window.screens.library_shown())  # the TV stayed where it was
        second = message["items"][1]
        self.assertIn(second["title"], local_xtream.movie_titles())
        remote.bridge.post(OpenItem("s1", second["id"]))
        self.assertTrue(wait_until(lambda: len(plays) == 1, LIBRARY_WAIT_MS))
        self.assertTrue(plays[0].startswith(self.xtream.url))
        remote.bridge.post(Closed("s1", "d1"))  # the phone left: its Library goes
        self.assertTrue(wait_until(lambda: "s1" not in remote._libraries))

    def test_a_series_tapped_on_the_phone_opens_its_seasons_on_the_tv(self):
        self.show()
        self.wait_ready(XTREAM)
        remote = self.window.remote
        remote.bridge.post(Get("s1", "library", "", 0, "series", ""))

        def titles():
            found = [m for m in self.libraries() if m["t"] == "library" and m["items"]]
            return found[-1] if found else None

        self.assertTrue(wait_until(lambda: titles() is not None, LIBRARY_WAIT_MS))
        self.assertEqual(titles()["section"], "series")
        remote.bridge.post(OpenItem("s1", titles()["items"][0]["id"]))
        library = self.window.library_controller
        self.assertTrue(wait_until(lambda: library.mode == "series", LIBRARY_WAIT_MS))
        self.assertTrue(self.window.screens.library_shown())


class PushTests(WindowCase):
    def setUp(self):
        super().setUp()
        self.hub = mock.Mock()
        self.window.remote.hub = self.hub
        self.window.remote.apply_config = lambda: None  # the first frame would turn it off
        self.addCleanup(setattr, self.window.remote, "hub", None)

    def sent(self, sid=None):
        out = []
        for call in self.hub.send.call_args_list:
            if sid is None or call.args[0] == sid:
                out.append(call.args[1])
        return out

    def test_state_on_open_and_after_a_press(self):
        self.open_first_channel()
        remote = self.window.remote
        remote.bridge.post(Opened("s1", "d1", "home"))
        self.assertTrue(wait_until(lambda: any(m["t"] == "state" for m in self.sent("s1"))))
        self.hub.broadcast.reset_mock()
        remote.bridge.post(Press("s1", "guide", True, 1))  # the guide opens: the TV changed
        remote.bridge.post(Press("s1", "guide", False, 1))
        self.assertTrue(wait_until(lambda: self.hub.broadcast.called))
        message = self.hub.broadcast.call_args.args[0]
        self.assertEqual((message["t"], message["context"]), ("state", "guide"))

    def test_nothing_changed_nothing_sent(self):
        self.open_first_channel()
        remote = self.window.remote
        remote.bridge.post(Opened("s1", "d1", "home"))
        QTest.qWait(50)
        self.hub.broadcast.reset_mock()
        QTest.qWait(1200)  # two diff ticks with the same channel on
        self.assertFalse(self.hub.broadcast.called)

    def test_get_guide_and_library_answer_that_phone(self):
        self.open_first_channel()
        remote = self.window.remote
        remote.bridge.post(Get("s2", "guide", "", 0))
        remote.bridge.post(Get("s2", "library", "", 0, "movies", ""))
        self.assertTrue(
            wait_until(lambda: {m["t"] for m in self.sent("s2")} >= {"guide", "library"})
        )

    def test_the_phones_own_search_leaves_the_tv_alone(self):
        """Search on the phone, without Show on TV: the desktop's search for the phone alone
        (every provider's channels, films and series), the TV's picture as it was."""
        self.show()
        remote = self.window.remote
        remote.bridge.post(Opened("s1", "d1", "home"))
        remote.bridge.post(Query("s1", "Channel"))

        def results():  # to that phone alone: another one's Search is its own
            found = [m for m in self.sent("s1") if m["t"] == "results"]
            return found[-1] if found and found[-1]["groups"] else None

        self.assertTrue(wait_until(lambda: results() is not None, timeout_ms=10000))
        self.assertFalse(self.window.screens.library_shown())  # the TV never searched
        message = results()
        self.assertEqual(message["q"], "Channel")
        self.assertIn("Channel 1", [i["title"] for i in message["groups"][0]["items"]])

    def test_search_query_mirrors_results(self):
        self.show()
        remote = self.window.remote
        remote.bridge.post(Opened("s1", "d1", "home"))
        remote.bridge.post(Query("s1", "Channel", tv=True))  # Show on TV
        self.assertTrue(wait_until(self.window.screens.library_shown))

        def results():
            found = [
                c.args[0] for c in self.hub.broadcast.call_args_list if c.args[0]["t"] == "results"
            ]
            return found[-1] if found and found[-1]["groups"] else None

        self.assertTrue(wait_until(lambda: results() is not None, timeout_ms=10000))
        message = results()
        self.assertEqual(message["q"], "Channel")
        group = message["groups"][0]
        self.assertEqual(group["kind"], "live")
        self.assertIn("Channel 1", [item["title"] for item in group["items"]])
        self.assertLessEqual(len(group["items"]), 30)

    def test_closed_phones_get_no_pushes(self):
        remote = self.window.remote
        remote.bridge.post(Opened("s1", "d1", "home"))
        QTest.qWait(50)
        from services.remote.protocol import Closed

        remote.bridge.post(Closed("s1", "d1"))
        QTest.qWait(50)
        self.hub.broadcast.reset_mock()
        QTest.qWait(700)  # past a diff tick
        self.assertFalse(self.hub.broadcast.called)


if __name__ == "__main__":
    unittest.main()


class ChangeTests(unittest.TestCase):
    def snapshot(self, pos, playing=True, volume=50):
        now = {"kind": "movies", "name": "Harbour Lights", "pos_ms": pos, "len_ms": 6_000_000}
        now["playing"] = playing
        return {"t": "state", "now": now, "volume": volume}

    def test_a_playing_films_clock_is_no_change(self):
        from ui.remote_controller import _changed

        self.assertFalse(_changed(self.snapshot(10_000), self.snapshot(11_000), 1_000))
        self.assertFalse(_changed(self.snapshot(10_000), self.snapshot(12_500), 1_000))

    def test_a_seek_or_another_change_is(self):
        from ui.remote_controller import _changed

        self.assertTrue(_changed(self.snapshot(10_000), self.snapshot(70_000), 1_000))
        self.assertTrue(_changed(self.snapshot(10_000), self.snapshot(10_000, volume=55), 0))
        self.assertTrue(_changed(self.snapshot(10_000), self.snapshot(10_000, playing=False), 0))
        paused = self.snapshot(10_000, playing=False)
        self.assertFalse(_changed(paused, self.snapshot(10_000, playing=False), 5_000))
