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

    def test_stop_is_a_players_button(self):  # 2.6: nothing plays, nothing streams
        self.assertEqual(BUTTONS["stop"].action, Action.STOP)

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
    def test_a_question_is_answered(self):  # 2.6: stop a recording for the phone's Watch?
        self.assertEqual(
            parse_phone_message({"t": "answer", "id": 3, "yes": True}),
            {"t": "answer", "id": 3, "yes": True},
        )
        for bad in (
            {"t": "answer", "id": "3", "yes": True},
            {"t": "answer", "id": 3, "yes": 1},
            {"t": "answer", "id": -1, "yes": False},
            {"t": "answer", "yes": False},
        ):
            self.assertIsNone(parse_phone_message(bad), bad)

    def test_a_recording_is_gone_back_to_or_stopped(self):
        for act in ("back", "stop"):
            self.assertEqual(
                parse_phone_message({"t": "rec", "id": "main", "do": act}),
                {"t": "rec", "id": "main", "do": act},
            )
        for bad in (
            {"t": "rec", "id": "main", "do": "start"},
            {"t": "rec", "id": "", "do": "stop"},
        ):
            self.assertIsNone(parse_phone_message(bad), bad)

    def test_a_cast_names_its_device_or_none(self):  # 2.8
        for to in ("Living room TV", ""):
            self.assertEqual(parse_phone_message({"t": "cast", "to": to}), {"t": "cast", "to": to})
        self.assertEqual(
            parse_phone_message({"t": "get", "what": "cast"}), {"t": "get", "what": "cast"}
        )
        for bad in ({"t": "cast"}, {"t": "cast", "to": 3}, {"t": "cast", "to": "x" * 201}):
            self.assertIsNone(parse_phone_message(bad), bad)

    def test_a_programme_is_scheduled_or_cancelled(self):  # 2.8
        for do in ("once", "series", "cancel"):
            self.assertEqual(
                parse_phone_message({"t": "sched", "id": "p4", "do": do}),
                {"t": "sched", "id": "p4", "do": do},
            )
        for bad in (
            {"t": "sched", "id": "p4", "do": "forget"},
            {"t": "sched", "id": "", "do": "once"},
            {"t": "sched", "id": ["p4"], "do": "once"},
        ):
            self.assertIsNone(parse_phone_message(bad), bad)

    def test_a_channel_is_recorded_at_a_time(self):  # 2.8 (owner)
        every = {"t": "timer", "id": "h2", "title": "Late", "begin": 1260, "minutes": 60}
        self.assertEqual(
            parse_phone_message(dict(every, days=[2, 0, 2])),
            dict(every, days=[0, 2], day=""),
        )
        once = dict(every, days=[], day="2026-10-27")
        self.assertEqual(parse_phone_message(once), once)
        self.assertEqual(parse_phone_message(dict(once, title="x" * 300))["title"], "x" * 80)
        for bad in (
            dict(every, days=[]),  # once, but on no day
            dict(once, day="27/10/2026"),
            dict(once, day="2026-02-30"),
            dict(every, days=[7]),
            dict(every, days=[True]),
            dict(every, days="0"),
            dict(every, days=[0], begin=1440),
            dict(every, days=[0], minutes=0),
            dict(every, days=[0], minutes=1441),
            dict(every, days=[0], title=3),
            dict(every, days=[0], id=""),
        ):
            self.assertIsNone(parse_phone_message(bad), bad)

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
        # 2.8: the Library's Refresh
        self.assertEqual(parse_phone_message({"t": "refresh", "x": 1}), {"t": "refresh"})
        self.assertEqual(  # how far the phone got in a film it plays
            parse_phone_message({"t": "pos", "ms": 61000, "len": 5400000}),
            {"t": "pos", "ms": 61000, "len": 5400000},
        )
        self.assertEqual(  # and the number of the play it is a report of
            parse_phone_message({"t": "pos", "ms": 61000, "len": 5400000, "p": 3}),
            {"t": "pos", "ms": 61000, "len": 5400000, "p": 3},
        )
        for bad in ("3", -1, True, 2**40):
            self.assertNotIn("p", parse_phone_message({"t": "pos", "ms": 1, "len": 2, "p": bad}))
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


class BackToButtonTests(unittest.TestCase):
    """2.6: the phone's Resume, shown while the TV offers where a channel was left."""

    def test_it_is_a_player_button_for_the_tv_s_back_to(self):
        from services.remote.protocol import BUTTON_META
        from ui.keymap import Action
        from ui.remote_buttons import BUTTONS

        self.assertEqual(BUTTON_META["back_to"].route, "player")
        self.assertEqual(BUTTONS["back_to"].action, Action.BACK_TO)
