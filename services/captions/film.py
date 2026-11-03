"""A film's sound for AI captions (2.9): QiTV's own libVLC plays it alone, a little ahead of
the picture, and says where in the film each moment of it is.

A film has no time-shift helper and is read whole, so the live route (services.captions.
decoder: the converter, as fast as bytes come, tied to the stream's own stamps) does not fit:
the converter reports no clock (`get_time()` stays 0), an MP4 seek there lands up to a GOP
early and an MKV start time is ignored (measured with 3.0.20). Played normally (no picture shown,
the sound into memory through libVLC's audio callbacks, in real time), the player's own clock
works: `FilmReader` opens the film at `start_s`, a few seconds past the picture, so the
captions come ahead of it (its video chosen but never decoded: INSTANCE_ARGS), and places each
block of sound at

    the player's time now + how long until the block plays (its pts against libvlc_clock)

kept as one offset between VLC's clock and the film's, from the median of many blocks (VLC's
time steps a little). A pause or flush starts the estimate again. `on_sound(at, samples)` gets
16 kHz mono floats with the film second they start at, on libVLC's thread.
"""

from __future__ import annotations

import ctypes
import logging
import statistics
import threading
from array import array
from collections.abc import Callable
from typing import Any, Optional

logger = logging.getLogger(__name__)

RATE = 16_000
_ESTIMATE_STEPS = 3  # fresh readings of VLC's time the first offset is the median of
_KEEP = 31  # the running estimate's window
_BEFORE_SEEK_S = 2.0  # a time this far before the start: the seek has not happened yet
_OUTLIER_S = 0.3
# The picture stays chosen, never decoded (each frame skipped) nor shown (a dummy output): an
# MKV's seek points are its video's (Cues), and without a video track libVLC 3.0.20 reaches
# `start_s` by reading the film from its start (2.9: 2.4 GB for a film resumed 27 minutes
# in, through the film relay's window: no caption ever came). libavcodec decodes, so the skip
# holds where a hardware decoder (VideoToolbox) would have taken the video.
INSTANCE_ARGS = (
    "--quiet",
    "--vout=dummy",
    "--codec=avcodec,any",
    "--avcodec-hw=none",
    "--avcodec-skip-frame=4",
    "--avcodec-skip-idct=4",
    "--no-stats",
    "--no-sub-autodetect-file",
    "--no-lua",
)


class FilmReader:
    """Plays a film's sound into memory from `start_s` until `stop()`."""

    def __init__(
        self,
        location: str,
        start_s: float,
        on_sound: Callable[[float, array], None],
        *,
        audio_track: Optional[int] = None,
        options: tuple[str, ...] = (),
        vlc_module: Any = None,
    ) -> None:
        if vlc_module is None:
            import vlc

            vlc_module = vlc
        self._vlc = vlc_module
        self._location = location
        self._start_s = max(0.0, float(start_s))
        self._on_sound = on_sound
        self._audio_track = audio_track
        self._options = options
        self._lock = threading.Lock()
        self._offsets: list[float] = []  # film second minus VLC's clock (s), per block
        self._offset: Optional[float] = None
        self._last_ms: Optional[int] = None  # VLC's time at the last block: it steps
        self._stopped = False
        self._paused = False
        self._instance: Any = None
        self._player: Any = None
        self._callbacks = self._make_callbacks()

    def start(self) -> None:
        vlc = self._vlc
        self._instance = vlc.Instance(list(INSTANCE_ARGS))
        if "://" in self._location:
            media = self._instance.media_new_location(self._location)
        else:
            media = self._instance.media_new_path(self._location)
        media.add_option(f":start-time={self._start_s:.3f}")
        for option in (":no-spu", *self._options):
            media.add_option(option)
        if self._audio_track is not None:
            media.add_option(f":audio-track-id={int(self._audio_track)}")
        player = self._instance.media_player_new()
        play, pause, resume, flush, drain = self._callbacks
        player.audio_set_callbacks(play, pause, resume, flush, drain, None)
        player.audio_set_format("S16N", RATE, 1)
        player.set_media(media)
        media.release()
        self._player = player
        player.play()

    def hold(self, paused: bool) -> None:
        """Pause (far enough ahead of the picture) or play on."""
        player = self._player
        if player is not None and paused != self._paused:
            self._paused = paused
            player.set_pause(1 if paused else 0)

    def finished(self) -> bool:
        player = self._player
        if player is None:
            return False
        vlc = self._vlc
        return player.get_state() in (vlc.State.Ended, vlc.State.Error)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
        player, self._player = self._player, None
        if player is not None:
            player.stop()
            player.release()
        if self._instance is not None:
            self._instance.release()
            self._instance = None

    # ---- where a block is in the film ------------------------------------------------
    def _place(self, pts_us: int) -> Optional[float]:
        """The film second a block VLC plays at `pts_us` (its clock) starts at. VLC's time
        moves in steps (a quarter to half a second): only a reading that just stepped is
        fresh, so only those make the offset."""
        player = self._player
        if player is None:
            return None
        now_ms = player.get_time()
        if now_ms is None or now_ms < 0 or now_ms / 1000.0 < self._start_s - _BEFORE_SEEK_S:
            return self._known(pts_us)
        stepped = self._last_ms is not None and now_ms != self._last_ms
        self._last_ms = now_ms
        if stepped:
            until = (pts_us - int(self._vlc.libvlc_clock())) / 1e6  # how long until it plays
            offset = now_ms / 1000.0 + until - pts_us / 1e6
            with self._lock:
                if self._offset is None or abs(offset - self._offset) <= _OUTLIER_S:
                    self._offsets.append(offset)
                    del self._offsets[:-_KEEP]
                    if len(self._offsets) >= _ESTIMATE_STEPS:
                        self._offset = statistics.median(self._offsets)
        return self._known(pts_us)

    def _known(self, pts_us: int) -> Optional[float]:
        with self._lock:
            return None if self._offset is None else pts_us / 1e6 + self._offset

    def _restart_estimate(self) -> None:
        with self._lock:
            self._offsets.clear()
            self._offset = None
            self._last_ms = None

    def _make_callbacks(self) -> tuple[Any, ...]:
        cb = self._vlc.CallbackDecorators

        @cb.AudioPlayCb
        def play(data: Any, samples: Any, count: int, pts: int) -> None:
            if self._stopped or not samples or count <= 0:
                return
            try:
                at = self._place(int(pts))
                if at is None:
                    return
                ints = array("h")
                ints.frombytes(ctypes.string_at(samples, int(count) * 2))
                floats = array("f", [v / 32768.0 for v in ints])
                self._on_sound(at, floats)
            except Exception as error:  # never into libVLC
                logger.warning("AI captions: a film's sound block failed: %s", error)

        @cb.AudioPauseCb
        def pause(data: Any, pts: int) -> None:
            self._restart_estimate()

        @cb.AudioResumeCb
        def resume(data: Any, pts: int) -> None:
            self._restart_estimate()

        @cb.AudioFlushCb
        def flush(data: Any, pts: int) -> None:
            self._restart_estimate()

        @cb.AudioDrainCb
        def drain(data: Any) -> None:
            return None

        return play, pause, resume, flush, drain
