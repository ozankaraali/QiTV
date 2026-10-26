"""Why QiTV's window closes, for its log (2.8).

On macOS the Dock's Quit, another app asking QiTV to quit and logging out close the window the
way QiTV's own Quit does (not as a spontaneous close), so the log could not tell them apart.
Each is marked as it arrives: QiTV's Quit action notes whether QiTV was the active app (its menu
and ⌘Q only reach QiTV then), and the application notes a quit request the system sends it.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

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
    return event.type() == QEvent.Type.Quit and event.spontaneous()


class QiTVApplication(QApplication):
    """QiTV's application: notes a quit request the system sends it (`quit_asked`)."""

    quit_asked: Optional[str] = None

    def event(self, event: QEvent) -> bool:
        if self.quit_asked is None and is_system_quit(event):
            self.quit_asked = SYSTEM
        return super().event(event)
