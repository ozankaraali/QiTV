import os
from update_checker import check_for_updates
import requests

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget
)


class OptionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Settings")

        self.config_manager = parent.config_manager
        self.provider_manager = parent.provider_manager
        self.providers = self.provider_manager.providers
        self.selected_provider_name = self.config_manager.selected_provider_name
        self.selected_provider_index = 0
        self.providers_modified = False
        self.current_provider_changed = False

        for i in range(len(self.providers)):
            if self.providers[i]["name"] == self.config_manager.selected_provider_name:
                self.selected_provider_index = i
                break

        self.main_layout = QVBoxLayout(self);

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

    def create_settings_ui(self):
        self.settings_tab = QWidget(self)
        self.options_tab.addTab(self.settings_tab, "Options")
        self.settings_layout = QFormLayout(self.settings_tab)

        # Add check button to allow checking for updates
        self.check_updates_checkbox = QCheckBox("Allow Check for Updates", self.settings_tab)
        self.check_updates_checkbox.setChecked(self.config_manager.check_updates)
        self.check_updates_checkbox.stateChanged.connect(self.on_check_updates_toggled)
        self.settings_layout.addRow(self.check_updates_checkbox)

        # Add check button to show STB content info
        self.show_stb_content_info_checkbox = QCheckBox("Show movie and serie info on STB provider", self.settings_tab)
        self.show_stb_content_info_checkbox.setChecked(self.config_manager.show_stb_content_info)
        self.settings_layout.addRow(self.show_stb_content_info_checkbox)

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

        self.file_button = QPushButton("Load File", self.providers_tab)
        self.file_button.clicked.connect(self.load_file)
        self.providers_layout.addWidget(self.file_button)

        self.username_label = QLabel("Username:", self.providers_tab)
        self.username_input = QLineEdit(self.providers_tab)
        self.providers_layout.addRow(self.username_label, self.username_input)

        self.password_label = QLabel("Password:", self.providers_tab)
        self.password_input = QLineEdit(self.providers_tab)
        self.providers_layout.addRow(self.password_label, self.password_input)

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
        self.selected_provider_name = self.providers[index].get("name", self.providers[index].get("url", ""))
        self.selected_provider_index = index
        self.edited_provider = self.providers[index]
        self.name_input.setText(self.edited_provider.get("name", ""))
        self.url_input.setText(self.edited_provider.get("url", ""))
        self.mac_input.setText(self.edited_provider.get("mac", ""))
        self.username_input.setText(self.edited_provider.get("username", ""))
        self.password_input.setText(self.edited_provider.get("password", ""))
        self.update_radio_buttons()
        self.update_inputs()

    def update_radio_buttons(self):
        provider_type = self.edited_provider.get("type", "")
        self.type_STB.setChecked(provider_type == "STB")
        self.type_M3UPLAYLIST.setChecked(provider_type == "M3UPLAYLIST")
        self.type_M3USTREAM.setChecked(provider_type == "M3USTREAM")
        self.type_XTREAM.setChecked(provider_type == "XTREAM")

    def update_inputs(self):
        self.mac_label.setVisible(self.type_STB.isChecked())
        self.mac_input.setVisible(self.type_STB.isChecked())
        self.file_button.setVisible(
            self.type_M3UPLAYLIST.isChecked() or self.type_M3USTREAM.isChecked()
        )

        self.url_input.setEnabled(True)

        self.username_label.setVisible(self.type_XTREAM.isChecked())
        self.username_input.setVisible(self.type_XTREAM.isChecked())
        self.password_label.setVisible(self.type_XTREAM.isChecked())
        self.password_input.setVisible(self.type_XTREAM.isChecked())

    def add_new_provider(self):
        new_provider = {"type": "STB", "name": "", "url": "", "mac": ""}
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

    def save_settings(self):
        self.config_manager.check_updates = self.check_updates_checkbox.isChecked()
        self.config_manager.show_stb_content_info = self.show_stb_content_info_checkbox.isChecked()

        current_provider_changed = False

        if self.config_manager.selected_provider_name != self.selected_provider_name:
            self.config_manager.selected_provider_name = self.selected_provider_name
            current_provider_changed = True

        # Save the configuration
        self.parent().save_config()

        if self.providers_modified:
            self.provider_manager.save_providers()

        if current_provider_changed:
            self.parent().set_provider()

        self.accept()

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
            result = self.provider_manager.do_handshake(url, self.mac_input.text())
        elif self.type_M3UPLAYLIST.isChecked() or self.type_M3USTREAM.isChecked():
            if url.startswith(("http://", "https://")):
                result = self.parent().verify_url(url)
            else:
                result = os.path.isfile(url)
        elif self.type_XTREAM.isChecked():
            result = self.parent().verify_url(url)

        self.verify_result.setText(
            "Provider verified successfully."
            if result
            else "Failed to verify provider."
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
            elif self.type_M3UPLAYLIST.isChecked():
                self.edited_provider["type"] = "M3UPLAYLIST"
            elif self.type_M3USTREAM.isChecked():
                self.edited_provider["type"] = "M3USTREAM"
            elif self.type_XTREAM.isChecked():
                self.edited_provider["type"] = "XTREAM"
                self.edited_provider["username"] = self.username_input.text()
                self.edited_provider["password"] = self.password_input.text()
            self.selected_provider_name = self.edited_provider["name"]
            self.provider_combo.setItemText(
                self.selected_provider_index,
                f"{self.selected_provider_index + 1}: {self.edited_provider['name']}",
            )
            self.providers_modified = True

    def on_check_updates_toggled(self):
        if self.check_updates_checkbox.isChecked():
            check_for_updates()


    @staticmethod
    def verify_url(url):
        if url.startswith(("http://", "https://")):
            try:
                response = requests.head(url, timeout=5)
                return response.status_code == 200
            except requests.RequestException as e:
                print(f"Error verifying URL: {e}")
                return False
        else:
            return os.path.isfile(url)