import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtTest import QTest
from test_tv_window import WindowCase, wait_until

from services.remote.protocol import (
    Answered,
    CastTo,
    Closed,
    Code,
    Get,
    Held,
    Opened,
    Paired,
    Position,
    Press,
    Query,
    RecAct,
    RefreshLists,
    Status,
    StoreSaved,
    ToTv,
    WatchHere,
)
from services.search import Hit
from ui.keymap import Action
from ui.library.search_session import SearchSession
from ui.link_lanes import Target
from ui.remote_controller import PHONE_GONE_TEXT


class RemoteDispatchTests(WindowCase):
    def remote(self):
        return self.window.remote

    def test_arrow_moves_like_keyboard_in_drawer(self):
        self.show()
        drawer = self.wait_for_drawer()  # no last channel: the drawer opens on the groups
        self.remote().fire("ok")  # into the highlighted group, as Enter does

        def on_an_item():
            node = drawer.highlighted()
            return node is not None and node.layer == "item"

        self.assertTrue(wait_until(on_an_item))
        self.assertEqual(drawer.highlighted().title, "Channel 1")
        self.remote().fire("down")
        self.assertTrue(wait_until(lambda: drawer.highlighted().title == "Channel 2"))
        QTest.keyClick(self.window, Qt.Key.Key_Up)  # the keyboard agrees on where it is
        self.assertTrue(wait_until(lambda: drawer.highlighted().title == "Channel 1"))

    def test_volume_in_library_stays_in_library(self):
        self.show()
        self.window.screens.open_library("movies")
        self.assertTrue(wait_until(self.window.screens.library_shown))
        controller = self.window.controller
        with mock.patch.object(controller, "handle", wraps=controller.handle) as handle:
            self.remote().fire("volup")
        handle.assert_called_once_with(Action.VOL_UP, "")
        self.assertTrue(self.window.screens.library_shown())

    def test_channel_in_library_returns_to_tv(self):
        self.show()
        self.window.screens.open_library("movies")
        self.assertTrue(wait_until(self.window.screens.library_shown))
        self.remote().fire("chdown")
        self.assertTrue(wait_until(lambda: not self.window.screens.library_shown()))

    def test_pip_closes_the_small_picture_whatever_the_tv_shows(self):
        self.show()
        self.wait_for_drawer()  # the channel list is open: P there opens its highlighted channel
        controller = self.window.controller
        with (
            mock.patch.object(controller, "pip_shown", return_value=True),
            mock.patch.object(controller, "close_pip") as close,
            mock.patch.object(controller, "handle") as handle,
        ):
            self.remote().fire("pip")
        close.assert_called_once_with()
        handle.assert_not_called()

    def test_pip_opens_as_the_p_key_while_none_is_open(self):
        self.show()
        controller = self.window.controller
        with mock.patch.object(controller, "handle") as handle:
            self.remote().fire("pip")
        handle.assert_called_once_with(Action.PIP, "")

    def test_fullscreen_brings_qitv_forward_first_from_the_background(self):
        """macOS ignores a fullscreen change asked of a window in the background (the phone's
        button while another app is in front): QiTV comes forward, then changes."""
        window = self.window
        active = [False]
        with (
            mock.patch("ui.tv_window.app_active", side_effect=lambda: active[0]),
            mock.patch.object(window, "activateWindow") as activate,
            mock.patch.object(window, "toggle_fullscreen") as toggle,
        ):
            self.remote().fire("fullscreen")
            activate.assert_called_once()
            toggle.assert_not_called()  # not yet: macOS would drop it
            active[0] = True
            self.assertTrue(wait_until(lambda: toggle.called))
        toggle.assert_called_once()

    def test_fullscreen_from_the_background_changes_even_if_qitv_stays_behind(self):
        window = self.window
        with (
            mock.patch("ui.tv_window.app_active", return_value=False),
            mock.patch.object(window, "activateWindow"),
            mock.patch.object(window, "toggle_fullscreen") as toggle,
        ):
            self.remote().fire("fullscreen")
            self.assertTrue(wait_until(lambda: toggle.called, timeout_ms=3000))
        toggle.assert_called_once()

    def test_fullscreen_in_front_changes_at_once(self):
        window = self.window
        with (
            mock.patch("ui.tv_window.app_active", return_value=True),
            mock.patch.object(window, "activateWindow") as activate,
            mock.patch.object(window, "toggle_fullscreen") as toggle,
        ):
            self.remote().fire("fullscreen")
        toggle.assert_called_once()
        activate.assert_not_called()

    def test_movies_and_tv_screens(self):
        self.show()
        self.remote().fire("movies")
        self.assertTrue(wait_until(self.window.screens.library_shown))
        self.assertEqual(self.window.library_controller.section, "movies")
        self.remote().fire("tv")
        self.assertTrue(wait_until(lambda: not self.window.screens.library_shown()))

    def test_tv_leaves_the_guide_for_the_picture(self):
        """The phone's TV from the TV's guide (or its channel list): back to the picture, as
        Back does; it only left the Library before."""
        self.open_first_channel()
        controller = self.window.controller
        self.remote().fire("guide")
        self.assertTrue(wait_until(lambda: controller.key_context() == "guide"))
        self.remote().fire("tv")
        self.assertTrue(wait_until(lambda: controller.key_context() == "player"))

    def test_a_press_from_another_thread_reaches_the_gui(self):
        self.show()
        self.wait_for_drawer()
        fired = []
        self.remote().fire = fired.append  # what a press turns into
        bridge = self.remote().bridge
        thread = threading.Thread(target=bridge.post, args=(Press("s1", "ok", True, 1),))
        thread.start()
        thread.join()
        self.assertTrue(wait_until(lambda: fired == ["ok"]))


class RemoteEventTests(WindowCase):
    def deliver(self, event):
        self.window.remote.bridge.post(event)
        QTest.qWait(10)

    def test_remote_off_by_default_starts_nothing(self):
        self.window.remote.apply_config()
        self.assertIsNone(self.window.remote.hub)

    def test_store_saved_persists_config(self):
        data = {"version": 1, "enabled": False, "devices": [], "relay": True}
        self.deliver(StoreSaved(data))
        self.assertEqual(self.window.config_manager.remote, data)
        saved = json.loads(Path(self.window.config_manager.config_path).read_text())
        self.assertEqual(saved["remote"], data)

    def test_devices_and_code_and_status_signals(self):
        got = {}
        remote = self.window.remote
        remote.devicesChanged.connect(lambda devices: got.setdefault("devices", devices))
        remote.codeChanged.connect(lambda url: got.setdefault("code", url))
        remote.statusChanged.connect(lambda status: got.setdefault("status", status))
        device = {"id": "d1", "name": "Phone A", "paired_at": 1.0, "last_used": 2.0, "keys": []}
        self.deliver(StoreSaved({"version": 1, "devices": [device]}))
        self.deliver(Code("http://192.168.1.20:5384/#1.-.a.b"))
        self.deliver(Status("http://192.168.1.20:5384/", "off"))
        self.assertEqual(got["devices"], [{"id": "d1", "name": "Phone A", "last_used": 2.0}])
        self.assertEqual(got["code"], "http://192.168.1.20:5384/#1.-.a.b")
        self.assertEqual(got["status"], {"home_url": "http://192.168.1.20:5384/", "relay": "off"})

    def test_paired_toast(self):
        self.deliver(Paired("s1", "d1", "Phone A"))
        self.assertIn(("Phone A can now control QiTV", "info"), self.toasts)

    def test_closed_drops_the_hold(self):
        fired = []
        remote = self.window.remote
        remote.fire = fired.append
        self.deliver(Press("s1", "down", True, 1))
        self.deliver(Closed("s1", "d1"))
        QTest.qWait(700)
        self.assertEqual(fired, ["down"])

    def test_hold_repeats_on_the_gui_thread(self):
        fired = []
        remote = self.window.remote
        remote.fire = fired.append
        alive = QTimer()  # the phone's keep-alive while the button is down
        alive.setInterval(100)
        alive.timeout.connect(lambda: remote.bridge.post(Held("s1")))
        self.deliver(Press("s1", "down", True, 1))
        alive.start()
        QTest.qWait(530)  # one press, then from 400 ms one every 40 ms
        alive.stop()
        remote.bridge.post(Press("s1", "down", False, 5))
        self.assertTrue(wait_until(lambda: len(fired) >= 5))
        QTest.qWait(200)
        # Where the phone let go, however late this machine's timers ran: overshoot steps back.
        self.assertEqual(fired.count("down") - fired.count("up"), 5)
        self.assertTrue(set(fired) <= {"down", "up"})


class ReviewFixControllerTests(WindowCase):
    def deliver(self, event):
        self.window.remote.bridge.post(event)
        QTest.qWait(10)

    def test_store_saved_keeps_the_settings_switches(self):
        device = {"id": "d1", "name": "Phone A", "paired_at": 1.0, "last_used": 2.0, "keys": []}
        self.window.config_manager.remote = {
            "version": 1,
            "enabled": True,
            "relay": False,
            "relay_url": "https://relay.example",
        }
        self.deliver(
            StoreSaved(
                {
                    "version": 1,
                    "enabled": False,
                    "relay": True,
                    "relay_url": "https://remote.qitv.app",
                    "devices": [device],
                }
            )
        )
        saved = self.window.config_manager.remote
        self.assertEqual((saved["enabled"], saved["relay"]), (True, False))
        self.assertEqual(saved["relay_url"], "https://relay.example")
        self.assertEqual(saved["devices"], [device])

    def test_removal_toast_waits_for_the_hub(self):
        from unittest import mock as _mock

        device = {"id": "d1", "name": "Phone A", "paired_at": 1.0, "last_used": 2.0, "keys": []}
        self.window.config_manager.remote = {"version": 1, "devices": [device]}
        self.window.remote.apply_config = lambda: None
        self.window.remote._names_from(self.window.config_manager.remote)
        self.window.remote.hub = _mock.Mock()
        self.addCleanup(setattr, self.window.remote, "hub", None)
        self.window.remote.remove_device("d1")
        self.assertNotIn(("Phone A can no longer control QiTV", "info"), self.toasts)
        self.deliver(StoreSaved({"version": 1, "devices": []}))
        self.assertIn(("Phone A can no longer control QiTV", "info"), self.toasts)


if __name__ == "__main__":
    unittest.main()


class PhoneQuestionTests(WindowCase):
    """2.6: the phone's Watch with no stream free asks on that phone (QiTV's words); its
    answer reaches the TV, a stale or a gone phone's answers keep the recording."""

    def connect(self):
        remote = self.window.remote
        self.hub = mock.Mock()
        remote.hub = self.hub
        self.addCleanup(setattr, remote, "hub", None)
        remote._phones.add("s8")
        self.addCleanup(remote._phones.discard, "s8")

    def sent(self, kind):
        calls = self.hub.send.call_args_list
        return [c.args[1] for c in calls if c.args[0] == "s8" and c.args[1]["t"] == kind]

    def ask(self):
        self.connect()
        self.window.controller.phoneAsked.emit("s8", "Q?", "Stop recording", "Keep recording")
        [ask] = self.sent("ask")
        return ask

    def test_a_question_goes_to_the_phone_that_asked(self):
        ask = self.ask()
        self.assertEqual(
            (ask["text"], ask["yes"], ask["no"]), ("Q?", "Stop recording", "Keep recording")
        )
        self.assertIsInstance(ask["id"], int)

    def test_its_answer_reaches_the_tv_once(self):
        ask = self.ask()
        controller = self.window.controller
        with mock.patch.object(controller, "answer_phone") as answer:
            self.window.remote.bridge.post(Answered("s8", ask["id"] + 1, True))  # stale
            self.window.remote.bridge.post(Answered("s8", ask["id"], True))
            self.window.remote.bridge.post(Answered("s8", ask["id"], False))  # once
            self.assertTrue(wait_until(lambda: answer.called))
            QTest.qWait(50)
        answer.assert_called_once_with(True)

    def test_keep_recording_tells_the_phone_nothing_opened(self):
        ask = self.ask()
        with mock.patch.object(self.window.controller, "answer_phone"):
            self.window.remote.bridge.post(Answered("s8", ask["id"], False))
            self.assertTrue(wait_until(lambda: self.sent("play")))
        self.assertEqual(self.sent("play"), [{"t": "play", "failed": True, "why": "recording"}])

    def test_a_phone_that_leaves_keeps_the_recording(self):
        self.ask()
        with mock.patch.object(self.window.controller, "answer_phone") as answer:
            self.window.remote.bridge.post(Closed("s8", None))
            self.assertTrue(wait_until(lambda: answer.called))
        answer.assert_called_once_with(False)

    def test_a_question_settled_on_the_tv_closes_on_the_phone(self):
        ask = self.ask()
        self.window.controller.phoneSettled.emit("s8")
        self.assertEqual(self.sent("unask"), [{"t": "unask", "id": ask["id"]}])
        # A newer question (the TV's, another phone's) stands for it: nothing opens there,
        # and the phone says so instead of "Opening on this phone…".
        self.assertEqual(self.sent("play"), [{"t": "play", "failed": True, "why": "elsewhere"}])

    def test_an_answered_question_closes_without_more(self):
        ask = self.ask()
        with mock.patch.object(self.window.controller, "answer_phone") as answer:
            answer.side_effect = lambda yes: self.window.controller.phoneSettled.emit("s8")
            self.window.remote.bridge.post(Answered("s8", ask["id"], True))
            self.assertTrue(wait_until(lambda: answer.called))
        self.assertEqual((self.sent("unask"), self.sent("play")), ([], []))

    def test_the_rec_line_goes_back_or_stops(self):
        self.connect()
        controller = self.window.controller
        with (
            mock.patch.object(controller, "go_back_to_recording") as back,
            mock.patch.object(controller, "stop_background_recording") as stop,
        ):
            self.window.remote.bridge.post(RecAct("s8", "b1", "back"))
            self.window.remote.bridge.post(RecAct("s8", "b2", "stop"))
            self.assertTrue(wait_until(lambda: stop.called))
        back.assert_called_once_with("b1")
        stop.assert_called_once_with("b2")

    def cast_controls(self):  # 2.8: one device found, nothing cast yet
        controls = mock.Mock()
        controls.menu_entries.return_value = [("This computer", "", True), ("Den", "Den", False)]
        controls.device.return_value = None
        return controls

    def test_the_phone_lists_the_devices_to_cast_to(self):  # 2.8
        self.connect()
        controls = self.cast_controls()
        with mock.patch.object(self.window.controller, "cast_controls", return_value=controls):
            self.window.remote.bridge.post(Get("s8", "cast", "", 0))
            self.assertTrue(wait_until(lambda: self.sent("cast")))
        self.assertEqual(
            self.sent("cast"), [{"t": "cast", "devices": [{"name": "Den", "on": False}], "on": ""}]
        )

    def test_the_phone_hears_when_casting_is_not_available(self):  # 2.8
        self.connect()
        controls = mock.Mock()
        controls.menu_entries.return_value = [("Casting isn't available", None, False)]
        controls.device.return_value = None
        controls.available.return_value = False
        with mock.patch.object(self.window.controller, "cast_controls", return_value=controls):
            self.window.remote.bridge.post(Get("s8", "cast", "", 0))
            self.assertTrue(wait_until(lambda: self.sent("cast")))
        self.assertEqual(self.sent("cast"), [{"t": "cast", "devices": [], "on": "", "off": True}])

    def test_the_phone_casts_and_stops_casting(self):  # 2.8
        self.connect()
        controls = self.cast_controls()
        with mock.patch.object(self.window.controller, "cast_controls", return_value=controls):
            self.window.remote.bridge.post(CastTo("s8", "Den"))
            self.window.remote.bridge.post(CastTo("s8", ""))
            self.assertTrue(wait_until(lambda: controls.stop.called))
        controls.cast_to.assert_called_once_with("Den")
        self.assertEqual(self.hub.broadcast.call_count, 2)  # every phone's list follows

    def test_casting_that_fails_leaves_qitv_running(self):  # 2.8
        self.connect()
        controls = self.cast_controls()
        controls.cast_to.side_effect = RuntimeError("no renderer")
        with (
            mock.patch.object(self.window.controller, "cast_controls", return_value=controls),
            self.assertLogs("ui.remote_controller", "ERROR"),
        ):
            self.window.remote.bridge.post(CastTo("s8", "Den"))
            self.assertTrue(wait_until(lambda: self.hub.broadcast.called))

    def test_watch_asks_on_that_phone(self):
        self.connect()
        asked_on = []

        def play_elsewhere(launch, chosen=None, *, ask_on=None, recordings=False):
            asked_on.append(ask_on)
            self.assertTrue(recordings)  # 2.8: the TV's recording goes to the phone too
            return True

        with mock.patch.object(
            self.window.controller, "play_elsewhere", side_effect=play_elsewhere
        ):
            self.window.remote.bridge.post(WatchHere("s8"))
            self.assertTrue(wait_until(lambda: asked_on))
        self.assertEqual(asked_on, ["s8"])


class FakeSearcher(QObject):
    """The remote's SearchSession as the phones' own Search uses it."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self.query, self.hits, self.searching, self.asked = "", None, False, []

    def set_query(self, text):
        self.query, self.searching = text, True
        self.asked.append(text)

    def land(self, hits):
        self.hits, self.searching = hits, False
        self.changed.emit()

    def clear(self):
        self.query, self.hits, self.searching = "", None, False


class QuietSearchTests(WindowCase):
    """A phone's own Search (the TV's picture stays) answers that phone: two phones searching
    at once each get what their own text finds, one after the other."""

    def setUp(self):
        super().setUp()
        remote = self.window.remote
        self.hub = mock.Mock()
        remote.hub = self.hub
        self.addCleanup(setattr, remote, "hub", None)
        for sid in ("s1", "s2"):
            remote._phones.add(sid)
            self.addCleanup(remote._phones.discard, sid)
        self.searcher = FakeSearcher()
        self.searcher.changed.connect(remote._push_quiet_results)
        remote._title_searcher = self.searcher
        self.addCleanup(setattr, remote, "_title_searcher", None)

    def answers(self, sid):
        """The texts of the results `sid` received, and the titles of the last."""
        sent = [c.args[1] for c in self.hub.send.call_args_list if c.args[0] == sid]
        sent += [c.args[0] for c in self.hub.broadcast.call_args_list]
        found = [m for m in sent if m["t"] == "results"]
        titles = [i["title"] for g in found[-1]["groups"] for i in g["items"]] if found else []
        return [m["q"] for m in found], titles

    def hit(self, title):
        return Hit("P", "movies", title, {"id": title, "name": title})

    def test_two_phones_searching_at_once_each_get_their_own(self):
        remote = self.window.remote
        remote.bridge.post(Query("s1", "dune"))
        remote.bridge.post(Query("s2", "alien"))
        self.assertEqual(self.searcher.asked, ["dune", "alien"])
        self.searcher.land([self.hit("Alien")])
        self.assertEqual(self.answers("s2"), (["alien"], ["Alien"]))
        self.assertEqual(self.answers("s1"), ([], []))  # not another phone's text
        self.assertEqual(self.searcher.asked[-1], "dune")  # its turn now
        self.searcher.land([self.hit("Dune")])
        self.assertEqual(self.answers("s1"), (["dune"], ["Dune"]))
        self.assertEqual(self.answers("s2"), (["alien"], ["Alien"]))

    def test_a_phone_clearing_its_text_leaves_the_others_search(self):
        remote = self.window.remote
        remote.bridge.post(Query("s1", "dune"))
        remote.bridge.post(Query("s2", ""))
        self.assertEqual(self.answers("s2"), ([""], []))
        self.assertEqual(self.answers("s1"), ([], []))
        self.assertEqual(self.searcher.query, "dune")  # still searched for
        self.searcher.land([self.hit("Dune")])
        self.assertEqual(self.answers("s1"), (["dune"], ["Dune"]))

    def test_a_phone_gone_is_searched_for_no_more(self):
        remote = self.window.remote
        remote.bridge.post(Query("s1", "dune"))
        remote.bridge.post(Query("s2", "alien"))
        remote.bridge.post(Closed("s1", "d1"))
        self.searcher.land([self.hit("Alien")])
        self.assertEqual(self.searcher.asked, ["dune", "alien"])


class WatchHereTests(WindowCase):
    """The phone's Watch on this phone: QiTV hands it the link of what the TV plays, or of a
    channel of its own list, as Play in VLC hands one; the TV's stream closed first, so the
    provider counts one. One screen at a time: the TV playing again stops the phone."""

    def connect(self):
        remote = self.window.remote
        self.hub = mock.Mock()
        remote.hub = self.hub
        self.addCleanup(setattr, remote, "hub", None)
        remote._phones.add("s8")
        self.addCleanup(remote._phones.discard, "s8")

    def sent(self, kind):
        calls = self.hub.send.call_args_list
        return [c.args[1] for c in calls if c.args[0] == "s8" and c.args[1]["t"] == kind]

    def plays(self):
        return self.sent("play")

    def hand_off(self, url, gone=False, handle=""):
        """The TV's controller handing `url` on at once, as it does once the link is in."""
        target = Target({"name": "News", "cmd": url}, "live", "News")
        results = []

        def play_elsewhere(launch, chosen=None, **_kwargs):
            if gone:
                self.window.remote._phones.discard("s8")
            results.append(launch(chosen or target, url))
            return True

        controller = self.window.controller
        with (
            mock.patch.object(controller, "play_elsewhere", side_effect=play_elsewhere),
            mock.patch.object(self.window.screens, "notify") as notify,
        ):
            self.window.remote.bridge.post(WatchHere("s8", handle))
            self.assertTrue(wait_until(lambda: results))
        return results[0], notify

    def test_hands_the_phone_the_link_and_the_tv_says_so(self):
        self.connect()
        result, notify = self.hand_off("http://p.test/news.m3u8")
        self.assertEqual(result, (True, ""))
        [play] = self.plays()
        self.assertEqual((play["url"], play["name"]), ("http://p.test/news.m3u8", "News"))
        notify.assert_called_once_with("Playing on the phone")

    def test_a_phone_gone_before_its_link_came(self):
        self.connect()
        result, notify = self.hand_off("http://p.test/news.m3u8", gone=True)
        self.assertEqual(result, (False, PHONE_GONE_TEXT))
        self.assertEqual(self.plays(), [])
        notify.assert_not_called()

    def test_nothing_playing_tells_the_phone(self):
        self.connect()
        self.window.remote.bridge.post(WatchHere("s8"))
        self.assertTrue(wait_until(self.plays))
        self.assertEqual(self.plays()[-1], {"t": "play", "failed": True})

    def listed_row(self, index):
        self.open_first_channel()
        self.connect()
        listed = self.window.remote._channel_list("s8")
        listed.channels("", 0, "")
        self.assertTrue(
            wait_until(lambda: self.sent("channels") and self.sent("channels")[-1]["rows"])
        )
        return self.sent("channels")[-1]["rows"][index]

    def test_a_channel_of_the_phones_list_plays_there(self):
        row = self.listed_row(1)
        self.hand_off("http://p.test/two.m3u8", handle=row["id"])
        play = self.plays()[-1]
        self.assertEqual((play["url"], play["name"]), ("http://p.test/two.m3u8", "Channel 2"))

    def test_a_handle_the_phone_never_had(self):
        self.connect()
        self.window.remote.bridge.post(WatchHere("s8", "nope"))
        self.assertTrue(wait_until(self.plays))
        self.assertEqual(self.plays()[-1], {"t": "play", "failed": True})

    def test_the_phones_refresh_loads_the_librarys_lists_again(self):  # 2.8
        self.connect()
        controller = self.window.screens.library_controller
        with mock.patch.object(controller, "refresh_lists") as refresh:
            self.window.remote.bridge.post(RefreshLists("s8"))
            self.assertTrue(wait_until(lambda: refresh.called))

    def test_back_to_the_tv_tunes_what_the_phone_played(self):
        row = self.listed_row(1)
        self.hand_off("http://p.test/two.m3u8", handle=row["id"])
        with mock.patch.object(self.window.screens, "tune_live") as tune:
            self.window.remote.bridge.post(ToTv("s8"))
            self.assertTrue(wait_until(lambda: tune.called))
        channel = tune.call_args.args[0]
        self.assertEqual(channel["name"], "Channel 2")
        self.assertEqual(self.sent("stop"), [])  # the phone let go itself

    def test_the_tv_playing_again_stops_the_phone(self):
        self.connect()
        self.hand_off("http://p.test/news.m3u8")
        remote = self.window.remote
        with mock.patch.object(self.window.controller, "showing_here", return_value=True):
            remote._push_state()
            remote._push_state()  # once
        self.assertEqual(self.sent("stop"), [{"t": "stop"}])

    def test_a_phone_that_slept_hands_back_from_its_new_session(self):
        row = self.listed_row(1)
        remote = self.window.remote
        remote._devices["s8"] = "d1"
        self.hand_off("http://p.test/two.m3u8", handle=row["id"])
        remote._devices["s9"] = "d1"  # the same phone, connected again
        remote._phones.add("s9")
        self.addCleanup(remote._phones.discard, "s9")
        with mock.patch.object(self.window.screens, "tune_live") as tune:
            remote.bridge.post(ToTv("s9"))
            self.assertTrue(wait_until(lambda: tune.called))
        self.assertEqual(tune.call_args.args[0]["name"], "Channel 2")

    def test_a_film_of_the_phones_library_plays_there_and_goes_back(self):
        self.connect()
        remote = self.window.remote
        film = {"id": 7, "name": "Harbour Lights", "cmd": "http://p.test/movie/u/p/7.mp4"}
        handle = remote._library("s8").handles.issue("title", (film, "movie", None))
        self.hand_off("http://p.test/movie/u/p/7.mp4", handle=handle)
        play = self.plays()[-1]
        self.assertEqual((play["name"], "num" in play), ("Harbour Lights", False))
        with mock.patch.object(self.window.controller, "play_vod") as play_vod:
            remote.bridge.post(ToTv("s8"))
            self.assertTrue(wait_until(lambda: play_vod.called))
        item, kind = play_vod.call_args.args[:2]
        self.assertEqual((item["name"], kind), ("Harbour Lights", "movie"))

    def test_a_film_starts_on_the_phone_where_it_stopped(self):
        """A film half watched goes on from there on the phone (its saved place travels with
        the link), not from the start, where the phone's first report would lose it."""
        self.connect()
        remote = self.window.remote
        film = {"id": 7, "name": "Harbour Lights", "cmd": "http://p.test/movie/u/p/7.mp4"}
        handle = remote._library("s8").handles.issue("title", (film, "movie", None))
        with mock.patch.object(self.window.watch, "resume_position", return_value=4_320_000):
            self.hand_off("http://p.test/movie/u/p/7.mp4", handle=handle)
        self.assertEqual(self.plays()[-1]["start"], 4320)

    def test_a_failed_hand_off_tells_the_phone(self):
        self.connect()
        with mock.patch.object(self.window.controller, "play_elsewhere", side_effect=RuntimeError):
            self.window.remote.bridge.post(WatchHere("s8"))
            self.assertTrue(wait_until(self.plays))
        self.assertEqual(self.plays()[-1], {"t": "play", "failed": True})

    def test_the_finds_let_go_when_nobody_finds(self):
        """As the desktop's find does once it ends: no phone's list finds anything, the shared
        find lets its text (and its index, in a while) go."""
        self.connect()
        remote = self.window.remote
        remote.bridge.post(Get("s8", "chgroups", "", 0, find="channel"))
        self.assertTrue(wait_until(lambda: remote._finder().query == "channel"))
        remote.bridge.post(Get("s8", "chgroups", "", 0, find=""))
        self.assertTrue(wait_until(lambda: remote._finder().query == ""))

    def test_how_far_the_phone_got_in_a_film_is_kept_as_the_tvs(self):
        self.connect()
        remote = self.window.remote
        film = {"id": 7, "name": "Harbour Lights", "cmd": "http://p.test/movie/u/p/7.mp4"}
        handle = remote._library("s8").handles.issue("title", (film, "movie", None))
        self.hand_off("http://p.test/movie/u/p/7.mp4", handle=handle)
        key = self.window.controller.resume_key(self.window.controller.title_target(film, "movie"))
        with mock.patch.object(self.window.watch, "update_position") as update:
            remote.bridge.post(Position("s8", 61000, 5400000, self.plays()[-1]["p"]))
            self.assertTrue(wait_until(lambda: update.called))
        update.assert_called_once_with(key, 61000, 5400000)

    def test_a_report_made_for_the_title_before_is_not_the_new_ones(self):
        """The phone zapped to another film: a report of the first still on its way when the
        second was handed over is the first one's (each play has its number), not kept."""
        self.connect()
        remote, controller = self.window.remote, self.window.controller
        films = [
            {"id": n, "name": f"Harbour {n}", "cmd": f"http://p.test/movie/u/p/{n}.mp4"}
            for n in (7, 8)
        ]
        plays = []
        for film in films:
            handle = remote._library("s8").handles.issue("title", (film, "movie", None))
            self.hand_off(film["cmd"], handle=handle)
            plays.append(self.plays()[-1]["p"])
        self.assertNotEqual(plays[0], plays[1])
        second = controller.resume_key(controller.title_target(films[1], "movie"))
        with mock.patch.object(self.window.watch, "update_position") as update:
            remote.bridge.post(Position("s8", 61000, 5400000, plays[0]))
            remote.bridge.post(Position("s8", 2000, 3600000, plays[1]))
            self.assertTrue(wait_until(lambda: update.called))
        update.assert_called_once_with(second, 2000, 3600000)

    def test_a_page_from_before_play_numbers_keeps_its_positions(self):  # 2.8 review M6
        """A page loaded before QiTV was updated says no play number: its reports are kept as
        2.7 kept them (what plays now is the one it reports on)."""
        self.connect()
        remote, controller = self.window.remote, self.window.controller
        film = {"id": 7, "name": "Harbour Lights", "cmd": "http://p.test/movie/u/p/7.mp4"}
        handle = remote._library("s8").handles.issue("title", (film, "movie", None))
        self.hand_off(film["cmd"], handle=handle)
        key = controller.resume_key(controller.title_target(film, "movie"))
        with mock.patch.object(self.window.watch, "update_position") as update:
            remote.bridge.post(Position("s8", 1000, 3600000))
            self.assertTrue(wait_until(lambda: update.called))
        update.assert_called_once_with(key, 1000, 3600000)

    def test_a_channel_on_the_phone_keeps_no_position(self):
        self.connect()
        self.hand_off("http://p.test/news.m3u8")
        play = self.plays()[-1]["p"]
        with mock.patch.object(self.window.watch, "update_position") as update:
            self.window.remote.bridge.post(Position("s8", 61000, 5400000, play))
            QTest.qWait(200)
        update.assert_not_called()

    def test_the_phones_library_searches_with_the_desktops_search(self):
        remote = self.window.remote
        library = remote._library("s3")
        self.addCleanup(remote._libraries.pop, "s3", None)
        self.assertIs(library._searcher, remote._searcher())
        self.assertIsInstance(remote._searcher(), SearchSession)

    def test_a_search_result_plays_on_the_phone(self):
        self.connect()
        remote = self.window.remote
        film = {"id": 7, "name": "Harbour Lights", "_provider": "P"}
        handle = remote.result_handles.issue("hit", Hit("P", "movies", "Harbour Lights", film))
        self.hand_off("http://p.test/movie/7.mp4", handle=handle)
        self.assertEqual(self.plays()[-1]["name"], "Harbour Lights")

    def test_a_series_does_not_play_on_the_phone(self):
        self.connect()
        remote = self.window.remote
        show = {"id": "s1", "name": "Harbour Watch"}
        handle = remote._library("s8").handles.issue("title", (show, "series", None))
        remote.bridge.post(WatchHere("s8", handle))
        self.assertTrue(wait_until(self.plays))
        self.assertEqual(self.plays()[-1], {"t": "play", "failed": True})

    def test_an_mkv_film_goes_through_qitvs_home_door(self):
        """Safari plays no MKV: QiTV's home door hands it to the phone as HLS, reading it from
        its provider (services/remote/films.py); the phone gets its address from there."""
        self.connect()
        remote = self.window.remote
        film = {"id": 7, "name": "Harbour Lights", "container_extension": "mkv"}
        handle = remote._library("s8").handles.issue("title", (film, "movie", None))
        with mock.patch.object(self.window.watch, "resume_position", return_value=61_000):
            self.hand_off("http://p.test/movie/u/p/7.mkv", handle=handle)
        self.assertEqual(self.plays(), [])
        self.hub.open_film.assert_called_once()
        sid, device, url, headers, verify, message = self.hub.open_film.call_args.args
        self.assertEqual((sid, device, url), ("s8", "s8", "http://p.test/movie/u/p/7.mkv"))
        self.assertTrue(headers["User-Agent"])
        self.assertIs(verify, True)
        self.assertIsInstance(message.pop("p"), int)  # its reports carry it back
        self.assertEqual(message, {"t": "play", "name": "Harbour Lights", "start": 61})

    def test_an_mkv_film_for_a_phone_through_the_relay(self):
        """The relay carries no film: the phone is told to be on QiTV's home network."""
        self.connect()
        remote = self.window.remote
        remote.bridge.post(Opened("s8", "d1", "relay"))
        self.assertTrue(wait_until(lambda: "s8" in remote._relayed))
        film = {"id": 7, "name": "Harbour Lights", "container_extension": "mkv"}
        handle = remote._library("s8").handles.issue("title", (film, "movie", None))
        self.hand_off("http://p.test/movie/u/p/7.mkv", handle=handle)
        self.hub.open_film.assert_not_called()
        self.assertEqual(self.plays()[-1], {"t": "play", "failed": True, "why": "relay"})

    RECORDING = {
        "id": "recording:Harbour News 2026-10-26 20.00.ts",
        "name": "Harbour News",
        "path": "/r/Harbour News 2026-10-26 20.00.ts",
        "_recording": True,
    }

    def hand_recording(self):
        remote = self.window.remote
        item = dict(self.RECORDING)
        handle = remote._library("s8").handles.issue("title", (item, "recording", None))
        self.hand_off(Path(item["path"]).as_uri(), handle=handle)
        return item

    def test_a_recording_goes_through_qitvs_home_door(self):
        """2.8: Safari plays a recording's MPEG-TS as HLS: QiTV's home door hands it to the
        phone from the disk (services/remote/recorded.py), from where it stopped."""
        self.connect()
        watch = self.window.watch
        with mock.patch.object(watch, "resume_position", return_value=61_000) as resume:
            item = self.hand_recording()
        resume.assert_called_with(item["id"])
        self.assertEqual(self.plays(), [])
        message = self.hub.open_recording.call_args.args[3]
        self.assertIsInstance(message.pop("p"), int)
        self.hub.open_recording.assert_called_once_with(
            "s8",
            "s8",
            item["path"],
            {"t": "play", "name": "Harbour News", "start": 61},
            writing=mock.ANY,
        )
        writing = self.hub.open_recording.call_args.kwargs["writing"]
        self.assertFalse(writing())  # 2.8 final review 8: the TV says whether it is written
        engine = mock.Mock(writing=frozenset({item["path"]}))
        with mock.patch.object(self.window, "engine", engine):
            self.assertTrue(writing())

    def test_a_recording_for_a_phone_through_the_relay(self):
        self.connect()
        remote = self.window.remote
        remote.bridge.post(Opened("s8", "d1", "relay"))
        self.assertTrue(wait_until(lambda: "s8" in remote._relayed))
        self.hand_recording()
        self.hub.open_recording.assert_not_called()
        self.assertEqual(self.plays()[-1], {"t": "play", "failed": True, "why": "rec_relay"})

    def test_how_far_the_phone_got_in_a_recording_is_kept_for_the_tv(self):
        self.connect()
        item = self.hand_recording()
        play = self.hub.open_recording.call_args.args[3]["p"]
        with mock.patch.object(self.window.watch, "update_position") as update:
            self.window.remote.bridge.post(Position("s8", 61000, 5400000, play))
            self.assertTrue(wait_until(lambda: update.called))
        update.assert_called_once_with(item["id"], 61000, 5400000)

    def test_a_recording_handed_back_plays_on_the_tv(self):
        self.connect()
        item = self.hand_recording()
        with mock.patch.object(self.window.controller, "play_recording") as play_recording:
            self.window.remote.bridge.post(ToTv("s8"))
            self.assertTrue(wait_until(lambda: play_recording.called))
        play_recording.assert_called_once_with(item)
        self.hub.forget_film.assert_called_with("s8")

    def test_a_phone_at_the_home_door_gets_its_films_there(self):
        self.connect()
        remote = self.window.remote
        remote.bridge.post(Opened("s8", "d1", "home"))
        self.assertTrue(wait_until(lambda: remote._devices.get("s8") == "d1"))
        self.assertNotIn("s8", remote._relayed)

    def test_handing_a_film_back_to_the_tv_lets_the_phones_film_go(self):
        self.connect()
        remote = self.window.remote
        film = {"id": 7, "name": "Harbour Lights", "container_extension": "mkv"}
        handle = remote._library("s8").handles.issue("title", (film, "movie", None))
        self.hand_off("http://p.test/movie/u/p/7.mkv", handle=handle)
        with mock.patch.object(self.window.controller, "play_vod") as play_vod:
            remote.bridge.post(ToTv("s8"))
            self.assertTrue(wait_until(lambda: play_vod.called))
        self.hub.forget_film.assert_called_once_with("s8")

    def test_something_else_on_the_phone_calls_off_its_film(self):
        """A channel (or an MP4) after an MKV still opening: the film comes no more."""
        self.connect()
        self.hand_off("http://p.test/news.m3u8")
        self.hub.forget_film.assert_called_once_with("s8")
        self.assertEqual(self.plays()[-1]["url"], "http://p.test/news.m3u8")

    def test_the_tv_playing_again_ends_the_phones_films(self):
        self.connect()
        self.hand_off("http://p.test/news.m3u8")
        with mock.patch.object(self.window.controller, "showing_here", return_value=True):
            self.window.remote._push_state()
        self.hub.drop_films.assert_called_once_with()

    def test_an_xtream_channel_goes_as_hls(self):
        remote = self.window.remote
        entry = {"type": "XTREAM", "username": "u", "password": "p"}
        with mock.patch.object(self.window.catalog, "provider_entry", return_value=entry):
            link = remote._phone_link({"name": "News"}, "http://p.test/live/u/p/7.ts")
        self.assertEqual(link, "http://p.test/live/u/p/7.m3u8")
