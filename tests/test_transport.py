import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QMouseEvent, QPixmap
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse, timeline, wait_until

from ui.overlays.painting import Units, badge_color
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

    def test_mini_player_and_second_channel_buttons_sit_left_of_fullscreen(self):
        # Owner (Phase 4): clickable controls for both pictures-in-picture.
        t = self.make()
        t.show_live("One", "")
        self.assertNotIn("pip", t._layout())  # no PiP until the window says it can
        t.set_pip_state(True, False)
        mini, pip, full = (t.button_rect(n) for n in ("mini", "pip", "fullscreen"))
        self.assertLess(mini.right(), full.left())
        self.assertLess(pip.right(), mini.left())
        self.assertLess(t.button_rect("subtitles").right(), pip.left())
        for name, signal in (("mini", t.miniPlayerRequested), ("pip", t.pipRequested)):
            spy = QSignalSpy(signal)
            self.click(t, t.button_rect(name).center().toPoint())
            self.assertEqual(spy.count(), 1, name)
        t.show_timeshift("One", "")
        t.update_timeline(
            {
                "start": 0.0,
                "end": 60.0,
                "position": 30.0,
                "live_edge": 60.0,
                "lag": 30.0,
                "mode": "shifted",
                "rate": 1.0,
                "paused": False,
            },
            [],
        )
        self.assertIn("seek", t._layout())
        self.assertLess(t.button_rect("pip").right(), t.button_rect("mini").left())

    def test_the_second_channel_is_live_tv_only(self):
        t = self.make()
        t.set_pip_state(True, False)
        t.show_vod("Film", "")
        t.update_position(0, 100_000)
        self.assertNotIn("pip", t._layout())  # hidden during a movie
        self.assertIn("mini", t._layout())  # the mini player still works

    def test_the_new_buttons_name_their_keys(self):
        t = self.make()
        t.show_live("One", "")
        t.set_pip_state(True, True)
        for name, text in (
            ("pip", "Second channel (P)"),
            ("mini", "Mini player (Alt+P)"),
            ("fullscreen", "Fullscreen (F)"),
        ):
            self.assertEqual(t.tooltip_at(t.button_rect(name).center()), text)
        self.assertEqual(t.tooltip_at(QPointF(640, 300)), "")

    def test_over_the_pip_only_its_controls_keep_the_clicks(self):
        t = self.make()
        t.show_live("One", "")
        t.set_pip_state(True, False)
        claims = t.hole_claims()
        for name in ("play", "fullscreen", "mini", "pip", "audio", "subtitles"):
            self.assertTrue(claims.contains(t.button_rect(name).center().toPoint()), name)
        self.assertFalse(claims.contains(QPoint(640, 300)))  # the scrim
        self.assertFalse(claims.contains(t.button_rect("live").center().toPoint()))  # a label

    def test_the_host_hears_when_its_controls_change(self):
        class Host(QWidget):
            changes = 0

            def overlay_changed(self):
                self.changes += 1

        host = Host()
        host.resize(1280, 720)
        t = Transport(host)
        t.show_live("One", "")
        before = host.changes
        t.update_timeline(
            {
                "start": 0.0,
                "end": 60.0,
                "position": 30.0,
                "live_edge": 60.0,
                "lag": 30.0,
                "mode": "shifted",
                "rate": 1.0,
                "paused": False,
            },
            [],
        )  # still the plain live row: nothing changed
        self.assertEqual(host.changes, before)
        t.show_timeshift("One", "")
        t.update_timeline(
            {
                "start": 0.0,
                "end": 60.0,
                "position": 30.0,
                "live_edge": 60.0,
                "lag": 30.0,
                "mode": "shifted",
                "rate": 1.0,
                "paused": False,
            },
            [],
        )  # the bar and Go live appear
        self.assertGreater(host.changes, before)
        changes = host.changes
        t.update_timeline(
            {
                "start": 0.0,
                "end": 61.0,
                "position": 31.0,
                "live_edge": 61.0,
                "lag": 30.0,
                "mode": "shifted",
                "rate": 1.0,
                "paused": False,
            },
            [],
        )  # a tick: the same controls
        self.assertEqual(host.changes, changes)

    def test_its_controls_top_is_the_top_of_its_band(self):
        # The PiP rises above it and the mixer sits on it (review I2, I3).
        t = self.make()
        t.show_live("One", "")
        top = t.geometry().top()  # host coordinates: a test window sits where the WM put it
        self.assertEqual(t.controls_top(), top + round(t.button_rect("play").top()))
        changes = QSignalSpy(t.bandChanged)
        t.show_timeshift("One", "")
        t.update_timeline(
            {
                "start": 0.0,
                "end": 60.0,
                "position": 30.0,
                "live_edge": 60.0,
                "lag": 30.0,
                "mode": "shifted",
                "rate": 1.0,
                "paused": False,
            },
            [],
        )
        self.assertGreaterEqual(changes.count(), 1)
        seek = t.seek_rect()
        self.assertLess(t.controls_top(), top + seek.top() - Units(1280).px(2))  # the bubble
        self.assertGreater(t.controls_top(), top + 720 * 0.6)  # still the picture's bottom

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
        elapse(300)
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
        elapse(250)
        self.assertTrue(t.isVisible())
        QTest.mouseRelease(t, Qt.LeftButton, Qt.NoModifier, QPoint(int(r.center().x()), y))
        self.assertEqual(spy.count(), 1)
        self.assertTrue(t.isVisible())
        elapse(250)
        self.assertFalse(t.isVisible())

    def test_mouse_move_keeps_alive(self):
        t = self.make(idle_ms=200)
        t.show_vod("A", "b")

        # A move every 60 ms restarts the 200 ms idle: at 360 ms it is still shown. The moves
        # are sent, not queued (QTest.mouseMove), so each lands before a later-due timer.
        def move(i):
            point = QPointF(300 + i * 7, 300)
            event = QMouseEvent(
                QEvent.Type.MouseMove,
                point,
                QPointF(t.mapToGlobal(point.toPoint())),
                Qt.NoButton,
                Qt.NoButton,
                Qt.NoModifier,
            )
            QApplication.sendEvent(t, event)

        moves = [(60 * i, lambda i=i: move(i)) for i in range(1, 6)]
        *_, shown = timeline(*moves, (360, t.isVisible))
        self.assertTrue(shown)

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


class _Host(QWidget):
    """A host that counts the overlay changes it is told about (as OverlayHost recomputes)."""

    def __init__(self):
        super().__init__()
        self.changes = 0
        self.resize(1280, 720)

    def overlay_changed(self):
        self.changes += 1


class _PaintCounter(QObject):
    def __init__(self, widget):
        super().__init__(widget)
        self.paints = 0
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Paint:
            self.paints += 1
        return False


class LostDragAndRepaintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, idle_ms=60):
        host = _Host()
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        transport = Transport(host, idle_ms=idle_ms)
        host.show()
        transport.show_vod("A", "b")
        transport.update_position(0, 100_000)
        transport.grab()
        return host, transport

    def press_on_the_bar(self, transport):
        r = transport.seek_rect()
        QTest.mousePress(
            transport, Qt.LeftButton, Qt.NoModifier, QPoint(int(r.x() + 5), int(r.center().y()))
        )

    def test_a_hide_during_a_drag_ends_it(self):
        # The host hides with a minimized window: the release never comes.
        host, transport = self.make()
        seeks = QSignalSpy(transport.seekRequested)
        self.press_on_the_bar(transport)
        host.hide()
        host.show()
        transport.poke()
        self.assertTrue(wait_until(lambda: not transport.isVisible()))
        self.assertEqual(seeks.count(), 0)
        QTest.mouseRelease(transport, Qt.LeftButton, Qt.NoModifier, QPoint(10, 10))

    def test_a_move_without_the_button_ends_a_drag_whose_release_was_lost(self):
        host, transport = self.make()
        seeks = QSignalSpy(transport.seekRequested)
        self.press_on_the_bar(transport)
        moved = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(400, 650),
            QPointF(transport.mapToGlobal(QPoint(400, 650))),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(transport, moved)
        self.assertTrue(wait_until(lambda: not transport.isVisible()))
        self.assertEqual(seeks.count(), 0)
        QTest.mouseRelease(transport, Qt.LeftButton, Qt.NoModifier, QPoint(10, 10))

    def test_mouse_moves_over_a_visible_transport_neither_repaint_nor_reshow_it(self):
        host, transport = self.make(idle_ms=3000)
        elapse(50)
        counter = _PaintCounter(transport)
        changes = host.changes
        for i in range(5):
            QTest.mouseMove(transport, QPoint(300 + i * 7, 300))
            transport.poke()  # what the controller does for moves over the video
        elapse(50)
        self.assertEqual(counter.paints, 0)
        self.assertEqual(host.changes, changes)  # no mask recomputation per move
        self.assertTrue(transport.isVisible())

    def test_a_poke_that_shows_it_paints_it(self):
        host, transport = self.make(idle_ms=3000)
        transport.dismiss()
        counter = _PaintCounter(transport)
        transport.poke()
        self.assertTrue(wait_until(lambda: counter.paints > 0))


class MiniPlayerLayoutTests(unittest.TestCase):
    """At a mini player's 360 px the font floor (11 px) is taller than the unit-sized rows."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_time_labels_clear_the_seek_line_and_the_buttons(self):
        t = Transport(idle_ms=3000)
        self.addCleanup(t.close)
        t.setGeometry(0, 0, 360, 202)
        t.show_vod("Film", "")
        t.update_position(60_000, 600_000)
        rects = t._layout()
        text_h = QFontMetricsF(t._times_font(Units(360))).height()
        times, seek, play = rects["times"], rects["seek"], rects["play"]
        self.assertGreaterEqual(times.height(), text_h)
        self.assertGreaterEqual(times.top(), seek.bottom())  # below the seek line
        self.assertLessEqual(times.bottom(), play.top())  # above the buttons
        self.assertFalse(t.grab().isNull())

    def test_a_large_transport_keeps_its_unit_layout(self):
        t = Transport(idle_ms=3000)
        self.addCleanup(t.close)
        t.setGeometry(0, 0, 1280, 720)
        t.show_vod("Film", "")
        t.update_position(60_000, 600_000)
        times = t._layout()["times"]
        self.assertEqual(round(times.height()), Units(1280).px(1.6))


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


class TransportBadgeCase(TransportCase):
    def logo(self, color="#ff0000"):
        pixmap = QPixmap(40, 40)
        pixmap.fill(QColor(color))
        return pixmap

    def centre(self, t):
        image = t.grab().toImage()
        r = t.badge_rect()
        return image.pixelColor(int(r.center().x()), int(r.center().y()))

    def test_a_logo_is_painted_and_the_name_starts_after_it(self):
        t = self.make()
        t.show_live("Nature One", "", self.logo())
        r = t.badge_rect()
        self.assertFalse(r.isEmpty())
        self.assertGreater(self.centre(t).red(), 200)
        self.assertLess(self.centre(t).green(), 60)
        un = Units(t.width())
        self.assertAlmostEqual(
            r.height(), QFontMetricsF(un.font(1.8, QFont.Weight.Bold)).height() * 1.9, 1
        )
        self.assertGreater(r.right(), un.px(3))
        # the name is painted right of the badge, none of it under it
        image = t.grab().toImage()
        gap = range(int(r.right()) + 1, int(r.right() + un.px(1.0)))
        name = range(int(r.right() + un.px(1.4)), int(r.right() + un.px(10)))
        rows = range(int(r.y()), int(r.bottom()))

        def bright(xs):
            return any(image.pixelColor(x, y).lightness() > 200 for x in xs for y in rows)

        self.assertFalse(bright(gap))
        self.assertTrue(bright(name))

    def test_no_logo_paints_the_initials_badge(self):
        t = self.make()
        t.show_live("Nature One", "")
        image = t.grab().toImage()
        r = t.badge_rect()
        corner = image.pixelColor(int(r.x() + r.width() * 0.2), int(r.y() + r.height() * 0.8))
        want = badge_color("Nature One")
        self.assertEqual(
            (corner.red(), corner.green(), corner.blue()), (want.red(), want.green(), want.blue())
        )

    def test_vod_shows_no_badge(self):
        t = self.make()
        t.show_vod("Salt Roads", "2024")
        self.assertTrue(t.badge_rect().isEmpty())

    def test_a_logo_arriving_later_repaints(self):
        t = self.make()
        t.show_live("Nature One", "")
        before = t.grab().toImage()
        t.set_logo(self.logo("#00ff00"))
        self.assertNotEqual(before, t.grab().toImage())
        self.assertGreater(self.centre(t).green(), 200)


if __name__ == "__main__":
    unittest.main()
