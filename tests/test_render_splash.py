"""The build's splash image (scripts/render_splash.py): the bare tile and the glyph's outline,
drawn whole and not yet filled (the loop at DRAWN_AT, ruling P2-12), the tile's own 100 px
square, which PyInstaller's bootloader shows on Windows (its corners keyed out: every pixel
opaque or transparent) and Linux (its corners the tile's colour: Tk there has no transparency)
while the one file unpacks; the splash helper's first frame has the same tile and outline in the
same place, then fills, its light and shadow coming in. The macOS app has none, and CI renders
it (and checks for Tcl/Tk) before packaging."""

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QRegion
from PySide6.QtWidgets import QApplication, QWidget

from scripts.render_splash import TILE_PX
from ui import splash_helper
from ui.opening_mark import (
    FILL_COLOR,
    MARK_PX,
    REDUCED_MOTION_ENV,
    STAGE_PX,
    STROKE_COLOR,
    TILE_COLOR,
    OpeningMark,
)

app = QApplication.instance() or QApplication([])

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "render_splash.py"
WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"
NO_SCREEN = ("QT_QPA_PLATFORM", "DISPLAY", "WAYLAND_DISPLAY")
INSET = (STAGE_PX - TILE_PX) // 2  # where the tile sits in the helper's window


def render(out: Path, style: str) -> QImage:
    """The script run as CI runs it, with no screen and no Qt platform chosen."""
    env = {k: v for k, v in os.environ.items() if k not in NO_SCREEN}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--style", style, str(out)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    image = QImage(str(out))
    if image.isNull():
        raise AssertionError(f"no image at {out}")
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def near(color: QColor, wanted: QColor, slack: int = 6) -> bool:
    return all(
        abs(a - b) <= slack for a, b in zip(color.getRgb()[:3], wanted.getRgb()[:3], strict=True)
    )


def pixels(image: QImage) -> list[tuple[int, int, QColor]]:
    width, height = image.width(), image.height()
    return [(x, y, image.pixelColor(x, y)) for x in range(width) for y in range(height)]


def helper_first_frame() -> QImage:
    """What the splash helper's window paints first over the bootloader's still: its mark
    started held from now (as _SplashWindow.begin starts it over a still), its light coming in
    from none, onto a transparent window, at one device pixel per pixel."""
    now = 1000.0
    mark = OpeningMark(clock=lambda: now)
    mark.resize(STAGE_PX, STAGE_PX)
    with mock.patch.dict(os.environ, {REDUCED_MOTION_ENV: "0"}):
        mark.start(now, light_in_s=splash_helper.LIGHT_IN_MS / 1000, held=True)
    image = QImage(STAGE_PX, STAGE_PX, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    mark.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    mark.deleteLater()
    return image.convertToFormat(QImage.Format.Format_ARGB32)


class RenderSplashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        cls.windows = render(Path(folder.name) / "nested" / "windows.png", "windows")
        cls.linux = render(Path(folder.name) / "linux.png", "linux")
        cls.shown = helper_first_frame()

    def outlined_tile(self, image):
        """The glyph's outline, drawn whole and not filled, on the tile."""
        opaque = [color for _x, _y, color in pixels(image) if color.alpha() == 255]
        self.assertGreater(sum(near(c, STROKE_COLOR) for c in opaque), 300)
        self.assertLess(sum(near(c, FILL_COLOR) for c in opaque), 20)  # no fill yet
        self.assertGreater(sum(near(c, TILE_COLOR) for c in opaque), 6000)

    def test_on_windows_every_pixel_is_opaque_or_keyed_out(self):
        # Tk keys out one colour there: a pixel partly transparent would show it as a fringe.
        image = self.windows
        self.assertEqual((image.width(), image.height()), (TILE_PX, TILE_PX))
        self.assertEqual({color.alpha() for _x, _y, color in pixels(image)}, {0, 255})
        last = TILE_PX - 1
        for x, y in ((0, 0), (last, 0), (0, last), (last, last)):
            self.assertEqual(image.pixelColor(x, y).alpha(), 0, (x, y))
        self.outlined_tile(image)

    def test_on_linux_the_corners_are_the_tiles_colour(self):
        # Tk there shows no transparency: a square dark tile, never a grey box around it.
        image = self.linux
        self.assertEqual((image.width(), image.height()), (TILE_PX, TILE_PX))
        self.assertEqual({color.alpha() for _x, _y, color in pixels(image)}, {255})
        last = TILE_PX - 1
        for x, y in ((0, 0), (last, 0), (0, last), (last, last), (1, 50), (50, last)):
            self.assertEqual(image.pixelColor(x, y).rgb(), TILE_COLOR.rgb(), (x, y))
        self.outlined_tile(image)

    def test_the_helper_first_shows_the_same_tile_and_outline_with_no_light(self):
        # Where the still is opaque and so is the helper's first frame, the pixels are the
        # same; around the tile the helper draws nothing yet (its light and shadow come in).
        for name, still in (("windows", self.windows), ("linux", self.linux)):
            with self.subTest(name):
                compared = []
                for x, y, color in pixels(still):
                    shown = self.shown.pixelColor(INSET + x, INSET + y)
                    if color.alpha() == 255 and shown.alpha() == 255:
                        compared.append(
                            max(abs(a - b) for a, b in zip(color.getRgb(), shown.getRgb()))
                        )
                self.assertGreater(len(compared), 9000)  # the 100 px tile but its corners
                self.assertLessEqual(max(compared), 1)
        outside = [
            color.alpha()
            for x, y, color in pixels(self.shown)
            if not (INSET <= x < INSET + TILE_PX and INSET <= y < INSET + TILE_PX)
        ]
        self.assertEqual(set(outside), {0})

    def test_it_is_the_tiles_own_square(self):
        # The tile is 400 of the mark's 416 grid units: 100 of its MARK_PX, on whole pixels,
        # so the still's edges are the tile's own and only its corners round off at the swap.
        self.assertEqual(TILE_PX, 100)
        self.assertEqual(MARK_PX, 104)
        top, left = self.windows.pixelColor(TILE_PX // 2, 0), self.windows.pixelColor(0, 50)
        self.assertEqual((top.alpha(), left.alpha()), (255, 255))  # the tile reaches its edges

    def test_the_still_sits_where_the_helpers_tile_is(self):
        # Tk centres the still as the helper centres its window (int(x + 0.5 * spare room));
        # 240 - 100 is even, so the still's corner is the helper's tile's on any screen.
        for area in (QRect(0, 0, 1280, 800), QRect(0, 0, 1281, 801), QRect(1920, -40, 1367, 769)):
            with self.subTest(area=area):
                screen = mock.Mock()
                screen.geometry.return_value = area
                with mock.patch.object(QGuiApplication, "primaryScreen", return_value=screen):
                    window = splash_helper.centred_on_primary()
                still = (
                    area.x() + int(0.5 * (area.width() - TILE_PX)),
                    area.y() + int(0.5 * (area.height() - TILE_PX)),
                )
                self.assertEqual((window.x() + INSET, window.y() + INSET), still)


def calls(spec: str) -> dict:
    tree = ast.parse((ROOT / spec).read_text(encoding="utf-8"))
    return {
        getattr(node.func, "id", ""): node for node in ast.walk(tree) if isinstance(node, ast.Call)
    }


class SpecTests(unittest.TestCase):
    def test_the_windows_and_linux_apps_show_it_while_they_unpack(self):
        for spec in ("qitv-windows.spec", "qitv-linux.spec"):
            with self.subTest(spec):
                found = calls(spec)
                splash = found["Splash"]
                self.assertEqual(ast.literal_eval(splash.args[0]), "build/splash.png")
                keywords = {k.arg: ast.unparse(k.value) for k in splash.keywords}
                self.assertEqual(keywords["binaries"], "a.binaries")
                self.assertEqual(keywords["datas"], "a.datas")
                self.assertEqual(keywords["text_pos"], "None")
                self.assertEqual(keywords["always_on_top"], "True")
                # Centred on the primary screen, as the splash helper centres its window (by
                # default, X11's Tk centres it on the whole virtual screen).
                self.assertEqual(keywords["center"], "'primary'")
                # One file: the splash target and its binaries go into the executable.
                exe = [ast.unparse(arg) for arg in found["EXE"].args]
                self.assertIn("splash", exe)
                self.assertIn("splash.binaries", exe)
                self.assertLess(exe.index("a.scripts"), exe.index("splash"))

    def test_the_mac_app_has_none(self):
        # PyInstaller's splash doesn't run in a macOS bundle (Tk on a second thread).
        self.assertNotIn("Splash", calls("qitv-macos.spec"))


class WorkflowTests(unittest.TestCase):
    def test_windows_and_linux_check_tcl_tk_and_render_it_before_packaging(self):
        steps = WORKFLOW.read_text(encoding="utf-8").split("      - name: ")
        packaging = {
            step.split("\n", 1)[0]: index
            for index, step in enumerate(steps)
            if "uv run pyinstaller" in step
        }
        for name, index in packaging.items():
            with self.subTest(name):
                before = "".join(steps[:index])
                job = before.rsplit("runs-on: ", 1)[1]
                checked = "uv run python scripts/render_splash.py --check-tcl-tk" in job
                rendered = "uv run python scripts/render_splash.py\n" in job
                if "macOS" in name:
                    self.assertFalse(checked or rendered)
                else:
                    self.assertTrue(checked, "no Tcl/Tk check before packaging")
                    self.assertTrue(rendered, "no splash image before packaging")
                    self.assertLess(
                        job.index("--check-tcl-tk"), job.index("scripts/render_splash.py\n")
                    )


class CheckTclTkTests(unittest.TestCase):
    def test_the_check_names_what_is_missing(self):
        from scripts import render_splash

        class Missing:
            available = False
            tcl_threaded = False
            tcl_shared_library = None
            tk_shared_library = None
            tcl_version = None
            tk_version = None

        problems = render_splash.tcl_tk_problems(Missing())
        self.assertTrue(problems)
        self.assertIn("tkinter", problems[0])

        class Found(Missing):
            available = True
            tcl_threaded = True
            tcl_shared_library = "/py/lib/libtcl9.0.so"
            tk_shared_library = "/py/lib/libtcl9tk9.0.so"
            tcl_version = (9, 0)
            tk_version = (9, 0)

        self.assertEqual(render_splash.tcl_tk_problems(Found()), [])
        unthreaded = type("Unthreaded", (Found,), {"tcl_threaded": False})
        self.assertEqual(len(render_splash.tcl_tk_problems(unthreaded())), 1)
        static = type("Static", (Found,), {"tk_shared_library": None})
        self.assertEqual(len(render_splash.tcl_tk_problems(static())), 1)


if __name__ == "__main__":
    unittest.main()
