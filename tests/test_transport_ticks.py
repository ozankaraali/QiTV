"""2.12: a tick of the player bar measures and paints only what it changed (the transport laid
its row out at every tick, 4 times a second, shown or hidden, and repainted the whole
window-sized overlay)."""

from datetime import timedelta
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect
from PySide6.QtGui import QFontMetrics, QFontMetricsF
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QWidget
from test_transport_timeshift import NOW, PROGRAMME, at_edge, timeline

from services import i18n
from ui.overlays.painting import ui_units
from ui.overlays.transport import Transport


class TickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, now=lambda: NOW) -> Transport:
        host = QWidget()
        host.resize(1280, 720)
        host.show()
        self.addCleanup(host.close)
        t = Transport(host, idle_ms=3000, now=now)
        t.show_timeshift("104 · Nature One HD", "Wild Coasts")
        t.update_timeline(at_edge(), PROGRAMME)
        t.grab()
        return t

    def measured(self) -> list:
        """The texts measured from now on (every horizontalAdvance call)."""
        calls: list = []
        for cls in (QFontMetrics, QFontMetricsF):
            original = cls.horizontalAdvance

            def advance(metrics, *args, _original=original):
                calls.append(args)
                return _original(metrics, *args)

            patcher = mock.patch.object(cls, "horizontalAdvance", advance)
            patcher.start()
            self.addCleanup(patcher.stop)
        return calls

    def updates(self, t: Transport) -> mock.MagicMock:
        """update() calls on `t` from now on, with what each asked to repaint."""
        patcher = mock.patch.object(t, "update", wraps=t.update)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_a_hidden_transport_only_keeps_what_its_ticks_say(self):
        t = self.make()
        t.dismiss()
        band = QSignalSpy(t.bandChanged)
        measured, updates = self.measured(), self.updates(t)
        for n in range(40):  # paused behind live: every tick says another lag
            t.update_timeline(timeline(lag=38.0 + n / 4, paused=True), PROGRAMME)
        self.assertEqual(measured, [])
        updates.assert_not_called()
        self.assertEqual(band.count(), 0)
        t.poke()  # it shows: laid out then, the bar that came meanwhile with it
        self.assertEqual(band.count(), 1)
        self.assertIn("seek", t._layout())
        self.assertEqual(t.texts()[1], "−00:47 behind live")

    def test_a_tick_that_changes_nothing_measures_and_paints_nothing(self):
        t = self.make()  # shown, at the live edge
        measured, updates = self.measured(), self.updates(t)
        for _ in range(8):
            t.update_timeline(at_edge(), PROGRAMME)
        self.assertEqual(measured, [])
        updates.assert_not_called()

    def test_a_tick_behind_live_repaints_the_bottom_band_alone(self):
        t = self.make()
        t.update_timeline(timeline(), PROGRAMME)  # behind live: the bar shows
        layout = t._layout()
        measured, updates = self.measured(), self.updates(t)
        t.update_timeline(timeline(start=0.1), PROGRAMME)  # the buffer's oldest moment went
        updates.assert_not_called()  # the knob's pixel and the texts stay as they were
        t.update_timeline(timeline(position=3400.0, lag=80.0), PROGRAMME)  # 42 s further back
        [call] = updates.call_args_list
        [rect] = call.args
        self.assertIsInstance(rect, QRect)
        self.assertLess(rect.top(), layout["seek"].top() - ui_units(1280).px(2))  # its bubble
        self.assertGreater(rect.top(), 720 * 0.6)  # not the title row's, nor the picture's
        self.assertEqual((rect.left(), rect.right(), rect.bottom()), (0, 1279, 719))
        self.assertIs(t._layout(), layout)  # nothing moved: nothing measured again
        self.assertEqual(measured, [])

    def test_behind_live_the_bubble_follows_the_clock_once_a_second(self):
        # Playing behind live at 1x every tick says the same: the bubble's time of day (the
        # moment playing) still goes on with the clock.
        clock = [NOW]
        t = self.make(now=lambda: clock[0])
        t.update_timeline(timeline(), PROGRAMME)
        updates = self.updates(t)
        clock[0] = NOW + timedelta(milliseconds=250)
        t.update_timeline(timeline(), PROGRAMME)  # the same second
        updates.assert_not_called()
        clock[0] = NOW + timedelta(seconds=1)
        t.update_timeline(timeline(), PROGRAMME)
        [call] = updates.call_args_list
        [rect] = call.args
        self.assertGreater(rect.top(), 720 * 0.6)  # the band, the bubble in it
        self.assertEqual(t.bubble_text(), "19:41:23")

    def test_a_film_repaints_its_band_once_a_second_not_at_every_tick(self):
        t = self.make()
        t.show_vod("A film", "")
        t.update_position(1000, 7_200_500)
        updates = self.updates(t)
        t.update_position(1250, 7_200_500)  # the same seconds, the same pixel
        updates.assert_not_called()
        t.update_position(2000, 7_200_500)
        [call] = updates.call_args_list
        self.assertGreater(call.args[0].top(), 720 * 0.6)

    def test_a_control_that_moves_lays_out_and_repaints_it_all(self):
        t = self.make()
        before = t._layout()
        updates = self.updates(t)
        t.update_timeline(at_edge(rate=1.05), PROGRAMME)  # catching up: the indicator's text
        updates.assert_called_once_with()
        self.assertIsNot(t._layout(), before)
        self.assertIn("catching up", t.indicator_text())
        t.set_quality_available(True)  # the ⚙
        self.assertIn("quality", t._layout())
        t.resize(900, 506)
        self.assertLess(t._layout()["fullscreen"].right(), 900)

    def test_a_language_lays_it_out_again(self):
        t = self.make()
        english = t._layout()["live"]
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.addCleanup(i18n.set_language, "en")
        self.assertIsNot(t._layout()["live"], english)

    def test_the_settled_indicator_width_is_measured_once_for_a_width_and_language(self):
        t = self.make()
        un = ui_units(t.width())
        width = t._steady_width(un)
        measured = self.measured()
        self.assertEqual(t._steady_width(un), width)
        self.assertEqual(measured, [])


if __name__ == "__main__":
    unittest.main()
