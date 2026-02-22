import logging
import platform
import sys

from PySide6.QtCore import QMetaObject, QPoint, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import QFrame, QLabel, QMainWindow, QProgressBar, QVBoxLayout
import vlc


class VLCLogger:
    def __init__(self):
        self.latest_error = ""

    def log(self, message):
        self.latest_error = message
        logging.error(f"VLC Error: {message}")

    def get_latest_error(self):
        return self.latest_error


class VideoPlayer(QMainWindow):
    playing = Signal()
    stopped = Signal()
    backRequested = Signal()
    forwardRequested = Signal()
    channelNextRequested = Signal()
    channelPrevRequested = Signal()
    mediaEnded = Signal()  # Emitted when playback ends naturally
    positionChanged = Signal(int, int)  # (position_ms, duration_ms) for saving progress

    # Timing constants for smooth paused seek (resume → seek → pause)
    _SEEK_RESUME_DELAY_MS = 60  # Delay before setting position after resume
    _SEEK_PAUSE_DELAY_MS = 140  # Delay before pausing after seek

    # Resize edge detection threshold (pixels from window edge to trigger resize cursor)
    _RESIZE_EDGE_PIXELS = 10

    def __init__(self, config_manager, *args, **kwargs):
        super(VideoPlayer, self).__init__(*args, **kwargs)
        self.config_manager = config_manager
        self.config = self.config_manager.config

        # Start normally, not on top
        self.is_pip_mode = False
        self.normal_geometry = None
        self.aspect_ratio = 16 / 9  # Default aspect ratio; will be updated when video plays
        self.setWindowFlags(Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self.dragging = False
        self.resizing = False
        self.drag_position = QPoint()

        self.config_manager.apply_window_settings("video_player", self)

        self.mainFrame = QFrame()
        self.setCentralWidget(self.mainFrame)
        self.setWindowTitle("QiTV Player")
        t_lay_parent = QVBoxLayout()
        t_lay_parent.setContentsMargins(0, 0, 0, 0)

        self.video_frame = QFrame()
        self.video_frame.mouseDoubleClickEvent = self.mouseDoubleClickEvent
        # Note: No custom eventFilter implemented; avoid installing unused event filter
        t_lay_parent.addWidget(self.video_frame)

        # Custom user-agent string
        self.vlc_logger = VLCLogger()

        # Initialize VLC instance
        self.instance = vlc.Instance(
            ["--video-on-top"]
        )  # vlc.Instance(["--verbose=2"])  # Enable verbose logging

        self.media_player = self.instance.media_player_new()
        self.media_player.video_set_mouse_input(False)
        self.media_player.video_set_key_input(False)
        self._last_url: str | None = None
        self._seek_retry_done: bool = False

        # Set up event manager for logging
        self.event_manager = self.media_player.event_manager()
        self.event_manager.event_attach(
            vlc.EventType.MediaPlayerEncounteredError, self.on_vlc_error
        )

        if sys.platform.startswith("linux"):
            self.media_player.set_xwindow(self.video_frame.winId())
        elif sys.platform == "win32":
            self.media_player.set_hwnd(self.video_frame.winId())
        elif sys.platform == "darwin":
            self.media_player.set_nsobject(int(self.video_frame.winId()))

        self.mainFrame.setLayout(t_lay_parent)
        # Don't auto-show - let caller decide when to show the player

        # Enable mouse tracking for inactivity detection
        self.setMouseTracking(True)
        self.video_frame.setMouseTracking(True)

        self.resize_corner = None

        # Seekable progress bar (handles press/move/dblclick without bubbling)
        class _SeekProgressBar(QProgressBar):
            def __init__(self, parent=None, on_seek=None):
                super().__init__(parent)
                self._on_seek = on_seek
                self.setMouseTracking(True)

            def _emit_seek(self, event):
                try:
                    w = self.width() or 1
                    # Support QPointF (position) and QPoint (pos)
                    x = (
                        event.position().x()
                        if hasattr(event, "position")
                        else float(event.pos().x())
                    )
                    frac = max(0.0, min(1.0, x / float(w)))
                    if callable(self._on_seek):
                        self._on_seek(frac)
                except Exception:
                    pass

            def mousePressEvent(self, event):
                if event.buttons() & Qt.LeftButton:
                    self._emit_seek(event)
                    event.accept()
                    return
                super().mousePressEvent(event)

            def mouseMoveEvent(self, event):
                if event.buttons() & Qt.LeftButton:
                    self._emit_seek(event)
                    event.accept()
                    return
                super().mouseMoveEvent(event)

            def mouseDoubleClickEvent(self, event):
                # Treat double-click as a seek action; do not bubble to parent/window
                if event.buttons() & Qt.LeftButton or event.button() == Qt.LeftButton:
                    self._emit_seek(event)
                    event.accept()
                    return
                super().mouseDoubleClickEvent(event)

        self.progress_bar = _SeekProgressBar(self, on_seek=self._on_seek_fraction)
        self.progress_bar.setRange(0, 1000)  # Use 1000 steps for smoother updates
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("00:00 / 00:00")
        self.progress_bar.setFixedHeight(22)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: rgba(0, 0, 0, 0.6);
                border: none;
                color: white;
                font-size: 12px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: rgba(0, 191, 165, 0.85);
                border-radius: 0px;
            }
        """)
        self.mainFrame.layout().addWidget(self.progress_bar)

        # OSD label for volume/status feedback overlay
        self._osd_label = QLabel(self.video_frame)
        self._osd_label.setAlignment(Qt.AlignCenter)
        self._osd_label.setStyleSheet("""
            QLabel {
                background-color: rgba(0, 0, 0, 0.7);
                color: white;
                font-size: 16px;
                font-weight: bold;
                border-radius: 8px;
                padding: 8px 16px;
            }
        """)
        self._osd_label.setVisible(False)
        self._osd_label.setFixedSize(180, 40)
        self._osd_timer = QTimer(self)
        self._osd_timer.setSingleShot(True)
        self._osd_timer.timeout.connect(lambda: self._osd_label.setVisible(False))

        # Track content type to reduce repeated progress bar toggles
        self._is_live = None  # type: bool | None
        self._is_live_hint = None  # type: bool | None
        self._seekable = True

        # Auto-play support: prevent multiple mediaEnded emissions
        self._ended_emitted = False
        self._content_id: str | None = None
        self._resume_position: int | None = None

        # Position save timer for watch tracking (15 second interval)
        self.position_save_timer = QTimer(self)
        self.position_save_timer.setInterval(15000)  # 15 seconds
        self.position_save_timer.timeout.connect(self._emit_position_changed)

        self.update_timer = QTimer(self)
        self.update_timer.setInterval(100)  # Update every 100ms
        self.update_timer.timeout.connect(self.update_progress)

        # Timer for auto-hiding progress bar and cursor on inactivity
        self.inactivity_timer = QTimer(self)
        self.inactivity_timer.setInterval(3000)  # 3 seconds of inactivity
        self.inactivity_timer.timeout.connect(self.on_inactivity)
        self._ui_visible = True  # Track UI visibility state

        # Deprecated direct handler; interactive behavior handled by _SeekProgressBar above

        # Stall detection watchdog: auto-restart when playback freezes
        # (e.g., during HLS intro→content transitions that break audio)
        self._stall_last_time = -1  # Last observed media time (ms)
        self._stall_count = 0  # Consecutive stall ticks
        self._auto_retry_count = 0  # Prevent infinite retry loops
        self._stall_timer = QTimer(self)
        self._stall_timer.setInterval(2000)  # Check every 2 seconds
        self._stall_timer.timeout.connect(self._check_stall)

        # Single vs double click handling
        self.click_position = None
        self.click_timer = QTimer(self)
        self.click_timer.setSingleShot(True)
        self.click_timer.timeout.connect(self.handle_click)
        self._ignore_single_click = False  # guard to suppress single-click after dblclick

        # Keyboard shortcuts via QActions
        self._setup_actions()

    def _setup_actions(self):
        # Note: Global application-level shortcuts are defined in ChannelList
        # to work regardless of focus. Avoid duplicating shortcuts here to
        # prevent "Ambiguous shortcut overload" warnings.

        action_play_pause = QAction("Play/Pause", self)
        action_play_pause.setShortcut(QKeySequence(Qt.Key_Space))
        action_play_pause.setShortcutContext(Qt.WindowShortcut)
        action_play_pause.triggered.connect(self.toggle_play_pause)
        self.addAction(action_play_pause)

        action_mute = QAction("Mute", self)
        action_mute.setShortcut(QKeySequence(Qt.Key_M))
        action_mute.setShortcutContext(Qt.WindowShortcut)
        action_mute.triggered.connect(self.toggle_mute)
        self.addAction(action_mute)

        action_fullscreen = QAction("Fullscreen", self)
        action_fullscreen.setShortcut(QKeySequence(Qt.Key_F))
        action_fullscreen.setShortcutContext(Qt.WindowShortcut)
        action_fullscreen.triggered.connect(self.toggle_fullscreen)
        self.addAction(action_fullscreen)

        action_exit_fullscreen = QAction("Exit Fullscreen", self)
        # Keep Escape local to the player window for exiting fullscreen
        action_exit_fullscreen.setShortcut(QKeySequence(Qt.Key_Escape))
        action_exit_fullscreen.triggered.connect(lambda: self.setWindowState(Qt.WindowNoState))
        self.addAction(action_exit_fullscreen)

        action_pip = QAction("Picture in Picture", self)
        action_pip.setShortcut(QKeySequence(Qt.ALT | Qt.Key_P))
        action_pip.setShortcutContext(Qt.WindowShortcut)
        action_pip.triggered.connect(self._action_toggle_pip)
        self.addAction(action_pip)

        action_audio_track = QAction("Cycle Audio Track", self)
        action_audio_track.setShortcut(QKeySequence(Qt.Key_A))
        action_audio_track.setShortcutContext(Qt.WindowShortcut)
        action_audio_track.triggered.connect(self.cycle_audio_track)
        self.addAction(action_audio_track)

        action_subtitle_track = QAction("Cycle Subtitle Track", self)
        action_subtitle_track.setShortcut(QKeySequence(Qt.Key_J))
        action_subtitle_track.setShortcutContext(Qt.WindowShortcut)
        action_subtitle_track.triggered.connect(self.cycle_subtitle_track)
        self.addAction(action_subtitle_track)

    def _action_toggle_pip(self):
        if self.windowState() == Qt.WindowFullScreen:
            self.setWindowState(Qt.WindowNoState)
        self.toggle_pip_mode()

    def _try_seek(self, target, fallback, use_time: bool = True) -> None:
        """Attempt a seek operation with fallback on failure.

        Args:
            target: Primary seek value (milliseconds if use_time, else position 0.0-1.0)
            fallback: Fallback position value (0.0-1.0) if primary fails
            use_time: If True, use set_time(target); otherwise use set_position(target)
        """
        try:
            if use_time:
                self.media_player.set_time(target)
            else:
                self.media_player.set_position(target)
        except Exception:
            try:
                self.media_player.set_position(fallback)
            except Exception:
                pass

    def _smooth_paused_seek(self, target, fallback, use_time: bool = True) -> None:
        """Seek while paused by briefly resuming playback.

        Temporarily resumes playback, seeks, then pauses again to avoid
        timestamp errors in some VLC demuxers.
        """
        try:
            self.media_player.play()
            QTimer.singleShot(
                self._SEEK_RESUME_DELAY_MS, lambda: self._try_seek(target, fallback, use_time)
            )
            QTimer.singleShot(self._SEEK_PAUSE_DELAY_MS, lambda: self.media_player.pause())
        except Exception:
            self._try_seek(target, fallback, use_time)

    def _on_seek_fraction(self, fraction: float) -> None:
        # Only seek when media is seekable and not live
        if getattr(self, "_seekable", None) is False or self._is_live is True:
            return
        try:
            media = getattr(self, "media", None)
            duration = media.get_duration() if media is not None else 0
        except Exception:
            duration = 0

        # Compute fallback position (clamped to avoid Ended state at 1.0)
        fallback_pos = max(0.0, min(0.999, float(fraction)))

        # Check if paused for smooth seek behavior
        try:
            paused = self.media_player.get_state() == vlc.State.Paused
        except Exception:
            paused = False
        use_smooth = paused and getattr(self.config_manager, "smooth_paused_seek", False)

        # Prefer time-based seek when duration is known
        if duration and duration > 0:
            # Clamp to slightly before end (avoid Ended firing on boundary)
            target_ms = max(
                0, min(int(duration - 1000), int(duration * max(0.0, min(1.0, fraction))))
            )
            if use_smooth:
                self._smooth_paused_seek(target_ms, fallback_pos, use_time=True)
            else:
                self._try_seek(target_ms, fallback_pos, use_time=True)
        else:
            # Unknown duration; use position-based seek
            if use_smooth:
                self._smooth_paused_seek(fallback_pos, fallback_pos, use_time=False)
            else:
                self._try_seek(fallback_pos, fallback_pos, use_time=False)

    def format_time(self, milliseconds):
        seconds = int(milliseconds / 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes:02d}:{seconds:02d}"

    def update_progress(self):
        state = self.media_player.get_state()
        # Only update the progress bar value/text here to avoid repeated visibility toggles
        if state == vlc.State.Playing:
            if self._is_live is False and getattr(self, "media", None):
                current_time = self.media_player.get_time()
                total_time = self.media.get_duration()
                if total_time and 0 <= current_time <= total_time:
                    formatted_current = self.format_time(current_time)
                    formatted_total = self.format_time(total_time)
                    self.progress_bar.setFormat(f"{formatted_current} / {formatted_total}")
                    try:
                        self.progress_bar.setValue(int(current_time * 1000 / total_time))
                    except ZeroDivisionError:
                        self.progress_bar.setValue(0)
        elif state == vlc.State.Error:
            self.handle_error("Playback error")
        elif state == vlc.State.Ended:
            if self._is_live is False:
                self.progress_bar.setFormat("Playback ended")
                self.progress_bar.setValue(1000)
                # Emit mediaEnded signal once per playback
                if not self._ended_emitted:
                    self._ended_emitted = True
                    self.mediaEnded.emit()
        elif state == vlc.State.Opening:
            if self._is_live is False:
                self.progress_bar.setFormat("Opening...")
                self.progress_bar.setValue(0)
        elif state == vlc.State.Buffering:
            if self._is_live is False:
                self.progress_bar.setFormat("Buffering...")
                self.progress_bar.setValue(0)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if platform.system() == "Darwin":
            delta = -delta
        if delta > 0:
            self.change_volume(10)  # Increase volume
        else:
            self.change_volume(-10)  # Decrease volume

    def keyPressEvent(self, event):
        # Optional keyboard/remote mode: Up/Down surf channels instead of volume
        try:
            remote_mode = bool(self.config_manager.keyboard_remote_mode)
        except Exception:
            remote_mode = False

        if remote_mode:
            if event.key() == Qt.Key_Up:
                self.channelPrevRequested.emit()
                return
            elif event.key() == Qt.Key_Down:
                self.channelNextRequested.emit()
                return
        else:
            if event.key() == Qt.Key_Up:
                self.change_volume(10)
                return
            elif event.key() == Qt.Key_Down:
                self.change_volume(-10)
                return
        super().keyPressEvent(event)

    def change_volume(self, step):
        current_volume = self.media_player.audio_get_volume()
        new_volume = max(0, min(100, current_volume + step))
        self.media_player.audio_set_volume(new_volume)
        self._show_osd(f"Volume: {new_volume}%")

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Suppress any pending single-click action
            if self.click_timer.isActive():
                self.click_timer.stop()
            self._ignore_single_click = True
            self.toggle_fullscreen()

    def closeEvent(self, event):
        if self.media_player.is_playing():
            self.media_player.stop()
            self.stopped.emit()
        self.config_manager.save_window_settings(self, "video_player")
        self.hide()
        event.ignore()

    def play_video(self, video_url, is_live=None, content_id=None, resume_position=None):
        """Play a video URL.

        Args:
            video_url: The URL to play
            is_live: Hint for live detection. True=live stream, False=VOD, None=auto-detect
            content_id: Unique identifier for tracking playback position
            resume_position: Position in milliseconds to resume from
        """
        # Reset auto-play flag for new playback
        self._ended_emitted = False
        self._content_id = content_id
        self._resume_position = resume_position
        if platform.system() == "Linux":
            self.media_player.set_xwindow(self.video_frame.winId())
        elif platform.system() == "Windows":
            self.media_player.set_hwnd(self.video_frame.winId())
        elif platform.system() == "Darwin":
            self.media_player.set_nsobject(int(self.video_frame.winId()))

        self._last_url = video_url
        self._is_live_hint = is_live  # Store hint for use in media_length_changed
        self._seek_retry_done = False
        self._auto_retry_count = 0
        self._stall_timer.stop()
        self.media = self.instance.media_new(video_url)
        try:
            # User agent
            self.media.add_option(":http-user-agent=VLC/3.0.20")

            # Hardware decoding (if available)
            self.media.add_option(":avcodec-hw=any")
            self.media.add_option(":avcodec-fast")
            self.media.add_option(":no-audio-time-stretch")

            is_vod_container = video_url and (
                '.mkv' in video_url.lower()
                or '.mp4' in video_url.lower()
                or '.avi' in video_url.lower()
                or '.webm' in video_url.lower()
            )

            if is_live is True:
                # Live streams: keep caching low for fast start.
                # High caching causes VLC to stall waiting for data
                # when some CDN servers in the HLS manifest are down.
                self.media.add_option(":network-caching=1000")
                self.media.add_option(":live-caching=500")
                self.media.add_option(":http-reconnect=true")
                # Adaptive streaming: start with lowest bandwidth to
                # begin playback quickly, then adapt upward.
                self.media.add_option(":adaptive-logic=lowest")
                # Reduce clock jitter sensitivity for streams with PCR
                # discontinuities (common during intro→content transitions).
                self.media.add_option(":clock-jitter=0")
            elif is_vod_container:
                # VOD file containers: higher caching for smooth seeking
                self.media.add_option(":network-caching=4000")
                self.media.add_option(":file-caching=2000")
                self.media.add_option(":http-reconnect=true")
                self.media.add_option(":input-timeshift-granularity=0")
                self.media.add_option(":input-fast-seek")
                self.media.add_option(":http-forward-cookies=true")
            else:
                # VOD streams (m3u8/ts) or unknown: moderate caching
                self.media.add_option(":network-caching=2000")
                self.media.add_option(":file-caching=1500")
                self.media.add_option(":live-caching=1000")
                self.media.add_option(":http-reconnect=true")
        except Exception as e:
            logging.warning(f"Failed to set VLC options: {e}")
        self.media_player.set_media(self.media)

        events = self.media_player.event_manager()
        events.event_attach(vlc.EventType.MediaPlayerLengthChanged, self.on_media_length_changed)
        self.media.parse_with_options(1, 0)

        play_result = self.media_player.play()
        if play_result == -1:
            self.handle_error("Failed to start playback")
        else:
            self.adjust_aspect_ratio()
            self.show()
            # Don't force activate - let user click to interact
            # This prevents stealing focus from channel list
            self.playing.emit()
            QTimer.singleShot(5000, self.check_playback_status)

            # Start stall watchdog for live streams (intro clip recovery)
            self._stall_last_time = -1
            self._stall_count = 0
            if is_live is True:
                self._stall_timer.start()

            # Start inactivity timer for auto-hiding UI
            self._ui_visible = True
            self.inactivity_timer.start()

    def check_playback_status(self):
        state = self.media_player.get_state()
        if state == vlc.State.Playing:  # only check if media has not been paused, or stopped
            if not self.media_player.is_playing():
                media_state = self.media.get_state()
                if media_state == vlc.State.Error:
                    self.handle_error("Playback error")
                else:
                    self.handle_error("Failed to start playback")
                self.stopped.emit()

    def _check_stall(self):
        """Watchdog: detect stalled playback and recover.

        IPTV streams with server-side intro clips can stall VLC when the
        stream format changes mid-playback (audio sample rate becomes 0).
        Recovery strategy:
          1st stall → cycle audio track off/on to force decoder reinit
          2nd stall → full restart (last resort)
        """
        try:
            state = self.media_player.get_state()
            if state not in (vlc.State.Playing, vlc.State.Buffering):
                self._stall_count = 0
                self._stall_last_time = -1
                return

            current_time = self.media_player.get_time()
            if current_time <= 0 and self._stall_last_time <= 0:
                # Still opening / not started yet — don't count
                return

            if current_time == self._stall_last_time:
                self._stall_count += 1
            else:
                self._stall_count = 0
                self._stall_last_time = current_time
                return

            self._stall_last_time = current_time

            # 3 consecutive stall checks × 2s = 6 seconds frozen
            if self._stall_count >= 3 and self._auto_retry_count < 3:
                self._auto_retry_count += 1
                self._stall_count = 0

                if self._auto_retry_count <= 2:
                    # First attempts: cycle audio track to reinit decoder
                    logging.warning(
                        "Stall detected (time=%s, attempt #%d) — cycling audio track",
                        current_time,
                        self._auto_retry_count,
                    )
                    self._recover_audio()
                else:
                    # Last resort: full restart
                    logging.warning(
                        "Stall detected (time=%s, attempt #%d) — restarting playback",
                        current_time,
                        self._auto_retry_count,
                    )
                    self._restart_playback()
        except Exception as e:
            logging.debug("Stall watchdog error: %s", e)

    def _recover_audio(self):
        """Cycle audio track off then back on to force VLC to reinit the decoder."""
        try:
            current_track = self.media_player.audio_get_track()
            # Disable audio
            self.media_player.audio_set_track(-1)
            # Re-enable after a short delay so VLC picks up new stream params
            QTimer.singleShot(500, lambda: self.media_player.audio_set_track(current_track))
        except Exception as e:
            logging.debug("Audio recovery failed: %s", e)

    def _restart_playback(self):
        """Stop and restart playback of the current URL."""
        url = self._last_url
        if not url:
            return
        is_live = self._is_live_hint
        content_id = self._content_id
        retry_count = self._auto_retry_count  # Preserve across restart
        self.media_player.stop()
        self._stall_timer.stop()

        def _do_restart():
            self.play_video(url, is_live=is_live, content_id=content_id)
            self._auto_retry_count = retry_count  # Restore retry count

        # Short delay to let VLC clean up, then replay
        QTimer.singleShot(300, _do_restart)

    def stop_video(self):
        self.media_player.stop()
        self.progress_bar.setVisible(False)
        self.update_timer.stop()
        self.position_save_timer.stop()  # Stop position tracking
        self._stall_timer.stop()
        self._auto_retry_count = 0
        self.inactivity_timer.stop()
        self.setCursor(Qt.ArrowCursor)  # Restore cursor
        self._ui_visible = True
        self._ended_emitted = False  # Reset for next playback
        self.stopped.emit()

    def toggle_mute(self):
        state = self.media_player.audio_get_mute()
        self.media_player.audio_set_mute(not state)
        self._show_osd("Muted" if not state else "Unmuted")

    def _show_osd(self, text: str, duration_ms: int = 2000):
        """Briefly show a floating OSD message over the video frame."""
        self._osd_label.setText(text)
        # Resize to fit text then center on video frame
        self._osd_label.adjustSize()
        self._osd_label.setFixedSize(
            max(180, self._osd_label.sizeHint().width() + 32),
            max(40, self._osd_label.sizeHint().height() + 16),
        )
        vf = self.video_frame
        self._osd_label.move(
            (vf.width() - self._osd_label.width()) // 2,
            (vf.height() - self._osd_label.height()) // 2,
        )
        self._osd_label.setVisible(True)
        self._osd_label.raise_()
        self._osd_timer.start(duration_ms)

    def cycle_audio_track(self):
        try:
            descriptions = self.media_player.audio_get_track_description()
            if not descriptions or len(descriptions) < 2:
                self._show_osd("No alternate audio tracks")
                return
            current = self.media_player.audio_get_track()
            ids = [d[0] for d in descriptions]
            idx = ids.index(current) if current in ids else 0
            next_idx = (idx + 1) % len(ids)
            self.media_player.audio_set_track(ids[next_idx])
            name = descriptions[next_idx][1]
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")
            self._show_osd(f"Audio: {name}")
        except Exception as e:
            logging.warning(f"Failed to cycle audio track: {e}")

    def cycle_subtitle_track(self):
        try:
            descriptions = self.media_player.video_get_spu_description()
            if not descriptions:
                self._show_osd("No subtitles available")
                return
            current = self.media_player.video_get_spu()
            ids = [d[0] for d in descriptions]
            idx = ids.index(current) if current in ids else -1
            next_idx = (idx + 1) % len(ids)
            self.media_player.video_set_spu(ids[next_idx])
            name = descriptions[next_idx][1]
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")
            self._show_osd(f"Subtitle: {name}")
        except Exception as e:
            logging.warning(f"Failed to cycle subtitle track: {e}")

    def toggle_play_pause(self):
        state = self.media_player.get_state()
        if state == vlc.State.Playing:
            self.media_player.pause()
            self._show_osd("\u23F8 Paused")
        else:
            self.media_player.play()
            self._show_osd("\u25B6 Playing")

    def toggle_pip_mode(self):
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        if not self.is_pip_mode:
            self.normal_geometry = self.geometry()  # Save current geometry for restoring later
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
            self.resize_and_position_pip()
            self.show()
        else:
            self.setWindowFlags(Qt.Window)
            self.setGeometry(self.normal_geometry)
            self.show()

        self.is_pip_mode = not self.is_pip_mode  # Toggle PiP mode

        QGuiApplication.restoreOverrideCursor()

    def mousePressEvent(self, event):
        # Map mouse Back/Forward buttons to navigation in the UI
        try:
            back_btn = Qt.MouseButton.BackButton
        except Exception:
            back_btn = getattr(Qt.MouseButton, "XButton1", None)
        try:
            fwd_btn = Qt.MouseButton.ForwardButton
        except Exception:
            fwd_btn = getattr(Qt.MouseButton, "XButton2", None)

        if back_btn is not None and event.button() == back_btn:
            self.backRequested.emit()
            event.accept()
            return
        if fwd_btn is not None and event.button() == fwd_btn:
            self.forwardRequested.emit()
            event.accept()
            return

        if event.button() == Qt.RightButton:
            self.toggle_pip_mode()
            return

        if event.button() == Qt.LeftButton:
            self.dragging = False
            self.resizing = False
            self.start_size = self.size()
            self.start_pos = self.pos()
            self.drag_position = event.globalPos()
            self.click_position = event.globalPos()

            # Determine the resize type (edges or corners)
            edge = self._RESIZE_EDGE_PIXELS
            if event.pos().x() <= edge:  # Left
                if event.pos().y() <= edge:  # Top-left corner
                    self.resize_corner = "top_left"
                elif event.pos().y() >= self.height() - edge:  # Bottom-left corner
                    self.resize_corner = "bottom_left"
                else:  # Left edge
                    self.resize_corner = "left"
            elif event.pos().x() >= self.width() - edge:  # Right
                if event.pos().y() <= edge:  # Top-right corner
                    self.resize_corner = "top_right"
                elif event.pos().y() >= self.height() - edge:  # Bottom-right corner
                    self.resize_corner = "bottom_right"
                else:  # Right edge
                    self.resize_corner = "right"
            elif event.pos().y() <= edge:  # Top edge
                self.resize_corner = "top"
            elif event.pos().y() >= self.height() - edge:  # Bottom edge
                self.resize_corner = "bottom"
            else:  # Center area - may be a click or drag, wait for mouseMoveEvent
                self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
                self.resize_corner = None
            self.resizing = bool(self.resize_corner)
            event.accept()

    def mouseMoveEvent(self, event):
        # Show UI on mouse movement
        self.show_ui()
        self.inactivity_timer.start()  # Restart inactivity timer

        if self.resizing:
            delta = event.globalPos() - self.drag_position
            new_width, new_height = self.start_size.width(), self.start_size.height()
            new_x, new_y = self.start_pos.x(), self.start_pos.y()

            if self.resize_corner in ["left", "top_left", "bottom_left"]:
                new_width = max(100, self.start_size.width() - delta.x())
                new_height = int(new_width / self.aspect_ratio)
                new_x = self.start_pos.x() + delta.x()  # shift right
            if self.resize_corner in ["right", "top_right", "bottom_right"]:
                new_width = max(100, self.start_size.width() + delta.x())
                new_height = int(new_width / self.aspect_ratio)
            if self.resize_corner in ["top", "top_left", "top_right"]:
                new_height = max(50, self.start_size.height() - delta.y())
                new_width = int(new_height * self.aspect_ratio)
                new_y = self.start_pos.y() + delta.y()  # shift down
            if self.resize_corner in ["bottom", "bottom_left", "bottom_right"]:
                new_height = max(50, self.start_size.height() + delta.y())
                new_width = int(new_height * self.aspect_ratio)

            self.setGeometry(new_x, new_y, new_width, new_height)
        elif event.buttons() & Qt.LeftButton and not self.resize_corner and not self.isFullScreen():
            # Mark as dragging only when mouse actually moves (disabled in fullscreen)
            self.dragging = True
            self.move(event.globalPos() - self.drag_position)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            # If a double-click was just handled, ignore this release
            if self._ignore_single_click:
                self._ignore_single_click = False
            else:
                # Only consider as a click if not dragging/resizing
                if not self.dragging and not self.resizing:
                    # Defer single-click to allow double-click detection window
                    interval = QGuiApplication.styleHints().mouseDoubleClickInterval()
                    self.click_timer.start(interval)

        self.dragging = False
        self.resizing = False
        self.resize_corner = None

    def handle_click(self):
        # This method is called when a single click is detected
        self.toggle_play_pause()

    def resize_to_aspect_ratio(self):
        width = self.width()
        height = int(width / self.aspect_ratio)
        self.resize(width, height)
        self.update()

    def resize_and_position_pip(self):
        screen_geometry = QGuiApplication.primaryScreen().geometry()
        available_geometry = QGuiApplication.primaryScreen().availableGeometry()
        pip_width = int(screen_geometry.width() * 0.25)  # PiP width is 25% of screen width
        pip_height = int(pip_width / self.aspect_ratio)
        x = screen_geometry.width() - pip_width - 10  # 10 pixels padding from edge
        y = (
            screen_geometry.height() - pip_height - 70
            if screen_geometry == available_geometry
            else available_geometry.height() - pip_height - 10
        )  # 10 pixels padding from edge
        self.setGeometry(x, y, pip_width, pip_height)
        self.show()  # Apply geometry changes
        self.update()

    def resizeEvent(self, event):
        if self.is_pip_mode:
            self.resize_to_aspect_ratio()
        # Re-center OSD label in video frame
        if hasattr(self, '_osd_label'):
            vf = self.video_frame
            lbl = self._osd_label
            lbl.move(
                (vf.width() - lbl.width()) // 2,
                (vf.height() - lbl.height()) // 2,
            )
        super().resizeEvent(event)

    def adjust_aspect_ratio(self):
        video_size = self.media_player.video_get_size()
        if video_size:
            width, height = video_size
            if width > 0 and height > 0:
                self.aspect_ratio = width / height

    def on_media_length_changed(self, event):
        QMetaObject.invokeMethod(self, "media_length_changed", Qt.QueuedConnection)

    @Slot()
    def media_length_changed(self):
        duration = self.media.get_duration()

        # Check VLC's seekability - best indicator for HLS VOD vs live
        # HLS VOD has #EXT-X-ENDLIST, VLC detects this and reports seekable
        try:
            vlc_seekable = bool(self.media_player.is_seekable())
        except Exception:
            vlc_seekable = False

        # Determine if content is live using this priority:
        # 1. Explicit hint from content_type (STB/Xtream APIs know their content)
        # 2. VLC seekability (most reliable for HLS - checks #EXT-X-ENDLIST)
        # 3. Duration heuristic (fallback)
        if getattr(self, "_is_live_hint", None) is not None:
            self._is_live = self._is_live_hint
        elif vlc_seekable:
            # VLC says seekable = VOD (even if duration looks small)
            self._is_live = False
        elif duration <= 0:
            # No duration and not seekable = live
            self._is_live = True
        elif duration < 60000:
            # Small duration (<60s) and not seekable = likely live buffer
            self._is_live = True
        else:
            # Long duration = probably VOD even if not seekable
            self._is_live = False

        self._seekable = vlc_seekable and not self._is_live
        if not self._is_live:  # VOD content
            # Configure progress bar but keep hidden until hover/activity
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setFormat("00:00 / " + self.format_time(duration))
            try:
                self.progress_bar.setEnabled(bool(self._seekable))
            except Exception:
                pass
            self.update_timer.start()
            # Start position save timer for VOD content
            self.position_save_timer.start()
            # If VOD is reported non-seekable, retry once with a minimal option profile
            if not self._seekable and not self._seek_retry_done:
                self._seek_retry_done = True
                QTimer.singleShot(0, self._retry_minimal_vod_profile)
            # Handle resume position if set
            if self._resume_position is not None and self._resume_position > 0:
                QTimer.singleShot(500, self._apply_resume_position)
        else:  # Live content
            self.update_timer.stop()
            self.position_save_timer.stop()  # No position tracking for live
            self.progress_bar.setFormat("Live")
        # Always start with progress bar hidden - show on hover/activity
        self.progress_bar.setVisible(False)
        self._ui_visible = False

    def on_vlc_error(self, event):
        # We don't use event data here, just log that an error occurred
        self.vlc_logger.log("An error occurred during playback")
        QMetaObject.invokeMethod(self, "media_error_occurred", Qt.QueuedConnection)

    @Slot()
    def media_error_occurred(self):
        self.handle_error("Playback error occurred")

    def handle_error(self, error_message):
        vlc_error = self.vlc_logger.get_latest_error()
        if vlc_error:
            error_message += f": {vlc_error}"
        logging.error(f"VLC Error: {error_message}")
        self.progress_bar.setVisible(True)
        self.progress_bar.setFormat(f"Error: {error_message}")
        self.progress_bar.setValue(0)
        self.update_timer.stop()

    def show_ui(self):
        """Show progress bar and cursor on mouse activity."""
        if not self._ui_visible:
            # Show progress bar for both live and VOD (shows "Live" or time)
            self.progress_bar.setVisible(True)
            self.setCursor(Qt.ArrowCursor)
            self._ui_visible = True

    def on_inactivity(self):
        """Hide progress bar and cursor after inactivity period."""
        if self._ui_visible:
            # Always hide progress bar on inactivity (windowed or fullscreen)
            self.progress_bar.setVisible(False)
            # Only hide cursor in fullscreen
            if self.isFullScreen():
                self.setCursor(Qt.BlankCursor)
            self._ui_visible = False
        self.inactivity_timer.stop()

    def toggle_fullscreen(self):
        if self.windowState() == Qt.WindowNoState:
            QGuiApplication.setOverrideCursor(Qt.WaitCursor)
            self.video_frame.show()
            self.setWindowState(Qt.WindowFullScreen)
            QGuiApplication.restoreOverrideCursor()
        else:
            # Exiting fullscreen - reset drag state to prevent window sticking to cursor
            self.dragging = False
            self.resizing = False
            self.setWindowState(Qt.WindowNoState)

    def _retry_minimal_vod_profile(self):
        try:
            if not self._last_url:
                return
            last_pos = 0
            try:
                last_pos = max(0, int(self.media_player.get_time()))
            except Exception:
                last_pos = 0
            self.media_player.stop()
            # Rebuild media with minimal, VLC-default-friendly options
            m = self.instance.media_new(self._last_url)
            try:
                m.add_option(":http-user-agent=VLC/3.0.20")
                m.add_option(":input-timeshift-granularity=0")
                m.add_option(":network-caching=3000")
            except Exception:
                pass
            self.media = m
            self.media_player.set_media(self.media)
            self.media_player.play()
            if last_pos > 0:
                QTimer.singleShot(1200, lambda: self.media_player.set_time(last_pos))
        except Exception:
            pass

    def _emit_position_changed(self):
        """Emit position for progress tracking (called by position_save_timer)."""
        try:
            if self._is_live:
                return
            state = self.media_player.get_state()
            if state not in (vlc.State.Playing, vlc.State.Paused):
                return
            position_ms = self.media_player.get_time()
            duration_ms = self.media.get_duration() if self.media else 0
            if position_ms > 0 and duration_ms > 0:
                self.positionChanged.emit(position_ms, duration_ms)
        except Exception:
            pass

    def _apply_resume_position(self):
        """Seek to the stored resume position after playback starts."""
        try:
            if self._resume_position is not None and self._resume_position > 0:
                self.media_player.set_time(self._resume_position)
        except Exception:
            pass
