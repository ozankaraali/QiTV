"""An episode's folder, listed on a thread of its own (spec §8).

A file opened in QiTV whose name is an episode has its folder listed as it opens, off the GUI
thread (a folder on a slow or unreachable network share takes seconds, or fails): `listed`
(the listing's number, its video files as (name, size)) comes on the GUI thread as plain data.
The thread creates no Qt objects and starts no timers.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal

from services.local_episodes import list_folder

logger = logging.getLogger(__name__)


class FolderListing(QObject):
    """Lists a folder on a daemon thread, as TVWindow's file check looks at a file."""

    listed = Signal(int, object)  # the listing's number, [(name, size or None)]

    def start(self, generation: int, folder: str) -> None:
        threading.Thread(
            target=self._list, args=(generation, folder), name="qitv-episodes", daemon=True
        ).start()

    def _list(self, generation: int, folder: str) -> None:
        try:
            listing = list_folder(folder)
        except Exception as error:  # list_folder never raises; never unanswered all the same
            logger.warning("An episode's folder could not be listed (%s)", type(error).__name__)
            listing = []
        try:
            self.listed.emit(generation, listing)
        except RuntimeError:
            pass  # QiTV closed meanwhile
