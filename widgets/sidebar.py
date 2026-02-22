"""Left navigation sidebar for qiTV."""
import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


class SidebarButton(QPushButton):
    """A flat toggle-style button for the sidebar."""

    def __init__(self, text, parent=None, checkable=True):
        super().__init__(text, parent)
        self.setCheckable(checkable)
        self.setFlat(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(32)
        self.setStyleSheet("""
            QPushButton {
                text-align: left;
                padding: 6px 12px;
                border: none;
                border-radius: 6px;
                font-size: 13px;
            }
            QPushButton:checked {
                background-color: rgba(51, 153, 255, 0.15);
                font-weight: bold;
            }
            QPushButton:hover:!checked {
                background-color: rgba(128, 128, 128, 0.1);
            }
        """)


class Sidebar(QWidget):
    """Left navigation sidebar with providers, content types, and quick actions."""

    provider_selected = Signal(str)       # provider name or "all"
    content_type_changed = Signal(str)    # "itv", "vod", "series"
    favorites_toggled = Signal(bool)      # favorites filter on/off
    history_clicked = Signal()
    resume_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(140)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 4, 8)
        layout.setSpacing(2)

        # --- Provider section ---
        self._provider_buttons = {}
        self._provider_section = QVBoxLayout()
        self._provider_section.setSpacing(2)
        layout.addLayout(self._provider_section)

        # --- Separator ---
        layout.addWidget(self._make_separator())
        layout.addSpacing(4)

        # --- Content type section ---
        self._type_buttons = {}
        for label, content_type in [("Channels", "itv"), ("Movies", "vod"), ("Series", "series")]:
            btn = SidebarButton(label)
            btn.clicked.connect(lambda checked, ct=content_type: self._on_content_type(ct))
            self._type_buttons[content_type] = btn
            layout.addWidget(btn)
        self._type_buttons["itv"].setChecked(True)

        # --- Separator ---
        layout.addSpacing(4)
        layout.addWidget(self._make_separator())
        layout.addSpacing(4)

        # --- Quick actions ---
        self.favorites_btn = SidebarButton("\u2605 Favorites")
        self.favorites_btn.clicked.connect(
            lambda checked: self.favorites_toggled.emit(checked)
        )
        layout.addWidget(self.favorites_btn)

        self.history_btn = SidebarButton("\u23F1 History", checkable=False)
        self.history_btn.clicked.connect(self.history_clicked.emit)
        layout.addWidget(self.history_btn)

        self.resume_btn = SidebarButton("\u25B6 Resume", checkable=False)
        self.resume_btn.clicked.connect(self.resume_clicked.emit)
        layout.addWidget(self.resume_btn)

        layout.addStretch()

    def set_providers(self, provider_names):
        """Populate the provider section with 'All' + individual providers."""
        # Clear existing
        for btn in self._provider_buttons.values():
            btn.setParent(None)
            btn.deleteLater()
        self._provider_buttons.clear()

        # "All" button
        all_btn = SidebarButton("All")
        all_btn.clicked.connect(lambda: self._on_provider("all"))
        self._provider_section.addWidget(all_btn)
        self._provider_buttons["all"] = all_btn

        for name in provider_names:
            btn = SidebarButton(name)
            btn.clicked.connect(lambda checked, n=name: self._on_provider(n))
            self._provider_section.addWidget(btn)
            self._provider_buttons[name] = btn

        # Select first real provider by default (not "All")
        if provider_names:
            self._select_provider(provider_names[0])
        elif self._provider_buttons:
            self._select_provider("all")

    def select_provider(self, name):
        """Externally set the active provider."""
        self._select_provider(name)

    def select_content_type(self, content_type):
        """Externally set the active content type without emitting signal."""
        for ct, btn in self._type_buttons.items():
            btn.setChecked(ct == content_type)

    def _on_provider(self, name):
        self._select_provider(name)
        self.provider_selected.emit(name)

    def _select_provider(self, name):
        for n, btn in self._provider_buttons.items():
            btn.setChecked(n == name)

    def _on_content_type(self, content_type):
        for ct, btn in self._type_buttons.items():
            btn.setChecked(ct == content_type)
        self.content_type_changed.emit(content_type)

    @staticmethod
    def _make_separator():
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        return sep
