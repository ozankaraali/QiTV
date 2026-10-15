import unittest

from PySide6.QtCore import Qt

from ui.keymap import Action, as_int, resolve

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


def library(key, text="", mods=NONE):
    return resolve(int(key), mods, text, context="library")


META = Qt.KeyboardModifier.MetaModifier


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

    def test_enter_and_return_are_ok(self):
        # OK opens the drawer, or tunes the fast-zap column's highlight while it is open
        self.assertEqual(closed(K.Key_Return, "\r"), (Action.OK, ""))
        self.assertEqual(closed(K.Key_Enter, "\r", KEYPAD), (Action.OK, ""))

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

    def test_remote_channel_keys(self):
        self.assertEqual(closed(K.Key_ChannelUp), (Action.CH_NEXT, ""))
        self.assertEqual(closed(K.Key_ChannelDown), (Action.CH_PREV, ""))

    def test_media_keys(self):
        for key in (K.Key_MediaTogglePlayPause, K.Key_MediaPlay, K.Key_MediaPause):
            self.assertEqual(closed(key), (Action.PLAY_PAUSE, ""))
        self.assertEqual(closed(K.Key_MediaNext), (Action.MEDIA_NEXT, ""))
        self.assertEqual(closed(K.Key_MediaPrevious), (Action.MEDIA_PREV, ""))


class AsIntTests(unittest.TestCase):
    def test_accepts_enums_ints_and_none(self):
        self.assertEqual(as_int(K.Key_A), 0x41)
        self.assertEqual(as_int(CTRL | SHIFT), (CTRL | SHIFT).value)
        self.assertEqual(as_int(7), 7)
        self.assertEqual(as_int(None), 0)


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

    def test_windows_altgr_types(self):
        altgr = CTRL | ALT  # Windows reports AltGr as Ctrl+Alt
        for key, text in ((K.Key_L, "ł"), (K.Key_Q, "@"), (K.Key_E, "€")):
            self.assertEqual(
                resolve(key.value, altgr, text, drawer_open=True, platform="win32"),
                (Action.NAV_TEXT, text),
            )
        self.assertIsNone(opened(K.Key_A, "\x01", CTRL))  # Ctrl+A is a control character

    def test_ctrl_alt_is_a_shortcut_outside_windows(self):
        for platform in ("darwin", "linux"):  # ⌘⌥ on macOS; Linux reports AltGr otherwise
            self.assertIsNone(
                resolve(K.Key_E.value, CTRL | ALT, "e", drawer_open=True, platform=platform)
            )

    def test_media_keys_work_with_the_drawer_open(self):
        self.assertEqual(opened(K.Key_MediaTogglePlayPause), (Action.PLAY_PAUSE, ""))
        self.assertEqual(opened(K.Key_MediaNext), (Action.MEDIA_NEXT, ""))

    def test_space_and_f_need_ctrl(self):
        self.assertEqual(opened(K.Key_Space, " ", CTRL), (Action.PLAY_PAUSE, ""))
        self.assertEqual(opened(K.Key_F, "f", CTRL), (Action.FULLSCREEN, ""))
        self.assertEqual(opened(K.Key_F, "f"), (Action.NAV_TEXT, "f"))


class LibraryKeymapTests(unittest.TestCase):
    def test_navigation_keys(self):
        self.assertEqual(library(K.Key_Up), (Action.NAV_UP, ""))
        self.assertEqual(library(K.Key_Down, mods=KEYPAD), (Action.NAV_DOWN, ""))
        self.assertEqual(library(K.Key_Left), (Action.NAV_LEFT, ""))
        self.assertEqual(library(K.Key_Right), (Action.NAV_RIGHT, ""))
        self.assertEqual(library(K.Key_PageUp), (Action.NAV_PAGE_UP, ""))
        self.assertEqual(library(K.Key_PageDown), (Action.NAV_PAGE_DOWN, ""))
        self.assertEqual(library(K.Key_Home), (Action.NAV_HOME, ""))
        self.assertEqual(library(K.Key_End), (Action.NAV_END, ""))
        self.assertEqual(library(K.Key_Return, "\r"), (Action.NAV_ACTIVATE, ""))
        self.assertEqual(library(K.Key_Enter, "\r", KEYPAD), (Action.NAV_ACTIVATE, ""))
        self.assertEqual(library(K.Key_Escape, "\x1b"), (Action.ESCAPE, ""))
        self.assertEqual(library(K.Key_Backspace, "\x08"), (Action.NAV_BACKSPACE, ""))

    def test_text_types(self):
        self.assertEqual(library(K.Key_A, "a"), (Action.NAV_TEXT, "a"))
        self.assertEqual(library(K.Key_S, "S", SHIFT), (Action.NAV_TEXT, "S"))
        self.assertEqual(library(K.Key_Space, " "), (Action.NAV_TEXT, " "))
        self.assertEqual(library(K.Key_F, "f"), (Action.NAV_TEXT, "f"))
        self.assertEqual(library(K.Key_D, "d"), (Action.NAV_TEXT, "d"))

    def test_slash_and_ctrl_f_search(self):
        # "/" carries its text: a zone that takes text (search, genre filter) types it
        self.assertEqual(library(K.Key_Slash, "/"), (Action.SEARCH, "/"))
        self.assertEqual(library(K.Key_7, "/", SHIFT), (Action.SEARCH, "/"))  # Shift+7 layouts
        self.assertEqual(library(K.Key_F, "f", CTRL), (Action.SEARCH, ""))

    def test_ctrl_d_favorite(self):
        self.assertEqual(library(K.Key_D, "d", CTRL), (Action.FAVORITE, ""))

    def test_fullscreen_keeps_a_key(self):
        # Ctrl+F searches here, so fullscreen takes Ctrl+Meta+F (macOS ⌃⌘F) and F11
        self.assertEqual(library(K.Key_F, "f", CTRL | META), (Action.FULLSCREEN, ""))
        self.assertEqual(library(K.Key_F11), (Action.FULLSCREEN, ""))

    def test_shared_shortcuts(self):
        self.assertEqual(library(K.Key_MediaTogglePlayPause), (Action.PLAY_PAUSE, ""))
        self.assertEqual(library(K.Key_P, "π", ALT), (Action.MINI_PLAYER, ""))
        self.assertEqual(library(K.Key_Space, " ", CTRL), (Action.PLAY_PAUSE, ""))


class ContextTests(unittest.TestCase):
    def test_drawer_open_is_an_alias_of_the_context(self):
        for key, text, mods in (
            (K.Key_Up, "", NONE),
            (K.Key_Return, "\r", NONE),
            (K.Key_A, "a", NONE),
            (K.Key_F, "f", CTRL),
            (K.Key_Backspace, "\x08", NONE),
        ):
            self.assertEqual(
                resolve(key.value, mods, text, context="drawer"),
                resolve(key.value, mods, text, drawer_open=True),
            )
            self.assertEqual(
                resolve(key.value, mods, text, context="player"),
                resolve(key.value, mods, text, drawer_open=False),
            )

    def test_player_is_the_default_context(self):
        self.assertEqual(resolve(K.Key_Up.value, NONE, ""), (Action.CH_PREV, ""))

    def test_library_keys_stay_in_the_library(self):
        self.assertIsNone(closed(K.Key_Slash, "/"))
        self.assertEqual(opened(K.Key_Slash, "/"), (Action.NAV_TEXT, "/"))
        self.assertEqual(opened(K.Key_F, "f", CTRL), (Action.FULLSCREEN, ""))
        self.assertIsNone(opened(K.Key_D, "d", CTRL))

    def test_unknown_context(self):
        with self.assertRaises(ValueError):
            resolve(K.Key_Up.value, NONE, "", context="guide")

    def test_new_actions_exist(self):
        for name in ("SEARCH", "FAVORITE", "NAV_HOME", "NAV_END", "LIBRARY", "GUIDE", "OPEN_FILE"):
            self.assertIn(name, Action.__members__)


if __name__ == "__main__":
    unittest.main()
