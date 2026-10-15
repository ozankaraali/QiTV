"""The volume mixer overlay: one row per picture (Phase 4 Task 3b, the owner's amendment)."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from ui.overlays.mixer import TRANSPORT_BAND_U, Mixer, MixerRow
from ui.overlays.painting import Units

MAIN = MixerRow("Main", "Nature One HD", 80, False)
PIP = MixerRow("PiP", "Sports Two", 30, True)
LEFT = Qt.MouseButton.LeftButton


class MixerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, idle_ms=3000, size=(1280, 720)):
        host = QWidget()
        host.resize(*size)
        host.show()
        mixer = Mixer(host, idle_ms=idle_ms)
        self.addCleanup(host.close)
        return host, mixer


class MixerTests(MixerCase):
    def test_one_row_without_pip(self):
        host, mixer = self.make()
        mixer.show_rows([MAIN], 0)
        self.assertTrue(mixer.isVisible())
        self.assertEqual(mixer.rows(), [MAIN])
        self.assertEqual(mixer.selected(), 0)
        self.assertFalse(mixer.grab().isNull())
        one_row = mixer.height()
        mixer.show_rows([MAIN, PIP], 1)
        self.assertGreater(mixer.height(), one_row)
        self.assertEqual(mixer.selected(), 1)
        mixer.show_rows([MAIN], 1)  # PiP closed: the selection stays on a row there is
        self.assertEqual(mixer.selected(), 0)

    def test_two_rows_bottom_centre_above_the_transport(self):
        host, mixer = self.make()
        mixer.show_rows([MAIN, PIP], 0)
        geometry = mixer.geometry()
        self.assertLessEqual(abs(geometry.center().x() - host.width() // 2), 1)
        band = Units(host.width()).px(TRANSPORT_BAND_U)
        self.assertEqual(geometry.bottom() + 1, host.height() - band)
        self.assertTrue(host.rect().contains(geometry))
        self.assertLess(mixer.slider_rect(0).right(), mixer.width())
        self.assertLess(mixer.speaker_rect(1).bottom(), mixer.height())

    def test_it_never_covers_the_pip(self):
        host, mixer = self.make()
        mixer.show_rows([MAIN, PIP], 0)
        over = QRect(mixer.geometry().center() - QPoint(40, 40), mixer.geometry().size())
        mixer.avoid(over)
        self.assertFalse(mixer.geometry().intersects(over))
        self.assertLess(mixer.geometry().bottom(), over.top())  # above it
        mixer.avoid(QRect())
        self.assertEqual(mixer.geometry().bottom() + 1, host.height() - Units(1280).px(19))

    def test_it_hides_three_seconds_after_the_last_change(self):
        _host, mixer = self.make(idle_ms=60)
        closed = QSignalSpy(mixer.closed)
        mixer.show_rows([MAIN], 0)
        elapse(40)
        mixer.show_rows([MAIN], 0)  # a change: the full wait again
        elapse(40)
        self.assertTrue(mixer.isVisible())
        elapse(40)
        self.assertFalse(mixer.isVisible())
        self.assertEqual(closed.count(), 1)

    def test_a_click_on_a_speaker_asks_to_mute_that_row(self):
        _host, mixer = self.make()
        mixer.show_rows([MAIN, PIP], 0)
        mutes, selected = QSignalSpy(mixer.muteRequested), QSignalSpy(mixer.rowSelected)
        QTest.mouseClick(mixer, LEFT, pos=mixer.speaker_rect(1).center().toPoint())
        self.assertEqual(mutes.count(), 1)
        self.assertEqual(mutes.at(0), [1])
        self.assertEqual(selected.at(0), [1])
        self.assertEqual(mixer.selected(), 1)

    def test_a_click_or_drag_on_a_slider_sets_that_rows_level(self):
        _host, mixer = self.make(idle_ms=60)
        mixer.show_rows([MAIN, PIP], 0)
        levels = QSignalSpy(mixer.levelRequested)
        slider = mixer.slider_rect(1)
        y = round(slider.center().y())
        start = QPoint(round(slider.left() + slider.width() * 0.75), y)
        QTest.mousePress(mixer, LEFT, pos=start)
        self.assertEqual(levels.at(0)[0], 1)
        self.assertAlmostEqual(levels.at(0)[1], 75, delta=2)
        elapse(100)  # a drag keeps it up
        self.assertTrue(mixer.isVisible())
        QTest.mouseMove(mixer, QPoint(round(slider.left()) - 20, y))
        self.assertEqual(levels.at(levels.count() - 1), [1, 0])
        QTest.mouseRelease(mixer, LEFT, pos=QPoint(round(slider.left()) - 20, y))
        elapse(100)
        self.assertFalse(mixer.isVisible())

    def test_rows_show_names_and_levels_in_a_mini_player(self):
        host, mixer = self.make(size=(360, 203))
        mixer.show_rows([MAIN, PIP], 1)
        self.assertTrue(host.rect().contains(mixer.geometry()))
        self.assertFalse(mixer.grab().isNull())
        for row in (0, 1):
            slider = mixer.slider_rect(row)
            self.assertGreaterEqual(slider.left(), 0)
            self.assertLessEqual(slider.right(), mixer.width())


if __name__ == "__main__":
    unittest.main()
