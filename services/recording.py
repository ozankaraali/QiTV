"""Recording the live channel being watched (2.3): where the files go, what they are called,
and what a recording's end says.

The time-shift helper writes the file from its buffer (`services.live.recorder`); the GUI
only names it. A recording goes to the Recordings folder: Settings' (the optional config key
`recordings_dir`; older builds ignore it), else the system's Movies folder (Videos on Windows
and Linux) and "QiTV" in it. A file is named "<channel> <YYYY-MM-DD HH.MM>.ts", with what file
systems refuse in a name replaced. Nothing here touches the disk.
"""

from __future__ import annotations

from datetime import datetime
import os
import re
from typing import Any

from PySide6.QtCore import QStandardPaths

from services.i18n import N_, tr, translate
from services.live.recorder import ENDED, FAILED, LIMIT, MAX_SECONDS, SPACE, STOPPED

APP_FOLDER = "QiTV"
SUFFIX = ".ts"
NAME_MAX = 100  # characters of the channel's name kept in a file name
# Characters Windows, macOS or Linux refuse in a file name, and control characters.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?", re.IGNORECASE)
LOST = "lost"  # the engine's: the helper went before it said how the recording ended

# The texts are kept in English and shown translated (`ended_text`).
# i18n: a toast: a recording of the channel was saved as this file
SAVED = N_("Recording saved: {name}")
# i18n: a toast: the recording reached its time limit and was saved as this file
LIMIT_TEXT = N_("Recording stopped after {hours} hours. Saved: {name}")
# i18n: a toast: the disk is nearly full, so the recording stopped; it was saved as this file
SPACE_TEXT = N_("Recording stopped: the disk is almost full. Saved: {name}")
# i18n: a toast: the disk is nearly full, so nothing could be recorded
NO_SPACE_TEXT = N_("Can't record: the disk is almost full")
# i18n: a toast: writing the recording's file failed; what was written is saved as this file
FAILED_TEXT = N_("Recording stopped: the file couldn't be written. Saved: {name}")
# i18n: a toast: the Recordings folder (Settings) cannot be written to
NOT_STARTED = N_("Can't record: the Recordings folder can't be written to")
# i18n: a toast: the recording ended unexpectedly (the time-shift buffer stopped)
LOST_TEXT = N_("Recording stopped unexpectedly")
# i18n: a message: the Recordings folder could not be opened (the first recording makes it)
NO_RECORDINGS = N_("There are no recordings yet")


def default_folder() -> str:
    """The system's Movies (macOS) or Videos (Windows, Linux) folder, and QiTV in it."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
    if not base:
        base = os.path.join(os.path.expanduser("~"), "Videos")
    return os.path.join(base, APP_FOLDER)


def folder(config: Any) -> str:
    """Where recordings go: Settings' folder, else `default_folder()`."""
    chosen = getattr(config, "recordings_dir", "")
    return chosen if isinstance(chosen, str) and chosen.strip() else default_folder()


def safe_name(text: str) -> str:
    """`text` as a file name: what file systems refuse is replaced by "_", spaces and dots at
    its ends go, Windows' reserved names get a "_" first, and it is at most NAME_MAX long."""
    name = _UNSAFE.sub("_", text)
    name = " ".join(name.split())[:NAME_MAX].strip(" .")
    if _RESERVED.fullmatch(name):
        name = "_" + name
    return name or APP_FOLDER


def file_name(channel: str, when: datetime) -> str:
    """ "<channel> <YYYY-MM-DD HH.MM>.ts"."""
    return f"{safe_name(channel)} {when.strftime('%Y-%m-%d %H.%M')}{SUFFIX}"


def path_for(config: Any, channel: str, when: datetime) -> str:
    """The file a recording of `channel` started at `when` asks for (the helper adds " (2)"
    and on when it exists)."""
    return os.path.join(folder(config), file_name(channel, when))


def ended_text(reason: str, path: str, written: int) -> tuple[str, str]:
    """What a recording's end says (translated) and the toast's kind ("info" or "error")."""
    name = os.path.basename(path)
    if reason in (STOPPED, ENDED):
        if name:
            return translate(SAVED, name=name), "info"
        return translate(NOT_STARTED), "error"  # nothing was written
    if reason == LIMIT:
        return translate(LIMIT_TEXT, hours=round(MAX_SECONDS / 3600), name=name), "info"
    if reason == SPACE:
        if name:
            return translate(SPACE_TEXT, name=name), "error"
        return translate(NO_SPACE_TEXT), "error"
    if reason == FAILED and name and written > 0:
        return translate(FAILED_TEXT, name=name), "error"
    if reason == LOST:
        return translate(LOST_TEXT), "error"
    return translate(NOT_STARTED), "error"


def elapsed_text(seconds: float) -> str:
    """How long a recording has run, as its indicator shows it: "01:23", from an hour
    "1:02:03"."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    clock = f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
    # i18n: the recording indicator on the player controls, in capitals; short ("REC 01:23")
    return tr("REC {time}", time=clock)
