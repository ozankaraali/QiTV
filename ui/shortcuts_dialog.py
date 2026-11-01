"""Help → Keyboard Shortcuts: the keymap's SHORTCUTS table, one titled section per screen."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services.i18n import N_, translate
from ui.keymap import SHORTCUT_TITLES, SHORTCUTS
from ui.theme import PALETTE, RADIUS_CARD, qcolor

TITLE = N_("Keyboard Shortcuts")  # i18n: the window's title (Help → Keyboard Shortcuts)
# i18n: the columns of Help → Keyboard Shortcuts: the keys, and the action they do (noun)
HEADERS = (N_("Keys"), N_("Action"))
MIN_WIDTH = 560


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
    """(keys, action, is_heading) in display order: each section's title, then its rows (the
    titles and actions in the active language)."""
    rows: list[tuple[str, str, bool]] = []
    for context, entries in SHORTCUTS.items():
        rows.append((translate(SHORTCUT_TITLES[context]), "", True))
        rows += [(key_label(keys), translate(what), False) for keys, what in entries]
    return rows


class ShortcutsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(translate(TITLE))
        self.rows = table_rows()
        self.table = QTableWidget(len(self.rows), len(HEADERS), self)
        self.table.setHorizontalHeaderLabels([translate(header) for header in HEADERS])
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
        # A long action wraps onto a second line where the window is too narrow for it.
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
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
        self.resize(self._fitting_width(), 640)

    def _fitting_width(self) -> int:
        """Wide enough for the longest keys and action on one line each (at least 560 px),
        but within nine tenths of the screen: a longer action wraps."""
        metrics = self.table.fontMetrics()
        pad = 4 * metrics.averageCharWidth()  # each cell's margins
        keys = max((metrics.horizontalAdvance(k) for k, _w, h in self.rows if not h), default=0)
        what = max((metrics.horizontalAdvance(w) for _k, w, h in self.rows if not h), default=0)
        scrollbar = self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        margins = self.layout().contentsMargins()
        width = keys + what + 2 * pad + scrollbar + margins.left() + margins.right() + 8
        screen = self.screen() or QGuiApplication.primaryScreen()
        room = int(screen.availableGeometry().width() * 0.9) if screen is not None else width
        return max(MIN_WIDTH, min(width, room))
