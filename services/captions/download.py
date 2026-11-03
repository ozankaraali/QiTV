"""Downloading what AI captions need (services/captions/models.py) on a worker thread; no
widgets.

`CaptionsDownload.start(tier)` fetches whatever the tier still misses, one file after another
on a QThread: each streams to "<name>.part", resumes with an HTTP Range after a dropped
connection (and in a later run), is checked against its pinned size and sha256, then takes its
name. Archives (the small tier's tar.bz2, the runtime's wheels) are extracted once everything
has arrived, and each download is recorded in verified.json as it lands. `cancel()` keeps the
".part" files, so the next start resumes them.

The signals reach the GUI thread as `services.update_checker.start_download`'s do
(`QTimer.singleShot(0, <GUI object>, …)` from the worker; the thread released through
`ThreadCleanup`).
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import threading
from typing import Any, Callable, Optional
import zipfile

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from services.captions.models import (
    EXTRACT_TAR_BZ2,
    EXTRACT_WHEEL,
    SKIPPED_IN_ARCHIVE,
    Download,
    Unsupported,
    captions_root,
    has_contents,
    mark_verified,
    missing,
)
from services.i18n import tr
from services.lazy_import import lazy_module
from services.redact import safe_message
from services.thread_cleanup import ThreadCleanup
from services.vlc_profile import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

# failed()'s reason after cancel(): English, never shown.
CANCELLED = "cancelled"

CHUNK = 256 * 1024
PROGRESS_STEP = 512 * 1024  # progress() at most this often (bytes)
TIMEOUT = (30, 60)  # connect, read (s)
ATTEMPTS = 4  # tries a file, each resuming where the last stopped
RETRY_WAIT_S = (2.0, 5.0, 10.0)
SPARE_BYTES = 200 * 1024 * 1024  # left free on the disk after the download and extraction
WHEEL_GROWTH = 4  # a wheel takes about this many times its size once extracted (at most)

_MB = 1024 * 1024
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-")


class Cancelled(Exception):
    """cancel() was called."""


class Damaged(Exception):
    """A download isn't what was pinned (its size, its sha256, or what it unpacks to)."""


class NoSpace(Exception):
    """The disk hasn't room for the download and what it unpacks to."""


class _Short(Exception):
    """The server stopped before the file's end: the next try resumes it."""

    def __init__(self, done: int, total: int) -> None:
        super().__init__(f"{done}/{total} bytes")
        self.done = done
        self.total = total


def file_sha256(path: Path, cancelled: Callable[[], bool] = lambda: False) -> str:
    hasher = hashlib.sha256()
    _hash_into(path, hasher, cancelled)
    return hasher.hexdigest()


def _hash_into(path: Path, hasher: Any, cancelled: Callable[[], bool]) -> None:
    with open(path, "rb") as f:
        while block := f.read(4 * _MB):
            if cancelled():
                raise Cancelled
            hasher.update(block)


# ---- unpacking ----------------------------------------------------------------------------


def extract_tar_bz2(
    archive: Path,
    folder: Path,
    cancelled: Callable[[], bool] = lambda: False,
    skip: tuple[str, ...] = SKIPPED_IN_ARCHIVE,
) -> None:
    """Unpack a release tarball into `folder` without its top folder, leaving out the
    folders named in `skip` and anything but files and folders (tarfile's "data" filter
    refuses what would land outside `folder`)."""
    folder.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r|bz2") as tar:
        for member in tar:
            if cancelled():
                raise Cancelled
            parts = PurePosixPath(member.name).parts
            if parts[:1] == ("/",):
                parts = parts[1:]
            inner = parts[1:]
            if not inner or inner[0] in skip or not (member.isfile() or member.isdir()):
                continue
            tar.extract(member.replace(name="/".join(inner), deep=False), folder, filter="data")


def _wheel_entry(name: str) -> Optional[str]:
    """Where a wheel's entry goes in site-packages (None: not there, or not safe)."""
    if "\\" in name or name.startswith("/") or ":" in name.split("/", 1)[0]:
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    if parts[0].endswith(".data"):  # <dist>.data/<scheme>/…: only the libraries' schemes
        if len(parts) > 2 and parts[1] in ("purelib", "platlib"):
            return "/".join(parts[2:])
        return None
    return "/".join(parts)


def extract_wheel(wheel: Path, folder: Path, cancelled: Callable[[], bool] = lambda: False) -> None:
    """Unpack a wheel into `folder` as pip would into site-packages: its packages and
    .dist-info, `.data/purelib|platlib` at the top, other `.data` schemes (scripts, headers,
    copies of the libraries for PATH) left out; Unix permission bits kept."""
    folder.mkdir(parents=True, exist_ok=True)
    base = folder.resolve()
    with zipfile.ZipFile(wheel) as zf:
        for info in zf.infolist():
            if cancelled():
                raise Cancelled
            entry = _wheel_entry(info.filename)
            if entry is None:
                continue
            target = folder / entry
            if not target.resolve().is_relative_to(base):
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out, CHUNK)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(target, (mode | 0o600) & 0o755)


# ---- the worker ---------------------------------------------------------------------------


def _range_start(response) -> Optional[int]:
    found = _CONTENT_RANGE.match(response.headers.get("Content-Range", ""))
    return int(found.group(1)) if found else None


def _part(download: Download) -> Path:
    target = download.target
    return target.with_name(target.name + ".part")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def space_needed(downloads: list[Download]) -> int:
    """Bytes the downloads still write: what isn't on disk yet, and what archives unpack to."""
    need = 0
    for download in downloads:
        size = download.file.size
        need += max(0, size - max(_size(_part(download)), _size(download.target)))
        if download.file.extract == EXTRACT_TAR_BZ2:
            need += sum(found for _path, found in download.contents if found)
        elif download.file.extract == EXTRACT_WHEEL:
            need += WHEEL_GROWTH * size
    return need


class _DownloadWorker(QObject):
    progress = Signal(object, object)  # done, total: bytes (past 2 GiB, which int is not)
    preparing = Signal()  # everything arrived; unpacking
    done = Signal(str)  # "" when all is in place, else why not (CANCELLED after cancel())

    def __init__(self, tier: str, root: Path, plan: Callable[[str], list[Download]]) -> None:
        super().__init__()
        self._tier = tier
        self._root = root
        self._plan = plan
        self._cancel = threading.Event()
        self._total = 0
        self._base = 0  # bytes of the files done
        self._shown = -PROGRESS_STEP  # what progress() said last

    def cancel(self) -> None:
        self._cancel.set()

    def _cancelled(self) -> bool:
        return self._cancel.is_set()

    @Slot()
    def run(self) -> None:
        try:
            self._run()
        except Cancelled:
            logger.info("The AI captions download was cancelled")
            self.done.emit(CANCELLED)
        except Unsupported as e:
            logger.warning("AI captions: %s", e)
            self.done.emit(tr("Download failed: {error}", error=str(e)))
        except _Short as e:
            logger.warning("An AI captions download stopped short: %s", e)
            self.done.emit(
                tr("Download incomplete: {done}/{total} bytes", done=e.done, total=e.total)
            )
        except Damaged as e:
            logger.warning("An AI captions download is damaged: %s", e)
            self.done.emit(tr("Download failed: {error}", error=str(e)))
        except (tarfile.TarError, zipfile.BadZipFile, EOFError) as e:
            logger.warning("An AI captions archive could not be unpacked: %s", e)
            self.done.emit(tr("Download failed: {error}", error=safe_message(e)))
        except requests.RequestException as e:
            logger.warning("An AI captions download failed: %s", safe_message(e))
            self.done.emit(tr("Download failed: {error}", error=safe_message(e)))
        except (NoSpace, OSError) as e:
            message = str(e) if isinstance(e, NoSpace) else safe_message(e)
            logger.warning("An AI captions download could not be saved: %s", message)
            self.done.emit(tr("Failed to save file: {error}", error=message))
        except Exception as e:  # reported, never left hanging
            logger.exception("The AI captions download failed")
            self.done.emit(tr("Download failed: {error}", error=safe_message(e)))
        else:
            self.done.emit("")

    def _run(self) -> None:
        downloads = self._plan(self._tier)
        self._total = sum(download.file.size for download in downloads)
        self._root.mkdir(parents=True, exist_ok=True)
        need = space_needed(downloads)
        free = shutil.disk_usage(self._root).free
        if free < need + SPARE_BYTES:
            raise NoSpace(f"{(need + SPARE_BYTES) // _MB} MB needed, {free // _MB} MB free")
        logger.info(
            "AI captions: downloading %d files (%d MB) for the %s model",
            len(downloads),
            self._total // _MB,
            self._tier,
        )
        with requests.Session() as session:
            session.headers["User-Agent"] = DEFAULT_USER_AGENT
            for download in downloads:
                self._fetch(session, download)
                self._base += download.file.size
                self._show(0, force=True)
                if not download.file.extract:
                    mark_verified(self._root, download.key(self._root), download.file.sha256)
        archives = [download for download in downloads if download.file.extract]
        if archives:
            self.preparing.emit()
        for download in archives:
            self._unpack(download)
        logger.info("AI captions: the %s model is in place", self._tier)

    def _show(self, in_file: int, force: bool = False) -> None:
        done = self._base + in_file
        if force or abs(done - self._shown) >= PROGRESS_STEP:
            self._shown = done
            self.progress.emit(done, self._total)

    def _fetch(self, session, download: Download) -> None:
        """Bring the download's whole, checked file to its name."""
        file, target, part = download.file, download.target, _part(download)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():  # arrived in an earlier run (an archive not yet unpacked)
            if _size(target) == file.size and file_sha256(target, self._cancelled) == file.sha256:
                return
            target.unlink()
        for attempt in range(ATTEMPTS):
            try:
                digest = self._transfer(session, download)
                break
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
                _Short,
            ) as e:
                if attempt + 1 >= ATTEMPTS:
                    raise
                wait = RETRY_WAIT_S[min(attempt, len(RETRY_WAIT_S) - 1)]
                logger.info(
                    "AI captions: %s stopped (%s); resuming in %.0f s",
                    file.path,
                    safe_message(e),
                    wait,
                )
                if self._cancel.wait(wait):
                    raise Cancelled
        if digest != file.sha256:
            part.unlink(missing_ok=True)
            raise Damaged(f"{file.path}: sha256 mismatch")
        os.replace(part, target)

    def _transfer(self, session, download: Download) -> str:
        """Stream the file to its .part, resuming what's there; its sha256."""
        file, part = download.file, _part(download)
        offset = _size(part)
        if offset > file.size:
            part.unlink()
            offset = 0
        hasher = hashlib.sha256()
        if offset:
            _hash_into(part, hasher, self._cancelled)
        self._show(offset)
        if offset == file.size:
            return hasher.hexdigest()
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with session.get(file.url, headers=headers, stream=True, timeout=TIMEOUT) as response:
            if offset and response.status_code == 416:  # can't resume: start over
                part.unlink(missing_ok=True)
                raise _Short(offset, file.size)
            response.raise_for_status()
            if offset and _range_start(response) != offset:  # the whole file came instead
                offset = 0
                hasher = hashlib.sha256()
            offset = self._write(response, part, offset, hasher, file.size)
        if offset > file.size:
            part.unlink(missing_ok=True)
            raise Damaged(f"{file.path}: larger than {file.size} bytes")
        if offset < file.size:
            raise _Short(offset, file.size)
        return hasher.hexdigest()

    def _write(self, response, part: Path, offset: int, hasher: Any, size: int) -> int:
        """Append the answer's body to `part` (from `offset`); where it ended (past `size`:
        the file is longer than pinned, and the rest wasn't written)."""
        with open(part, "ab" if offset else "wb") as out:
            for chunk in response.iter_content(CHUNK):
                if self._cancel.is_set():
                    raise Cancelled
                if offset + len(chunk) > size:
                    return offset + len(chunk)
                out.write(chunk)
                hasher.update(chunk)
                offset += len(chunk)
                self._show(offset)
        return offset

    def _unpack(self, download: Download) -> None:
        archive = download.target
        logger.info("AI captions: unpacking %s", download.file.path)
        if download.file.extract == EXTRACT_TAR_BZ2:
            extract_tar_bz2(archive, download.folder, self._cancelled)
        elif download.file.extract == EXTRACT_WHEEL:
            extract_wheel(archive, download.folder, self._cancelled)
        if not has_contents(download.folder, download.contents):
            raise Damaged(f"{download.file.path}: not the files expected")
        mark_verified(self._root, download.key(self._root), download.file.sha256)
        archive.unlink(missing_ok=True)


# ---- the GUI side -------------------------------------------------------------------------


class CaptionsDownload(QObject):
    """Downloads a tier with everything it needs, one download at a time. Lives on the GUI
    thread; its signals arrive there.

    - `progress(done, total)`: bytes, at most every 512 KB (a resumed file counts as done).
    - `preparing()`: everything arrived; archives are being unpacked (up to a minute).
    - `finished()`: the tier is installed (`models.installed(tier)`).
    - `failed(reason)`: a text for the viewer, or CANCELLED after `cancel()`.
    """

    progress = Signal(object, object)  # bytes past 2 GiB (an int signal stops at 2 GiB)
    preparing = Signal()
    finished = Signal()
    failed = Signal(str)

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        root: Optional[Path] = None,
        plan: Optional[Callable[[str], list[Download]]] = None,
    ) -> None:
        """`root`: the captions folder (default `captions_root()`); `plan(tier)`: what to
        fetch (default `models.missing(tier, root)`, worked out on the worker thread)."""
        super().__init__(parent)
        self._root = root
        self._plan = plan
        self._worker: Optional[_DownloadWorker] = None

    def running(self) -> bool:
        """True from start() until finished or failed is emitted."""
        return self._worker is not None

    def cancel(self) -> None:
        """Stop soon (within a read of the network or disk); `failed(CANCELLED)` follows.
        The unfinished file stays for the next start to resume."""
        if self._worker is not None:
            self._worker.cancel()

    def start(self, tier: str) -> bool:
        """Download the tier; False (and nothing done) while a download runs."""
        if self._worker is not None:
            return False
        root = self._root if self._root is not None else captions_root()
        plan = self._plan or (lambda name: missing(name, root))
        thread = QThread()
        worker = _DownloadWorker(tier, root, plan)
        worker.moveToThread(thread)
        self._worker = worker
        outcome: list[str] = []

        def on_done(reason: str) -> None:  # the worker thread
            outcome.append(reason)
            thread.quit()

        # Worker signals reach these Python callables on the worker thread
        # (Qt.DirectConnection), which hand them to the GUI thread (`_later`), as in
        # update_checker.start_download.
        thread.started.connect(worker.run)
        worker.progress.connect(
            lambda done, total: self._later(worker, lambda: self.progress.emit(done, total)),
            Qt.DirectConnection,
        )
        worker.preparing.connect(
            lambda: self._later(worker, self.preparing.emit), Qt.DirectConnection
        )
        worker.done.connect(on_done, Qt.DirectConnection)
        ThreadCleanup(thread, worker).finished.connect(lambda _t: self._ended(worker, outcome))
        _stop_before_update(self)
        thread.start()
        return True

    def _later(self, worker: _DownloadWorker, emit: Callable[[], object]) -> None:
        """From the worker thread: `emit` on this object's (the GUI) thread, unless that
        download ended meanwhile; never once this object is gone."""
        QTimer.singleShot(0, self, lambda: self._worker is worker and emit())

    def _ended(self, worker: _DownloadWorker, outcome: list[str]) -> None:
        """The GUI thread, once the worker's thread is joined."""
        if self._worker is not worker:
            return
        self._worker = None
        reason = outcome[0] if outcome else tr("Download failed: {error}", error="")
        if reason:
            self.failed.emit(reason)
        else:
            self.finished.emit()


def _stop_before_update(download: CaptionsDownload) -> None:
    """A self-update waits for background threads: a download in progress stops (its .part
    stays for later)."""
    from services.update_checker import call_before_update

    call_before_update(download.cancel)
