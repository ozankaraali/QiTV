import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QMouseEvent, QPainter, QPixmap
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse, timeline, wait_until

from ui.keymap import key_hint
from ui.overlays.painting import Units, badge_color, draw_icon
from ui.overlays.transport import SPEEDS, Transport


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

    def test_the_shades_leave_the_small_picture_alone(self):
        # The bottom shade, cut to the PiP label's row by the host's mask, showed as a dark
        # rectangle behind the label (owner's screenshot): it is not painted over the PiP.
        t = self.make()
        t.show_vod("Salt Roads", "2024")
        pip = QRect(880, 400, 360, 200)  # a bottom-right PiP, in the bottom shade's reach
        t.set_pip_rect(pip)
        image = t.grab().toImage()
        inside, beside = QPoint(pip.left() + 40, pip.bottom() - 4), QPoint(
            pip.left() - 40, pip.bottom() - 4
        )
        self.assertEqual(image.pixelColor(inside).alpha(), 0)
        self.assertGreater(image.pixelColor(beside).alpha(), 0)
        t.set_pip_rect(QRect())  # PiP closed: the shade is whole again
        self.assertGreater(t.grab().toImage().pixelColor(inside).alpha(), 0)

    def test_it_follows_the_small_picture_in_its_hosts_coordinates(self):
        t = self.make()
        t.move(10, 20)
        t.follow_pip(QRect(110, 220, 50, 30))
        self.assertEqual(t._pip_rect, QRect(100, 200, 50, 30))

    def test_a_late_report_of_the_small_picture_after_teardown_is_dropped(self):
        # 2.0.2 review (M6): the PiP frame may report once more while the window closes;
        # a slot without a receiver would reach a deleted transport.
        class Frame(QObject):
            smallRectChanged = Signal(QRect)

        host, frame = QWidget(), Frame()
        transport = Transport(host)
        frame.smallRectChanged.connect(transport.follow_pip)
        errors = []
        hook, sys.excepthook = sys.excepthook, lambda *info: errors.append(info[1])
        self.addCleanup(setattr, sys, "excepthook", hook)
        transport.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        frame.smallRectChanged.emit(QRect(1, 2, 3, 4))
        self.assertEqual(errors, [])
        for obj in (frame, host):
            obj.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

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

    def test_the_external_player_button_sits_with_the_picture_buttons(self):
        # Owner (2026-10-15): what plays now goes to VLC or MPV from the player controls.
        t = self.make()
        t.show_live("One", "")
        t.set_pip_state(True, False)
        external = t.button_rect("external")
        self.assertLess(external.right(), t.button_rect("pip").left())
        self.assertLess(t.button_rect("subtitles").right(), external.left())
        self.assertEqual(t.tooltip_at(external.center()), "Open in external player")
        spy = QSignalSpy(t.externalRequested)
        self.click(t, external.center().toPoint())
        self.assertEqual(spy.count(), 1)
        # The shell's menu opens from the button (global coordinates).
        self.assertEqual(spy.at(0)[0], t.mapToGlobal(external.topLeft().toPoint()))

    def test_movies_and_time_shift_have_the_external_player_button_too(self):
        t = self.make()
        t.show_vod("Film", "")
        t.update_position(0, 100_000)
        external = t.button_rect("external")
        self.assertLess(external.right(), t.button_rect("mini").left())
        self.assertLess(t.button_rect("audio").right(), external.left())
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
        self.assertLess(t.button_rect("golive").right(), t.button_rect("external").left())

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

    def test_in_the_mini_player_and_fullscreen_their_buttons_show_the_way_back(self):
        t = self.make()
        t.show_live("One", "")
        self.assertEqual((t.icon("mini"), t.icon("fullscreen")), ("mini_player", "fullscreen"))
        t.set_window_mode(mini=True, fullscreen=False)
        self.assertEqual(t.icon("mini"), "full_window")
        mini = t.button_rect("mini").center()
        self.assertEqual(t.tooltip_at(mini), "Back to the full window (Alt+P)")
        t.set_window_mode(mini=False, fullscreen=True)
        self.assertEqual((t.icon("mini"), t.icon("fullscreen")), ("mini_player", "exit_fullscreen"))
        full = t.button_rect("fullscreen").center()
        self.assertEqual(t.tooltip_at(full), "Exit fullscreen (F)")
        for name in ("full_window", "exit_fullscreen"):  # both are drawn
            image = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            draw_icon(painter, name, QRectF(0, 0, 32, 32), QColor("white"))
            painter.end()
            alphas = (image.pixelColor(x, y).alpha() for x in range(32) for y in range(32))
            self.assertTrue(any(alphas), name)

    def modes(self, t):
        """Live, a movie, then time-shifted live (the bar): each lays its buttons out."""
        t.show_live("One", "")
        yield "live"
        t.show_vod("Film", "")
        t.update_position(0, 100_000)
        yield "movie"
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
        yield "shifted"

    def test_the_volume_button_sits_left_of_the_audio_track_button(self):
        # Owner (2.0.1): ♪ picks the audio track; nothing opened the volume mixer.
        t = self.make()
        t.set_pip_state(True, False)
        for mode in self.modes(t):
            with self.subTest(mode=mode):
                rects = t._layout()
                volume = rects["volume"]
                right = [(r.left(), n) for n, r in rects.items() if r.left() >= volume.right()]
                self.assertEqual(min(right)[1], "audio")  # its neighbour on the right
                self.assertEqual(t.tooltip_at(volume.center()), "Volume (+/−, M)")
                spy = QSignalSpy(t.volumeRequested)
                self.click(t, volume.center().toPoint())
                self.assertEqual(spy.count(), 1)

    def test_the_volume_button_shows_whether_the_picture_is_heard(self):
        t = self.make()
        t.show_live("One", "")
        volume = t.button_rect("volume").toAlignedRect()
        heard = t.grab().toImage().copy(volume)
        t.set_muted(True)
        self.assertNotEqual(t.grab().toImage().copy(volume), heard)
        t.set_muted(False)
        self.assertEqual(t.grab().toImage().copy(volume), heard)

    def test_over_the_pip_only_its_controls_keep_the_clicks(self):
        t = self.make()
        t.show_live("One", "")
        t.set_pip_state(True, False)
        claims = t.hole_claims()
        for name in (
            "play",
            "fullscreen",
            "mini",
            "pip",
            "external",
            "audio",
            "volume",
            "subtitles",
        ):
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
        self.assertLess(t.button_rect("next").right(), t.button_rect("slower").x() + 1)
        t.set_next_available(False)
        self.assertEqual(t._layout(), base)
        self.assertNotIn("next", t._layout())
        t.set_next_available(True)
        t.show_live("104", "Nature")
        self.assertNotIn("next", t._layout())

    def test_speed_steps_both_ways_and_stops_at_the_ends(self):
        t = self.make()
        t.show_vod("A", "b")
        spy = QSignalSpy(t.speedRequested)
        for name, times in (("faster", 4), ("slower", 7), ("speed", 2)):
            for _ in range(times):
                self.click(t, t.button_rect(name).center().toPoint())
        # Faster to 2x and no further, slower to 0.5x and no further, the number back to 1x.
        rates = [spy.at(i)[0] for i in range(spy.count())]
        self.assertEqual(rates, [1.25, 1.5, 2.0, 1.5, 1.25, 1.0, 0.75, 0.5, 1.0])
        lefts = [t.button_rect(n).left() for n in ("slower", "speed", "faster")]
        self.assertEqual(lefts, sorted(lefts))
        self.assertEqual(t.tooltip_at(t.button_rect("faster").center()), "Faster")
        self.assertEqual(t.tooltip_at(t.button_rect("slower").center()), "Slower")

    def test_the_speed_chip_fits_its_text_in_a_small_window(self):
        # "1.25x" was cut off: the chip shrank with the window, its font stops at a floor.
        t = self.make()
        for width in (360, 480, 640, 1280):
            t.setGeometry(0, 0, width, width * 9 // 16)
            t.show_vod("A", "b")
            fm = QFontMetricsF(t._chip_font(Units(width)))
            layout = t._layout()
            chip = layout["slower"].united(layout["faster"])
            for rate in SPEEDS:
                t.set_speed(rate)
                label = f"{rate:g}x"
                with self.subTest(width=width, label=label):
                    self.assertGreaterEqual(layout["speed"].width(), fm.horizontalAdvance(label))
                    self.assertGreaterEqual(layout["speed"].height(), fm.height())
            for name, rect in layout.items():
                if name not in ("seek", "times", "slower", "speed", "faster"):
                    with self.subTest(width=width, clear_of=name):
                        self.assertFalse(chip.intersects(rect))

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


LIVE_EDGE = {
    "start": 0.0,
    "end": 60.0,
    "position": 59.0,
    "live_edge": 60.0,
    "lag": 1.0,
    "mode": "live",
    "rate": 1.0,
    "paused": False,
}
MIN_HIT_PX = 28  # the owner's mouse-only rule: every new control, at the default window


class OnScreenKeysTests(unittest.TestCase):
    """The owner's mouse-only rule (2.0.1): the player's keys have buttons too, each naming
    its key, at least 28 px wide at the default window, and kept over the PiP."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self):
        t = Transport(idle_ms=3000)
        t.setGeometry(0, 0, 1280, 720)
        t.show()
        self.addCleanup(t.close)
        return t

    def click(self, t, point):
        QTest.mouseClick(t, Qt.LeftButton, Qt.NoModifier, point.toPoint())

    def assert_big_enough(self, rect, name):
        self.assertGreaterEqual(rect.width(), MIN_HIT_PX, name)
        self.assertGreaterEqual(rect.height(), MIN_HIT_PX, name)

    def test_live_tv_has_previous_channel_guide_and_search(self):
        t = self.make()
        t.show_live("One", "News at six")
        signals = {
            "previous": (t.previousRequested, key_hint("Previous channel", "Backspace")),
            "guide": (t.guideRequested, key_hint("Guide", "G")),
            "search": (t.searchRequested, key_hint("Search", "Ctrl+F")),
        }
        for name, (signal, tooltip) in signals.items():
            with self.subTest(name=name):
                rect = t.button_rect(name)
                self.assert_big_enough(rect, name)
                self.assertEqual(t.tooltip_at(rect.center()), tooltip)
                spy = QSignalSpy(signal)
                self.click(t, rect.center())
                self.assertEqual(spy.count(), 1)
        # Left to right: ⟲ ▦ ⌕, then the speaker (the channel's, then the sound's buttons).
        lefts = [t.button_rect(n).left() for n in ("previous", "guide", "search", "volume")]
        self.assertEqual(lefts, sorted(lefts))

    def test_a_movie_has_search_but_not_the_channel_buttons(self):
        t = self.make()
        t.show_vod("Film", "")
        t.update_position(0, 100_000)
        rects = t._layout()
        self.assertIn("search", rects)
        self.assertLess(rects["search"].right(), rects["subtitles"].left())
        for name in ("previous", "guide"):
            self.assertNotIn(name, rects)
        self.assertEqual(t.title_rects(), {})  # no channel: nothing to favor or to tell

    def test_a_title_from_the_library_has_a_way_back_to_it(self):
        t = self.make()
        t.set_library_available(True)
        t.show_vod("Film", "")
        t.update_position(0, 100_000)
        rect = t.button_rect("library")
        self.assert_big_enough(rect, "library")
        # Named as this computer names the key: "(⎋)" on macOS, "(Esc)" elsewhere.
        self.assertEqual(t.tooltip_at(rect.center()), key_hint("Back to the Library", "Esc"))
        # At the left of the right-hand group, clear of the speed chip.
        self.assertLess(rect.right(), t.button_rect("search").left())
        self.assertGreater(rect.left(), t.button_rect("faster").right())
        self.assertTrue(t.hole_claims().contains(rect.center().toPoint()))
        spy = QSignalSpy(t.libraryRequested)
        self.click(t, rect.center())
        self.assertEqual(spy.count(), 1)

    def test_the_way_back_to_the_library_shows_only_for_its_titles(self):
        host = _Host()
        self.addCleanup(host.deleteLater)
        t = Transport(host, idle_ms=3000)
        host.show()
        t.show_vod("A local file", "")
        t.update_position(0, 100_000)
        self.assertNotIn("library", t._layout())
        plain = t.grab().toImage()
        changes = host.changes
        t.set_library_available(True)
        self.assertGreater(host.changes, changes)  # the host masks the new button
        rect = t.button_rect("library").toAlignedRect()
        self.assertNotEqual(t.grab().toImage().copy(rect), plain.copy(rect))  # painted
        t.show_live("One", "")
        self.assertNotIn("library", t._layout())
        t.show_timeshift("One", "")
        for state in (LIVE_EDGE, dict(LIVE_EDGE, position=30.0, lag=30.0, mode="shifted")):
            t.update_timeline(state, [])
            self.assertNotIn("library", t._layout())
        t.show_vod("Film", "")
        self.assertIn("library", t._layout())
        t.set_library_available(False)
        self.assertNotIn("library", t._layout())

    def test_time_shifted_live_shows_back_10_seconds_at_the_live_edge(self):
        t = self.make()
        t.show_live("One", "")
        self.assertNotIn("back10", t._layout())  # played directly: nothing to go back in
        t.show_timeshift("One", "")
        t.update_timeline(LIVE_EDGE, [])
        back = t.button_rect("back10")
        self.assertGreater(back.left(), t.button_rect("play").right())
        self.assertLess(back.right(), t.button_rect("indicator").left())
        self.assert_big_enough(back, "back10")
        self.assertEqual(t.tooltip_at(back.center()), key_hint("Back 10 seconds", "Left"))
        spy = QSignalSpy(t.seekRelativeRequested)
        self.click(t, back.center())
        self.assertEqual([spy.at(i)[0] for i in range(spy.count())], [-10_000])

    def test_the_bars_buttons_name_their_keys(self):
        t = self.make()
        t.show_vod("Film", "")
        t.update_position(0, 100_000)
        for name, tooltip in (
            ("play", key_hint("Play / pause", "Space")),
            ("back10", key_hint("Back 10 seconds", "Left")),
            ("fwd10", key_hint("Forward 10 seconds", "Right")),
        ):
            self.assertEqual(t.tooltip_at(t.button_rect(name).center()), tooltip, name)
        t.show_timeshift("One", "")
        t.update_timeline(dict(LIVE_EDGE, position=30.0, lag=30.0, mode="shifted"), [])
        golive = t.button_rect("golive")
        self.assertEqual(t.tooltip_at(golive.center()), key_hint("Go live", "L"))

    def double_click(self, t, point):
        """As a mouse does it: press, release, then a press that is a double-click."""
        QTest.mouseDClick(t.windowHandle(), Qt.LeftButton, Qt.NoModifier, point.toPoint())

    def test_a_quick_second_click_counts_once_on_toggles_and_jumps(self):
        # 2.0.1 review M1: ★ would toggle twice and ⟲ zap back again, ⛶ go in and out.
        t = self.make()
        t.set_pip_state(True, False)
        t.show_live("One", "")
        for name, signal, rect in (
            ("favorite", t.favoriteRequested, t.title_rects()["favorite"]),
            ("previous", t.previousRequested, t.button_rect("previous")),
            ("fullscreen", t.fullscreenRequested, t.button_rect("fullscreen")),
            ("pip", t.pipRequested, t.button_rect("pip")),
            ("audio", t.audioRequested, t.button_rect("audio")),  # 2.2: they open panels
            ("subtitles", t.subtitleRequested, t.button_rect("subtitles")),
        ):
            with self.subTest(name=name):
                spy = QSignalSpy(signal)
                self.double_click(t, rect.center())
                self.assertEqual(spy.count(), 1)

    def test_a_quick_second_click_repeats_a_step(self):
        t = self.make()
        t.show_vod("Film", "")
        t.update_position(0, 100_000)
        spy = QSignalSpy(t.seekRelativeRequested)
        self.double_click(t, t.button_rect("back10").center())
        self.assertEqual([spy.at(i)[0] for i in range(spy.count())], [-10_000, -10_000])

    def test_the_sound_buttons_say_what_they_do(self):
        # 2.0.1 review M3: ♪ and the subtitles had no tooltip; 2.2: they name their sync keys.
        t = self.make()
        t.show_vod("Film", "")
        t.update_position(0, 100_000)
        for name, tooltip in (
            ("audio", "Sound: track and sync (J, K)"),
            ("subtitles", key_hint("Subtitles: track, file and sync", "Shift+J, Shift+K")),
        ):
            self.assertEqual(t.tooltip_at(t.button_rect(name).center()), tooltip, name)

    def test_the_channels_name_shows_its_info(self):
        t = self.make()
        t.show_live("Nature One", "Wild rivers")
        title = t.title_rects()["title"]
        self.assertTrue(title.contains(t.badge_rect()))  # the logo is part of it
        self.assert_big_enough(title, "title")
        self.assertEqual(t.tooltip_at(title.center()), key_hint("Channel info", "I"))
        spy = QSignalSpy(t.infoRequested)
        self.click(t, title.center())
        self.assertEqual(spy.count(), 1)

    def test_a_star_after_the_name_adds_or_removes_the_favorite(self):
        t = self.make()
        t.show_live("Nature One", "Wild rivers")
        rects = t.title_rects()
        star = rects["favorite"]
        self.assert_big_enough(star, "favorite")
        self.assertGreater(star.left(), rects["title"].right())
        self.assertLess(star.bottom(), t.controls_top())  # the title row, not the controls
        self.assertEqual(t.tooltip_at(star.center()), key_hint("Add to favorites", "Ctrl+D"))
        spy = QSignalSpy(t.favoriteRequested)
        self.click(t, star.center())
        self.assertEqual(spy.count(), 1)
        plain = t.grab().toImage().copy(star.toAlignedRect())
        t.set_favorite(True)
        self.assertEqual(t.tooltip_at(star.center()), key_hint("Remove from favorites", "Ctrl+D"))
        self.assertNotEqual(t.grab().toImage().copy(star.toAlignedRect()), plain)  # lit
        t.set_favorite(False)
        self.assertEqual(t.grab().toImage().copy(star.toAlignedRect()), plain)

    def test_the_subtitle_starts_after_the_star(self):
        t = self.make()
        t.show_live("Nature One", "Wild rivers")
        star = t.title_rects()["favorite"]
        image = t.grab().toImage()
        rows = range(int(star.top()), int(star.bottom()))
        after = range(int(star.right()) + 2, int(star.right()) + Units(1280).px(8))
        self.assertTrue(any(image.pixelColor(x, y).lightness() > 150 for x in after for y in rows))

    def test_over_the_pip_the_new_controls_keep_their_clicks(self):
        t = self.make()
        t.show_live("One", "")
        claims = t.hole_claims()
        for name in ("previous", "guide", "search"):
            self.assertTrue(claims.contains(t.button_rect(name).center().toPoint()), name)
        for name, rect in t.title_rects().items():
            self.assertTrue(claims.contains(rect.center().toPoint()), name)
        self.assertFalse(claims.contains(QPoint(1100, 40)))  # the clock is a label

    def test_the_host_hears_when_the_star_moves(self):
        class Host(QWidget):
            changes = 0

            def overlay_changed(self):
                self.changes += 1

        host = Host()
        host.resize(1280, 720)
        t = Transport(host)
        t.show_live("One", "")
        t.show_live("One", "")
        before = host.changes
        t.show_live("A channel with a much longer name", "")  # the star moves right
        self.assertGreater(host.changes, before)

    def test_the_channel_buttons_keep_the_controls_top(self):
        # The PiP rises above the bottom row only: the title row is not part of it.
        t = self.make()
        t.show_live("One", "")
        top = t.geometry().top()
        self.assertEqual(t.controls_top(), top + round(t.button_rect("play").top()))


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


class SmallWidthLayoutTests(unittest.TestCase):
    """The controls never overlap, down to a mini player's width (the external button made
    the right side longer; the Stay live indicator gives way)."""

    WIDTHS = (300, 360, 480, 640, 960, 1280)

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def state(mode, lag, rate):
        return {
            "start": 0.0,
            "end": 600.0,
            "position": 600.0 - lag,
            "live_edge": 600.0,
            "lag": lag,
            "mode": mode,
            "rate": rate,
            "paused": False,
        }

    def modes(self, t):
        t.show_vod("Film", "")
        t.update_position(0, 100_000)
        t.set_next_available(True)
        yield "movie with Next"
        t.set_library_available(True)
        yield "movie from the Library, with Next"
        t.set_next_available(False)
        t.show_live("One", "")
        yield "live"
        t.show_timeshift("One", "")
        t.update_timeline(self.state("live", 12.5, 1.05), [])
        yield "stay live, catching up"
        auto = dict(self.state("live", 12.5, 1.05), target=10.0, auto=True, adjusted=True)
        t.update_timeline(auto, [])
        yield "stay live, catching up, Auto adjusted"
        slower = dict(self.state("live", 12.5, 0.95), target=10.0, auto=True, adjusted=True)
        t.update_timeline(dict(slower, nudge=0.95), [])
        yield "stay live, adding buffer, Auto adjusted"
        t.update_timeline(self.state("shifted", 300.0, 1.0), [])
        yield "shifted, Go live"

    def test_no_two_controls_overlap_at_any_width(self):
        for width in self.WIDTHS:
            t = Transport(idle_ms=3000)
            self.addCleanup(t.close)
            t.setGeometry(0, 0, width, round(width * 9 / 16))
            t.set_pip_state(True, False)
            for mode in self.modes(t):
                with self.subTest(width=width, mode=mode):
                    rects = {
                        name: rect
                        for name, rect in t._layout().items()
                        if name not in ("seek", "times")  # the rows above the buttons
                    }
                    self.assertIn("volume", rects)
                    self.assertIn("search", rects)
                    movie = mode.startswith("movie")
                    if not movie:
                        self.assertIn("guide", rects)
                        self.assertIn("previous", rects)
                    # ‹ Library: only for a title from the Library (live TV ignores it).
                    library = mode == "movie from the Library, with Next"
                    self.assertEqual("library" in rects, library)
                    if mode.startswith("stay live, "):
                        self.assertIn("back10", rects)
                    names = list(rects)
                    for i, a in enumerate(names):
                        self.assertGreaterEqual(rects[a].left(), 0, a)
                        self.assertLessEqual(rects[a].right(), width, a)
                        for b in names[i + 1 :]:
                            self.assertFalse(rects[a].intersects(rects[b]), f"{a} overlaps {b}")
                    self.assertFalse(t.grab().isNull())

    def test_without_room_for_minus_and_plus_the_speed_shows_alone(self):
        # Shifted, with Go live, in narrow windows: − 1.25x +, else the number alone (readable,
        # it goes back to 1x), else nothing. Where each starts depends on the font's widths.
        t = Transport(idle_ms=3000)
        self.addCleanup(t.close)
        t.set_pip_state(True, False)
        t.show_timeshift("One", "")
        states = {}
        for width in range(300, 642, 2):
            t.setGeometry(0, 0, width, round(width * 9 / 16))
            t.update_timeline(self.state("shifted", 300.0, 1.5), [])
            rects = t._layout()
            shown = tuple(n for n in ("slower", "speed", "faster") if n in rects)
            states[width] = shown
            with self.subTest(width=width, shown=shown):
                self.assertIn(shown, (("slower", "speed", "faster"), ("speed",), ()))
                fm = QFontMetricsF(t._chip_font(Units(width)))
                self.assertGreaterEqual(rects["golive"].height(), fm.height())
                if shown != ("speed",):
                    continue
                self.assertGreaterEqual(rects["speed"].width(), fm.horizontalAdvance("1.25x"))
                self.assertGreaterEqual(rects["speed"].height(), fm.height())
                self.assertFalse(rects["speed"].intersects(rects["golive"]))
                spy = QSignalSpy(t.speedRequested)
                QTest.mouseClick(t, Qt.LeftButton, Qt.NoModifier, rects["speed"].center().toPoint())
                self.assertEqual([spy.at(i)[0] for i in range(spy.count())], [1.0])
                t.set_speed(1.5)
        self.assertEqual(states[640], ("slower", "speed", "faster"))

    def test_the_title_rows_controls_stay_inside_at_any_width(self):
        for width in self.WIDTHS:
            t = Transport(idle_ms=3000)
            self.addCleanup(t.close)
            t.setGeometry(0, 0, width, round(width * 9 / 16))
            t.show_live("A channel with a rather long name indeed", "Its programme")
            with self.subTest(width=width):
                rects = t.title_rects()
                self.assertFalse(rects["title"].intersects(rects["favorite"]))
                for name, rect in rects.items():
                    self.assertGreaterEqual(rect.left(), 0, name)
                    self.assertLessEqual(rect.right(), width, name)

    def test_a_wide_transport_shows_the_whole_indicator(self):
        t = Transport(idle_ms=3000)
        self.addCleanup(t.close)
        t.setGeometry(0, 0, 1280, 720)
        t.set_pip_state(True, False)
        t.show_timeshift("One", "")
        t.update_timeline(self.state("live", 12.5, 1.05), [])
        head, tail = t._indicator_parts()
        fm = QFontMetricsF(t._indicator_font(Units(1280)))
        box = t._layout()["indicator"]
        self.assertGreater(box.width(), fm.horizontalAdvance(head) + fm.horizontalAdvance(tail))


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
