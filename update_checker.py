import requests
from PySide6.QtWidgets import QMessageBox

from config_manager import ConfigManager


def check_for_updates():
    repo = "ozankaraali/QiTV"
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        response = requests.get(api_url)
        response.raise_for_status()
        latest_release = response.json()
        latest_version = extract_version_from_tag(latest_release["name"])
        if latest_version and compare_versions(
            latest_version, ConfigManager.CURRENT_VERSION
        ):
            show_update_dialog(latest_version, latest_release["html_url"])
        else:
            print("No new updates found or version is pre-release.")
    except requests.RequestException as e:
        print(f"Error checking for updates: {e}")


def extract_version_from_tag(tag):
    # Handle common tag formats like 'v1.2.3', 'release-1.2.3', or '1.2.3'
    import re

    match = re.search(r"(\d+\.\d+\.\d+)", tag)
    if match:
        return match.group(0)
    else:
        print(f"Unexpected version format found: '{tag}'")
        return None


def compare_versions(latest_version, current_version):
    latest_version_parts = list(map(int, latest_version.split(".")))
    current_version_parts = list(map(int, current_version.split(".")))
    return latest_version_parts > current_version_parts


def show_update_dialog(latest_version, release_url):
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Information)
    msg.setText(f"A new version ({latest_version}) is available!")
    msg.setInformativeText(
        "Would you like to open the release page to download the update?"
    )
    msg.setWindowTitle("Update Available")
    msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    if msg.exec_() == QMessageBox.Yes:
        import webbrowser

        webbrowser.open(release_url)
