"""QiTV's log on disk (2.7.2): the console's lines, kept where a packaged app's would be lost.

A packaged QiTV started from the Finder, the Start menu or a desktop has no terminal, so its
log went nowhere and why it quit could not be read afterwards. `install` also writes the log
to a small rotating file in `log_dir()`, every line with addresses and query strings masked
(QiTV's own lines name none; a library's might), logs an unhandled exception's frames without
its message, and has a native crash dump its threads' stacks to `CRASH_NAME` beside it.
"""

from __future__ import annotations

import faulthandler
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import sys
from typing import IO, Any, Mapping, Optional

from services.redact import mask, safe_traceback

logger = logging.getLogger(__name__)

LOG_NAME = "qitv.log"
CRASH_NAME = "crash.log"
MAX_BYTES = 1_000_000  # then qitv.log.1, .2: a few runs' worth, never more than 3 MB
BACKUPS = 2
FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_crash_file: Optional[IO[str]] = None  # open while QiTV runs: a crash writes to it


def log_dir(
    system: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    """Where each system keeps an app's logs: ~/Library/Logs/QiTV on a Mac,
    %LOCALAPPDATA%\\QiTV\\Logs on Windows, $XDG_STATE_HOME/qitv (~/.local/state) elsewhere; a
    config of its own (QITV_CONFIG_DIR: the smoke run, a test) keeps its log in its "logs"."""
    system = system or platform.system()
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if env.get("QITV_CONFIG_DIR"):
        return Path(env["QITV_CONFIG_DIR"]) / "logs"
    if system == "Darwin":
        return home / "Library" / "Logs" / "QiTV"
    if system == "Windows":
        return Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "QiTV" / "Logs"
    return Path(env.get("XDG_STATE_HOME") or home / ".local" / "state") / "qitv"


class _Masked(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return mask(super().format(record))


def _unhandled(kind: type[BaseException], error: BaseException, frames: Any) -> None:
    """sys.excepthook (PySide6 hands it an exception a slot raised): its frames and type, never
    its message, which may repeat an address."""
    logger.error("Unhandled %s:\n%s", kind.__name__, safe_traceback(error.with_traceback(frames)))


def install(directory: Optional[Path] = None) -> Optional[Path]:
    """Also log to `directory` (`log_dir()` by default): the log file's path, or None when it
    cannot be written there (the console log goes on alone)."""
    global _crash_file
    directory = log_dir() if directory is None else directory
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / LOG_NAME, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
        )
    except OSError as error:
        logger.warning("The log file could not be opened (%s).", type(error).__name__)
        return None
    handler.setFormatter(_Masked(FORMAT))
    logging.getLogger().addHandler(handler)
    sys.excepthook = _unhandled
    try:
        _crash_file = open(directory / CRASH_NAME, "a", encoding="utf-8")
        faulthandler.enable(_crash_file)
    except OSError as error:
        logger.warning("The crash log could not be opened (%s).", type(error).__name__)
    return directory / LOG_NAME


def close() -> None:
    """The crash log closes (the process is ending; tests)."""
    global _crash_file
    if _crash_file is not None:
        faulthandler.disable()
        _crash_file.close()
        _crash_file = None
