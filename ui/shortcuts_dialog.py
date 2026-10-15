"""Help → Keyboard Shortcuts: the keymap's SHORTCUTS table, one titled section per screen."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.keymap import SHORTCUT_TITLES, SHORTCUTS
from ui.theme import PALETTE, RADIUS_CARD, qcolor

TITLE = "Keyboard Shortcuts"
HEADERS = ("Keys", "Action")


def key_label(keys: str) -> str:
    """How a SHORTCUTS entry's keys read here: native names (⌘, ↑ on macOS), "0–9" ranges."""
    labels = []
    for part in keys.split(", "):
        first, ellipsis, last = part.partition("…")
        if ellipsis and len(first) == 1 and len(last) == 1:
            labels.append(f"{first}–{last}")
        else:
            labels.append(QKeySequence(part).toString(QKeySequence.SequenceFormat.NativeText))
    return " / ".join(labels)


def table_rows() -> list[tuple[str, str, bool]]:
    """(keys, action, is_heading) in display order: each section's title, then its rows."""
    rows: list[tuple[str, str, bool]] = []
    for context, entries in SHORTCUTS.items():
        rows.append((SHORTCUT_TITLES[context], "", True))
        rows += [(key_label(keys), what, False) for keys, what in entries]
    return rows


class ShortcutsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self.rows = table_rows()
        self.table = QTableWidget(len(self.rows), len(HEADERS), self)
        self.table.setHorizontalHeaderLabels(list(HEADERS))
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        heading_font = QFont(self.font())
        heading_font.setBold(True)
        for row, (keys, what, heading) in enumerate(self.rows):
            if heading:
                item = QTableWidgetItem(keys)
                item.setFont(heading_font)
                item.setForeground(qcolor(PALETTE.accent))
                self.table.setItem(row, 0, item)
                self.table.setSpan(row, 0, 1, len(HEADERS))
                continue
            for column, text in enumerate((keys, what)):
                item = QTableWidgetItem(text)
                item.setForeground(qcolor(PALETTE.text))
                self.table.setItem(row, column, item)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet(
            f"QTableWidget {{ background: {PALETTE.panel_raised}; color: {PALETTE.text};"
            f" border: none; border-radius: {RADIUS_CARD}px; }}"
            f" QHeaderView::section {{ background: {PALETTE.canvas}; color: {PALETTE.text};"
            " border: none; padding: 4px 8px; }"
        )
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(buttons)
        self.resize(560, 640)
