"""The desktop picture in picture's player controls (ui/float_controls.py, 2.13.5): a mirror of
the window's controls over the floating window, its own hover, its clicks."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402
from timer_fixtures import elapse  # noqa: E402

from tests.test_desktop_pip import mouse  # noqa: E402
from tests.test_transport_timeshift import PROGRAMME, timeline  # noqa: E402
from ui import float_controls  # noqa: E402
from ui.float_controls import FloatControls, FloatHost  # noqa: E402
from ui.overlays.transport import OWN_SETTERS, Transport  # noqa: E402

_PREFIXES = ("set_", "show_", "update_", "end_")


class MirrorTests(unittest.TestCase):
    """Transport.add_mirror: what the controls show is told; whether they show is not."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, idle_ms=3000):
        t = Transport(idle_ms=idle_ms)
        t.setGeometry(0, 0, 1280, 720)
        self.addCleanup(t.close)
        return t

    def same(self, primary, mirror):
        """Both show the same: their texts, their buttons and what each button paints."""
        self.assertEqual(mirror.texts(), primary.texts())
        layout = primary._layout()
        self.assertEqual(set(mirror._layout()), set(layout))
        for name in layout:
            self.assertEqual(mirror.icon(name), primary.icon(name), name)
        self.assertEqual(mirror.title_rects().keys(), primary.title_rects().keys())
        self.assertEqual(mirror.recording(), primary.recording())
        self.assertEqual(mirror.failed(), primary.failed())

    def test_every_state_setter_is_told_or_the_windows_own(self):
        # A setter added later and left out would leave the floating window's controls behind.
        for name, value in vars(Transport).items():
            if name.startswith(_PREFIXES) and callable(value):
                told = getattr(value, "mirrored", False)
                self.assertTrue(told or name in OWN_SETTERS, f"{name}: mirrored or OWN_SETTERS?")
                self.assertFalse(told and name in OWN_SETTERS, name)

    def test_a_mirror_added_later_catches_up_then_follows(self):
        primary = self.make()
        primary.show_timeshift("104 · Nature", "Wild Coasts", QPixmap(8, 8))
        primary.update_timeline(timeline(), PROGRAMME)
        primary.set_favorite(True)
        primary.set_recording(12.0)
        primary.set_pip_state(True, False)
        primary.set_muted(True)
        primary.set_quality_available(True)
        mirror = self.make()
        primary.add_mirror(mirror)
        self.same(primary, mirror)
        self.assertTrue(mirror._favorite)
        primary.update_timeline(timeline(position=3400.0, lag=80.0), PROGRAMME)
        self.same(primary, mirror)
        primary.show_vod("Salt Roads", "2024")  # another mode
        primary.update_position(60_000, 5_400_000)
        primary.set_paused(True)
        self.same(primary, mirror)
        self.assertEqual(mirror.icon("play"), primary.icon("play"))
        primary.show_live("1 · News", "", None)  # live, then a channel that failed to open
        self.same(primary, mirror)
        primary.show_failed("1 · News", "", None)
        self.same(primary, mirror)
        primary.show_audio("Song", "Album")  # the music player's dock
        primary.set_audio_state(True, "one", True)
        self.same(primary, mirror)
        primary.end_audio()
        self.assertFalse(mirror.audio())

    def test_whether_it_shows_and_the_windows_own_are_not_told(self):
        primary, mirror = self.make(), self.make()
        primary.add_mirror(mirror)
        primary.show_vod("Salt Roads", "2024")
        mirror.dismiss()
        primary.poke()
        primary.hold("audio")
        self.assertTrue(mirror.isHidden())
        self.assertEqual(mirror.held(), "")
        primary.set_window_mode(mini=False, fullscreen=True)
        mirror.set_window_mode(mini=True, fullscreen=False)
        self.assertEqual(primary.icon("fullscreen"), "exit_fullscreen")
        self.assertEqual(mirror.icon("mini"), "full_window")  # the mini player's way back
        primary.set_pip_rect(QRect(800, 400, 300, 200))
        self.assertTrue(mirror._pip_rect.isEmpty())
        primary.dismiss()
        mirror.poke()
        self.assertTrue(primary.isHidden())  # nor the other way

    def test_a_hover_shows_it_and_resting_on_its_controls_keeps_it_up(self):
        t = self.make(idle_ms=60)
        t.show_vod("Salt Roads", "2024")
        t.dismiss()
        t.hover_at(QPointF(640, 360))  # over the picture
        self.assertFalse(t.isHidden())
        elapse(150)
        self.assertTrue(t.isHidden())  # idle: it fades, as over the window
        t.hover_at(QPointF(640, 700))  # onto the controls, and resting there
        elapse(150)
        self.assertFalse(t.isHidden())
        t.hover_at(None)  # off it
        elapse(150)
        self.assertTrue(t.isHidden())
        t.hover_at(QPointF(640, 701))  # a tremble is no move
        self.assertTrue(t.isHidden())


class FloatControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.floating = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.floating.setGeometry(QRect(100, 100, 640, 360))
        self.floating.show()
        self.addCleanup(self.floating.deleteLater)
        self.primary = Transport(idle_ms=3000)
        self.primary.setGeometry(0, 0, 1280, 720)
        self.addCleanup(self.primary.close)
        self.primary.show_vod("Salt Roads", "2024")
        self.primary.dismiss()
        self.joined = []
        self.come_back = mock.Mock()
        self.mute = mock.Mock()
        self.picture = []
        self.controls = FloatControls(
            self.floating,
            self.primary,
            come_back=self.come_back,
            mute=self.mute,
            picture_mouse=lambda event: self.picture.append(event.type()),
            join_all_spaces=self.joined.append,
        )
        self.mirror = self.controls.mirror
        self.addCleanup(self.controls.deleteLater)

    def show_controls(self):
        self.controls.hover_at(self.floating.geometry().center())
        self.app.processEvents()
        self.assertTrue(self.mirror.isVisible())

    def test_it_starts_hidden_in_the_mini_players_look_over_the_floating_window(self):
        host = self.controls.host
        self.assertIsInstance(host, FloatHost)
        self.assertIs(host.followed(), self.floating)
        self.assertTrue(self.mirror.isHidden())  # it shows on a hover
        self.assertFalse(host.isVisible())
        self.assertEqual(self.mirror.icon("mini"), "full_window")
        self.assertEqual(self.mirror.texts(), self.primary.texts())
        self.assertEqual(self.joined, [host])  # on every desktop, a panel that takes no focus
        self.show_controls()
        self.assertTrue(host.isVisible())
        self.assertGreater(len(self.joined), 1)  # and again as it shows
        self.assertEqual(set(self.joined), {host})
        self.assertEqual(host.geometry(), self.floating.geometry())
        self.assertTrue(self.primary.isHidden())  # the window's own stay as they are

    def test_what_acts_on_the_player_acts_in_place(self):
        self.show_controls()
        spy = QSignalSpy(self.primary.playPauseRequested)
        QTest.mouseClick(
            self.mirror,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            self.mirror.button_rect("play").center().toPoint(),
        )
        self.assertEqual(spy.count(), 1)
        steps = QSignalSpy(self.primary.seekRelativeRequested)
        self.mirror.seekRelativeRequested.emit(10_000)
        self.assertEqual(steps.count(), 1)
        self.assertEqual(steps.at(0), [10_000])
        self.mirror.volumeRequested.emit()  # the speaker: mute, here
        self.mute.assert_called_once()
        self.app.processEvents()
        self.come_back.assert_not_called()

    def test_its_way_back_only_comes_back(self):
        fullscreen = QSignalSpy(self.primary.fullscreenRequested)
        mini = QSignalSpy(self.primary.miniPlayerRequested)
        self.mirror.fullscreenRequested.emit()
        self.mirror.miniPlayerRequested.emit()
        self.app.processEvents()
        self.assertEqual(self.come_back.call_count, 2)
        self.assertEqual((fullscreen.count(), mini.count()), (0, 0))  # never out of fullscreen

    def test_what_opens_in_the_window_comes_back_first_and_opens_there(self):
        order = []
        self.come_back.side_effect = lambda: order.append("back")
        self.primary.audioRequested.connect(lambda: order.append("sound"))
        cast = QSignalSpy(self.primary.castRequested)
        self.mirror.audioRequested.emit()
        self.assertEqual(order, [])  # once its click is through
        self.app.processEvents()
        self.assertEqual(order, ["back", "sound"])
        self.assertFalse(self.primary.isHidden())  # up, as after a click on them
        self.primary.set_cast_state(True, False)
        self.mirror.castRequested.emit(QPoint(5, 5))
        self.app.processEvents()
        self.assertEqual(cast.count(), 1)
        rect = self.primary.button_rect("cast")  # from the window's own button
        self.assertEqual(cast.at(0), [self.primary.mapToGlobal(rect.topLeft().toPoint())])

    def test_every_request_has_its_way(self):
        names = float_controls.requests()
        self.assertIn("playPauseRequested", names)
        self.assertNotIn("bandChanged", names)
        known = {*float_controls.IN_PLACE, *float_controls.WAY_BACK, float_controls.MUTE}
        self.assertLessEqual(known, set(names))  # (no name mistyped: each is wired as said)
        self.assertLessEqual({"audioRequested", "searchRequested"}, set(names) - known)

    def test_a_press_on_the_shades_is_the_pictures_one_on_a_control_its_own(self):
        self.show_controls()
        beside = QPoint(self.mirror.width() // 2, 30)  # the top shade (a film's: no control)
        self.assertIsNone(self.mirror.control_at(QPointF(beside)))
        mouse(self.mirror, QEvent.Type.MouseButtonPress, beside)
        mouse(self.mirror, QEvent.Type.MouseMove, beside + QPoint(30, 0))
        mouse(self.mirror, QEvent.Type.MouseButtonRelease, beside, Qt.MouseButton.NoButton)
        mouse(self.mirror, QEvent.Type.MouseMove, beside, Qt.MouseButton.NoButton)  # a hover
        self.assertEqual(
            self.picture,
            [
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseMove,
                QEvent.Type.MouseButtonRelease,
            ],
        )
        self.picture.clear()
        spy = QSignalSpy(self.primary.playPauseRequested)
        QTest.mouseClick(
            self.mirror,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            self.mirror.button_rect("play").center().toPoint(),
        )
        self.assertEqual(self.picture, [])
        self.assertEqual(spy.count(), 1)

    def test_back_in_the_window_it_hides_and_forgets_the_pointer(self):
        self.show_controls()
        self.controls.docked()
        self.assertTrue(self.mirror.isHidden())
        self.assertIsNone(self.mirror._pointer)


if __name__ == "__main__":
    unittest.main()
