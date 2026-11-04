"""The painted fields' caret and selection (2.9.3, ui.text_edit): the owner found that "ctrl a and
click to change cursor place do not work", text went in at its end only."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter
from PySide6.QtWidgets import QApplication

from ui.keymap import Action
from ui.text_edit import Dragging, TextEdit, edit_key, index_at, paint_edit


class TextEditTests(unittest.TestCase):
    def test_typing_goes_in_at_the_caret_and_over_the_selection(self):
        edit = TextEdit("salt")
        edit.place(2)
        self.assertTrue(edit.insert("X"))
        self.assertEqual((edit.text, edit.cursor), ("saXlt", 3))
        edit.select_all()
        edit.insert("tide")
        self.assertEqual((edit.text, edit.cursor, edit.has_selection()), ("tide", 4, False))

    def test_backspace_and_delete_take_the_selection_or_a_character(self):
        edit = TextEdit("harbour")
        edit.place(3)
        edit.backspace()
        self.assertEqual((edit.text, edit.cursor), ("habour", 2))
        edit.delete()
        self.assertEqual((edit.text, edit.cursor), ("haour", 2))
        edit.place(5, select=True)
        edit.backspace()
        self.assertEqual(edit.text, "ha")
        edit.place(0)
        self.assertFalse(edit.backspace())  # nothing before the caret

    def test_moves_by_character_word_and_to_an_end_and_select_with_shift(self):
        edit = TextEdit("the lighthouse keeper")
        edit.place(0)
        edit.move(1, word=True)
        self.assertEqual(edit.cursor, 3)
        edit.move(1, word=True, select=True)
        self.assertEqual(edit.selected_text(), " lighthouse")
        edit.move(-1)  # an arrow ends a selection at its side
        self.assertEqual((edit.cursor, edit.has_selection()), (3, False))
        edit.move(1, line=True)
        self.assertEqual(edit.cursor, len(edit.text))
        edit.move(-1, word=True)
        self.assertEqual(edit.cursor, len("the lighthouse "))

    def test_a_double_click_takes_the_word(self):
        edit = TextEdit("old harbour lights")
        edit.select_word(6)
        self.assertEqual(edit.selected_text(), "harbour")

    def test_the_same_text_from_elsewhere_keeps_the_caret_and_new_text_puts_it_at_the_end(self):
        edit = TextEdit("salt")
        edit.place(1)
        edit.set_text("salt")
        self.assertEqual((edit.cursor, edit.placed), (1, True))
        edit.set_text("tide")
        self.assertEqual((edit.cursor, edit.placed), (4, False))


class EditKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_arrows_stay_the_lists_until_the_caret_is_put_in_the_text(self):
        edit = TextEdit("salt")
        self.assertIsNone(edit_key(edit, Action.NAV_LEFT))  # the remote's ←: the lists'
        edit.place(4)  # a click
        self.assertIs(edit_key(edit, Action.NAV_LEFT), False)
        self.assertEqual(edit.cursor, 3)
        self.assertIsNone(edit_key(edit, Action.NAV_DOWN))  # on to the lists ...
        self.assertEqual((edit.cursor, edit.placed), (4, False))  # ... the caret at the end
        self.assertIsNone(edit_key(edit, Action.NAV_LEFT))
        self.assertIs(edit_key(edit, Action.NAV_LEFT, arrows=True), False)  # a search's own

    def test_shift_with_an_arrow_selects_from_where_the_caret_is(self):
        edit = TextEdit("salt")
        self.assertIs(edit_key(edit, Action.NAV_LEFT, "select word"), False)
        self.assertEqual(edit.selected_text(), "salt")
        self.assertTrue(edit_key(edit, Action.NAV_TEXT, "x"))
        self.assertEqual(edit.text, "x")

    def test_select_all_copy_cut_and_paste(self):
        edit = TextEdit("harbour")
        clipboard = QGuiApplication.clipboard()
        self.assertIs(edit_key(edit, Action.SELECT_ALL), False)
        edit_key(edit, Action.COPY)
        self.assertEqual(clipboard.text(), "harbour")
        self.assertTrue(edit_key(edit, Action.CUT))
        self.assertEqual(edit.text, "")
        clipboard.setText("  lights\nsecond line")
        self.assertTrue(edit_key(edit, Action.PASTE))
        self.assertEqual(edit.text, "lights")  # its first line

    def test_home_and_end_once_the_caret_is_in(self):
        edit = TextEdit("salt")
        self.assertIsNone(edit_key(edit, Action.NAV_HOME))
        edit.place(2)
        edit_key(edit, Action.NAV_HOME, "select")
        self.assertEqual(edit.selected_text(), "sa")


class PaintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def paint(self, edit, width=200):
        image = QImage(width + 20, 40, QImage.Format.Format_ARGB32)
        p = QPainter(image)
        font = QFont()
        font.setPixelSize(16)
        p.setFont(font)
        paint_edit(
            p, QRectF(10, 0, width, 40), edit, QColor("white"), caret=True, selection=QColor("red")
        )
        p.end()

    def test_a_click_finds_the_place_in_the_text(self):
        edit = TextEdit("harbour lights")
        self.paint(edit)
        self.assertEqual(index_at(edit, 0), 0)  # left of the text: its start
        self.assertEqual(index_at(edit, 500), len(edit.text))
        middle = index_at(edit, 60)
        self.assertTrue(0 < middle < len(edit.text), middle)

    def test_a_long_text_moves_so_the_caret_shows(self):
        edit = TextEdit("x" * 200)
        self.paint(edit, width=100)
        self.assertGreater(edit.scroll, 0)  # the end shows, where it is typed
        self.assertEqual(index_at(edit, 10 + 100), len(edit.text))
        edit.place(0)
        self.paint(edit, width=100)
        self.assertEqual(edit.scroll, 0)

    def test_a_drag_selects(self):
        edit = TextEdit("harbour lights")
        self.paint(edit)
        drag = Dragging()
        drag.press(edit, 10, shift=False)
        drag.move(500)
        drag.release()
        self.assertEqual(edit.selected_text(), "harbour lights")


if __name__ == "__main__":
    unittest.main()
