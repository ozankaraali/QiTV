"""Saving what a live source reads from the provider, for replay (moved from
services/live/source.py as it was; a source makes one with `_Capture.from_environment`), and
reading an HLS capture's segments back (`captured_segments`)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import shutil
import threading
import time
from typing import BinaryIO, TextIO

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
    no playlist). ``<stamp>-<hash>.times`` is text with one ``<cumulative bytes> <seconds>``
    line per read: the bytes in the ``.ts`` file once that read was saved, and the seconds
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
    or packed AAC with its ID3 tag), after ``# segment <seconds> <sequence>`` naming the video
    segment it is woven into. `hls_audio.weave_capture` weaves the pairs again as the helper
    did, so a capture replays with sound (``scripts/replay_capture.py`` does).

    The files are made at the first bytes, readable by their owner only (and the directory,
    when it is made here), and written in the reading thread, buffered. Both are flushed by
    a comment line, by a read that comes a second or more after the last flush (so also the
    first read after a stall), and when the source closes: `flush` from the closing thread,
    then the reading thread's own close (the helper ends with ``os._exit``, which flushes
    nothing). A capture stops after 2 GiB or 3 hours, or where the disk would keep less than
    1 GiB free; a failure to write logs one warning and stops it (its audio pair with it).
    Playback goes on unchanged either way.
    """

    def __init__(self, directory: str, url: str, *, video: _Capture | None = None) -> None:
        """`video`: this is that capture's audio pair (`audio`)."""
        self._video = video
        self._opened: float = time.monotonic() if video is None else video._opened
        digest = hashlib.sha256(url.encode("utf-8", "surrogatepass")).hexdigest()[:8]
        self.name = f"{time.strftime('%Y%m%d-%H%M%S')}-{digest}"
        self._directory = directory
        self._lock = threading.Lock()  # the reading thread, and `flush` from a closing one
        self._files: tuple[BinaryIO, TextIO] | None = None
        self._written = 0
        self._checked = 0  # what had been written when the free space was last looked up
        self._flushed = self._opened
        self._stopped = False
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
    def _ended(self) -> bool:
        """It stopped (an audio pair also once its video's did)."""
        return self._stopped or (self._video is not None and self._video._stopped)

    def write(self, block: bytes, at: float | None = None) -> None:
        """One read's bytes, which arrived at ``at`` (`time.monotonic`; now by default)."""
        if self._ended or not block:
            return
        now = time.monotonic() if at is None else at
        with self._lock:
            try:
                self._write(block, now)
            except Exception as error:
                self._failed(error)

    def mark(self, event: str, *words: object) -> None:
        """A ``# <event> <seconds> [<word> …]`` line in the times file, now; both files are
        flushed."""
        if self._ended:
            return
        now = time.monotonic()
        line = " ".join([f"# {event} {now - self._opened:.3f}", *map(str, words)])
        with self._lock:
            try:
                files = self._open()
                if files is not None:
                    files[1].write(line + "\n")
                    self._flush(now)
            except Exception as error:
                self._failed(error)

    def flush(self) -> None:
        """From any thread (the source's ``close`` and ``wait_closed``): what is buffered goes
        to disk (the audio pair's too). Never waits: while the reading thread is writing, it
        is left to that thread, which closes the files as the source ends."""
        if self._audio is not None:
            self._audio.flush()
        if self._stopped or not self._lock.acquire(blocking=False):
            return
        try:
            if self._files is not None:
                self._flush(time.monotonic())
        except Exception as error:
            self._failed(error)
        finally:
            self._lock.release()

    def close(self) -> None:
        """In the reading thread, as the source ends: both files are flushed and closed (and
        the audio pair's)."""
        with self._lock:
            self._close()

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
        self._stopped = True
        files, self._files = self._files, None
        if files is not None:
            for file in files:
                try:
                    file.close()
                except Exception as error:
                    log.debug("Closing the capture failed (%s).", type(error).__name__)
        if self._audio is not None:  # only ever written by this (the reading) thread
            self._audio.close()

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
