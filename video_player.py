import logging
import platform
import sys

import vlc
from PySide6.QtCore import QMetaObject, QPoint, Qt, QTimer, Slot, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QMainWindow, QProgressBar, QVBoxLayout

logging.basicConfig(level=logging.ERROR)


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

    def __init__(self, config_manager, *args, **kwargs):
        super(VideoPlayer, self).__init__(*args, **kwargs)
        self.config_manager = config_manager
        self.config = self.config_manager.config

        # Start normally, not on top
        self.is_pip_mode = False
        self.normal_geometry = None
        self.aspect_ratio = (
            16 / 9
        )  # Default aspect ratio; will be updated when video plays
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
        self.video_frame.installEventFilter(self)
        t_lay_parent.addWidget(self.video_frame)

        # Custom user-agent string
        user_agent = "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3"
        self.vlc_logger = VLCLogger()

        # Initialize VLC instance
        self.instance = vlc.Instance(
            ["--video-on-top", f"--http-user-agent={user_agent}"]
        )  # vlc.Instance(["--verbose=2"])  # Enable verbose logging

        self.media_player = self.instance.media_player_new()
        self.media_player.video_set_mouse_input(False)
        self.media_player.video_set_key_input(False)

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
        self.show()

        self.resize_corner = None

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1000)  # Use 1000 steps for smoother updates
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("00:00 / 00:00")
        self.mainFrame.layout().addWidget(self.progress_bar)

        self.update_timer = QTimer(self)
        self.update_timer.setInterval(100)  # Update every 100ms
        self.update_timer.timeout.connect(self.update_progress)

        self.progress_bar.mousePressEvent = self.seek_video

        self.click_position = None
        self.click_timer = QTimer(self)
        self.click_timer.setSingleShot(True)
        self.click_timer.timeout.connect(self.handle_click)

    def seek_video(self, event):
        if self.media_player.is_playing():
            width = self.progress_bar.width()
            click_position = event.position().x()
            seek_position = click_position / width
            self.media_player.set_position(seek_position)

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
        if state == vlc.State.Playing:
            current_time = self.media_player.get_time()
            total_time = self.media.get_duration()

            if total_time > 0:
                formatted_current = self.format_time(current_time)
                formatted_total = self.format_time(total_time)
                self.progress_bar.setFormat(f"{formatted_current} / {formatted_total}")
                self.progress_bar.setValue(int(current_time * 1000 / total_time))
            else:
                self.progress_bar.setFormat("Live")
                self.progress_bar.setValue(0)
        elif state == vlc.State.Error:
            self.handle_error("Playback error")
        elif state == vlc.State.Ended:
            self.progress_bar.setFormat("Playback ended")
            self.progress_bar.setValue(1000)  # Set to 100%
        elif state == vlc.State.Opening:
            self.progress_bar.setFormat("Opening...")
            self.progress_bar.setValue(0)
        elif state == vlc.State.Buffering:
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
        if event.key() == Qt.Key_Up:
            self.change_volume(10)  # Increase volume
        elif event.key() == Qt.Key_Down:
            self.change_volume(-10)  # Decrease volume
        elif event.key() == Qt.Key_Space:
            self.toggle_play_pause()  # Toggle Play/Pause
        elif event.key() == Qt.Key_M:
            self.toggle_mute()  # Toggle Mute
        elif event.key() == Qt.Key_Escape:
            self.setWindowState(Qt.WindowNoState)
        elif event.key() == Qt.Key_F:
            if self.windowState() == Qt.WindowNoState:
                QGuiApplication.setOverrideCursor(Qt.WaitCursor)
                self.video_frame.show()
                self.setWindowState(Qt.WindowFullScreen)

                self.activateWindow()  # Ensure the PiP window is focused and on top
                self.raise_()

                QGuiApplication.restoreOverrideCursor()
            else:
                self.setWindowState(Qt.WindowNoState)
        elif event.key() == Qt.Key_P and event.modifiers() == Qt.AltModifier:
            if self.windowState() == Qt.WindowFullScreen:
                self.setWindowState(Qt.WindowNoState)
            self.toggle_pip_mode()
        super().keyPressEvent(event)

    def change_volume(self, step):
        current_volume = self.media_player.audio_get_volume()
        new_volume = max(0, min(100, current_volume + step))
        self.media_player.audio_set_volume(new_volume)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.windowState() == Qt.WindowNoState:
                QGuiApplication.setOverrideCursor(Qt.WaitCursor)
                self.video_frame.show()
                self.setWindowState(Qt.WindowFullScreen)

                self.activateWindow()  # Ensure the PiP window is focused and on top
                self.raise_()

                QGuiApplication.restoreOverrideCursor()
            else:
                self.setWindowState(Qt.WindowNoState)

    def closeEvent(self, event):
        if self.media_player.is_playing():
            self.media_player.stop()
            self.stopped.emit()
        self.config_manager.save_window_settings(self, "video_player")
        self.hide()
        event.ignore()

    def play_video(self, video_url):
        if platform.system() == "Linux":
            self.media_player.set_xwindow(self.video_frame.winId())
        elif platform.system() == "Windows":
            self.media_player.set_hwnd(self.video_frame.winId())
        elif platform.system() == "Darwin":
            self.media_player.set_nsobject(int(self.video_frame.winId()))

        self.media = self.instance.media_new(video_url)
        self.media_player.set_media(self.media)

        events = self.media_player.event_manager()
        events.event_attach(
            vlc.EventType.MediaPlayerLengthChanged, self.on_media_length_changed
        )
        self.media.parse_with_options(1, 0)

        play_result = self.media_player.play()
        if play_result == -1:
            self.handle_error("Failed to start playback")
        else:
            self.adjust_aspect_ratio()
            self.show()
            self.playing.emit()
            QTimer.singleShot(5000, self.check_playback_status)

    def check_playback_status(self):
        state = self.media_player.get_state()
        if state == vlc.State.Playing: # only check if media has not been paused, or stopped
            if not self.media_player.is_playing():
                media_state = self.media.get_state()
                if media_state == vlc.State.Error:
                    self.handle_error("Playback error")
                else:
                    self.handle_error("Failed to start playback")
                self.stopped.emit()

    def stop_video(self):
        self.media_player.stop()
        self.progress_bar.setVisible(False)
        self.update_timer.stop()
        self.stopped.emit()

    def toggle_mute(self):
        state = self.media_player.audio_get_mute()
        self.media_player.audio_set_mute(not state)

    def toggle_play_pause(self):
        state = self.media_player.get_state()
        if state == vlc.State.Playing:
            self.media_player.pause()
        else:
            self.media_player.play()

    def toggle_pip_mode(self):
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        if not self.is_pip_mode:
            self.normal_geometry = (
                self.geometry()
            )  # Save current geometry for restoring later
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
            self.resize_and_position_pip()
            self.show()
        else:
            self.setWindowFlags(Qt.Window)
            self.setGeometry(self.normal_geometry)
            self.show()

        self.is_pip_mode = not self.is_pip_mode  # Toggle PiP mode
        self.activateWindow()  # Ensure the PiP window is focused and on top
        self.raise_()

        QGuiApplication.restoreOverrideCursor()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.click_position = event.globalPos()
            self.click_timer.start(
                200
            )  # Wait for 200ms to determine if it's a click or drag

        elif event.button() == Qt.RightButton:
            self.toggle_pip_mode()
            return

        if event.button() == Qt.LeftButton:
            self.dragging = False
            self.resizing = False
            self.start_size = self.size()
            self.start_pos = self.pos()
            self.drag_position = event.globalPos()

            # Determine the resize type (edges or corners)
            if event.pos().x() <= 10:  # Left
                if event.pos().y() <= 10:  # Top-left corner
                    self.resize_corner = "top_left"
                elif event.pos().y() >= self.height() - 10:  # Bottom-left corner
                    self.resize_corner = "bottom_left"
                else:  # Left edge
                    self.resize_corner = "left"
            elif event.pos().x() >= self.width() - 10:  # Right
                if event.pos().y() <= 10:  # Top-right corner
                    self.resize_corner = "top_right"
                elif event.pos().y() >= self.height() - 10:  # Bottom-right corner
                    self.resize_corner = "bottom_right"
                else:  # Right edge
                    self.resize_corner = "right"
            elif event.pos().y() <= 10:  # Top edge
                self.resize_corner = "top"
            elif event.pos().y() >= self.height() - 10:  # Bottom edge
                self.resize_corner = "bottom"
            else:  # Dragging
                self.dragging = True
                self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
                self.resize_corner = None
            self.resizing = bool(self.resize_corner)
            event.accept()

    def mouseMoveEvent(self, event):
        if (
            self.click_timer.isActive()
            and (event.globalPos() - self.click_position).manhattanLength() > 3
        ):
            self.click_timer.stop()  # Cancel the click timer if the mouse has moved

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
        elif self.dragging and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self.drag_position)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.click_timer.isActive():
            self.click_timer.stop()
            self.handle_click()

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
        pip_width = int(
            screen_geometry.width() * 0.25
        )  # PiP width is 25% of screen width
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
        if duration > 0:  # VOD content
            self.progress_bar.setVisible(True)
            self.progress_bar.setFormat("00:00 / " + self.format_time(duration))
            self.update_timer.start()
        else:  # Live content
            self.progress_bar.setVisible(False)  # Hide the progress bar
            self.progress_bar.setFormat("Live")
            # self.update_timer.start()

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
