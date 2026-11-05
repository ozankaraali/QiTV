"""Recordings' thumbnails (2.9.7): a frame of each recording as its tile's picture.

A recording is an MPEG-TS file; libVLC (the bundled one, no native build) reads one frame of
it on a worker thread: muted, no window (its frames are drawn into memory), software decoding,
from a fifth of the way in (five minutes at most, so a programme's pre-roll is passed). The
frame is kept as a small JPEG in the cache folder, named after the recording's path, and made
once: a recording that gives no frame is not read again this run. One at a time, only those a
view asks for (`url`), so the Recordings tab never decodes a folder at once.

Tiles show it through the image cache's own loader (services.image_loader): `url` gives
"thumb:<name>" once the picture exists (until then ""), and `file_of` resolves such a name in
this folder only. The GUI thread never touches the disk here: the worker answers whether a
thumbnail exists (`ready`) and makes it if not.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import queue
import threading
from typing import Any, Callable, Optional
import unicodedata

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

logger = logging.getLogger(__name__)

SCHEME = "thumb:"
FOLDER = "recording-thumbs"  # in the cache folder (the config folder's "cache")
WIDTH, HEIGHT = 384, 216
QUALITY = 82
SMALLEST = 2 * 1024 * 1024  # bytes: a recording just begun has no picture worth taking yet
FRAMES = 6  # the frame kept: a few after the seek, past the first (often grey) ones
TIMEOUT_S = 8.0
STEP_S = 0.1  # how often the reading looks whether the file ended
INSTANCE_ARGS = (
    "--quiet",
    "--no-audio",
    "--codec=avcodec,any",
    "--avcodec-hw=none",
    "--no-stats",
    "--no-sub-autodetect-file",
    "--no-spu",
    "--no-osd",
    "--no-video-title-show",
    "--no-lua",
)

_folder: Optional[str] = None  # where `file_of` looks (set by the Thumbnails in use)


def name_of(path: str) -> str:
    """The thumbnail's file name for the recording at `path`."""
    key = os.path.normcase(os.path.normpath(unicodedata.normalize("NFC", path)))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".jpg"


def file_of(url: str) -> Optional[str]:
    """The thumbnail file a "thumb:" url names, in the thumbnails' folder only (None for any
    other url, or a name that isn't there). On the image loader's thread."""
    if not url.startswith(SCHEME) or _folder is None:
        return None
    name = url[len(SCHEME) :]
    if not name or name != os.path.basename(name) or name.startswith("."):
        return None
    path = os.path.join(_folder, name)
    return path if os.path.isfile(path) else None


def moment_of(item: dict) -> float:
    """Where in the recording its picture is taken: a fifth in, at most five minutes (a
    minute in when its length isn't known)."""
    start, stop = item.get("_start"), item.get("_stop")
    if start and stop and float(stop) > float(start):
        return round(min(300.0, (float(stop) - float(start)) / 5), 1)
    return 60.0


class Thumbnails(QObject):
    ready = Signal(str)  # a thumbnail's url (made, or found made before)
    failed = Signal(str)  # a recording's path that gave no picture

    def __init__(
        self,
        folder: str,
        grab: Optional[Callable[[str, float], Optional[QImage]]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        global _folder
        _folder = folder
        self._folder = folder
        self._grab = grab or grab_frame
        self._made: set[str] = set()  # names known to exist
        self._asked: set[str] = set()  # paths asked for (made, failed or under way)
        self._jobs: "queue.Queue[Optional[tuple[str, float]]]" = queue.Queue()
        self._closed = False
        self._worker: Optional[threading.Thread] = None
        self._found.connect(self._on_found)
        self._lost.connect(self._on_lost)
        self._small.connect(self._on_small)

    # signals from the worker, to the GUI thread
    _found = Signal(str)  # a name made or there before
    _lost = Signal(str)  # a path that gave no picture
    _small = Signal(str)  # a path too short to read yet: asked again later

    def url(self, item: dict) -> str:
        """The picture of the recording `item` ("" until there is one: then it's asked for,
        and `ready` says when)."""
        path = str(item.get("path") or "")
        if not path or self._closed:
            return ""
        name = name_of(path)
        if name in self._made:
            return SCHEME + name
        if path not in self._asked:
            self._asked.add(path)
            self._jobs.put((path, moment_of(item)))
            self._start()
        return ""

    def shutdown(self, wait: bool = False) -> None:
        """Nothing more is read; `wait`: until the worker is done (tests; a frame under way
        takes up to TIMEOUT_S, so the app doesn't wait)."""
        self._closed = True
        self._jobs.put(None)
        if wait and self._worker is not None:
            self._worker.join(TIMEOUT_S + 2)

    def _start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, name="recording-thumbs", daemon=True)
            self._worker.start()

    def _on_found(self, name: str) -> None:
        self._made.add(name)
        self.ready.emit(SCHEME + name)

    def _on_lost(self, path: str) -> None:
        self.failed.emit(path)

    def _on_small(self, path: str) -> None:
        self._asked.discard(path)  # its next view asks again

    # ---- the worker ---------------------------------------------------------------------

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None or self._closed:
                return
            path, at = job
            name = name_of(path)
            target = os.path.join(self._folder, name)
            try:
                if os.path.isfile(target):
                    self._found.emit(name)
                    continue
                if os.path.getsize(path) < SMALLEST:
                    self._small.emit(path)
                    continue
                image = self._grab(path, at)
                if (image is None or image.isNull()) and at > 0 and not self._closed:
                    image = self._grab(path, 0.0)  # shorter than its slot (stopped early)
                if image is None or image.isNull() or self._closed:
                    self._lost.emit(path)
                    continue
                os.makedirs(self._folder, exist_ok=True)
                partial = target + ".part"
                if not image.save(partial, "JPG", QUALITY):
                    self._lost.emit(path)
                    continue
                os.replace(partial, target)
                self._found.emit(name)
            except Exception as e:  # a file gone or unreadable: no picture for it
                logger.info("A recording's picture could not be made: %s", type(e).__name__)
                self._lost.emit(path)


def grab_frame(path: str, at: float, vlc_module: Any = None) -> Optional[QImage]:
    """A frame of the recording at `path`, `at` seconds in, WIDTH × HEIGHT (None when none
    came within TIMEOUT_S). Muted and with no window: libVLC draws into memory."""
    if vlc_module is None:
        try:
            import vlc as bundled
        except Exception:  # no libVLC
            return None
        vlc_module = bundled
    vlc = vlc_module
    pitch = WIDTH * 4
    buffer = ctypes.create_string_buffer(pitch * HEIGHT)
    done = threading.Event()
    seen = [0]
    kept: list[bytes] = []

    @vlc.CallbackDecorators.VideoLockCb
    def lock(_opaque, planes):
        planes[0] = ctypes.cast(buffer, ctypes.c_void_p).value
        return None

    @vlc.CallbackDecorators.VideoUnlockCb
    def unlock(_opaque, _picture, _planes):
        return None

    @vlc.CallbackDecorators.VideoDisplayCb
    def display(_opaque, _picture):
        seen[0] += 1
        if seen[0] >= FRAMES and not done.is_set():
            kept.append(bytes(buffer.raw))
            done.set()

    instance = vlc.Instance(list(INSTANCE_ARGS))
    if instance is None:
        return None
    player = instance.media_player_new()
    try:
        player.video_set_callbacks(lock, unlock, display, None)
        player.video_set_format("RV32", WIDTH, HEIGHT, pitch)
        media = instance.media_new_path(path)
        media.add_option(f":start-time={max(0.0, at):.1f}")
        media.add_option(":no-audio")
        player.set_media(media)
        media.release()
        player.play()
        ended = (vlc.State.Ended, vlc.State.Error, vlc.State.Stopped)
        waited = 0.0
        while not done.wait(STEP_S) and waited < TIMEOUT_S:
            waited += STEP_S
            if player.get_state() in ended:  # past its end, or unreadable: no frame
                break
    finally:
        player.stop()
        player.release()
        instance.release()
    if not kept:
        return None
    return QImage(kept[0], WIDTH, HEIGHT, pitch, QImage.Format.Format_RGB32).copy()
