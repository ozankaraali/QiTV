"""Settings' Keys and About QiTV (2.13.1): the Help menu's two windows as sections of Settings.

Keys lists the keymap's SHORTCUTS table as the README's key tables do, a layer of lines to read
for each screen; About QiTV its version, the credits, TMDB's notice and logo, and its links."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import QApplication, QWidget

from scripts import readme_keys
from services import i18n, settings_model as m
from services.config_manager import get_app_version
from ui.keymap import SHORTCUT_TITLES, SHORTCUTS
from ui.overlays.painting import ui_units
from ui.overlays.settings_drawer import SettingsDrawer
from ui.overlays.settings_layout import RIGHT, VALUE_FONT_U, columns
from ui.settings_about import (
    ABOUT_PREFIX,
    CREDITS_TEXT,
    HOME_URL,
    ISSUES_URL,
    KEYS_PREFIX,
    LOGO_U,
    NOTICES_LABEL,
    NOTICES_URL,
    TMDB_LOGO_FILE,
    TMDB_LOGO_U,
    TMDB_NOTICE,
    AboutRows,
    about_rows,
    key_label,
    keys_rows,
)

app = QApplication.instance() or QApplication([])


class FakeSettings:
    def __init__(self):
        self.handlers = {}

    def register(self, prefix, handler):
        self.handlers[prefix] = handler


class KeysRowsTests(unittest.TestCase):
    def test_a_row_for_each_screen_as_the_readme_groups_them(self):
        rows = keys_rows()
        self.assertEqual([r.key for r in rows], [KEYS_PREFIX + c for c in SHORTCUTS])
        self.assertEqual([r.text for r in rows], [SHORTCUT_TITLES[c] for c in SHORTCUTS])
        # The README's key tables come from the same table, under the same titles
        self.assertEqual(
            [line for line in readme_keys.markdown().splitlines() if line.startswith("**")],
            [f"**{r.text}**" for r in rows],
        )
        for row, (context, entries) in zip(rows, SHORTCUTS.items()):
            with self.subTest(context=context):
                self.assertEqual(row.kind, m.OPENS)
                layer = row.section
                self.assertTrue(layer.browse)  # lines to read: the keys scroll them
                self.assertEqual(layer.text, SHORTCUT_TITLES[context])
                self.assertEqual([r.text for r in layer.rows], [what for _keys, what in entries])
                self.assertEqual(
                    [r.note for r in layer.rows], [key_label(keys) for keys, _what in entries]
                )
                for line in layer.rows:
                    self.assertEqual((line.kind, line.whole), (m.INFO, True))
        keys = [line.key for row in rows for line in row.section.rows]
        self.assertEqual(len(keys), len(set(keys)))  # "Search", "Move": one key each
        self.assertEqual(len(keys), sum(len(entries) for entries in SHORTCUTS.values()))

    def test_keys_read_as_this_computer_names_them(self):
        self.assertEqual(key_label("0…9"), "0–9")
        self.assertEqual(key_label("A…Z"), "A–Z")
        self.assertEqual(key_label("M"), "M")
        self.assertIn(" / ", key_label("Up, Down"))

    def test_the_sections_list_them_and_the_handler_owns_their_rows(self):
        settings = FakeSettings()
        rows = AboutRows(settings)
        self.assertIs(settings.handlers[KEYS_PREFIX], rows)
        self.assertIs(settings.handlers[ABOUT_PREFIX], rows)
        context = m.Context(keys=rows.keys(), about=rows.about())
        by_key = {s.key: s for s in m.sections("Linux", context=context)}
        self.assertEqual(by_key[m.KEYS].rows, keys_rows())
        self.assertEqual(by_key[m.ABOUT].rows, about_rows())
        line = keys_rows()[0].section.rows[0]
        self.assertEqual(rows.value(line), "")  # no value of its own: its keys (the note) show


class AboutRowsTests(unittest.TestCase):
    def rows(self):
        return {row.key: row for row in about_rows()}

    def test_it_names_the_version_the_credits_and_tmdb(self):
        rows = about_rows()
        version = next(r for r in rows if r.note == get_app_version())
        self.assertEqual((version.text, version.literal, version.kind), ("QiTV", True, m.INFO))
        self.assertIn("STB portals, Xtream and M3U playlists", version.sub)
        credits = next(r for r in rows if r.text == CREDITS_TEXT)
        for name in ("VLC", "Qt", "Python", "OpenSubtitles", "iptv-org", "TMDB"):
            self.assertIn(name, credits.text)
        for name in ("Jellyfin", "Emby", "Chromecast"):  # 2.8: what it works with
            self.assertIn(name, credits.text)
        self.assertTrue(credits.whole)
        # TMDB's terms' own template (2.8), and their logo under it, far less prominent than
        # QiTV's own (its logo above the version)
        self.assertEqual(
            TMDB_NOTICE,
            "QiTV uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise"
            " approved by TMDB.",
        )
        keys = [r.key for r in rows]
        notice = next(i for i, r in enumerate(rows) if r.text == TMDB_NOTICE)
        logo = rows[notice + 1]
        self.assertEqual((logo.image, logo.image_u), (TMDB_LOGO_FILE, TMDB_LOGO_U))
        qitv = next(r for r in rows if r.image == "qitv")
        self.assertLess(keys.index(qitv.key), keys.index(version.key))
        self.assertLessEqual(TMDB_LOGO_U, LOGO_U / 6)

    def test_its_links_open_in_the_browser(self):
        settings = FakeSettings()
        handler = AboutRows(settings)
        links = [r for r in about_rows() if r.kind == m.ACTION]
        self.assertEqual(
            [(r.text, r.literal) for r in links],
            [(NOTICES_LABEL, False), (HOME_URL, True), ("Report an issue", False)],
        )
        with mock.patch("ui.settings_about.system_open.open_url") as open_url:
            for row in links:
                handler.chosen(row)
            handler.chosen(next(r for r in about_rows() if r.kind == m.INFO))  # nothing
        self.assertEqual(
            [c.args[0] for c in open_url.call_args_list], [NOTICES_URL, HOME_URL, ISSUES_URL]
        )
        self.assertTrue(NOTICES_URL.endswith("THIRD-PARTY-NOTICES.md"))


class FitTests(unittest.TestCase):
    """At 1280 × 720, in English and in Turkish, no line of Keys or About is cut short."""

    def drawer(self):
        host = QWidget()
        host.resize(1280, 720)
        self.addCleanup(host.deleteLater)
        drawer = SettingsDrawer(lambda row: "", lambda row: False, host)
        return drawer

    def assert_whole(self, drawer, rows):
        un = ui_units(1280)
        area = columns(un, 720.0)[1][RIGHT]
        boxes = drawer._boxes(RIGHT, un, area)
        fm = QFontMetricsF(un.font(VALUE_FONT_U))
        for row, box in zip(rows, boxes):
            with self.subTest(row=row.key):
                for line in box.titles:
                    self.assertFalse(line.endswith("…"), line)
                if row.note:
                    self.assertGreaterEqual(
                        box.end.width() + 0.5, fm.horizontalAdvance(row.note), row.note
                    )

    def test_no_line_is_cut_short(self):
        for language in ("en", "tr"):
            with self.subTest(language=language):
                self.assertEqual(i18n.set_language(language), language)
                self.addCleanup(i18n.install_qt_translations, app)  # (English again)
                self.addCleanup(i18n.set_language, "en")
                i18n.install_qt_translations(app)  # Qt's key names too: "Üst Karakter+J"
                drawer = self.drawer()
                sections = m.sections(
                    "Linux", context=m.Context(keys=keys_rows(), about=about_rows())
                )
                for row in keys_rows():
                    drawer.open_at(sections, m.KEYS)
                    drawer.right()
                    while drawer.focused_row() is not None and drawer.focused_row().key != row.key:
                        drawer.move_by(1)
                    drawer.activate()
                    self.assertEqual(drawer.path(), (m.KEYS, row.key))
                    self.assert_whole(drawer, row.section.rows)
                drawer.open_at(sections, m.ABOUT)
                self.assert_whole(drawer, about_rows())


if __name__ == "__main__":
    unittest.main()
