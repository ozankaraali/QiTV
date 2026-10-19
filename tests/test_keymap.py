import unittest

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from ui.keymap import (
    CONTEXTS,
    SHORTCUT_TITLES,
    SHORTCUTS,
    Action,
    as_int,
    key_hint,
    resolve,
    shortcut_keys,
)

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


def guide(key, text="", mods=NONE):
    return resolve(int(key), mods, text, context="guide")


META = Qt.KeyboardModifier.MetaModifier


def resolve_text(key_text, context):
    """resolve() for a QKeySequence text such as "Ctrl+F" or "a", with the text it types."""
    combo = QKeySequence(key_text)[0]
    key, mods = combo.key().value, combo.keyboardModifiers()
    held = mods & (CTRL | ALT | META)
    text = chr(key).lower() if not held and 0x20 <= key < 0x7F else ""
    return resolve(key, mods, text, context=context)


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
        self.assertEqual(closed(K.Key_P, "p"), (Action.PIP, ""))  # plain P: Phase 4's PiP

    def test_p_picture_in_picture_and_s_swap(self):
        self.assertEqual(closed(K.Key_P, "p"), (Action.PIP, ""))
        self.assertEqual(closed(K.Key_P, "P", SHIFT), (Action.PIP, ""))
        self.assertEqual(closed(K.Key_S, "s"), (Action.SWAP, ""))
        self.assertEqual(closed(K.Key_P, "p", CTRL), (Action.PIP, ""))
        self.assertIsNone(closed(K.Key_S, "s", CTRL))

    def test_tab_switches_the_mixers_rows(self):
        self.assertEqual(closed(K.Key_Tab, "\t"), (Action.TAB, ""))

    def test_j_and_k_move_the_sound_and_with_shift_the_subtitles(self):
        # 2.2, VLC's J and K; its G and H are the subtitles' there, but G is the guide here.
        self.assertEqual(closed(K.Key_J, "j"), (Action.SOUND_EARLIER, ""))
        self.assertEqual(closed(K.Key_K, "k"), (Action.SOUND_LATER, ""))
        self.assertEqual(closed(K.Key_J, "J", SHIFT), (Action.SUBTITLES_EARLIER, ""))
        self.assertEqual(closed(K.Key_K, "K", SHIFT), (Action.SUBTITLES_LATER, ""))
        self.assertIsNone(closed(K.Key_J, "j", CTRL))
        self.assertIsNone(closed(K.Key_K, "˚", ALT))

    def test_j_and_k_are_free_everywhere_else(self):
        # The channel list and the Library type them; the guide does nothing with them.
        for mods, text in ((NONE, "j"), (SHIFT, "J")):
            self.assertEqual(opened(K.Key_J, text, mods), (Action.NAV_TEXT, text))
            self.assertEqual(library(K.Key_J, text, mods), (Action.NAV_TEXT, text))
            self.assertIsNone(guide(K.Key_J, text, mods))
            self.assertIsNone(guide(K.Key_K, text.replace("j", "k").replace("J", "K"), mods))
        claimed = {  # the screens that act on letters (the others type them)
            key
            for context in ("player", "guide")
            for keys, _what in SHORTCUTS[context]
            for key in shortcut_keys(keys)
            if key in ("J", "K", "H", "Shift+J", "Shift+K")
        }
        self.assertEqual(claimed, {"J", "K", "Shift+J", "Shift+K"})

    def test_other_shifted_letters_still_act_as_their_letters(self):
        self.assertEqual(closed(K.Key_L, "L", SHIFT), (Action.GO_LIVE, ""))
        self.assertEqual(closed(K.Key_M, "M", SHIFT), (Action.MUTE, ""))

    def test_media_volume_keys(self):
        self.assertEqual(closed(K.Key_VolumeUp), (Action.VOL_UP, ""))
        self.assertEqual(closed(K.Key_VolumeDown), (Action.VOL_DOWN, ""))
        self.assertEqual(closed(K.Key_VolumeMute), (Action.MUTE, ""))
        self.assertEqual(opened(K.Key_VolumeUp), (Action.VOL_UP, ""))  # they never type

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

    def test_p_types_and_ctrl_p_opens_the_highlighted_channel_in_the_pip(self):
        # The drawer types to filter ("Premier…"): Ctrl/⌘+P is its PiP key, as Ctrl+D its
        # favorite one.
        self.assertEqual(opened(K.Key_P, "p"), (Action.NAV_TEXT, "p"))
        self.assertEqual(opened(K.Key_S, "s"), (Action.NAV_TEXT, "s"))
        self.assertEqual(opened(K.Key_P, "p", CTRL), (Action.PIP, ""))

    def test_media_keys_work_with_the_drawer_open(self):
        self.assertEqual(opened(K.Key_MediaTogglePlayPause), (Action.PLAY_PAUSE, ""))
        self.assertEqual(opened(K.Key_MediaNext), (Action.MEDIA_NEXT, ""))

    def test_space_needs_ctrl_and_f_types(self):
        self.assertEqual(opened(K.Key_Space, " ", CTRL), (Action.PLAY_PAUSE, ""))
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


class GuideKeymapTests(unittest.TestCase):
    def test_navigation_keys(self):
        self.assertEqual(guide(K.Key_Up), (Action.NAV_UP, ""))
        self.assertEqual(guide(K.Key_Down, mods=KEYPAD), (Action.NAV_DOWN, ""))
        self.assertEqual(guide(K.Key_PageUp), (Action.NAV_PAGE_UP, ""))
        self.assertEqual(guide(K.Key_PageDown), (Action.NAV_PAGE_DOWN, ""))
        self.assertEqual(guide(K.Key_Left), (Action.NAV_LEFT, ""))
        self.assertEqual(guide(K.Key_Right), (Action.NAV_RIGHT, ""))
        self.assertEqual(guide(K.Key_Return, "\r"), (Action.NAV_ACTIVATE, ""))
        self.assertEqual(guide(K.Key_Enter, "\r", KEYPAD), (Action.NAV_ACTIVATE, ""))
        self.assertEqual(guide(K.Key_Escape, "\x1b"), (Action.ESCAPE, ""))

    def test_g_closes_and_playback_keys_still_work(self):
        self.assertEqual(guide(K.Key_G, "g"), (Action.GUIDE, ""))
        self.assertEqual(guide(K.Key_G, "G", SHIFT), (Action.GUIDE, ""))
        self.assertEqual(guide(K.Key_Space, " "), (Action.PLAY_PAUSE, ""))
        self.assertEqual(guide(K.Key_M, "m"), (Action.MUTE, ""))
        self.assertEqual(guide(K.Key_F, "f"), (Action.FULLSCREEN, ""))
        self.assertEqual(guide(K.Key_Minus, "-"), (Action.VOL_DOWN, ""))
        self.assertEqual(guide(K.Key_MediaTogglePlayPause), (Action.PLAY_PAUSE, ""))

    def test_digits_and_channel_keys_reach_the_guide(self):
        # digits jump to a channel number; Ch± move the highlight (the guide routes them)
        self.assertEqual(guide(K.Key_7, "7"), (Action.DIGIT, "7"))
        self.assertEqual(guide(K.Key_0, "0", KEYPAD), (Action.DIGIT, "0"))
        self.assertEqual(guide(K.Key_ChannelUp), (Action.CH_NEXT, ""))
        self.assertEqual(guide(K.Key_ChannelDown), (Action.CH_PREV, ""))

    def test_nothing_types_or_goes_back(self):
        self.assertIsNone(guide(K.Key_A, "a"))
        self.assertIsNone(guide(K.Key_Backspace, "\x08"))
        self.assertIsNone(guide(K.Key_G, "g", CTRL))
        self.assertIsNone(guide(K.Key_7, "7", CTRL))


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

    def test_ctrl_f_searches_and_f11_or_ctrl_meta_f_toggle_fullscreen_everywhere(self):
        # Owner's ruling: Ctrl/⌘+F is the quick search in every context; plain F stays the
        # player's fullscreen, and F11 and ⌃⌘F (Ctrl+Meta+F) toggle it from anywhere.
        for context in ("player", "drawer", "guide", "library"):
            with self.subTest(context=context):
                search = resolve(K.Key_F.value, CTRL, "f", context=context)
                self.assertEqual(search, (Action.SEARCH, ""))
                for key, mods in ((K.Key_F11, NONE), (K.Key_F, CTRL | META)):
                    self.assertEqual(
                        resolve(key.value, mods, "", context=context), (Action.FULLSCREEN, "")
                    )
        self.assertEqual(closed(K.Key_F, "f"), (Action.FULLSCREEN, ""))

    def test_ctrl_d_is_favorite_everywhere(self):
        for context in ("player", "drawer", "guide", "library"):
            self.assertEqual(
                resolve(K.Key_D.value, CTRL, "d", context=context), (Action.FAVORITE, "")
            )

    def test_unknown_context(self):
        with self.assertRaises(ValueError):
            resolve(K.Key_Up.value, NONE, "", context="menu")

    def test_ctrl_p_is_only_the_players_and_the_drawers(self):
        for context in ("guide", "library"):
            self.assertIsNone(resolve(K.Key_P.value, CTRL, "p", context=context), context)

    def test_new_actions_exist(self):
        for name in ("SEARCH", "FAVORITE", "NAV_HOME", "NAV_END", "LIBRARY", "GUIDE"):
            self.assertIn(name, Action.__members__)


class KeyHintTests(unittest.TestCase):
    """On-screen controls name their key in their tooltip (owner's mouse-only rule, 2.0.1)."""

    def test_a_hint_names_the_key_as_this_computer_does(self):
        native = QKeySequence.SequenceFormat.NativeText
        self.assertEqual(key_hint("Guide", "G"), "Guide (G)")
        self.assertEqual(
            key_hint("Search", "Ctrl+F"), f"Search ({QKeySequence('Ctrl+F').toString(native)})"
        )
        self.assertEqual(
            key_hint("Previous channel", "Backspace"),
            f"Previous channel ({QKeySequence('Backspace').toString(native)})",
        )

    def test_arrows_read_as_arrows_everywhere(self):
        self.assertEqual(key_hint("Earlier", "Left"), "Earlier (←)")
        self.assertEqual(key_hint("Later", "Right"), "Later (→)")


if __name__ == "__main__":
    unittest.main()


class ShortcutTableTests(unittest.TestCase):
    """Help → Keyboard Shortcuts lists SHORTCUTS: every key there resolves in its context."""

    def test_alternatives_and_ranges_expand_to_single_keys(self):
        self.assertEqual(shortcut_keys("Up, Down"), ["Up", "Down"])
        self.assertEqual(shortcut_keys("/, Ctrl+F"), ["/", "Ctrl+F"])
        self.assertEqual(shortcut_keys("Ctrl+,"), ["Ctrl+,"])
        self.assertEqual(shortcut_keys("0…9"), [str(d) for d in range(10)])
        self.assertEqual(len(shortcut_keys("A…Z")), 26)

    def test_the_table_has_a_titled_section_per_context(self):
        self.assertEqual(list(SHORTCUTS), list(SHORTCUT_TITLES))
        self.assertEqual(set(SHORTCUTS) - {"menu"}, set(CONTEXTS))
        for rows in SHORTCUTS.values():
            self.assertTrue(rows)
            for keys, what in rows:
                self.assertTrue(keys and what)

    def test_every_listed_key_resolves_in_its_context(self):
        # The menu's keys are checked against the window's menus (test_tv_window).
        for context, rows in SHORTCUTS.items():
            if context == "menu":
                continue
            for keys, what in rows:
                for key in shortcut_keys(keys):
                    with self.subTest(context=context, key=key, what=what):
                        self.assertIsNotNone(resolve_text(key, context))

    def test_menu_keys_are_never_claimed_by_a_screen(self):
        # resolve() claims a key before any menu shortcut (ShortcutOverride): a menu row the
        # keymap also resolves would never reach its menu entry (the Ctrl+F clash).
        for keys, what in SHORTCUTS["menu"]:
            for key in shortcut_keys(keys):
                for context in CONTEXTS:
                    with self.subTest(key=key, what=what, context=context):
                        self.assertIsNone(resolve_text(key, context))

    def test_search_and_fullscreen_rows_follow_the_ruling(self):
        rows = {
            (context, what): keys
            for context, entries in SHORTCUTS.items()
            for keys, what in entries
        }
        for context in ("player", "drawer", "guide", "library"):
            self.assertEqual(rows[(context, "Search")], "Ctrl+F", context)
        self.assertEqual(rows[("player", "Fullscreen")], "F, F11, Ctrl+Meta+F")
