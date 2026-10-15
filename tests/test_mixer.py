"""The volume mixer overlay: one row per picture (Phase 4 Task 3b, the owner's amendment)."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse, timeline

from ui.overlays.mixer import BOTTOM_U, FLOOR_GAP_U, Mixer, MixerRow
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

    def test_bottom_centre_4u_from_the_bottom(self):
        host, mixer = self.make()
        mixer.show_rows([MAIN, PIP], 0)
        geometry = mixer.geometry()
        self.assertLessEqual(abs(geometry.center().x() - host.width() // 2), 1)
        self.assertEqual(geometry.bottom() + 1, host.height() - Units(1280).px(BOTTOM_U))
        self.assertTrue(host.rect().contains(geometry))
        self.assertLess(mixer.slider_rect(0).right(), mixer.width())
        self.assertLess(mixer.speaker_rect(1).bottom(), mixer.height())

    def test_just_above_the_transport_while_it_shows(self):
        host, mixer = self.make()
        mixer.show_rows([MAIN, PIP], 0)
        mixer.set_floor(600)  # the transport's controls start there
        self.assertEqual(mixer.geometry().bottom() + 1, 600 - Units(1280).px(FLOOR_GAP_U))
        mixer.set_floor(None)
        self.assertEqual(mixer.geometry().bottom() + 1, host.height() - Units(1280).px(BOTTOM_U))

    def test_it_never_covers_the_pip(self):
        host, mixer = self.make()
        mixer.show_rows([MAIN, PIP], 0)
        over = QRect(mixer.geometry().center() - QPoint(40, 40), mixer.geometry().size())
        mixer.avoid(over)
        self.assertFalse(mixer.geometry().intersects(over))
        self.assertLess(mixer.geometry().bottom(), over.top())  # above it
        mixer.avoid(QRect())
        self.assertEqual(mixer.geometry().bottom() + 1, host.height() - Units(1280).px(BOTTOM_U))

    def test_it_hides_three_seconds_after_the_last_change(self):
        # Timer order, not wall-clock waits (CI): each step runs as its probe fires, before
        # any timer due later; the change at 100 ms restarts the 200 ms wait (due at 300+).
        # The first show is a step too: shown before the probes start, a runner stalled for
        # 100 ms between the two found the mixer's wait due before the change at 100 ms.
        _host, mixer = self.make(idle_ms=200)
        closed = QSignalSpy(mixer.closed)
        show = lambda: mixer.show_rows([MAIN], 0)  # noqa: E731
        _, _, shown = timeline((0, show), (100, show), (250, mixer.isVisible))
        self.assertTrue(shown)
        self.assertEqual(closed.count(), 0)
        elapse(250)  # past the restarted wait however late it started
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
