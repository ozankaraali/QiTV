"""2.6: the question QiTV asks before it stops a recording (ui/overlays/question_card.py)."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.keymap import Action
from ui.overlays.question_card import QuestionCard

QUESTION = "No stream is free: One is recording. Stop the recording?"


class QuestionCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.card = QuestionCard(self.host)
        self.answers = []
        self.card.answered.connect(self.answers.append)
        self.card.ask(QUESTION, "Stop recording", "Keep recording")

    def tearDown(self):
        self.host.deleteLater()

    def test_it_shows_with_the_safe_answer_focused(self):
        self.assertTrue(self.card.isVisibleTo(self.host))
        self.assertEqual(self.card.question(), QUESTION)
        self.assertEqual(self.card.focused(), "no")

    def test_enter_on_the_first_focus_keeps_the_recording(self):
        self.assertTrue(self.card.handle(Action.OK))
        self.assertEqual(self.answers, [False])
        self.assertTrue(self.card.isHidden())

    def test_an_arrow_moves_to_stop_and_enter_stops(self):
        for action in (Action.SEEK_FWD, Action.NAV_RIGHT, Action.TAB, Action.CH_NEXT):
            self.card.ask(QUESTION, "Stop recording", "Keep recording")
            self.assertTrue(self.card.handle(action))
            self.assertEqual(self.card.focused(), "yes", action)
        self.assertTrue(self.card.handle(Action.NAV_ACTIVATE))
        self.assertEqual(self.answers, [True])

    def test_esc_and_backspace_keep_the_recording(self):
        self.card.handle(Action.SEEK_FWD)
        self.assertTrue(self.card.handle(Action.ESCAPE))
        self.card.ask(QUESTION, "Stop recording", "Keep recording")
        self.assertTrue(self.card.handle(Action.PREVIOUS))
        self.assertEqual(self.answers, [False, False])

    def test_a_click_answers(self):
        stop = self.card.button_rect("yes").center().toPoint()
        QTest.mouseClick(self.card, Qt.MouseButton.LeftButton, pos=stop)
        self.card.ask(QUESTION, "Stop recording", "Keep recording")
        keep = self.card.button_rect("no").center().toPoint()
        QTest.mouseClick(self.card, Qt.MouseButton.LeftButton, pos=keep)
        self.assertEqual(self.answers, [True, False])

    def test_other_keys_wait_for_the_answer_but_the_sound_does_not(self):
        for action in (Action.DIGIT, Action.PIP, Action.RECORD, Action.MEDIA_NEXT):
            self.assertTrue(self.card.handle(action), action)
        for action in (Action.MUTE, Action.VOL_UP, Action.FULLSCREEN):
            self.assertFalse(self.card.handle(action), action)
        self.assertEqual(self.answers, [])

    def test_a_hidden_card_takes_no_keys(self):
        self.card.escape()
        self.assertFalse(self.card.handle(Action.OK))
        self.assertEqual(self.answers, [False])

    def test_left_unanswered_it_keeps_the_recording(self):
        self.card.ask(QUESTION, "Stop recording", "Keep recording", ms=20)
        QTest.qWait(80)
        self.assertEqual(self.answers, [False])
        self.assertTrue(self.card.isHidden())

    def test_a_new_question_replaces_the_one_shown(self):
        self.card.handle(Action.SEEK_FWD)
        self.card.ask("Another?", "Stop recording", "Keep recording")
        self.assertEqual(self.answers, [])  # the newer question stands for both
        self.assertEqual(self.card.focused(), "no")
        self.assertEqual(self.card.question(), "Another?")


if __name__ == "__main__":
    unittest.main()
