"""Saving what a live source reads from the provider, for replay (moved from
services/live/source.py as it was; a source makes one with `_Capture.from_environment`), and
reading an HLS capture's segments back (`captured_segments`)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
import hashlib
import logging
import os
from pathlib import Path
import queue
import shutil
import threading
import time
from typing import BinaryIO, Callable, Optional, TextIO

log = logging.getLogger(__name__)

AUDIO_PAIR = ".audio"  # `<name>.audio.ts` and `.audio.times`: an HLS channel's separate audio

# A directory where every opened source saves what it reads from the provider, with arrival
# times (`_Capture`). For debugging only: the files hold the provider's stream. Unset (the
# default), nothing is saved and nothing is paid.
CAPTURE_ENV = "QITV_CAPTURE_DIR"
_CAPTURE_MAX_BYTES = 2 << 30  # a capture stops after 2 GiB ...
_CAPTURE_MAX_SECONDS = 3 * 3600.0  # ... or 3 hours
_CAPTURE_FREE_FLOOR = 1 << 30  # and never leaves the disk less than 1 GiB free
_CAPTURE_CHECK_BYTES = 32 << 20  # the free space is looked up again after this much
_CAPTURE_BUFFER = 1 << 20
_CAPTURE_FLUSH_S = 1.0  # a read this long after the last flush flushes both files
_CAPTURE_QUEUE_BYTES = 64 << 20  # reads waiting for a slow disk; more are dropped
_CAPTURE_CLOSE_WAIT_S = 2.0  # a closing source waits this long for its capture's last writes


def _private(path: str, flags: int) -> int:
    """`open`'s opener for a capture file: readable and writable by its owner only."""
    return os.open(path, flags, 0o600)


class _Capture:
    """What one opened source reads from the provider, saved under ``QITV_CAPTURE_DIR`` so a
    glitch can be replayed exactly: by the replay tool (``scripts/replay_capture.py``), or
    the ``.ts`` file alone in VLC or mpv.

    ``<stamp>-<hash>.ts`` holds the bytes as they were read, before any parsing, joining or
    segmenting: a raw TS body from its first byte on, and an HLS channel's media segments one
    after another in arrival order (an AES-128 segment decrypted, as it reaches the joiner;
    an fMP4 one as the TS it is remuxed to; no playlist). ``<stamp>-<hash>.times`` is text
    with one ``<cumulative bytes> <seconds>`` line per read: the bytes in the ``.ts`` file
    once that read was saved, and the seconds
    since the open began, with 3 decimals. Lines starting with ``#`` are comments, which a
    reader skips: ``# reconnect <seconds>`` once a raw TS stream's new connection answered
    (its bytes go on in the same pair, as they go on in the stream the helper reads), and for
    HLS ``# segment <seconds> <sequence>`` as each segment answers (its media sequence
    number), after ``# discontinuity <seconds>`` when a break comes before it. The stamp is
    the local time at the open and the hash 8 hex digits of the channel URL's SHA-256: no
    address or credential reaches a file or a log.

    An HLS channel whose separate audio rendition is woven in (`services.live.hls_audio`)
    saves the audio's segments in a second pair, ``<name>.audio.ts`` and ``.audio.times``
    (`audio`), on the same clock and in the same form: each audio segment as it was read (TS,
    or packed AAC with its ID3 tag; fMP4 as the TS it is remuxed to), after ``# segment
    <seconds> <sequence>`` naming its own media sequence number (the renditions are matched by
    time). `hls_audio.weave_capture` weaves the pairs again as the helper did, so a capture
    replays with sound (``scripts/replay_capture.py`` does).

    The files are made at the first bytes, readable by their owner only (and the directory,
    when it is made here), and written buffered by a thread of the capture's own, in the
    order the reads came: the reading thread only queues them, so a slow disk never holds
    the stream up. Reads that would leave more than 64 MiB waiting for the disk are dropped
    (one warning says so). Both files are flushed by a comment line, by a read that comes a
    second or more after the last flush (so also the first read after a stall), and when the
    source closes: `flush` from the closing thread, then the reading thread's own close,
    which waits up to 2 seconds for what is queued (the helper ends with ``os._exit``, which
    flushes nothing). A capture stops after 2 GiB or 3 hours, or where the disk would keep
    less than 1 GiB free; a failure to write logs one warning and stops it (its audio pair
    with it). Playback goes on unchanged either way.
    """

    def __init__(self, directory: str, url: str, *, video: _Capture | None = None) -> None:
        """`video`: this is that capture's audio pair (`audio`)."""
        self._video = video
        self._opened: float = time.monotonic() if video is None else video._opened
        digest = hashlib.sha256(url.encode("utf-8", "surrogatepass")).hexdigest()[:8]
        self.name = f"{time.strftime('%Y%m%d-%H%M%S')}-{digest}"
        self._directory = directory
        # The writer's alone: the files and what went to them.
        self._files: tuple[BinaryIO, TextIO] | None = None
        self._written = 0
        self._checked = 0  # what had been written when the free space was last looked up
        self._flushed = self._opened
        self._stopped = False  # by a limit, a failure or the close (the writer)
        # The reading thread's: the writer and its jobs (None: the close), the bytes queued
        # and not yet written, and whether reads were dropped (said once).
        self._writer: threading.Thread | None = None
        self._jobs: queue.SimpleQueue[Optional[Callable[[], None]]] = queue.SimpleQueue()
        self._queued = 0
        self._queued_lock = threading.Lock()  # the reading thread adds, the writer takes
        self._closing = False  # close() came: nothing more is queued
        self._dropped = False
        self._audio: _Capture | None = None

    def audio(self) -> _Capture:
        """The pair a separate HLS audio rendition's segments go to (made with their first
        bytes, named after this one's: ``<name>.audio.ts`` and ``.audio.times``)."""
        if self._audio is None:
            self._audio = _Capture(self._directory, "", video=self)
        return self._audio

    @classmethod
    def from_environment(cls, url: str) -> _Capture | None:
        directory = os.environ.get(CAPTURE_ENV, "")
        return cls(directory, url) if directory else None

    @property
    def _halted(self) -> bool:
        """It stopped (an audio pair also once its video's did)."""
        return self._stopped or (self._video is not None and self._video._stopped)

    @property
    def _ended(self) -> bool:
        """Nothing more is queued: it stopped, or its source closed."""
        video = self._video
        return self._halted or self._closing or (video is not None and video._closing)

    def write(self, block: bytes, at: float | None = None) -> None:
        """One read's bytes, which arrived at ``at`` (`time.monotonic`; now by default)."""
        if self._ended or not block:
            return
        now = time.monotonic() if at is None else at
        with self._queued_lock:
            room = self._queued + len(block) <= _CAPTURE_QUEUE_BYTES
            if room:
                self._queued += len(block)
        if room:
            self._queue(partial(self._write_queued, block, now))
        elif not self._dropped:
            self._dropped = True
            log.warning(
                "The live stream capture %s is dropping reads: its disk is slow.", self.name
            )

    def mark(self, event: str, *words: object) -> None:
        """A ``# <event> <seconds> [<word> …]`` line in the times file, now; both files are
        flushed."""
        if self._ended:
            return
        now = time.monotonic()
        line = " ".join([f"# {event} {now - self._opened:.3f}", *map(str, words)])
        self._queue(partial(self._mark_queued, line, now))

    def flush(self) -> None:
        """From any thread (the source's ``close`` and ``wait_closed``): what was read goes to
        disk (the audio pair's too), once the writer has written it. Never waits."""
        if self._audio is not None:
            self._audio.flush()
        if self._writer is not None and not self._ended:
            self._jobs.put(self._flush_queued)

    def close(self) -> None:
        """In the reading thread, as the source ends: what was read is written, and both files
        are flushed and closed (and the audio pair's). Waits up to 2 seconds for that."""
        if self._audio is not None:
            self._audio.close()
        self._closing = True
        writer = self._writer
        if writer is None:
            self._stopped = True  # nothing was ever queued: no file was made
            return
        self._jobs.put(None)
        writer.join(_CAPTURE_CLOSE_WAIT_S)
        if writer.is_alive():
            log.info("The live stream capture %s is still being written.", self.name)

    # ---- the writer ---------------------------------------------------------------------------

    def _queue(self, job: Callable[[], None]) -> None:
        if self._writer is None:
            writer = threading.Thread(target=self._run, name="qitv-capture", daemon=True)
            try:
                writer.start()
            except Exception as error:  # the stream goes on without its capture
                self._failed(error)
                return
            self._writer = writer
        self._jobs.put(job)

    def _run(self) -> None:
        """The writer: each job in the order queued; the files close at the close."""
        while (job := self._jobs.get()) is not None:
            try:
                job()
            except Exception as error:
                self._failed(error)
        self._close()

    def _write_queued(self, block: bytes, now: float) -> None:
        try:
            if not self._halted:
                self._write(block, now)
        finally:
            with self._queued_lock:
                self._queued -= len(block)

    def _mark_queued(self, line: str, now: float) -> None:
        if self._halted:
            return
        files = self._open()
        if files is not None:
            files[1].write(line + "\n")
            self._flush(now)

    def _flush_queued(self) -> None:
        if not self._halted and self._files is not None:
            self._flush(time.monotonic())

    def _write(self, block: bytes, now: float) -> None:
        seconds = now - self._opened
        if seconds >= _CAPTURE_MAX_SECONDS:
            self._stop(logging.INFO, "after 3 hours")
            return
        if self._written + len(block) > _CAPTURE_MAX_BYTES:
            self._stop(logging.INFO, "at 2 GiB")
            return
        files = self._open()
        if files is None:
            return
        data, times = files
        data.write(block)
        self._written += len(block)
        times.write(f"{self._written} {seconds:.3f}\n")
        if now - self._flushed >= _CAPTURE_FLUSH_S:
            self._flush(now)

    def _flush(self, now: float) -> None:
        if self._files is not None:
            for file in self._files:  # the bytes first: no line counts bytes still unsaved
                file.flush()
        self._flushed = now

    def _close(self) -> None:
        """The writer's: the files close (an audio pair's writer closes its own)."""
        self._stopped = True
        files, self._files = self._files, None
        if files is not None:
            for file in files:
                try:
                    file.close()
                except Exception as error:
                    log.debug("Closing the capture failed (%s).", type(error).__name__)

    def _open(self) -> tuple[BinaryIO, TextIO] | None:
        """The files, made at the first call; None once the disk is too full."""
        if self._files is None:
            os.makedirs(self._directory, mode=0o700, exist_ok=True)
            if not self._room():
                return None
            self._files = self._create()
            what = "the live stream" if self._video is None else "its separate audio"
            log.info("Capturing %s to %s.ts and .times.", what, self.name)
        elif self._written - self._checked >= _CAPTURE_CHECK_BYTES and not self._room():
            return None
        return self._files

    def _create(self) -> tuple[BinaryIO, TextIO]:
        """A new pair: a second open in the same second of the same channel adds -2, -3...;
        an audio pair takes its video's name."""
        names = [self._video.name + AUDIO_PAIR] if self._video is not None else []
        names = names or [self.name] + [f"{self.name}-{attempt}" for attempt in range(2, 100)]
        for name in names:
            path = os.path.join(self._directory, name)
            try:
                data = open(path + ".ts", "xb", buffering=_CAPTURE_BUFFER, opener=_private)
            except FileExistsError:
                continue
            try:
                times = open(path + ".times", "x", encoding="ascii", newline="\n", opener=_private)
            except BaseException:
                data.close()
                raise
            self.name = name
            return data, times
        raise FileExistsError()

    def _room(self) -> bool:
        """Whether the disk keeps 1 GiB free until the next look, even with the buffer."""
        self._checked = self._written
        free = shutil.disk_usage(self._directory).free
        if free >= _CAPTURE_FREE_FLOOR + _CAPTURE_CHECK_BYTES + _CAPTURE_BUFFER:
            return True
        self._stop(logging.WARNING, "with less than 1 GiB free on its disk")
        return False

    def _stop(self, level: int, why: str) -> None:
        self._close()
        log.log(level, "The live stream capture %s stopped %s.", self.name, why)

    def _failed(self, error: Exception) -> None:
        # An OSError's text names the path: only its type is told.
        self._close()
        log.warning(
            "The live stream capture %s stopped: it could not be written (%s).",
            self.name,
            type(error).__name__,
        )


@dataclass(frozen=True)
class CapturedSegment:
    """One HLS segment a capture pair holds: its media sequence number, its bytes as they were
    read, and when its last read came (seconds since the open)."""

    sequence: int
    data: bytes
    at: float


def captured_segments(name: str) -> list[CapturedSegment]:
    """The HLS segments of the capture pair `name` (its path without ``.ts`` or ``.times``),
    by its ``# segment <seconds> <sequence>`` lines: none for a raw TS capture."""
    data = Path(name + ".ts").read_bytes()
    marks: list[list] = []  # [where its bytes begin, its sequence, its last read's time]
    end = 0
    for line in Path(name + ".times").read_text(encoding="ascii").splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[:2] == ["#", "segment"]:
            marks.append([end, int(parts[3]), float(parts[2])])
        elif len(parts) == 2 and parts[0] != "#":
            end = int(parts[0])
            if marks:
                marks[-1][2] = float(parts[1])
    stops = [mark[0] for mark in marks[1:]] + [len(data)]
    return [
        CapturedSegment(sequence, data[start:stop], at)
        for (start, sequence, at), stop in zip(marks, stops)
    ]
