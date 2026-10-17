"""The EPG guide's behaviour over live TV (spec §5.1): what it opens on and the keys it takes.

The guide lists the zap group's channels (every live channel when nothing was tuned yet) on
the channel tuned last; in all providers mode, when the drawer shows another live group, that
group's channels (any provider's) instead. While it is open it takes the keys:
- ↑/↓, PgUp/PgDn and Ch± move the highlight; ←/→ move the time window;
- digits jump to a channel number (the fast way through thousands of channels), Enter tunes
  the highlighted channel and closes the guide;
- G and Esc close it. A request for the drawer (the View menu, a click) or for the previous
  channel (the Playback menu) closes it and then acts as usual; the media keys do nothing,
  as with the drawer open. Playback, volume, fullscreen and favorites keys are not its own.
Its header's buttons do the same with the mouse: ‹ › and × act in the guide itself, ⌕ and ⛶
go to the shell (TVWindow, TVController).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.catalog import ALL_GROUP_ID
from services.zapping import ZapContext
from ui.keymap import Action
from ui.overlays.guide import Guide

logger = logging.getLogger(__name__)

DIGITS_MS = 1200  # a pause this long ends a channel number, as for number entry
MAX_DIGITS = 4
_CLOSERS = (Action.OPEN_DRAWER, Action.PREVIOUS)
_IGNORED = (Action.MEDIA_NEXT, Action.MEDIA_PREV)


class GuideController(QObject):
    tuneRequested = Signal(object, str, object)  # channel, group key, the group's channels

    def __init__(
        self, guide: Guide, *, digits_ms: int = DIGITS_MS, parent: Optional[QObject] = None
    ) -> None:
        super().__init__(parent)
        self.guide = guide
        self._group = ""
        self._channels: list[dict] = []
        self._digits = ""
        self._digit_timer = QTimer(self, singleShot=True, interval=digits_ms)
        self._digit_timer.timeout.connect(self._end_number)
        guide.tuneRequested.connect(self._on_tune)

    def is_open(self) -> bool:
        return bool(self.guide.isVisible())

    def open(
        self,
        context: ZapContext,
        current: Optional[dict],
        all_live: Callable[[], list[dict]],
    ) -> bool:
        """Open on `context`'s channels at `current`; False when there is nothing to list."""
        group, channels = context.group_key, list(context.channels)
        if not channels:
            group, channels = ALL_GROUP_ID, list(all_live())
        if not channels:
            return False
        self._group, self._channels = group, channels
        self._end_number()
        index = next((i for i, c in enumerate(channels) if ZapContext.same(c, current)), 0)
        self.guide.open_guide(channels, index)
        return True

    def close(self) -> None:
        self._end_number()
        self.guide.close_guide()

    def invalidate(self) -> None:
        """New programme data: the open guide looks its rows up again."""
        if self.is_open():
            self.guide.invalidate()

    def current_channel(self) -> Optional[dict]:
        return self.guide.current_channel() if self.is_open() else None

    def visible_channels(self) -> list[dict]:
        """The channels on screen while the guide is open (any provider's)."""
        return self.guide.visible_channels() if self.is_open() else []

    def handle(self, action: Action, payload: str = "") -> bool:
        """Act on a key while the guide is open; False when the usual handling follows."""
        guide = self.guide
        if action is not Action.DIGIT:
            self._end_number()
        moves: dict[Action, tuple[Callable[[int], None], int]] = {
            Action.NAV_UP: (guide.move_by, -1),
            Action.NAV_DOWN: (guide.move_by, 1),
            Action.CH_PREV: (guide.move_by, -1),
            Action.CH_NEXT: (guide.move_by, 1),
            Action.NAV_PAGE_UP: (guide.page, -1),
            Action.NAV_PAGE_DOWN: (guide.page, 1),
            Action.NAV_LEFT: (guide.shift, -1),
            Action.NAV_RIGHT: (guide.shift, 1),
        }
        if action in moves:
            move, delta = moves[action]
            move(delta)
        elif action is Action.DIGIT:
            self._digit(payload)
        elif action is Action.NAV_ACTIVATE:
            guide.activate()
        elif action is Action.ESCAPE:
            guide.escape()
        elif action is Action.GUIDE:
            self.close()
        elif action in _CLOSERS:
            self.close()
            return False
        elif action not in _IGNORED:
            return False
        return True

    # ---- channel numbers ----------------------------------------------------------------------

    def _digit(self, digit: str) -> None:
        if not digit.isdigit() or len(self._digits) >= MAX_DIGITS:
            return
        self._digits += digit
        self._digit_timer.start()
        channel = ZapContext(self._group, self._channels).by_number(self._digits)
        if channel is None:
            return  # e.g. "99" of "999": the highlight stays on 99
        target = next((i for i, c in enumerate(self._channels) if c is channel), -1)
        current = self.guide.current_channel()
        here = next((i for i, c in enumerate(self._channels) if c is current), -1)
        if target >= 0 and here >= 0 and target != here:
            self.guide.move_by(target - here)

    def _end_number(self) -> None:
        self._digit_timer.stop()
        self._digits = ""

    def _on_tune(self, channel: dict) -> None:
        group, channels = self._group, self._channels
        self.close()
        self.tuneRequested.emit(channel, group, channels)
