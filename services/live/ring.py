"""Bounded segment rings for the live time-shift pipeline: RAM (default) and disk.

One writer thread appends keyframe-aligned MPEG-TS segments; several reader threads read
them progressively (including the still-open one) with `read_at`, which pins nothing, and
wait on `changed` for more data. Eviction removes the oldest *completed* segments first;
the open segment is never removed. Error texts are fixed strings.

Kept buffers (2.6): each completed segment notes the time of day it completed (`wall`); one
older than `STALE_S` goes (`retire_before`). Every disk ring of a helper shares the user's
disk amount (`DiskBudget`): over it, the oldest segment of a kept (parked) ring goes first,
then the oldest of any.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
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
_MEDIA_PATTERN = re.compile(r"segment-[0-9]+\.ts")
_OWNER = "QiTV temporary time-shift v1\n"
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
            while self._charged > max_bytes and self._completed:
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
        while self._charged + additional > self._max_bytes and self._completed:
            self._retire_oldest_locked()
        if self._charged + additional > self._max_bytes:
            raise RingError(self._LIMIT_TEXT)
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
            if self.closed or not self._completed:
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
            while not self.closed and self._completed and self._completed[0].wall < wall:
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

    def close(self) -> None:
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
    try:
        files = list(directory.iterdir())
        if any(
            item.is_symlink()
            or not item.is_file()
            or not (item.name in {"owner", "session.lock"} or _MEDIA_PATTERN.fullmatch(item.name))
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
        if not _SESSION_PATTERN.fullmatch(session_dir_name):
            raise RingError("Cannot lock the temporary playback buffer.")
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
            (directory / "owner").write_text(_OWNER, encoding="ascii")
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

    def _free_storage(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
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


def cleanup_session_dir(root: Path, name: str) -> bool:
    """Remove a dead helper's session directory; True when removed or absent, False
    when a live owner still holds it (left untouched)."""
    directory = Path(root) / name
    if not _SESSION_PATTERN.fullmatch(name):
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
    kept (parked) ring's first, the oldest first, then the oldest of any. Lock order: the
    budget's lock, then one ring's at a time.
    """

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._rings: dict[int, tuple[Ring, bool]] = {}  # id -> (ring, parked)

    def register(self, ring: Ring, *, parked: bool = False) -> None:
        with self._lock:
            self._rings[id(ring)] = (ring, parked)

    def set_parked(self, ring: Ring, parked: bool) -> None:
        with self._lock:
            if id(ring) in self._rings:
                self._rings[id(ring)] = (ring, parked)

    def unregister(self, ring: Ring) -> None:
        with self._lock:
            self._rings.pop(id(ring), None)

    def trim(self) -> list[Ring]:
        """Fit the rings into `max_bytes`; returns the parked rings that lost segments (their
        sessions say so: nothing else would)."""
        changed: list[Ring] = []
        with self._lock:
            rings = list(self._rings.values())
            while sum(ring.charged for ring, _ in rings) > self.max_bytes:
                victim = self._victim(rings)
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
            held = [(r, p) for r, p in rings if p is parked and r.oldest_wall() is not None]
            if held:
                return min(held, key=lambda item: item[0].oldest_wall() or 0.0)
        return None
