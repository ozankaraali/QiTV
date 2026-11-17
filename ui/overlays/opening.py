"""While a file opened in QiTV opens, QiTV's mark draws itself in the middle of the player page,
on the black where the file's picture will be (ui/opening_mark.py), and opens out of the page
once the picture shows.

The window shows it as its router plays a media file (TVWindow.open_paths), unless a picture
shows already: a film handed over while another one shows its picture opens with none. It
opens out (OpeningMark.finish; it has no fade of its own) at:
- the file's picture (its session's pictureShown);
- a song's first `playing`, or a film's played on a cast device: neither has a picture here;
- the end of the opening: the file failed, ended, gave way to another title or was stopped
  (TVController, through the window's opening_over);
- OPENING_TIMEOUT_MS after it showed, whatever came.

It takes no click (clicks reach the player under it, as on the black), no key and no focus.
The mark is MARK_PX wide in the middle of a STAGE_PX square, the room its light takes, and that
square stays in the middle of the page as the window changes size.
"""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from services.i18n import N_
from services.playback import PlaybackState
from ui.opening_mark import STAGE_PX, OpeningMark
from ui.overlays.base import Overlay

OPENING_TIMEOUT_MS = 20_000  # the mark goes after this long, whatever came of the file
APPEAR_MS = 300  # a mark let go once the splash's has gone fades in this long
_PLAYING = PlaybackState.PLAYING.value


def _passive(widget: QWidget) -> None:
    """Clicks go through `widget` to the player, and it never takes the focus."""
    widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)


class OpeningOverlay(Overlay):
    """The mark over the player page while a file opens (module docstring): open_for() shows
    it for a session's file, close_opening() opens it out of the page."""

    accessible_name = N_("Loading…")

    def __init__(
        self, parent: Optional[QWidget] = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        _passive(self)
        self.resize(STAGE_PX, STAGE_PX)
        self._mark = OpeningMark(self, clock=clock)
        _passive(self._mark)
        self._mark.setGeometry(self.rect())
        self._timeout = QTimer(self, singleShot=True, interval=OPENING_TIMEOUT_MS)
        self._timeout.timeout.connect(self.close_opening)
        self._session: Any = None  # the Session the file opens in (services.playback)
        self._url = ""  # the file's address there
        # What the session played as the file was given it (it opens once that let go): a
        # picture of it is not the file's. None: the file's address already.
        self._before: Optional[str] = None
        self._sound_only = False
        self._opening = False  # it draws for a file still opening
        self._finishing = False  # it opens out of the page (OpeningMark.finish)
        self._held = False  # its mark draws unseen (hold_mark)
        # Where its loop begins (ui/splash_helper.py: the splash's own mark's, so that the two
        # show the same frame); None: as it starts.
        self.loop_origin: Callable[[], Optional[float]] = lambda: None
        # Whether that mark is held (it took over from the bootloader's still: it fills from the
        # still's outline, then stays lit): this one is too, on the same origin, so that the two
        # show the same frame.
        self.loop_held: Callable[[], bool] = lambda: False
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    @property
    def mark(self) -> OpeningMark:
        return self._mark

    def opening(self) -> bool:
        """It draws for a file still opening (not once it opens out)."""
        return self._opening

    def open_for(self, session: Any, url: str, *, sound_only: bool = False) -> None:
        """Draw while `session` opens the file at `url` (its address there): until its picture
        shows, or, `sound_only` (a song, a film on a cast device), until it plays. A mark that
        draws for another file draws on for this one, its OPENING_TIMEOUT_MS from now; one
        opening out draws again once it has."""
        self._listen(session)
        before = session.url
        self._url, self._before = url, (before if before != url else None)
        self._sound_only = sound_only
        self._opening = True
        self._timeout.start()
        if not self._finishing:
            self._start_mark()  # nothing while it draws already
        self._mark.setVisible(not self._held)
        self._layout()
        self.show()

    def hold_mark(self, held: bool, *, fade: bool = False) -> None:
        """The splash's mark stands where this one draws (ui/splash_helper.py): this one draws
        on unseen, on the same loop, until it is let go; one that opens out by then stays
        unseen until its next file. Let go with `fade` (the splash's mark has gone: it opened
        out elsewhere, or ended), it fades in over APPEAR_MS; without (the hand-over's swap:
        the splash's stands on it, the same frame), it shows at once."""
        was_held, self._held = self._held, held
        if held:
            self._mark.hide()
        elif self._opening and was_held:
            self._mark.show()
            if fade:
                self._mark.appear(APPEAR_MS / 1000)

    def close_opening(self) -> None:
        """The mark opens out of the page, then hides; nothing when it does not draw."""
        self._timeout.stop()
        self._listen(None)
        if not self._opening:
            return
        self._opening = False
        if not self._finishing:
            self._finishing = True
            self._mark.finish(self._opened_out)

    def _opened_out(self) -> None:
        self._finishing = False
        if self._opening:
            # a file opened as it opened out: it draws again for that one
            self._start_mark()
        else:
            self.hide()

    def _start_mark(self) -> None:
        origin = self.loop_origin()
        self._mark.start(origin, held=origin is not None and self.loop_held())

    def _listen(self, session: Any) -> None:
        old = self._session
        if session is old:
            return
        self._session = session
        if old is not None and isValid(old):
            old.pictureShown.disconnect(self._on_picture)
            old.stateChanged.disconnect(self._on_state)
        if session is not None:
            session.pictureShown.connect(self._on_picture)
            session.stateChanged.connect(self._on_state)

    def _on_picture(self) -> None:
        """A picture shows in the session: the file's, or that of a title chosen after it (a
        recording from the Library); not one of what played before it, still reported."""
        url = self._session.url if self._session is not None else None
        if url == self._url or url != self._before:
            self.close_opening()

    def _on_state(self, state: str) -> None:
        session = self._session
        if self._sound_only and state == _PLAYING and session is not None:
            if session.url == self._url:
                self.close_opening()  # a song has no picture: it plays

    def _layout(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            x = round((parent.width() - STAGE_PX) / 2)
            y = round((parent.height() - STAGE_PX) / 2)
            self.setGeometry(x, y, STAGE_PX, STAGE_PX)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._layout()  # in the middle of the page as the window changes size
        return False
