"""The bottom of the player belongs to the transport's controls while they show (Phase 4).

While the transport shows, the mixer sits just above its controls (else 4u from the bottom),
and while the transport, the mixer or the zap banner shows, a bottom corner's PiP rises above
the transport's controls or the banner's info row, whichever is higher (PipFrame.set_lift,
animated), and settles back once they hide. The host covers the player page, so the overlays'
host coordinates are the PiP frame's page coordinates.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject

from ui.overlays.painting import Units
from ui.pip import MARGIN_UNITS

GAP_U = 1.0  # between the transport's controls (or the banner's row) and the PiP above them
_EVENTS = (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize)


class BottomBand(QObject):
    def __init__(self, transport, mixer, banner, frame, parent: Optional[QObject] = None) -> None:
        """`transport` has controls_top() and bandChanged; `mixer` set_floor(); `banner`
        content_top(); `frame` set_lift() (the window's Transport, Mixer, ZapBanner and
        PipFrame)."""
        super().__init__(parent)
        self._transport = transport
        self._mixer = mixer
        self._banner = banner
        self._frame = frame
        for widget in (transport, mixer, banner):
            widget.installEventFilter(self)
        transport.bandChanged.connect(self.update)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in _EVENTS:
            self.update()
        return False

    def update(self) -> None:
        transport, mixer, banner = self._transport, self._mixer, self._banner
        shown = not transport.isHidden()
        top = transport.controls_top()
        mixer.set_floor(top if shown else None)
        lift = 0
        if shown or not mixer.isHidden():
            lift = self._lift_above(transport, top)
        if not banner.isHidden():
            lift = max(lift, self._lift_above(banner, banner.content_top()))
        self._frame.set_lift(lift)

    @staticmethod
    def _lift_above(overlay, top: int) -> int:
        """How far a bottom corner's PiP rises to end GAP_U above `top` (`overlay` reaches the
        host's bottom, so its width and bottom are the player's)."""
        un = Units(overlay.width())
        bottom: int = overlay.geometry().bottom() + 1 - un.px(MARGIN_UNITS)  # the PiP's
        return max(0, bottom - (top - un.px(GAP_U)))
