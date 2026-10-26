"""Settings → Parental controls (2.8): the PIN, locking and unlocking, and what is hidden.

A PIN (four digits or more) turns parental controls on. Channels and groups are hidden from the
channel list's right-click (and groups that look adult with one button here); while QiTV is
locked they are left out everywhere, and what they protect never enters the history. Unlocking
asks for the PIN and lasts an hour; Lock now ends it. Each action applies at once (not on Save):
`window.parental_changed()` refreshes the lists.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from services.i18n import N_, tr, translate
from services.parental import Parental

MIN_DIGITS = 4
# i18n: Settings → Parental controls: what the tab does
INTRO = N_(
    "A PIN hides channels and groups you choose (right-click them in the channel list) while"
    " QiTV is locked, and keeps them out of the history. Unlocking with the PIN lasts an hour."
)


def _ask_pin(parent: QWidget, label: str) -> Optional[str]:
    # i18n: the title of the window that asks for the parental controls' PIN
    text, ok = QInputDialog.getText(parent, tr("Parental controls"), label, QLineEdit.Password)
    return text if ok else None


def build_parental_tab(dialog: Any) -> Optional[QWidget]:
    """The tab, added to `dialog.options_tab` (none without the window's parental controls)."""
    window = dialog.parent()
    guard: Optional[Parental] = getattr(window, "parental", None)
    if guard is None:
        return None
    tab = QWidget(dialog)
    # i18n: a tab of the Settings window
    dialog.options_tab.addTab(tab, tr("Parental controls"))
    layout = QVBoxLayout(tab)
    intro = QLabel(translate(INTRO), tab)
    intro.setWordWrap(True)
    layout.addWidget(intro)
    status = QLabel(tab)
    layout.addWidget(status)
    row = QHBoxLayout()
    set_pin = QPushButton(tab)
    remove_pin = QPushButton(tr("Remove PIN…"), tab)  # i18n: Settings → Parental controls
    unlock = QPushButton(tr("Unlock for an hour…"), tab)  # i18n: Settings → Parental controls
    lock = QPushButton(tr("Lock now"), tab)  # i18n: Settings → Parental controls
    for button in (set_pin, remove_pin, unlock, lock):
        row.addWidget(button)
    layout.addLayout(row)
    # i18n: Settings → Parental controls (2.8): a lasting rule; also the groups a provider adds
    # later ("XXX", "Adult", "18+" as group names say it)
    adult = QCheckBox(tr("Hide groups whose names look adult (XXX, Adult, 18+…)"), tab)
    layout.addWidget(adult)
    hidden_label = QLabel(tab)
    layout.addWidget(hidden_label)
    hidden = QListWidget(tab)
    layout.addWidget(hidden)
    row2 = QHBoxLayout()
    show_again = QPushButton(tr("Show again"), tab)  # i18n: Settings → Parental controls
    row2.addWidget(show_again)
    row2.addStretch(1)
    layout.addLayout(row2)
    dialog.parental_widgets = {
        "status": status,
        "set_pin": set_pin,
        "remove_pin": remove_pin,
        "unlock": unlock,
        "lock": lock,
        "hidden": hidden,
        "show_again": show_again,
        "adult": adult,
    }

    def changed() -> None:
        refresh()
        notify = getattr(window, "parental_changed", None)
        if notify is not None:
            notify()

    def refresh() -> None:
        has_pin = guard.has_pin()
        if not has_pin:
            # i18n: Settings → Parental controls: no PIN, nothing is hidden
            status.setText(tr("Off: no PIN is set."))
        elif guard.locked():
            status.setText(tr("On: locked."))  # i18n: Settings → Parental controls
        else:
            status.setText(tr("On: unlocked for now."))  # i18n: Settings → Parental controls
        # i18n: Settings → Parental controls: the button (no PIN yet / one already set)
        set_pin.setText(tr("Change PIN…") if has_pin else tr("Set PIN…"))
        remove_pin.setEnabled(has_pin)
        locked = has_pin and guard.locked()
        unlock.setEnabled(locked)
        lock.setEnabled(has_pin and not locked)
        adult.blockSignals(True)
        adult.setChecked(guard.adult_rule())
        adult.blockSignals(False)
        adult.setEnabled(has_pin and not (locked and guard.adult_rule()))  # off: with the PIN
        hidden.clear()
        if locked:
            # i18n: Settings → Parental controls, while locked: the list of what is hidden
            # shows only once unlocked with the PIN
            hidden_label.setText(tr("Unlock to see or change what is hidden."))
            show_again.setEnabled(False)
            return
        # i18n: Settings → Parental controls: the list of what is hidden
        hidden_label.setText(tr("Hidden while locked:"))
        for entry in guard.hidden():
            text = entry.get("title") or entry.get("key", "")
            if entry.get("provider"):
                text = f"{text} ({entry['provider']})"
            row_item = QListWidgetItem(text)
            row_item.setData(Qt.ItemDataRole.UserRole, entry.get("key"))
            hidden.addItem(row_item)
        show_again.setEnabled(has_pin and hidden.count() > 0)

    def new_pin() -> None:
        if guard.has_pin():
            old = _ask_pin(tab, tr("Your current PIN:"))  # i18n: asking for the PIN
            if old is None:
                return
            if not guard.check(old):
                _wrong(tab)
                return
        pin = _ask_pin(tab, tr("A new PIN (4 digits or more):"))  # i18n: asking for a PIN
        if pin is None:
            return
        if len(pin) < MIN_DIGITS or not pin.isdigit():
            # i18n: a message: the PIN typed is too short or has letters
            QMessageBox.warning(tab, tr("Parental controls"), tr("A PIN is 4 digits or more."))
            return
        if _ask_pin(tab, tr("The same PIN again:")) != pin:  # i18n: confirming the PIN
            # i18n: a message: the PIN was typed differently the second time
            QMessageBox.warning(tab, tr("Parental controls"), tr("The PINs were not the same."))
            return
        guard.set_pin(pin)
        changed()

    def drop_pin() -> None:
        pin = _ask_pin(tab, tr("Your current PIN:"))
        if pin is not None and not guard.clear_pin(pin):
            _wrong(tab)
        changed()

    def open_up() -> None:
        pin = _ask_pin(tab, tr("Your PIN:"))  # i18n: asking for the PIN to unlock
        if pin is not None and not guard.unlock(pin):
            _wrong(tab)
        changed()

    def close_up() -> None:
        guard.lock()
        changed()

    def reveal() -> None:
        current = hidden.currentItem()
        if current is not None and guard.show_again(str(current.data(Qt.ItemDataRole.UserRole))):
            changed()

    def adult_rule(on: bool) -> None:
        guard.set_adult_rule(on)  # refused while locked when it would show them
        changed()

    set_pin.clicked.connect(new_pin)
    remove_pin.clicked.connect(drop_pin)
    unlock.clicked.connect(open_up)
    lock.clicked.connect(close_up)
    show_again.clicked.connect(reveal)
    adult.toggled.connect(adult_rule)
    dialog.parental_refresh = refresh
    refresh()
    return tab


def _wrong(parent: QWidget) -> None:
    # i18n: a message: the PIN typed is not the parental controls' PIN
    QMessageBox.warning(parent, tr("Parental controls"), tr("That PIN is not right."))
