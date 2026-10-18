"""One libVLC media player, polled on the GUI thread (no VLC callbacks cross threads).

Ending an input (set_media while one plays, stop, release) joins libVLC's input thread,
and on macOS that join can wait for the main thread: until a stream's first picture is
shown, its decoder waits for the video output, whose Open dispatch_syncs onto the main
queue (caopengllayer). Joined from the GUI thread, that is a deadlock: the first zap of
CI run 38675064690 hung in libvlc_media_player_set_media. So the session ends inputs on a
worker (_PlayerJob), never touches the player's input lock from the GUI thread while one
runs, and opens the newest media once it has finished. Every platform takes this path:
the GUI thread also no longer waits up to ~100 ms for a join on each zap.

Mute and volume live on libVLC's audio output, which a stop closes; the next media opens a
new one once its audio decodes, which can be after the Playing state. Meanwhile libVLC drops
audio_set_mute and audio_set_volume (measured with 3.0.20), so the session keeps what it was
asked for and applies it again once each media's output is open: it looks every
_AUDIO_POLL_MS for _AUDIO_WATCH_MS, then at each regular poll while playing. Without an output
`volume()` reports what was asked for, else the last level an open output reported.

With hardware decoding (Settings; `hardware`), each tick while a stream plays reads libVLC's
media statistics: once its picture stood still while its sound played (`FrozenPicture`, which
has the lab) where pictures can show (`_can_show`: on a window system, or once VLC made a video
output for one of this player's media), the stream decodes in software from then on (`software_options()`, media options
that override the instance's decoder), `pictureFroze` follows, and a stream other than a
time-shift one opens again where it plays (a film at its position, a live stream live). A
time-shift stream's position is LiveSession's: it reopens it. Each seek, the resume one
included, gives the decoder a first picture's time again. The same URL opened again in this
player (a reconnect, a retry) decodes in software too; `play(software=True)` asks for it (a
channel that froze before).
"""

from __future__ import annotations

from enum import Enum
import logging
import os
import platform
import time
from typing import Any, Callable, Optional, Protocol

from PySide6.QtCore import QElapsedTimer, QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from services.jobs import JobRunner
from services.picture_watch import Counters, FrozenPicture
from services.vlc_profile import PlayKind, media_options

logger = logging.getLogger(__name__)

try:
    import vlc
except (ImportError, OSError) as e:  # libVLC missing or broken
    vlc = None
    logger.warning("libVLC unavailable: %s", e)

_STALL_AUDIO_RETRIES = 2
_AUDIO_RESTORE_MS = 500
_RESTART_DELAY_MS = 300
_RESUME_GIVE_UP_MS = 3000
_RESUME_END_MARGIN_MS = 1000
_STALL_BUDGET_RESET_MS = 30000  # healthy playback after a stall refills the retry budget
_AUDIO_POLL_MS = 30  # how often to look for the audio output while mute/volume wait for it
_AUDIO_WATCH_MS = 5000  # then only the regular poll looks (media without audio never opens one)
_DEFAULT_VOLUME = 100  # libVLC's, until a level is read from an open audio output
NO_VLC_MESSAGE = "Video needs VLC: install it from videolan.org and restart QiTV"
# A file for libVLC's verbose log (the smoke test sets it). For debugging only: libVLC logs
# every stream URL, and provider URLs can carry credentials.
VLC_LOG_ENV = "QITV_VLC_LOG"


class PlaybackState(str, Enum):
    IDLE = "idle"
    OPENING = "opening"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"
    ENDED = "ended"
    ERROR = "error"


def decoder_args(hardware: bool) -> list[str]:
    """VLC's video decoder: VLC's own choice with `hardware` (Settings; the default again
    since 2.1, when VLC takes software where the graphics chip can't), else software.

    2.0.5 made software the default: the Mac's hardware decoder (VideoToolbox) had shown
    about half the pictures of an interlaced TV channel and frozen it at times. Since 2.1 a
    stream whose picture freezes while its sound plays goes back to software by itself
    (`services.picture_watch`, `software_options()`), and a lab in a real window showed full
    frame rates with less CPU. `--no-videotoolbox` exists only where VLC has that module (an
    unknown option fails).
    """
    if hardware:
        return []
    args = ["--avcodec-hw=none"]
    if platform.system() == "Darwin":
        args.append("--no-videotoolbox")
    return args


# Qt platforms without a window system: VLC can make no video output there.
_HEADLESS_PLATFORMS = ("offscreen", "minimal")
# Set to "1" for the smoke test (main.SMOKE_ENV; CI's packaged smoke and scripts/smoke_tv.py).
SMOKE_ENV = "QITV_SMOKE"


def display_exists() -> bool:
    """The app runs on a window system VLC can draw into: a GUI application on any Qt
    platform but a headless one (the offscreen smoke, CI's unit tests)."""
    if not isinstance(QGuiApplication.instance(), QGuiApplication):
        return False
    return QGuiApplication.platformName() not in _HEADLESS_PLATFORMS


def software_options() -> list[str]:
    """Media options that decode one media in software on an instance that decodes in
    hardware: `decoder_args(False)` as media options, which override the instance's."""
    return [":" + arg[2:] for arg in decoder_args(hardware=False)]


def default_args(hardware_decoding: bool = False) -> list[str]:
    """The app's libVLC arguments: quiet, or a verbose log file when VLC_LOG_ENV names one."""
    # VLC 3's volume-save keeps the last level set on any audio output in the process's
    # config, and every output opened later starts there: the PiP player's 30 became the
    # large picture's. Each player is given its own level instead (SessionManager).
    args = ["--no-video-title-show", "--no-volume-save", *decoder_args(hardware_decoding)]
    if platform.system() == "Windows":
        # VLC 3's default Windows output, mmdevice, sets volume and mute on the process's
        # audio session (ISimpleAudioVolume), which every player's stream joins: the main
        # and PiP players would mute and level each other. DirectSound sets them on each
        # player's own buffer. (Callers passing their own arguments keep their output.)
        args.append("--aout=directsound")
    log = os.environ.get(VLC_LOG_ENV)
    if not log:
        return [*args, "--quiet"]
    logger.warning("libVLC logs verbosely to %s (%s); it records stream URLs", log, VLC_LOG_ENV)
    return [*args, "--verbose=2", "--file-logging", f"--logfile={log}"]


def create_instance(extra_args: Optional[list[str]] = None):
    if vlc is None:
        return None
    try:
        return vlc.Instance(extra_args or default_args())
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


class _Job(Enum):
    DETACH = "detach"  # set_media(None): end the input; libVLC keeps the video output for reuse
    STOP = "stop"  # stop() and set_media(None): end the input, close the outputs


class _PlayerJob(QObject):
    """Ends the player's input on a worker thread (see the module docstring)."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, player: Any, job: _Job) -> None:
        super().__init__()
        self._player = player
        self._job = job

    def run(self) -> None:
        if self._job is _Job.STOP:
            self._player.stop()
        self._player.set_media(None)
        self.finished.emit(None)


class PlaybackSession(QObject):
    stateChanged = Signal(str)
    positionChanged = Signal(int, int)
    errorOccurred = Signal(str)
    ended = Signal()
    seekableChanged = Signal(bool)
    # libVLC let go of its input after a stop: the stream's connection is closed. Only
    # while holds_input() is True after a stop() does one follow.
    inputEnded = Signal()
    # Hardware decoding froze the picture while the sound played: the stream decodes in
    # software now (reopened, unless it is a time-shift stream; see the module docstring).
    pictureFroze = Signal()

    def __init__(
        self,
        instance,
        parent: Optional[QObject] = None,
        *,
        poll_ms: int = 250,
        open_timeout_ms: int = 20000,
        stall_ms: int = 6000,
        runner: Optional[Any] = None,
        hardware: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ):
        """`hardware`: `instance` decodes in hardware (Settings): a frozen picture falls back
        to software (module docstring)."""
        super().__init__(parent)
        self._runner = runner if runner is not None else JobRunner(self)
        self._instance = instance
        self._hardware = hardware
        self._clock = clock
        self._software = False  # this media decodes in software
        self._froze_url: Optional[str] = None  # the stream whose picture froze here
        self._frozen = FrozenPicture()
        self._had_vout = False  # VLC made a video output for one of this player's media
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
        self._since_stall = QElapsedTimer()  # started at each counted stall
        self._poll = QTimer(self, interval=poll_ms)
        self._poll.timeout.connect(self._tick)
        self._released = False
        self._playing_clock = QElapsedTimer()
        self._pending_track: Optional[int] = None
        self._restore_timer = QTimer(self, singleShot=True, interval=_AUDIO_RESTORE_MS)
        self._restore_timer.timeout.connect(self._restore_audio)
        self._restart_timer = QTimer(self, singleShot=True, interval=_RESTART_DELAY_MS)
        self._restart_timer.timeout.connect(self._restart_stream)
        # Ending inputs off the GUI thread (module docstring):
        self._job: Optional[_Job] = None  # running: the GUI thread leaves the player alone
        self._job_token = 0  # the running job's; a completion with another one is stale
        self._has_input = False  # libVLC holds an input that set_media/stop would join
        self._engaged = False  # an input or a kept video output may remain: a stop is due
        self._start_wanted = False  # open self._url once the job has finished
        self._stop_owed = False  # stop() came during a DETACH: stop once it has finished
        self._rate: Optional[float] = None  # asked for during a job: set before the next play
        # Asked for by the user (None: never): applied again once each media's audio opens.
        self._mute_wanted: Optional[bool] = None
        self._volume_wanted: Optional[int] = None
        self._audio_due = False  # the wanted mute/volume still to be applied to this media
        self._volume_seen = _DEFAULT_VOLUME  # the last level read from an open audio output
        self._audio_timer = QTimer(self, singleShot=True, interval=_AUDIO_POLL_MS)
        self._audio_timer.timeout.connect(self._look_for_audio)
        self._audio_watch = QElapsedTimer()  # since the wanted audio began waiting

    def _player_free(self) -> bool:
        """The GUI thread may call into libVLC: not released, and no job holds its input lock.

        libvlc_media_player_set_media and _stop hold the player's input lock while they join;
        get_time, is_seekable, set_rate and most other calls take that lock too, so one
        made meanwhile would wait for a join that may itself be waiting for this thread.
        """
        return not self._released and self._job is None

    # --- properties -------------------------------------------------------------------
    @property
    def url(self) -> Optional[str]:
        return self._url

    @property
    def kind(self) -> Optional[PlayKind]:
        return self._kind

    @property
    def hardware(self) -> bool:
        """The instance decodes in hardware: frozen pictures are watched for."""
        return self._hardware

    def state(self) -> PlaybackState:
        return self._state

    def time_ms(self) -> int:
        if not self._player_free():
            return 0
        return max(0, int(self._player.get_time() or 0))

    def length_ms(self) -> int:
        if not self._player_free():
            return 0
        return max(0, int(self._player.get_length() or 0))

    def is_seekable(self) -> bool:
        if not self._player_free():
            return False
        return bool(self._player.is_seekable())

    def holds_input(self) -> bool:
        """libVLC holds an input, or a worker still ends one: its connection may be open."""
        return self._has_input or self._job is not None

    # --- output -----------------------------------------------------------------------
    def set_window(self, win_id: int) -> None:
        # Sets a variable on the player, without its input lock: allowed during a job.
        if self._released:
            return
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
        referrer: Optional[str] = None,
        software: bool = False,
    ) -> None:
        """`software`: decode this stream in software (with hardware decoding; a channel
        whose picture froze before)."""
        if self._released:
            return
        self._cancel_pending()
        self._url, self._kind = url, kind
        self._options = media_options(
            kind, latency=latency, user_agent=user_agent, referrer=referrer
        )
        self._start_ms = start_ms if start_ms and start_ms > 0 else None
        self._stalls, self._restarted = 0, False
        self._software = software or url == self._froze_url
        self._start_media()

    def _start_media(self) -> None:
        """Open self._url now, or once a worker has ended the old input."""
        self._poll.stop()
        self._set_seekable(False)
        self._ever_played = False
        self._frozen.restart()
        self._last_time = -1
        self._audio_due = self._mute_wanted is not None or self._volume_wanted is not None
        self._set_state(PlaybackState.OPENING)
        self._stop_owed = False
        if self._job is not None:
            self._start_wanted = True  # a burst of zaps opens only the newest
        elif self._has_input:
            self._start_wanted = True
            self._run_job(_Job.DETACH)
        else:
            self._open()

    def _open(self) -> None:
        """set_media and play on the GUI thread: only while libVLC holds no input to join."""
        media = self._instance.media_new(self._url)
        options = list(self._options)
        if self._software and self._hardware:
            options += software_options()
        for option in options:
            media.add_option(option)
        self._player.set_media(media)
        media.release()
        if self._rate is not None:
            self._player.set_rate(self._rate)
            self._rate = None
        self._engaged = True
        self._opened.start()
        self._stall_clock.start()
        if self._player.play() == -1:
            self._fail("VLC could not start playback")
            return
        self._has_input = True
        self._poll.start()
        self._watch_audio()

    def _end_input(self) -> None:
        """Stop libVLC's input and outputs on a worker; nothing to do if none is open."""
        self._start_wanted = False
        if self._job is not None:
            self._stop_owed = self._job is _Job.DETACH
        elif self._engaged:
            self._run_job(_Job.STOP)

    def _run_job(self, job: _Job) -> None:
        self._job = job
        self._job_token += 1
        self._runner.run_worker(
            _PlayerJob(self._player, job),
            self._job_done,
            self._job_failed,
            context=self._job_token,
        )

    def _job_failed(self, token: int, message: str) -> None:
        logger.warning("Ending the stream in libVLC failed: %s", message)
        self._job_done(token)

    def _job_done(self, token: int, _payload: object = None) -> None:
        """On the GUI thread, after the worker has let go of the player."""
        if self._job is None or token != self._job_token:
            return
        job, self._job = self._job, None
        self._has_input = False
        if job is _Job.STOP:
            self._engaged = False
        if self._released:
            if self._engaged:  # a DETACH: close the kept outputs on a worker too, then release
                self._run_job(_Job.STOP)
            else:
                self._release_player()
        elif self._start_wanted:
            self._start_wanted = False
            self._open()
        elif self._stop_owed:
            self._stop_owed = False
            self._run_job(_Job.STOP)
        if not self._released and not self.holds_input():
            self.inputEnded.emit()

    def _cancel_pending(self) -> None:
        """Cancel delayed work (audio restore, restart) belonging to the old media."""
        self._restore_timer.stop()
        self._restart_timer.stop()
        self._audio_timer.stop()
        self._pending_track = None

    def _set_seekable(self, seekable: bool) -> None:
        if seekable != self._seekable:
            self._seekable = seekable
            self.seekableChanged.emit(seekable)

    def stop(self) -> None:
        if self._released:
            return
        self._poll.stop()
        self._cancel_pending()
        self._start_ms = None
        self._end_input()
        self._set_seekable(False)
        self._set_state(PlaybackState.IDLE)

    def toggle_pause(self) -> None:
        if self._player_free():
            self._player.pause()

    def set_paused(self, paused: bool) -> None:
        if self._player_free():
            self._player.set_pause(1 if paused else 0)

    def seek(self, ms: int) -> None:
        if self.is_seekable():
            self._player.set_time(max(0, int(ms)))
            self._frozen.restart()  # the decoder starts again where the seek lands

    def seek_relative(self, ms: int) -> None:
        self.seek(self.time_ms() + ms)

    def set_rate(self, rate: float) -> None:
        if self._released:
            return
        if self._job is not None:
            self._rate = float(rate)  # for the media that opens next
            return
        self._player.set_rate(float(rate))

    # Volume and mute take only the audio output's locks, never the input lock: libVLC lets
    # interfaces call them from any thread, so they stay live while a job runs. Without an
    # audio output libVLC drops them: what was asked for is applied again (module docstring).
    def volume(self) -> int:
        if self._released:
            return 0
        if self._volume_wanted is not None:
            return self._volume_wanted
        value = int(self._player.audio_get_volume())
        if value >= 0:
            self._volume_seen = value
        return self._volume_seen  # without an audio output, the last level one reported

    def set_volume(self, value: int) -> None:
        if self._released:
            return
        self._volume_wanted = max(0, min(100, int(value)))
        self._audio_due = True
        self._player.audio_set_volume(self._volume_wanted)
        self._watch_audio()

    def toggle_mute(self) -> bool:
        if self._released:
            return False
        muted = self._mute_wanted
        if muted is None:
            muted = int(self._player.audio_get_mute()) > 0  # -1: no audio output, not muted
        self.set_muted(not muted)
        return not muted

    def set_muted(self, muted: bool) -> None:
        if self._released:
            return
        self._mute_wanted = bool(muted)
        self._audio_due = True
        self._player.audio_set_mute(self._mute_wanted)
        self._watch_audio()

    def _watch_audio(self) -> None:
        """Look for the audio output often while the wanted mute/volume wait for it."""
        if self._audio_due and self._has_input and not self._released:
            self._audio_watch.start()
            self._audio_timer.start()

    def _look_for_audio(self) -> None:
        if self._released or not self._audio_due or not self._has_input:
            return  # applied, or no media: the next one's open looks again
        if self._player_free():
            self._apply_audio()
        if self._audio_due and self._audio_watch.elapsed() < _AUDIO_WATCH_MS:
            self._audio_timer.start()

    def _apply_audio(self) -> None:
        """Apply the wanted mute and volume once this media's audio output is open."""
        if not self._audio_due or int(self._player.audio_get_mute()) < 0:
            return
        self._audio_due = False
        if self._volume_wanted is not None:
            self._player.audio_set_volume(self._volume_wanted)
        if self._mute_wanted is not None:
            self._player.audio_set_mute(self._mute_wanted)

    def audio_tracks(self) -> list[tuple[int, str]]:
        if not self._player_free():
            return []
        descriptions = self._player.audio_get_track_description() or []
        return [(int(i), self._decode(n)) for i, n in descriptions]

    def set_audio_track(self, track_id: int) -> None:
        if self._player_free():
            self._player.audio_set_track(int(track_id))

    def subtitle_tracks(self) -> list[tuple[int, str]]:
        if not self._player_free():
            return []
        descriptions = self._player.video_get_spu_description() or []
        return [(int(i), self._decode(n)) for i, n in descriptions]

    def set_subtitle_track(self, track_id: int) -> None:
        if self._player_free():
            self._player.video_set_spu(int(track_id))

    @staticmethod
    def _decode(name) -> str:
        return name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)

    def release(self) -> None:
        """Release the player; with an input still open, after a worker has stopped it.

        The TV window stops first and waits for background threads, so its release finds
        the player stopped and frees it here and now.
        """
        if self._released:
            return
        self._released = True
        self._poll.stop()
        self._cancel_pending()
        self._start_wanted = self._stop_owed = False
        if self._job is not None:
            return  # _job_done stops (after a DETACH) and releases it
        if self._engaged:
            self._run_job(_Job.STOP)  # _job_done releases it
            return
        self._release_player()

    def _release_player(self) -> None:
        try:
            self._player.release()
        except Exception as e:
            logger.debug("Releasing the media player failed: %s", e)

    # --- polling ----------------------------------------------------------------------
    def _set_state(self, state: PlaybackState) -> None:
        if state is not self._state:
            self._state = state
            self.stateChanged.emit(state.value)

    def _fail(self, message: str) -> None:
        # Stop libVLC too: after an open timeout it keeps connecting, and would otherwise
        # play behind the error and hold one of the provider's connections.
        self._poll.stop()
        self._cancel_pending()
        self._end_input()
        self._set_seekable(False)
        self._set_state(PlaybackState.ERROR)
        self.errorOccurred.emit(message)

    def _tick(self) -> None:
        if not self._player_free():
            return
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
            if not self._ever_played:
                self._playing_clock.start()
            self._ever_played = True
        self._set_state(state)
        if state is PlaybackState.PLAYING:
            self._apply_audio()
        if state in (PlaybackState.OPENING, PlaybackState.BUFFERING) and not self._ever_played:
            if self._opened.elapsed() > self._open_timeout_ms:
                self._fail("Timed out opening the stream")
            return
        seekable = self.is_seekable()
        self._set_seekable(seekable)
        if self._start_ms is not None and state is PlaybackState.PLAYING:
            if seekable:
                target = self._start_ms
                length = self.length_ms()
                if length > 0:
                    target = max(0, min(target, length - _RESUME_END_MARGIN_MS))
                self._player.set_time(target)
                self._frozen.restart()
                self._start_ms = None
            elif self._playing_clock.elapsed() > _RESUME_GIVE_UP_MS:
                logger.debug("Dropping the resume position: the media is not seekable")
                self._start_ms = None
        if state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            self.positionChanged.emit(self.time_ms(), self.length_ms())
        if self._watch_picture(state):
            return  # it decodes in software now
        self._watch_stall(state)

    def _watch_picture(self, state: PlaybackState) -> bool:
        """With hardware decoding, a picture that stands still while the sound plays sends
        the stream to software (module docstring); True when it did."""
        if not self._hardware or self._software:
            return False
        counters = self._counters()
        playing = state is PlaybackState.PLAYING and self._can_show()
        if counters is None or not self._frozen.update(self._clock(), playing, counters):
            return False
        self._software = True
        self._froze_url = self._url
        logger.info(
            "The picture stood still while the sound played (hardware decoding): "
            "this %s decodes in software from now",
            "time-shift stream" if self._kind is PlayKind.LIVE_PIPELINE else "stream",
        )
        if self._kind is not PlayKind.LIVE_PIPELINE:  # LiveSession reopens its own
            start = self.time_ms() if self._seekable and self.length_ms() > 0 else 0
            self._cancel_pending()  # what waited for the old media (an audio track restore)
            self._start_ms = start or None  # a film where it plays
            self._start_media()
        self.pictureFroze.emit()
        return True

    def _can_show(self) -> bool:
        """Pictures could show: the app runs on a window system (`display_exists`), VLC has a
        video output for this media, or it made one for an earlier media of this player.
        A decoder that sends no picture gets no output (VideoToolbox at a tune into intra
        refresh, also in a real window): with a window system that is the decoder's doing,
        so it is judged from the first tune. Offscreen (the smoke, CI) VLC can make no output
        at all ("video output creation failed"), whatever the decoder does: only once one
        existed does a stream there count as frozen."""
        if int(self._player.has_vout() or 0) > 0:
            self._had_vout = True
        if self._had_vout:
            return True
        # The smoke test judges a stream without an output only once one existed: CI's
        # macOS smoke runs the packaged app on the real cocoa platform, but libVLC never
        # opens a video display in that VM, so a window system there still shows nothing
        # (CI run 39173463876 sent a time-shift stream to software, and its reopen broke
        # the Go live check). A viewer's app judges a first tune without a picture.
        return display_exists() and os.environ.get(SMOKE_ENV) != "1"

    def _counters(self) -> Optional[Counters]:
        """This media's statistics from libVLC (the player's media and its input item: never
        the input lock a job may hold, and only called while none runs)."""
        media = self._player.get_media()
        if media is None:
            return None
        try:
            stats = vlc.MediaStats()
            if not media.get_stats(stats):
                return None
            return Counters(
                int(stats.displayed_pictures),
                int(stats.lost_pictures),
                int(stats.decoded_video),
                int(stats.played_abuffers),  # decoded audio may be dropped (a seek's preroll)
            )
        finally:
            media.release()

    def _watch_stall(self, state: PlaybackState) -> None:
        if self._kind not in (PlayKind.LIVE, PlayKind.UNKNOWN):
            return
        current = self.time_ms()
        if current <= 0 and self._last_time <= 0:
            # v1.13.8 guard: a stream that has not reported a clock yet is not stalled.
            self._last_time = current
            self._stall_clock.restart()
            return
        if (
            state not in (PlaybackState.PLAYING, PlaybackState.BUFFERING)
            or current != self._last_time
        ):
            if (
                self._stalls
                and state is PlaybackState.PLAYING
                and current != self._last_time
                and self._since_stall.elapsed() >= _STALL_BUDGET_RESET_MS
            ):
                logger.info("Playback recovered; the stall budget is reset")
                self._stalls, self._restarted = 0, False
            self._last_time = current
            self._stall_clock.restart()
            return
        if self._stall_clock.elapsed() < self._stall_ms:
            return
        self._stall_clock.restart()
        self._since_stall.start()
        self._stalls += 1
        if self._stalls <= _STALL_AUDIO_RETRIES:
            logger.warning("Playback stalled (attempt %d); cycling the audio track", self._stalls)
            self._pending_track = self._player.audio_get_track()
            self._player.audio_set_track(-1)
            self._restore_timer.start()
        elif not self._restarted:
            logger.warning("Playback stalled again; restarting the stream")
            self._restarted = True
            self._poll.stop()
            self._end_input()
            self._restart_timer.start()  # _start_media waits for the stop if it is still running

    def _restore_audio(self) -> None:
        if not self._player_free() or self._pending_track is None:
            return
        track, self._pending_track = self._pending_track, None
        self._player.audio_set_track(track)

    def _restart_stream(self) -> None:
        if self._released or self._url is None:
            return
        self._start_media()


class Session(Protocol):
    """What the TV shell uses of a playback session: PlaybackSession or NullSession."""

    stateChanged: Any
    positionChanged: Any
    errorOccurred: Any
    ended: Any
    seekableChanged: Any
    inputEnded: Any
    pictureFroze: Any

    @property
    def url(self) -> Optional[str]: ...

    @property
    def kind(self) -> Optional[PlayKind]: ...

    @property
    def hardware(self) -> bool: ...

    def state(self) -> PlaybackState: ...

    def time_ms(self) -> int: ...

    def length_ms(self) -> int: ...

    def is_seekable(self) -> bool: ...

    def holds_input(self) -> bool: ...

    def set_window(self, win_id: int) -> None: ...

    def play(
        self,
        url: str,
        *,
        kind: PlayKind,
        latency: str = "normal",
        user_agent: Optional[str] = None,
        start_ms: Optional[int] = None,
        referrer: Optional[str] = None,
        software: bool = False,
    ) -> None: ...

    def stop(self) -> None: ...

    def toggle_pause(self) -> None: ...

    def set_paused(self, paused: bool) -> None: ...

    def seek(self, ms: int) -> None: ...

    def seek_relative(self, ms: int) -> None: ...

    def set_rate(self, rate: float) -> None: ...

    def volume(self) -> int: ...

    def set_volume(self, value: int) -> None: ...

    def toggle_mute(self) -> bool: ...

    def set_muted(self, muted: bool) -> None: ...

    def audio_tracks(self) -> list[tuple[int, str]]: ...

    def set_audio_track(self, track_id: int) -> None: ...

    def subtitle_tracks(self) -> list[tuple[int, str]]: ...

    def set_subtitle_track(self, track_id: int) -> None: ...

    def release(self) -> None: ...


class NullSession(QObject):
    """A Session without libVLC: nothing plays, and every play explains why."""

    stateChanged = Signal(str)
    positionChanged = Signal(int, int)
    errorOccurred = Signal(str)
    ended = Signal()
    seekableChanged = Signal(bool)
    inputEnded = Signal()
    pictureFroze = Signal()

    @property
    def url(self) -> Optional[str]:
        return None

    @property
    def kind(self) -> Optional[PlayKind]:
        return None

    @property
    def hardware(self) -> bool:
        return False

    def state(self) -> PlaybackState:
        return PlaybackState.IDLE

    def time_ms(self) -> int:
        return 0

    def length_ms(self) -> int:
        return 0

    def is_seekable(self) -> bool:
        return False

    def holds_input(self) -> bool:
        return False

    def set_window(self, win_id: int) -> None:
        pass

    def play(
        self,
        url: str,
        *,
        kind: PlayKind,
        latency: str = "normal",
        user_agent: Optional[str] = None,
        start_ms: Optional[int] = None,
        referrer: Optional[str] = None,
        software: bool = False,
    ) -> None:
        self.errorOccurred.emit(NO_VLC_MESSAGE)

    def stop(self) -> None:
        pass

    def toggle_pause(self) -> None:
        pass

    def set_paused(self, paused: bool) -> None:
        pass

    def seek(self, ms: int) -> None:
        pass

    def seek_relative(self, ms: int) -> None:
        pass

    def set_rate(self, rate: float) -> None:
        pass

    def volume(self) -> int:
        return 0

    def set_volume(self, value: int) -> None:
        pass

    def toggle_mute(self) -> bool:
        return False

    def set_muted(self, muted: bool) -> None:
        pass

    def audio_tracks(self) -> list[tuple[int, str]]:
        return []

    def set_audio_track(self, track_id: int) -> None:
        pass

    def subtitle_tracks(self) -> list[tuple[int, str]]:
        return []

    def set_subtitle_track(self, track_id: int) -> None:
        pass

    def release(self) -> None:
        pass
