"""Settings → Providers → Hidden groups (2.9.1): the groups hidden to tidy the lists
(services.hidden_groups), each shown again with a click, no PIN."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from services.i18n import N_, tr, translate

# i18n: Settings → Providers → Hidden groups: what the list is, and how a group got there
INTRO = N_("Groups and genres hidden with “Hide group” in a list's menu. Nothing is locked.")
SECTIONS = {
    "live": N_("Live TV"),  # i18n: Settings → Providers → Hidden groups: a group's kind
    "movies": N_("Movies"),  # i18n: Settings → Providers → Hidden groups: a group's kind
    "series": N_("Series"),  # i18n: Settings → Providers → Hidden groups: a group's kind
}


def add_hidden_groups_box(dialog: Any, layout: Any, parent: QWidget) -> Optional[QGroupBox]:
    """The box, at the end of `layout` (none without the window's hidden groups)."""
    window = dialog.parent()
    tidy = getattr(window, "hidden_groups", None)
    if tidy is None:
        return None
    box = QGroupBox(tr("Hidden groups"), parent)  # i18n: Settings → Providers: a box
    inner = QVBoxLayout(box)
    intro = QLabel(translate(INTRO), box)
    intro.setWordWrap(True)
    inner.addWidget(intro)
    listing = QListWidget(box)
    listing.setMaximumHeight(140)
    inner.addWidget(listing)
    row = QHBoxLayout()
    show_again = QPushButton(tr("Show again"), box)
    row.addWidget(show_again)
    row.addStretch(1)
    inner.addLayout(row)
    layout.addRow(box)
    dialog.hidden_groups_widgets = {"list": listing, "show_again": show_again}

    def refresh() -> None:
        listing.clear()
        for entry in tidy.entries():
            kind = SECTIONS.get(str(entry.get("section")))
            text = str(entry.get("title") or entry.get("key"))
            details = [translate(kind)] if kind else []
            if entry.get("provider"):
                details.append(str(entry["provider"]))
            if details:
                text = f"{text} ({', '.join(details)})"
            row_item = QListWidgetItem(text)
            row_item.setData(Qt.ItemDataRole.UserRole, entry.get("key"))
            listing.addItem(row_item)
        if listing.count():
            listing.setCurrentRow(0)
        show_again.setEnabled(listing.count() > 0)
        box.setVisible(listing.count() > 0)

    def reveal() -> None:
        current = listing.currentItem()
        if current is not None and tidy.show_again(str(current.data(Qt.ItemDataRole.UserRole))):
            controller = getattr(window, "controller", None)
            changed = getattr(controller, "lists_changed", None)
            if changed is not None:
                changed()
        refresh()

    show_again.clicked.connect(reveal)
    refresh()
    return box
