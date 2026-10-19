"""Record a live session from its time-shift ring to a file, in the helper process (2.3).

`Recorder` follows the session's ring as the loopback server's readers do (`server._Handler.
_stream`): segment `seq` from its PAT/PMT header, then every later segment without its header,
so the file holds the stream's own bytes (MPEG-TS, its timestamps as the ring stores them),
following the open segment as it grows. It reads with the lease-free `Ring.read_at` and pins
nothing, so the session's writer never waits for it; and it opens no connection: every byte it
writes came through the session's one upstream connection.

A recorder that falls behind the ring's eviction (a slow disk, or a start the ring dropped
before it got there) goes on at the oldest segment still held, with that segment's header; a
ring the session replaced (its RAM fallback after a disk failure) is followed the same way.
It ends once, with one reason:
- "stopped": `stop()` (the user, a channel change, the session's close, the helper's
  shutdown): what the ring holds up to its live edge is written first, as long as the ring
  stays open; a ring closed meanwhile ends it where it got to;
- "ended": the session's input ended and everything it buffered was written;
- "limit": `max_s` seconds passed since the start;
- "space": the disk holding the file had less than `min_free` bytes free, or was full;
- "failed": the file could not be created or written (or the session had no ring).
`on_done(recorder)` is then called once from the recorder's thread, with its `reason`, its
`path` (the file written: a unique name next to the one asked for when that one exists; ""
when none was made) and `written`, the file's size. An empty file is removed. Nothing here
logs a path or the stream's bytes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import errno
import logging
import os
from pathlib import Path
import shutil
import threading
import time
from typing import BinaryIO

from services.live.ring import Ring, RingError

_LOG = logging.getLogger(__name__)
_CHUNK = 256 * 1024
_WAIT = 0.5  # a wait for more data is cut this short to look at the caps again
_BATCH_S = 0.2  # at the live edge, what arrives within this is written at once
_SPACE_EVERY = 16 * 1024 * 1024  # the free space is looked at again after this many bytes
_UNIQUE_TRIES = 99
_FULL = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}

STOPPED, ENDED, LIMIT, SPACE, FAILED = "stopped", "ended", "limit", "space", "failed"
REASONS = (STOPPED, ENDED, LIMIT, SPACE, FAILED)
# A recording stops after this long and once the disk holding it has less than this free.
MAX_SECONDS = 4 * 60 * 60.0
MIN_FREE_BYTES = 2 * 1024**3


def _free_bytes(directory: Path) -> int:
    return shutil.disk_usage(directory).free


def _open_new(path: Path) -> BinaryIO:
    """A new file at `path` (never one that exists), written unbuffered: what was written is
    the OS's, also when the helper exits at once (`os._exit`)."""
    return open(path, "xb", buffering=0)


def candidates(path: Path) -> Iterator[Path]:
    """`path`, then "<stem> (2)<suffix>" and on: a recording never replaces a file."""
    yield path
    for number in range(2, _UNIQUE_TRIES + 2):
        yield path.with_name(f"{path.stem} ({number}){path.suffix}")


class Recorder:
    """Writes one session's ring to a file on a thread of its own (see the module docstring)."""

    def __init__(
        self,
        ring_of: Callable[[], Ring | None],
        is_ended: Callable[[], bool],
        path: str,
        seq: int,
        on_done: Callable[[Recorder], None],
        *,
        max_s: float = MAX_SECONDS,
        min_free: int = MIN_FREE_BYTES,
        clock: Callable[[], float] = time.monotonic,
        free_bytes: Callable[[Path], int] = _free_bytes,
        open_new: Callable[[Path], BinaryIO] = _open_new,
    ) -> None:
        """`ring_of()`: the session's ring now (None once it is gone); `is_ended()`: its input
        ended (nothing more is appended)."""
        self._ring_of = ring_of
        self._is_ended = is_ended
        self._asked = Path(path)
        self._seq = seq
        self._on_done = on_done
        self._max_s = max_s
        self._min_free = min_free
        self._clock = clock
        self._free_bytes = free_bytes
        self._open_new = open_new
        self._stop = threading.Event()
        self._file: BinaryIO | None = None
        self.path = ""  # the file being written, once made
        self.written = 0
        self.reason = ""  # why it ended, once it has (one of REASONS)
        self.name = ""  # what its owner calls it (the parent's recording id)
        self._checked_at = 0  # `written` when the free space was last looked at
        self._ring: Ring | None = None  # the ring being followed (stop() wakes its waiters)
        self.thread = threading.Thread(target=self._run, name="qitv-live-record", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        """Finish at the live edge (see the module docstring); never blocks."""
        self._stop.set()
        ring = self._ring
        if ring is not None:
            with ring.changed:
                ring.changed.notify_all()

    def join(self, timeout: float) -> bool:
        """Wait up to `timeout` for the recorder to finish; True once it has."""
        if self.thread.ident is not None:
            self.thread.join(timeout)
        return not self.thread.is_alive()

    # ---- the recorder's thread ----------------------------------------------------------
    def _run(self) -> None:
        reason = FAILED
        try:
            reason = self._record()
        except OSError as error:
            reason = SPACE if error.errno in _FULL else FAILED
            _LOG.warning("A recording could not be written (%s).", type(error).__name__)
        except RingError:
            _LOG.warning("A recording could not read the buffer.")  # a disk buffer's failure
        except Exception as error:  # a defect: the file is kept, the session goes on
            _LOG.warning("A recording failed unexpectedly (%s).", type(error).__name__)
        finally:
            self._close()
            self.reason = reason
            try:
                self._on_done(self)
            except Exception as error:
                _LOG.warning("A recording's end was not reported (%s).", type(error).__name__)

    def _record(self) -> str:
        ring = self._ring_of()
        if ring is None:
            return FAILED
        self._create()
        if self._short_of_space():
            return SPACE
        return self._follow(ring, self._seq)

    def _create(self) -> None:
        self._asked.parent.mkdir(parents=True, exist_ok=True)
        for path in candidates(self._asked):
            try:
                self._file = self._open_new(path)
            except FileExistsError:
                continue
            self.path = str(path)
            return
        raise FileExistsError(errno.EEXIST, "every name is taken")

    def _follow(self, ring: Ring, number: int) -> str:
        """Write from segment `number` on; the reason it ended."""
        started = self._clock()
        offset = 0
        self._ring = ring
        while True:
            if self._clock() - started >= self._max_s:
                return LIMIT
            with ring.changed:
                before = _snapshot(ring)
            ended = self._is_ended()  # before the read: an ended session appends nothing more
            stopping = self._stop.is_set()
            result = ring.read_at(number, offset, _CHUNK)
            if result is None:
                # Evicted (a slow disk, a start the ring dropped), not opened yet, or closed.
                status = ring.wait_for_seq(number, 0.0)
                if status == "evicted":
                    number, offset = self._oldest(ring, number), 0  # a gap: with its header
                elif status == "timeout":
                    if ended or stopping:
                        return ENDED if ended else STOPPED
                    self._wait(ring, before)
                else:  # closed: the session's next ring, or the end
                    following = self._next_ring(ring)
                    if following is None:
                        return STOPPED
                    # A segment begun in the old ring is not written twice.
                    at_least = number + 1 if offset else number
                    ring, number, offset = following, self._oldest(following, at_least), 0
                    self._ring = ring
                continue
            data, complete, _header = result
            if data:
                self._write(data)
                offset += len(data)
                if self._short_of_space():
                    return SPACE
                continue
            if not complete:
                if ended or stopping:
                    return ENDED if ended else STOPPED
                self._wait(ring, before)
                continue
            status = ring.wait_for_seq(number + 1, 0.0)
            if status == "open":
                following_header = ring.read_at(number + 1, 0, 0)  # its header's length only
                if following_header is not None:
                    number, offset = number + 1, following_header[2]
            elif status == "evicted":
                number, offset = self._oldest(ring, number + 1), 0
            elif status == "timeout":
                if ended or stopping:
                    return ENDED if ended else STOPPED
                self._wait(ring, before)
            else:
                following = self._next_ring(ring)
                if following is None:
                    return STOPPED
                ring, number, offset = following, self._oldest(following, number + 1), 0
                self._ring = ring

    def _oldest(self, ring: Ring, at_least: int) -> int:
        """Where to go on after a gap: the oldest segment `ring` holds, never before
        `at_least` (`at_least` itself while it holds none: the next read waits for it)."""
        oldest = ring.oldest_seq
        if oldest is None:
            return at_least
        if oldest > at_least:
            _LOG.info("A recording skipped what the buffer no longer held.")
        return max(oldest, at_least)

    def _next_ring(self, closed: Ring) -> Ring | None:
        """The session's ring after `closed` (its RAM fallback), or None (it was torn down)."""
        ring = self._ring_of()
        if ring is None or ring is closed or ring.closed:
            return None
        return ring

    def _wait(self, ring: Ring, before: _Snapshot) -> None:
        """Up to `_WAIT` for the ring to change from `before`, close, or the recorder to stop;
        after a change, `_BATCH_S` more, so that a few writes take what many small appends
        brought (a stop cuts that short)."""
        stop = self._stop
        with ring.changed:
            changed = ring.changed.wait_for(
                lambda: stop.is_set() or ring.closed or _snapshot(ring) != before, _WAIT
            )
        if changed:
            stop.wait(_BATCH_S)

    def _write(self, data: bytes) -> None:
        handle = self._file
        assert handle is not None
        view = memoryview(data)
        while view:
            count = handle.write(view)
            if not count:
                raise OSError(errno.EIO, "short write")
            view = view[count:]
            self.written += count

    def _short_of_space(self) -> bool:
        """The disk holding the file has less than `min_free` free (looked at every
        `_SPACE_EVERY` bytes, and at the start)."""
        if self.written and self.written - self._checked_at < _SPACE_EVERY:
            return False
        self._checked_at = self.written
        try:
            return self._free_bytes(self._asked.parent) < self._min_free
        except OSError:
            return False  # not known: the writes say when it is full

    def _close(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            handle.close()
        except OSError as error:
            _LOG.warning("A recording was not closed cleanly (%s).", type(error).__name__)
        if not self.written and self.path:
            try:
                os.remove(self.path)  # nothing was recorded: no empty file is left
            except OSError:
                pass
            self.path = ""


_Snapshot = tuple[int, int | None, int | None]


def _snapshot(ring: Ring) -> _Snapshot:
    """Differs after every append, eviction, completion or new segment (as the server's)."""
    return ring.bytes, ring.oldest_seq, ring.open_seq
