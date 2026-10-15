"""The PiP and the mixer keep clear of the transport's controls (Phase 4 rulings)."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import wait_until

from ui.bottom_band import BottomBand
from ui.overlays.mixer import BOTTOM_U, Mixer, MixerRow
from ui.overlays.painting import Units
from ui.overlays.transport import Transport
from ui.pip import PipFrame, corner_rect

SHIFTED = {
    "start": 0.0,
    "end": 60.0,
    "position": 30.0,
    "live_edge": 60.0,
    "lag": 30.0,
    "mode": "shifted",
    "rate": 1.0,
    "paused": False,
}


class Config:
    pip_corner = "br"

    def save_config(self):
        pass


class BottomBandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # The host covers the page (as in the window): page and host coordinates agree.
        self.page = QWidget()
        self.page.resize(1280, 720)
        self.main, self.pip = QWidget(self.page), QWidget(self.page)
        self.frame = PipFrame(self.page, self.main, self.pip, Config())
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.transport = Transport(self.host)
        self.mixer = Mixer(self.host)
        self.band = BottomBand(self.transport, self.mixer, self.frame)
        for widget in (self.page, self.host):
            widget.show()
            self.addCleanup(widget.close)
        self.frame.show_pip()
        self.corner = corner_rect(self.page.size(), "br")
        self.gap = Units(1280).px(1)

    def settled(self):
        """The small rect once the lift animation (LIFT_MS, started at once) has ended."""
        self.assertTrue(wait_until(lambda: not self.frame.lifting(), 2000))
        return self.frame.small_rect()

    def test_the_pip_rises_above_the_transport_and_settles_back(self):
        self.transport.show_live("One", "")
        rect = self.settled()
        self.assertLessEqual(rect.bottom() + 1, self.transport.controls_top() - self.gap)
        self.assertEqual(rect.size(), self.corner.size())
        self.transport.dismiss()
        self.assertEqual(self.settled(), self.corner)

    def test_a_taller_band_lifts_it_further(self):
        self.transport.show_live("One", "")
        live = self.settled()
        self.transport.show_timeshift("One", "")
        self.transport.update_timeline(SHIFTED, [])
        shifted = self.settled()
        self.assertLess(shifted.top(), live.top())
        self.assertLessEqual(shifted.bottom() + 1, self.transport.controls_top() - self.gap)

    def test_the_mixer_sits_on_the_transport_or_4u_from_the_bottom(self):
        self.mixer.show_rows([MixerRow("Main", "One", 80, False)], 0)
        bottom = self.host.height() - Units(1280).px(BOTTOM_U)
        self.assertEqual(self.mixer.geometry().bottom() + 1, bottom)
        self.assertLess(self.settled().bottom(), self.corner.bottom())  # it rises for the mixer
        self.transport.show_live("One", "")
        self.assertLessEqual(self.mixer.geometry().bottom() + 1, self.transport.controls_top())
        self.transport.dismiss()
        self.assertEqual(self.mixer.geometry().bottom() + 1, bottom)
        self.mixer.dismiss()
        self.assertEqual(self.settled(), self.corner)


if __name__ == "__main__":
    unittest.main()
