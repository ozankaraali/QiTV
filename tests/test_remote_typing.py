"""2.15: the phone types into QiTV's text boxes, opens the Open Link card, and its Watch of a
site's channel that isn't live waits for it (ui/remote_typing.py, ui/remote_waits.py)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from test_tv_window import WindowCase, wait_until
from timer_fixtures import pause

from services.link_kind import LIVE
from services.provider_link import XTREAM, ProviderLink
from services.remote.protocol import Closed, OpenLinkCard, StoreSaved, ToTv, Typed, WatchHere
from ui import live_controls, remote_controller
from ui.keymap import Action
from ui.link_lanes import Target
from ui.remote_typing import focused_field

KICK_PAGE = "https://kick.com/examplechannel"
OFFLINE = "examplechannel isn't live now"


class PhoneCase(WindowCase):
    def connect(self, sid="s8", device="d8"):
        remote = self.window.remote
        if not isinstance(remote.hub, mock.Mock):
            self.hub = mock.Mock()
            remote.hub = self.hub
            self.addCleanup(setattr, remote, "hub", None)
        remote._phones.add(sid)
        remote._devices[sid] = device
        self.addCleanup(remote._phones.discard, sid)

    def broadcast(self, kind):
        calls = self.hub.broadcast.call_args_list
        return [c.args[0] for c in calls if c.args[0]["t"] == kind]

    def sent(self, kind, sid="s8"):
        calls = self.hub.send.call_args_list
        return [c.args[1] for c in calls if c.args[0] == sid and c.args[1]["t"] == kind]


class TypingTests(PhoneCase):
    def fields(self):
        return self.broadcast("field")

    def test_open_link_from_the_phone_and_typing_into_its_field(self):
        """The phone's Open link opens the card as File → Open Link… does; its field has the
        keys, so the phones hear of it (numbered, with its text), and what one types goes in as
        typed (the found line follows); its Go is Enter: the link plays. A number no longer the
        box's types nothing; the card gone, the phones hear no box has the keys."""
        self.show()
        self.wait_for_drawer()
        self.connect()
        window, card, remote = self.window, self.window.open_link_card, self.window.remote
        remote._on_event(OpenLinkCard("s8"))
        self.assertFalse(card.isHidden())
        remote._push_state()
        [shown] = self.fields()
        number = shown["id"]
        self.assertEqual(shown, {"t": "field", "id": number, "label": "Open a link", "text": ""})
        remote._on_event(Typed("s8", number, KICK_PAGE))
        self.assertEqual(card.values(), {"link": KICK_PAGE})
        self.assertEqual(card.found(), ("✓ A live stream", True))  # as typed: its line follows
        remote._push_state()
        self.assertEqual(len(self.fields()), 1)  # the same box: not told again
        remote._on_event(Typed("s8", number + 1, "elsewhere"))  # never this box's number
        self.assertEqual(card.values(), {"link": KICK_PAGE})
        with mock.patch.object(window, "play_network_link") as play:
            remote._on_event(Typed("s8", number, KICK_PAGE + "/", True))
            self.assertTrue(wait_until(lambda: play.called))
        play.assert_called_once_with(KICK_PAGE, LIVE)
        self.assertTrue(card.isHidden())
        remote._push_state()
        self.assertEqual(self.fields()[-1], {"t": "field"})
        remote._push_state()
        self.assertEqual(self.fields()[-1:], [{"t": "field"}])  # said once

    def test_a_phone_that_comes_hears_of_the_box_there_is(self):
        self.show()
        self.wait_for_drawer()
        self.connect()
        remote = self.window.remote
        self.window.open_link("https://example.test/live.m3u8")
        remote._push_state()
        [shown] = self.fields()
        self.connect("s9", "d9")
        remote._push_field(to="s9")
        self.assertEqual(self.sent("field", "s9"), [shown])

    def test_a_secret_box_is_masked_and_its_text_never_sent(self):
        """The provider Add card's password: the phones hear it is secret, never its text nor
        its length; what is typed goes in; a box the keys move to is another number."""
        self.show()
        self.wait_for_drawer()
        self.connect()
        window, remote = self.window, self.window.remote
        link = ProviderLink(XTREAM, "http://panel.example", "demo", "hunter2")
        window.add_provider(link)
        box = focused_field(window)
        self.assertIsNotNone(box)
        card = box.holder
        for _ in range(4):
            if card.focus()[1] == "password":
                break
            card.handle(Action.NAV_DOWN)
        self.assertEqual(card.focus(), ("field", "password"))
        remote._push_state()
        shown = self.fields()[-1]
        self.assertEqual(
            shown, {"t": "field", "id": shown["id"], "label": "Password", "secret": True}
        )
        self.assertNotIn("hunter2", repr(self.hub.mock_calls))
        remote._on_event(Typed("s8", shown["id"], "correct horse"))
        self.assertEqual(card.values()["password"], "correct horse")
        card.handle(Action.NAV_UP)  # the keys go to the username: another box, numbered anew
        remote._push_state()
        user = self.fields()[-1]
        self.assertEqual((user["label"], user["text"]), ("Username", "demo"))
        self.assertEqual(user["id"], shown["id"] + 1)
        remote._on_event(Typed("s8", shown["id"], "late"))  # typed for the password: nowhere
        self.assertEqual(card.values()["username"], "demo")
        self.assertEqual(card.values()["password"], "correct horse")

    def test_settings_text_edited_in_place_takes_the_phones_text(self):
        self.show()
        self.wait_for_drawer()
        self.connect()
        window, remote = self.window, self.window.remote
        window.open_settings(row="opensubtitles_username")
        settings = window.settings_drawer
        self.assertTrue(wait_until(settings.isVisible))
        self.assertIsNone(focused_field(window))  # its rows have the keys: typing finds
        QTest.keyClick(window, Qt.Key.Key_Return)  # editing the text in place
        self.assertTrue(settings.editing())
        remote._push_state()
        shown = self.fields()[-1]
        self.assertEqual((shown["label"], shown["text"]), ("Username", ""))
        with mock.patch.object(settings, "edited") as edited:
            remote._on_event(Typed("s8", shown["id"], "harbour", True))  # Go: Enter keeps it
            self.assertTrue(wait_until(lambda: edited.emit.called))
        edited.emit.assert_called_once_with("opensubtitles_username", "harbour")
        self.assertFalse(settings.editing())

    def test_the_channel_lists_find_box_once_it_shows_a_caret(self):
        """Not while the list merely shows (the phone's buttons move it): once its Find box is
        clicked or typed into; the phone's text finds as typing does, and deleting it all
        keeps the box (and the phone's sheet) open."""
        self.show()
        drawer = self.wait_for_drawer()
        self.connect()
        remote = self.window.remote
        self.assertIsNone(focused_field(self.window))
        drawer.start_find()
        remote._push_state()
        shown = self.fields()[-1]
        self.assertEqual((shown["label"], shown["text"]), ("Find a genre or channel", ""))
        remote._on_event(Typed("s8", shown["id"], "Channel 2"))
        self.assertEqual(drawer.find_text, "Channel 2")
        self.assertTrue(drawer.finding())
        remote._on_event(Typed("s8", shown["id"], ""))
        self.assertFalse(drawer.finding())
        remote._push_state()
        self.assertEqual(self.fields()[-1], shown)  # still that box: nothing said
        self.assertIsNotNone(focused_field(self.window))

    def test_the_numpads_digits_type_into_the_find_box(self):
        """A digit of the phone's numpad with the channel list open types into its find (as
        the keyboard's): that box has the keys then (the page keeps its sheet off while its
        numpad shows)."""
        self.show()
        self.wait_for_drawer()
        self.window.remote.fire("d5")
        box = focused_field(self.window)
        self.assertIsNotNone(box)
        self.assertEqual((box.label, box.text), ("Find a genre or channel", "5"))

    def test_a_question_over_the_box_has_the_keys(self):
        self.show()
        self.wait_for_drawer()
        window = self.window
        window.open_link()
        self.assertIsNotNone(focused_field(window))
        with mock.patch.object(window.question_card, "isVisible", return_value=True):
            self.assertIsNone(focused_field(window))


class SiteWaitTests(PhoneCase):
    """A phone's Watch of a site's channel that isn't live waits for it: the phone says so,
    QiTV asks the site again calmly (one ask at a time, none while the phone is away), and
    hands the phone the stream once it is live; asking for something else, handing back to
    the TV, leaving for good or being unpaired ends the wait."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(live_controls, "OFFLINE_ASK_S", 0.15))
        self.enterContext(mock.patch.object(live_controls, "OFFLINE_JITTER_S", 0.0))
        self.target = Target({"name": "examplechannel · Kick", "cmd": KICK_PAGE}, "live", "Kick")
        self.asks = []  # (when, kwargs) of each play_elsewhere
        self.answers = []  # each ask's answer: a reason it failed, or a link (handed on)
        self.play_launch = []  # each ask's launch
        controller = self.window.controller
        self.enterContext(
            mock.patch.object(controller, "play_elsewhere", side_effect=self.play_elsewhere)
        )
        self.enterContext(mock.patch.object(self.window.screens, "notify"))
        self.heard = []

    def play_elsewhere(self, launch, chosen=None, **kwargs):
        self.asks.append((time.monotonic(), kwargs))
        self.play_launch.append(launch)
        answer = self.answers.pop(0) if self.answers else None
        target = chosen or self.target
        if answer is None:
            return True  # no answer (yet)
        if answer.startswith("http"):
            again = kwargs.get("again")
            if again is None or again():
                self.heard.append(launch(target, answer))
        else:
            self.heard.append(kwargs["failed"](target, answer))
        return True

    def watch(self, sid="s8"):
        """Phone `sid` watches the channel (its Watch of what the TV plays, here)."""
        self.window.remote._on_event(WatchHere(sid, "", True))

    def test_waits_says_so_and_plays_once_live(self):
        self.connect()
        self.answers = [OFFLINE, OFFLINE, "https://edge.example/live.m3u8?token=sentinel"]
        self.watch()
        self.assertEqual(self.heard, [True])  # it begins to wait: the TV says nothing
        why = self.sent("play")
        self.assertEqual(why, [{"t": "play", "failed": True, "why": "offline", "name": "Kick"}])
        self.assertTrue(wait_until(lambda: len(self.asks) == 3, timeout_ms=3000))
        self.assertEqual(self.heard, [True, False, (True, "")])  # waited on, then handed on
        played = self.sent("play")[-1]
        self.assertEqual(played["url"], "https://edge.example/live.m3u8?token=sentinel")
        self.assertEqual(len(self.sent("play")), 2)  # nothing said while it waited on
        for _, kwargs in self.asks[1:]:
            self.assertIn("again", kwargs)  # beside the TV's choices, never dropping them
        gaps = [b[0] - a[0] for a, b in zip(self.asks, self.asks[1:])]
        self.assertTrue(all(gap >= 0.12 for gap in gaps), gaps)  # calm, never a burst
        pause(400)
        self.assertEqual(len(self.asks), 3)  # it came: asked no more
        self.assertFalse(self.window.remote._waits.waiting("d8"))

    def test_one_ask_at_a_time(self):
        self.connect()
        self.answers = [OFFLINE]  # then: no answer to the next ask yet
        self.watch()
        self.assertTrue(wait_until(lambda: len(self.asks) == 2, timeout_ms=3000))
        pause(500)
        self.assertEqual(len(self.asks), 2)  # the next waits for its answer

    def test_another_answer_ends_the_wait_and_the_phone_hears_it_failed(self):
        self.connect()
        self.answers = [OFFLINE, "No channel named examplechannel on Kick"]
        self.watch()
        self.assertTrue(wait_until(lambda: len(self.asks) == 2, timeout_ms=3000))
        self.assertEqual(self.heard, [True, None])  # said as ever, by the TV
        self.assertEqual(self.sent("play")[-1], {"t": "play", "failed": True})
        pause(400)
        self.assertEqual(len(self.asks), 2)

    def ends(self, action):
        self.connect()
        self.answers = [OFFLINE]
        self.watch()
        self.assertTrue(self.window.remote._waits.waiting("d8"))
        action()
        self.assertFalse(self.window.remote._waits.waiting("d8"))
        asks = len(self.asks)
        pause(400)
        self.assertEqual(len(self.asks), asks)

    def test_the_phone_tuning_away_ends_it(self):
        self.ends(lambda: self.window.remote._on_event(WatchHere("s8", "nope")))

    def test_handing_back_to_the_tv_ends_it(self):
        self.ends(lambda: self.window.remote._on_event(ToTv("s8")))

    def test_unpairing_the_phone_ends_it(self):
        remote = self.window.remote
        self.ends(lambda: remote._on_event(StoreSaved({"version": 1, "devices": []})))

    def test_the_remote_turned_off_ends_it(self):
        self.ends(self.window.remote.shutdown)

    def test_a_phone_away_is_asked_for_once_back_and_ends_after_a_while(self):
        self.connect()
        remote = self.window.remote
        self.answers = [OFFLINE]
        self.watch()
        with mock.patch.object(remote_controller, "PHONE_GONE_S", 0.6):
            remote._on_event(Closed("s8", "d8"))
            pause(350)
            self.assertEqual(len(self.asks), 1)  # nobody there: nothing asked
            self.assertTrue(remote._waits.waiting("d8"))
            self.assertTrue(wait_until(lambda: not remote._waits.waiting("d8"), timeout_ms=2000))
        pause(300)
        self.assertEqual(len(self.asks), 1)

    def test_a_phone_back_in_time_is_asked_for_on_its_new_session(self):
        self.connect()
        remote = self.window.remote
        self.answers = [OFFLINE]
        self.watch()
        with mock.patch.object(remote_controller, "PHONE_GONE_S", 5.0):
            remote._on_event(Closed("s8", "d8"))
            self.connect("s10", "d8")  # a reload: the same phone, another session
            self.answers = ["https://edge.example/live.m3u8?token=sentinel"]
            self.assertTrue(wait_until(lambda: self.sent("play", "s10"), timeout_ms=3000))
        self.assertTrue(self.sent("play", "s10")[-1]["url"].startswith("https://edge.example"))

    def test_a_late_answer_to_an_ask_before_begins_no_wait(self):
        """The phone handed back to the TV (or watched another) before its site answered: the
        answer says nothing and begins no wait."""
        self.connect()
        remote = self.window.remote
        self.watch()  # no answer yet
        failed = self.asks[-1][1]["failed"]
        remote._on_event(ToTv("s8"))
        self.assertFalse(failed(self.target, OFFLINE))  # nobody's: nothing said on the TV
        self.assertFalse(remote._waits.waiting("d8"))
        self.assertEqual(self.sent("play"), [])
        pause(400)
        self.assertEqual(len(self.asks), 1)

    def test_an_older_pages_watch_fails_as_before(self):
        """An older page shows no wait (and would play on what it played): QiTV waits for
        nothing, and the TV says why as before."""
        self.connect()
        self.window.remote._on_event(WatchHere("s8"))
        self.assertIsNone(self.asks[-1][1]["failed"])
        self.assertNotIn("again", self.asks[-1][1])

    def test_an_ask_made_before_the_phone_slept_hands_on_to_its_new_session(self):
        self.connect()
        remote = self.window.remote
        self.answers = [OFFLINE]
        self.watch()
        self.assertTrue(wait_until(lambda: len(self.asks) == 2, timeout_ms=3000))  # unanswered
        ask = self.asks[-1][1]
        with mock.patch.object(remote_controller, "PHONE_GONE_S", 5.0):
            remote._on_event(Closed("s8", "d8"))
            self.connect("s10", "d8")  # the same phone, back as another session
            self.assertTrue(ask["again"]())
            launch = self.play_launch[-1]
            self.assertEqual(launch(self.target, "https://edge.example/live.m3u8"), (True, ""))
        self.assertTrue(self.sent("play", "s10")[-1]["url"].startswith("https://edge.example"))
        self.assertFalse(remote._waits.waiting("d8"))

    def test_a_late_answer_once_the_phone_slept_still_counts(self):
        """The answer to an ask made on the session before (the phone came back as another):
        still that phone's, it waits on."""
        self.connect()
        remote = self.window.remote
        self.answers = [OFFLINE]
        self.watch()
        self.assertTrue(wait_until(lambda: len(self.asks) == 2, timeout_ms=3000))
        failed = self.asks[-1][1]["failed"]
        remote._on_event(Closed("s8", "d8"))
        self.connect("s10", "d8")
        self.assertFalse(failed(self.target, OFFLINE))  # waits on
        self.assertTrue(remote._waits.waiting("d8"))

    def test_waiting_holds_no_stream_of_what_the_phone_played(self):
        self.connect()
        remote, controller = self.window.remote, self.window.controller
        self.answers = ["http://p.test/news.m3u8"]
        self.watch()  # it plays something
        self.assertIn("d8", remote._watching)
        self.answers = [OFFLINE]
        with mock.patch.object(controller, "phone_stopped") as stopped:
            self.watch()
        stopped.assert_called_once_with()
        self.assertEqual(remote._watching, {})
        self.hub.forget_film.assert_any_call("d8")


if __name__ == "__main__":
    unittest.main()
