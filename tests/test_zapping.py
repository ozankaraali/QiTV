import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from timer_fixtures import elapse

from services.zapping import ZapContext, ZapController

CHANNELS = [{"id": i, "name": f"C{i}", "number": str(i)} for i in (1, 2, 3, 7)]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ZapContextTests(unittest.TestCase):
    def test_peek_wraps(self):
        ctx = ZapContext("g", CHANNELS)
        ctx.set_current(CHANNELS[0])
        self.assertEqual(ctx.peek(-1), (3, CHANNELS[3]))
        ctx.set_current(CHANNELS[3])
        self.assertEqual(ctx.peek(1), (0, CHANNELS[0]))

    def test_by_number_falls_back_to_all_channels(self):
        # "101" in Sports said "No channel 101"; TV numbers are global.
        everything = CHANNELS + [{"id": 101, "name": "C101", "number": "101"}]
        sports = ZapContext("sports", CHANNELS[:2], fallback=everything)
        self.assertEqual(sports.by_number("101")["id"], 101)
        self.assertEqual(sports.by_number("2"), CHANNELS[1])  # the group wins
        self.assertIsNone(sports.by_number("55"))
        self.assertIsNone(ZapContext("g", CHANNELS[:2]).by_number("7"))  # no fallback given
        self.assertEqual(ZapContext("g", CHANNELS).fallback, [])

    def test_single_and_empty(self):
        self.assertIsNone(ZapContext("g", []).peek(1))
        one = ZapContext("g", [CHANNELS[0]])
        one.set_current(CHANNELS[0])
        self.assertEqual(one.peek(1), (0, CHANNELS[0]))

    def test_by_number(self):
        ctx = ZapContext("g", CHANNELS)
        self.assertEqual(ctx.by_number("007"), CHANNELS[3])
        self.assertIsNone(ctx.by_number("9"))
        unnumbered = ZapContext("g", [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}])
        self.assertEqual(unnumbered.by_number("2")["id"], "b")


class ZapControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.clock = Clock()
        self.zap = ZapController(rest_ms=40, burst_ms=1000, digits_ms=60, clock=self.clock)
        self.zap.set_context(ZapContext("g", CHANNELS))
        self.zap.tuned(CHANNELS[0])
        self.tunes, self.columns, self.digits, self.missing = [], [], [], []
        self.zap.tuneRequested.connect(self.tunes.append)
        self.zap.columnRequested.connect(self.columns.append)
        self.zap.digitsChanged.connect(self.digits.append)
        self.zap.numberNotFound.connect(self.missing.append)

    def test_single_press_tunes_immediately(self):
        self.zap.step(1)
        self.assertEqual(self.tunes, [CHANNELS[1]])
        self.assertEqual(self.columns, [])

    def test_burst_opens_column_and_tunes_after_rest(self):
        self.zap.step(1)
        self.zap.tuned(CHANNELS[1])
        self.clock.now += 0.3
        self.zap.step(1)
        self.clock.now += 0.2
        self.zap.step(1)
        self.assertEqual(self.columns, [True])
        self.assertEqual(self.tunes, [CHANNELS[1]])
        elapse(120)
        self.assertEqual(self.tunes, [CHANNELS[1], CHANNELS[3]])

    def test_rest_on_already_tuned_channel_does_nothing(self):
        self.zap.step(1)
        self.zap.tuned(CHANNELS[1])
        self.clock.now += 0.1
        self.zap.step(1)
        self.zap.step(-1)
        elapse(120)
        self.assertEqual(self.tunes, [CHANNELS[1]])

    def test_digits(self):
        self.zap.digit("0")
        self.zap.digit("7")
        self.assertEqual(self.digits, ["0", "07"])
        elapse(150)
        self.assertEqual(self.tunes, [CHANNELS[3]])
        self.assertEqual(self.digits[-1], "")

    def test_digits_tune_a_channel_outside_the_group(self):
        outside = {"id": 101, "name": "C101", "number": "101"}
        self.zap.set_context(ZapContext("g", CHANNELS, fallback=CHANNELS + [outside]))
        for d in "101":
            self.zap.digit(d)
        elapse(150)
        self.assertEqual((self.tunes, self.missing), ([outside], []))

    def test_unknown_number(self):
        self.zap.digit("9")
        elapse(150)
        self.assertEqual((self.tunes, self.missing, self.digits[-1]), ([], ["9"], ""))

    def test_max_digits(self):
        for d in "12345":
            self.zap.digit(d)
        self.assertEqual(self.digits[-1], "1234")

    def test_previous_channel(self):
        fresh = ZapController(clock=self.clock)
        seen = []
        fresh.tuneRequested.connect(seen.append)
        fresh.previous()
        self.assertEqual(seen, [])
        self.zap.tuned(CHANNELS[2])
        self.zap.previous()
        self.assertEqual(self.tunes, [CHANNELS[0]])

    def test_previous_channel_names_the_one_tuned_before(self):
        fresh = ZapController(clock=self.clock)
        self.assertIsNone(fresh.previous_channel())
        fresh.tuned(CHANNELS[0])
        self.assertIsNone(fresh.previous_channel())
        fresh.tuned(CHANNELS[2])
        self.assertEqual(fresh.previous_channel(), CHANNELS[0])
        fresh.reset()
        self.assertIsNone(fresh.previous_channel())

    def test_second_press_during_async_tune_advances_without_duplicate(self):
        self.zap.step(1)  # immediate tune of CHANNELS[1]; tuned() not called yet
        self.clock.now += 0.2
        highlights = []
        self.zap.highlightChanged.connect(lambda c, i: highlights.append((c, i)))
        self.zap.step(1)
        self.assertEqual(highlights, [(CHANNELS[2], 2)])
        elapse(120)
        self.assertEqual(self.tunes, [CHANNELS[1], CHANNELS[2]])

    def test_set_context_with_shorter_list_is_safe(self):
        self.zap.step(1)
        self.zap.tuned(CHANNELS[1])
        self.clock.now += 0.1
        self.zap.step(1)
        self.zap.step(1)
        self.zap.set_context(ZapContext("h", CHANNELS[:1]))
        self.assertEqual(self.columns, [True, False])
        elapse(120)
        self.assertEqual(self.tunes, [CHANNELS[1]])

    def test_out_of_band_tune_reanchors_or_closes_column(self):
        self.zap.step(1)
        self.zap.tuned(CHANNELS[1])
        self.clock.now += 0.1
        self.zap.step(1)
        self.zap.tuned(CHANNELS[3])
        self.clock.now += 0.1
        self.zap.step(-1)
        elapse(120)
        self.assertEqual(self.tunes[-1], CHANNELS[2])
        self.zap.tuned({"id": 99, "name": "Other"})
        self.assertEqual(self.columns[-1], False)

    def test_same_identity_rules(self):
        self.assertFalse(ZapContext.same({"id": 1, "name": "A"}, {"cmd": "x", "name": "A"}))
        self.assertTrue(ZapContext.same({"name": "A"}, {"name": "A"}))
        self.assertTrue(ZapContext.same({"id": 1, "name": "A"}, {"id": "1", "name": "B"}))

    def test_same_prefers_cmd_over_positional_ids(self):
        # M3U ids are list positions, so after a playlist change the id pointed at another
        # channel. The stream address is the identity when both have it.
        old = {"id": 3, "name": "News", "cmd": "http://x/news.ts"}
        self.assertFalse(ZapContext.same(old, {"id": 3, "name": "Kids", "cmd": "http://x/kids.ts"}))
        self.assertTrue(ZapContext.same(old, {"id": 9, "name": "News", "cmd": "http://x/news.ts"}))
        self.assertTrue(ZapContext.same({"id": 1, "cmd": ""}, {"id": "1", "cmd": "c"}))
        self.assertFalse(ZapContext.same({"cmd": "c"}, {"id": 1, "name": "A"}))

    def test_reordered_playlist_keeps_the_tuned_channel(self):
        playlist = [{"id": i, "name": n, "cmd": f"http://x/{n}"} for i, n in enumerate("ABC")]
        reordered = [{"id": i, "name": n, "cmd": f"http://x/{n}"} for i, n in enumerate("CAB")]
        self.zap.set_context(ZapContext("g", playlist))
        self.zap.tuned(playlist[0])  # A
        self.zap.set_context(ZapContext("g", reordered))
        self.assertEqual(self.zap.context.current()["name"], "A")
        self.zap.step(1)
        self.assertEqual(self.tunes[-1]["name"], "B")

    def test_reset_forgets_the_previous_provider(self):
        # Backspace after a provider switch asked for the old provider's channel.
        self.zap.tuned(CHANNELS[2])  # previous: CHANNELS[0]
        self.clock.now += 0.1
        self.zap.step(1)
        self.zap.step(1)  # burst: the column is open and the rest timer runs
        self.zap.digit("7")
        self.zap.reset()
        self.assertEqual(self.columns[-1], False)
        self.assertEqual(self.digits[-1], "")
        self.assertEqual((self.zap.context.group_key, self.zap.context.channels), ("", []))
        before = list(self.tunes)
        self.zap.previous()
        elapse(150)  # neither the rest timer nor the digit timer fires
        self.assertEqual(self.tunes, before)
        other = [{"id": "b1", "name": "B1", "cmd": "b1"}, {"id": "b2", "name": "B2", "cmd": "b2"}]
        self.zap.set_context(ZapContext("h", other))
        self.clock.now += 5
        self.zap.step(1)  # nothing tuned on the new provider: starts at the first channel
        self.assertEqual(self.tunes[-1], other[0])

    def test_note_request_anchors_the_next_step_without_tuning(self):
        nine = {"id": 9, "name": "Nine", "number": "9"}
        ten = {"id": 10, "name": "Ten", "number": "10"}
        self.zap.set_context(ZapContext("g", CHANNELS + [nine, ten]))
        self.zap.note_request(nine)  # e.g. a drawer pick the shell tuned itself
        self.assertEqual(self.tunes, [])
        self.clock.now += 5
        self.zap.step(1)
        self.assertEqual(self.tunes, [ten])
        self.zap.tuned(nine)  # the late confirmation does not cancel the newer zap
        self.zap.tuned(ten)
        self.zap.previous()
        self.assertEqual(self.tunes[-1], nine)

    def test_cancel_digits(self):
        self.zap.digit("7")
        self.zap.cancel_digits()
        self.assertEqual(self.digits, ["7", ""])
        elapse(150)
        self.assertEqual((self.tunes, self.missing), ([], []))
        self.zap.digit("2")  # a new entry starts from scratch
        elapse(150)
        self.assertEqual(self.tunes, [CHANNELS[1]])

    def test_number_of_is_public(self):
        self.assertEqual(ZapContext.number_of({"number": " 07 "}), "07")
        self.assertEqual(ZapContext.number_of({"number": 0}), "0")
        self.assertEqual(ZapContext.number_of({}), "")
        self.assertEqual(ZapContext._number({"number": 5}), "5")

    def test_number_zero_and_duplicates(self):
        ctx = ZapContext(
            "g", [{"id": 1, "number": "00"}, {"id": 2, "number": "5"}, {"id": 3, "number": "5"}]
        )
        self.assertEqual(ctx.by_number("0")["id"], 1)
        self.assertEqual(ctx.by_number("5")["id"], 2)

    def test_late_confirmation_does_not_cancel_second_press(self):
        self.zap.step(1)  # requests CH1
        self.clock.now += 0.2
        highlights = []
        self.zap.highlightChanged.connect(lambda c, i: highlights.append((c, i)))
        self.zap.step(1)  # highlights CH2
        self.zap.tuned(CHANNELS[1])  # late confirmation of press 1
        elapse(120)
        self.assertEqual(self.tunes, [CHANNELS[1], CHANNELS[2]])
        self.assertEqual(highlights, [(CHANNELS[2], 2)])

    def test_out_of_band_reanchor_emits_highlight(self):
        self.zap.step(1)
        self.zap.tuned(CHANNELS[1])
        self.clock.now += 0.1
        self.zap.step(1)
        highlights = []
        self.zap.highlightChanged.connect(lambda c, i: highlights.append((c, i)))
        self.zap.tuned(CHANNELS[3])
        self.assertEqual(highlights, [(CHANNELS[3], 3)])

    def test_numeric_zero_number(self):
        ctx = ZapContext("g", [{"id": 1, "number": 0}, {"id": 2, "number": 5}])
        self.assertEqual(ctx.by_number("0")["id"], 1)

    def test_previous_twice_toggles(self):
        self.zap.tuned(CHANNELS[2])
        self.zap.previous()
        self.zap.previous()
        self.assertEqual(self.tunes, [CHANNELS[0], CHANNELS[2]])
        self.zap.previous()
        self.assertEqual(self.tunes[-1], CHANNELS[0])


if __name__ == "__main__":
    unittest.main()
