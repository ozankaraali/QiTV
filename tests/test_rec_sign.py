"""2.6: the REC sign of recordings going on while something else is watched."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from tests.timer_fixtures import wait_until
from ui.overlays.painting import Units
from ui.overlays.rec_sign import RecSign


class RecSignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.now = [163.0]
        self.sign = RecSign(self.host, clock=lambda: self.now[0])
        self.back, self.stopped = [], []
        self.sign.goBack.connect(self.back.append)
        self.sign.stop.connect(self.stopped.append)

    def tearDown(self):
        self.host.deleteLater()

    def test_it_shows_for_a_recording_and_counts_its_time(self):
        self.assertTrue(self.sign.isHidden())
        self.sign.set_recordings([("main", "One", 100.0)])
        self.assertTrue(self.sign.isVisibleTo(self.host))
        self.assertEqual(self.sign.text(), "REC One 01:03")
        self.now[0] = 100.0 + 3723
        self.assertEqual(self.sign.text(), "REC One 1:02:03")

    def test_several_are_counted(self):
        self.sign.set_recordings([("main", "One", 100.0), ("b1", "Two", 120.0)])
        self.assertEqual(self.sign.text(), "REC 2 channels")

    def test_none_hides_it(self):
        self.sign.set_recordings([("main", "One", 100.0)])
        self.sign.set_recordings([])
        self.assertTrue(self.sign.isHidden())

    def test_its_menu_goes_back_or_stops(self):
        self.sign.set_recordings([("main", "One", 100.0), ("b1", "Two", 120.0)])
        menu = self.sign.menu()
        texts = [a.text() for a in menu.actions() if not a.isSeparator()]
        self.assertEqual(
            texts,
            [
                "Go back to One",
                "Stop recording",
                "Go back to Two",
                "Stop recording",
                "Manage Recordings…",  # 2.9.7: the Library's Recordings page
            ],
        )
        actions = [a for a in menu.actions() if not a.isSeparator()]
        actions[0].trigger()
        actions[3].trigger()
        self.assertEqual((self.back, self.stopped), (["main"], ["b1"]))

    def test_on_the_player_it_sits_under_the_clock_clear_of_the_channel_name(self):
        # 2.8.12: at the top left it covered the controls' logo and channel name.
        self.host.show()  # a hidden host's resizes wait until it shows
        self.sign.set_recordings([("main", "One", 100.0)])
        geometry = self.sign.geometry()
        un = Units(1280)
        self.assertEqual(geometry.right() + 1, 1280 - un.px(3.0))  # as the clock's right edge
        self.assertGreaterEqual(geometry.top(), un.px(5.7))  # under the title row's logo
        self.host.resize(1600, 900)
        self.assertEqual(self.sign.geometry().right() + 1, 1600 - Units(1600).px(3.0))

    def test_the_librarys_stays_in_its_bottom_left_corner(self):
        sign = RecSign(self.host, clock=lambda: self.now[0], bottom=True)
        sign.set_recordings([("main", "One", 100.0)])
        un = Units(1280)
        self.assertEqual(sign.geometry().left(), un.px(2.4))
        self.assertEqual(sign.geometry().bottom() + 1, 720 - un.px(2.4))

    def test_it_hides_while_the_drawer_the_guide_or_the_grid_covers_the_player(self):
        self.host.show()
        drawer, guide = QWidget(self.host), QWidget(self.host)
        drawer.hide()
        guide.hide()
        self.sign.cover_with(drawer, guide)
        self.sign.set_recordings([("main", "One", 100.0)])
        self.assertTrue(self.sign.isVisible())
        drawer.show()
        self.assertTrue(self.sign.isHidden())
        guide.show()
        drawer.hide()
        self.assertTrue(self.sign.isHidden())  # the guide still covers it
        guide.hide()
        self.assertTrue(self.sign.isVisible())
        drawer.show()
        self.sign.set_recordings([("main", "One", 100.0), ("b1", "Two", 120.0)])
        self.assertTrue(self.sign.isHidden())  # a new recording under the drawer: still hidden
        self.sign.set_recordings([])
        drawer.hide()
        self.assertTrue(self.sign.isHidden())  # nothing records any more

    def test_a_click_opens_the_menu(self):
        self.sign.set_recordings([("main", "One", 100.0)])
        opened = []
        self.sign.open_menu = lambda: opened.append(True)
        QTest.mouseClick(self.sign, Qt.MouseButton.LeftButton, pos=self.sign.rect().center())
        self.assertEqual(opened, [True])


class RecSignHudTests(unittest.TestCase):  # 2.9.7: on the player, it comes and goes with the HUD
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.hud = QWidget(self.host)
        self.hud.hide()
        self.host.show()  # a hidden window's children get no show event
        self.sign = RecSign(self.host, clock=lambda: 163.0)
        self.sign._brief.setInterval(30)
        self.sign.follow_hud(self.hud)
        self.managed = []
        self.sign.manageRequested.connect(lambda: self.managed.append(True))

    def tearDown(self):
        self.host.deleteLater()

    def shown(self):
        return self.sign.isVisibleTo(self.host)

    def test_a_new_recording_shows_it_a_moment_then_it_goes(self):
        self.sign.set_recordings([("main", "One", 100.0), ("b1", "Two", 120.0)])
        self.assertTrue(self.shown())
        self.assertTrue(wait_until(lambda: not self.shown(), 2000))
        self.sign.set_recordings([("main", "One", 100.0), ("b1", "Two", 120.0)])
        self.assertFalse(self.shown())  # the same recordings: nothing new to say
        self.sign.set_recordings([("main", "One", 100.0)])  # one ended
        self.assertTrue(self.shown())

    def test_it_shows_while_the_controls_show(self):
        self.sign.set_recordings([("main", "One", 100.0)])
        self.assertTrue(wait_until(lambda: not self.shown(), 2000))
        self.hud.show()
        self.assertTrue(self.shown())
        QTest.qWait(80)
        self.assertTrue(self.shown())  # the controls up: it stays
        self.hud.hide()
        self.assertFalse(self.shown())

    def test_its_menu_ends_with_the_recordings_page(self):
        self.sign.set_recordings([])
        actions = [a for a in self.sign.menu().actions() if not a.isSeparator()]
        self.assertEqual([a.text() for a in actions], ["Manage Recordings…"])
        actions[0].trigger()
        self.assertEqual(self.managed, [True])


if __name__ == "__main__":
    unittest.main()
