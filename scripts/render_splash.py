"""The build's splash image: the tile and lit glyph of QiTV's held, lit mark (ui.opening_mark),
which PyInstaller's bootloader shows on Windows and Linux from the double-click while the
one-file app unpacks, before Python runs (qitv-windows.spec, qitv-linux.spec). The splash
helper's first frame has the same tile and glyph in the same place (ui/splash_helper.py); its
light and shadow come in as it takes over.

    uv run python scripts/render_splash.py [--style windows|linux] [OUT]   # build/splash.png
    uv run python scripts/render_splash.py --check-tcl-tk

MARK_PX square, one pixel per pixel, with no light and no shadow: Tk can't show them. On Windows
it keys out one colour, so every pixel is opaque or transparent (a partly transparent one would
show that colour as a fringe); on Linux it shows no transparency, so the tile's colour fills the
corners. The style is this system's unless given. It draws offscreen, with no screen.
--check-tcl-tk says whether this Python has the Tcl/Tk that PyInstaller's splash needs
(tkinter, a threaded Tcl, shared libraries) and exits 1 if not: CI runs both before packaging.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "build" / "splash.png"
_APP: list[Any] = []  # the QGuiApplication the painting needs, for the run


STYLES = ("windows", "linux")
OPAQUE_FROM = 128  # Windows: a pixel at least half covered is opaque, any other transparent


def system_style() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def render(out: Path, style: Optional[str] = None) -> Path:
    """Paints the held, lit frame's tile and glyph into `out` (a PNG) in `style` (by default
    this system's), its folders made as needed."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter

    from ui.opening_mark import MARK_PX, STILL_T, TILE_COLOR, paint_mark

    style = style or system_style()
    if style not in STYLES:
        raise ValueError(f"no style {style!r}")
    if QGuiApplication.instance() is None:
        _APP.append(QGuiApplication(["render_splash"]))
    image = QImage(MARK_PX, MARK_PX, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(TILE_COLOR if style == "linux" else QColor(Qt.GlobalColor.transparent))
    painter = QPainter(image)
    try:
        # As the helper's window paints its mark (MARK_PX in the middle of its stage), bare.
        paint_mark(painter, QRectF(image.rect()), STILL_T, still=True, bare=True)
    finally:
        painter.end()
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    if style == "windows":
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                if color.alpha() >= OPAQUE_FROM:
                    color.setAlpha(255)
                    image.setPixelColor(x, y, color)
                else:
                    image.setPixelColor(x, y, QColor(0, 0, 0, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(out), "PNG"):
        raise OSError(f"could not write {out}")
    return out


def tcl_tk_problems(info: Any) -> list[str]:
    """What PyInstaller's Splash target would refuse in this Python's Tcl/Tk (`info`, its
    PyInstaller.utils.hooks.tcl_tk.tcltk_info); empty when the splash can be built."""
    if not info.available:
        return ["no tkinter (Tcl/Tk) in this Python"]
    problems = []
    if not info.tcl_threaded:
        problems.append("its Tcl is not threaded")
    if not info.tcl_shared_library or not info.tk_shared_library:
        problems.append("its Tcl/Tk is not in shared libraries (a static _tkinter)")
    return problems


def check_tcl_tk() -> int:
    """Prints this Python's Tcl/Tk; 0 when PyInstaller's splash can be built with it."""
    from PyInstaller.utils.hooks.tcl_tk import tcltk_info

    print(f"Python: {sys.executable} {sys.version.split()[0]}")
    problems = tcl_tk_problems(tcltk_info)
    if tcltk_info.available:
        print(f"Tcl {tcltk_info.tcl_version}, Tk {tcltk_info.tk_version}")
        print(f"Tcl: {tcltk_info.tcl_shared_library}")
        print(f"Tk: {tcltk_info.tk_shared_library}")
    for problem in problems:
        print(f"::error::The splash cannot be built: {problem} ({sys.executable}).")
    return 1 if problems else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--check-tcl-tk"]:
        return check_tcl_tk()
    style = None
    if args[:1] == ["--style"] and len(args) >= 2 and args[1] in STYLES:
        style, args = args[1], args[2:]
    if len(args) > 1 or (args and args[0].startswith("-")):
        print(__doc__, file=sys.stderr)
        return 2
    out = render(Path(args[0]) if args else DEFAULT_OUT, style)
    print(f"Wrote {out} ({style or system_style()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
