"""Auto-play countdown dialogs for episode, movie, and resume functionality."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout


class BaseCountdownDialog(QDialog):
    """Base class for countdown dialogs."""

    countdownFinished = Signal()
    cancelled = Signal()

    def __init__(
        self,
        parent=None,
        title: str = "Auto-play",
        countdown_seconds: int = 5,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.countdown_seconds = countdown_seconds
        self.remaining_seconds = countdown_seconds

        self.setWindowFlags(
            Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.CustomizeWindowHint | Qt.WindowTitleHint
        )
        self.setModal(False)  # Non-blocking

        self._setup_ui()
        self._setup_timer()

    def _setup_ui(self):
        """Set up the dialog UI. Override in subclasses for custom layouts."""
        self.layout = QVBoxLayout(self)

        self.message_label = QLabel(self)
        self.message_label.setWordWrap(True)
        self.layout.addWidget(self.message_label)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, self.countdown_seconds * 10)  # 100ms precision
        self.progress_bar.setValue(self.countdown_seconds * 10)
        self.progress_bar.setTextVisible(False)
        self.layout.addWidget(self.progress_bar)

        # Buttons
        self.button_layout = QHBoxLayout()

        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self._on_cancel)
        self.button_layout.addWidget(self.cancel_button)

        self.layout.addLayout(self.button_layout)

    def _setup_timer(self):
        """Set up the countdown timer."""
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(100)  # Update every 100ms for smooth progress
        self.countdown_timer.timeout.connect(self._on_tick)

    def _on_tick(self):
        """Handle timer tick."""
        current = self.progress_bar.value()
        if current <= 0:
            self.countdown_timer.stop()
            self.countdownFinished.emit()
            self.accept()
        else:
            self.progress_bar.setValue(current - 1)
            self.remaining_seconds = current // 10
            self._update_message()

    def _update_message(self):
        """Update the message label. Override in subclasses."""
        pass

    def _on_cancel(self):
        """Handle cancel button click."""
        self.countdown_timer.stop()
        self.cancelled.emit()
        self.reject()

    def showEvent(self, event):
        """Start countdown when dialog is shown."""
        super().showEvent(event)
        self._update_message()
        self.countdown_timer.start()

    def closeEvent(self, event):
        """Stop timer on close."""
        self.countdown_timer.stop()
        super().closeEvent(event)


class EpisodeAutoPlayDialog(BaseCountdownDialog):
    """Dialog for auto-playing next episode with countdown."""

    def __init__(
        self,
        parent=None,
        next_episode_name: str = "Next Episode",
        countdown_seconds: int = 5,
    ):
        self.next_episode_name = next_episode_name
        super().__init__(
            parent=parent,
            title="Next Episode",
            countdown_seconds=countdown_seconds,
        )
        self.setMinimumWidth(350)

    def _update_message(self):
        self.message_label.setText(
            f"<b>Up next:</b> {self.next_episode_name}\n\n"
            f"Playing in {self.remaining_seconds}s..."
        )


class MovieSuggestionDialog(BaseCountdownDialog):
    """Dialog for suggesting next movie from same category."""

    playNowClicked = Signal()

    def __init__(
        self,
        parent=None,
        movie_name: str = "Next Movie",
        category_name: str = "",
        countdown_seconds: int = 10,
    ):
        self.movie_name = movie_name
        self.category_name = category_name
        super().__init__(
            parent=parent,
            title="Suggested Movie",
            countdown_seconds=countdown_seconds,
        )
        self.setMinimumWidth(400)

    def _setup_ui(self):
        """Set up custom UI with Play Now button."""
        self.layout = QVBoxLayout(self)

        self.message_label = QLabel(self)
        self.message_label.setWordWrap(True)
        self.layout.addWidget(self.message_label)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, self.countdown_seconds * 10)
        self.progress_bar.setValue(self.countdown_seconds * 10)
        self.progress_bar.setTextVisible(False)
        self.layout.addWidget(self.progress_bar)

        # Buttons
        self.button_layout = QHBoxLayout()

        self.play_now_button = QPushButton("Play Now", self)
        self.play_now_button.clicked.connect(self._on_play_now)
        self.button_layout.addWidget(self.play_now_button)

        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self._on_cancel)
        self.button_layout.addWidget(self.cancel_button)

        self.layout.addLayout(self.button_layout)

    def _on_play_now(self):
        """Handle Play Now button click."""
        self.countdown_timer.stop()
        self.playNowClicked.emit()
        self.countdownFinished.emit()
        self.accept()

    def _update_message(self):
        category_text = f" from <b>{self.category_name}</b>" if self.category_name else ""
        self.message_label.setText(
            f"Here's a video{category_text} you may like:\n\n"
            f"<b>{self.movie_name}</b>\n\n"
            f"Playing in {self.remaining_seconds}s..."
        )


class ResumeCountdownDialog(BaseCountdownDialog):
    """Dialog for resuming playback with countdown."""

    resumeClicked = Signal()
    startOverClicked = Signal()

    def __init__(
        self,
        parent=None,
        content_name: str = "Content",
        resume_position_text: str = "00:00",
        countdown_seconds: int = 10,
    ):
        self.content_name = content_name
        self.resume_position_text = resume_position_text
        super().__init__(
            parent=parent,
            title="Resume Playback",
            countdown_seconds=countdown_seconds,
        )
        self.setMinimumWidth(350)

    def _setup_ui(self):
        """Set up custom UI with Resume and Start Over buttons."""
        self.layout = QVBoxLayout(self)

        self.message_label = QLabel(self)
        self.message_label.setWordWrap(True)
        self.layout.addWidget(self.message_label)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, self.countdown_seconds * 10)
        self.progress_bar.setValue(self.countdown_seconds * 10)
        self.progress_bar.setTextVisible(False)
        self.layout.addWidget(self.progress_bar)

        # Buttons
        self.button_layout = QHBoxLayout()

        self.resume_button = QPushButton("Resume", self)
        self.resume_button.clicked.connect(self._on_resume)
        self.button_layout.addWidget(self.resume_button)

        self.start_over_button = QPushButton("Start Over", self)
        self.start_over_button.clicked.connect(self._on_start_over)
        self.button_layout.addWidget(self.start_over_button)

        self.layout.addLayout(self.button_layout)

    def _on_resume(self):
        """Handle Resume button click."""
        self.countdown_timer.stop()
        self.resumeClicked.emit()
        self.countdownFinished.emit()  # Resume is the default action
        self.accept()

    def _on_start_over(self):
        """Handle Start Over button click."""
        self.countdown_timer.stop()
        self.startOverClicked.emit()
        self.reject()

    def _on_cancel(self):
        """Override to do nothing - we only have Resume/Start Over."""
        pass

    def _update_message(self):
        self.message_label.setText(
            f"Resume <b>{self.content_name}</b> from {self.resume_position_text}?\n\n"
            f"Resuming in {self.remaining_seconds}s..."
        )


class SeriesCompleteDialog(QDialog):
    """Dialog shown when a series is complete (no more episodes)."""

    def __init__(self, parent=None, series_name: str = "Series"):
        super().__init__(parent)
        self.setWindowTitle("Series Complete")
        self.setModal(False)
        self.setMinimumWidth(300)

        layout = QVBoxLayout(self)

        message = QLabel(f"<b>{series_name}</b>\n\nYou've reached the end of this series!")
        message.setWordWrap(True)
        layout.addWidget(message)

        ok_button = QPushButton("OK", self)
        ok_button.clicked.connect(self.accept)
        layout.addWidget(ok_button)


class NoCategoryMoviesDialog(QDialog):
    """Dialog shown when there are no more unwatched movies in a category."""

    def __init__(self, parent=None, category_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("No More Movies")
        self.setModal(False)
        self.setMinimumWidth(300)

        layout = QVBoxLayout(self)

        if category_name:
            msg = f"No more unwatched movies in <b>{category_name}</b>."
        else:
            msg = "No more unwatched movies in this category."

        message = QLabel(msg)
        message.setWordWrap(True)
        layout.addWidget(message)

        ok_button = QPushButton("OK", self)
        ok_button.clicked.connect(self.accept)
        layout.addWidget(ok_button)
