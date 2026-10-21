"""Settings → Phone remote (2.5): turn the remote on, the relay, the paired phones.

Built into the Settings window (ui/settings_dialog.py) as its own tab. The paired phones are
the remote's (services/remote/pairing.py); this tab only lists them and asks the remote to
remove one. Save writes the switches and the relay address into the config's `remote` object,
keeping everything else in it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from services.i18n import N_, tr, translate
from services.remote.protocol import RELAY_URL, STORE_VERSION

# i18n: Settings → Phone remote: what the tab is for
REMOTE_INTRO = N_(
    "Use your phone as a remote: scan the code QiTV shows on the TV once, and the phone's "
    "browser becomes a remote with a guide and search."
)
# i18n: Settings → Phone remote: what the relay is; it can't read what passes through it
RELAY_HINT = N_(
    "Lets a paired phone reach QiTV from any network, through the QiTV relay. The relay "
    "passes along scrambled messages it can't read."
)
# i18n: Settings → Phone remote: no phone paired yet
NO_PHONES = N_("No phone is paired yet")
# i18n: Settings → Phone remote: a paired phone's line; {name} the phone, {when} a date
PHONE_LINE = N_("{name} · last used {when}")
# i18n: Settings → Phone remote: the settings were written by a newer QiTV
NEWER_SETTINGS = N_("These phone remote settings come from a newer QiTV; it keeps them.")
# The relay's state, as Settings → Phone remote says it.
RELAY_STATES = {
    "off": N_("Off"),  # i18n: the relay's state in Settings → Phone remote
    "connecting": N_("Connecting…"),  # i18n: the relay's state in Settings → Phone remote
    "online": N_("Connected"),  # i18n: the relay's state in Settings → Phone remote
    # i18n: the relay's state in Settings → Phone remote
    "resting": N_("Resting until 00:00 UTC (its daily limit)"),
    # i18n: the relay's state: a copy of this QiTV's settings runs elsewhere
    "replaced": N_("Another QiTV is using this relay room"),
    # i18n: the phone remote's state: it couldn't start (no free port)
    "error": N_("The phone remote couldn't start"),
}


def _known(data: dict) -> bool:
    return data.get("version") in (None, STORE_VERSION)


class _Link(QObject):
    """The tab's ends of the remote's signals: they go when the tab does."""

    def __init__(self, tab: QWidget, fill: Any, show_status: Any) -> None:
        super().__init__(tab)
        self._fill, self._show_status = fill, show_status

    @Slot(list)
    def devices(self, devices: list) -> None:
        self._fill(devices)

    @Slot(dict)
    def status(self, status: dict) -> None:
        self._show_status(status)


def build_remote_tab(dialog: Any) -> QWidget:
    """The tab, added to `dialog.options_tab`; its widgets become the dialog's attributes."""
    config = dialog.config_manager
    data = config.remote if isinstance(config.remote, dict) else {}
    tab = QWidget(dialog)
    tab.setObjectName("remoteSettings")
    dialog.options_tab.addTab(tab, tr("Phone remote"))  # i18n: a tab of the Settings window
    layout = QVBoxLayout(tab)
    intro = QLabel(translate(REMOTE_INTRO), tab)
    intro.setWordWrap(True)
    layout.addWidget(intro)
    form = QFormLayout()
    layout.addLayout(form)
    # i18n: Settings → Phone remote: the switch that turns the remote on
    dialog.remote_enabled_checkbox = QCheckBox(tr("Phone remote"), tab)
    dialog.remote_enabled_checkbox.setChecked(data.get("enabled") is True)
    form.addRow(dialog.remote_enabled_checkbox)
    # i18n: Settings → Phone remote: the relay's switch
    dialog.remote_relay_checkbox = QCheckBox(tr("Reach QiTV from any network"), tab)
    dialog.remote_relay_checkbox.setChecked(data.get("relay", True) is not False)
    form.addRow(dialog.remote_relay_checkbox)
    relay_hint = QLabel(translate(RELAY_HINT), tab)
    relay_hint.setWordWrap(True)
    form.addRow(relay_hint)
    dialog.remote_relay_url_input = QLineEdit(tab)
    dialog.remote_relay_url_input.setPlaceholderText(RELAY_URL)
    url = data.get("relay_url")
    if isinstance(url, str) and url != RELAY_URL:
        dialog.remote_relay_url_input.setText(url)
    form.addRow(tr("Relay address"), dialog.remote_relay_url_input)  # i18n: a Settings label
    remote = dialog.parent().remote
    dialog.remote_home_label = QLabel(remote.status.get("home_url") or "—", tab)
    dialog.remote_home_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    # i18n: Settings → Phone remote: the address of QiTV on the home network
    form.addRow(tr("Home address"), dialog.remote_home_label)
    dialog.remote_relay_label = QLabel(tab)
    form.addRow(tr("Relay"), dialog.remote_relay_label)  # i18n: Settings → Phone remote: a label
    layout.addWidget(QLabel(tr("Paired phones"), tab))  # i18n: Settings → Phone remote: a heading
    dialog.remote_phones_list = QListWidget(tab)
    layout.addWidget(dialog.remote_phones_list)
    buttons = QHBoxLayout()
    layout.addLayout(buttons)
    # i18n: Settings → Phone remote: a button: unpair the selected phone
    dialog.remote_remove_button = QPushButton(tr("Remove"), tab)
    dialog.remote_remove_button.setEnabled(False)
    buttons.addWidget(dialog.remote_remove_button)
    buttons.addStretch(1)
    # i18n: Settings → Phone remote: a button: save, close Settings, show the pairing QR
    dialog.remote_show_code_button = QPushButton(tr("Show the pairing code on the TV"), tab)
    buttons.addWidget(dialog.remote_show_code_button)
    dialog.remote_tab = tab

    def fill(devices: list) -> None:
        phones = dialog.remote_phones_list
        phones.clear()
        for device in devices:
            if not isinstance(device, dict):
                continue
            when = _date(device.get("last_used"))
            text = translate(PHONE_LINE, name=str(device.get("name", "")), when=when)
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, device.get("id"))
            phones.addItem(item)
        if not phones.count():
            empty = QListWidgetItem(translate(NO_PHONES))
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            phones.addItem(empty)
        dialog.remote_remove_button.setEnabled(False)

    def selected() -> str:
        item = dialog.remote_phones_list.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, str) else ""

    def remove() -> None:
        device_id = selected()
        if not device_id:
            return
        remote.remove_device(device_id)
        row = dialog.remote_phones_list.currentRow()
        dialog.remote_phones_list.takeItem(row)
        dialog.remote_remove_button.setEnabled(False)

    def show_code() -> None:
        window = dialog.parent()
        dialog.save_settings()
        QTimer.singleShot(0, window, window.open_phone_remote)

    def show_status(status: dict) -> None:
        dialog.remote_home_label.setText(status.get("home_url") or "—")
        state = status.get("relay") or "off"
        text = RELAY_STATES.get(state, RELAY_STATES["off"])
        dialog.remote_relay_label.setText(translate(text))

    fill(data.get("devices", []))
    show_status(remote.status)
    dialog.remote_phones_list.currentRowChanged.connect(
        lambda _row: dialog.remote_remove_button.setEnabled(bool(selected()))
    )
    dialog.remote_remove_button.clicked.connect(remove)
    dialog.remote_show_code_button.clicked.connect(show_code)
    link = _Link(tab, fill, show_status)
    remote.statusChanged.connect(link.status)
    remote.devicesChanged.connect(link.devices)
    if not _known(data):
        for widget in (
            dialog.remote_enabled_checkbox,
            dialog.remote_relay_checkbox,
            dialog.remote_relay_url_input,
            dialog.remote_phones_list,
            dialog.remote_show_code_button,
        ):
            widget.setEnabled(False)
        note = QLabel(translate(NEWER_SETTINGS), tab)
        note.setWordWrap(True)
        layout.insertWidget(1, note)
    return tab


def save_remote_settings(dialog: Any) -> None:
    """Write the tab's switches and relay address, keeping the paired phones and the identity."""
    config = dialog.config_manager
    data = dict(config.remote) if isinstance(config.remote, dict) else {}
    if not _known(data):
        return  # a newer QiTV's settings: kept as they are
    data["version"] = STORE_VERSION
    data["enabled"] = dialog.remote_enabled_checkbox.isChecked()
    data["relay"] = dialog.remote_relay_checkbox.isChecked()
    data["relay_url"] = dialog.remote_relay_url_input.text().strip().rstrip("/") or RELAY_URL
    config.remote = data


def _date(timestamp: object) -> str:
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return "—"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
