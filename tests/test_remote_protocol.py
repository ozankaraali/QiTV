import json
import unittest

from services.remote.protocol import (
    BUTTON_META,
    b64d,
    b64e,
    parse_opening,
    parse_phone_message,
    steps_for,
)
from ui.keymap import Action
from ui.remote_buttons import BUTTONS


class StepsTests(unittest.TestCase):
    def test_steps_formula(self):
        got = [steps_for(m) for m in (0, 399, 400, 439, 440, 1200)]
        self.assertEqual(got, [1, 1, 2, 2, 3, 22])

    def test_negative_time_is_one_step(self):
        self.assertEqual(steps_for(-5), 1)


class ButtonTests(unittest.TestCase):
    def test_every_button_maps_to_something_real(self):
        self.assertEqual(set(BUTTONS), set(BUTTON_META))
        for name, button in BUTTONS.items():
            with self.subTest(name):
                if button.kind == "key":
                    self.assertGreater(button.key, 0)
                elif button.kind == "action":
                    self.assertIsInstance(button.action, Action)
                else:
                    self.assertEqual(button.kind, "screen")
                    self.assertIn(button.payload, ("tv", "movies", "series"))

    def test_reverse_pairs_are_symmetric(self):
        for name, meta in BUTTON_META.items():
            if meta.reverse is not None:
                self.assertEqual(BUTTON_META[meta.reverse].reverse, name)

    def test_only_reversible_buttons_reverse(self):
        reversible = {n for n, m in BUTTON_META.items() if m.reverse}
        self.assertEqual(reversible, {"up", "down", "left", "right"})

    def test_routes(self):
        self.assertEqual(BUTTON_META["volup"].route, "stay")
        self.assertEqual(BUTTON_META["chdown"].route, "player")
        self.assertEqual(BUTTON_META["up"].route, "keys")
        self.assertEqual(BUTTON_META["movies"].route, "screen")
        self.assertTrue(BUTTON_META["chup"].repeat)
        self.assertFalse(BUTTON_META["ok"].repeat)

    def test_ch_up_is_the_keyboards_up(self):
        self.assertIs(BUTTONS["chup"].action, Action.CH_PREV)
        self.assertIs(BUTTONS["chdown"].action, Action.CH_NEXT)

    def test_digits(self):
        self.assertEqual([BUTTONS[f"d{i}"].text for i in range(10)], [str(i) for i in range(10)])


class OpeningTests(unittest.TestCase):
    def test_opening_frames(self):
        self.assertEqual(parse_opening('{"v":1,"mode":"pair"}').mode, "pair")
        kk = parse_opening('{"v":1,"mode":"kk","key":"abc"}')
        self.assertEqual((kk.mode, kk.key_id), ("kk", "abc"))
        ticket = parse_opening('{"v":1,"mode":"ticket","ticket":"t1"}')
        self.assertEqual((ticket.mode, ticket.ticket_id), ("ticket", "t1"))

    def test_bad_openings(self):
        for text in (
            '{"v":2,"mode":"pair"}',
            '{"v":1,"mode":"kk"}',
            '{"v":1,"mode":"ticket"}',
            '{"v":1,"mode":"other"}',
            "junk",
            "[]",
            json.dumps({"v": 1, "mode": "kk", "key": "x" * 300}),
        ):
            with self.subTest(text=text[:30]):
                self.assertIsNone(parse_opening(text))


class MessageTests(unittest.TestCase):
    def test_messages_validated(self):
        self.assertEqual(parse_phone_message({"t": "down", "b": "up"}), {"t": "down", "b": "up"})
        self.assertEqual(
            parse_phone_message({"t": "up", "b": "ok", "n": 3}), {"t": "up", "b": "ok", "n": 3}
        )
        self.assertEqual(parse_phone_message({"t": "held"}), {"t": "held"})
        self.assertEqual(parse_phone_message({"t": "watch", "x": 1}), {"t": "watch"})
        self.assertEqual(  # a channel of the phone's list, to play on the phone
            parse_phone_message({"t": "watch", "id": "h3"}), {"t": "watch", "id": "h3"}
        )
        self.assertEqual(parse_phone_message({"t": "totv"}), {"t": "totv"})
        self.assertEqual(  # how far the phone got in a film it plays
            parse_phone_message({"t": "pos", "ms": 61000, "len": 5400000}),
            {"t": "pos", "ms": 61000, "len": 5400000},
        )
        q = parse_phone_message({"t": "query", "s": "h" * 150})
        self.assertEqual(len(q["s"]), 100)
        self.assertFalse(q["tv"])  # the phone's own search: the TV's picture stays
        self.assertTrue(parse_phone_message({"t": "query", "s": "h", "tv": True})["tv"])
        self.assertFalse(parse_phone_message({"t": "query", "s": "h", "tv": "yes"})["tv"])
        self.assertEqual(
            parse_phone_message(
                {"t": "get", "what": "guide", "list": "g4", "page": 2, "find": "n" * 150}
            ),
            {"t": "get", "what": "guide", "list": "g4", "page": 2, "find": "n" * 100},
        )
        self.assertEqual(  # the group the TV zaps through, the first page
            parse_phone_message({"t": "get", "what": "guide"}),
            {"t": "get", "what": "guide", "list": "", "page": 0, "find": ""},
        )
        self.assertEqual(
            parse_phone_message({"t": "get", "what": "timetable", "id": "h1"}),
            {"t": "get", "what": "timetable", "id": "h1"},
        )
        self.assertEqual(
            parse_phone_message({"t": "get", "what": "groups", "page": 1, "find": "news"}),
            {"t": "get", "what": "groups", "page": 1, "find": "news"},
        )
        self.assertEqual(  # the phone numbers its asks; the answer says which it answers
            parse_phone_message({"t": "get", "what": "groups", "n": 7})["n"], 7
        )
        self.assertEqual(
            parse_phone_message({"t": "get", "what": "genres", "section": "series"}),
            {"t": "get", "what": "genres", "section": "series", "page": 0, "find": ""},
        )
        self.assertEqual(
            parse_phone_message({"t": "get", "what": "library", "genre": "g2", "find": "harb"}),
            {
                "t": "get",
                "what": "library",
                "section": "movies",
                "genre": "g2",
                "page": 0,
                "find": "harb",
            },
        )
        self.assertEqual(
            parse_phone_message(
                {"t": "get", "what": "library", "section": "series", "genre": "g3", "page": 2}
            ),
            {
                "t": "get",
                "what": "library",
                "section": "series",
                "genre": "g3",
                "page": 2,
                "find": "",
            },
        )
        self.assertEqual(  # what QiTV chooses: Movies, its first shelf, the first page
            parse_phone_message({"t": "get", "what": "library"}),
            {
                "t": "get",
                "what": "library",
                "section": "movies",
                "genre": "",
                "page": 0,
                "find": "",
            },
        )
        for t in ("where", "forget"):
            self.assertEqual(parse_phone_message({"t": t}), {"t": t})
        self.assertEqual(
            parse_phone_message({"t": "open", "id": "abc"}), {"t": "open", "id": "abc"}
        )

    def test_the_remote_tabs_channel_list_asks_as_the_guide_does(self):
        self.assertEqual(
            parse_phone_message({"t": "get", "what": "channels", "list": "g4", "page": 2}),
            {"t": "get", "what": "channels", "list": "g4", "page": 2, "find": ""},
        )
        self.assertEqual(
            parse_phone_message({"t": "get", "what": "chgroups", "find": "new", "n": 3}),
            {"t": "get", "what": "chgroups", "page": 0, "find": "new", "n": 3},
        )
        self.assertEqual(  # the providers above the groups, and the groups of one of them
            parse_phone_message({"t": "get", "what": "chsources", "n": 4}),
            {"t": "get", "what": "chsources", "n": 4},
        )
        self.assertEqual(
            parse_phone_message({"t": "get", "what": "groups", "source": "g1a"}),
            {"t": "get", "what": "groups", "page": 0, "find": "", "source": "g1a"},
        )
        for obj in (
            {"t": "get", "what": "groups", "source": "g" * 33},
            {"t": "get", "what": "chgroups", "source": 5},
            {"t": "get", "what": "channels", "list": "g" * 33},
            {"t": "get", "what": "channels", "page": -1},
            {"t": "get", "what": "chgroups", "find": 5},
        ):
            with self.subTest(obj=obj):
                self.assertIsNone(parse_phone_message(obj))

    def test_bad_messages(self):
        for obj in (
            {"t": "nope"},
            {"t": "down", "b": "selfdestruct"},
            {"t": "up", "b": "ok", "n": 0},
            {"t": "up", "b": "ok", "n": True},
            {"t": "up", "b": "ok", "n": 100001},
            {"t": "get", "what": "guide", "list": "all", "page": -1},
            {"t": "get", "what": "guide", "list": "g" * 33, "page": 0},
            {"t": "get", "what": "guide", "list": "g1", "find": 5},
            {"t": "get", "what": "timetable"},
            {"t": "get", "what": "timetable", "id": "x" * 33},
            {"t": "get", "what": "settings"},
            {"t": "get", "what": "library", "section": "live"},
            {"t": "get", "what": "library", "find": 3},
            {"t": "get", "what": "groups", "page": -1},
            {"t": "get", "what": "groups", "n": -1},
            {"t": "get", "what": "groups", "n": 2**31},
            {"t": "get", "what": "genres", "n": "7"},
            {"t": "get", "what": "genres", "section": "live"},
            {"t": "get", "what": "library", "genre": "g" * 33},
            {"t": "get", "what": "library", "genre": 3},
            {"t": "get", "what": "library", "page": -1},
            {"t": "get", "what": "library", "page": "1"},
            {"t": "open", "id": "x" * 33},
            {"t": "watch", "id": "x" * 33},
            {"t": "watch", "id": 3},
            {"t": "pos", "ms": -1, "len": 10},
            {"t": "pos", "ms": 10, "len": 0},
            {"t": "pos", "ms": "10", "len": 10},
            {"t": "pos", "ms": 10, "len": 10**12},
            {"t": "query", "s": 5},
            ["t", "held"],
            "held",
            None,
        ):
            with self.subTest(obj=obj):
                self.assertIsNone(parse_phone_message(obj))


class Base64Tests(unittest.TestCase):
    def test_b64_round_trip_without_padding(self):
        for data in (b"", b"a", b"ab", b"abc", bytes(range(32))):
            text = b64e(data)
            self.assertNotIn("=", text)
            self.assertEqual(b64d(text), data)

    def test_b64_rejects_junk(self):
        with self.assertRaises(ValueError):
            b64d("%%%")


class ReviewFixProtocolTests(unittest.TestCase):
    def test_unhashable_values_are_refused_not_raised(self):
        for obj in (
            {"t": "down", "b": [1]},
            {"t": "up", "b": {"a": 1}, "n": 1},
            {"t": "get", "what": "guide", "list": ["all"], "page": 0},
            {"t": "open", "id": ["x"]},
            {"t": ["down"]},
        ):
            with self.subTest(obj=obj):
                self.assertIsNone(parse_phone_message(obj))


if __name__ == "__main__":
    unittest.main()
