"""Native menu bar for qiTV main window."""
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import QMenu, QMenuBar


class AppMenuBar:
    """Builds and manages the native menu bar for the ChannelList window."""

    def __init__(self, parent_window):
        self.window = parent_window
        self.menu_bar = parent_window.menuBar()
        self._provider_action_group = None
        self._provider_actions = {}

        self._build_file_menu()
        self._build_edit_menu()
        self._build_view_menu()
        self._build_providers_menu()
        self._build_help_menu()

    # --- File menu ---
    def _build_file_menu(self):
        menu = self.menu_bar.addMenu("&File")

        self.open_file_action = menu.addAction("&Open File...")
        self.open_file_action.setShortcut(QKeySequence.Open)

        # Export submenu
        self.export_menu = menu.addMenu("&Export")
        self.export_shown_action = self.export_menu.addAction("Export Shown Channels")
        self.export_menu.addSeparator()
        self.export_cached_action = self.export_menu.addAction("Export Cached Content")
        self.export_complete_action = self.export_menu.addAction("Export Complete (Fetch All)")
        self.export_menu.addSeparator()
        self.export_all_live_action = self.export_menu.addAction("Export All Live Channels")

        menu.addSeparator()

        self.settings_action = menu.addAction("&Settings...")
        self.settings_action.setShortcut(QKeySequence("Ctrl+,"))

    # --- Edit menu ---
    def _build_edit_menu(self):
        menu = self.menu_bar.addMenu("&Edit")

        self.update_action = menu.addAction("&Update Content")
        self.update_action.setShortcut(QKeySequence("Ctrl+R"))

        self.rescan_logos_action = menu.addAction("Rescan &Logos")

        menu.addSeparator()

        self.search_descriptions_action = menu.addAction("Search &Descriptions")
        self.search_descriptions_action.setCheckable(True)

    # --- View menu ---
    def _build_view_menu(self):
        menu = self.menu_bar.addMenu("&View")

        self.show_epg_action = menu.addAction("Show &EPG")
        self.show_epg_action.setCheckable(True)

        self.show_vod_info_action = menu.addAction("Show &VOD Info")
        self.show_vod_info_action.setCheckable(True)

        self.show_info_panel_action = menu.addAction("Show &Info Panel")
        self.show_info_panel_action.setCheckable(True)

        menu.addSeparator()

        # Player selection submenu
        play_menu = menu.addMenu("&Play with")
        self._player_group = QActionGroup(self.window)
        self._player_group.setExclusive(True)

        self.player_internal_action = play_menu.addAction("Internal Player")
        self.player_internal_action.setCheckable(True)
        self._player_group.addAction(self.player_internal_action)

        self.player_vlc_action = play_menu.addAction("VLC")
        self.player_vlc_action.setCheckable(True)
        self._player_group.addAction(self.player_vlc_action)

        self.player_mpv_action = play_menu.addAction("MPV")
        self.player_mpv_action.setCheckable(True)
        self._player_group.addAction(self.player_mpv_action)

    # --- Providers menu ---
    def _build_providers_menu(self):
        self.providers_menu = self.menu_bar.addMenu("&Providers")

        self.add_provider_action = self.providers_menu.addAction("&Add Provider...")
        self.edit_providers_action = self.providers_menu.addAction("&Edit Providers...")

        self._provider_separator = self.providers_menu.addSeparator()

        self._provider_action_group = QActionGroup(self.window)
        self._provider_action_group.setExclusive(True)

    def set_providers(self, provider_names, selected_name=None):
        """Update the provider radio items in the Providers menu."""
        # Remove old provider actions
        for action in self._provider_actions.values():
            self._provider_action_group.removeAction(action)
            self.providers_menu.removeAction(action)
        self._provider_actions.clear()

        for name in provider_names:
            action = self.providers_menu.addAction(name)
            action.setCheckable(True)
            self._provider_action_group.addAction(action)
            self._provider_actions[name] = action
            if name == selected_name:
                action.setChecked(True)

    def select_provider(self, name):
        """Set the checked provider in the menu."""
        action = self._provider_actions.get(name)
        if action:
            action.setChecked(True)

    # --- Help menu ---
    def _build_help_menu(self):
        menu = self.menu_bar.addMenu("&Help")
        self.shortcuts_action = menu.addAction("&Keyboard Shortcuts")
        self.about_action = menu.addAction("&About qiTV")

    # --- State sync ---
    def sync_from_config(self, config_manager):
        """Set initial checked states from config."""
        self.show_epg_action.setChecked(config_manager.channel_epg)
        self.show_vod_info_action.setChecked(config_manager.show_stb_content_info)
        self.search_descriptions_action.setChecked(False)

        if config_manager.play_in_vlc:
            self.player_vlc_action.setChecked(True)
        elif config_manager.play_in_mpv:
            self.player_mpv_action.setChecked(True)
        else:
            self.player_internal_action.setChecked(True)
