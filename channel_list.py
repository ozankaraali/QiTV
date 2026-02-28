"""Core ChannelList window - UI setup, navigation, providers, menus, shortcuts.

Domain-specific logic is in mixin modules (mixins/) and worker classes (workers.py).
"""

from datetime import datetime
import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
import tzlocal

from content_loader import ContentLoader
from image_loader import ImageLoader
from mixins import ContentLoadingMixin, DisplayMixin, ExportMixin, PlaybackMixin
from options import OptionsDialog
from widgets.delegates import HtmlItemDelegate
from widgets.menu_bar import AppMenuBar
from widgets.sidebar import Sidebar
from widgets.top_bar import TopBar
from workers import SetProviderThread

logger = logging.getLogger(__name__)


class ChannelList(
    PlaybackMixin,
    ContentLoadingMixin,
    ExportMixin,
    DisplayMixin,
    QMainWindow,
):
    def __init__(self, app, player, config_manager, provider_manager, image_manager, epg_manager):
        super().__init__()
        self.app = app
        self.player = player
        self.config_manager = config_manager
        self.provider_manager = provider_manager
        self.image_manager = image_manager
        self.epg_manager = epg_manager
        self.splitter_ratio = 0.75
        self.splitter_content_info_ratio = 0.33
        self.config_manager.apply_window_settings("channel_list", self)

        self.setWindowTitle("QiTV Content List")

        self.container_widget = QWidget(self)
        self.setCentralWidget(self.container_widget)

        self.content_type = "itv"  # Default to channels (STB type)
        self.current_list_content: Optional[str] = None
        self.content_info_shown: Optional[str] = None
        self.image_loader: Optional[ImageLoader] = None
        self.content_loader: Optional[ContentLoader] = None
        self._provider_combo_connected = False  # Track if signal is connected
        self._all_providers_mode = False
        self._all_provider_cache_snapshot: List[tuple[str, dict]] = []
        self._pending_cross_provider_activation: Optional[Dict[str, Any]] = None
        self._info_panel_enabled = True

        self.link: Optional[str] = None
        self.current_category: Optional[Dict[str, Any]] = None  # For back navigation
        self.current_series: Optional[Dict[str, Any]] = None
        self.current_season: Optional[Dict[str, Any]] = None
        self.navigation_stack = []  # To keep track of navigation for back button
        self.forward_stack = []  # Forward history to undo last Back
        self._suppress_forward_clear = False

        # Auto-play state tracking
        self._current_playing_item: Optional[Dict[str, Any]] = None
        self._current_playing_type: Optional[str] = None
        self._current_episode_index: int = -1
        self._current_episode_list: List[Dict[str, Any]] = []
        self._current_seasons_list: List[Dict[str, Any]] = []
        self._current_category_movies: List[Dict[str, Any]] = []
        self._current_content_id: Optional[str] = None
        self._autoplay_dialog: Optional[QDialog] = None

        # External VLC player instance (for single-instance behavior)
        self._external_vlc_instance = None
        self._external_vlc_player = None

        # External MPV player instance (for single-instance behavior)
        self._external_mpv_player = None

        # Create UI components
        self.create_top_bar()
        self.sidebar = Sidebar(self.container_widget)
        self.create_list_panel()
        self.create_content_info_panel()

        # Build the menu bar
        self.app_menu = AppMenuBar(self)
        self._connect_menu_actions()

        # Populate providers into combo, sidebar, and menu (after all UI exists)
        self.populate_provider_combo()

        # Right side: top bar + content list
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self.top_bar)
        right_layout.addWidget(self.list_panel)

        widget_top = QWidget()
        top_layout = QHBoxLayout(widget_top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)
        top_layout.addWidget(self.sidebar)
        top_layout.addWidget(right_panel, 1)

        # Splitter with content info part
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(widget_top)
        self.splitter.addWidget(self.content_info_panel)
        self.splitter.setSizes([1, 0])
        self.splitter.setHandleWidth(5)
        self.splitter.setStyleSheet(
            """
            QSplitter::handle {
                background-color: rgba(128, 128, 128, 0.2);
                border-radius: 2px;
            }
            QSplitter::handle:hover {
                background-color: rgba(128, 128, 128, 0.4);
            }
        """
        )

        container_layout = QVBoxLayout(self.container_widget)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.addWidget(self.splitter)

        # Connect player signals for auto-play and position tracking
        self.player.mediaEnded.connect(self.on_media_ended)
        self.player.positionChanged.connect(self.on_position_changed)

        # Input integration from player: mouse back/forward and remote Up/Down
        try:
            self.player.backRequested.connect(self.go_back)
            self.player.forwardRequested.connect(self.go_forward)
            self.player.channelNextRequested.connect(self.channel_surf_next)
            self.player.channelPrevRequested.connect(self.channel_surf_prev)
        except Exception:
            pass

        self.splitter.splitterMoved.connect(self.update_splitter_ratio)

        # Global shortcuts mirrored on main window
        self._setup_global_shortcuts()

        # Create a timer to update "On Air" status
        self.refresh_on_air_timer = QTimer(self)
        self.refresh_on_air_timer.timeout.connect(self.refresh_on_air)

        self.update_layout()

        self.set_provider()

        # Keep references to background jobs (threads/workers)
        self._bg_jobs = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        # Stop and delete timer
        if self.refresh_on_air_timer.isActive():
            self.refresh_on_air_timer.stop()
        self.refresh_on_air_timer.deleteLater()

        self.app.quit()
        self.player.close()
        self.image_manager.save_index()
        self.epg_manager.save_index()
        self.config_manager.save_window_settings(self, "channel_list")
        event.accept()

    # ------------------------------------------------------------------
    # EPG refresh
    # ------------------------------------------------------------------

    def refresh_on_air(self):
        for i in range(self.content_list.topLevelItemCount()):
            item = self.content_list.topLevelItem(i)
            item_data = item.data(0, Qt.UserRole)
            content_type = item_data.get("type")

            if self.config_manager.channel_epg and self.can_show_epg(content_type):
                epg_data = self.epg_manager.get_programs_for_channel(item_data["data"], None, 1)
                if epg_data:
                    epg_item = epg_data[0]
                    # Determine format by keys (robust against mixed sources)
                    if "time" in epg_item and "time_to" in epg_item:
                        start_time = datetime.strptime(epg_item["time"], "%Y-%m-%d %H:%M:%S")
                        end_time = datetime.strptime(epg_item["time_to"], "%Y-%m-%d %H:%M:%S")
                    elif "@start" in epg_item and "@stop" in epg_item:
                        start_time = datetime.strptime(epg_item["@start"], "%Y%m%d%H%M%S %z")
                        end_time = datetime.strptime(epg_item["@stop"], "%Y%m%d%H%M%S %z")
                    else:
                        # Unknown structure; skip gracefully
                        item.setData(2, Qt.UserRole, 0)
                        item.setData(3, Qt.UserRole, "")
                        continue
                    now = datetime.now(start_time.tzinfo)
                    if end_time != start_time:
                        progress = (
                            100
                            * (now - start_time).total_seconds()
                            / (end_time - start_time).total_seconds()
                        )
                    else:
                        progress = 0 if now < start_time else 100
                    progress = max(0, min(100, progress))
                    if "title" in epg_item:  # XMLTV style
                        title_val = epg_item.get("title")
                        text = ""
                        if isinstance(title_val, dict):
                            text = title_val.get("__text") or ""
                        elif isinstance(title_val, list) and title_val:
                            # take first element's text if present
                            first = title_val[0]
                            if isinstance(first, dict):
                                text = first.get("__text") or ""
                        # Localize displayed times
                        try:
                            local_tz = tzlocal.get_localzone()
                            ls = start_time.astimezone(local_tz).strftime("%H:%M")
                            le = end_time.astimezone(local_tz).strftime("%H:%M")
                            epg_text = f"{ls}-{le}  {str(text)}"
                        except Exception:
                            epg_text = str(text)
                    else:
                        # STB style: treat naive datetimes as local
                        try:
                            ls = start_time.strftime("%H:%M")
                            le = end_time.strftime("%H:%M")
                            name_txt = str(epg_item.get("name") or "")
                            epg_text = f"{ls}-{le}  {name_txt}"
                        except Exception:
                            epg_text = str(epg_item.get("name") or "")
                    item.setData(2, Qt.UserRole, progress)
                    item.setData(3, Qt.UserRole, epg_text)
                else:
                    # Avoid passing None to Qt (causes _pythonToCppCopy warnings)
                    item.setData(2, Qt.UserRole, 0)
                    item.setData(3, Qt.UserRole, "")

        self.content_list.viewport().update()

    # ------------------------------------------------------------------
    # Provider management
    # ------------------------------------------------------------------

    def set_provider(self, force_update=False):
        self.lock_ui_before_loading()
        self.progress_bar.setRange(0, 0)  # busy indicator

        if force_update:
            self.provider_manager.clear_current_provider_cache()

        # Reset navigation histories on provider switch
        self.navigation_stack.clear()
        self.forward_stack.clear()

        # Remember if this call was a forced update so we can use it in the
        # UI-thread handler safely.
        self._set_provider_force_update = force_update

        self.set_provider_thread = SetProviderThread(
            self.provider_manager,
            self.epg_manager,
            force_epg_refresh=bool(force_update),
        )
        self.set_provider_thread.progress.connect(self.update_busy_progress)
        # Ensure the finished handler runs on the GUI thread (no lambda)
        self.set_provider_thread.finished.connect(
            self._on_set_provider_thread_finished, Qt.QueuedConnection
        )
        self.set_provider_thread.start()

    def set_provider_finished(self, force_update=False):
        self.progress_bar.setRange(0, 100)  # Stop busy indicator
        if hasattr(self, "set_provider_thread"):
            self.set_provider_thread.deleteLater()
            del self.set_provider_thread
        self.unlock_ui_after_loading()

        # Connect provider combo signal after first initialization (deferred to main thread)
        if not self._provider_combo_connected:
            QTimer.singleShot(0, lambda: self._connect_provider_combo_signal())
            self._provider_combo_connected = True

        # No need to switch content type if not STB
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")

        if force_update:
            self.update_content()
        else:
            self.load_content()

        # If a resume operation was requested while switching provider, handle it now
        pending = getattr(self, "_pending_resume", None)
        if pending:
            try:
                item_data = pending.get("item_data")
                item_type = pending.get("item_type")
                is_episode = item_type == "episode"

                # Ensure content_type matches the item being resumed
                if item_type == "channel":
                    self.content_type = "itv"
                elif item_type == "movie":
                    self.content_type = "vod"
                elif item_type == "episode":
                    self.content_type = "series"

                current_provider_type = self.provider_manager.current_provider.get("type", "")
                if current_provider_type == "STB":
                    self.play_item(item_data, is_episode=is_episode, item_type=item_type)
                elif pending.get("link"):
                    self.link = pending["link"]
                    self._play_content(self.link)
                else:
                    # Fallback: recreate the link
                    self.play_item(item_data, is_episode=is_episode, item_type=item_type)
            finally:
                self._pending_resume = None

        pending_cross_provider = getattr(self, "_pending_cross_provider_activation", None)
        if pending_cross_provider:
            try:
                self._activate_cross_provider_result(
                    item_data=pending_cross_provider["item_data"],
                    item_type=pending_cross_provider["item_type"],
                    source_content_type=pending_cross_provider["source_content_type"],
                    provider_name=None,
                )
            finally:
                self._pending_cross_provider_activation = None

    def _connect_provider_combo_signal(self):
        """Connect provider combo signal (called after initialization)."""
        # Avoid disconnecting when not connected (causes warnings); connect once.
        self.provider_combo.currentTextChanged.connect(self.on_provider_changed)

    def _on_set_provider_thread_finished(self):
        # Called in the GUI thread after provider setup completes in background
        force_update = getattr(self, "_set_provider_force_update", False)
        self.set_provider_finished(force_update)

    def populate_provider_combo(self):
        """Populate the provider dropdown with available providers."""
        # Block signals to prevent triggering change during population
        was_blocked = self.provider_combo.blockSignals(True)

        try:
            self.provider_combo.clear()

            for provider in self.provider_manager.providers:
                self.provider_combo.addItem(provider["name"])

            # Set current provider
            current_name = self.config_manager.selected_provider_name
            index = self.provider_combo.findText(current_name)
            if index >= 0:
                self.provider_combo.setCurrentIndex(index)
        finally:
            # Restore previous signal blocking state
            self.provider_combo.blockSignals(was_blocked)

        # Also populate sidebar and menu bar if they exist
        provider_names = [
            self.provider_combo.itemText(i) for i in range(self.provider_combo.count())
        ]
        current_name = self.config_manager.selected_provider_name
        if hasattr(self, "sidebar"):
            self.sidebar.set_providers(provider_names)
            if current_name:
                self.sidebar.select_provider(current_name)
        if hasattr(self, "app_menu"):
            self.app_menu.set_providers(provider_names, current_name)

    def on_provider_changed(self, provider_name):
        """Handle provider selection change from combo box."""
        if not provider_name:
            return

        # Check if this is actually a change
        if provider_name == self.config_manager.selected_provider_name:
            return

        # Update config
        self.config_manager.selected_provider_name = provider_name
        self.config_manager.save_config()

        # Reload provider (use QTimer to ensure we're in the main thread)
        QTimer.singleShot(0, lambda: self.set_provider())

    # ------------------------------------------------------------------
    # Splitter management
    # ------------------------------------------------------------------

    def update_splitter_ratio(self, pos, index):
        sizes = self.splitter.sizes()
        total_size = sizes[0] + sizes[1]
        if total_size:
            self.splitter_ratio = sizes[0] / total_size

    def update_splitter_content_info_ratio(self, pos, index):
        sizes = self.splitter_content_info.sizes()
        total_size = sizes[0] + sizes[1]
        if total_size:
            self.splitter_content_info_ratio = sizes[0] / total_size

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def create_top_bar(self):
        self.top_bar = TopBar(self.container_widget)
        self.top_bar.back_clicked.connect(self.go_back)
        self.top_bar.search_changed.connect(self.filter_content)

        # Hidden provider combo kept for data-population compatibility
        self.provider_combo = QComboBox()
        self.provider_combo.setVisible(False)

    def _setup_global_shortcuts(self):
        def not_in_text_input():
            from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QTextEdit

            w = QApplication.focusWidget()
            return not isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit))

        # Playback shortcuts: mirror on ChannelList with Window scope
        # so they work when this window is active, without colliding
        # with VideoPlayer's own shortcuts when it has focus.

        # Fullscreen
        act_full = QAction("Fullscreen", self)
        act_full.setShortcut(QKeySequence(Qt.Key_F))
        act_full.setShortcutContext(Qt.WindowShortcut)
        act_full.triggered.connect(
            lambda: self.player.toggle_fullscreen() if not_in_text_input() else None
        )
        self.addAction(act_full)

        # Mute
        act_mute = QAction("Mute", self)
        act_mute.setShortcut(QKeySequence(Qt.Key_M))
        act_mute.setShortcutContext(Qt.WindowShortcut)
        act_mute.triggered.connect(
            lambda: self.player.toggle_mute() if not_in_text_input() else None
        )
        self.addAction(act_mute)

        # Play/Pause
        act_play = QAction("Play/Pause", self)
        act_play.setShortcut(QKeySequence(Qt.Key_Space))
        act_play.setShortcutContext(Qt.WindowShortcut)
        act_play.triggered.connect(
            lambda: self.player.toggle_play_pause() if not_in_text_input() else None
        )
        self.addAction(act_play)

        # Picture-in-Picture
        act_pip = QAction("PiP", self)
        act_pip.setShortcut(QKeySequence(Qt.ALT | Qt.Key_P))
        act_pip.setShortcutContext(Qt.WindowShortcut)

        def _pip():
            if not_in_text_input():
                if self.player.windowState() == Qt.WindowFullScreen:
                    self.player.setWindowState(Qt.WindowNoState)
                self.player.toggle_pip_mode()

        act_pip.triggered.connect(_pip)
        self.addAction(act_pip)

        # Back navigation via keyboard (Backspace/Back keys)
        act_back = QAction("Back", self)
        act_back.setShortcutContext(Qt.ApplicationShortcut)
        try:
            act_back.setShortcuts([QKeySequence(Qt.Key_Backspace), QKeySequence(Qt.Key_Back)])
        except Exception:
            act_back.setShortcut(QKeySequence(Qt.Key_Backspace))
        act_back.triggered.connect(self.go_back)
        self.addAction(act_back)

        # Forward navigation via keyboard (Forward key / Alt+Right fallback)
        act_forward = QAction("Forward", self)
        act_forward.setShortcutContext(Qt.ApplicationShortcut)
        forward_shortcuts = []
        try:
            forward_shortcuts.append(QKeySequence(Qt.Key_Forward))
        except Exception:
            pass
        try:
            # StandardKey.Forward
            forward_shortcuts.append(QKeySequence(QKeySequence.StandardKey.Forward))
        except Exception:
            pass
        if not forward_shortcuts:
            try:
                forward_shortcuts = [QKeySequence(Qt.ALT | Qt.Key_Right)]
            except Exception:
                forward_shortcuts = []
        if forward_shortcuts:
            act_forward.setShortcuts(forward_shortcuts)
        act_forward.triggered.connect(self.go_forward)
        self.addAction(act_forward)

    def create_list_panel(self):
        self.list_panel = QWidget(self.container_widget)
        list_layout = QVBoxLayout(self.list_panel)
        list_layout.setContentsMargins(0, 0, 0, 0)

        self.content_list = QTreeWidget(self.list_panel)
        self.content_list.setSelectionMode(QTreeWidget.SingleSelection)
        self.content_list.setIndentation(0)
        self.content_list.setAlternatingRowColors(True)
        self.content_list.itemSelectionChanged.connect(self.item_selected)
        self.content_list.itemActivated.connect(self.item_activated)
        self.content_list.installEventFilter(self)
        self.content_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.content_list.customContextMenuRequested.connect(self.show_content_context_menu)
        self.refresh_content_list_size()

        list_layout.addWidget(self.content_list, 1)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        list_layout.addWidget(self.progress_bar)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_loading)
        self.cancel_button.setVisible(False)
        list_layout.addWidget(self.cancel_button)

    def create_content_info_panel(self):
        self.content_info_panel = QWidget(self.container_widget)
        self.content_info_layout = QVBoxLayout(self.content_info_panel)
        self.content_info_panel.setVisible(False)

    def setup_movie_tvshow_content_info(self):
        self.clear_content_info_panel()
        self.content_info_text = QLabel(self.content_info_panel)
        self.content_info_text.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Ignored
        )  # Allow to reduce splitter below label minimum size
        self.content_info_text.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.content_info_text.setWordWrap(True)
        self.content_info_layout.addWidget(self.content_info_text, 1)
        self.content_info_shown = "movie_tvshow"

    def setup_channel_program_content_info(self):
        self.clear_content_info_panel()
        self.splitter_content_info = QSplitter(Qt.Horizontal)
        self.program_list = QListWidget()
        self.program_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.program_list.setItemDelegate(HtmlItemDelegate())
        self.splitter_content_info.addWidget(self.program_list)
        self.content_info_text = QLabel()
        self.content_info_text.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.content_info_text.setWordWrap(True)
        self.splitter_content_info.addWidget(self.content_info_text)
        self.content_info_layout.addWidget(self.splitter_content_info)
        self.splitter_content_info.setSizes(
            [
                int(self.content_info_panel.width() * self.splitter_content_info_ratio),
                int(self.content_info_panel.width() * (1 - self.splitter_content_info_ratio)),
            ]
        )
        self.content_info_shown = "channel"

        self.program_list.itemSelectionChanged.connect(self.update_channel_program)
        self.splitter_content_info.splitterMoved.connect(self.update_splitter_content_info_ratio)

    def clear_content_info_panel(self):
        # Clear all widgets from the content_info layout
        for i in reversed(range(self.content_info_layout.count())):
            widget = self.content_info_layout.itemAt(i).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        # Clear the layout itself
        while self.content_info_layout.count():
            item = self.content_info_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self.clear_layout(item.layout())

        # Hide the content_info panel if it is visible
        if self.content_info_panel.isVisible():
            self.content_info_panel.setVisible(False)
            self.splitter.setSizes([1, 0])

        self.content_info_shown = None
        self.update_layout()

    def update_layout(self):
        # Layout margins are handled by individual widgets (sidebar, top_bar)
        if self.content_info_panel.isVisible():
            self.content_info_layout.setContentsMargins(8, 4, 8, 8)

    @staticmethod
    def clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # ------------------------------------------------------------------
    # Menu & signal connections
    # ------------------------------------------------------------------

    def _connect_menu_actions(self):
        """Connect menu bar actions to existing handler methods."""
        m = self.app_menu

        # File menu
        m.open_file_action.triggered.connect(self.open_file)
        m.export_shown_action.triggered.connect(self.export_shown_channels)
        m.export_cached_action.triggered.connect(self.export_content_cached)
        m.export_complete_action.triggered.connect(self.export_content_complete)
        m.export_all_live_action.triggered.connect(self.export_all_live_channels)
        m.settings_action.triggered.connect(self.options_dialog)

        # Edit menu
        m.update_action.triggered.connect(lambda: self.set_provider(force_update=True))
        m.rescan_logos_action.triggered.connect(self.rescan_logos)
        m.search_descriptions_action.toggled.connect(
            lambda checked: self.filter_content(self.top_bar.search_text())
        )

        # View menu
        m.show_epg_action.toggled.connect(self._on_menu_epg_toggled)
        m.show_vod_info_action.toggled.connect(self._on_menu_vod_info_toggled)
        m.show_info_panel_action.toggled.connect(self._on_menu_info_panel_toggled)
        m.player_internal_action.triggered.connect(lambda: self._set_player_mode("internal"))
        m.player_vlc_action.triggered.connect(lambda: self._set_player_mode("vlc"))
        m.player_mpv_action.triggered.connect(lambda: self._set_player_mode("mpv"))

        # Providers menu
        m.add_provider_action.triggered.connect(self.options_dialog)
        m.edit_providers_action.triggered.connect(self.options_dialog)
        assert m._provider_action_group is not None
        m._provider_action_group.triggered.connect(
            lambda action: self._on_menu_provider_selected(action.text())
        )

        # Help menu
        m.check_updates_action.triggered.connect(self._manual_check_for_updates)
        m.about_action.triggered.connect(self._show_about)

        # Sidebar signals
        self.sidebar.provider_selected.connect(self._on_sidebar_provider_selected)
        self.sidebar.content_type_changed.connect(self._on_sidebar_content_type)
        self.sidebar.favorites_toggled.connect(self._on_sidebar_favorites)
        self.sidebar.history_clicked.connect(self.show_watch_history)
        self.sidebar.resume_clicked.connect(self.resume_last_watched)

        # Populate hamburger menu (subset of menu bar)
        hm = self.top_bar.hamburger_menu
        hm.addAction(m.settings_action)
        hm.addAction(m.update_action)
        hm.addMenu(m.export_menu)
        hm.addSeparator()
        # Player mode actions directly in hamburger (no submenu for reliable hover)
        hm.addAction(m.player_internal_action)
        hm.addAction(m.player_vlc_action)
        hm.addAction(m.player_mpv_action)

        # Sync initial state from config
        m.sync_from_config(self.config_manager)
        self._info_panel_enabled = bool(m.show_info_panel_action.isChecked())

    def _on_menu_epg_toggled(self, checked):
        self.config_manager.channel_epg = checked
        self.save_config()
        self.epg_manager.set_current_epg()
        self.refresh_channels()

    def _on_menu_vod_info_toggled(self, checked):
        self.config_manager.show_stb_content_info = checked
        self.save_config()
        self.item_selected()

    def _on_menu_info_panel_toggled(self, checked):
        self._info_panel_enabled = bool(checked)
        self.config_manager.show_info_panel = checked
        self.save_config()
        if self._info_panel_enabled:
            self.item_selected()
        else:
            self.clear_content_info_panel()

    def _set_player_mode(self, mode):
        self.config_manager.play_in_vlc = mode == "vlc"
        self.config_manager.play_in_mpv = mode == "mpv"
        self.save_config()
        if mode == "internal":
            pass  # Player will open on next play
        else:
            if hasattr(self, "player") and self.player.isVisible():
                self.player.close()

    def _on_menu_provider_selected(self, provider_name):
        self.sidebar.select_provider(provider_name)
        self._switch_provider(provider_name)

    def _on_sidebar_provider_selected(self, name):
        if name == "all":
            self._enter_all_providers_mode()
            return
        self._exit_all_providers_mode()
        self.app_menu.select_provider(name)
        self._switch_provider(name)

    def _switch_provider(self, provider_name):
        if provider_name == self.config_manager.selected_provider_name:
            return
        self.config_manager.selected_provider_name = provider_name
        self.config_manager.save_config()
        QTimer.singleShot(0, lambda: self.set_provider())

    def _on_sidebar_content_type(self, content_type):
        self.content_type = content_type
        self.current_category = None
        self.current_series = None
        self.current_season = None
        self.navigation_stack.clear()
        self.forward_stack.clear()
        self.load_content()
        self.top_bar.clear_search()

    def _on_sidebar_favorites(self, checked):
        if checked and self.current_list_content == "category":
            # At category level – show a flat list of only favorited items
            self._show_favorites_flat()
        elif not checked and self.current_category is None:
            # Turning off favorites at root level – restore category view
            self.load_content()
        else:
            # Inside a category or other view – just re-filter the current list
            self.filter_content(self.top_bar.search_text())

    def _show_favorites_flat(self):
        """Load all content across categories and display only favorites."""
        content_data = self.provider_manager.current_provider_content.get(self.content_type, {})
        if not isinstance(content_data, dict):
            return

        # Gather all items from all categories
        all_items = []
        if "sorted_channels" in content_data:
            all_items = content_data.get("contents", [])
        elif isinstance(content_data.get("contents"), dict):
            for cat_items in content_data["contents"].values():
                if isinstance(cat_items, list):
                    all_items.extend(cat_items)

        if not all_items:
            return

        # Filter to favorites only
        favorites = set(self.config_manager.favorites)
        fav_items = [
            item for item in all_items if (item.get("name") or item.get("title", "")) in favorites
        ]

        # Determine display content type
        content_type_map = {"itv": "channel", "series": "serie", "vod": "movie"}
        display_type = content_type_map.get(self.content_type, "m3ucontent")

        if not fav_items:
            # Show empty state
            self.content_list.clear()
            self.content_list.setColumnCount(1)
            self.content_list.setHeaderLabels([f"No favorites in {display_type}s"])
            return

        self.display_content(fav_items, content=display_type)
        # Hide back button – we're still at root level, not inside a category
        self.top_bar.set_back_visible(False)

    def _manual_check_for_updates(self):
        from update_checker import check_for_updates

        check_for_updates(config_manager=self.config_manager, manual=True)

    def _show_about(self):
        from config_manager import get_app_version

        QMessageBox.about(
            self,
            "About qiTV",
            f"qiTV v{get_app_version()}\nA cross-platform IPTV player",
        )

    def _enter_all_providers_mode(self):
        """Enter cross-provider search mode."""
        self._all_providers_mode = True
        self._all_provider_cache_snapshot = self.provider_manager.get_all_providers_cached_content()
        self.content_list.clear()
        self.content_list.setColumnCount(1)
        self.content_list.setHeaderLabels(["Type to search across all providers..."])
        self.top_bar.search_box.setPlaceholderText("Search all providers (3+ chars)...")
        self.top_bar.search_box.setFocus()

    def _exit_all_providers_mode(self):
        """Exit cross-provider search mode."""
        self._all_providers_mode = False
        self._all_provider_cache_snapshot = []
        self.top_bar.search_box.setPlaceholderText("Search content...")

    # ------------------------------------------------------------------
    # EPG display helpers
    # ------------------------------------------------------------------

    def show_vodinfo(self):
        self.config_manager.show_stb_content_info = self.app_menu.show_vod_info_action.isChecked()
        self.save_config()
        self.item_selected()

    def show_epg(self):
        self.config_manager.channel_epg = self.app_menu.show_epg_action.isChecked()
        self.save_config()

        # Refresh the EPG data
        self.epg_manager.set_current_epg()
        self.refresh_channels()

    def refresh_channels(self):
        # No refresh for content other than itv
        if self.content_type != "itv":
            return
        # No refresh from itv list of categories
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "").upper()
        if config_type == "STB" and not self.current_category:
            return

        # Get the index of the selected item in the content list
        selected_item = self.content_list.selectedItems()
        selected_row = None
        if selected_item:
            selected_row = self.content_list.indexOfTopLevelItem(selected_item[0])

        # Store how was sorted the content list
        sort_column = self.content_list.sortColumn()

        # Update the content list
        if config_type != "STB":
            # For non-STB (Xtream or M3U), display content directly
            content_data = self.provider_manager.current_provider_content.get(self.content_type, {})
            # Get the items from either 'contents' or the content_data itself
            items = content_data.get("contents", content_data)

            # Determine content type for display
            if config_type == "XTREAM":
                content_type_name = "channel"
            else:
                content_type_name = "m3ucontent"

            self.display_content(items, content=content_type_name, select_first=False)
        else:
            # Reload the current category
            self.load_content_in_category(self.current_category)

        # Restore the sorting
        self.content_list.sortItems(sort_column, self.content_list.header().sortIndicatorOrder())

        # Restore the selected item
        if selected_row is not None:
            item = self.content_list.topLevelItem(selected_row)
            if item:
                self.content_list.setCurrentItem(item)
                self.item_selected()

    def can_show_content_info(self, item_type):
        # Show metadata panel for VOD/Series across STB and Xtream providers
        return item_type in ["movie", "serie", "season", "episode"]

    def can_show_epg(self, item_type):
        if item_type in ["channel", "m3ucontent"]:
            if self.config_manager.epg_source == "No Source":
                return False
            if self.config_manager.epg_source == "STB":
                # STB EPG source works with both STB and Xtream providers
                provider_type = self.provider_manager.current_provider.get("type", "").upper()
                if provider_type not in ["STB", "XTREAM"]:
                    return False
            return True
        return False

    # ------------------------------------------------------------------
    # Item selection & activation
    # ------------------------------------------------------------------

    def item_selected(self):
        if not self._info_panel_enabled:
            self.clear_content_info_panel()
            return

        selected_items = self.content_list.selectedItems()
        if selected_items:
            item = selected_items[0]
            data = item.data(0, Qt.UserRole)
            if data and "type" in data:
                item_data = data["data"]
                item_type = item.data(0, Qt.UserRole)["type"]

                if (
                    self.can_show_content_info(item_type)
                    and self.config_manager.show_stb_content_info
                ):
                    self.switch_content_info_panel(item_type)
                    self.populate_movie_tvshow_content_info(item_data)
                elif self.can_show_epg(item_type) and self.config_manager.channel_epg:
                    self.switch_content_info_panel(item_type)
                    self.populate_channel_programs_content_info(item_data)
                else:
                    self.clear_content_info_panel()
                self.update_layout()

    def item_activated(self, item):
        data = item.data(0, Qt.UserRole)
        if data and "type" in data:
            item_data = data["data"]
            item_type = item.data(0, Qt.UserRole)["type"]

            if data.get("cross_provider_result"):
                self._activate_cross_provider_result(
                    item_data=item_data,
                    item_type=item_type,
                    source_content_type=data.get("source_content_type", "itv"),
                    provider_name=data.get("provider"),
                )
                return

            # Clear forward history unless we are performing a programmatic forward
            if not getattr(self, "_suppress_forward_clear", False):
                self.forward_stack.clear()

            nav_len = len(self.navigation_stack)
            if item_type == "category":
                self.navigation_stack.append(("root", self.current_category, item.text(0)))
                self.current_category = item_data
                self.load_content_in_category(item_data)
            elif item_type == "serie":
                if self.content_type == "series":
                    # For series, load seasons
                    self.navigation_stack.append(("category", self.current_category, item.text(0)))
                    self.current_series = item_data
                    self.load_series_seasons(item_data)
            elif item_type == "season":
                # Load episodes for the selected season
                self.navigation_stack.append(("series", self.current_series, item.text(0)))
                self.current_season = item_data
                self.load_season_episodes(item_data)
            elif item_type in ["m3ucontent", "channel", "movie"]:
                self.play_item(item_data, item_type=item_type)
            elif item_type == "episode":
                # Play the selected episode
                self.play_item(item_data, is_episode=True, item_type=item_type)
            else:
                logger.info("Unknown item type selected.")

            # Clear search box after navigating and force re-filtering if needed
            if len(self.navigation_stack) != nav_len:
                self.top_bar.clear_search()
                if not self.top_bar.search_box.isModified():
                    self.filter_content(self.top_bar.search_text())
        else:
            logger.info("Item with no type selected.")

    def _activate_cross_provider_result(
        self,
        item_data: Dict[str, Any],
        item_type: str,
        source_content_type: str,
        provider_name: Optional[str],
    ) -> None:
        if provider_name and provider_name != self.config_manager.selected_provider_name:
            self._pending_cross_provider_activation = {
                "item_data": item_data,
                "item_type": item_type,
                "source_content_type": source_content_type,
            }
            self._exit_all_providers_mode()
            self.app_menu.select_provider(provider_name)
            self.sidebar.select_provider(provider_name)
            self._switch_provider(provider_name)
            return

        # Set content type after provider switch completes (not before)
        if source_content_type in {"itv", "vod", "series"}:
            self.content_type = source_content_type
            self.sidebar.select_content_type(source_content_type)

        self._exit_all_providers_mode()
        self.top_bar.clear_search()

        if item_type == "serie":
            self.navigation_stack.clear()
            self.forward_stack.clear()
            self.current_category = None
            self.current_series = item_data
            self.current_season = None
            self.load_series_seasons(item_data)
            return

        if item_type in ["channel", "movie", "m3ucontent"]:
            self.play_item(item_data, item_type=item_type)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def go_back(self):
        if self.navigation_stack:
            nav_type, previous_data, previous_selected_id = self.navigation_stack.pop()
            # Save to forward stack so we can undo this Back
            self.forward_stack.append((nav_type, previous_data, previous_selected_id))
            if nav_type == "root":
                # Display root categories
                content = self.provider_manager.current_provider_content.setdefault(
                    self.content_type, {}
                )
                categories = content.get("categories", [])
                self.display_categories(categories, select_first=previous_selected_id)
                self.current_category = None
            elif nav_type == "category":
                # Go back to category content
                self.current_category = previous_data
                self.load_content_in_category(
                    self.current_category, select_first=previous_selected_id
                )
                self.current_series = None
            elif nav_type == "series":
                # Go back to series seasons
                self.current_series = previous_data
                self.load_series_seasons(self.current_series, select_first=previous_selected_id)
                self.current_season = None

            # Clear search box after navigating backward and force re-filtering if needed
            self.top_bar.clear_search()
            if not self.top_bar.search_box.isModified():
                self.filter_content(self.top_bar.search_text())
        else:
            # Already at the root level
            pass

    def go_forward(self):
        if not self.forward_stack:
            return
        nav_type, previous_data, previous_selected_id = self.forward_stack.pop()
        # Redo the last navigation by selecting the same item again
        try:
            self._suppress_forward_clear = True
            items = self.content_list.findItems(previous_selected_id or "", Qt.MatchExactly, 0)
            if items:
                self.content_list.setCurrentItem(items[0])
                self.item_activated(items[0])
        finally:
            self._suppress_forward_clear = False

    def _is_playable_item(self, item: QTreeWidgetItem) -> bool:
        try:
            data = item.data(0, Qt.UserRole)
            t = data.get("type") if isinstance(data, dict) else None
            return t in {"m3ucontent", "channel", "movie", "episode"}
        except Exception:
            return False

    def channel_surf_next(self):
        """Move selection down by one; auto-play only if playable (not folders)."""
        cl = self.content_list
        count = cl.topLevelItemCount()
        if count == 0:
            return
        current = cl.currentItem()
        idx = cl.indexOfTopLevelItem(current) if current else -1
        idx = (idx + 1) % count
        candidate = cl.topLevelItem(idx)
        if candidate is not None:
            cl.setCurrentItem(candidate)
            if self._is_playable_item(candidate):
                self.item_activated(candidate)

    def channel_surf_prev(self):
        """Move selection up by one; auto-play only if playable (not folders)."""
        cl = self.content_list
        count = cl.topLevelItemCount()
        if count == 0:
            return
        current = cl.currentItem()
        idx = cl.indexOfTopLevelItem(current) if current else 0
        idx = (idx - 1) % count
        candidate = cl.topLevelItem(idx)
        if candidate is not None:
            cl.setCurrentItem(candidate)
            if self._is_playable_item(candidate):
                self.item_activated(candidate)

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        try:
            if obj is self.content_list:
                if event.type() == QEvent.KeyPress:
                    # Honor Keyboard/Remote Mode for list as well
                    if bool(self.config_manager.keyboard_remote_mode):
                        if event.key() == Qt.Key_Up:
                            self.channel_surf_prev()
                            return True
                        elif event.key() == Qt.Key_Down:
                            self.channel_surf_next()
                            return True

                if event.type() == QEvent.MouseButtonPress:
                    try:
                        back_btn = Qt.MouseButton.BackButton
                    except Exception:
                        back_btn = getattr(Qt.MouseButton, "XButton1", None)
                    try:
                        fwd_btn = Qt.MouseButton.ForwardButton
                    except Exception:
                        fwd_btn = getattr(Qt.MouseButton, "XButton2", None)

                    if back_btn is not None and event.button() == back_btn:
                        self.go_back()
                        return True
                    if fwd_btn is not None and event.button() == fwd_btn:
                        self.go_forward()
                        return True
        except Exception:
            pass
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Config / Options
    # ------------------------------------------------------------------

    def save_config(self):
        self.config_manager.save_config()

    def save_provider(self):
        self.provider_manager.save_provider()

    def options_dialog(self):
        options = OptionsDialog(self)
        options.exec_()
        # Refresh provider combo in case providers were added/removed/renamed
        self.populate_provider_combo()

    # ------------------------------------------------------------------
    # Static utilities
    # ------------------------------------------------------------------

    @staticmethod
    def sanitize_url(url):
        # Keep it minimal and non-invasive; prior working behavior
        return (url or "").strip()

    @staticmethod
    def shorten_header(s):
        return s[:20] + "..." + s[-25:] if len(s) > 45 else s

    @staticmethod
    def get_item_type(item):
        data = item.data(0, Qt.UserRole)
        return data.get("type") if data else None

    @staticmethod
    def get_item_name(item, item_type):
        return item.text(1 if item_type == "channel" else 0)

    @staticmethod
    def get_logo_column(item_type):
        return 0 if item_type == "m3ucontent" else 1
