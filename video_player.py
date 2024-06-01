import platform
import sys
import vlc
from PySide6.QtCore import Qt, QEvent, QPoint
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMainWindow, QFrame, QHBoxLayout

class VideoPlayer(QMainWindow):
    def __init__(self, config_manager, *args, **kwargs):
        super(VideoPlayer, self).__init__(*args, **kwargs)
        self.config_manager = config_manager
        self.config = self.config_manager.config

        # Start normally, not on top
        self.is_pip_mode = False
        self.normal_geometry = None
        self.aspect_ratio = 16 / 9  # Default aspect ratio
        self.setWindowFlags(Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self.dragging = False
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

    def eventFilter(self, obj, event):
        if obj == self.video_frame:
            if event.type() == QEvent.Wheel:
                self.wheelEvent(event)
                return True
            elif event.type() == QEvent.KeyPress:
                self.keyPressEvent(event)
                return True
        return False

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
                self.video_frame.show()
                self.setWindowState(Qt.WindowFullScreen)
            else:
                self.setWindowState(Qt.WindowNoState)
        elif event.key() == Qt.Key_P and event.modifiers() == Qt.AltModifier:
            self.toggle_pip_mode()
        super().keyPressEvent(event)

    def change_volume(self, step):
        current_volume = self.media_player.audio_get_volume()
        new_volume = max(0, min(100, current_volume + step))
        self.media_player.audio_set_volume(new_volume)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.windowState() == Qt.WindowNoState:
                self.video_frame.show()
                self.setWindowState(Qt.WindowFullScreen)
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
        if not self.is_pip_mode:
            self.normal_geometry = self.geometry()  # Save current geometry for restoring later
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
            self.show()
            self.video_frame.setStyleSheet("background: black;")  # Prevents black background
            self.resize_to_pip_size()
            self.move_to_bottom_right()  # Move window to bottom right on first enter PiP mode
            self.dragging = True  # Enable dragging in PiP mode
        else:
            self.setWindowFlags(Qt.Window)
            self.show()
            self.video_frame.setStyleSheet("")  # Reset background style
            self.setGeometry(self.normal_geometry)
            self.dragging = False  # Disable dragging in non-PiP mode

        self.is_pip_mode = not self.is_pip_mode  # Toggle PiP mode
        self.show()  # Ensure the window is visible after changing flags

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.is_pip_mode:
            self.dragging = True
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.dragging and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self.drag_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.dragging = False

    def resize_to_aspect_ratio(self):
        current_size = self.size()
        width = current_size.width()
        height = int(width / self.aspect_ratio)
        self.resize(width, height)
        self.update()

    def resize_to_pip_size(self):
        screen_geometry = QGuiApplication.primaryScreen().availableGeometry()
        pip_width = int(screen_geometry.width() * 0.25)  # PiP width is 20% of screen width
        pip_height = int(pip_width / self.aspect_ratio)
        self.setFixedSize(pip_width, pip_height)
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

    def move_to_bottom_right(self):
        screen_geometry = QGuiApplication.primaryScreen().availableGeometry()
        window_size = self.size()
        x = screen_geometry.width() - window_size.width() - 10  # 10 pixels padding from edge
        y = screen_geometry.height() - window_size.height() - 10  # 10 pixels padding from edge
        self.move(x, y)
