from PySide6.QtWidgets import (
    QFileDialog,
    QPushButton,
    QLineEdit,
    QDialog,
    QLabel,
    QFormLayout,
    QRadioButton,
    QButtonGroup,
    QComboBox,
)


class OptionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Options")
        self.layout = QFormLayout(self)
        self.config = parent.config
        self.selected_provider_index = self.config.get("selected", 0)

        self.create_options_ui()
        self.load_providers()

    def create_options_ui(self):
        self.provider_label = QLabel("Select Provider:", self)
        self.provider_combo = QComboBox(self)
        self.provider_combo.currentIndexChanged.connect(self.load_provider_settings)
        self.layout.addRow(self.provider_label, self.provider_combo)

        self.add_provider_button = QPushButton("Add Provider", self)
        self.add_provider_button.clicked.connect(self.add_new_provider)
        self.layout.addWidget(self.add_provider_button)

        self.remove_provider_button = QPushButton("Remove Provider", self)
        self.remove_provider_button.clicked.connect(self.remove_provider)
        self.layout.addWidget(self.remove_provider_button)

        self.create_stream_type_ui()
        self.url_label = QLabel("Server URL:", self)
        self.url_input = QLineEdit(self)
        self.layout.addRow(self.url_label, self.url_input)

        self.mac_label = QLabel("MAC Address (STB only):", self)
        self.mac_input = QLineEdit(self)
        self.layout.addRow(self.mac_label, self.mac_input)

        self.file_button = QPushButton("Load File", self)
        self.file_button.clicked.connect(self.load_file)
        self.layout.addWidget(self.file_button)

        self.verify_button = QPushButton("Verify Provider", self)
        self.verify_button.clicked.connect(self.verify_provider)
        self.layout.addWidget(self.verify_button)
        self.verify_result = QLabel("", self)
        self.layout.addWidget(self.verify_result)
        self.save_button = QPushButton("Save", self)
        self.save_button.clicked.connect(self.save_settings)
        self.layout.addWidget(self.save_button)

    def create_stream_type_ui(self):
        self.type_label = QLabel("Stream Type:", self)
        self.type_group = QButtonGroup(self)
        self.type_STB = QRadioButton("STB", self)
        self.type_M3UPLAYLIST = QRadioButton("M3U Playlist", self)
        self.type_M3USTREAM = QRadioButton("M3U Stream", self)
        self.type_group.addButton(self.type_STB)
        self.type_group.addButton(self.type_M3UPLAYLIST)
        self.type_group.addButton(self.type_M3USTREAM)

        self.type_STB.toggled.connect(self.update_inputs)
        self.type_M3UPLAYLIST.toggled.connect(self.update_inputs)
        self.type_M3USTREAM.toggled.connect(self.update_inputs)

        self.layout.addRow(self.type_label)
        self.layout.addRow(self.type_STB)
        self.layout.addRow(self.type_M3UPLAYLIST)
        self.layout.addRow(self.type_M3USTREAM)

    def load_providers(self):
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        for i, provider in enumerate(self.config["data"]):
            self.provider_combo.addItem(f"Provider {i + 1}: {provider['url']}", userData=provider)
        self.provider_combo.blockSignals(False)
        self.provider_combo.setCurrentIndex(self.selected_provider_index)
        self.load_provider_settings(self.selected_provider_index)

    def load_provider_settings(self, index):
        if index == -1 or index >= len(self.config["data"]):
            return
        self.selected_provider_index = index
        self.selected_provider = self.config["data"][index]
        self.url_input.setText(self.selected_provider.get("url", ""))
        self.mac_input.setText(self.selected_provider.get("mac", ""))
        self.update_radio_buttons()
        self.update_inputs()

    def update_radio_buttons(self):
        provider_type = self.selected_provider.get("type", "")
        self.type_STB.setChecked(provider_type == "STB")
        self.type_M3UPLAYLIST.setChecked(provider_type == "M3UPLAYLIST")
        self.type_M3USTREAM.setChecked(provider_type == "M3USTREAM")

    def update_inputs(self):
        self.mac_label.setVisible(self.type_STB.isChecked())
        self.mac_input.setVisible(self.type_STB.isChecked())
        self.file_button.setVisible(
            self.type_M3UPLAYLIST.isChecked() or self.type_M3USTREAM.isChecked()
        )
        self.url_input.setEnabled(True)

    def add_new_provider(self):
        new_provider = {"type": "STB", "url": "", "mac": ""}
        self.config["data"].append(new_provider)
        self.load_providers()
        self.provider_combo.setCurrentIndex(len(self.config["data"]) - 1)

    def remove_provider(self):
        if len(self.config["data"]) == 1:
            return
        del self.config["data"][self.provider_combo.currentIndex()]
        self.load_providers()
        self.provider_combo.setCurrentIndex(min(self.selected_provider_index, len(self.config["data"]) - 1))

    def save_settings(self):
        if self.selected_provider:
            self.selected_provider["url"] = self.url_input.text()
            self.selected_provider["mac"] = (
                self.mac_input.text() if self.type_STB.isChecked() else ""
            )
            self.selected_provider["type"] = (
                "STB"
                if self.type_STB.isChecked()
                else "M3UPLAYLIST" if self.type_M3UPLAYLIST.isChecked() else "M3USTREAM"
            )
            self.config["selected"] = self.selected_provider_index
            self.parent().save_config()
            self.parent().load_channels()
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
        if self.type_STB.isChecked():
            result = self.parent().do_handshake(
                self.url_input.text(), self.mac_input.text(), load=False
            )
        elif self.type_M3UPLAYLIST.isChecked() or self.type_M3USTREAM.isChecked():
            result = self.parent().verify_url(self.url_input.text())
        self.verify_result.setText(
            "Provider verified successfully."
            if result
            else "Failed to verify provider."
        )
        self.verify_result.setStyleSheet("color: green;" if result else "color: red;")
