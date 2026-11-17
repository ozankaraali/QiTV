"""QiTV's mark as the opening draws it (ui/opening_mark.py): the frames paint_mark makes,
rendered offscreen on the logo's 416 grid; the asset's shape against assets/qitv.png; the
OpeningMark widget's loop and finish on a fake clock; and how it learns the system asks for
less motion."""

import ast
import ctypes
import os
from pathlib import Path
import subprocess
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QRegion, QTransform
from PySide6.QtWidgets import QApplication, QWidget

from ui import opening_mark
from ui.opening_mark import (
    LOOP_MS,
    MARK_PATH,
    OpeningMark,
    mark_rect,
    paint_mark,
    parse_path,
)

app = QApplication.instance() or QApplication([])

ROOT = Path(__file__).resolve().parent.parent
TILE = QColor("#1c1917")
STROKE = QColor("#ff7a6b")
FILL = QColor("#ff6656")
BASE_INSIDE = QPointF(150, 290)  # inside the glyph's base bar, far from its edges
LOOP_S = LOOP_MS / 1000


def render(t, *, still=False, opening=0.0, size=416, dpr=1.0, rect=None, opacity=1.0):
    image = QImage(round(size * dpr), round(size * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setOpacity(opacity)
    paint_mark(painter, rect or QRectF(0, 0, size, size), t, still=still, opening=opening)
    painter.end()
    return image


def at(image, point, dpr=1.0):
    return image.pixelColor(int(point.x() * dpr), int(point.y() * dpr))


def near(color, expected, tolerance=3):
    return (
        color.alpha() == 255
        and abs(color.red() - expected.red()) <= tolerance
        and abs(color.green() - expected.green()) <= tolerance
        and abs(color.blue() - expected.blue()) <= tolerance
    )


def differs(a, b, tolerance=6):
    pairs = ((a.red(), b.red()), (a.green(), b.green()), (a.blue(), b.blue()))
    return max(abs(x - y) for x, y in pairs + ((a.alpha(), b.alpha()),)) > tolerance


def blurred_pixels(image, y, x0, x1):
    """Pixels on row `y` between x0 and x1 that are neither the colour at x0 nor at x1."""
    outside, inside = image.pixelColor(x0, y), image.pixelColor(x1, y)
    return sum(
        1
        for x in range(x0, x1 + 1)
        if differs(image.pixelColor(x, y), outside) and differs(image.pixelColor(x, y), inside)
    )


class FramesTests(unittest.TestCase):
    def test_while_the_line_draws_the_mark_is_not_filled_yet(self):
        image = render(0.3)
        drawn, not_yet = MARK_PATH.pointAtPercent(0.05), MARK_PATH.pointAtPercent(0.97)
        self.assertTrue(near(at(image, drawn), STROKE), at(image, drawn).name())
        self.assertTrue(near(at(image, not_yet), TILE), at(image, not_yet).name())
        self.assertTrue(near(at(image, BASE_INSIDE), TILE), at(image, BASE_INSIDE).name())

    def test_nothing_is_drawn_at_the_loops_start(self):
        image = render(0.02)
        start = MARK_PATH.pointAtPercent(0.0)
        self.assertTrue(near(at(image, start), TILE), at(image, start).name())

    def test_held_frame_is_filled_and_outlined(self):
        image = render(0.7)
        self.assertTrue(near(at(image, BASE_INSIDE), FILL), at(image, BASE_INSIDE).name())
        for percent in (0.05, 0.5, 0.97):
            point = MARK_PATH.pointAtPercent(percent)
            self.assertTrue(near(at(image, point), STROKE), (percent, at(image, point).name()))

    def test_still_is_the_held_frame_at_0_7(self):
        self.assertEqual(render(0.0, still=True), render(0.7))

    def test_a_bare_mark_paints_its_tile_and_glyph_alone(self):
        # The splash standing on the player's mark: their light and shadow aren't drawn twice.
        def stage(bare):
            image = QImage(240, 240, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            rect = mark_rect(QRectF(0, 0, 240, 240), opening_mark.MARK_PX)
            paint_mark(painter, rect, 0.56, bare=bare)  # the bloom
            painter.end()
            return image

        full, bare = stage(False), stage(True)
        for outside in ((50, 120), (120, 172)):  # the light beside the tile; its shadow below
            self.assertGreater(full.pixelColor(*outside).alpha(), 10, outside)
            self.assertEqual(bare.pixelColor(*outside).alpha(), 0, outside)
        for inside in ((120, 124), (80, 80), (150, 100)):  # the tile and the glyph
            self.assertEqual(bare.pixelColor(*inside), full.pixelColor(*inside), inside)
        self.assertFalse(OpeningMark().bare)  # a mark paints all of itself unless told

    def test_its_light_and_shadow_come_in_as_one_amount(self):
        # The splash taking over from the bootloader's still (its bare tile and glyph): the
        # light and the shadow come in from none, the tile and the glyph unchanged.
        def stage(**kwargs):
            image = QImage(240, 240, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            rect = mark_rect(QRectF(0, 0, 240, 240), opening_mark.MARK_PX)
            paint_mark(painter, rect, opening_mark.STILL_T, **kwargs)
            painter.end()
            return image

        none, half, full = stage(light=0.0), stage(light=0.5), stage()
        self.assertEqual(none, stage(bare=True))
        self.assertEqual(stage(light=1.0), full)
        for outside in ((50, 120), (120, 174)):  # the light beside the tile; its shadow below
            levels = [image.pixelColor(*outside).alpha() for image in (none, half, full)]
            self.assertEqual(levels[0], 0, outside)
            self.assertLess(levels[1], levels[2], outside)
            self.assertGreater(levels[1], 0, outside)
        for inside in ((120, 124), (80, 80), (150, 100)):  # the tile and the glyph
            self.assertEqual(half.pixelColor(*inside), full.pixelColor(*inside), inside)

    def test_the_mark_is_gone_at_98_percent_but_the_tile_stays(self):
        image = render(0.985)
        self.assertTrue(near(at(image, BASE_INSIDE), TILE), at(image, BASE_INSIDE).name())

    def test_a_fully_opened_mark_is_transparent(self):
        image = render(0.7, opening=1.0)
        alphas = {
            image.pixelColor(x, y).alpha() for y in range(0, 416, 2) for x in range(0, 416, 2)
        }
        self.assertEqual(alphas, {0})

    def test_opening_grows_and_fades_the_whole_mark(self):
        image = render(0.7, opening=0.5)
        inside = at(image, BASE_INSIDE)
        self.assertTrue(60 < inside.alpha() < 200, inside.alpha())
        # as one layer: the faded glyph keeps its colour, the tile does not show through it
        self.assertTrue(near(QColor(inside.red(), inside.green(), inside.blue()), FILL, 2))
        # 1.06x about the centre: the stroke's outer edge on the left (x=72.5 at rest) moves
        # out to x=64.4, over what was the tile
        grown, rest = at(image, QPointF(68, 290)), at(render(0.7), QPointF(68, 290))
        self.assertTrue(near(rest, TILE), rest.name())
        self.assertLess(grown.alpha(), 255)
        self.assertTrue(near(QColor(grown.red(), grown.green(), grown.blue()), STROKE, 2))

    def test_the_painters_opacity_fades_the_mark_as_one(self):
        image = render(0.7, opacity=0.5)
        inside = at(image, BASE_INSIDE)
        self.assertTrue(100 < inside.alpha() < 156, inside.alpha())
        self.assertTrue(near(QColor(inside.red(), inside.green(), inside.blue()), FILL, 2))

    def test_the_fading_glyph_has_no_bright_rim(self):
        # stroke over fill, faded as a group: where they overlap is no brighter than the stroke
        image = render(0.93)
        rim = at(image, QPointF(80.0, 290))  # on the stroke, 2.5 inside the fill's edge
        outer = at(image, QPointF(75.0, 290))  # on the stroke, 2.5 outside it
        self.assertLessEqual(abs(rim.green() - outer.green()), 3, (rim.name(), outer.name()))

    def test_the_path_sits_inside_the_tile(self):
        bounds = MARK_PATH.boundingRect()
        self.assertGreater(bounds.width(), 200)
        self.assertGreater(bounds.height(), 200)
        self.assertTrue(QRectF(8, 8, 400, 400).contains(bounds.adjusted(-5, -5, 5, 5)), bounds)

    def test_the_light_blooms_at_the_fill_and_ends_inside_its_circle(self):
        # the mark 416 wide in the middle of 832: the light (184/104 of it, x1.14 at the
        # bloom) has room; it reaches zero at 62 % of its gradient's farthest-corner radius
        rect = QRectF(208, 208, 416, 416)
        lit, calm = render(0.56, size=832, rect=rect), render(0.0, size=832, rect=rect)
        above = QPointF(416, 416 - 230)  # above the tile and its shadow
        self.assertGreater(at(lit, above).alpha(), at(calm, above).alpha())
        self.assertGreater(at(calm, above).alpha(), 0)
        self.assertGreater(at(lit, above).red(), at(lit, above).blue())
        self.assertEqual(at(lit, QPointF(416, 416 - 400)).alpha(), 0)

    def test_the_tile_casts_a_shadow_downwards(self):
        rect = QRectF(208, 208, 416, 416)
        image = render(0.0, size=832, rect=rect)
        # 4 below the tile and 4 above it: the light is the same, the shadow falls below
        below, above = image.pixelColor(416, 208 + 412), image.pixelColor(416, 208 + 4)
        self.assertLess(below.red(), above.red())


class SharpnessTests(unittest.TestCase):
    """At 1.5x the frame is drawn for the device's pixels, not scaled up from a smaller one."""

    DPR = 1.5

    def assert_sharp(self, image, growth=1.0):
        # across the stroke's outer edge on the base bar's left (x = 72.5 at rest), with the
        # tile on one side and the stroke on the other
        edge, row = 208 - (208 - 72.5) * growth, 208 + (290 - 208) * growth
        x0, x1 = int((edge - 6) * self.DPR), int((edge + 6) * self.DPR)
        self.assertLessEqual(blurred_pixels(image, int(row * self.DPR), x0, x1), 2)

    def test_the_held_frame_is_sharp(self):
        self.assert_sharp(render(0.7, dpr=self.DPR))

    def test_a_fading_glyph_is_sharp(self):
        self.assert_sharp(render(0.93, dpr=self.DPR))

    def test_an_opening_mark_is_sharp(self):
        self.assert_sharp(render(0.7, opening=0.4, dpr=self.DPR), growth=1.048)


class ShapeTests(unittest.TestCase):
    """The asset's glyph against assets/qitv.png's coral pixels. The 416 grid is that icon at
    half size with 48 pixels cut from each side: grid x is (icon x - 96) / 2."""

    @staticmethod
    def mask(image, channel, threshold):
        data = bytes(image.convertToFormat(QImage.Format.Format_ARGB32).constBits())
        table = bytes(1 if v >= threshold else 0 for v in range(256))
        return data[channel::4].translate(table)

    @staticmethod
    def count(bits):
        return bits.count(1)

    def test_the_mark_covers_the_logos_glyph(self):
        logo = QImage(str(ROOT / "assets" / "qitv.png"))
        self.assertEqual((logo.width(), logo.height()), (1024, 1024))
        glyph = self.mask(logo, 2, 142)  # red: half way from the tile's 28 to the coral's 255
        drawn = QImage(1024, 1024, QImage.Format.Format_ARGB32)
        drawn.fill(Qt.GlobalColor.transparent)
        painter = QPainter(drawn)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setTransform(QTransform.fromScale(2, 2) * QTransform.fromTranslate(96, 96))
        painter.fillPath(MARK_PATH, QColor("white"))
        painter.end()
        fill = self.mask(drawn, 3, 128)
        both = (int.from_bytes(glyph, "big") & int.from_bytes(fill, "big")).to_bytes(
            len(fill), "big"
        )
        inside = self.count(both) / self.count(glyph)
        outside = 1 - self.count(both) / self.count(fill)
        self.assertGreater(inside, 0.97)
        self.assertLess(outside, 0.03)


class ParserTests(unittest.TestCase):
    def test_lines_and_curves(self):
        path = parse_path("M0 0 L10 0 L10 10 Z")
        self.assertEqual(path.boundingRect(), QRectF(0, 0, 10, 10))
        curve = parse_path("M0,0 C0 10 10 10 10 0 Z")
        self.assertEqual(curve.elementAt(1).type, QPainterPath.ElementType.CurveToElement)

    def test_other_commands_are_refused(self):
        for d in ("M0 0 A5 5 0 0 1 10 0", "m0 0 l10 0", "M0 0 L10", "L10 0"):
            with self.subTest(d=d), self.assertRaises(ValueError):
                parse_path(d)

    def test_the_specs_bundle_the_asset(self):
        for spec in ("qitv-macos.spec", "qitv-windows.spec", "qitv-linux.spec"):
            with self.subTest(spec=spec):
                tree = ast.parse((ROOT / spec).read_text(encoding="utf-8"))
                analysis = next(
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis"
                )
                datas = next(k.value for k in analysis.keywords if k.arg == "datas")
                pairs = [ast.literal_eval(e) for e in datas.elts if isinstance(e, ast.Tuple)]
                sources = {src for src, dest in pairs if dest == "assets"}
                self.assertTrue(
                    sources & {"assets", "assets/*", "assets/qitv-mark.svg"}, (spec, sources)
                )

    def test_the_asset_is_found_in_a_frozen_app(self):
        with mock.patch("sys._MEIPASS", "/bundle", create=True):
            self.assertEqual(opening_mark.mark_file(), Path("/bundle/assets/qitv-mark.svg"))
        self.assertTrue(opening_mark.mark_file().is_file())


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class WidgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        motion = mock.patch.object(OpeningMark, "reduced_motion", return_value=False)
        self.reduced = motion.start()
        self.addCleanup(motion.stop)
        self.mark = OpeningMark(clock=self.clock)
        self.addCleanup(self.mark.deleteLater)
        self.calls = []

    def then(self):
        self.calls.append(self.clock.now)

    def run_out(self, step=0.01, limit=3.0):
        """Ticks until `then` ran; the seconds it took."""
        start = self.clock.now
        while not self.calls and self.clock.now - start < limit:
            self.clock.now += step
            self.mark._tick()
        self.assertTrue(self.calls, "then never ran")
        return self.calls[0] - start

    def at_phase(self, phase):
        self.mark.start()
        self.clock.now += phase * LOOP_S

    def test_the_loop_runs_on_the_clock(self):
        self.at_phase(0.25)
        self.assertAlmostEqual(self.mark.frame().t, 0.25)
        self.clock.now += LOOP_S
        self.assertAlmostEqual(self.mark.frame().t, 0.25)

    def test_a_mark_started_at_anothers_origin_shows_its_frame(self):
        # The splash's mark and the player's (another process, the same monotonic clock).
        self.mark.start()
        origin = self.mark.origin
        self.assertEqual(origin, self.clock.now)
        self.clock.now += 1.3
        other = OpeningMark(clock=self.clock)
        self.addCleanup(other.deleteLater)
        other.start(origin)
        self.assertEqual(other.origin, origin)
        for _ in range(5):
            self.clock.now += 0.37
            self.assertAlmostEqual(other.frame(self.clock.now).t, self.mark.frame().t)
        other.start(origin + 1.0)  # while it loops, start() changes nothing (as before)
        self.assertEqual(other.origin, origin)
        self.assertIsNone(OpeningMark(clock=self.clock).origin)  # before any start

    def test_an_early_finish_completes_the_draw_within_0_4_s_then_opens(self):
        self.at_phase(0.1)
        self.mark.finish(self.then)
        self.clock.now += 0.4
        frame = self.mark.frame()
        self.assertAlmostEqual(frame.t, 0.56)
        self.assertAlmostEqual(frame.opening, 0.0)
        self.clock.now -= 0.4
        took = self.run_out()
        self.assertGreaterEqual(took, 0.75 - 0.011)
        self.assertLessEqual(took, 0.75 + 0.011)

    def test_a_late_finish_draws_the_rest_twice_as_fast(self):
        self.at_phase(0.5)
        self.mark.finish(self.then)
        took = self.run_out()
        self.assertAlmostEqual(took, 0.06 * LOOP_S / 2 + 0.35, delta=0.011)

    def test_a_finish_while_lit_opens_at_once(self):
        self.at_phase(0.7)
        self.mark.finish(self.then)
        self.clock.now += 0.175
        frame = self.mark.frame()
        self.assertAlmostEqual(frame.t, 0.7)
        self.assertGreater(frame.opening, 0.2)
        self.assertLess(frame.opening, 0.8)
        self.clock.now -= 0.175
        self.assertAlmostEqual(self.run_out(), 0.35, delta=0.011)

    def test_a_finish_while_fading_lights_the_mark_again_first(self):
        self.at_phase(0.95)
        self.mark.finish(self.then)
        back = 0.07 * LOOP_S / 2
        self.clock.now += back
        self.assertAlmostEqual(self.mark.frame().t, 0.88)
        self.clock.now -= back
        self.assertAlmostEqual(self.run_out(), back + 0.35, delta=0.011)

    def test_a_finish_before_start_still_draws_first(self):
        self.mark.finish(self.then)
        self.assertLessEqual(self.run_out(), 0.75 + 0.011)

    def test_then_runs_once_for_each_finish(self):
        self.at_phase(0.3)
        second = []
        self.mark.finish(self.then)
        self.mark.finish(lambda: second.append(1))
        self.run_out()
        for _ in range(50):
            self.clock.now += 0.02
            self.mark._tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(second, [1])

    def test_a_finished_mark_starts_again(self):
        self.at_phase(0.7)
        self.mark.finish(self.then)
        self.run_out()
        self.mark.start()
        self.assertAlmostEqual(self.mark.frame().t, 0.0)
        self.assertAlmostEqual(self.mark.frame().opening, 0.0)

    def test_start_during_a_finish_keeps_it_and_its_then(self):
        self.at_phase(0.3)
        self.mark.finish(self.then)
        self.clock.now += 0.2
        before = self.mark.frame()
        self.mark.start()  # e.g. a second open while the first one's mark opens
        self.assertEqual(self.mark.frame(), before)
        self.clock.now -= 0.2
        self.assertAlmostEqual(self.run_out(), 0.75, delta=0.011)

    def test_start_during_a_plain_finish_before_any_start_keeps_it(self):
        self.reduced.return_value = True
        self.mark.finish(self.then)
        self.mark.start()
        self.assertAlmostEqual(self.run_out(), 0.35, delta=0.011)

    def test_finish_after_the_end_calls_then_later_from_the_event_loop(self):
        self.at_phase(0.7)
        self.mark.finish(self.then)
        self.run_out()
        late = []
        self.mark.finish(lambda: late.append(1))
        self.assertEqual(late, [])  # never from inside finish()
        for _ in range(20):
            if late:
                break
            app.processEvents()
        self.assertEqual(late, [1])

    def test_a_light_in_fades_its_light_in_from_start(self):
        self.mark.start(light_in_s=0.3)
        self.assertEqual(self.mark.frame().light, 0.0)
        self.clock.now += 0.15
        self.assertAlmostEqual(self.mark.frame().light, 0.5, delta=0.01)  # eased: half way
        self.clock.now += 0.075
        self.assertTrue(0.5 < self.mark.frame().light < 1.0)
        self.clock.now += 0.075
        self.assertEqual(self.mark.frame().light, 1.0)
        other = OpeningMark(clock=self.clock)
        self.addCleanup(other.deleteLater)
        other.start()
        self.assertEqual(other.frame().light, 1.0)  # without one, all of it from the start

    def test_a_light_in_under_reduced_motion_is_a_plain_fade(self):
        # The held, lit frame, its light fading in: nothing moves; the timer stops after.
        self.reduced.return_value = True
        self.mark.start(light_in_s=0.3)
        self.mark.show()
        self.assertTrue(self.mark._timer.isActive())
        self.clock.now += 0.15
        frame = self.mark.frame()
        self.assertTrue(frame.still)
        self.assertTrue(0.0 < frame.light < 1.0)
        self.clock.now += 0.16
        self.mark._tick()
        self.assertEqual(self.mark.frame().light, 1.0)
        self.assertFalse(self.mark._timer.isActive())

    def test_it_paints_the_light_its_frame_says(self):
        self.mark.resize(240, 240)
        self.mark.start(light_in_s=0.3)

        def beside_the_tile():
            image = QImage(240, 240, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            self.mark.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
            return image.pixelColor(64, 120).alpha()  # 6 px beside the tile

        self.assertEqual(beside_the_tile(), 0)
        self.clock.now += 0.3
        self.assertGreater(beside_the_tile(), 0)

    def test_reduced_motion_holds_the_lit_frame_and_fades_plainly(self):
        self.reduced.return_value = True
        self.mark.start()
        self.clock.now += 1.0
        self.assertTrue(self.mark.frame().still)
        self.mark.finish(self.then)
        self.clock.now += 0.175
        frame = self.mark.frame()
        self.assertTrue(frame.still)
        self.assertEqual(frame.opening, 0.0)
        self.assertTrue(0.2 < frame.fade < 0.8, frame.fade)
        self.clock.now -= 0.175
        self.assertAlmostEqual(self.run_out(), 0.35, delta=0.011)

    def test_reduced_motion_known_late_still_holds_the_frame(self):
        self.at_phase(0.01)
        self.assertFalse(self.mark.frame().still)
        self.reduced.return_value = True
        self.mark._tick()
        self.assertTrue(self.mark.frame().still)

    def test_a_hidden_mark_stops_its_loop(self):
        self.mark.start()
        self.mark.show()
        self.assertTrue(self.mark._timer.isActive())
        self.mark.hide()
        self.assertFalse(self.mark._timer.isActive())

    def test_the_mark_paints_in_the_middle(self):
        self.mark.resize(240, 240)
        self.at_phase(0.7)
        image = QImage(240, 240, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        # its own painting only: a top-level widget's render() fills the window's background
        self.mark.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
        # the 104 px mark centred: the base's inside maps to 68 + 150/4, 68 + 290/4
        self.assertTrue(near(image.pixelColor(68 + 37, 68 + 72), FILL))
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)

    def test_an_opening_mark_inside_a_window_lands_on_the_devices_pixels(self):
        # the open's layer through a child widget's offset, at a fractional pixel ratio
        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        parent.resize(260, 260)
        mark = OpeningMark(parent, clock=self.clock)
        mark.move(7, 9)
        mark.resize(240, 240)
        mark.start()
        self.clock.now += 0.7 * LOOP_S
        mark.finish(self.then)
        self.assertTrue(mark._timer.isActive())  # never shown: the finish still runs to `then`
        self.clock.now += 0.1
        frame = mark.frame()
        self.assertTrue(0.0 < frame.opening < 1.0)

        def image():
            picture = QImage(390, 390, QImage.Format.Format_ARGB32_Premultiplied)
            picture.setDevicePixelRatio(1.5)
            picture.fill(Qt.GlobalColor.transparent)
            return picture

        through_window, direct = image(), image()
        parent.render(through_window, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
        painter = QPainter(direct)
        rect = mark_rect(QRectF(7, 9, 240, 240))
        paint_mark(painter, rect, frame.t, opening=frame.opening)
        painter.end()
        self.assertGreater(at(direct, rect.center(), 1.5).alpha(), 0)
        worst = max(
            max(
                abs(a.red() - b.red()),
                abs(a.green() - b.green()),
                abs(a.blue() - b.blue()),
                abs(a.alpha() - b.alpha()),
            )
            for y in range(0, 390, 3)
            for x in range(0, 390, 3)
            for a, b in [(through_window.pixelColor(x, y), direct.pixelColor(x, y))]
        )
        self.assertLessEqual(worst, 2)


class ReducedMotionTests(unittest.TestCase):
    def setUp(self):
        opening_mark._forget_reduced_motion()
        self.addCleanup(opening_mark._forget_reduced_motion)

    def linux(self, run):
        patches = [
            mock.patch.object(opening_mark, "_platform", return_value="linux"),
            mock.patch.object(opening_mark.subprocess, "run", side_effect=run),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def answer(self):
        opening_mark._motion.worker.join(2)
        return opening_mark._reduced_motion()

    def test_linux_asks_gsettings_on_a_worker_without_waiting(self):
        release = threading.Event()
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            release.wait(2)
            return subprocess.CompletedProcess(args, 0, "false\n", "")

        self.linux(run)
        self.assertFalse(opening_mark._reduced_motion())  # asked, not answered yet
        self.assertFalse(opening_mark._reduced_motion())
        release.set()
        self.assertTrue(self.answer())
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(tuple(args), opening_mark.GSETTINGS_ANIMATIONS)
        self.assertEqual(args[-2:], ["org.gnome.desktop.interface", "enable-animations"])
        self.assertEqual(kwargs["timeout"], 1.0)

    def test_linux_animations_on_or_no_answer_mean_motion(self):
        def missing(args, **kwargs):
            raise FileNotFoundError("gsettings")

        def slow(args, **kwargs):
            raise subprocess.TimeoutExpired(args, 1.0)

        def on(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "true\n", "")

        for run in (missing, slow, on):
            with self.subTest(run=run.__name__):
                opening_mark._forget_reduced_motion()
                with (
                    mock.patch.object(opening_mark, "_platform", return_value="linux"),
                    mock.patch.object(opening_mark.subprocess, "run", side_effect=run),
                ):
                    opening_mark._reduced_motion()
                    self.assertFalse(self.answer())

    def test_windows_reads_client_area_animation(self):
        seen = []

        def spi(action, param, pointer, flags):
            seen.append(action)
            pointer._obj.value = 0  # animations off
            return 1

        user32 = mock.Mock(SystemParametersInfoW=spi)
        with (
            mock.patch.object(opening_mark, "_platform", return_value="win32"),
            mock.patch.object(opening_mark, "_user32", return_value=user32),
        ):
            self.assertTrue(opening_mark._reduced_motion())
        self.assertEqual(seen, [0x1042])
        self.assertEqual(
            spi.argtypes, [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        )
        self.assertIs(spi.restype, ctypes.c_long)

    def test_macos_asks_the_shared_workspace(self):
        from ui import macos_window

        sent = []

        def send(receiver, selector, restype=ctypes.c_void_p, *args):
            sent.append((receiver, selector))
            return {b"sharedWorkspace": 22, b"accessibilityDisplayShouldReduceMotion": True}[
                selector
            ]

        objc = mock.Mock()
        objc.objc_getClass.return_value = 11
        with (
            mock.patch.object(opening_mark, "_platform", return_value="darwin"),
            mock.patch.object(macos_window, "_objc", return_value=objc),
            mock.patch.object(macos_window, "_send", side_effect=send),
        ):
            self.assertTrue(opening_mark._reduced_motion())
        objc.objc_getClass.assert_called_with(b"NSWorkspace")
        self.assertEqual(
            sent, [(11, b"sharedWorkspace"), (22, b"accessibilityDisplayShouldReduceMotion")]
        )

    def test_macos_without_appkit_asks_again_later(self):
        from ui import macos_window

        objc = mock.Mock()
        objc.objc_getClass.return_value = None
        with (
            mock.patch.object(opening_mark, "_platform", return_value="darwin"),
            mock.patch.object(macos_window, "_objc", return_value=objc),
        ):
            self.assertFalse(opening_mark._reduced_motion())
            self.assertFalse(opening_mark._reduced_motion())
        self.assertEqual(objc.objc_getClass.call_count, 2)


if __name__ == "__main__":
    unittest.main()
