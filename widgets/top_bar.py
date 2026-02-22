"""Slim top bar with back button, search field, and hamburger menu."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QMenu, QPushButton, QSizePolicy, QWidget


class TopBar(QWidget):
    """Top toolbar: [Back] [Search...] [Hamburger]"""

    search_changed = Signal(str)  # Debounced search text
    back_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        # Back button
        self.back_button = QPushButton("\u2190 Back")
        self.back_button.setVisible(False)
        self.back_button.clicked.connect(self.back_clicked.emit)
        self.back_button.setFixedWidth(70)
        layout.addWidget(self.back_button)

        # Search field
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search content...")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMinimumHeight(30)
        self.search_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.search_box.setStyleSheet(
            """
            QLineEdit {
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 13px;
                border: 1px solid rgba(201, 107, 67, 0.35);
            }
            QLineEdit:focus {
                border: 1px solid rgba(201, 107, 67, 0.7);
            }
        """
        )
        layout.addWidget(self.search_box)

        # Debounce timer for search
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._emit_search)
        self.search_box.textChanged.connect(self._on_text_changed)

        # Hamburger menu button
        self.hamburger_button = QPushButton("\u2630")
        self.hamburger_button.setFixedSize(30, 30)
        self.hamburger_button.setStyleSheet(
            """
            QPushButton {
                font-size: 18px;
                border: none;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: rgba(201, 107, 67, 0.22);
            }
        """
        )
        layout.addWidget(self.hamburger_button)

        # Hamburger menu (populated by caller)
        self.hamburger_menu = QMenu(self)
        self.hamburger_button.setMenu(self.hamburger_menu)

    def set_back_visible(self, visible):
        self.back_button.setVisible(visible)

    def clear_search(self):
        self.search_box.clear()

    def search_text(self):
        return self.search_box.text()

    def _on_text_changed(self, text):
        self._search_timer.start()

    def _emit_search(self):
        self.search_changed.emit(self.search_box.text())
