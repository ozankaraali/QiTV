import unittest

from PySide6.QtCore import Qt

from ui.keymap import Action, resolve

NONE = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
CTRL = Qt.KeyboardModifier.ControlModifier
ALT = Qt.KeyboardModifier.AltModifier
KEYPAD = Qt.KeyboardModifier.KeypadModifier
K = Qt.Key


def closed(key, text="", mods=NONE):
    return resolve(int(key), mods, text, drawer_open=False)


def opened(key, text="", mods=NONE):
    return resolve(int(key), mods, text, drawer_open=True)


class ClosedKeymapTests(unittest.TestCase):
    """One assertion per row of the spec §5.3 table (no overlay open)."""

    def test_channel_keys(self):
        self.assertEqual(closed(K.Key_Up), (Action.CH_PREV, ""))
        self.assertEqual(closed(K.Key_Down), (Action.CH_NEXT, ""))
        self.assertEqual(closed(K.Key_PageUp), (Action.CH_PREV, ""))
        self.assertEqual(closed(K.Key_PageDown), (Action.CH_NEXT, ""))

    def test_macos_arrows_carry_the_keypad_modifier(self):
        self.assertEqual(closed(K.Key_Down, mods=KEYPAD), (Action.CH_NEXT, ""))

    def test_digits(self):
        self.assertEqual(closed(K.Key_7, "7"), (Action.DIGIT, "7"))
        self.assertEqual(closed(K.Key_0, "0", KEYPAD), (Action.DIGIT, "0"))
        self.assertIsNone(closed(K.Key_7, "7", CTRL))

    def test_backspace_is_previous_channel(self):
        self.assertEqual(closed(K.Key_Backspace), (Action.PREVIOUS, ""))

    def test_enter_and_return_open_the_drawer(self):
        self.assertEqual(closed(K.Key_Return, "\r"), (Action.OPEN_DRAWER, ""))
        self.assertEqual(closed(K.Key_Enter, "\r", KEYPAD), (Action.OPEN_DRAWER, ""))

    def test_left_right_seek(self):
        self.assertEqual(closed(K.Key_Left), (Action.SEEK_BACK, ""))
        self.assertEqual(closed(K.Key_Right), (Action.SEEK_FWD, ""))

    def test_space_pauses(self):
        self.assertEqual(closed(K.Key_Space, " "), (Action.PLAY_PAUSE, ""))

    def test_l_goes_live(self):
        self.assertEqual(closed(K.Key_L, "l"), (Action.GO_LIVE, ""))

    def test_g_guide(self):
        self.assertEqual(closed(K.Key_G, "g"), (Action.GUIDE, ""))

    def test_i_info(self):
        self.assertEqual(closed(K.Key_I, "i"), (Action.INFO, ""))
        self.assertEqual(closed(K.Key_I, "I", SHIFT), (Action.INFO, ""))

    def test_f_fullscreen(self):
        self.assertEqual(closed(K.Key_F, "f"), (Action.FULLSCREEN, ""))

    def test_m_mute(self):
        self.assertEqual(closed(K.Key_M, "m"), (Action.MUTE, ""))
        self.assertIsNone(closed(K.Key_M, "m", CTRL))

    def test_plus_minus_volume(self):
        self.assertEqual(closed(K.Key_Plus, "+", SHIFT), (Action.VOL_UP, ""))
        self.assertEqual(closed(K.Key_Equal, "="), (Action.VOL_UP, ""))
        self.assertEqual(closed(K.Key_Minus, "-"), (Action.VOL_DOWN, ""))
        self.assertEqual(closed(K.Key_Minus, "-", KEYPAD), (Action.VOL_DOWN, ""))

    def test_escape(self):
        self.assertEqual(closed(K.Key_Escape, "\x1b"), (Action.ESCAPE, ""))

    def test_alt_p_mini_player(self):
        self.assertEqual(closed(K.Key_P, "π", ALT), (Action.MINI_PLAYER, ""))
        self.assertIsNone(closed(K.Key_P, "p"))

    def test_unmapped_key(self):
        self.assertIsNone(closed(K.Key_Q, "q"))


class OpenKeymapTests(unittest.TestCase):
    def test_arrows_page_enter_backspace(self):
        self.assertEqual(opened(K.Key_Up), (Action.NAV_UP, ""))
        self.assertEqual(opened(K.Key_Down, mods=KEYPAD), (Action.NAV_DOWN, ""))
        self.assertEqual(opened(K.Key_PageUp), (Action.NAV_PAGE_UP, ""))
        self.assertEqual(opened(K.Key_PageDown), (Action.NAV_PAGE_DOWN, ""))
        self.assertEqual(opened(K.Key_Left), (Action.NAV_LEFT, ""))
        self.assertEqual(opened(K.Key_Right), (Action.NAV_RIGHT, ""))
        self.assertEqual(opened(K.Key_Return, "\r"), (Action.NAV_ACTIVATE, ""))
        self.assertEqual(opened(K.Key_Enter, "\r", KEYPAD), (Action.NAV_ACTIVATE, ""))
        self.assertEqual(opened(K.Key_Backspace, "\x08"), (Action.NAV_BACKSPACE, ""))

    def test_escape(self):
        self.assertEqual(opened(K.Key_Escape, "\x1b"), (Action.ESCAPE, ""))

    def test_text_filters(self):
        self.assertEqual(opened(K.Key_A, "a"), (Action.NAV_TEXT, "a"))
        self.assertEqual(opened(K.Key_A, "A", SHIFT), (Action.NAV_TEXT, "A"))
        self.assertEqual(opened(K.Key_7, "7"), (Action.NAV_TEXT, "7"))
        self.assertEqual(opened(K.Key_M, "m"), (Action.NAV_TEXT, "m"))
        self.assertEqual(opened(K.Key_Space, " "), (Action.NAV_TEXT, " "))

    def test_non_ascii_letters_are_text(self):
        for letter in "çğıöşüİ":
            self.assertEqual(opened(0, letter), (Action.NAV_TEXT, letter))
        # macOS Option composes letters on non-Turkish layouts
        self.assertEqual(opened(K.Key_C, "ç", ALT), (Action.NAV_TEXT, "ç"))

    def test_modifiers_other_than_shift_do_not_type(self):
        self.assertIsNone(opened(K.Key_A, "a", CTRL))
        self.assertIsNone(opened(K.Key_7, "7", ALT))
        self.assertIsNone(opened(K.Key_Tab, "\t"))

    def test_space_and_f_need_ctrl(self):
        self.assertEqual(opened(K.Key_Space, " ", CTRL), (Action.PLAY_PAUSE, ""))
        self.assertEqual(opened(K.Key_F, "f", CTRL), (Action.FULLSCREEN, ""))
        self.assertEqual(opened(K.Key_F, "f"), (Action.NAV_TEXT, "f"))


if __name__ == "__main__":
    unittest.main()
