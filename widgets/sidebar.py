"""Left navigation sidebar for qiTV."""

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QLabel,
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
        self.setStyleSheet(
            """
            QPushButton {
                text-align: left;
                padding: 6px 12px;
                border: none;
                border-radius: 6px;
                font-size: 13px;
            }
            QPushButton:checked {
                background-color: rgba(201, 107, 67, 0.24);
                color: #C96B43;
                font-weight: bold;
            }
            QPushButton:hover:!checked {
                background-color: rgba(255, 255, 255, 0.06);
            }
        """
        )


class Sidebar(QWidget):
    """Left navigation sidebar with content types, quick actions, and provider switcher."""

    provider_selected = Signal(str)  # provider name or "all"
    content_type_changed = Signal(str)  # "itv", "vod", "series"
    favorites_toggled = Signal(bool)  # favorites filter on/off
    history_clicked = Signal()
    resume_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(150)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 4, 8)
        layout.setSpacing(2)

        # --- Content type section ---
        self._type_buttons = {}
        self._type_button_group = QButtonGroup(self)
        self._type_button_group.setExclusive(True)
        for label, content_type in [
            ("Channels", "itv"),
            ("Movies", "vod"),
            ("Series", "series"),
        ]:
            btn = SidebarButton(label)
            btn.clicked.connect(lambda checked, ct=content_type: self._on_content_type(ct))
            self._type_button_group.addButton(btn)
            self._type_buttons[content_type] = btn
            layout.addWidget(btn)
        self._type_buttons["itv"].setChecked(True)

        # --- Separator ---
        layout.addSpacing(4)
        layout.addWidget(self._make_separator())
        layout.addSpacing(4)

        # --- Quick actions ---
        self.favorites_btn = SidebarButton("\u2605 Favorites")
        self.favorites_btn.clicked.connect(lambda checked: self.favorites_toggled.emit(checked))
        layout.addWidget(self.favorites_btn)

        self.history_btn = SidebarButton("\u23f1 History", checkable=False)
        self.history_btn.clicked.connect(self.history_clicked.emit)
        layout.addWidget(self.history_btn)

        self.resume_btn = SidebarButton("\u25b6 Resume", checkable=False)
        self.resume_btn.clicked.connect(self.resume_clicked.emit)
        layout.addWidget(self.resume_btn)

        # --- Separator ---
        layout.addSpacing(4)
        layout.addWidget(self._make_separator())
        layout.addSpacing(4)

        # --- Search all providers ---
        self.all_btn = SidebarButton("\U0001f50d Search All", checkable=False)
        self.all_btn.clicked.connect(lambda: self.provider_selected.emit("all"))
        layout.addWidget(self.all_btn)

        layout.addStretch()

        # --- Provider switcher at bottom ---
        layout.addWidget(self._make_separator())
        layout.addSpacing(4)

        provider_label = QLabel("Provider")
        provider_label.setStyleSheet(
            "font-size: 11px; color: rgba(255,255,255,0.5); padding-left: 4px;"
        )
        layout.addWidget(provider_label)
        layout.addSpacing(2)

        self.provider_combo = QComboBox()
        self.provider_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.provider_combo.setMinimumHeight(28)
        self.provider_combo.setStyleSheet(
            """
            QComboBox {
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 12px;
                border: 1px solid rgba(201, 107, 67, 0.35);
            }
            QComboBox:hover {
                border: 1px solid rgba(201, 107, 67, 0.55);
            }
        """
        )
        self.provider_combo.currentTextChanged.connect(self._on_combo_provider_changed)
        layout.addWidget(self.provider_combo)

    def set_providers(self, provider_names):
        """Populate the provider dropdown."""
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        for name in provider_names:
            self.provider_combo.addItem(name)
        self.provider_combo.blockSignals(False)

    def select_provider(self, name):
        """Externally set the active provider."""
        self.provider_combo.blockSignals(True)
        index = self.provider_combo.findText(name)
        if index >= 0:
            self.provider_combo.setCurrentIndex(index)
        self.provider_combo.blockSignals(False)

    def select_content_type(self, content_type):
        """Externally set the active content type without emitting signal."""
        for ct, btn in self._type_buttons.items():
            btn.setChecked(ct == content_type)

    def _on_combo_provider_changed(self, name):
        if name:
            self.provider_selected.emit(name)

    def _on_content_type(self, content_type):
        for ct, btn in self._type_buttons.items():
            btn.setChecked(ct == content_type)
        self.content_type_changed.emit(content_type)

    @staticmethod
    def _make_separator():
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Plain)
        sep.setStyleSheet("background-color: rgba(201, 107, 67, 0.3); max-height: 1px;")
        return sep
