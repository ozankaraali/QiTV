"""The QiTV logo (assets/qitv.png) for the Library's top bar, the welcome card and About.

The app icon's transparent margin and soft shadow are cropped off, so a logo drawn `n` pixels
tall is the rounded square itself. Pixmaps are made, with smooth scaling, at the device pixel
ratio of the widget that draws them (its screen's), so they stay crisp on HiDPI displays.
GUI thread only.
"""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from typing import Optional

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QBitmap, QImage, QPixmap, QRegion

logger = logging.getLogger(__name__)

BRAND_NAME = "QiTV"
LOGO_FILE = "qitv.png"
MASTER_PX = 256  # the cropped logo is kept this large; smaller sizes are scaled from it
_PIXMAPS_KEPT = 8

_master: Optional[QImage] = None
_missing_logged = False
_pixmaps: dict[tuple[int, float], QPixmap] = {}


def logo_path() -> Path:
    """assets/qitv.png next to the sources, or in a PyInstaller bundle's data."""
    bundle = getattr(sys, "_MEIPASS", None)
    root = Path(bundle) if bundle else Path(__file__).resolve().parent.parent
    return root / "assets" / LOGO_FILE


def _load_master() -> Optional[QImage]:
    global _master, _missing_logged
    if _master is not None:
        return _master
    image = QImage(str(logo_path()))
    if image.isNull():
        if not _missing_logged:
            _missing_logged = True
            logger.warning("The QiTV logo is missing (%s); the name is shown instead", LOGO_FILE)
        return None
    image = image.scaled(
        MASTER_PX, MASTER_PX, Qt.AspectRatioMode.KeepAspectRatio, Qt.SmoothTransformation
    )
    # The rounded square only: alpha of at least 50% (the shadow and margin are dropped).
    mask = image.createAlphaMask(Qt.ImageConversionFlag.ThresholdAlphaDither)
    bounds: QRect = QRegion(QBitmap.fromImage(mask)).boundingRect()
    _master = image.copy(bounds) if bounds.isValid() else image
    return _master


def logo_available() -> bool:
    """The logo asset can be drawn (else the name is shown instead)."""
    return _load_master() is not None


def logo_aspect() -> float:
    """Width over height of the drawn logo (1.0 when it is missing)."""
    master = _load_master()
    return master.width() / master.height() if master is not None else 1.0


def logo_pixmap(height: float, ratio: float) -> Optional[QPixmap]:
    """The logo `height` logical pixels tall for a widget whose devicePixelRatioF() is `ratio`;
    None when the asset is missing."""
    master = _load_master()
    if master is None or height <= 0:
        return None
    ratio = ratio if ratio > 0 else 1.0
    key = (round(height), ratio)
    pixmap = _pixmaps.get(key)
    if pixmap is None:
        device_h = max(1, round(key[0] * ratio))
        device_w = max(1, round(device_h * master.width() / master.height()))
        scaled = master.scaled(
            device_w, device_h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.SmoothTransformation
        )
        pixmap = QPixmap.fromImage(scaled)
        pixmap.setDevicePixelRatio(ratio)
        if len(_pixmaps) >= _PIXMAPS_KEPT:
            _pixmaps.pop(next(iter(_pixmaps)))
        _pixmaps[key] = pixmap
    return pixmap
