"""The bottom of the player belongs to the transport's controls while they show (Phase 4).

While the transport shows, the mixer sits just above its controls (else 4u from the bottom),
and while the transport or the mixer shows, a bottom corner's PiP rises above those controls
(PipFrame.set_lift, animated) and settles back once both hide. The host covers the player
page, so the transport's host coordinates are the PiP frame's page coordinates.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject

from ui.overlays.painting import Units
from ui.pip import MARGIN_UNITS

GAP_U = 1.0  # between the transport's controls and the PiP risen above them
_EVENTS = (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize)


class BottomBand(QObject):
    def __init__(self, transport, mixer, frame, parent: Optional[QObject] = None) -> None:
        """`transport` has controls_top() and bandChanged; `mixer` set_floor(); `frame`
        set_lift() (the window's Transport, Mixer and PipFrame)."""
        super().__init__(parent)
        self._transport = transport
        self._mixer = mixer
        self._frame = frame
        for widget in (transport, mixer):
            widget.installEventFilter(self)
        transport.bandChanged.connect(self.update)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in _EVENTS:
            self.update()
        return False

    def update(self) -> None:
        transport, mixer = self._transport, self._mixer
        shown = not transport.isHidden()
        top = transport.controls_top()
        mixer.set_floor(top if shown else None)
        lift = 0
        if shown or not mixer.isHidden():
            un = Units(transport.width())
            bottom = transport.geometry().bottom() + 1 - un.px(MARGIN_UNITS)  # the PiP's
            lift = max(0, bottom - (top - un.px(GAP_U)))
        self._frame.set_lift(lift)
