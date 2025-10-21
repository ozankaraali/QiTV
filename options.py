import logging
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
import orjson as json
import requests

from config_manager import MultiKeyDict
from update_checker import check_for_updates


class AddXmltvMappingDialog(QDialog):
    def __init__(self, parent=None, channel_name="", logo_url="", channel_ids=""):
        super().__init__(parent)
        self.setWindowTitle("Add/Edit XMLTV Mapping")

        self.layout = QFormLayout(self)

        self.channel_name_input = QLineEdit(self)
        self.channel_name_input.setText(channel_name)
        self.layout.addRow("Channel Name:", self.channel_name_input)

        self.logo_url_input = QLineEdit(self)
        self.logo_url_input.setText(logo_url)
        self.layout.addRow("Logo URL:", self.logo_url_input)

        self.channel_ids_input = QLineEdit(self)
        self.channel_ids_input.setText(channel_ids)
        self.layout.addRow("Channel IDs (comma-separated):", self.channel_ids_input)

        self.button_box = QHBoxLayout()
        self.ok_button = QPushButton("OK", self)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        self.button_box.addWidget(self.ok_button)
        self.button_box.addWidget(self.cancel_button)

        self.layout.addRow(self.button_box)

    def get_data(self):
        return (
            self.channel_name_input.text(),
            self.logo_url_input.text(),
            self.channel_ids_input.text(),
        )


class OptionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Settings")

        self.config_manager = parent.config_manager
        self.provider_manager = parent.provider_manager
        self.epg_manager = parent.epg_manager
        self.providers = self.provider_manager.providers
        self.selected_provider_name = self.config_manager.selected_provider_name
        self.selected_provider_index = 0
        self.epg_settings_modified = False
        self.xmltv_mapping_modified = False
        self.providers_modified = False
        self.current_provider_changed = False

        for i in range(len(self.providers)):
            if self.providers[i]["name"] == self.config_manager.selected_provider_name:
                self.selected_provider_index = i
                break

        self.main_layout = QVBoxLayout(self)

        self.create_options_ui()

        self.save_button = QPushButton("Save", self)
        self.save_button.clicked.connect(self.save_settings)

        self.main_layout.addWidget(self.options_tab)
        self.main_layout.addWidget(self.save_button)

        self.load_providers()

    def create_options_ui(self):
        self.options_tab = QTabWidget(self)

        # Add tab with settings
        self.create_settings_ui()

        # Add tab with providers
        self.create_providers_ui()

        # Add tab with EPG settings
        self.create_epg_ui()

    def create_settings_ui(self):
        self.settings_tab = QWidget(self)
        self.options_tab.addTab(self.settings_tab, "Settings")
        self.settings_layout = QFormLayout(self.settings_tab)

        # Add check button to allow checking for updates
        self.check_updates_checkbox = QCheckBox("Allow Check for Updates", self.settings_tab)
        self.check_updates_checkbox.setChecked(self.config_manager.check_updates)
        self.check_updates_checkbox.stateChanged.connect(self.on_check_updates_toggled)
        self.settings_layout.addRow(self.check_updates_checkbox)

        # Add check button to enable channel logos
        self.channel_logos_checkbox = QCheckBox("Enable Channel Logos", self.settings_tab)
        self.channel_logos_checkbox.setChecked(self.config_manager.channel_logos)
        self.settings_layout.addRow(self.channel_logos_checkbox)

        # Add cache options
        self.cache_options_layout = QVBoxLayout()
        self.cache_image_size_label = QLabel(
            f"Max size of image cache (actual size: {self.get_cache_image_size():.2f} MB)",
            self.settings_tab,
        )
        self.cache_image_size_input = QLineEdit(self.settings_tab)
        self.cache_image_size_input.setText(str(self.config_manager.max_cache_image_size))
        self.settings_layout.addRow(self.cache_image_size_label, self.cache_image_size_input)

        self.clear_image_cache_button = QPushButton("Clear Image Cache", self.settings_tab)
        self.clear_image_cache_button.clicked.connect(self.clear_image_cache)
        self.settings_layout.addRow(self.clear_image_cache_button)

        # Network security options
        self.prefer_https_checkbox = QCheckBox(
            "Prefer HTTPS when available (Xtream/STB/M3U)", self.settings_tab
        )
        self.prefer_https_checkbox.setToolTip(
            "When enabled, the app will try HTTPS endpoints first when providers support them."
        )
        self.prefer_https_checkbox.setChecked(self.config_manager.prefer_https)
        self.settings_layout.addRow(self.prefer_https_checkbox)

        self.ssl_verify_checkbox = QCheckBox(
            "Verify SSL certificates (recommended)", self.settings_tab
        )
        self.ssl_verify_checkbox.setToolTip(
            "Disable only if your provider uses self-signed certificates."
        )
        self.ssl_verify_checkbox.setChecked(self.config_manager.ssl_verify)
        self.settings_layout.addRow(self.ssl_verify_checkbox)

        # Keyboard/Remote mode: use Up/Down to surf channels while playing
        self.keyboard_remote_checkbox = QCheckBox(
            "Keyboard/Remote Mode (Up/Down channel surf)", self.settings_tab
        )
        self.keyboard_remote_checkbox.setToolTip(
            "When enabled, the video player will use Up/Down keys to switch to the previous/next playable item."
        )
        self.keyboard_remote_checkbox.setChecked(self.config_manager.keyboard_remote_mode)
        self.settings_layout.addRow(self.keyboard_remote_checkbox)

    def create_providers_ui(self):
        self.providers_tab = QWidget(self)
        self.options_tab.addTab(self.providers_tab, "Providers")
        self.providers_layout = QFormLayout(self.providers_tab)

        self.provider_label = QLabel("Select Provider:", self.providers_tab)
        self.provider_combo = QComboBox(self.providers_tab)
        self.provider_combo.currentIndexChanged.connect(self.load_provider_settings)
        self.providers_layout.addRow(self.provider_label, self.provider_combo)

        self.add_provider_button = QPushButton("Add Provider", self.providers_tab)
        self.add_provider_button.clicked.connect(self.add_new_provider)
        self.providers_layout.addWidget(self.add_provider_button)

        self.remove_provider_button = QPushButton("Remove Provider", self.providers_tab)
        self.remove_provider_button.clicked.connect(self.remove_provider)
        self.providers_layout.addWidget(self.remove_provider_button)

        self.name_label = QLabel("Name:", self.providers_tab)
        self.name_input = QLineEdit(self.providers_tab)
        self.providers_layout.addRow(self.name_label, self.name_input)

        self.create_stream_type_ui()

        self.url_label = QLabel("Server URL:", self.providers_tab)
        self.url_input = QLineEdit(self.providers_tab)
        self.providers_layout.addRow(self.url_label, self.url_input)

        self.mac_label = QLabel("MAC Address:", self.providers_tab)
        self.mac_input = QLineEdit(self.providers_tab)
        self.providers_layout.addRow(self.mac_label, self.mac_input)

        self.serial_label = QLabel("Serial Number (Optional):", self.providers_tab)
        self.serial_input = QLineEdit(self.providers_tab)
        self.serial_input.setPlaceholderText("Leave empty if not required")
        self.providers_layout.addRow(self.serial_label, self.serial_input)

        self.device_id_label = QLabel("Device ID (Optional):", self.providers_tab)
        self.device_id_input = QLineEdit(self.providers_tab)
        self.device_id_input.setPlaceholderText("Leave empty if not required")
        self.providers_layout.addRow(self.device_id_label, self.device_id_input)

        self.file_button = QPushButton("Load File", self.providers_tab)
        self.file_button.clicked.connect(self.load_file)
        self.providers_layout.addWidget(self.file_button)

        self.username_label = QLabel("Username:", self.providers_tab)
        self.username_input = QLineEdit(self.providers_tab)
        self.providers_layout.addRow(self.username_label, self.username_input)

        self.password_label = QLabel("Password:", self.providers_tab)
        self.password_input = QLineEdit(self.providers_tab)
        self.providers_layout.addRow(self.password_label, self.password_input)

        # Per-provider network preferences
        self.provider_prefer_https_checkbox = QCheckBox(
            "Prefer HTTPS for this provider", self.providers_tab
        )
        self.provider_ssl_verify_checkbox = QCheckBox(
            "Verify SSL certificates for this provider", self.providers_tab
        )
        self.providers_layout.addRow(self.provider_prefer_https_checkbox)
        self.providers_layout.addRow(self.provider_ssl_verify_checkbox)

        self.verify_apply_group = QWidget(self.providers_tab)
        self.verify_button = QPushButton("Verify Provider", self.verify_apply_group)
        self.verify_button.clicked.connect(self.verify_provider)
        self.apply_button = QPushButton("Apply Change", self.verify_apply_group)
        self.apply_button.clicked.connect(self.apply_provider)
        verify_apply_layout = QHBoxLayout(self.verify_apply_group)
        verify_apply_layout.addWidget(self.verify_button)
        verify_apply_layout.addWidget(self.apply_button)
        self.verify_result = QLabel("", self.providers_tab)
        self.providers_layout.addWidget(self.verify_apply_group)
        self.providers_layout.addWidget(self.verify_result)

    def create_stream_type_ui(self):
        self.type_label = QLabel("Stream Type:", self)
        self.type_group = QButtonGroup(self)
        self.type_STB = QRadioButton("STB", self)
        self.type_M3UPLAYLIST = QRadioButton("M3U Playlist", self)
        self.type_M3USTREAM = QRadioButton("M3U Stream", self)
        self.type_XTREAM = QRadioButton("Xtream", self)
        self.type_group.addButton(self.type_STB)
        self.type_group.addButton(self.type_M3UPLAYLIST)
        self.type_group.addButton(self.type_M3USTREAM)
        self.type_group.addButton(self.type_XTREAM)

        self.type_STB.toggled.connect(self.update_inputs)
        self.type_M3UPLAYLIST.toggled.connect(self.update_inputs)
        self.type_M3USTREAM.toggled.connect(self.update_inputs)
        self.type_XTREAM.toggled.connect(self.update_inputs)

        grid_layout = QGridLayout()
        grid_layout.addWidget(self.type_STB, 0, 0)
        grid_layout.addWidget(self.type_M3UPLAYLIST, 0, 1)
        grid_layout.addWidget(self.type_M3USTREAM, 1, 0)
        grid_layout.addWidget(self.type_XTREAM, 1, 1)
        self.providers_layout.addRow(self.type_label, grid_layout)

    def create_epg_ui(self):
        self.epg_tab = QWidget(self)
        self.options_tab.addTab(self.epg_tab, "EPG")
        self.epg_layout = QFormLayout(self.epg_tab)

        # Add EPG settings
        self.epg_source_label = QLabel("EPG Source")
        self.epg_source_combo = QComboBox()
        self.epg_source_combo.addItems(["No Source", "STB", "Local File", "URL"])
        self.epg_source_combo.setCurrentText(self.config_manager.epg_source)
        self.epg_source_combo.currentIndexChanged.connect(self.on_epg_source_changed)
        self.epg_layout.addRow(self.epg_source_label)
        self.epg_layout.addRow(self.epg_source_combo)

        self.epg_url_label = QLabel("EPG URL")
        self.epg_url_input = QLineEdit()
        self.epg_url_input.setText(self.config_manager.epg_url)
        self.epg_layout.addRow(self.epg_url_label)
        self.epg_layout.addRow(self.epg_url_input)

        self.epg_file_label = QLabel("EPG File")
        self.epg_file_input = QLineEdit()
        self.epg_file_input.setText(self.config_manager.epg_file)
        self.epg_file_button = QPushButton("Browse")
        self.epg_file_button.clicked.connect(self.browse_epg_file)
        self.epg_layout.addRow(self.epg_file_label)
        self.epg_layout.addRow(self.epg_file_input)
        self.epg_layout.addRow(self.epg_file_button)

        # Add expiring EPG settings
        self.epg_expiration_layout = QHBoxLayout()
        self.epg_expiration_label = QLabel("Check update every")
        self.epg_expiration_spinner = QSpinBox()
        self.epg_expiration_spinner.setValue(self.config_manager.epg_expiration_value)
        self.epg_expiration_spinner.setMinimum(1)
        self.epg_expiration_spinner.setMaximum(9999)
        self.epg_expiration_spinner.setSingleStep(1)
        self.epg_expiration_combo = QComboBox()
        self.epg_expiration_combo.addItems(["Minutes", "Hours", "Days", "Weeks", "Monthes"])
        self.epg_expiration_combo.setCurrentText(self.config_manager.epg_expiration_unit)
        self.epg_expiration_layout.addWidget(self.epg_expiration_label)
        self.epg_expiration_layout.addWidget(self.epg_expiration_spinner)
        self.epg_expiration_layout.addWidget(self.epg_expiration_combo)
        self.epg_layout.addRow(self.epg_expiration_layout)

        # Create a vertical layout for the settings, XMLTV mapping table, and buttons
        self.xmltv_group_widget = QWidget()
        self.xmltv_group_layout = QVBoxLayout(self.xmltv_group_widget)
        self.xmltv_group_label = QLabel("XMLTV Mapping")
        self.xmltv_group_layout.addWidget(self.xmltv_group_label)

        # XMLTV mapping table
        self.xmltv_mapping_table = QTableWidget(self.xmltv_group_widget)
        self.xmltv_mapping_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.xmltv_mapping_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.xmltv_mapping_table.setColumnCount(3)
        self.xmltv_mapping_table.setHorizontalHeaderLabels(
            ["Channel Name", "Logo URL", "Channel IDs"]
        )
        self.xmltv_mapping_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.xmltv_mapping_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.xmltv_mapping_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.xmltv_mapping_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.xmltv_group_layout.addWidget(self.xmltv_mapping_table)

        self.load_xmltv_channel_mapping()

        # Create a horizontal layout for the buttons
        self.xmltv_buttons_layout = QHBoxLayout()
        self.add_xmltv_mapping_button = QPushButton("Add")
        self.add_xmltv_mapping_button.clicked.connect(self.add_xmltv_mapping)
        self.xmltv_buttons_layout.addWidget(self.add_xmltv_mapping_button)

        self.edit_xmltv_mapping_button = QPushButton("Edit")
        self.edit_xmltv_mapping_button.clicked.connect(self.edit_xmltv_mapping)
        self.xmltv_buttons_layout.addWidget(self.edit_xmltv_mapping_button)

        self.delete_xmltv_mapping_button = QPushButton("Delete")
        self.delete_xmltv_mapping_button.clicked.connect(self.delete_xmltv_mapping)
        self.xmltv_buttons_layout.addWidget(self.delete_xmltv_mapping_button)

        self.import_xmltv_mapping_button = QPushButton("Import")
        self.import_xmltv_mapping_button.clicked.connect(self.import_xmltv_mapping)
        self.xmltv_buttons_layout.addWidget(self.import_xmltv_mapping_button)

        self.export_xmltv_mapping_button = QPushButton("Export")
        self.export_xmltv_mapping_button.clicked.connect(self.export_xmltv_mapping)
        self.xmltv_buttons_layout.addWidget(self.export_xmltv_mapping_button)

        # Add the horizontal layout to the vertical layout
        self.xmltv_group_layout.addLayout(self.xmltv_buttons_layout)

        self.epg_layout.addRow(self.xmltv_group_widget)

        # Initial call to set visibility based on the current selection
        self.on_epg_source_changed()

        # EPG list window (hours; 0 = unlimited)
        self.epg_list_window_label = QLabel("EPG List Window (hours; 0 = unlimited)")
        self.epg_list_window_spinner = QSpinBox()
        self.epg_list_window_spinner.setMinimum(0)
        self.epg_list_window_spinner.setMaximum(168)
        try:
            self.epg_list_window_spinner.setValue(int(self.config_manager.epg_list_window_hours))
        except Exception:
            self.epg_list_window_spinner.setValue(24)
        self.epg_layout.addRow(self.epg_list_window_label, self.epg_list_window_spinner)

    def load_providers(self):
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        for i, provider in enumerate(self.providers):
            # can we get the first couple ... last couple of characters of the name?
            prov = (
                provider["name"][:30] + "..." + provider["name"][-15:]
                if len(provider["name"]) > 45
                else provider["name"]
            )
            self.provider_combo.addItem(f"{i + 1}: {prov}", userData=provider)
        self.provider_combo.blockSignals(False)
        self.provider_combo.setCurrentIndex(self.selected_provider_index)
        self.load_provider_settings(self.selected_provider_index)

    def load_provider_settings(self, index):
        if index == -1 or index >= len(self.providers):
            return
        self.selected_provider_name = self.providers[index].get(
            "name", self.providers[index].get("url", "")
        )
        self.selected_provider_index = index
        self.edited_provider = self.providers[index]
        self.name_input.setText(self.edited_provider.get("name", ""))
        self.url_input.setText(self.edited_provider.get("url", ""))
        self.mac_input.setText(self.edited_provider.get("mac", ""))
        self.serial_input.setText(self.edited_provider.get("serial_number", ""))
        self.device_id_input.setText(self.edited_provider.get("device_id", ""))
        self.username_input.setText(self.edited_provider.get("username", ""))
        self.password_input.setText(self.edited_provider.get("password", ""))
        # Set per-provider network preferences with global fallbacks
        self.provider_prefer_https_checkbox.setChecked(
            self.edited_provider.get("prefer_https", self.config_manager.prefer_https)
        )
        self.provider_ssl_verify_checkbox.setChecked(
            self.edited_provider.get("ssl_verify", self.config_manager.ssl_verify)
        )
        self.update_radio_buttons()
        self.update_inputs()

    def on_epg_source_changed(self):
        epg_source = self.epg_source_combo.currentText()

        self.epg_url_label.hide()
        self.epg_url_input.hide()
        self.epg_expiration_label.hide()
        self.epg_expiration_spinner.hide()
        self.epg_expiration_combo.hide()
        self.epg_file_label.hide()
        self.epg_file_input.hide()
        self.epg_file_button.hide()
        self.xmltv_group_widget.hide()

        if epg_source == "URL":
            self.epg_url_label.show()
            self.epg_url_input.show()

        if epg_source == "Local File":
            self.epg_file_label.show()
            self.epg_file_input.show()
            self.epg_file_button.show()
        elif epg_source != "No Source":
            self.epg_expiration_label.show()
            self.epg_expiration_spinner.show()
            self.epg_expiration_combo.show()

        if epg_source not in ["STB", "No Source"]:
            self.xmltv_group_widget.show()

    def update_radio_buttons(self):
        provider_type = self.edited_provider.get("type", "")
        self.type_STB.setChecked(provider_type == "STB")
        self.type_M3UPLAYLIST.setChecked(provider_type == "M3UPLAYLIST")
        self.type_M3USTREAM.setChecked(provider_type == "M3USTREAM")
        self.type_XTREAM.setChecked(provider_type == "XTREAM")

    def update_inputs(self):
        self.mac_label.setVisible(self.type_STB.isChecked())
        self.mac_input.setVisible(self.type_STB.isChecked())
        self.serial_label.setVisible(self.type_STB.isChecked())
        self.serial_input.setVisible(self.type_STB.isChecked())
        self.device_id_label.setVisible(self.type_STB.isChecked())
        self.device_id_input.setVisible(self.type_STB.isChecked())
        self.file_button.setVisible(
            self.type_M3UPLAYLIST.isChecked() or self.type_M3USTREAM.isChecked()
        )

        self.url_input.setEnabled(True)

        self.username_label.setVisible(self.type_XTREAM.isChecked())
        self.username_input.setVisible(self.type_XTREAM.isChecked())
        self.password_label.setVisible(self.type_XTREAM.isChecked())
        self.password_input.setVisible(self.type_XTREAM.isChecked())

    def add_new_provider(self):
        new_provider = {
            "type": "STB",
            "name": "",
            "url": "",
            "mac": "",
            "serial_number": "",
            "device_id": "",
        }
        self.providers.append(new_provider)
        self.load_providers()
        self.provider_combo.setCurrentIndex(len(self.providers) - 1)
        self.providers_modified = True

    def remove_provider(self):
        if len(self.providers) == 1:
            return
        del self.providers[self.provider_combo.currentIndex()]
        self.load_providers()
        self.provider_combo.setCurrentIndex(
            min(self.selected_provider_index, len(self.providers) - 1)
        )
        self.providers_modified = True

    def browse_epg_file(self):
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName()
        if file_path:
            self.epg_file_input.setText(file_path)

    def save_settings(self):
        self.config_manager.check_updates = self.check_updates_checkbox.isChecked()
        self.config_manager.max_cache_image_size = int(self.cache_image_size_input.text())
        self.config_manager.prefer_https = self.prefer_https_checkbox.isChecked()
        self.config_manager.ssl_verify = self.ssl_verify_checkbox.isChecked()
        self.config_manager.keyboard_remote_mode = self.keyboard_remote_checkbox.isChecked()

        need_to_refresh_content_list_size = False
        current_provider_changed = False

        if self.config_manager.channel_logos != self.channel_logos_checkbox.isChecked():
            self.config_manager.channel_logos = self.channel_logos_checkbox.isChecked()
            need_to_refresh_content_list_size = True

        if self.epg_source_combo.currentText() != self.config_manager.epg_source:
            self.config_manager.epg_source = self.epg_source_combo.currentText()
            self.epg_settings_modified = True
        if self.config_manager.epg_url != self.epg_url_input.text():
            self.config_manager.epg_url = self.epg_url_input.text()
            self.epg_settings_modified = True
        if self.config_manager.epg_file != self.epg_file_input.text():
            self.config_manager.epg_file = self.epg_file_input.text()
            self.epg_settings_modified = True
        if self.config_manager.epg_expiration_value != self.epg_expiration_spinner.value():
            self.config_manager.epg_expiration_value = self.epg_expiration_spinner.value()
        if self.config_manager.epg_expiration_unit != self.epg_expiration_combo.currentText():
            self.config_manager.epg_expiration_unit = self.epg_expiration_combo.currentText()

        if self.config_manager.selected_provider_name != self.selected_provider_name:
            self.config_manager.selected_provider_name = self.selected_provider_name
            current_provider_changed = True

        # Save EPG list window hours
        try:
            self.config_manager.epg_list_window_hours = int(self.epg_list_window_spinner.value())
        except Exception:
            pass

        # Save the configuration
        self.parent().save_config()

        if self.providers_modified:
            self.provider_manager.save_providers()

        if current_provider_changed:
            self.parent().set_provider()
        elif self.epg_settings_modified:
            self.epg_manager.set_current_epg()
            self.parent().refresh_channels()
        elif self.xmltv_mapping_modified:
            if self.config_manager.epg_source != "STB":
                self.epg_manager.reindex_programs()

        if need_to_refresh_content_list_size:
            self.parent().refresh_content_list_size()

        self.accept()

    def get_cache_size(self):
        cache_dir = self.parent().get_cache_directory()
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(cache_dir):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                total_size += os.path.getsize(fp)
        return total_size / (1024 * 1024)  # Convert to MB

    def load_file(self):
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName()
        if file_path:
            self.url_input.setText(file_path)
            self.file_button.setVisible(False)

    def verify_provider(self):
        self.verify_result.setText("Verifying...")
        self.verify_result.repaint()
        result = False
        url = self.url_input.text()

        if self.type_STB.isChecked():
            result = self.provider_manager.do_handshake(
                url,
                self.mac_input.text(),
                serial_number=self.serial_input.text(),
                device_id=self.device_id_input.text(),
                prefer_https_override=self.provider_prefer_https_checkbox.isChecked(),
                ssl_verify_override=self.provider_ssl_verify_checkbox.isChecked(),
            )
        elif self.type_M3UPLAYLIST.isChecked() or self.type_M3USTREAM.isChecked():
            if url.startswith(("http://", "https://")):
                result = self.verify_url(
                    url,
                    prefer_https=self.provider_prefer_https_checkbox.isChecked(),
                    verify_ssl=self.provider_ssl_verify_checkbox.isChecked(),
                )
            else:
                result = os.path.isfile(url)
        elif self.type_XTREAM.isChecked():
            result = self.verify_url(
                url,
                prefer_https=self.provider_prefer_https_checkbox.isChecked(),
                verify_ssl=self.provider_ssl_verify_checkbox.isChecked(),
            )

        self.verify_result.setText(
            "Provider verified successfully." if result else "Failed to verify provider."
        )
        self.verify_result.setStyleSheet("color: green;" if result else "color: red;")

    def apply_provider(self):
        if self.edited_provider:
            self.edited_provider["name"] = self.name_input.text()
            self.edited_provider["url"] = self.url_input.text()
            if not self.edited_provider["name"]:
                self.edited_provider["name"] = self.edited_provider["url"]
            if self.type_STB.isChecked():
                self.edited_provider["type"] = "STB"
                self.edited_provider["mac"] = self.mac_input.text()
                self.edited_provider["serial_number"] = self.serial_input.text()
                self.edited_provider["device_id"] = self.device_id_input.text()
            elif self.type_M3UPLAYLIST.isChecked():
                self.edited_provider["type"] = "M3UPLAYLIST"
            elif self.type_M3USTREAM.isChecked():
                self.edited_provider["type"] = "M3USTREAM"
            elif self.type_XTREAM.isChecked():
                self.edited_provider["type"] = "XTREAM"
                self.edited_provider["username"] = self.username_input.text()
                self.edited_provider["password"] = self.password_input.text()
            # Save per-provider network preferences
            self.edited_provider["prefer_https"] = self.provider_prefer_https_checkbox.isChecked()
            self.edited_provider["ssl_verify"] = self.provider_ssl_verify_checkbox.isChecked()
            self.selected_provider_name = self.edited_provider["name"]
            self.provider_combo.setItemText(
                self.selected_provider_index,
                f"{self.selected_provider_index + 1}: {self.edited_provider['name']}",
            )
            self.providers_modified = True

    def clear_image_cache(self):
        self.parent().image_manager.clear_cache()
        self.cache_image_size_label = QLabel(
            f"Max size of image cache (actual size: {self.get_cache_image_size():.2f} MB)",
            self.settings_tab,
        )

    def get_cache_image_size(self):
        total_size = self.parent().image_manager.current_cache_size
        return total_size / (1024 * 1024)  # Convert to MB

    def on_check_updates_toggled(self):
        if self.check_updates_checkbox.isChecked():
            check_for_updates()

    def load_xmltv_channel_mapping(self):
        self.xmltv_mapping_table.setRowCount(len(self.config_manager.xmltv_channel_map))
        for row_position, (key, value) in enumerate(self.config_manager.xmltv_channel_map.items()):
            self.xmltv_mapping_table.setItem(row_position, 0, QTableWidgetItem(value["name"]))
            self.xmltv_mapping_table.setItem(
                row_position, 1, QTableWidgetItem(value.get("icon", ""))
            )
            self.xmltv_mapping_table.setItem(row_position, 2, QTableWidgetItem(", ".join(key)))

    def add_xmltv_mapping(self):
        dialog = AddXmltvMappingDialog(self)
        if dialog.exec() == QDialog.Accepted:
            channel_name, logo_url, channel_ids = dialog.get_data()

            if not channel_name or not channel_ids:
                # Show an error message if the input is invalid
                error_dialog = QDialog(self)
                error_dialog.setWindowTitle("Error")
                error_layout = QVBoxLayout(error_dialog)
                error_label = QLabel("Channel Name and Channel IDs are required.", error_dialog)
                error_layout.addWidget(error_label)
                error_button = QPushButton("OK", error_dialog)
                error_button.clicked.connect(error_dialog.accept)
                error_layout.addWidget(error_button)
                error_dialog.exec()
                return

            # Split the Channel IDs by comma and strip any extra whitespace
            channel_ids_list = [id.strip() for id in channel_ids.split(",")]

            # Add the new mapping to the config manager
            self.config_manager.xmltv_channel_map[tuple(channel_ids_list)] = {
                "name": channel_name,
                "icon": logo_url,
            }

            # Refresh the XMLTV mapping table
            self.xmltv_mapping_modified = True
            self.load_xmltv_channel_mapping()

    def edit_xmltv_mapping(self):
        # Assuming you have a way to get the selected mapping's current values
        selected_items = self.xmltv_mapping_table.selectedItems()
        if not selected_items:
            return

        row = selected_items[0].row()
        current_channel_name = self.xmltv_mapping_table.item(row, 0).text()
        current_logo_url = self.xmltv_mapping_table.item(row, 1).text()
        current_channel_ids = self.xmltv_mapping_table.item(row, 2).text()

        dialog = AddXmltvMappingDialog(
            self,
            channel_name=current_channel_name,
            logo_url=current_logo_url,
            channel_ids=current_channel_ids,
        )
        if dialog.exec() == QDialog.Accepted:
            channel_name, logo_url, channel_ids = dialog.get_data()

            if not channel_name or not channel_ids:
                # Show an error message if the input is invalid
                error_dialog = QDialog(self)
                error_dialog.setWindowTitle("Error")
                error_layout = QVBoxLayout(error_dialog)
                error_label = QLabel("Channel Name and Channel IDs are required.", error_dialog)
                error_layout.addWidget(error_label)
                error_button = QPushButton("OK", error_dialog)
                error_button.clicked.connect(error_dialog.accept)
                error_layout.addWidget(error_button)
                error_dialog.exec()
                return

            # Split the Channel IDs by comma and strip any extra whitespace
            channel_ids_list = [id.strip() for id in channel_ids.split(",")]

            # Update the existing mapping in the config manager
            key_tuple = tuple(current_channel_ids.split(","))
            del self.config_manager.xmltv_channel_map[key_tuple[0].strip()]
            self.config_manager.xmltv_channel_map[tuple(channel_ids_list)] = {
                "name": channel_name,
                "icon": logo_url,
            }

            # Refresh the XMLTV mapping table
            self.xmltv_mapping_modified = True
            self.load_xmltv_channel_mapping()

    def delete_xmltv_mapping(self):
        selected_items = self.xmltv_mapping_table.selectedItems()
        if not selected_items:
            return

        self.xmltv_mapping_modified = True
        rows = {item.row() for item in selected_items}
        keys = [self.xmltv_mapping_table.item(row, 2).text() for row in rows]
        for key in keys:
            self.config_manager.xmltv_channel_map.pop(tuple(key.split(","))[0].strip(), None)

        self.load_xmltv_channel_mapping()

    def import_xmltv_mapping(self):
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName()
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    list_channels = json.loads(f.read())
                if list_channels is not None:
                    multiKey = MultiKeyDict()
                    for k, v in list_channels.items():
                        xmltv_ids = v.get("xmltv_id", [])
                        if xmltv_ids:
                            v.pop("xmltv_id")
                            multiKey[tuple(xmltv_ids)] = v
                    self.config_manager.xmltv_channel_map = multiKey
                    self.load_xmltv_channel_mapping()
                    self.xmltv_mapping_modified = True
            except (FileNotFoundError, json.JSONDecodeError):
                pass

    def export_xmltv_mapping(self):
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getSaveFileName()
        if file_path:
            with open(
                file_path if file_path.endswith(".json") else file_path + ".json",
                "w",
                encoding="utf-8",
            ) as f:
                export = {}
                for k, v in self.config_manager.xmltv_channel_map.items():
                    mainKey = k[0].strip()
                    export[mainKey] = v
                    export[mainKey]["xmltv_id"] = list(k)
                f.write(json.dumps(export, option=json.OPT_INDENT_2).decode("utf-8"))

    @staticmethod
    def verify_url(url, *, prefer_https=False, verify_ssl=True):
        if url.startswith(("http://", "https://")):
            try:
                test_urls = []
                if prefer_https and url.startswith("http://"):
                    test_urls.append("https://" + url[len("http://") :])
                test_urls.append(url)
                for turl in test_urls:
                    try:
                        response = requests.head(turl, timeout=5, verify=verify_ssl)
                        if response.status_code == 200:
                            return True
                    except requests.RequestException:
                        continue
                return False
            except Exception as e:
                logging.getLogger(__name__).warning(f"Error verifying URL: {e}")
                return False
        else:
            return os.path.isfile(url)
