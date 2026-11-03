"""The sound of a stream as 16 kHz mono samples, decoded by QiTV's own libVLC (2.9, AI captions).

Runs in the captions process. The reader thread `feed()`s the stream's bytes as they come from
the helper's loopback server; libVLC reads them through media callbacks (no second upstream
connection, no file) and its stream output decodes the sound alone, as fast as the bytes
arrive (not at the pace of a player: measured 75x real time on a file):

    #transcode{vcodec=none,acodec=s16l,channels=1,samplerate=16000}:smem{...,no-time-sync}

smem hands each decoded block to `on_block(vlc_us, pcm)` (signed 16-bit little-endian
samples, 16 kHz, mono) on a libVLC thread, stamped on VLC's clock (services.captions.
stream_clock ties that to the stream). `audio_track` names the ES (the PID in a TS) to decode,
the viewer's sound track; None takes libVLC's default.
"""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

RATE = 16_000
_UNKNOWN_SIZE = (1 << 64) - 1

_PRERENDER = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t
)
_POSTRENDER = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.c_uint,
    ctypes.c_size_t,
    ctypes.c_int64,
)


class AudioDecoder:
    """Bytes in, timestamped 16 kHz mono sound out, until `stop()`."""

    def __init__(
        self,
        on_block: Callable[[int, bytes], None],
        *,
        audio_track: Optional[int] = None,
        vlc_module: Any = None,
    ) -> None:
        if vlc_module is None:
            import vlc  # the caller loaded the bundled libVLC first

            vlc_module = vlc
        self._vlc = vlc_module
        self._on_block = on_block
        self._audio_track = audio_track
        self._lock = threading.Condition()
        self._pending = bytearray()
        self._ended = False
        self._stopped = False
        self._buffers: dict[int, Any] = {}
        self._instance: Any = None
        self._player: Any = None
        self._media: Any = None
        # Kept alive as long as libVLC may call them.
        self._callbacks = self._make_callbacks()

    # ---- the reader's side -----------------------------------------------------------
    def feed(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._stopped:
                return
            self._pending += data
            self._lock.notify_all()

    def end(self) -> None:
        """No more bytes: libVLC decodes what it has and ends."""
        with self._lock:
            self._ended = True
            self._lock.notify_all()

    # ---- life ------------------------------------------------------------------------
    def start(self) -> None:
        vlc = self._vlc
        self._instance = vlc.Instance(
            [
                "--quiet",
                "--no-video",
                "--aout=dummy",
                "--no-stats",
                "--no-sub-autodetect-file",
                "--no-lua",
                "--no-media-library",
            ]
        )
        open_cb, read_cb, seek_cb, close_cb, pre, post = self._callbacks
        self._media = self._instance.media_new_callbacks(open_cb, read_cb, seek_cb, close_cb, None)
        address = lambda f: ctypes.cast(f, ctypes.c_void_p).value  # noqa: E731
        self._media.add_option(
            ":sout=#transcode{vcodec=none,scodec=none,acodec=s16l,channels=1,samplerate="
            f"{RATE}}}:smem{{audio-prerender-callback={address(pre)},"
            f"audio-postrender-callback={address(post)},no-time-sync}}"
        )
        for option in (":no-sout-video", ":no-sout-spu", ":no-sout-smem-time-sync", ":demux=ts"):
            self._media.add_option(option)
        if self._audio_track is not None:
            self._media.add_option(f":audio-track-id={int(self._audio_track)}")
        self._player = self._instance.media_player_new()
        self._player.set_media(self._media)
        self._player.play()

    def finished(self) -> bool:
        """libVLC ended: the bytes ended (`end()`), or it gave up on them."""
        if self._player is None:
            return False
        state = self._player.get_state()
        vlc = self._vlc
        return state in (vlc.State.Ended, vlc.State.Error, vlc.State.Stopped)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._lock.notify_all()
        player, self._player = self._player, None
        if player is not None:
            player.stop()
            player.release()
        if self._media is not None:
            self._media.release()
            self._media = None
        if self._instance is not None:
            self._instance.release()
            self._instance = None
        self._buffers.clear()

    # ---- libVLC's callbacks (its threads) ----------------------------------------------
    def _make_callbacks(self) -> tuple[Any, ...]:
        cb = self._vlc.CallbackDecorators

        @cb.MediaOpenCb
        def open_cb(opaque: Any, datap: Any, sizep: Any) -> int:
            datap[0] = None
            sizep[0] = _UNKNOWN_SIZE
            return 0

        @cb.MediaReadCb
        def read_cb(opaque: Any, buf: Any, length: int) -> int:
            with self._lock:
                while not self._pending and not self._ended and not self._stopped:
                    self._lock.wait(0.5)
                if self._stopped:
                    return -1
                if not self._pending:
                    return 0  # the end of the stream
                n = min(length, len(self._pending))
                ctypes.memmove(buf, bytes(self._pending[:n]), n)
                del self._pending[:n]
                return n

        @cb.MediaSeekCb
        def seek_cb(opaque: Any, offset: int) -> int:
            return -1  # a live stream: no seeking

        @cb.MediaCloseCb
        def close_cb(opaque: Any) -> None:
            return None

        @_PRERENDER
        def prerender(data: Any, pp: Any, size: int) -> None:
            block = ctypes.create_string_buffer(max(1, size))
            address = ctypes.addressof(block)
            self._buffers[address] = block
            pp[0] = address

        @_POSTRENDER
        def postrender(
            data: Any,
            pcm: Any,
            channels: int,
            rate: int,
            samples: int,
            bits: int,
            size: int,
            pts: int,
        ) -> None:
            block = self._buffers.pop(pcm or 0, None)
            if block is None or self._stopped:
                return
            if channels != 1 or rate != RATE or bits != 16:
                logger.warning(
                    "AI captions: libVLC gave %d channels at %d Hz, %d bits", channels, rate, bits
                )
                return
            try:
                self._on_block(int(pts), bytes(block.raw[: int(size)]))
            except Exception as error:  # a callback must never raise into libVLC
                logger.warning("AI captions: a sound block failed: %s", error)

        return open_cb, read_cb, seek_cb, close_cb, prerender, postrender
