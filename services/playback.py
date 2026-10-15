"""One libVLC media player, polled on the GUI thread (no VLC callbacks cross threads)."""

from __future__ import annotations

from enum import Enum
import logging
import platform
from typing import Optional

from PySide6.QtCore import QElapsedTimer, QObject, QTimer, Signal

from services.vlc_profile import PlayKind, media_options

logger = logging.getLogger(__name__)

try:
    import vlc
except (ImportError, OSError) as e:  # libVLC missing or broken
    vlc = None
    logger.warning("libVLC unavailable: %s", e)

_STALL_AUDIO_RETRIES = 2


class PlaybackState(str, Enum):
    IDLE = "idle"
    OPENING = "opening"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"
    ENDED = "ended"
    ERROR = "error"


def create_instance(extra_args: Optional[list[str]] = None):
    if vlc is None:
        return None
    try:
        return vlc.Instance(extra_args or ["--no-video-title-show", "--quiet"])
    except Exception as e:
        logger.warning("Could not create a libVLC instance: %s", e)
        return None


def _map_state(state) -> PlaybackState:
    mapping = {
        vlc.State.Opening: PlaybackState.OPENING,
        vlc.State.Buffering: PlaybackState.BUFFERING,
        vlc.State.Playing: PlaybackState.PLAYING,
        vlc.State.Paused: PlaybackState.PAUSED,
        vlc.State.Ended: PlaybackState.ENDED,
        vlc.State.Error: PlaybackState.ERROR,
    }
    return mapping.get(state, PlaybackState.IDLE)


class PlaybackSession(QObject):
    stateChanged = Signal(str)
    positionChanged = Signal(int, int)
    errorOccurred = Signal(str)
    ended = Signal()
    seekableChanged = Signal(bool)

    def __init__(
        self,
        instance,
        parent: Optional[QObject] = None,
        *,
        poll_ms: int = 250,
        open_timeout_ms: int = 20000,
        stall_ms: int = 6000,
    ):
        super().__init__(parent)
        self._instance = instance
        self._player = instance.media_player_new()
        self._player.video_set_mouse_input(False)
        self._player.video_set_key_input(False)
        self._state = PlaybackState.IDLE
        self._url: Optional[str] = None
        self._kind: Optional[PlayKind] = None
        self._options: list[str] = []
        self._start_ms: Optional[int] = None
        self._seekable = False
        self._ever_played = False
        self._open_timeout_ms = open_timeout_ms
        self._stall_ms = stall_ms
        self._opened = QElapsedTimer()
        self._stall_clock = QElapsedTimer()
        self._last_time = -1
        self._stalls = 0
        self._restarted = False
        self._poll = QTimer(self, interval=poll_ms)
        self._poll.timeout.connect(self._tick)

    # --- properties -------------------------------------------------------------------
    @property
    def url(self) -> Optional[str]:
        return self._url

    @property
    def kind(self) -> Optional[PlayKind]:
        return self._kind

    def state(self) -> PlaybackState:
        return self._state

    def time_ms(self) -> int:
        return max(0, int(self._player.get_time() or 0))

    def length_ms(self) -> int:
        return max(0, int(self._player.get_length() or 0))

    def is_seekable(self) -> bool:
        return bool(self._player.is_seekable())

    # --- output -----------------------------------------------------------------------
    def set_window(self, win_id: int) -> None:
        system = platform.system()
        if system == "Darwin":
            self._player.set_nsobject(int(win_id))
        elif system == "Windows":
            self._player.set_hwnd(int(win_id))
        else:
            self._player.set_xwindow(int(win_id))

    # --- control ----------------------------------------------------------------------
    def play(
        self,
        url: str,
        *,
        kind: PlayKind,
        latency: str = "normal",
        user_agent: Optional[str] = None,
        start_ms: Optional[int] = None,
    ) -> None:
        self._url, self._kind = url, kind
        self._options = media_options(kind, latency=latency, user_agent=user_agent)
        self._start_ms = start_ms if start_ms and start_ms > 0 else None
        self._stalls, self._restarted = 0, False
        self._start_media()

    def _start_media(self) -> None:
        media = self._instance.media_new(self._url)
        for option in self._options:
            media.add_option(option)
        self._player.set_media(media)
        media.release()
        self._seekable = False
        self._ever_played = False
        self._last_time = -1
        self._opened.start()
        self._stall_clock.start()
        self._set_state(PlaybackState.OPENING)
        if self._player.play() == -1:
            self._fail("VLC could not start playback")
            return
        self._poll.start()

    def stop(self) -> None:
        self._poll.stop()
        self._player.stop()
        self._set_state(PlaybackState.IDLE)

    def toggle_pause(self) -> None:
        self._player.pause()

    def set_paused(self, paused: bool) -> None:
        self._player.set_pause(1 if paused else 0)

    def seek(self, ms: int) -> None:
        if self.is_seekable():
            self._player.set_time(max(0, int(ms)))

    def seek_relative(self, ms: int) -> None:
        self.seek(self.time_ms() + ms)

    def set_rate(self, rate: float) -> None:
        self._player.set_rate(float(rate))

    def volume(self) -> int:
        return max(0, int(self._player.audio_get_volume()))

    def set_volume(self, value: int) -> None:
        self._player.audio_set_volume(max(0, min(100, int(value))))

    def toggle_mute(self) -> bool:
        muted = not bool(self._player.audio_get_mute())
        self._player.audio_set_mute(muted)
        return muted

    def audio_tracks(self) -> list[tuple[int, str]]:
        descriptions = self._player.audio_get_track_description() or []
        return [(int(i), self._decode(n)) for i, n in descriptions]

    def set_audio_track(self, track_id: int) -> None:
        self._player.audio_set_track(int(track_id))

    def subtitle_tracks(self) -> list[tuple[int, str]]:
        descriptions = self._player.video_get_spu_description() or []
        return [(int(i), self._decode(n)) for i, n in descriptions]

    def set_subtitle_track(self, track_id: int) -> None:
        self._player.video_set_spu(int(track_id))

    @staticmethod
    def _decode(name) -> str:
        return name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)

    def release(self) -> None:
        self._poll.stop()
        try:
            self._player.stop()
            self._player.release()
        except Exception as e:
            logger.debug("Releasing the media player failed: %s", e)

    # --- polling ----------------------------------------------------------------------
    def _set_state(self, state: PlaybackState) -> None:
        if state is not self._state:
            self._state = state
            self.stateChanged.emit(state.value)

    def _fail(self, message: str) -> None:
        self._poll.stop()
        self._set_state(PlaybackState.ERROR)
        self.errorOccurred.emit(message)

    def _tick(self) -> None:
        state = _map_state(self._player.get_state())
        if state is PlaybackState.ERROR:
            self._fail("The stream could not be played")
            return
        if state is PlaybackState.ENDED:
            if not self._ever_played:
                self._fail("The stream could not be opened")
                return
            self._poll.stop()
            self._set_state(PlaybackState.ENDED)
            self.ended.emit()
            return
        if state is PlaybackState.IDLE and self._state is PlaybackState.OPENING:
            state = PlaybackState.OPENING  # libVLC reports NothingSpecial briefly after play()
        if state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            self._ever_played = True
        self._set_state(state)
        if state in (PlaybackState.OPENING, PlaybackState.BUFFERING) and not self._ever_played:
            if self._opened.elapsed() > self._open_timeout_ms:
                self._fail("Timed out opening the stream")
            return
        seekable = self.is_seekable()
        if seekable != self._seekable:
            self._seekable = seekable
            self.seekableChanged.emit(seekable)
        if self._start_ms is not None and seekable and state is PlaybackState.PLAYING:
            self._player.set_time(self._start_ms)
            self._start_ms = None
        if state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            self.positionChanged.emit(self.time_ms(), self.length_ms())
        self._watch_stall(state)

    def _watch_stall(self, state: PlaybackState) -> None:
        if self._kind not in (PlayKind.LIVE, PlayKind.UNKNOWN):
            return
        current = self.time_ms()
        if (
            state not in (PlaybackState.PLAYING, PlaybackState.BUFFERING)
            or current != self._last_time
        ):
            self._last_time = current
            self._stall_clock.restart()
            return
        if self._stall_clock.elapsed() < self._stall_ms:
            return
        self._stall_clock.restart()
        self._stalls += 1
        if self._stalls <= _STALL_AUDIO_RETRIES:
            logger.warning("Playback stalled (attempt %d); cycling the audio track", self._stalls)
            track = self._player.audio_get_track()
            self._player.audio_set_track(-1)
            QTimer.singleShot(500, lambda: self._player.audio_set_track(track))
        elif not self._restarted:
            logger.warning("Playback stalled again; restarting the stream")
            self._restarted = True
            self._player.stop()
            self._start_media()
