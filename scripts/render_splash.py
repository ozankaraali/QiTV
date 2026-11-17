"""The build's splash image: QiTV's held, lit mark (ui.opening_mark), which PyInstaller's
bootloader shows on Windows and Linux from the double-click while the one-file app unpacks,
before Python runs (qitv-windows.spec, qitv-linux.spec). The splash helper's first frame is
this frame (ui/splash_helper.py), so its window replaces the bootloader's unseen.

    uv run python scripts/render_splash.py [OUT]     # OUT: build/splash.png
    uv run python scripts/render_splash.py --check-tcl-tk

240 px square (STAGE_PX), one pixel per pixel, transparent outside the mark's light. It draws
offscreen, with no screen. --check-tcl-tk says whether this Python has the Tcl/Tk that
PyInstaller's splash needs (tkinter, a threaded Tcl, shared libraries) and exits 1 if not: CI
runs both before packaging.
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


def render(out: Path) -> Path:
    """Paints the held, lit frame into `out` (a PNG), its folders made as needed."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter

    from ui.opening_mark import MARK_PX, STAGE_PX, STILL_T, mark_rect, paint_mark

    if QGuiApplication.instance() is None:
        _APP.append(QGuiApplication(["render_splash"]))
    image = QImage(STAGE_PX, STAGE_PX, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        # As the helper's window paints its mark: MARK_PX in the middle of the stage.
        paint_mark(painter, mark_rect(QRectF(image.rect()), MARK_PX), STILL_T, still=True)
    finally:
        painter.end()
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
    if len(args) > 1 or (args and args[0].startswith("-")):
        print(__doc__, file=sys.stderr)
        return 2
    out = render(Path(args[0]) if args else DEFAULT_OUT)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
