"""StreamQuestion on its own: what was asked runs once the stopped recordings' streams are back."""

import os
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from timer_fixtures import wait_until

from ui import stream_question
from ui.stream_question import StreamQuestion


class Budget(QObject):
    """The streams each provider's purposes hold; one goes back when a recording lets it go."""

    released = Signal(str)

    def __init__(self, held):
        super().__init__()
        self.held = held  # provider -> [purpose, ...]

    def cap(self, provider, limit):
        return limit

    def holders(self, provider):
        return list(self.held.get(provider, []))

    def let_go(self, provider, purpose):
        self.held[provider].remove(purpose)
        self.released.emit(provider)


class Recording(SimpleNamespace):
    """A player that records, under its stream's purpose."""

    def __init__(self, purpose, name):
        super().__init__(
            name=purpose,
            tuner=SimpleNamespace(purpose=purpose),
            channel={"name": name},
            live=SimpleNamespace(recording=lambda: self.on),
            on=True,
        )


class StreamQuestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.one, self.two = Recording("rec1", "One"), Recording("rec2", "Two")
        self.budget = Budget({"P": ["rec1"], "Q": ["rec2"]})
        self.ran = []

        def stop(slot, replaced):
            slot.on = False  # its stream goes back later (`let_go`)

        self.question = StreamQuestion(
            budget=self.budget,
            recordings=lambda: [r for r in (self.one, self.two) if r.on],
            large=lambda: None,
            card=lambda: None,
            stop=stop,
            closed=lambda: False,
        )

    def ask(self, provider, purpose, name):
        asked = self.question.needs(
            provider, 1, purpose, lambda: self.ran.append(name), ask_on="phone"
        )
        self.assertTrue(asked)
        self.question.answer(True)

    def test_a_tiles_question_answered_meanwhile_keeps_the_open_that_waits(self):
        # 2.13.4 (bug scan #13): the second answer took the first one's place and its open
        # never ran, not even at the end of its wait
        self.ask("P", "main", "tune")
        self.assertEqual(self.ran, [])  # One's stream is not back yet
        tile = Recording("tile", "Three")
        self.assertTrue(
            self.question.confirm(tile, lambda: self.ran.append("tile"), ask_on="phone")
        )
        self.question.answer(True)
        self.assertEqual(self.ran, ["tile"])  # no stream to wait for: at once
        self.budget.let_go("P", "rec1")
        self.assertEqual(self.ran, ["tile", "tune"])

    def test_two_opens_at_two_providers_each_run_once_their_stream_is_back(self):
        self.ask("P", "main", "tune")
        self.ask("Q", "pip", "pip")
        self.budget.let_go("Q", "rec2")
        self.assertEqual(self.ran, ["pip"])
        self.budget.let_go("P", "rec1")
        self.assertEqual(self.ran, ["pip", "tune"])

    def test_each_open_waits_for_its_stream_as_long_as_it_would_alone(self):
        with mock.patch.object(stream_question, "RELEASE_WAIT_MS", 50):
            self.ask("P", "main", "tune")
            self.ask("Q", "pip", "pip")
        self.assertTrue(wait_until(lambda: len(self.ran) == 2))  # neither stream came back
        self.assertEqual(sorted(self.ran), ["pip", "tune"])

    def test_a_newer_open_of_the_same_player_stands_for_the_one_that_waits(self):
        self.ask("P", "main", "first")
        self.ask("Q", "main", "second")  # the viewer chose again meanwhile
        self.budget.let_go("Q", "rec2")
        self.budget.let_go("P", "rec1")
        self.assertEqual(self.ran, ["second"])


if __name__ == "__main__":
    unittest.main()
