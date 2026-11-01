"""Open a web page or a folder with the system's own program: the browser, the file manager.

A frozen Linux build runs with LD_LIBRARY_PATH pointing into its bundle, and a program
started from it (xdg-open, then the browser or the file manager) would load QiTV's libraries
instead of its own, and may not start. There xdg-open starts with the environment from before
QiTV (`external_players.player_env`, as the players get), and is never waited for. Elsewhere,
and from source, Qt opens it (QDesktopServices).
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
from typing import Optional, Union

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from services.external_players import player_env

logger = logging.getLogger(__name__)


def opens_with_xdg_open(system: Optional[str] = None, frozen: Optional[bool] = None) -> bool:
    """Whether QiTV starts xdg-open itself: a frozen Linux build's."""
    system = system or platform.system()
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    return system == "Linux" and frozen


def open_url(url: Union[str, QUrl]) -> bool:
    """`url` (a web address, or a local file's or folder's) with the system's program; False
    when it could not be opened (a folder that is not there yet)."""
    qurl = url if isinstance(url, QUrl) else QUrl(url)
    if not opens_with_xdg_open():
        return QDesktopServices.openUrl(qurl)
    if qurl.isLocalFile():
        target = qurl.toLocalFile()
        if not os.path.exists(target):
            return False
    else:
        target = qurl.toString()
    try:
        subprocess.Popen(
            ["xdg-open", target],
            env=player_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:  # no xdg-open: Qt finds the desktop's own way
        logger.warning("xdg-open did not start (%s)", type(e).__name__)
        return QDesktopServices.openUrl(qurl)
    return True


def open_folder(path: str) -> bool:
    """The folder at `path` in the system's file manager; False when it is not there (yet)."""
    return open_url(QUrl.fromLocalFile(path))
