"""The files that hold an account's sign-in, readable by this user only (a privacy review).

A Linux home is often readable by everyone (0755), and QiTV's files were written as the umask
left them (0644): config.json (Xtream passwords, a portal's MAC, the phone remote's keys), the
provider cache (index.json and each provider's lists, whose links carry the sign-in),
live_tuning.json and the log could be read by another user of the computer. At each start
`tighten` takes the group's and others' bits off the files there already and off their folders
(the settings folder and the log folder: what is written in them later is private too). It never
adds a bit, and does nothing on Windows (whose folders are the user's own).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import platform
import stat
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_OTHERS = stat.S_IRWXG | stat.S_IRWXO  # group's and others' bits: 0o077
# In the settings folder: the files that hold a sign-in (or what was watched, and how).
SETTINGS_FILES = ("config.json", "live_tuning.json", "recording_schedule.json")
LOG_FILES = ("qitv.log", "qitv.log.1", "qitv.log.2", "crash.log")
PORTABLE_FLAG = "portable.txt"  # the settings live in the program's folder: left as it is


def _private(path: Path) -> bool:
    """Take the group's and others' bits off `path` (which may be gone): True when it changed."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if not mode & _OTHERS:
            return False
        os.chmod(path, mode & ~_OTHERS)
        return True
    except FileNotFoundError:
        return False
    except OSError as error:  # another user's file, a read-only disk: left as it is
        logger.info("Could not make a file private (%s)", type(error).__name__)
        return False


def _provider_files(provider_dir: Path) -> Iterable[Path]:
    try:
        return [provider_dir / name for name in os.listdir(provider_dir) if name.endswith(".json")]
    except OSError:
        return []


def tighten(config_dir: str | os.PathLike, log_dir: Optional[Path], system: str = "") -> int:
    """Make the sign-in's files and their folders this user's only; how many changed. A few
    calls on files of the user's own disk: fine on the GUI thread at startup."""
    if (system or platform.system()) == "Windows":
        return 0
    settings = Path(config_dir)
    provider_dir = settings / "cache" / "provider"
    folders = [provider_dir]
    if not (settings / PORTABLE_FLAG).exists():
        folders.insert(0, settings)
    files = [settings / name for name in SETTINGS_FILES]
    files += _provider_files(provider_dir)
    if log_dir is not None:
        folders.append(Path(log_dir))
        files += [Path(log_dir) / name for name in LOG_FILES]
    changed = sum(_private(path) for path in [*folders, *files])
    if changed:
        logger.info("Made %d of QiTV's files and folders readable by this user only.", changed)
    return changed
