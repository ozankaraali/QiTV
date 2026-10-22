import unittest

from remote_phone import PhoneClient

from services.remote.noise import Keypair
from services.remote.pairing import RemoteStore
from services.remote.protocol import (
    MAX_MESSAGE,
    Closed,
    Forgot,
    Get,
    Held,
    Opened,
    OpenItem,
    Paired,
    Position,
    Press,
    Query,
    StoreSaved,
    ToTv,
    WatchHere,
)
from services.remote.session import MessageTooBig, Session


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def kinds(events):
    return [type(e) for e in events]


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = RemoteStore.from_config(None)
        self.store.room = "room-1"
        self.tv_pub = self.store.identity_pub()
        self.sid = 0

    def session(self, door="home", same=None, home="http://192.168.1.20:5384/"):
        self.sid += 1
        return Session(
            self.store,
            sid=f"s{self.sid}",
            door=door,
            home_url=lambda: home,
            same_network=same,
            clock=self.clock,
            tv_name="Living room",
            hello=lambda: {"lang": "en", "strings": {}},
        )

    def pair(self, door="home", code=None, same=None):
        """Pair a new phone; returns (phone, session, events, hello)."""
        if code is None:
            code = self.store.current_code() or self.store.open_pairing(self.clock.now)
        phone = PhoneClient(self.tv_pub, mode="pair", code=code)
        session = self.session(door=door, same=same)
        self.assertEqual(session.receive(phone.opening()).frames, [])
        reply = session.receive(phone.first())
        self.assertIsNone(reply.close, reply.close)
        msg2, hello_frame = reply.frames
        self.payload = phone.finish(msg2)
        return phone, session, reply.events, phone.open(hello_frame)

    def kk(self, key, key_id, door="home", same=None):
        phone = PhoneClient(self.tv_pub, mode="kk", key=key, key_id=key_id)
        session = self.session(door=door, same=same)
        first = session.receive(phone.opening())
        if first.close:
            return phone, session, first
        reply = session.receive(phone.first())
        if reply.close is None:
            phone.finish(reply.frames[0])
        return phone, session, reply


class MessageSizeTests(SessionCase):
    """What QiTV sends stays inside a relay frame, in any script."""

    def test_letters_go_as_utf8_not_escapes(self):
        phone, session, _, _ = self.pair()
        title = "Добрый вечер" * 20  # Cyrillic: 2 bytes a letter, not a 6-byte \uXXXX
        sealed = session.send({"t": "x", "title": title})
        self.assertLess(len(sealed), len(title) * 2 + 64)
        self.assertEqual(phone.open(sealed)["title"], title)

    def test_a_message_too_big_is_refused_before_it_is_sealed(self):
        phone, session, _, _ = self.pair()
        with self.assertRaises(MessageTooBig):
            session.send({"t": "x", "rows": ["字" * 80] * (MAX_MESSAGE // 200)})
        # nothing was sealed, so the phone's count stays in step: the next message opens
        self.assertEqual(phone.open(session.send({"t": "ok"})), {"t": "ok"})


class PairingTests(SessionCase):
    def test_pair_with_current_code(self):
        phone, session, events, hello = self.pair(door="relay")
        self.assertEqual(kinds(events), [Paired, Opened, StoreSaved])
        self.assertEqual(events[0].name, "Test phone")
        self.assertEqual(hello["t"], "hello")
        self.assertEqual((hello["proto"], hello["tv"], hello["lang"]), (1, "Living room", "en"))
        self.assertEqual(hello["door"], "relay")
        self.assertEqual((hello["room"], hello["relay"]), ("room-1", self.store.relay_url))
        self.assertEqual(self.payload["tv"], "Living room")
        self.assertEqual(self.payload["room"], "room-1")
        self.assertEqual(self.payload["relay"], self.store.relay_url)
        self.assertEqual(self.store.find_key(self.payload["key"])[1].pub, phone.key.pub)
        self.assertTrue(session.established)
        self.assertEqual(session.device_id, self.payload["device"])

    def test_pair_without_relay_says_so(self):
        self.store.relay = False
        _, _, _, hello = self.pair()
        self.assertIsNone(self.payload["relay"])
        self.assertIsNone(self.payload["room"])
        self.assertIsNone(hello["room"])

    def test_pair_with_previous_code_within_grace(self):
        old = self.store.open_pairing(self.clock.now)
        self.store.rotate_code(self.clock.now)
        self.clock.now += 14
        _, _, events, _ = self.pair(code=old)
        self.assertIn(Paired, kinds(events))

    def test_pair_after_grace_closes_4401_pair_expired(self):
        old = self.store.open_pairing(self.clock.now)
        self.store.rotate_code(self.clock.now)
        self.clock.now += 16
        phone = PhoneClient(self.tv_pub, mode="pair", code=old)
        session = self.session()
        session.receive(phone.opening())
        reply = session.receive(phone.first())
        self.assertEqual(reply.close, (4401, "pair-expired"))
        self.assertEqual(reply.events, [])

    def test_pair_while_screen_closed_is_expired(self):
        phone = PhoneClient(self.tv_pub, mode="pair", code=b"\0" * 16)
        session = self.session()
        reply = session.receive(phone.opening())
        self.assertEqual(reply.close, (4401, "pair-expired"))

    def test_code_is_burnt_after_one_pairing(self):
        code = self.store.open_pairing(self.clock.now)
        self.pair(code=code)
        phone = PhoneClient(self.tv_pub, mode="pair", code=code)
        session = self.session()
        session.receive(phone.opening())
        self.assertEqual(session.receive(phone.first()).close, (4401, "pair-expired"))

    def test_long_names_are_cut_and_empty_named(self):
        code = self.store.open_pairing(self.clock.now)
        phone = PhoneClient(self.tv_pub, mode="pair", code=code)
        session = self.session()
        session.receive(phone.opening())
        reply = session.receive(phone.first({"pub": phone_pub(phone), "name": "x" * 80}))
        self.assertEqual(len(reply.events[0].name), 40)
        code = self.store.current_code()
        phone = PhoneClient(self.tv_pub, mode="pair", code=code)
        session = self.session()
        session.receive(phone.opening())
        reply = session.receive(phone.first({"pub": phone_pub(phone), "name": "  "}))
        self.assertEqual(reply.events[0].name, "Phone")


def phone_pub(phone):
    from services.remote.protocol import b64e

    return b64e(phone.key.pub)


class KkTests(SessionCase):
    def test_kk_with_paired_key(self):
        phone, _, _, _ = self.pair()
        key_id = self.payload["key"]
        again, session, reply = self.kk(phone.key, key_id)
        self.assertIsNone(reply.close)
        self.assertEqual(kinds(reply.events)[:1], [Opened])
        self.assertEqual(reply.events[0].door, "home")
        hello = again.open(reply.frames[1])
        self.assertEqual(hello["t"], "hello")
        events = session.receive(again.seal({"t": "down", "b": "down"})).events
        events += session.receive(again.seal({"t": "up", "b": "down", "n": 3})).events
        events += session.receive(again.seal({"t": "held"})).events
        sid = session.sid
        self.assertEqual(
            events, [Press(sid, "down", True, 1), Press(sid, "down", False, 3), Held(sid)]
        )

    def test_messages_become_events(self):
        phone, session, _, _ = self.pair()
        sid = session.sid
        got = []
        for message in (
            {"t": "query", "s": "harb"},
            {"t": "query", "s": "harb", "tv": True},
            {"t": "open", "id": "h1"},
            {"t": "get", "what": "guide", "list": "g2", "page": 1, "find": "news"},
            {"t": "get", "what": "timetable", "id": "h7"},
            {"t": "get", "what": "genres", "section": "series", "n": 4},
            {"t": "get", "what": "library", "section": "series", "genre": "g2", "page": 1},
            {"t": "watch"},
            {"t": "watch", "id": "h3"},
            {"t": "totv"},
            {"t": "get", "what": "sources", "n": 2},
            {"t": "get", "what": "chgroups", "source": "g1a", "n": 3},
            {"t": "pos", "ms": 61000, "len": 5400000},
        ):
            got += session.receive(phone.seal(message)).events
        self.assertEqual(
            got,
            [
                Query(sid, "harb"),
                Query(sid, "harb", tv=True),
                OpenItem(sid, "h1"),
                Get(sid, "guide", "g2", 1, find="news"),
                Get(sid, "timetable", "", 0, handle="h7"),
                Get(sid, "genres", "", 0, "series", n=4),
                Get(sid, "library", "", 1, "series", "g2"),
                WatchHere(sid),
                WatchHere(sid, "h3"),
                ToTv(sid),
                Get(sid, "sources", "", 0, n=2),
                Get(sid, "chgroups", "", 0, n=3, source="g1a"),
                Position(sid, 61000, 5400000),
            ],
        )

    def test_kk_with_unknown_key_closes_4401(self):
        _, _, reply = self.kk(Keypair.generate(), "nope")
        self.assertEqual(reply.close, (4401, "unknown"))

    def test_kk_with_removed_device_closes_4401(self):
        phone, _, _, _ = self.pair()
        self.store.remove_device(self.payload["device"])
        _, _, reply = self.kk(phone.key, self.payload["key"])
        self.assertEqual(reply.close, (4401, "unknown"))

    def test_kk_with_a_stolen_key_id_but_another_key_fails(self):
        self.pair()
        _, _, reply = self.kk(Keypair.generate(), self.payload["key"])
        self.assertEqual(reply.close, (4401, "unknown"))


class HopTests(SessionCase):
    def test_ticket_adds_home_key_then_kk_works(self):
        phone, relay_session, _, _ = self.pair(door="relay", same=True)
        device_id = self.payload["device"]
        reply = relay_session.receive(phone.seal({"t": "where"}))
        where = phone.open(reply.frames[0])
        self.assertEqual(where["t"], "where")
        self.assertTrue(where["same"])
        self.assertEqual(where["home"], "http://192.168.1.20:5384/")
        home = PhoneClient(self.tv_pub, mode="ticket", ticket=where["ticket"])
        session = self.session(door="home")
        session.receive(home.opening())
        reply = session.receive(home.first())
        self.assertIsNone(reply.close)
        answer = home.finish(reply.frames[0])
        self.assertEqual(answer["device"], device_id)
        self.assertEqual(kinds(reply.events), [Opened, StoreSaved])
        self.assertEqual(self.store.find_key(answer["key"])[1].door, "home")
        _, _, again = self.kk(home.key, answer["key"])
        self.assertIsNone(again.close)

    def test_ticket_is_one_use(self):
        phone, relay_session, _, _ = self.pair(door="relay", same=True)
        where = phone.open(relay_session.receive(phone.seal({"t": "where"})).frames[0])
        for expected in (None, (4401, "unknown")):
            home = PhoneClient(self.tv_pub, mode="ticket", ticket=where["ticket"])
            session = self.session()
            first = session.receive(home.opening())
            reply = first if first.close else session.receive(home.first())
            self.assertEqual(reply.close, expected)

    def test_where_on_home_door_has_no_ticket(self):
        phone, session, _, _ = self.pair(door="home")
        where = phone.open(session.receive(phone.seal({"t": "where"})).frames[0])
        self.assertIsNone(where["ticket"])
        self.assertIsNone(where["home"])

    def test_where_not_same_network(self):
        phone, session, _, _ = self.pair(door="relay", same=False)
        where = phone.open(session.receive(phone.seal({"t": "where"})).frames[0])
        self.assertFalse(where["same"])
        self.assertIsNotNone(where["ticket"])  # "Connect directly" may still hop


class HostileTests(SessionCase):
    def test_garbage_is_closed(self):
        session = self.session()
        self.assertEqual(session.receive("x").close[0], 4400)
        session = self.session()
        self.assertEqual(session.receive(b"\0\1").close[0], 4400)
        phone, session, _, _ = self.pair()
        self.assertEqual(session.receive(b"\0" * 70000).close[0], 4413)
        phone, session, _, _ = self.pair()
        self.assertEqual(session.receive(phone.seal(["t", "held"])).close[0], 4400)
        phone, session, _, _ = self.pair()
        self.assertEqual(session.receive(b"\0" * 40).close[0], 4400)  # doesn't open
        phone, session, _, _ = self.pair()
        self.assertEqual(session.receive("text after handshake").close[0], 4400)

    def test_unknown_message_types_are_ignored(self):
        phone, session, _, _ = self.pair()
        reply = session.receive(phone.seal({"t": "from-the-future"}))
        self.assertIsNone(reply.close)
        self.assertEqual(reply.events, [])

    def test_burst_closes_4429(self):
        phone, session, _, _ = self.pair()
        for _ in range(60):
            self.assertIsNone(session.receive(phone.seal({"t": "held"})).close)
        self.assertEqual(session.receive(phone.seal({"t": "held"})).close[0], 4429)
        self.clock.now += 2  # a closed session stays closed
        self.assertEqual(session.receive(phone.seal({"t": "held"})).events, [])

    def test_rate_window_slides(self):
        phone, session, _, _ = self.pair()
        for _ in range(60):
            session.receive(phone.seal({"t": "held"}))
        self.clock.now += 1.01
        self.assertIsNone(session.receive(phone.seal({"t": "held"})).close)

    def test_bad_pairing_payload_closes_4400(self):
        code = self.store.open_pairing(self.clock.now)
        phone = PhoneClient(self.tv_pub, mode="pair", code=code)
        session = self.session()
        session.receive(phone.opening())
        self.assertEqual(session.receive(phone.first({"pub": "short"})).close[0], 4400)


class LifecycleTests(SessionCase):
    def test_bye_on_removal(self):
        phone, session, _, _ = self.pair()
        self.assertEqual(phone.open(session.bye("removed")), {"t": "bye", "reason": "removed"})

    def test_send_seals_for_the_phone(self):
        phone, session, _, _ = self.pair()
        self.assertEqual(
            phone.open(session.send({"t": "state", "volume": 5})), {"t": "state", "volume": 5}
        )

    def test_forget_removes_the_device(self):
        phone, session, _, _ = self.pair()
        reply = session.receive(phone.seal({"t": "forget"}))
        self.assertEqual(kinds(reply.events), [Forgot, StoreSaved])
        self.assertEqual(reply.close, (1000, "forgotten"))
        self.assertEqual(self.store.devices, [])

    def test_touch_saves_at_most_once_a_minute(self):
        phone, _, _, _ = self.pair()
        key_id = self.payload["key"]
        self.clock.now += 120
        _, _, reply = self.kk(phone.key, key_id)
        self.assertIn(StoreSaved, kinds(reply.events))
        self.clock.now += 10
        _, _, reply = self.kk(phone.key, key_id)
        self.assertNotIn(StoreSaved, kinds(reply.events))

    def test_closed_event_names_the_device(self):
        _, session, _, _ = self.pair()
        self.assertEqual(session.closed_event(), Closed(session.sid, self.payload["device"]))


class ReviewFixTests(SessionCase):
    def test_a_phone_removed_mid_handshake_cannot_finish_it(self):
        phone, _, _, _ = self.pair()
        key_id = self.payload["key"]
        again = PhoneClient(self.tv_pub, mode="kk", key=phone.key, key_id=key_id)
        session = self.session()
        self.assertIsNone(session.receive(again.opening()).close)
        self.store.remove_device(self.payload["device"])
        reply = session.receive(again.first())
        self.assertEqual(reply.close, (4401, "unknown"))
        self.assertFalse(session.established)
        self.assertEqual(reply.events, [])

    def test_a_list_for_a_button_closes_nothing_raises(self):
        phone, session, _, _ = self.pair()
        for bad in ({"t": "down", "b": [1]}, {"t": "up", "b": {"x": 1}, "n": 1}):
            reply = session.receive(phone.seal(bad))
            self.assertIsNone(reply.close)
            self.assertEqual(reply.events, [])


class HopAnsweredSessionTests(SessionCase):
    def test_where_says_when_the_last_hop_never_arrived(self):
        phone, session, _, _ = self.pair(door="relay", same=True)
        first = phone.open(session.receive(phone.seal({"t": "where"})).frames[0])
        self.assertFalse(first["unreachable"])
        self.clock.now += 10  # its ticket was never used: home didn't answer
        second = phone.open(session.receive(phone.seal({"t": "where"})).frames[0])
        self.assertTrue(second["unreachable"])


if __name__ == "__main__":
    unittest.main()
