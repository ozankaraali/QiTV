"""Bounded segment rings for the live time-shift pipeline: RAM (default) and disk.

One writer thread appends keyframe-aligned MPEG-TS segments; several reader threads read
them progressively (including the still-open one) with `read_at`, which pins nothing, and
wait on `changed` for more data. Eviction removes the oldest *completed* segments first;
the open segment is never removed. Error texts are fixed strings.

Kept buffers (2.6): each completed segment notes the time of day it completed (`wall`); one
older than `STALE_S` goes (`retire_before`). Every disk ring of a helper shares the user's
disk amount (`DiskBudget`): over it, the oldest segment of a kept (parked) ring goes first,
then the oldest of any.

A disk ring in a `.qitv-buffer-<32 hex>` folder (v2, its `owner` reading `_KEPT_OWNER`) keeps
an index, `index.jsonl`, one JSON object per line, appended and never rewritten in place
(compacted through `index.jsonl.new` once most of its segment lines name segments gone):
`{"v": 2}` first, then `{"seg": seq, "h": header_len, "n": size, "s": start, "e": end, "d":
discontinuity, "q": start_quality, "w": wall, "j": junction}` per completed segment (the
session's `note_segment`; 2.6.2 adds `"m"`, the HLS media sequence it begins, when it begins
one), `{"part": first_seq, "s": start, "w": wall}` per part,
`{"label": {...}}` and `{"where": {...}}` (the last counts). `close(keep=True)` leaves such a
folder for a later helper (`read_index`); older builds' sweeps match only
`.qitv-timeshift-<32 hex>` folders with the v1 marker, so they never touch it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import BinaryIO

from PySide6.QtCore import QLockFile

_SESSION_PATTERN = re.compile(r"\.qitv-timeshift-[0-9a-f]{32}")
_KEPT_PATTERN = re.compile(r"\.qitv-buffer-[0-9a-f]{32}")  # 2.6: a buffer that may be kept
_MEDIA_PATTERN = re.compile(r"segment-[0-9]+\.ts")
_OWNER = "QiTV temporary time-shift v1\n"
_KEPT_OWNER = "QiTV time-shift buffer v2\n"
_INDEX = "index.jsonl"
_INDEX_NEW = "index.jsonl.new"
_INDEX_NAMES = {_INDEX, _INDEX_NEW}
_RESERVE_BYTES = 64 * 1024 * 1024
_IO_TIMEOUT = 2.0
_MANAGER_LOCK = ".qitv-timeshift-manager.lock"
_Bytes = bytes
_LOG = logging.getLogger(__name__)
_READ_FAILED = "The temporary buffer could not be read."
_STOPPED = "Recording stopped."
_WRITE_FAILED = "The temporary disk could not complete a buffer write."
STALE_S = 24 * 3600  # 2.6: a segment older than this goes (a day-old timeline is no use)
_wall_time = time.time  # a segment's time of day; tests replace it


class RingError(Exception):
    """An explanation that never contains provider data or filesystem paths."""


@dataclass
class Segment:
    seq: int
    header_len: int = 0
    size: int = 0
    complete: bool = False
    retired: bool = False
    charge: int = 0
    wall: float = 0.0  # 2.6: Unix time it completed (0.0 while open)
    junction: bool = False  # 2.6: it begins a kept buffer's new run (its header is sent too)


class Ring(ABC):
    """Shared eviction and wakeup logic; subclasses own the storage."""

    _LIMIT_TEXT = "The disk buffer limit cannot fit one keyframe interval or an active read."

    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise RingError("The temporary buffer size must be positive.")
        self._max_bytes = max_bytes
        # Lock order: _disk_lock (storage operations) outside _state_lock (metadata).
        self._disk_lock = threading.Lock()
        self._state_lock = threading.RLock()
        # While holding `changed` only wait/notify and the read-only properties are safe;
        # read_at/append/close take the disk lock first (lock order).
        self.changed = threading.Condition(self._state_lock)
        self._segments: dict[int, Segment] = {}
        self._completed: deque[Segment] = deque()
        self._open: Segment | None = None
        self._bytes = 0
        self._charged = 0
        self._failure = ""
        self.closed = False
        self._protected: int | None = None  # 2.6.2: never retire from this segment on

    # ---- storage hooks (called with the disk lock held) -------------------------------
    @abstractmethod
    def _charge_for(self, size: int) -> int: ...

    @abstractmethod
    def _create(self, segment: Segment) -> None: ...

    @abstractmethod
    def _write(self, segment: Segment, data: _Bytes) -> None: ...

    @abstractmethod
    def _finish(self, segment: Segment) -> None: ...

    @abstractmethod
    def _unlink(self, segment: Segment) -> None: ...

    @abstractmethod
    def _read(self, segment: Segment, offset: int, size: int) -> _Bytes: ...

    @abstractmethod
    def _extra_check(self, segment: Segment, additional: int) -> None: ...

    @abstractmethod
    def _free_storage(self) -> None: ...

    def _release_storage(self) -> None:
        """Let go of the storage, keeping what can be kept (`close(keep=True)`)."""
        self._free_storage()

    def _forget(self, segment: Segment) -> None:
        """A segment went (called with the disk lock held)."""

    # ---- 2.6: a kept buffer's index (a v2 disk ring writes one; others keep none) -----
    def note_segment(
        self,
        seq: int,
        start: float,
        end: float,
        discontinuity: bool,
        start_quality: str,
        media_seq: int | None = None,
    ) -> None:
        """Segment `seq` completed and the timeline placed it (`media_seq`: the HLS media
        sequence it begins, 2.6.2)."""

    def note_part(self, first_seq: int, start: float, wall: float) -> None:
        """A part of the timeline begins at segment `first_seq`."""

    def note_label(self, label: dict) -> None:
        """Whose buffer it is (the channel's key; never a URL or a name)."""

    def note_where(self, where: dict) -> None:
        """Where the user was in it."""

    # ---- properties -------------------------------------------------------------------
    @property
    def oldest_seq(self) -> int | None:
        with self._state_lock:
            if self._completed:
                return self._completed[0].seq
            return self._open.seq if self._open is not None else None

    @property
    def newest_seq(self) -> int | None:
        with self._state_lock:
            if self._open is not None:
                return self._open.seq
            return self._completed[-1].seq if self._completed else None

    @property
    def open_seq(self) -> int | None:
        with self._state_lock:
            return self._open.seq if self._open is not None else None

    @property
    def bytes(self) -> int:
        with self._state_lock:
            return self._bytes

    @property
    def charged(self) -> int:
        with self._state_lock:
            return self._charged

    @property
    def max_bytes(self) -> int:
        with self._state_lock:
            return self._max_bytes

    def protect_from(self, seq: int | None) -> None:
        """Never retire segment `seq` or a later one (2.6.2: what the player reads in a server
        part, and what lies ahead of it); None lifts it. Over the cap with only protected
        segments left, the ring takes more instead of failing (its writer is paced)."""
        with self._state_lock:
            self._protected = seq

    def retirable(self) -> bool:
        """It holds a completed segment that may go (2.6.2: none, or only protected ones)."""
        with self._state_lock:
            return self._retirable_locked()

    def _retirable_locked(self) -> bool:
        protected = self._protected
        return bool(self._completed) and (protected is None or self._completed[0].seq < protected)

    def set_max_bytes(self, max_bytes: int) -> None:
        """Hold up to `max_bytes` from now on (a multiview tile's ring grows with the focus and
        shrinks without it): a smaller cap evicts the oldest completed segments at once until
        what is held fits; the open segment always stays, as the eviction keeps it."""
        if max_bytes <= 0:
            raise RingError("The temporary buffer size must be positive.")
        with self._disk_lock:
            with self._state_lock:
                if self.closed:
                    return
                self._max_bytes = max_bytes
            while self._charged > max_bytes and self._retirable_locked():
                self._retire_oldest_locked()
            with self.changed:
                self.changed.notify_all()

    # ---- writer side ------------------------------------------------------------------
    def open_segment(self, seq: int, header: _Bytes, *, junction: bool = False) -> None:
        """`junction` (2.6): the segment begins a kept buffer's new run, whose tables may
        differ: a reader following on into it reads its header too (`junction()`)."""
        with self._disk_lock:
            self._check_writable()
            with self._state_lock:
                if seq in self._segments:
                    raise RingError("The temporary buffer segment already exists.")
            segment = Segment(seq, header_len=len(header), junction=junction)
            previous = self._open
            if previous is not None:
                self._guard(lambda: self._finish(previous))
            try:
                self._guard(lambda: self._create(segment))
            except RingError:
                if previous is not None:
                    with self.changed:
                        previous.complete = True
                        self._completed.append(previous)
                        self._open = None
                        self.changed.notify_all()
                raise
            # One atomic step: no reader can see "previous complete, new missing".
            with self.changed:
                if previous is not None:
                    previous.complete = True
                    self._completed.append(previous)
                self._segments[seq] = segment
                self._open = segment
                self.changed.notify_all()
            self._append_locked(segment, header)

    def append(self, data: _Bytes) -> None:
        with self._disk_lock:
            self._check_writable()
            segment = self._open
            if segment is None:
                raise RingError("No temporary buffer segment is open.")
            self._append_locked(segment, data)

    def complete(self, seq: int) -> None:
        with self._disk_lock:
            segment = self._open
            if segment is None or segment.seq != seq:
                return
            self._complete_locked(segment)

    def _check_writable(self) -> None:
        if self.closed:
            raise RingError("The temporary buffer was closed.")
        if self._failure:
            raise RingError(self._failure)

    def _guard(self, action: Callable[[], None]) -> None:
        try:
            action()
        except RingError as error:
            self._failure = str(error)
            raise
        except OSError:
            self._failure = _WRITE_FAILED
            raise RingError(_WRITE_FAILED) from None

    def _append_locked(self, segment: Segment, data: _Bytes) -> None:
        if not data:
            return
        self._guard(lambda: self._reserve_write(segment, len(data)))
        self._guard(lambda: self._write(segment, data))
        with self.changed:
            segment.size += len(data)
            self._bytes += len(data)
            self.changed.notify_all()

    def _reserve_write(self, segment: Segment, length: int) -> None:
        charge = self._charge_for(segment.size + length)
        additional = charge - segment.charge
        while self._charged + additional > self._max_bytes and self._retirable_locked():
            self._retire_oldest_locked()
        if self._charged + additional > self._max_bytes and not self._completed:
            raise RingError(self._LIMIT_TEXT)  # (only protected ones left: it takes more)
        self._extra_check(segment, additional)
        with self._state_lock:
            self._charged += additional
            segment.charge = charge

    def _complete_locked(self, segment: Segment) -> None:
        self._guard(lambda: self._finish(segment))
        with self.changed:
            segment.wall = _wall_time()
            segment.complete = True
            if self._open is segment:
                self._open = None
            self._completed.append(segment)
            self.changed.notify_all()

    def _delete_locked(self, segment: Segment) -> None:
        # Metadata is removed before the storage is freed, so no reader looks it up again.
        with self._state_lock:
            self._segments.pop(segment.seq, None)
        self._forget(segment)
        try:
            self._unlink(segment)
        finally:  # gone from the ring either way: the accounting follows (2.6)
            with self._state_lock:
                self._bytes -= segment.size
                self._charged -= segment.charge

    def clear_failure(self) -> None:
        """Write again after a failure (2.6: a kept ring's resume tries its disk anew)."""
        with self._disk_lock:
            self._failure = ""

    def drop_open(self) -> None:
        """Let the open segment go, whatever it holds (2.6: a write to it failed; its number is
        used again by the next one). Its storage is freed as far as it can be."""
        with self._disk_lock:
            segment = self._open
            if segment is None or self.closed:
                return
            with self.changed:
                self._open = None
                segment.retired = True
                self.changed.notify_all()
            try:
                self._delete_locked(segment)
            except OSError:
                pass  # its file stays behind; the folder goes with the ring

    def oldest_wall(self) -> float | None:
        """When the oldest completed segment completed (Unix time); None without one."""
        with self._state_lock:
            return self._completed[0].wall if self._completed else None

    def retire_oldest(self) -> bool:
        """Let the oldest completed segment go (2.6, the disk amount); False without one."""
        with self._disk_lock:
            if self.closed or not self._retirable_locked():
                return False
            self._retire_oldest_locked()
            with self.changed:
                self.changed.notify_all()
            return True

    def retire_before(self, wall: float) -> int:
        """Let every completed segment that completed before `wall` go (2.6: a day old);
        the open one stays. Returns how many went."""
        count = 0
        with self._disk_lock:
            while not self.closed and self._retirable_locked() and self._completed[0].wall < wall:
                self._retire_oldest_locked()
                count += 1
            if count:
                with self.changed:
                    self.changed.notify_all()
        return count

    def _retire_oldest_locked(self) -> None:
        with self._state_lock:
            segment = self._completed.popleft()
            segment.retired = True
        self._delete_locked(segment)

    # ---- reader side ------------------------------------------------------------------
    def read_at(self, seq: int, offset: int, size: int) -> tuple[_Bytes, bool, int] | None:
        """Lease-free read: `(data, complete, header_len)`, or None if `seq` is absent,
        retired or the ring is closed.

        Lookup and read both happen under the disk lock, the only place segments are
        retired, deleted or grown, so nothing is pinned against eviction and no deleted or
        half-written byte is returned. `complete` describes the segment at the time of the
        read: when True, empty `data` means the segment was read to its end. A `size` of
        zero (or an offset at the end) returns no data without touching storage.
        """
        with self._disk_lock:
            with self._state_lock:
                segment = self._segments.get(seq)
                if segment is None or segment.retired or self.closed:
                    return None
                complete, header_len = segment.complete, segment.header_len
                size = min(size, segment.size - offset) if offset >= 0 else 0
            if size <= 0:
                return b"", complete, header_len
            try:
                return self._read(segment, offset, size), complete, header_len
            except OSError, KeyError:
                raise RingError(_READ_FAILED) from None

    def junction(self, seq: int) -> bool:
        """Segment `seq` is held and begins a new run (see `open_segment`)."""
        with self._state_lock:
            segment = self._segments.get(seq)
            return segment is not None and segment.junction and not segment.retired

    def wait_for_seq(self, seq: int, timeout: float) -> str:
        """Wait for segment `seq`: "open" (open or complete), "evicted", "closed", "timeout"."""

        def status() -> str:
            if self.closed:
                return "closed"
            segment = self._segments.get(seq)
            if segment is not None:
                return "evicted" if segment.retired else "open"
            oldest = self._completed[0].seq if self._completed else None
            if oldest is None and self._open is not None:
                oldest = self._open.seq
            if oldest is not None and seq < oldest:
                return "evicted"
            return "timeout"

        with self.changed:
            self.changed.wait_for(lambda: status() != "timeout", timeout)
            return status()

    def close(self, *, keep: bool = False) -> None:
        """Close the ring and free its storage; `keep` (2.6, a helper going down): a kept
        buffer's folder stays for a later helper."""
        with self.changed:
            if self.closed:
                return
            self.closed = True
            self.changed.notify_all()
        with self._disk_lock:
            with self._state_lock:
                self._segments.clear()
                self._completed.clear()
                self._open = None
            try:
                if keep:
                    self._release_storage()
                else:
                    self._free_storage()
            finally:
                with self._state_lock:
                    self._bytes = 0
                    self._charged = 0


class RamRing(Ring):
    _LIMIT_TEXT = "The memory buffer cannot fit one keyframe interval."
    DEFAULT_MAX_BYTES = 128 * 1024 * 1024

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        super().__init__(max_bytes)
        self._buffers: dict[int, bytearray] = {}

    def _charge_for(self, size: int) -> int:
        return size

    def _create(self, segment: Segment) -> None:
        self._buffers[segment.seq] = bytearray()

    def _write(self, segment: Segment, data: bytes) -> None:
        self._buffers[segment.seq].extend(data)

    def _finish(self, segment: Segment) -> None:
        pass

    def _unlink(self, segment: Segment) -> None:
        self._buffers.pop(segment.seq, None)

    def _read(self, segment: Segment, offset: int, size: int) -> bytes:
        return bytes(self._buffers[segment.seq][offset : offset + size])

    def _extra_check(self, segment: Segment, additional: int) -> None:
        pass

    def _free_storage(self) -> None:
        self._buffers.clear()


def _allocation_unit(directory: Path) -> int:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        sectors = wintypes.DWORD()
        bytes_per_sector = wintypes.DWORD()
        free = wintypes.DWORD()
        total = wintypes.DWORD()
        root = str(directory.resolve().anchor)
        if not ctypes.windll.kernel32.GetDiskFreeSpaceW(
            root,
            ctypes.byref(sectors),
            ctypes.byref(bytes_per_sector),
            ctypes.byref(free),
            ctypes.byref(total),
        ):
            raise RingError("Cannot determine the temporary disk allocation size.")
        return int(sectors.value * bytes_per_sector.value)
    return max(512, os.statvfs(directory).f_frsize)


def _owned_files(directory: Path, *, require_marker: bool = True) -> list[Path] | None:
    """Do not follow symlinks or remove anything outside our tiny file format."""
    known = {"owner", "session.lock"}
    if _KEPT_PATTERN.fullmatch(directory.name):
        known = known | _INDEX_NAMES  # a v2 folder's index
    try:
        files = list(directory.iterdir())
        if any(
            item.is_symlink()
            or not item.is_file()
            or not (item.name in known or _MEDIA_PATTERN.fullmatch(item.name))
            for item in files
        ):
            return None
        if not require_marker:
            return files
        marker = directory / "owner"
        # Text-mode writes use CRLF on Windows; read_text normalizes both endings.
        if marker.stat().st_size not in (len(_OWNER), len(_OWNER) + 1):
            return None
        if marker.read_text(encoding="ascii") != _OWNER:
            return None
        return files
    except OSError, UnicodeError:
        return None


def _remove_stale_sessions(root: Path) -> None:
    for directory in root.iterdir():
        if (
            not _SESSION_PATTERN.fullmatch(directory.name)
            or directory.is_symlink()
            or not directory.is_dir()
            or _owned_files(directory) is None
        ):
            continue
        lock = QLockFile(str(directory / "session.lock"))
        # Age alone never makes a running session stale.
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            continue
        try:
            files = _owned_files(directory)
            if files is not None:
                for item in files:
                    if item.name != "session.lock":
                        item.unlink()
        finally:
            lock.unlock()
        if files is not None:
            directory.rmdir()


def remove_stale_sessions(root: Path) -> None:
    """Remove the buffers that dead owners left in `root` (a crash or a power loss).

    Runs under the manager lock; skipped when `root` does not exist yet or another process
    holds the lock (a disk session start sweeps then). Live sessions keep their locks and
    are never touched. Raises OSError.
    """
    root = Path(root)
    if not root.is_dir():
        return
    manager = QLockFile(str(root / _MANAGER_LOCK))
    manager.setStaleLockTime(0)
    if not manager.tryLock(0):
        return
    try:
        _remove_stale_sessions(root)
    finally:
        manager.unlock()


class DiskRing(Ring):
    def __init__(
        self,
        root: Path,
        session_dir_name: str,
        max_bytes: int,
        stop: threading.Event | None = None,
    ) -> None:
        super().__init__(max_bytes)
        kept = bool(_KEPT_PATTERN.fullmatch(session_dir_name))
        if not kept and not _SESSION_PATTERN.fullmatch(session_dir_name):
            raise RingError("Cannot lock the temporary playback buffer.")
        self.dir_name = session_dir_name
        self._kept = kept  # 2.6: a v2 folder with an index
        self._index_lock = threading.Lock()
        # 2.6: the index's path while it is written (opened for each line: a kept buffer holds
        # no file open but its lock); None: no index, or it failed.
        self._index: Path | None = None
        self._index_segs: dict[int, dict] = {}  # the held segments' lines
        self._index_parts: list[dict] = []
        self._index_label: dict | None = None
        self._index_where: dict | None = None
        self._index_dead = 0  # segment lines naming segments gone since the last compaction
        self._root = Path(root)
        self._directory: Path | None = None
        self._session_lock: QLockFile | None = None
        self._unit = 4096
        self._file: BinaryIO | None = None
        self._file_seq: int | None = None
        try:
            self._prepare_directory(self._root / session_dir_name, stop)
        except BaseException:
            try:
                self._free_storage()
            except OSError, RingError:
                _LOG.warning("Cleanup after a failed buffer start was incomplete.")
            raise

    def _prepare_directory(self, directory: Path, stop: threading.Event | None) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        manager = QLockFile(str(self._root / _MANAGER_LOCK))
        manager.setStaleLockTime(0)
        deadline = time.monotonic() + _IO_TIMEOUT
        while not manager.tryLock(0):
            if time.monotonic() >= deadline:
                raise RingError("The temporary buffer directory is busy.")
            if stop is not None:
                if stop.wait(0.05):
                    raise RingError(_STOPPED)
            else:
                time.sleep(0.05)
        try:
            _remove_stale_sessions(self._root)
            directory.mkdir(mode=0o700)
            self._directory = directory
            (directory / "owner").write_text(
                _KEPT_OWNER if self._kept else _OWNER, encoding="ascii"
            )
            if self._kept:
                (directory / _INDEX).write_bytes(b'{"v":2}\n')
                self._index = directory / _INDEX
            lock = QLockFile(str(directory / "session.lock"))
            lock.setStaleLockTime(0)
            if not lock.tryLock(0):
                raise RingError("Cannot lock the temporary playback buffer.")
            self._session_lock = lock
            self._unit = _allocation_unit(directory)
            if shutil.disk_usage(directory).free < _RESERVE_BYTES:
                raise RingError("Not enough free disk space for the temporary buffer.")
        except OSError:
            raise RingError("The temporary playback buffer could not be created.") from None
        finally:
            manager.unlock()

    def _path(self, segment: Segment) -> Path:
        assert self._directory is not None
        return self._directory / f"segment-{segment.seq}.ts"

    def _charge_for(self, size: int) -> int:
        return ((size + self._unit - 1) // self._unit) * self._unit

    def _create(self, segment: Segment) -> None:
        self._file = self._path(segment).open("xb", buffering=0)
        self._file_seq = segment.seq

    def _write(self, segment: Segment, data: bytes) -> None:
        assert self._file is not None
        written = self._file.write(data)
        if written is None or written != len(data):
            raise RingError(_WRITE_FAILED)

    def _finish(self, segment: Segment) -> None:
        if self._file is not None and self._file_seq == segment.seq:
            self._file.close()
            self._file = None
            self._file_seq = None

    def _unlink(self, segment: Segment) -> None:
        if self._file is not None and self._file_seq == segment.seq:  # 2.6: an open one dropped
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
            self._file_seq = None
        self._path(segment).unlink()

    def _read(self, segment: Segment, offset: int, size: int) -> bytes:
        # Open, seek, read and close per chunk so Windows can unlink retired files.
        with self._path(segment).open("rb") as handle:
            handle.seek(offset)
            return handle.read(size)

    def _extra_check(self, segment: Segment, additional: int) -> None:
        assert self._directory is not None
        if additional and shutil.disk_usage(self._directory).free < _RESERVE_BYTES + additional:
            raise RingError("Recording stopped to preserve free disk space.")

    def _close_files(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
        self._file = None
        with self._index_lock:
            self._index = None

    def _release_storage(self) -> None:
        """Keep a v2 folder as it is, its lock free for a later helper (2.6); a v1 one goes."""
        if not self._kept:
            self._free_storage()
            return
        self._close_files()
        if self._session_lock is not None:
            self._session_lock.unlock()
            self._session_lock = None
        self._directory = None

    def _free_storage(self) -> None:
        self._close_files()
        directory = self._directory
        if directory is None:
            return
        failed = False
        try:
            # This process created the directory, even if writing its ownership
            # marker failed because the disk filled up.
            files = _owned_files(directory, require_marker=False)
            if files is not None:
                for item in files:
                    if item.name != "session.lock":
                        item.unlink()
            if self._session_lock is not None:
                self._session_lock.unlock()
                self._session_lock = None
            if files is not None:
                directory.rmdir()
        except OSError:
            failed = True
        self._directory = None
        if failed:
            raise RingError("Some temporary buffer files could not be removed.")

    # ---- 2.6: the index of a v2 folder ----------------------------------------------------
    @classmethod
    def adopt(cls, root: Path, dir_name: str, max_bytes: int) -> tuple[DiskRing, Index]:
        """Take a kept (v2) folder back (a later helper): its lock taken, its index read, the
        files its index does not hold removed, the index written anew. RingError when it is
        not one, holds nothing whole, or another process holds it."""
        if not _KEPT_PATTERN.fullmatch(dir_name):
            raise RingError("Not a kept buffer.")
        directory = Path(root) / dir_name
        if directory.is_symlink() or not directory.is_dir():
            raise RingError("The kept buffer is gone.")
        ring = cls.__new__(cls)
        Ring.__init__(ring, max_bytes)
        ring.dir_name = dir_name
        ring._kept = True
        ring._index_lock = threading.Lock()
        ring._index = None
        ring._index_segs, ring._index_parts, ring._index_dead = {}, [], 0
        ring._index_label = ring._index_where = None
        ring._root, ring._directory, ring._session_lock = Path(root), None, None
        ring._unit, ring._file, ring._file_seq = 4096, None, None
        lock = QLockFile(str(directory / "session.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise RingError("The kept buffer is in use.")
        ring._session_lock, ring._directory = lock, directory
        try:
            index = read_index(directory)
            if index is None or not index.segments:
                raise RingError("The kept buffer could not be read.")
            held = {record["seg"] for record in index.segments}
            for item in directory.iterdir():
                media = _MEDIA_PATTERN.fullmatch(item.name)
                if (media and int(item.name[8:-3]) not in held) or item.name == _INDEX_NEW:
                    item.unlink(missing_ok=True)
            ring._unit = _allocation_unit(directory)
            for record in index.segments:
                segment = Segment(
                    record["seg"],
                    header_len=record["h"],
                    size=record["n"],
                    complete=True,
                    charge=ring._charge_for(record["n"]),
                    wall=float(record["w"]),
                    junction=bool(record.get("j")),
                )
                ring._segments[segment.seq] = segment
                ring._completed.append(segment)
                ring._bytes += segment.size
                ring._charged += segment.charge
                ring._index_segs[segment.seq] = record
            ring._index_parts = list(index.parts)
            ring._index_label, ring._index_where = index.label, index.where
            ring._index = directory / _INDEX
            ring._compact()
        except OSError:
            ring._release_storage()
            raise RingError("The kept buffer could not be read.") from None
        except BaseException:
            ring._release_storage()
            raise
        return ring, index

    def _append_index(self, record: dict) -> None:
        line = (json.dumps(record, separators=(",", ":")) + "\n").encode("ascii")
        with self._index_lock:
            if self._index is None:
                return
            try:
                with self._index.open("ab") as handle:
                    handle.write(line)
            except OSError:
                _LOG.warning("A kept buffer's index could not be written; it will not be kept.")
                self._index = None

    def note_segment(
        self,
        seq: int,
        start: float,
        end: float,
        discontinuity: bool,
        start_quality: str,
        media_seq: int | None = None,
    ) -> None:
        if not self._kept:
            return
        with self._state_lock:
            segment = self._segments.get(seq)
            if segment is None or not segment.complete:
                return
            record = {
                "seg": seq,
                "h": segment.header_len,
                "n": segment.size,
                "s": start,
                "e": end,
                "d": bool(discontinuity),
                "q": start_quality,
                "w": segment.wall,
                "j": segment.junction,
            }
            if media_seq is not None:
                record["m"] = media_seq  # 2.6.2; older builds ignore the key
            self._index_segs[seq] = record
        self._append_index(record)

    def note_part(self, first_seq: int, start: float, wall: float) -> None:
        if self._kept:
            record = {"part": first_seq, "s": start, "w": wall}
            with self._state_lock:
                self._index_parts.append(record)
            self._append_index(record)

    def note_label(self, label: dict) -> None:
        if self._kept:
            self._index_label = dict(label)
            self._append_index({"label": self._index_label})

    def note_where(self, where: dict) -> None:
        if self._kept:
            self._index_where = dict(where)
            self._append_index({"where": self._index_where})

    def _forget(self, segment: Segment) -> None:
        if not self._kept:
            return
        with self._state_lock:
            if self._index_segs.pop(segment.seq, None) is None:
                return
            self._index_dead += 1
            due = self._index_dead > max(64, len(self._index_segs))
        if due:
            self._compact()

    def _compact(self) -> None:
        """Write the index anew with the held segments only (called with the disk lock)."""
        directory = self._directory
        if directory is None:
            return
        with self._state_lock:
            held = [self._index_segs[seq] for seq in sorted(self._index_segs)]
            oldest = held[0]["seg"] if held else None
            parts = [
                p
                for i, p in enumerate(self._index_parts)
                if oldest is None
                or i + 1 == len(self._index_parts)
                or self._index_parts[i + 1]["part"] > oldest
            ]
            self._index_parts = parts
            self._index_dead = 0
        records: list[dict] = [{"v": 2}]
        if self._index_label is not None:
            records.append({"label": self._index_label})
        if self._index_where is not None:
            records.append({"where": self._index_where})
        records += parts + held
        text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
        with self._index_lock:
            if self._index is None:
                return
            try:
                (directory / _INDEX_NEW).write_text(text, encoding="ascii")
                os.replace(directory / _INDEX_NEW, directory / _INDEX)
            except OSError:
                _LOG.warning("A kept buffer's index could not be compacted; it will not be kept.")
                self._index = None


# ---- 2.6: the kept buffer's index --------------------------------------------------------


@dataclass
class Index:
    """What a kept buffer's folder holds (`read_index`): its whole segments' lines in order
    (the newest unbroken run of them), its parts, whose it is and where the user was."""

    segments: list[dict] = field(default_factory=list)
    parts: list[dict] = field(default_factory=list)
    label: dict | None = None
    where: dict | None = None


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _segment_line(record: dict) -> bool:
    return (
        type(record.get("seg")) is int
        and type(record.get("h")) is int
        and type(record.get("n")) is int
        and _number(record.get("s"))
        and _number(record.get("e"))
        and _number(record.get("w"))
    )


def read_index(folder: Path) -> Index | None:
    """A v2 folder's index (module docstring); None when it is not one or cannot be read.
    A torn or odd line is skipped. Only the newest unbroken run of whole segments counts: a
    segment whose file is missing or of another size ends it (a damaged buffer keeps what is
    after the damage). Reads files only: call it off the GUI thread."""
    try:
        marker = (folder / "owner").read_text(encoding="ascii").replace("\r\n", "\n")
        if marker != _KEPT_OWNER:
            return None
        raw = (folder / _INDEX).read_bytes()
    except OSError, UnicodeError:
        return None
    segments: dict[int, dict] = {}
    index = Index()
    versioned = False
    for line in raw.split(b"\n"):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("v") == 2:
            versioned = True
        elif _segment_line(record):
            segments[record["seg"]] = record
        elif (
            type(record.get("part")) is int
            and _number(record.get("s"))
            and _number(record.get("w"))
        ):
            index.parts.append(record)
        elif isinstance(record.get("label"), dict):
            index.label = record["label"]
        elif isinstance(record.get("where"), dict):
            index.where = record["where"]
    if not versioned:
        return None
    run: list[dict] = []
    for seq in sorted(segments, reverse=True):
        if run and run[-1]["seg"] != seq + 1:
            break
        try:
            size = (folder / f"segment-{seq}.ts").stat().st_size
        except OSError:
            break
        if size != segments[seq]["n"]:
            break
        run.append(segments[seq])
    run.reverse()
    index.segments = run
    return index


def cleanup_session_dir(root: Path, name: str) -> bool:
    """Remove a dead helper's session directory; True when removed or absent, False
    when a live owner still holds it (left untouched)."""
    directory = Path(root) / name
    if not (_SESSION_PATTERN.fullmatch(name) or _KEPT_PATTERN.fullmatch(name)):
        raise OSError("not a temporary session name")
    if directory.is_symlink():
        raise OSError("temporary session was replaced")
    if directory.exists():
        if _owned_files(directory, require_marker=False) is None:
            raise OSError("temporary session contains unowned files")
        lock = QLockFile(str(directory / "session.lock"))
        lock.setStaleLockTime(0)
        # A live helper still holds its lock; only a dead owner's can be reclaimed.
        if not lock.tryLock(0):
            return False
        try:
            files = _owned_files(directory, require_marker=False)
            if files is None:
                raise OSError("temporary session contains unowned files")
            for item in files:
                if item.name != "session.lock":
                    item.unlink(missing_ok=True)
        finally:
            lock.unlock()
        directory.rmdir()
    # A forced stop may have interrupted directory preparation while the helper held
    # the manager lock. QLockFile only reaps it if its owner died; an active
    # sibling's manager is never unlocked or removed.
    manager_path = Path(root) / _MANAGER_LOCK
    if manager_path.exists():
        manager = QLockFile(str(manager_path))
        manager.setStaleLockTime(0)
        if manager.tryLock(0):
            manager.unlock()
    return True


class DiskBudget:
    """The user's disk amount, shared by every disk ring of a helper (2.6).

    Each ring keeps under its own cap as it writes; `trim()`, called once a segment completed
    and without any ring's lock held, lets the oldest segments go until all of them fit: a
    server part's played ones first (2.6.2: never the live buffer it runs into), then a kept
    (parked) ring's, the oldest first, then the oldest of any. Lock order: the budget's lock,
    then one ring's at a time.
    """

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._rings: dict[int, tuple[Ring, bool]] = {}  # id -> (ring, parked)
        self._parts: set[int] = set()  # 2.6.2: server parts' rings

    def register(self, ring: Ring, *, parked: bool = False, part: bool = False) -> None:
        with self._lock:
            self._rings[id(ring)] = (ring, parked)
            if part:
                self._parts.add(id(ring))

    def set_parked(self, ring: Ring, parked: bool) -> None:
        with self._lock:
            if id(ring) in self._rings:
                self._rings[id(ring)] = (ring, parked)

    def unregister(self, ring: Ring) -> None:
        with self._lock:
            self._rings.pop(id(ring), None)
            self._parts.discard(id(ring))

    def trim(self) -> list[Ring]:
        """Fit the rings into `max_bytes`; returns the parked rings that lost segments (their
        sessions say so: nothing else would)."""
        changed: list[Ring] = []
        with self._lock:
            rings = list(self._rings.values())
            while sum(ring.charged for ring, _ in rings) > self.max_bytes:
                parts = [(r, p) for r, p in rings if id(r) in self._parts and r.retirable()]
                victim = (
                    min(parts, key=lambda item: item[0].oldest_wall() or 0.0) if parts else None
                )
                victim = victim or self._victim(rings)
                if victim is None:
                    break
                ring, parked = victim
                try:
                    if not ring.retire_oldest():
                        break
                except OSError, RingError:
                    # Its files went (a temp cleaner) or cannot be removed now: the ring is let
                    # go whole, quietly, never troubling the writer that asked.
                    _LOG.info("A kept time-shift buffer could not be trimmed; it goes.")
                    rings.remove(victim)
                    self._rings.pop(id(ring), None)
                    try:
                        ring.close()
                    except OSError:
                        pass  # its folder is swept with the session's
                    parked = True  # its owner says so, whatever it was
                if parked and ring not in changed:
                    changed.append(ring)
        return changed

    @staticmethod
    def _victim(rings: list[tuple[Ring, bool]]) -> tuple[Ring, bool] | None:
        """The ring whose oldest segment goes next: a parked one's oldest, else any one's."""
        for parked in (True, False):
            held = [(r, p) for r, p in rings if p is parked and r.retirable()]
            if held:
                return min(held, key=lambda item: item[0].oldest_wall() or 0.0)
        return None
