"""Design tokens for the TV shell (spec §5.6). Colours are CSS-style strings; qcolor() converts."""

from dataclasses import dataclass
import re

from PySide6.QtGui import QColor

_RGBA = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)")
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")


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
    """Build a QColor from CSS '#rgb', '#rgba', '#rrggbb', '#rrggbbaa' or 'rgba(r,g,b,a)'.

    `alpha` (0-1) overrides the colour's own alpha. Raises ValueError for anything else.
    """
    if alpha is not None and not 0.0 <= alpha <= 1.0:
        raise ValueError(f"Alpha must be between 0 and 1: {alpha!r}")
    text = value.strip()
    match = _RGBA.fullmatch(text)
    if match:
        red, green, blue = (int(part) for part in match.groups()[:3])
        opacity = float(match.group(4))
        if max(red, green, blue) > 255 or not 0.0 <= opacity <= 1.0:
            raise ValueError(f"Colour component out of range: {value!r}")
        color = QColor(red, green, blue, round(opacity * 255))
    elif _HEX.fullmatch(text):
        digits = text[1:]
        if len(digits) in (3, 4):
            digits = "".join(ch * 2 for ch in digits)
        red, green, blue = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
        color = QColor(red, green, blue, int(digits[6:8], 16) if len(digits) == 8 else 255)
    else:
        raise ValueError(f"Unsupported colour: {value!r}")
    if alpha is not None:
        color.setAlphaF(alpha)
    return color
