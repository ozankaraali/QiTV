"""The controller that applies the Settings layer's steps at once and routes its keys."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from services.settings_model import GUIDE, PLAYBACK, sections
from ui.keymap import Action
from ui.overlays.settings_drawer import SettingsDrawer
from ui.settings_controller import SettingsController


def _walk(rows):
    for row in rows:
        yield row
        if row.section is not None:
            yield from _walk(row.section.rows)


DEFAULTS = {
    row.key: row.default for s in sections("Darwin") for row in _walk(s.rows) if row.choices
}


class FakeValues:
    def __init__(self, **values):
        self.values = dict(values)
        self.sets = []

    def get(self, key):
        if key not in self.values:  # a row not set by the test reads as its default
            self.values[key] = DEFAULTS[key]
        return self.values[key]

    def set(self, key, value):
        self.sets.append((key, value))
        self.values[key] = value


class SettingsControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.values = FakeValues(live_latency="auto")
        self.legacy, self.reopened = [], []
        self.drawer = SettingsDrawer(
            lambda row: self.controller.value_text(row),
            lambda row: self.controller.lit(row),
            self.host,
        )
        self.controller = SettingsController(
            self.drawer,
            self.values,
            "Darwin",
            self.legacy.append,
            lambda: self.reopened.append(True),
        )

    def tearDown(self):
        # A mouse-free close arms no guard, but keep later modules clean all the same.
        elapse(QApplication.doubleClickInterval() + 80)

    def test_a_step_writes_the_next_value(self):
        self.controller.open(PLAYBACK)
        self.controller.handle(Action.NAV_RIGHT)  # into the rows
        for _ in range(30):
            row = self.drawer.focused_row()
            if row is not None and row.key == "live_latency":
                break
            self.controller.handle(Action.NAV_DOWN)
        self.assertEqual(self.drawer.focused_row().key, "live_latency")
        self.assertTrue(self.controller.handle(Action.NAV_RIGHT))
        self.assertEqual(self.values.sets, [("live_latency", "low")])

    def test_legacy_closes_then_opens_the_old_tab(self):
        self.controller.open(GUIDE)
        self.controller.handle(Action.NAV_RIGHT)
        self.controller.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.legacy, ["epg"])
        self.assertFalse(self.controller.visible())

    def test_back_from_the_sections_reopens_the_drawer_only_when_opened_from_it(self):
        self.controller.open(from_drawer=True)
        self.controller.handle(Action.ESCAPE)
        self.assertEqual(self.reopened, [True])
        self.assertFalse(self.controller.visible())
        self.controller.open()
        self.controller.handle(Action.NAV_BACKSPACE)
        self.assertEqual(self.reopened, [True])

    def test_close_does_not_reopen_the_drawer(self):
        self.controller.open(from_drawer=True)
        self.controller.close()
        self.assertFalse(self.controller.visible())
        self.assertEqual(self.reopened, [])

    def test_player_keys_pass_through(self):
        self.controller.open()
        self.assertFalse(self.controller.handle(Action.VOL_UP))
        self.assertFalse(self.controller.handle(Action.MUTE))


if __name__ == "__main__":
    unittest.main()
