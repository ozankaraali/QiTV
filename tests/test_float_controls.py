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

from services.picture import same_shape  # noqa: E402
from tests.test_desktop_pip import mouse  # noqa: E402
from tests.test_transport_timeshift import PROGRAMME, timeline  # noqa: E402
from ui import float_controls, small_window  # noqa: E402
from ui.desktop_pip import FloatingPlayer  # noqa: E402
from ui.float_controls import FloatControls, FloatHost  # noqa: E402
from ui.overlays.transport import OWN_SETTERS, Transport  # noqa: E402
from ui.small_window import SmallWindow  # noqa: E402

_PREFIXES = ("set_", "show_", "update_", "end_")
LEFT, NO_KEYS = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
_NOT_CLICKED = ("seek", "times", "live", "indicator")  # (the bar is tested by its own reach)


def on_window(handle, kind, at):
    """The left button's `kind` ("press", "move", "release") at `at` (global) on `handle`'s
    window, as the platform delivers it: Qt picks the widget under it."""
    local = handle.mapFromGlobal(at)
    if kind == "press":
        QTest.mousePress(handle, LEFT, NO_KEYS, local)
    elif kind == "move":
        QTest.mouseMove(handle, local)
    else:
        QTest.mouseRelease(handle, LEFT, NO_KEYS, local)
    QApplication.processEvents()


def drag_on(handle, start, by):
    """A left press at `start` (global) on `handle`'s window, moved by `by` and released."""
    on_window(handle, "press", start)
    on_window(handle, "move", start + by)
    on_window(handle, "release", start + by)


def edge_zones(small: SmallWindow) -> dict[str, tuple[QPoint, QPoint]]:
    """A point within each edge's band and each corner's (the small window's coordinates; a
    corner's along an edge, near the next one), and a way out of it."""
    w, h = small.width(), small.height()
    edge, corner = small.band()
    i = edge // 2
    return {
        "left": (QPoint(i, h - corner - 3), QPoint(-40, 0)),  # (over the bottom shade)
        "right": (QPoint(w - 1 - i, corner + 2), QPoint(40, 0)),  # (the top one)
        "top": (QPoint(w // 2, i), QPoint(0, -40)),
        "bottom": (QPoint(w // 2, h - 1 - i), QPoint(0, 40)),
        "top-left": (QPoint(i, corner - 2), QPoint(-40, -40)),
        "top-right": (QPoint(w - corner + 1, i), QPoint(40, -40)),
        "bottom-left": (QPoint(corner - 2, h - 1 - i), QPoint(-40, 40)),
        "bottom-right": (QPoint(w - 1 - i, h - 1 - i), QPoint(40, 40)),
    }


def controls_on_the_band(small: SmallWindow, transport: Transport) -> list[tuple]:
    """Each control drawn within `small`'s edge band (`transport` over it, the same size): its
    name, a point on it within the band and one on it just beside the band (the small window's
    coordinates)."""
    rects = {n: r for n, r in transport._layout().items() if n not in _NOT_CLICKED}
    rects.update(transport.title_rects())
    w, h = transport.width(), transport.height()
    edge, _corner = small.band()
    found = []
    for name, drawn in rects.items():
        rect = drawn.toRect()
        for strip in (
            QRect(0, 0, edge, h),
            QRect(w - edge, 0, edge, h),
            QRect(0, 0, w, edge),
            QRect(0, h - edge, w, edge),
        ):
            part = rect.intersected(strip)
            if part.isEmpty():
                continue
            inside = part.center()
            beside = QPoint(
                min(max(inside.x(), edge), w - edge - 1), min(max(inside.y(), edge), h - edge - 1)
            )
            found.append((name, inside, beside))
            break
    return found


def off_a_control(mirror: Transport) -> tuple[str, QPoint]:
    """A control, and a point within its hit area but off its drawn place (REACH_PX around)."""
    layout = mirror._layout()
    reach = float_controls.REACH_PX
    for name, hit in mirror._hits(layout).items():
        if name == "seek" or name not in layout:
            continue
        drawn = layout[name].adjusted(-reach, -reach, reach, reach)
        for point in (
            QPointF(hit.center().x(), hit.top() + 1),
            QPointF(hit.center().x(), hit.bottom() - 1),
            QPointF(hit.left() + 1, hit.center().y()),
            QPointF(hit.right() - 1, hit.center().y()),
        ):
            if not drawn.contains(point) and mirror.control_at(point) == name:
                return name, point.toPoint()
    raise AssertionError("every hit area is within its control's reach")


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

    def test_a_press_off_a_drawn_control_is_the_pictures_even_within_its_hit_area(self):
        # The owner (2.13.5): clicks on the small picture opened the guide in the window; a
        # control's hit area covers most of the bottom there. Only the controls themselves act.
        self.show_controls()
        name, point = off_a_control(self.mirror)
        asked = []
        for request in float_controls.requests():
            getattr(self.mirror, request).connect(lambda *_a, r=request: asked.append(r))
        mouse(self.mirror, QEvent.Type.MouseButtonPress, point)
        mouse(self.mirror, QEvent.Type.MouseButtonRelease, point, Qt.MouseButton.NoButton)
        self.app.processEvents()
        self.assertEqual(asked, [], name)
        self.assertEqual(
            self.picture, [QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease]
        )
        self.come_back.assert_not_called()

    def test_back_in_the_window_it_hides_and_forgets_the_pointer(self):
        self.show_controls()
        self.controls.docked()
        self.assertTrue(self.mirror.isHidden())
        self.assertIsNone(self.mirror._pointer)


class EdgeBandTests(unittest.TestCase):
    """The small windows' grab band (ui/small_window.py): its edges and corners by its size."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def window(self, width, height):
        window = SmallWindow(Qt.WindowType.Widget)
        window.put(QRect(40, 40, width, height))
        self.addCleanup(window.deleteLater)
        return window

    def test_the_band_follows_the_width_up_to_its_most(self):
        for size, band in (
            ((240, 135), (8, small_window.CORNER_PX)),  # the smallest 16:9 one
            ((360, 203), (12, small_window.CORNER_PX)),  # a quarter of a 1440 px screen
            ((480, 270), (small_window.EDGE_PX, small_window.CORNER_PX)),
            ((202, 359), (6, small_window.CORNER_PX)),  # an upright picture's
        ):
            self.assertEqual(self.window(*size).band(), band, size)

    def test_never_more_than_a_fifth_of_its_smaller_side(self):
        # (only a shape no picture has: the width's rule keeps it smaller in every other)
        self.assertEqual(self.window(300, 40).band(), (8, 8))
        self.assertEqual(self.window(400, 100).band(), (small_window.EDGE_PX - 1, 20))

    def test_a_corner_reaches_along_its_edges_not_into_the_picture(self):
        window = self.window(360, 203)
        edge, corner = window.band()
        self.assertEqual(window._edges_at(QPoint(2, corner - 1)), (True, True, False, False))
        self.assertEqual(window._edges_at(QPoint(corner - 1, 2)), (True, True, False, False))
        self.assertEqual(window._edges_at(QPoint(2, corner)), (True, False, False, False))
        self.assertEqual(window._edges_at(QPoint(edge, edge)), (False,) * 4)  # ▶ and ⛶'s place
        self.assertEqual(
            window._edges_at(QPoint(359 - corner + 1, 200)), (False, False, True, True)
        )
        self.assertEqual(window._edges_at(QPoint(-1, 100)), (False,) * 4)  # off it


class FloatingEdgesTests(unittest.TestCase):
    """The floating window's edges win over its controls (the owner: "it's a bit hard to catch
    edges now"): delivered to the controls' window, as macOS does while they show."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.make(idle_ms=3000)

    def make(self, idle_ms):
        self.place = QRect(100, 100, 360, 203)
        self.floating = FloatingPlayer()
        self.addCleanup(self.floating.deleteLater)
        self.floating.put(self.place)
        self.floating.set_ratio(16 / 9)
        self.floating.hold(QWidget())
        self.floating.show()
        self.primary = Transport(idle_ms=idle_ms)
        self.primary.setGeometry(0, 0, 1280, 720)
        self.addCleanup(self.primary.close)
        self.primary.show_live("Channel", "Programme")
        self.primary.dismiss()
        self.come_back = mock.Mock()
        self.controls = FloatControls(
            self.floating,
            self.primary,
            come_back=self.come_back,
            mute=mock.Mock(),
            picture_mouse=self.floating.picture_mouse,
            join_all_spaces=lambda _widget: None,
        )
        self.addCleanup(self.controls.deleteLater)
        self.mirror = self.controls.mirror
        self.asked = []
        for request in float_controls.requests():
            getattr(self.mirror, request).connect(lambda *_a, r=request: self.asked.append(r))
        self.show_controls()

    def show_controls(self):
        self.controls.floated()
        self.controls.hover_at(self.floating.geometry().center())
        self.app.processEvents()
        self.assertTrue(self.controls.host.isVisible())
        self.assertEqual(self.controls.host.geometry(), self.floating.geometry())
        self.handle = self.controls.host.windowHandle()

    def at(self, point):
        return self.floating.mapToGlobal(point)

    def test_each_edge_and_corner_resizes_it_in_its_shape_over_its_controls(self):
        for zone, (point, way) in edge_zones(self.floating).items():
            self.floating.put(self.place)
            self.show_controls()
            drag_on(self.handle, self.at(point), way)
            geometry = self.floating.geometry()
            self.assertGreater(geometry.width(), self.place.width(), zone)
            self.assertTrue(same_shape(geometry.width() / geometry.height(), 16 / 9), zone)
            left, top = "left" in zone, "top" in zone
            self.assertEqual(
                geometry.right() if left else geometry.left(),
                self.place.right() if left else self.place.left(),
                zone,
            )
            self.assertEqual(
                geometry.bottom() if top else geometry.top(),
                self.place.bottom() if top else self.place.top(),
                zone,
            )
        self.assertEqual(self.asked, [])
        self.come_back.assert_not_called()

    def test_a_control_on_the_band_gives_it_to_the_edge_and_clicks_beside_it(self):
        found = controls_on_the_band(self.floating, self.mirror)
        names = {name for name, _inside, _beside in found}
        self.assertLessEqual({"play", "fullscreen", "favorite"}, names)  # (each corner, the top)
        for name, inside, beside in found:
            self.assertTrue(self.controls._on_a_control(QPointF(inside)), name)
            on_window(self.handle, "press", self.at(inside))
            self.assertIsNotNone(self.floating._press, name)  # a resize begins
            on_window(self.handle, "release", self.at(inside))
            self.assertEqual(self.asked, [], name)
            self.assertTrue(self.controls._on_a_control(QPointF(beside)), name)
            on_window(self.handle, "press", self.at(beside))
            on_window(self.handle, "release", self.at(beside))
            self.assertTrue(self.asked, name)  # the control's own
            self.assertIsNone(self.floating._press, name)
            self.asked.clear()
            self.floating.put(self.place)
            self.show_controls()

    def test_its_middle_still_drags_it(self):
        middle = QPoint(self.place.width() // 2, round(self.place.height() * 0.66))
        self.assertFalse(self.controls._on_a_control(QPointF(middle)))  # (the bottom shade)
        drag_on(self.handle, self.at(middle), QPoint(30, 20))
        self.assertEqual(self.floating.geometry(), self.place.translated(30, 20))

    def test_a_double_click_on_the_band_over_a_control_still_comes_back(self):
        returned = QSignalSpy(self.floating.returnRequested)
        name, inside, _beside = controls_on_the_band(self.floating, self.mirror)[0]
        QTest.mouseDClick(self.handle, LEFT, NO_KEYS, self.handle.mapFromGlobal(self.at(inside)))
        self.app.processEvents()
        self.assertEqual(returned.count(), 1, name)
        self.assertEqual(self.asked, [], name)

    def test_the_pointer_says_where_it_resizes_over_its_controls(self):
        cursor = Qt.CursorShape
        w, h = self.place.width(), self.place.height()
        for point, shape in (
            (QPoint(2, 2), cursor.SizeFDiagCursor),
            (QPoint(w - 3, 2), cursor.SizeBDiagCursor),
            (QPoint(2, round(h * 0.8)), cursor.SizeHorCursor),
            (QPoint(w // 2, h - 3), cursor.SizeVerCursor),
        ):
            on_window(self.handle, "move", self.at(point))
            self.assertEqual(self.handle.cursor().shape(), shape, point)
        on_window(self.handle, "move", self.at(QPoint(w // 2, round(h * 0.66))))
        self.assertEqual(self.handle.cursor().shape(), cursor.ArrowCursor)

    def test_its_controls_stay_up_through_a_resize(self):
        self.doCleanups()
        self.make(idle_ms=200)
        start = self.at(QPoint(self.place.width() - 3, self.place.height() - 3))
        on_window(self.handle, "press", start)
        elapse(500)  # (still, mid-resize)
        self.assertTrue(self.controls.host.isVisible())
        on_window(self.handle, "move", start + QPoint(40, 40))
        on_window(self.handle, "release", start + QPoint(40, 40))
        self.assertGreater(self.floating.width(), self.place.width())
        self.assertTrue(self.controls.host.isVisible())


if __name__ == "__main__":
    unittest.main()
