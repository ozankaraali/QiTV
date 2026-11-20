"""A question with a progress bar (2.13.1): an update's download, in QiTV's own card.

The ChoiceCard (ui/overlays/settings_card.py) with, under its words, a bar the card's width: a
muted line, filled in the accent colour as far as the work got (a question asked without
`progress` has none). `set_progress` moves it and changes the words under the title ("12.5 /
80.0 MB") while it shows, saying nothing to assistive technology each time (the title and the
first words were said as it showed). The window's cards are ProgressCards (ui.window_cards).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF
from PySide6.QtGui import QFont, QPainter
from PySide6.QtWidgets import QWidget

from ui.overlays.painting import Units, fill_round
from ui.overlays.settings_card import BODY, MIDDLE, PANEL, TITLE, Button, ChoiceCard
from ui.theme import PALETTE, qcolor

BAR = ("bar", "")  # its place on the card (control_rect(*BAR))
_BAR_GAP_U = 1.0  # under the words
_BAR_H_U = 0.5
_TRACK_ALPHA = 0.22  # the line it fills (as the RangeCard's track)
_Rects = dict[tuple[str, str], QRectF]


def _clamped(fraction: Optional[float]) -> Optional[float]:
    return None if fraction is None else max(0.0, min(1.0, float(fraction)))


class ProgressCard(ChoiceCard):
    """A ChoiceCard that may show how far a piece of work got."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._progress: Optional[float] = None

    def ask(
        self,
        title: str,
        body: str,
        buttons: list[Button],
        default: str,
        alternates: Optional[list[Button]] = None,
        progress: Optional[float] = None,
    ) -> None:
        """ChoiceCard.ask; with `progress` (0 to 1) the bar under the words."""
        self._progress = _clamped(progress)
        super().ask(title, body, buttons, default, alternates)

    def progress(self) -> Optional[float]:
        """How far the bar is filled (None: the card has none)."""
        return self._progress

    def set_progress(self, fraction: float, body: str) -> None:
        """The bar at `fraction` (0 to 1), `body` its words, while it shows."""
        self._progress = _clamped(fraction)
        self._body = body
        self.update()

    def _place(
        self, un: Units, fonts: dict[str, QFont], width: float, squeeze: float
    ) -> tuple[_Rects, float]:
        """The question's places, the bar put under its words and everything under them moved
        down by its height."""
        rects, footer = super()._place(un, fonts, width, squeeze)
        if self._progress is None:
            return rects, footer
        under = rects[BODY] if BODY in rects else rects[TITLE]
        left, right = rects[TITLE].left(), rects[TITLE].right()
        top = under.bottom() + un.px(_BAR_GAP_U) * squeeze
        height = max(2.0, float(un.px(_BAR_H_U)))
        edge, shift = under.bottom(), top + height - under.bottom()
        for name, rect in list(rects.items()):
            if name not in (MIDDLE, PANEL) and rect.top() >= edge - 0.5:
                rects[name] = rect.translated(0, shift)
        rects[BAR] = QRectF(left, top, max(1.0, right - left), height)
        for name in (MIDDLE, PANEL):
            rect = rects[name]
            rects[name] = QRectF(rect.left(), rect.top(), rect.width(), rect.height() + shift)
        return rects, footer + shift

    def _paint_middle(self, p: QPainter, un: Units, fonts: dict[str, QFont], rects: _Rects) -> None:
        super()._paint_middle(p, un, fonts, rects)
        bar = rects.get(BAR)
        if bar is None or self._progress is None:
            return
        radius = bar.height() / 2
        fill_round(p, bar, qcolor(PALETTE.text, _TRACK_ALPHA), radius)
        if self._progress > 0:
            done = max(bar.height(), bar.width() * self._progress)
            filled = QRectF(bar.left(), bar.top(), done, bar.height())
            fill_round(p, filled, qcolor(PALETTE.accent), radius)
