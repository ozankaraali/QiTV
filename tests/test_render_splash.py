"""The build's splash image (scripts/render_splash.py): QiTV's held, lit mark, 240 px square with
a transparent outside, the very frame the splash helper (ui/splash_helper.py) first shows over
it; the Windows and Linux one-file apps show it from PyInstaller's bootloader while they unpack,
the macOS app doesn't, and CI renders it (and checks for Tcl/Tk) before packaging."""

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QRegion
from PySide6.QtWidgets import QApplication, QWidget

from ui.opening_mark import FILL_COLOR, LOOP_S, STAGE_PX, STILL_T, TILE_COLOR, OpeningMark

app = QApplication.instance() or QApplication([])

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "render_splash.py"
WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"
NO_SCREEN = ("QT_QPA_PLATFORM", "DISPLAY", "WAYLAND_DISPLAY")


def render(out: Path) -> QImage:
    """The script run as CI runs it, with no screen and no Qt platform chosen."""
    env = {k: v for k, v in os.environ.items() if k not in NO_SCREEN}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(out)],
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


def helper_first_frame() -> QImage:
    """What the splash helper's window paints first over the bootloader's splash: its mark
    started at the lit frame's origin, onto a transparent window, at one device pixel per
    pixel."""
    now = 1000.0
    mark = OpeningMark(clock=lambda: now)
    mark.resize(STAGE_PX, STAGE_PX)
    mark.start(now - STILL_T * LOOP_S)
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
        cls.image = render(Path(folder.name) / "nested" / "splash.png")

    def test_it_is_240_px_square_transparent_outside_and_lit_inside(self):
        image = self.image
        self.assertEqual((image.width(), image.height()), (STAGE_PX, STAGE_PX))
        last = STAGE_PX - 1
        for x, y in ((0, 0), (last, 0), (0, last), (last, last)):
            self.assertEqual(image.pixelColor(x, y).alpha(), 0, (x, y))
        # The mark's square, 104 px in the middle: its tile, and its glyph filled (lit).
        square = [image.pixelColor(x, y) for x in range(68, 172) for y in range(68, 172)]
        opaque = [color for color in square if color.alpha() == 255]
        self.assertGreater(sum(near(c, FILL_COLOR) for c in opaque), 500)
        self.assertGreater(sum(near(c, TILE_COLOR) for c in opaque), 2000)
        # The light around the tile: a translucent coral, fading out before the corners.
        light = image.pixelColor(STAGE_PX // 2, 46)
        self.assertTrue(0 < light.alpha() < 255, light.alpha())
        self.assertGreater(light.red(), light.blue())

    def test_it_is_the_frame_the_splash_helper_first_shows(self):
        # The helper's window replaces the bootloader's: the same pixels, so the swap is unseen.
        shown = helper_first_frame()
        differences = [
            max(abs(a - b) for a, b in zip(self.image.pixelColor(x, y).getRgb(), c.getRgb()))
            for x in range(STAGE_PX)
            for y in range(STAGE_PX)
            for c in (shown.pixelColor(x, y),)
        ]
        self.assertLessEqual(max(differences), 1)


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
