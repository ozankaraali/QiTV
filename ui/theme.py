"""Design tokens for the TV shell (spec §5.6). Colours are CSS-style strings; qcolor() converts."""

from dataclasses import dataclass
import re

from PySide6.QtGui import QColor

_RGBA = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)")


@dataclass(frozen=True)
class Palette:
    canvas: str = "#0b0f14"
    panel: str = "rgba(8,11,15,0.86)"
    panel_raised: str = "#0e1217"
    text: str = "#eef2f6"
    text_muted_alpha: float = 0.62
    accent: str = "#e08a5f"
    brand: str = "#C96B43"
    live: str = "#d6453a"
    ok: str = "#5fbf7a"
    warn: str = "#e0b24a"


PALETTE = Palette()
RADIUS_CARD = 10
RADIUS_PANEL = 14
FOCUS_FILL_ALPHA = 0.22


def qcolor(value: str, alpha: float | None = None) -> QColor:
    """Build a QColor from '#rrggbb' or 'rgba(r,g,b,a)'; `alpha` (0–1) overrides the alpha."""
    match = _RGBA.fullmatch(value.strip())
    if match:
        red, green, blue = (int(part) for part in match.groups()[:3])
        color = QColor(red, green, blue)
        color.setAlpha(round(float(match.group(4)) * 255))
    else:
        color = QColor(value)
        if not value.startswith("#") or not color.isValid():
            raise ValueError(f"Unsupported colour: {value!r}")
    if alpha is not None:
        color.setAlphaF(alpha)
    return color
