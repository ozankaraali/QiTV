"""AI captions' downloaded files in Settings › Subtitles & AI captions (2.9): the space they
take, measured off the GUI thread (deleted first, when asked), how Settings says it, and the
question it asks before deleting them."""

from __future__ import annotations

import importlib

from PySide6.QtCore import QObject, Signal

from services.i18n import N_, tr
from ui.captions_texts import size_text

# i18n: asked before Delete; size: as "880 MB"
DELETE_QUESTION = N_(
    "Delete AI captions' speech model ({size})? Turning AI captions on downloads it again."
)


class CaptionsFilesJob(QObject):
    """AI captions' downloaded files, off the GUI thread: their size on disk, after deleting
    them with `delete`. None: this build has no AI captions' files to tell of."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, delete: bool = False) -> None:
        super().__init__()
        self._delete = delete

    def run(self) -> None:
        try:
            models = importlib.import_module("services.captions.models")
        except ImportError:
            self.finished.emit(None)
            return
        if self._delete:
            models.delete_all()
        self.finished.emit(int(models.space_used()))


def captions_space_text(size) -> str:
    """Settings' line on AI captions' downloaded files (""; nothing to tell)."""
    if size is None:
        return ""
    if size <= 0:
        return tr("Nothing downloaded")  # i18n: Settings → AI captions: no speech model yet
    # i18n: Settings → AI captions: the space the speech model takes; size: as "880 MB"
    return tr("Downloaded: {size}", size=size_text(size))
