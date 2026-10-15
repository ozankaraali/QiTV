import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.overlays.transport import Transport


class TransportCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, idle_ms=3000):
        t = Transport(idle_ms=idle_ms)
        t.setGeometry(0, 0, 1280, 720)
        t.show()
        t.grab()  # warm up font loading so timer tests are not skewed by first paint
        self.addCleanup(t.close)
        return t

    def click(self, t, point):
        QTest.mouseClick(t, Qt.LeftButton, Qt.NoModifier, point)

    def test_vod_texts(self):
        t = self.make()
        t.show_vod("Salt Roads", "2024")
        t.update_position(30_000, 120_000)
        self.assertEqual(t.texts(), ("0:30", "-1:30"))

    def test_seek_click(self):
        t = self.make()
        t.show_vod("A", "b")
        t.update_position(0, 120_000)
        spy = QSignalSpy(t.seekRequested)
        r = t.seek_rect()
        self.click(t, QPoint(int(r.x() + r.width() * 0.75), int(r.center().y())))
        self.assertEqual(spy.count(), 1)
        self.assertAlmostEqual(spy.at(0)[0], 90_000, delta=100)

    def test_drag_previews_then_emits_on_release(self):
        t = self.make()
        t.show_vod("A", "b")
        t.update_position(0, 100_000)
        spy = QSignalSpy(t.seekRequested)
        r = t.seek_rect()
        y = int(r.center().y())
        QTest.mousePress(t, Qt.LeftButton, Qt.NoModifier, QPoint(int(r.x() + 5), y))
        QTest.mouseMove(t, QPoint(int(r.x() + r.width() / 2), y))
        self.assertEqual(spy.count(), 0)
        self.assertEqual(t.texts()[0], "0:50")
        QTest.mouseRelease(t, Qt.LeftButton, Qt.NoModifier, QPoint(int(r.x() + r.width() / 2), y))
        self.assertEqual(spy.count(), 1)

    def test_buttons_emit(self):
        t = self.make()
        t.show_vod("A", "b")
        t.update_position(0, 100_000)
        cases = {
            "play": t.playPauseRequested,
            "fullscreen": t.fullscreenRequested,
            "audio": t.audioRequested,
            "subtitles": t.subtitleRequested,
        }
        for name, sig in cases.items():
            spy = QSignalSpy(sig)
            self.click(t, t.button_rect(name).center().toPoint())
            self.assertEqual(spy.count(), 1, name)
        spy = QSignalSpy(t.seekRelativeRequested)
        self.click(t, t.button_rect("back10").center().toPoint())
        self.click(t, t.button_rect("fwd10").center().toPoint())
        self.assertEqual([spy.at(0)[0], spy.at(1)[0]], [-10_000, 10_000])

    def test_next_button_only_when_available(self):
        t = self.make()
        t.show_vod("A", "b")
        base = {k: v for k, v in t._layout().items()}
        self.assertNotIn("next", base)
        t.set_next_available(True)
        spy = QSignalSpy(t.nextRequested)
        self.click(t, t.button_rect("next").center().toPoint())
        self.assertEqual(spy.count(), 1)
        self.assertGreater(t.button_rect("next").x(), t.button_rect("fwd10").right() - 1)
        self.assertLess(t.button_rect("next").right(), t.button_rect("speed").x() + 1)
        t.set_next_available(False)
        self.assertEqual(t._layout(), base)
        self.assertNotIn("next", t._layout())
        t.set_next_available(True)
        t.show_live("104", "Nature")
        self.assertNotIn("next", t._layout())

    def test_speed_cycles(self):
        t = self.make()
        t.show_vod("A", "b")
        spy = QSignalSpy(t.speedRequested)
        for _ in range(4):
            self.click(t, t.button_rect("speed").center().toPoint())
        self.assertEqual([spy.at(i)[0] for i in range(4)], [1.25, 1.5, 2.0, 1.0])

    def test_idle_hide_and_poke(self):
        t = self.make(idle_ms=60)
        t.show_vod("A", "b")
        QTest.qWait(300)
        self.assertFalse(t.isVisible())
        t.poke()
        self.assertTrue(t.isVisible())
        t.dismiss()
        self.assertFalse(t.isVisible())

    def test_live(self):
        t = self.make()
        t.show_live("104", "Nature")
        self.assertEqual(t.texts(), ("LIVE", ""))
        spy = QSignalSpy(t.seekRequested)
        vod = Transport()
        vod.setGeometry(0, 0, 1280, 720)
        vod.show_vod("A", "b")
        vod.update_position(0, 100_000)
        where = vod.seek_rect().center().toPoint()
        vod.close()
        self.click(t, where)
        self.assertEqual(spy.count(), 0)
        play = QSignalSpy(t.playPauseRequested)
        self.click(t, t.button_rect("play").center().toPoint())
        self.assertEqual(play.count(), 1)

    def test_drag_survives_idle(self):
        t = self.make(idle_ms=60)
        t.show_vod("A", "b")
        t.update_position(0, 100_000)
        spy = QSignalSpy(t.seekRequested)
        r = t.seek_rect()
        y = int(r.center().y())
        QTest.mousePress(t, Qt.LeftButton, Qt.NoModifier, QPoint(int(r.x() + 5), y))
        QTest.qWait(250)
        self.assertTrue(t.isVisible())
        QTest.mouseRelease(t, Qt.LeftButton, Qt.NoModifier, QPoint(int(r.center().x()), y))
        self.assertEqual(spy.count(), 1)
        self.assertTrue(t.isVisible())
        QTest.qWait(250)
        self.assertFalse(t.isVisible())

    def test_mouse_move_keeps_alive(self):
        t = self.make(idle_ms=200)
        t.show_vod("A", "b")
        for i in range(6):
            QTest.qWait(60)
            QTest.mouseMove(t, QPoint(300 + i * 7, 300))
        self.assertTrue(t.isVisible())

    def test_play_click_scales(self):
        for w, h in ((800, 450), (1920, 1080)):
            t = Transport()
            t.setGeometry(0, 0, w, h)
            t.show()
            t.show_vod("A", "b")
            spy = QSignalSpy(t.playPauseRequested)
            self.click(t, t.button_rect("play").center().toPoint())
            self.assertEqual(spy.count(), 1, w)
            t.close()

    def test_mask_only_bars(self):
        t = self.make()
        t.show_vod("A", "b")
        mask = t.mask_region()
        self.assertTrue(mask.contains(QPoint(640, 10)))
        self.assertTrue(mask.contains(QPoint(640, 700)))
        self.assertFalse(mask.contains(QPoint(640, 360)))


class HostSizingTests(unittest.TestCase):
    """The transport fills its host, as the banner and toast lay themselves out from it."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def host(self, width=1280, height=720):
        host = QWidget()
        host.resize(width, height)
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        return host

    def test_fills_its_host_from_the_start(self):
        host = self.host()
        transport = Transport(host)
        self.assertEqual(transport.geometry(), host.rect())

    def test_fills_its_host_on_show_and_after_every_resize(self):
        host = self.host()
        transport = Transport(host)
        host.show()
        transport.show_vod("Film", "")
        self.assertEqual(transport.geometry(), host.rect())
        host.resize(1600, 900)
        self.assertEqual(transport.geometry(), QRect(0, 0, 1600, 900))
        transport.dismiss()
        host.resize(960, 540)  # while hidden too: the next show needs no layout pass
        self.assertEqual(transport.geometry(), QRect(0, 0, 960, 540))
        transport.poke()
        self.assertEqual(transport.geometry(), host.rect())

    def test_a_host_resized_before_it_was_ever_shown_is_followed_on_show(self):
        host = self.host(400, 300)
        transport = Transport(host)
        host.resize(1280, 720)  # pending until the host shows
        transport.show_live("News", "")
        self.assertEqual(transport.geometry(), QRect(0, 0, 1280, 720))


if __name__ == "__main__":
    unittest.main()
