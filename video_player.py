import platform
import sys

import vlc
from PySide6.QtCore import Qt, QEvent, QPoint, QTimer
from PySide6.QtGui import QGuiApplication, QCursor
from PySide6.QtWidgets import QMainWindow, QFrame, QHBoxLayout


class VideoPlayer(QMainWindow):
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
        t_lay_parent = QHBoxLayout()
        t_lay_parent.setContentsMargins(0, 0, 0, 0)

        self.video_frame = QFrame()
        self.video_frame.mouseDoubleClickEvent = self.mouseDoubleClickEvent
        self.video_frame.installEventFilter(self)
        t_lay_parent.addWidget(self.video_frame)
        self.instance = vlc.Instance(["--video-on-top"])
        self.media_player = self.instance.media_player_new()
        self.media_player.video_set_mouse_input(False)
        self.media_player.video_set_key_input(False)

        if sys.platform.startswith("linux"):
            self.media_player.set_xwindow(self.video_frame.winId())
        elif sys.platform == "win32":
            self.media_player.set_hwnd(self.video_frame.winId())
        elif sys.platform == "darwin":
            self.media_player.set_nsobject(int(self.video_frame.winId()))

        self.mainFrame.setLayout(t_lay_parent)
        self.show()

        self.resize_corner = None

        # Initialize the inactivity timer and set up cursor hiding mechanism
        self.inactivity_timer = QTimer(self)
        self.inactivity_timer.setInterval(5000)  # 5000 milliseconds = 5 seconds
        self.inactivity_timer.timeout.connect(self.hide_cursor)
        self.inactivity_timer.start()

        # Set cursor visibility state
        self.cursor_visible = True
        self.last_mouse_pos = self.video_frame.mapFromGlobal(QCursor.pos())

    def eventFilter(self, obj, event):
        if obj == self.video_frame:
            if event.type() == QEvent.MouseMove:
                if not self.cursor_visible or event.pos() != self.last_mouse_pos:
                    self.reset_inactivity_timer()
                    self.last_mouse_pos = event.pos()
                return True
            elif event.type() == QEvent.Wheel:
                self.wheelEvent(event)
                return True
            elif event.type() == QEvent.KeyPress:
                self.keyPressEvent(event)
                return True
        return False

    def reset_inactivity_timer(self):
        self.inactivity_timer.start()  # Reset the timer
        if not self.cursor_visible:
            self.show_cursor()

    def hide_cursor(self):
        self.video_frame.setCursor(Qt.BlankCursor)
        self.cursor_visible = False

    def show_cursor(self):
        self.video_frame.unsetCursor()
        self.cursor_visible = True

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
        self.config_manager.save_window_settings(self.geometry(), "video_player")
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
        self.media_player.play()
        self.adjust_aspect_ratio()  # Ensure the aspect ratio is set initially
        self.show()

    def stop_video(self):
        self.media_player.stop()

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
        self.dragging = False
        self.resizing = False
        self.resize_corner = None

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
