"""Pictures on the phone at home, QiTV's side (ui/remote_pictures.py, ui/remote_controller.py):
a phone at home is handed tokens for the posters and logos it shows, one through the relay none;
a picture too large to hand as it is goes as QiTV's own scaled copy, from the TV's caches."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import mock

from PySide6.QtGui import QColor, QPixmap
from test_tv_window import WindowCase, wait_until

from services.remote.pictures import LOGO, POSTER
from services.remote.protocol import Opened, PictureWanted
from ui import remote_controller
from ui.link_lanes import Target

LOGO_URL = "https://logos.example/one.png?user=me&pass=pw"
POSTER_URL = "https://posters.example/one.jpg"

STATE = {
    "t": "state",
    "context": "player",
    "section": "live",
    "now": {"kind": "live", "num": "1", "name": "Harbourline", "tint": "#336699"},
    "volume": 40,
}


def keys(message) -> set:
    """Every key anywhere in a message."""
    found: set = set()
    if isinstance(message, dict):
        for key, value in message.items():
            found.add(key)
            found |= keys(value)
    elif isinstance(message, list):
        for value in message:
            found |= keys(value)
    return found


class PhonePictureTests(WindowCase):
    def setUp(self):
        super().setUp()
        self.remote = self.window.remote
        self.hub = mock.Mock()
        self.remote.hub = self.hub
        self.addCleanup(setattr, self.remote, "hub", None)

    def connect(self, sid, door="home"):
        """A phone's session, as the hub opened it (one through the relay opened too: QiTV's
        own side must still hand it nothing)."""
        remote = self.remote
        remote._phones.add(sid)
        remote._devices[sid] = f"d-{sid}"
        if door == "relay":
            remote._relayed.add(sid)
        remote.pictures.shelf.open(sid, "192.168.1.5")
        self.addCleanup(remote._phones.discard, sid)
        self.addCleanup(remote._relayed.discard, sid)

    def sent(self, sid):
        """What phone `sid` was sent, its own and every phone's, in order."""
        messages = []
        for name, args, _kwargs in self.hub.mock_calls:
            if name == "send" and args[0] == sid:
                messages.append(args[1])
            elif name == "broadcast":
                messages.append(args[0])
        return messages

    def test_a_phone_at_home_gets_tokens_one_through_the_relay_none(self):
        self.connect("h1")
        self.connect("r1", "relay")
        token = self.remote.pictures.token("h1", POSTER_URL, POSTER)
        self.assertTrue(token)
        self.assertNotIn("posters.example", token)
        self.assertEqual(self.remote.pictures.token("h1", POSTER_URL, POSTER), token)
        self.assertEqual(self.remote.pictures.token("r1", POSTER_URL, POSTER), "")
        self.assertEqual(self.remote.pictures.token("gone", POSTER_URL, POSTER), "")

    def test_no_logos_while_channel_logos_are_off(self):
        self.connect("h1")
        self.window.config_manager.channel_logos = False
        self.assertEqual(self.remote.pictures.token("h1", LOGO_URL, LOGO), "")
        self.assertTrue(self.remote.pictures.token("h1", POSTER_URL, POSTER))

    def test_the_lists_hand_tokens_at_home_alone(self):
        self.connect("h1")
        self.connect("r1", "relay")
        film = {"name": "Harbour Lights", "logo": POSTER_URL}
        channel = {"name": "Harbourline", "logo": LOGO_URL}
        home, relayed = self.remote._library("h1"), self.remote._library("r1")
        self.assertTrue(home._pictures(film, "movie"))
        self.assertEqual(relayed._pictures(film, "movie"), "")
        self.assertEqual(home._pictures(film, "recording"), "")  # recordings keep their cards
        for make in (self.remote._guide, self.remote._channel_list):
            self.assertTrue(make("h1")._pictures(channel))
            self.assertEqual(make("r1")._pictures(channel), "")

    def test_the_now_playing_picture_goes_to_phones_at_home_alone(self):
        self.connect("h1")
        self.connect("r1", "relay")
        with (
            mock.patch.object(remote_controller, "state", return_value=dict(STATE)),
            mock.patch.object(self.remote.pictures, "now", return_value=(LOGO_URL, LOGO)),
        ):
            self.remote._last_state = None
            self.remote._push_state()
        [home] = [m for m in self.sent("h1") if m["t"] == "state"]
        [relayed] = [m for m in self.sent("r1") if m["t"] == "state"]
        self.assertEqual(home["now"]["img"], self.remote.pictures.token("h1", LOGO_URL, LOGO))
        self.assertNotIn("img", keys(relayed))
        self.assertEqual(home["now"]["name"], "Harbourline")
        self.assertNotIn("img", STATE["now"])  # the shared snapshot is never dressed in place

    def test_a_new_picture_alone_is_news_for_the_phones(self):
        self.connect("h1")
        pictured = [None]
        with (
            mock.patch.object(remote_controller, "state", side_effect=lambda w: dict(STATE)),
            mock.patch.object(self.remote.pictures, "now", side_effect=lambda: pictured[0]),
        ):
            self.remote._last_state = None
            self.remote._push_state()
            pictured[0] = (LOGO_URL, LOGO)  # its logo became known: the same state otherwise
            self.remote._push_state()
            self.remote._push_state()  # and nothing new after
        states = [m for m in self.sent("h1") if m["t"] == "state"]
        self.assertEqual(len(states), 2)
        self.assertNotIn("img", states[0]["now"])
        self.assertTrue(states[1]["now"]["img"])

    def test_without_a_picture_the_state_is_broadcast_as_before(self):
        self.connect("r1", "relay")
        with (
            mock.patch.object(remote_controller, "state", return_value=dict(STATE)),
            mock.patch.object(self.remote.pictures, "now", return_value=(LOGO_URL, LOGO)),
        ):
            self.remote._last_state = None
            self.remote._push_state()
        self.hub.broadcast.assert_called_once()
        self.assertNotIn("img", keys(self.hub.broadcast.call_args.args[0]))

    def test_a_phone_opening_hears_the_now_picture_at_home_alone(self):
        self.connect("h1")
        self.connect("r1", "relay")
        with (
            mock.patch.object(remote_controller, "state", side_effect=lambda w: dict(STATE)),
            mock.patch.object(self.remote.pictures, "now", return_value=(LOGO_URL, LOGO)),
        ):
            self.remote.bridge.post(Opened("h1", "d-h1", "home"))
            self.remote.bridge.post(Opened("r1", "d-r1", "relay"))
            self.assertTrue(wait_until(lambda: len(self.hub.send.call_args_list) >= 2))
        [home] = [m for m in self.sent("h1") if m["t"] == "state"]
        [relayed] = [m for m in self.sent("r1") if m["t"] == "state"]
        self.assertTrue(home["now"]["img"])
        self.assertNotIn("img", keys(relayed))

    def test_the_now_picture_is_the_channels_logo_or_the_titles_poster(self):
        controller = self.window.controller
        pictures = self.remote.pictures
        with (
            mock.patch.object(controller, "_current", None),
            mock.patch.object(controller, "_channel", {"name": "Harbourline", "logo": LOGO_URL}),
        ):
            self.assertEqual(pictures.now(), (LOGO_URL, LOGO))
        film = Target({"name": "Harbour Lights", "logo": POSTER_URL}, "movies", "Harbour Lights")
        with mock.patch.object(controller, "_current", film):
            self.assertEqual(pictures.now(), (POSTER_URL, POSTER))
        local = Target(
            {"name": "Home video", "logo": "/Users/x/a.jpg"}, "movies", "Home video", local=True
        )
        with mock.patch.object(controller, "_current", local):
            self.assertIsNone(pictures.now())
        with (
            mock.patch.object(controller, "_current", None),
            mock.patch.object(controller, "_channel", None),
        ):
            self.assertIsNone(pictures.now())

    def test_a_large_poster_goes_as_the_tvs_own_scaled_copy(self):
        pixmap = QPixmap(24, 36)
        pixmap.fill(QColor("#336699"))
        self.window.posters._store(POSTER_URL, pixmap)
        self.remote.bridge.post(PictureWanted(POSTER_URL, POSTER))
        self.assertTrue(wait_until(lambda: self.hub.put_picture.called))
        source, kind, data, kind_of = self.hub.put_picture.call_args.args
        self.assertEqual((source, kind, kind_of), (POSTER_URL, POSTER, "image/jpeg"))
        self.assertEqual(data[:3], b"\xff\xd8\xff")

    def test_a_logo_goes_as_a_png_once_the_tv_has_loaded_it(self):
        logos = self.window.logos
        with mock.patch.object(logos, "request") as request:
            self.remote.bridge.post(PictureWanted(LOGO_URL, LOGO))
            self.assertTrue(wait_until(lambda: request.called))
        request.assert_called_once_with([LOGO_URL])
        self.hub.put_picture.assert_not_called()
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor("#ffffff"))
        logos._store(LOGO_URL, pixmap)
        logos.logoReady.emit(LOGO_URL)
        self.assertTrue(wait_until(lambda: self.hub.put_picture.called))
        source, kind, data, kind_of = self.hub.put_picture.call_args.args
        self.assertEqual((source, kind, kind_of), (LOGO_URL, LOGO, "image/png"))
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        logos.logoReady.emit(LOGO_URL)  # once
        self.assertEqual(self.hub.put_picture.call_count, 1)
