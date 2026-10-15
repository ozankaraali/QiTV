"""Bounded segment rings for the live time-shift pipeline: RAM (default) and disk.

One writer thread appends keyframe-aligned MPEG-TS segments; several reader threads
lease segments, read them progressively (including the still-open one) and wait for
more data. Eviction removes the oldest *completed* segments first; the open segment
and leased segments are never removed while in use. Error texts are fixed strings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import BinaryIO

from PySide6.QtCore import QLockFile

_SESSION_PREFIX = ".qitv-timeshift-"
_SESSION_PATTERN = re.compile(r"\.qitv-timeshift-[0-9a-f]{32}")
_MEDIA_PATTERN = re.compile(r"segment-[0-9]+\.ts")
_OWNER = "QiTV temporary time-shift v1\n"
_RESERVE_BYTES = 64 * 1024 * 1024
_IO_TIMEOUT = 2.0
_MANAGER_LOCK = ".qitv-timeshift-manager.lock"
_Bytes = bytes
_WRITE_FAILED = "The temporary disk could not complete a buffer write."


class RingError(Exception):
    """An explanation that never contains provider data or filesystem paths."""


@dataclass
class Segment:
    seq: int
    header_len: int = 0
    size: int = 0
    complete: bool = False
    retired: bool = False
    readers: int = 0
    charge: int = 0


class Ring(ABC):
    """Shared eviction, lease and wakeup logic; subclasses own the storage."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise RingError("The temporary buffer size must be positive.")
        self._max_bytes = max_bytes
        # Lock order: _disk_lock (storage operations) outside _state_lock (metadata).
        self._disk_lock = threading.Lock()
        self._state_lock = threading.Lock()
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

    # ---- writer side ------------------------------------------------------------------
    def open_segment(self, seq: int, header: _Bytes) -> None:
        with self._disk_lock:
            self._check_writable()
            if self._open is not None:
                self._complete_locked(self._open)
            segment = Segment(seq, header_len=len(header))
            with self._state_lock:
                if seq in self._segments:
                    raise RingError("The temporary buffer segment already exists.")
            self._guard(lambda: self._create(segment))
            with self._state_lock:
                self._segments[seq] = segment
                self._open = segment
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
            raise RingError(
                "The disk buffer limit cannot fit one keyframe interval or an active read."
            )
        self._extra_check(segment, additional)
        with self._state_lock:
            self._charged += additional
            segment.charge = charge

    def _complete_locked(self, segment: Segment) -> None:
        self._finish(segment)
        with self.changed:
            segment.complete = True
            if self._open is segment:
                self._open = None
            self._completed.append(segment)
            self.changed.notify_all()

    def _delete_locked(self, segment: Segment) -> None:
        # Metadata is removed before the storage is freed so no new reader can
        # acquire a lease between the two operations.
        with self._state_lock:
            self._segments.pop(segment.seq, None)
        self._unlink(segment)
        with self._state_lock:
            self._bytes -= segment.size
            self._charged -= segment.charge

    def _retire_oldest_locked(self) -> None:
        with self._state_lock:
            segment = self._completed.popleft()
            segment.retired = True
            delete = not segment.readers
        if delete:
            self._delete_locked(segment)

    # ---- reader side ------------------------------------------------------------------
    def lease(self, seq: int) -> Segment | None:
        with self._state_lock:
            segment = self._segments.get(seq)
            if segment is None or segment.retired or self.closed:
                return None
            segment.readers += 1
            return segment

    def release(self, segment: Segment) -> None:
        with self._disk_lock:
            with self._state_lock:
                segment.readers -= 1
                delete = segment.retired and not segment.readers and not self.closed
            if delete:
                self._delete_locked(segment)

    def read(self, segment: Segment, offset: int, size: int) -> _Bytes:
        """Return up to `size` bytes at `offset`, never beyond what was written."""
        if size <= 0 or offset < 0:
            return b""
        with self._disk_lock:
            if self.closed:
                return b""
            with self._state_lock:
                size = min(size, segment.size - offset)
            if size <= 0:
                return b""
            try:
                return self._read(segment, offset, size)
            except OSError:
                return b""

    def wait_for(self, segment: Segment, offset: int, timeout: float) -> bool:
        def ready() -> bool:
            return segment.size > offset or segment.complete or self.closed

        with self.changed:
            self.changed.wait_for(ready, timeout)
            return ready()

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
    def __init__(self, max_bytes: int = 128 * 1024 * 1024) -> None:
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


class DiskRing(Ring):
    def __init__(self, root: Path, session_dir_name: str, max_bytes: int) -> None:
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
            self._prepare_directory(self._root / session_dir_name)
        except BaseException:
            try:
                self._free_storage()
            except OSError:
                pass
            raise

    def _prepare_directory(self, directory: Path) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        manager = QLockFile(str(self._root / _MANAGER_LOCK))
        manager.setStaleLockTime(0)
        deadline = time.monotonic() + _IO_TIMEOUT
        while not manager.tryLock(0):
            if time.monotonic() >= deadline:
                raise RingError("The temporary buffer directory is busy.")
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


def cleanup_session_dir(root: Path, name: str) -> None:
    """Remove a dead helper's session directory; leave a live owner's untouched."""
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
        if lock.tryLock(0):
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
