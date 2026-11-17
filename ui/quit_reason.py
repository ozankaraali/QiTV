"""Why QiTV's window closes, for its log (2.8).

On macOS the Dock's Quit, another app asking QiTV to quit and logging out close the window the
way QiTV's own Quit does (not as a spontaneous close), so the log could not tell them apart.
Each is marked as it arrives: QiTV's Quit action notes whether QiTV was the active app (its menu
and ⌘Q only reach QiTV then), and the application notes a quit request the system sends it.

The application also passes on the files QiTV is asked to open from outside: macOS's open event
(a double-click in Finder gives no argument) and the files another start hands over. Those that
come before the window can take them are kept for it.
"""

from __future__ import annotations

from collections.abc import Sequence
import logging
from typing import Any, Optional

from PySide6.QtCore import QEvent, Signal
from PySide6.QtGui import QFileOpenEvent
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

MENU = "menu"  # QiTV's Quit, chosen in QiTV (its menu or the shortcut)
SYSTEM = "system"  # the system asked: the Dock, another app, logging out


def describe(spontaneous: bool, asked: Optional[str]) -> str:
    """The log's words for why the window closes."""
    if asked == SYSTEM:
        return "the system asked QiTV to quit: the Dock, another app or logging out"
    if asked == MENU:
        return "QiTV's Quit, chosen in QiTV"
    if spontaneous:
        return "the window's close button or the system"
    return "QiTV closed its window"


def asked_by_menu(active: bool) -> str:
    """QiTV's Quit action ran: chosen in QiTV when it was the active app, else a request from
    outside that reached the action."""
    return MENU if active else SYSTEM


def is_system_quit(event: Any) -> bool:
    return bool(event.type() == QEvent.Type.Quit and event.spontaneous())


class QiTVApplication(QApplication):
    """QiTV's application: notes a quit request the system sends it (`quit_asked`), and passes
    on the files it is asked to open (`filesOpened`); those that come before the window takes
    them (`take_opened`) are kept for it too."""

    quit_asked: Optional[str] = None
    filesOpened = Signal(list)

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._kept: Optional[list[str]] = []  # None once the window took them
        self._told: set[str] = set()  # when macOS's open event came, logged once each

    def event(self, event: QEvent) -> bool:
        if self.quit_asked is None and is_system_quit(event):
            self.quit_asked = SYSTEM
        if isinstance(event, QFileOpenEvent) and event.file():
            when = "before" if self._kept is not None else "after"
            if when not in self._told:  # does a launch's file come before the window shows?
                self._told.add(when)
                logger.info("FileOpen arrived %s the window.", when)
            self.add_opened([event.file()])
            return True
        return super().event(event)

    def add_opened(self, paths: Sequence[str]) -> None:
        """Files to open (macOS's open event, another start's hand-over): kept until the window
        takes them, passed on after (never both: they would open twice)."""
        files = [path for path in paths if path]
        if not files:
            return
        if self._kept is not None:
            self._kept.extend(files)
            return
        self.filesOpened.emit(files)

    def kept_opened(self) -> list[str]:
        """The files kept so far, still kept for the window: a second start hands them over
        with its own."""
        return list(self._kept or [])

    def take_opened(self) -> list[str]:
        """The files that came before the window, once; from now on they are only passed on."""
        kept, self._kept = self._kept or [], None
        return kept
