"""Writing a file whole or not at all (no Qt), and sweeping the temporary files of writes that
were killed midway."""

from __future__ import annotations

from contextlib import contextmanager
import logging
import os
import re
import tempfile
import threading
import time
from typing import IO, Callable, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

STALE_AFTER_S = 24 * 60 * 60  # a temporary file untouched this long belongs to no write
# A temporary file as atomic_write (and the stores' own writers) name them: ".<name>-" and
# tempfile.mkstemp's 8 random characters, ".tmp".
_TEMPORARY = re.compile(r"^\..+-[a-z0-9_]{8}\.tmp$")


@contextmanager
def atomic_write(
    path: str, replace: Optional[Callable[[str, str], object]] = None
) -> Iterator[IO[bytes]]:
    """A binary file to write `path` through: a temporary file beside it (".<name>-….tmp"),
    swapped in once written by `replace(temporary, path)` (os.replace by default; config.json
    retries a locked file, a cache skips the swap when a newer write is queued). When the
    write fails, or `replace` leaves the temporary file, it is removed and `path` stays as it
    was: a failed or interrupted write never truncates it. A process killed midway leaves the
    temporary file: sweep_stale_temporaries removes it a day later."""
    stem = os.path.splitext(os.path.basename(path))[0]
    fd, temporary = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".", prefix=f".{stem}-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            yield f
        (replace or os.replace)(temporary, path)
    finally:
        try:
            os.remove(temporary)  # left by a failure, or by `replace`
        except OSError:  # swapped in (or, on Windows, still open somewhere)
            pass


def sweep_stale_temporaries(
    directories: Iterable[str], older_than: float = STALE_AFTER_S, now: Optional[float] = None
) -> int:
    """Remove the temporary files (".<name>-xxxxxxxx.tmp") that writes killed midway left in
    `directories`, once unchanged for `older_than` seconds; how many went. A missing or
    unreadable directory, and a file that cannot be removed, are passed over."""
    now = time.time() if now is None else now
    removed = 0
    for directory in directories:
        try:
            with os.scandir(directory) as entries:
                found = [entry for entry in entries if _TEMPORARY.match(entry.name)]
        except OSError:
            continue
        for entry in found:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if now - entry.stat(follow_symlinks=False).st_mtime < older_than:
                    continue  # perhaps a write under way
                os.remove(entry.path)
                removed += 1
            except OSError:
                continue
    if removed:
        logger.info("Removed %d temporary file(s) left by interrupted writes", removed)
    return removed


def sweep_in_background(directories: Iterable[str]) -> threading.Thread:
    """sweep_stale_temporaries on a thread of its own (at start-up: the GUI thread never
    waits for the disk)."""
    thread = threading.Thread(
        target=sweep_stale_temporaries,
        args=(list(directories),),
        name="temporary-sweep",
        daemon=True,
    )
    thread.start()
    return thread
