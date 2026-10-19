"""The macOS disk image (CI, once QiTV.app is signed): QiTV.app beside a shortcut to
Applications, in a window drawn for dragging one onto the other.

    uv run python scripts/make_dmg.py dist/QiTV.app dist/qitv-macos-universal.dmg

dmgbuild makes it headless, with no Finder or AppleScript: it copies the bundle with ditto, so
the signature still seals it, writes the window (its size, the icons' places, no toolbar or
sidebar) into the image's .DS_Store, and joins assets/dmg/background.png and its @2x into one
TIFF for the window's background. scripts/draw_dmg_background.py draws those two pictures from
the layout below; draw them again whenever it changes.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = ROOT / "assets" / "dmg" / "background.png"  # background@2x.png lies beside it
VOLUME_ICON = ROOT / "assets" / "qitv.icns"
VOLUME_NAME = "QiTV"
APP_NAME = "QiTV.app"
APPLICATIONS = "Applications"

# The window in points. The background is this size (its @2x twice it); Finder keeps the
# toolbar, sidebar, path and status bars hidden, so the background fills the window.
WINDOW_SIZE = (640, 380)
WINDOW_ORIGIN = (200, 120)  # where on the screen it opens
ICON_SIZE = 128
TEXT_SIZE = 14  # the icons' names
# Each icon's centre in the window, from its top left; Finder writes the name under the icon.
ICON_CENTRES = {APP_NAME: (170, 206), APPLICATIONS: (470, 206)}
# LZFSE (macOS 10.11 and newer; QiTV needs 13): on an Intel Mac's build, a tenth smaller than
# zlib (UDZO) and quicker to copy QiTV out of. LZMA (ULMO) is a quarter smaller, but copying
# QiTV out of it took four times as long (20 s), and checking the image twice as long.
FORMAT = "ULFO"


def settings(app: Path) -> dict[str, Any]:
    """dmgbuild's settings for an image holding `app` (named QiTV.app inside it)."""
    return {
        "format": FORMAT,
        "filesystem": "HFS+",
        "files": [(str(app), APP_NAME)],
        "symlinks": {APPLICATIONS: "/Applications"},
        "icon": str(VOLUME_ICON),
        "background": str(BACKGROUND),
        "window_rect": (WINDOW_ORIGIN, WINDOW_SIZE),
        "default_view": "icon-view",
        "show_toolbar": False,
        "show_sidebar": False,
        "show_pathbar": False,
        "show_status_bar": False,
        "show_tab_view": False,
        "icon_size": ICON_SIZE,
        "text_size": TEXT_SIZE,
        "label_pos": "bottom",
        "arrange_by": None,
        "icon_locations": dict(ICON_CENTRES),
    }


def build(app: Path, image: Path) -> None:
    """Write the disk image `image` holding `app`."""
    if not (app / "Contents" / "Info.plist").is_file():
        raise SystemExit(f"{app} is not an app bundle")
    # Imported here: dmgbuild reads the macOS version when imported, and fails elsewhere.
    import dmgbuild

    dmgbuild.build_dmg(str(image), VOLUME_NAME, settings=settings(app))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app", type=Path, help="the signed QiTV.app")
    parser.add_argument("image", type=Path, help="the .dmg to write")
    args = parser.parse_args(argv)
    build(args.app, args.image)
    print(f"{args.image}: {args.image.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
