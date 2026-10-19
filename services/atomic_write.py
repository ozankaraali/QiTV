"""Writing a file whole or not at all (no Qt)."""

from __future__ import annotations

from contextlib import contextmanager
import os
import tempfile
from typing import IO, Callable, Iterator, Optional


@contextmanager
def atomic_write(
    path: str, replace: Optional[Callable[[str, str], object]] = None
) -> Iterator[IO[bytes]]:
    """A binary file to write `path` through: a temporary file beside it (".<name>-….tmp"),
    swapped in once written by `replace(temporary, path)` (os.replace by default; config.json
    retries a locked file, a cache skips the swap when a newer write is queued). When the
    write fails, or `replace` leaves the temporary file, it is removed and `path` stays as it
    was: a failed or interrupted write never truncates it."""
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
