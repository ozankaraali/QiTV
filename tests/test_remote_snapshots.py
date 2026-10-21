"""The phone remote's second screen, QiTV's side: what's on, the guide, search results and the
Library, as plain dicts with opaque handles (ui/remote_snapshots.py), and what a phone's tap on a
handle does (ui/remote_controller.py)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import unittest
from unittest import mock

from PySide6.QtTest import QTest
from test_tv_window import WindowCase, wait_until

from services.library import continue_items
from services.remote.protocol import Get, Opened, OpenItem, Press, Query
from services.watch_state import WatchState
from ui.remote_snapshots import Handles, guide_page, library_lists, state

MOVIE = {"stream_id": 7001, "name": "Harbour Lights", "year": "1998"}


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

    def test_state_before_anything_plays(self):
        snapshot = state(self.window)
        self.assertIsNone(snapshot["now"])

    def test_guide_page_paginates_and_marks_playing(self):
        self.open_first_channel()
        handles = Handles()
        page = guide_page(self.window, "all", 0, handles)
        self.assertEqual(page["t"], "guide")
        self.assertEqual((page["list"], page["page"]), ("all", 0))
        self.assertGreaterEqual(page["pages"], 1)
        self.assertLessEqual(len(page["rows"]), 50)
        playing = [row for row in page["rows"] if row["playing"]]
        self.assertEqual([row["name"] for row in playing], ["Channel 1"])
        row = page["rows"][0]
        self.assertEqual(set(row), {"id", "num", "name", "now", "next", "playing"})
        self.assertNotIn("http", json.dumps(page))
        self.assertEqual(guide_page(self.window, "all", 99, handles)["rows"], [])

    def test_guide_lists_without_a_channel_list(self):
        page = guide_page(self.window, "favorites", 0, Handles())
        self.assertEqual(page["rows"], [])

    def test_open_handle_tunes_the_channel(self):
        self.open_first_channel()
        remote = self.window.remote
        page = guide_page(self.window, "all", 0, remote.handles)
        second = next(row for row in page["rows"] if row["name"] == "Channel 2")
        remote.bridge.post(OpenItem("s1", second["id"]))
        controller = self.window.controller
        self.assertTrue(wait_until(lambda: (controller._channel or {}).get("name") == "Channel 2"))

    def test_handles_are_capped(self):
        handles = Handles(limit=2000)
        ids = [handles.issue("channel", {"n": i}) for i in range(2100)]
        self.assertIsNone(handles.get(ids[0]))
        self.assertIsNone(handles.get(ids[99]))
        self.assertEqual(handles.get(ids[100]), ("channel", {"n": 100}))
        self.assertEqual(handles.get(ids[-1]), ("channel", {"n": 2099}))
        self.assertIsNone(handles.get("made-up"))


class LibraryShelfTests(WindowCase):
    def prepare_config(self):
        super().prepare_config()
        provider = "Local fixture"
        cid = WatchState.content_id(provider, MOVIE, item_type="movie")
        self.config.watch_history = [
            {
                "content_id": cid,
                "item_data": MOVIE,
                "item_type": "movie",
                "provider_name": provider,
                "timestamp": 100.0,
                "name": MOVIE["name"],
            }
        ]
        self.config.playback_positions = {cid: {"position": 600_000, "duration": 6_000_000}}

    def test_library_lists_shape(self):
        lists = library_lists(self.window, Handles())
        self.assertEqual(lists["t"], "library")
        [item] = lists["continue"]
        self.assertEqual(item["title"], "Harbour Lights")
        self.assertAlmostEqual(item["progress"], 0.1)
        self.assertEqual(set(item), {"id", "title", "sub", "progress"})
        self.assertIn("mylist", lists)

    def test_extracted_library_functions_match_model(self):
        model = self.window.screens.library_model
        model.set_section("movies")
        watch = self.window.controller.watch
        for providers in (model._history_providers(), {"Local fixture"}, set()):
            with mock.patch.object(model, "_history_providers", return_value=providers):
                self.assertEqual(
                    model._continue_items(), continue_items(self.config, watch, providers, "movie")
                )
        self.assertEqual(len(continue_items(self.config, watch, {"Local fixture"}, "movie")), 1)


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
        remote.bridge.post(Get("s2", "guide", "all", 0))
        remote.bridge.post(Get("s2", "library", "", 0))
        self.assertTrue(
            wait_until(lambda: {m["t"] for m in self.sent("s2")} >= {"guide", "library"})
        )

    def test_search_query_mirrors_results(self):
        self.show()
        remote = self.window.remote
        remote.bridge.post(Opened("s1", "d1", "home"))
        remote.bridge.post(Query("s1", "Channel"))
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


class HandlePoolTests(WindowCase):
    def test_search_results_do_not_evict_guide_handles(self):
        remote = self.window.remote
        guide_handle = remote.handles.issue("channel", {"name": "Channel 1"})
        for i in range(2100):
            remote.result_handles.issue("hit", i)
        self.assertEqual(remote.handles.get(guide_handle), ("channel", {"name": "Channel 1"}))


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
