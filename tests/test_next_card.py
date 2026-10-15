import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.overlay_host import OverlayHost
from ui.overlays.next_card import NextCard


class NextCardCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self):
        window = QWidget()
        window.resize(1280, 720)
        host = OverlayHost(window, window)
        host.resize(1280, 720)
        card = NextCard()
        host.add_overlay(card)
        card.setGeometry(0, 0, 1280, 720)
        window.show()
        self.addCleanup(window.close)
        self.addCleanup(host.close)
        return host, card

    def wait_for(self, predicate, ms=3000):
        waited = 0
        while not predicate() and waited < ms:
            QTest.qWait(30)
            waited += 30

    def test_countdown_emits_play_now_once(self):
        _, card = self.make()
        spy = QSignalSpy(card.playNow)
        card.show_next("Ep 2", "S1 E2", seconds=1)
        self.assertTrue(card.isVisible())
        self.assertEqual(card.remaining(), 1)
        self.wait_for(lambda: spy.count() > 0)
        QTest.qWait(300)
        self.assertEqual(spy.count(), 1)
        self.assertFalse(card.isVisible())

    def test_escape_cancels_and_stops_timer(self):
        _, card = self.make()
        play = QSignalSpy(card.playNow)
        cancelled = QSignalSpy(card.cancelled)
        self.assertFalse(card.escape())
        card.show_next("Ep 2", "S1 E2", seconds=1)
        self.assertTrue(card.escape())
        self.assertEqual(cancelled.count(), 1)
        self.assertFalse(card.isVisible())
        QTest.qWait(1300)
        self.assertEqual(play.count(), 0)

    def test_activate_plays_once_and_stops(self):
        _, card = self.make()
        play = QSignalSpy(card.playNow)
        card.show_next("Ep 2", "S1 E2", seconds=1)
        card.activate()
        QTest.qWait(1300)
        self.assertEqual(play.count(), 1)
        self.assertFalse(card.isVisible())

    def test_click_buttons(self):
        _, card = self.make()
        play = QSignalSpy(card.playNow)
        card.show_next("Ep 2", "S1 E2", seconds=10)
        pos = card.button_rect("play").center().toPoint()
        QTest.mouseClick(card, Qt.LeftButton, Qt.NoModifier, pos)
        self.assertEqual(play.count(), 1)
        self.assertFalse(card.isVisible())
        cancelled = QSignalSpy(card.cancelled)
        card.show_next("Ep 3", "S1 E3", seconds=10)
        pos = card.button_rect("cancel").center().toPoint()
        QTest.mouseClick(card, Qt.LeftButton, Qt.NoModifier, pos)
        self.assertEqual(cancelled.count(), 1)
        self.assertFalse(card.isVisible())

    def click_host(self, host, pos):
        # Through the host's window, so Qt hit-tests to the child under the point.
        QTest.mouseClick(host.windowHandle(), Qt.LeftButton, Qt.NoModifier, pos)
        QTest.qWait(QApplication.doubleClickInterval() + 150)

    def test_clicks_on_card_do_not_reach_host(self):
        host, card = self.make()
        card.show_next("Ep 2", "S1 E2", seconds=30)
        clicks = QSignalSpy(host.clicked)
        # Through the host at the card's position: the card is the widget hit.
        body = card.geometry().center()
        self.assertIs(host.childAt(body), card)
        self.click_host(host, body)
        self.assertEqual(clicks.count(), 0)
        self.assertTrue(card.isVisible())
        # Positive control: a click on the host outside the card does emit.
        self.click_host(host, QPoint(20, 20))
        self.assertEqual(clicks.count(), 1)

    def test_label_kinds(self):
        _, card = self.make()
        card.show_next("Ep 2", "S1 E2", seconds=10)
        self.assertEqual(card.label(), "Next episode in 10 s")
        card.show_next("Movie 2", "2024", seconds=7, kind="movie")
        self.assertEqual(card.label(), "Next in 7 s")

    def test_resumes_when_parent_reshown(self):
        host, card = self.make()
        play = QSignalSpy(card.playNow)
        card.show_next("Ep 2", "S1 E2", seconds=1)
        card.parentWidget().hide()
        self.assertFalse(card._timer.isActive())
        QTest.qWait(1300)
        self.assertEqual(play.count(), 0)
        card.parentWidget().show()
        self.assertTrue(card._timer.isActive())
        self.wait_for(lambda: play.count() > 0)
        self.assertEqual(play.count(), 1)


if __name__ == "__main__":
    unittest.main()
