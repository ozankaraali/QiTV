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
FIND = "Find a genre or channel"  # the channel list's Find box
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
        box's types nothing; the card gone, the phones hear of the box under it: the channel
        list's Find box, offered quiet (typing there finds)."""
        self.show()
        self.wait_for_drawer()
        self.connect()
        window, card, remote = self.window, self.window.open_link_card, self.window.remote
        remote._on_event(OpenLinkCard("s8"))
        self.assertFalse(card.isHidden())
        remote._push_state()
        [shown] = self.fields()
        number = shown["id"]
        opened = {"t": "field", "id": number, "label": "Open a link", "text": "", "link": True}
        self.assertEqual(shown, opened)
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
        under = self.fields()[-1]
        self.assertEqual((under["label"], under.get("quiet"), "link" in under), (FIND, True, False))
        remote._push_state()
        self.assertEqual(self.fields()[-1:], [under])  # said once

    def test_open_link_again_names_the_card_to_the_phone_that_asked(self):
        """Review 1: a second Open link (the card open already: by this phone, another, or the
        TV) is the same box: the phone that asked hears of it at once, or its sheet waits."""
        self.show()
        self.wait_for_drawer()
        self.connect()
        self.connect("s9", "d9")
        remote = self.window.remote
        remote._on_event(OpenLinkCard("s8"))
        shown = self.fields()[-1]
        self.assertTrue(shown["link"])
        for sid in ("s8", "s9"):
            remote._on_event(OpenLinkCard(sid))
            self.assertEqual(self.sent("field", sid)[-1], shown)
        self.assertEqual(self.fields()[-1], shown)  # (not told to all again)

    def test_go_waits_while_the_card_checks(self):
        """Review 3: the card cannot take typing while it checks: neither the text nor Enter
        goes in then; both do once it can."""
        self.show()
        self.wait_for_drawer()
        self.connect()
        remote, card = self.window.remote, self.window.open_link_card
        remote._on_event(OpenLinkCard("s8"))
        number = self.fields()[-1]["id"]
        card.set_busy(True)
        with mock.patch.object(remote, "_press") as press:
            remote._on_event(Typed("s8", number, KICK_PAGE, True))
            self.assertEqual(card.values(), {"link": ""})
            press.assert_not_called()
            remote._push_state()
            press.assert_not_called()
            card.set_busy(False)
            remote._push_state()
            self.assertEqual(card.values(), {"link": KICK_PAGE})
            press.assert_called_once_with("ok")
            remote._push_state()
            press.assert_called_once_with("ok")  # once

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
        box = focused_field(window)  # its rows have the keys: typing finds a setting
        self.assertEqual((box.label, box.quiet), ("Find a setting", True))
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
        offered = focused_field(self.window)  # typing would find: offered quiet
        self.assertEqual((offered.label, offered.quiet), (FIND, True))
        drawer.start_find()
        remote._push_state()
        shown = self.fields()[-1]
        self.assertEqual((shown["label"], shown["text"], "quiet" in shown), (FIND, "", False))
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

    def test_typing_into_a_quiet_box_starts_it_and_it_is_not_told_again(self):
        """The phone's keyboard button on the channel list's quiet Find box: its typing opens
        the find (as typing does), and the box that opened is not told as another."""
        self.show()
        drawer = self.wait_for_drawer()
        self.connect()
        remote = self.window.remote
        remote._push_state()
        offered = self.fields()[-1]
        self.assertTrue(offered["quiet"])
        remote._on_event(Typed("s8", offered["id"], "Channel 2"))
        self.assertTrue(drawer.finding())
        remote._push_state()
        self.assertEqual(self.fields(), [offered])

    def test_settings_find_takes_the_phones_text(self):
        self.show()
        self.wait_for_drawer()
        self.connect()
        window, remote = self.window, self.window.remote
        window.open_settings()
        settings = window.settings_drawer
        self.assertTrue(wait_until(settings.isVisible))
        remote._push_state()
        offered = self.fields()[-1]
        self.assertEqual((offered["label"], offered["quiet"]), ("Find a setting", True))
        remote._on_event(Typed("s8", offered["id"], "subtitles"))
        self.assertTrue(settings.finding())
        self.assertEqual(settings.find_text(), "subtitles")
        remote._on_event(Typed("s8", offered["id"], "subtitles size"))
        self.assertEqual(settings.find_text(), "subtitles size")
        remote._on_event(Typed("s8", offered["id"], ""))  # all of it deleted: the find ends
        self.assertFalse(settings.finding())
        remote._push_state()
        self.assertEqual(self.fields(), [offered])

    def test_the_librarys_search_and_genre_filter(self):
        self.show()
        self.wait_for_drawer()
        self.connect()
        window, remote = self.window, self.window.remote
        window.screens.open_library("movies")
        self.assertTrue(wait_until(window.screens.library_shown))
        library = window.library_controller
        box = focused_field(window)  # typing anywhere searches: offered quiet
        self.assertEqual((box.label, box.quiet), ("Search movies, series and channels", True))
        remote._push_state()
        offered = self.fields()[-1]
        remote._on_event(Typed("s8", offered["id"], "harbour"))
        searched = (library.mode, library.zone, library.query)
        self.assertEqual(searched, ("search", "search", "harbour"))
        remote._push_state()
        self.assertEqual(self.fields(), [offered])  # the search it opened: the same box
        library._focus("genres", field=True)  # the genre filter chosen
        remote._push_state()
        genres = self.fields()[-1]
        self.assertEqual((genres["label"], "quiet" in genres), ("Find a genre…", False))
        remote._on_event(Typed("s8", genres["id"], "dra"))
        self.assertEqual(window.library_page.genres.filter_text, "dra")

    def test_the_opensubtitles_search_field(self):
        """Its key page's field is secret; what is searched for is not; typing anywhere on the
        page goes into the field, so it is offered quiet off it."""
        self.show()
        page = self.window.subtitle_search
        page.show_key_page()
        box = page.phone_field()
        self.assertEqual((box.label, box.secret, box.text), ("Paste your API key here", True, ""))
        self.assertTrue(page.phone_type(box.key, " abc 123 "))
        self.assertEqual(page.key_text(), "abc123")
        page.show_search("Harbour Lights (1998)")
        box = page.phone_field()
        searched = (box.label, box.secret, box.text)
        self.assertEqual(searched, ("Film or series title", False, "Harbour Lights (1998)"))
        self.assertTrue(page.phone_type(box.key, "Harbour Lights"))
        self.assertEqual(page.query(), "Harbour Lights")
        page.dismiss()
        self.assertIsNone(page.phone_field())

    def test_the_phones_search_shown_on_the_tv_names_the_librarys_search(self):
        """The phone's Search with Show on TV opens the TV's search: that box has the keys
        (the page keeps its sheet off while its own search shows)."""
        self.show()
        self.wait_for_drawer()
        self.connect()
        remote = self.window.remote
        remote.fire("search")
        self.assertTrue(wait_until(self.window.screens.library_shown))
        remote._push_state()
        shown = self.fields()[-1]
        label = "Search movies, series and channels"
        self.assertEqual((shown["label"], "quiet" in shown), (label, False))

    def test_go_with_nothing_typed_presses_nothing_where_typing_would_start(self):
        """Review 1 (batch 3): Go into the Library's quiet search with nothing in it would
        press Enter on the highlighted poster: none; typed into the channel list's quiet find,
        the find it opened has the keys, and Go is Enter there."""
        self.show()
        self.wait_for_drawer()
        self.connect()
        window, remote = self.window, self.window.remote
        with mock.patch.object(remote, "_press") as press:
            remote._push_state()
            find = self.fields()[-1]
            remote._on_event(Typed("s8", find["id"], "Channel 2", True))
            press.assert_called_once_with("ok")
        window.screens.open_library("movies")
        self.assertTrue(wait_until(window.screens.library_shown))
        with mock.patch.object(remote, "_press") as press:
            remote._push_state()
            search = self.fields()[-1]
            self.assertTrue(search["quiet"])
            remote._on_event(Typed("s8", search["id"], "", True))
            press.assert_not_called()
        self.assertEqual(window.library_controller.mode == "search", False)

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

    def test_the_tv_playing_something_else_ends_the_wait(self):
        """Review 2: one screen at a time: the TV starting to play something else ends a
        phone's wait, as it ends its Watch; the phone hears the TV took over."""
        self.connect()
        remote, controller = self.window.remote, self.window.controller
        self.answers = [OFFLINE]
        self.watch()
        self.assertTrue(remote._waits.waiting("d8"))
        remote._push_state()
        self.assertTrue(remote._waits.waiting("d8"))  # the TV as it was: it waits on
        with mock.patch.object(controller, "showing_here", return_value=True):
            remote._push_state()  # the TV's own stream back (a reconnect): nothing new
        self.assertTrue(remote._waits.waiting("d8"))
        controller._lanes.supersede()  # the TV's newer choice (a tune, a film, Stop)
        remote._push_state()
        self.assertFalse(remote._waits.waiting("d8"))
        self.assertEqual(self.sent("stop"), [{"t": "stop"}])
        asks = len(self.asks)
        pause(400)
        self.assertEqual(len(self.asks), asks)

    def test_the_tv_waiting_for_the_channel_itself_takes_over(self):
        self.connect()
        remote, controller = self.window.remote, self.window.controller
        self.answers = [OFFLINE]
        self.watch()
        with mock.patch.object(controller, "tv_waits_for", return_value=True):
            remote._push_state()
        self.assertFalse(remote._waits.waiting("d8"))  # its own wait asks: one, not two

    def test_two_phones_waiting_for_a_channel_share_its_asks(self):
        """Review 6: one wait per channel: one ask each time for every phone waiting on it;
        live, it plays on the phone that asked last, and the other hears another screen took
        over."""
        self.connect()
        self.connect("s9", "d9")
        remote = self.window.remote
        self.answers = [OFFLINE, OFFLINE]
        self.watch("s8")
        self.watch("s9")
        self.assertEqual(self.heard, [True, False])  # its row counts the channel once
        self.assertEqual(len(self.sent("play", "s9")), 1)  # the joiner is told it waits too
        [wait] = remote._waits.waits()
        self.assertEqual(wait.devices, ["d8", "d9"])
        self.answers = [OFFLINE, "https://edge.example/live.m3u8?token=sentinel"]
        self.assertTrue(wait_until(lambda: len(self.asks) == 4, timeout_ms=3000))
        pause(100)
        self.assertEqual(len(self.asks), 4)  # one ask a turn, not one a phone
        self.assertEqual(self.asks[2][1]["ask_on"], "s9")  # for the phone that asked last
        self.assertTrue(self.sent("play", "s9")[-1]["url"].startswith("https://edge.example"))
        self.assertEqual(self.sent("stop", "s8"), [{"t": "stop"}])
        self.assertEqual(remote._waits.waits(), [])

    def test_another_phones_watch_ends_a_wait(self):
        """One screen at a time: another phone's Watch that plays ends a phone's wait (its
        channel live later must not take the stream back from it); that phone hears so."""
        self.connect()
        self.connect("s9", "d9")
        remote = self.window.remote
        self.answers = [OFFLINE]
        self.watch("s8")
        self.answers = ["https://edge.example/live.m3u8?token=sentinel"]
        self.watch("s9")  # the same channel, live for it now
        self.window.controller.handedOn.emit()  # (the controller's, as the phone took it)
        self.assertFalse(remote._waits.waiting("d8"))
        self.assertEqual(self.sent("stop", "s8"), [{"t": "stop"}])
        self.assertEqual(self.sent("stop", "s9"), [])
        asks = len(self.asks)
        pause(400)
        self.assertEqual(len(self.asks), asks)

    def test_the_tv_taking_over_as_the_link_comes_hands_nothing_on(self):
        """Review 2 (batch 3): the TV made a newer choice since the wait's ask went: its link,
        coming within the half second before the next look, is not handed on (it would tear
        the TV's new picture down); the wait ends, the phone hears the TV took over."""
        self.connect()
        remote, controller = self.window.remote, self.window.controller
        self.answers = [OFFLINE]
        self.watch()
        self.assertTrue(wait_until(lambda: len(self.asks) == 2, timeout_ms=3000))  # unanswered
        ask = self.asks[-1][1]
        controller._lanes.supersede()  # the TV tunes, no look since
        self.assertFalse(ask["again"]())
        self.assertFalse(remote._waits.waiting("d8"))
        self.assertEqual(self.sent("stop"), [{"t": "stop"}])

    def test_the_wait_is_over_only_once_the_stream_is_handed_on(self):
        """Review 3 (batch 3): live, the wait stays until the stream is handed on (a recording
        question may hold it back): the others hear another screen took over only then; and
        the phone that asked last gone by then, the next one waiting gets it."""
        self.connect()
        self.connect("s9", "d9")
        remote = self.window.remote
        self.answers = [OFFLINE, OFFLINE]
        self.watch("s8")
        self.watch("s9")
        self.assertTrue(wait_until(lambda: len(self.asks) == 3, timeout_ms=3000))  # unanswered
        ask, launch = self.asks[-1][1], self.play_launch[-1]
        self.assertTrue(ask["again"]())
        self.assertTrue(remote._waits.waiting("d8"))  # not over yet: nothing handed on
        self.assertEqual(self.sent("stop", "s8") + self.sent("stop", "s9"), [])
        remote._phones.discard("s9")  # the phone that asked last left meanwhile
        self.assertEqual(launch(self.target, "https://edge.example/live.m3u8"), (True, ""))
        self.assertTrue(self.sent("play", "s8")[-1]["url"].startswith("https://edge.example"))
        self.assertEqual(remote._waits.waits(), [])

    def test_a_declined_recording_question_ends_that_phones_wait(self):
        """Ruling (batch 4): live, the hand-off asked the phone that asked last whether to
        stop a recording; "Keep recording" ends its wait (it hears so, as today) and it is
        never asked again; another phone waiting gets the channel's next ask."""
        self.connect()
        self.connect("s9", "d9")
        remote = self.window.remote
        self.answers = [OFFLINE, OFFLINE]
        self.watch("s8")
        self.watch("s9")
        self.assertTrue(wait_until(lambda: len(self.asks) == 3, timeout_ms=3000))  # unanswered
        self.assertEqual(self.asks[-1][1]["ask_on"], "s9")
        remote._on_phone_asked("s9", "Stop recording?", "Stop", "Keep recording")
        remote._on_answered("s9", remote._asks["s9"], False)
        self.assertEqual(self.sent("play", "s9")[-1]["why"], "recording")
        self.assertFalse(remote._waits.waiting("d9"))
        self.assertTrue(remote._waits.waiting("d8"))
        self.assertTrue(wait_until(lambda: len(self.asks) == 4, timeout_ms=3000))
        self.assertEqual(self.asks[-1][1]["ask_on"], "s8")  # the next one still there

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
