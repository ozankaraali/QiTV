"""Settings › Time-shift & recordings in the drawer (plan 3): the disk buffer turned on (from the
next channel) or off (asked first: what it kept goes), the memory per stream with how far back
it reaches, the disk cache's size on a value card (applied at once), and the Recordings
folder: changed, back to the default, opened. Over a real TV window whose time-shift helper is
a stand-in; nothing reaches the network or the system's file manager."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from timer_fixtures import wait_until

from services import recording, settings_model as m, system_open
from services.config_manager import ConfigManager
from services.i18n import translate
from tests.test_settings_general import GeneralCase
from ui.keymap import Action
from ui.live_tuner import LiveStart, start_kwargs
from ui.overlays import settings_card
from ui.overlays.settings_card import CANCEL, OK
from ui.settings_timeshift import TURN_OFF, TURN_OFF_QUESTION

MIB = 1024 * 1024


class TimeshiftCase(GeneralCase):
    disk = False  # the disk buffer on as the window starts

    def prepare_config(self):
        self.config.timeshift_enabled = self.disk

    def setUp(self):
        super().setUp()
        self.kept, self.engine = mock.Mock(), mock.Mock()
        for name, part in (("kept", self.kept), ("engine", self.engine)):
            patch = mock.patch.object(self.window, name, part)
            patch.start()
            self.addCleanup(patch.stop)

    @property
    def rows(self):
        return self.window.timeshift_rows

    def index(self, key):
        return list(self.shown()).index(key)


class DiskOnTests(TimeshiftCase):
    disk = True

    def test_turning_the_disk_off_asks_first(self):
        row = self.open_at(m.TIMESHIFT, "timeshift_enabled")
        self.assertEqual((row.kind, row.default), (m.STEP, False))
        self.assertEqual(self.window.settings.value_text(row), "On")
        self.assertEqual(self.shown()[m.DISK_CACHE].kind, m.ACTION)
        # Every way to step it asks first, Cancel where the keys start; Cancel and Esc keep it.
        for action in (Action.NAV_RIGHT, Action.NAV_LEFT, Action.NAV_ACTIVATE, Action.NAV_RIGHT):
            self.key(action)
            question = self.rows.question
            self.assertTrue(self.window.settings.card_shown(), action)
            self.assertEqual(question.title(), translate(TURN_OFF_QUESTION))
            self.assertEqual(question.focus(), ("button", CANCEL))
            self.assertTrue(self.config.timeshift_enabled)  # nothing yet
            self.key(Action.NAV_ACTIVATE if action is not Action.NAV_RIGHT else Action.ESCAPE)
            self.assertFalse(self.window.settings.card_shown())
            self.assertTrue(self.config.timeshift_enabled)
        # The mouse's › asks too.
        QTest.mouseClick(
            self.drawer,
            Qt.MouseButton.LeftButton,
            pos=self.drawer.step_rect(self.index(row.key), 1).center().toPoint(),
        )
        self.assertTrue(self.window.settings.card_shown())
        self.key(Action.ESCAPE)
        # Settings going with the question up keeps it on.
        self.key(Action.NAV_RIGHT)
        self.window.settings.close()
        self.assertTrue(wait_until(lambda: not self.window.settings.visible()))
        self.assertTrue(self.rows.question.isHidden())
        self.assertTrue(self.config.timeshift_enabled)
        self.kept.set_disk.assert_not_called()
        self.engine.drop_kept.assert_not_called()
        # Turn off: off, saved, and what it kept goes.
        self.open_at(m.TIMESHIFT, "timeshift_enabled")
        self.key(Action.NAV_RIGHT)
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.rows.question.focus(), ("button", TURN_OFF))
        self.key(Action.NAV_ACTIVATE)
        self.assertFalse(self.window.settings.card_shown())
        self.assertFalse(self.config.timeshift_enabled)
        self.kept.set_disk.assert_called_once_with(False)
        self.engine.drop_kept.assert_called_once_with()
        self.assertEqual(self.window.settings.value_text(self.drawer.focused_row()), "Off")
        self.assertEqual(self.shown()[m.DISK_CACHE].kind, m.INFO)  # nothing on disk to size
        self.config.save_config(wait=True)
        self.assertFalse(ConfigManager().timeshift_enabled)

    def test_disk_cache_card_sets_the_amount_at_once(self):
        row = self.open_at(m.TIMESHIFT, m.DISK_CACHE)
        self.assertEqual((row.kind, row.note), (m.ACTION, "2 GiB"))
        self.assertIn("at once", self.drawer._help_text(row))
        self.key(Action.NAV_ACTIVATE)
        card = self.rows.range_card
        self.assertTrue(self.window.settings.card_shown())
        self.assertEqual(card.title(), translate(row.text))
        self.assertEqual((card.value(), card.value_text()), (2048, "2 GiB"))
        self.assertEqual(
            card.note_text(), "1080p at 8 Mb/s: About 36 min\n4K at 25 Mb/s: About 11 min"
        )
        self.key(Action.NAV_RIGHT)  # one step: 256 MiB
        self.assertEqual((card.value(), card.value_text()), (2304, "2.25 GiB"))
        self.assertEqual(
            card.note_text(), "1080p at 8 Mb/s: About 40 min\n4K at 25 Mb/s: About 13 min"
        )
        self.key(Action.ESCAPE)  # cancelled: nothing changes
        self.assertEqual(self.config.timeshift_max_mib, 2048)
        self.engine.set_disk_amount.assert_not_called()
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(card.value(), 2048)  # from the size as it is
        self.key(Action.NAV_PAGE_UP)  # ten steps
        self.assertEqual(card.value(), 4608)
        self.key(Action.NAV_ACTIVATE)  # kept
        self.assertFalse(self.window.settings.card_shown())
        self.assertEqual(self.config.timeshift_max_mib, 4608)
        self.engine.set_disk_amount.assert_called_once_with(4608 * MIB)  # at once
        self.assertEqual(self.shown()[m.DISK_CACHE].note, "4.5 GiB")
        self.assertEqual(self.drawer.focused_row().key, m.DISK_CACHE)
        # The ends, by Home and End; OK by the mouse's way too (its button).
        self.key(Action.NAV_ACTIVATE)
        self.key(Action.NAV_END)
        self.assertEqual((card.value(), card.value_text()), (16384, "16 GiB"))
        self.key(Action.NAV_HOME)
        self.assertEqual((card.value(), card.value_text()), (256, "256 MiB"))
        self.key(Action.NAV_DOWN)
        self.assertEqual(card.focus(), ("button", OK))
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.config.timeshift_max_mib, 256)
        self.engine.set_disk_amount.assert_called_with(256 * MIB)
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().timeshift_max_mib, 256)


class DiskOffTests(TimeshiftCase):
    def test_section_rows_and_texts(self):
        self.assertNotIn(m.TIMESHIFT, m.LEGACY_TABS)
        context = m.Context(
            timeshift_disk=True, recordings_folder="/media/QiTV", recordings_own=True
        )
        section = {s.key: s for s in m.sections("Linux", (), context)}[m.TIMESHIFT]
        self.assertEqual(section.legacy_tab, "")
        self.assertEqual(
            [row.key for row in section.rows],
            [
                "timeshift_enabled",
                "timeshift_ram_mib",
                m.DISK_CACHE,
                m.RECORDINGS_FOLDER,
                m.CHANGE_FOLDER,
                m.DEFAULT_FOLDER,
                m.OPEN_FOLDER,
            ],
        )
        self.assertEqual(
            [row.text for row in section.rows],
            [
                "Use disk for a longer time-shift buffer",
                "Memory per stream",
                "Disk cache size",
                "Recordings folder",
                "Change…",
                "Use the default",
                "Open folder",
            ],
        )
        self.assertEqual(
            [row.kind for row in section.rows],
            [m.STEP, m.STEP, m.ACTION, m.INFO, m.ACTION, m.ACTION, m.ACTION],
        )
        for row in section.rows:
            self.assertTrue(row.help, row.key)
            self.assertLessEqual(len(translate(row.help)), 90, row.key)
        # In the window, at 1280 × 720: each stepper's title in two lines at most.
        self.window.resize(1280, 720)
        self.open_at(m.TIMESHIFT, "timeshift_enabled")
        self.drawer.grab()
        for key in ("timeshift_enabled", "timeshift_ram_mib"):
            box = self.drawer._box("right", self.index(key))
            self.assertLessEqual(len(box.titles), 2, key)
            self.assertNotIn("…", "".join(box.titles), key)

    def test_turning_it_on_applies_from_the_next_channel(self):
        start = LiveStart("http://tv.example/live.m3u8", "QiTV", True)
        self.assertFalse(start_kwargs(start, self.config)["disk"])
        row = self.open_at(m.TIMESHIFT, "timeshift_enabled")
        self.assertIn("next channel", self.drawer._help_text(row))
        self.assertEqual(self.shown()[m.DISK_CACHE].kind, m.INFO)  # nothing to size yet
        self.assertEqual(self.shown()[m.DISK_CACHE].note, "2 GiB")
        self.key(Action.NAV_RIGHT)  # on: nothing asked
        self.assertFalse(self.window.settings.card_shown())
        self.assertTrue(self.config.timeshift_enabled)
        self.kept.set_disk.assert_called_once_with(True)
        self.engine.drop_kept.assert_not_called()
        self.assertTrue(start_kwargs(start, self.config)["disk"])  # the next tune's
        self.assertEqual(self.shown()[m.DISK_CACHE].kind, m.ACTION)  # its size can change now
        self.assertEqual(self.drawer.focused_row().key, "timeshift_enabled")
        self.config.save_config(wait=True)
        self.assertTrue(ConfigManager().timeshift_enabled)

    def test_memory_note_follows_the_choice(self):
        row = self.open_at(m.TIMESHIFT, "timeshift_ram_mib")
        self.assertEqual([c.value for c in row.choices], [128, 256, 512])
        self.assertEqual([c.shown() for c in row.choices], ["128 MiB", "256 MiB", "512 MiB"])
        self.assertEqual(row.default, 256)
        self.assertEqual(self.window.settings.value_text(row), "256 MiB")
        self.assertEqual(row.sub, "≈ 4 min of HD at 8 Mb/s")
        self.assertIn("≈ 4 min of HD at 8 Mb/s", self.drawer._spoken())
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.config.timeshift_ram_mib, 512)
        self.assertEqual(self.drawer.focused_row().sub, "≈ 9 min of HD at 8 Mb/s")
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.config.timeshift_ram_mib, 128)
        self.assertEqual(self.drawer.focused_row().sub, "≈ 2 min of HD at 8 Mb/s")
        self.assertEqual(self.drawer.focused_row().key, "timeshift_ram_mib")
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().timeshift_ram_mib, 128)
        # While the disk is off, its size is no row to act on: the keys go past it.
        self.key(Action.NAV_DOWN)
        self.assertEqual(self.drawer.focused_row().key, m.CHANGE_FOLDER)
        self.engine.set_disk_amount.assert_not_called()

    def test_recordings_folder_actions(self):
        default = recording.default_folder()
        chosen = os.path.join(self.config_dir, "Elsewhere", "")
        self.open_at(m.TIMESHIFT, m.CHANGE_FOLDER)
        self.assertEqual(self.shown()[m.RECORDINGS_FOLDER].note, default)
        self.assertNotIn(m.DEFAULT_FOLDER, self.shown())  # it is the default already
        with mock.patch.object(settings_card, "pick_folder", return_value=None) as pick:
            self.key(Action.NAV_ACTIVATE)  # Change…, then the picker cancelled: nothing
        pick.assert_called_once_with(self.window, "Choose the Recordings folder", default)
        self.assertEqual(self.config.recordings_dir, "")
        with mock.patch.object(settings_card, "pick_folder", return_value=chosen):
            self.key(Action.NAV_ACTIVATE)
        folder = os.path.normpath(chosen)
        self.assertEqual(self.config.recordings_dir, folder)
        self.assertEqual(self.shown()[m.RECORDINGS_FOLDER].note, folder)
        self.assertEqual(recording.places(self.config), [folder, default])  # still listed
        self.assertIn(m.DEFAULT_FOLDER, self.shown())
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().recordings_dir, folder)
        # Open folder: the system's file manager; a folder not there yet says so.
        self.key(Action.NAV_DOWN)
        self.key(Action.NAV_DOWN)
        self.assertEqual(self.drawer.focused_row().key, m.OPEN_FOLDER)
        with mock.patch.object(system_open, "open_folder", return_value=False) as opened:
            self.key(Action.NAV_ACTIVATE)
        opened.assert_called_once_with(folder)
        self.assertIn((translate(recording.NO_RECORDINGS), "info"), self.toasts)
        said = len(self.toasts)
        with mock.patch.object(system_open, "open_folder", return_value=True) as opened:
            self.key(Action.NAV_ACTIVATE)
        opened.assert_called_once_with(folder)
        self.assertEqual(len(self.toasts), said)
        # Use the default: back to it; the folder used before stays listed, and the row goes.
        self.key(Action.NAV_UP)
        self.assertEqual(self.drawer.focused_row().key, m.DEFAULT_FOLDER)
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.config.recordings_dir, "")
        self.assertEqual(self.shown()[m.RECORDINGS_FOLDER].note, default)
        self.assertEqual(recording.places(self.config), [default, folder])
        self.assertNotIn(m.DEFAULT_FOLDER, self.shown())
        self.assertIn(self.drawer.focused_row().key, (m.CHANGE_FOLDER, m.OPEN_FOLDER))
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().recordings_dir, "")


if __name__ == "__main__":
    unittest.main()
