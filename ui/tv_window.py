"""The single TV window: the native video surface, the glued overlays and the menus (spec §5, §8).

Every key goes through ui.keymap to the TVController, so the open drawer receives typed text.
Single-key menu entries only show their key; modifier combinations are real shortcuts. Keys the
keymap resolves are claimed before any shortcut or native menu key equivalent
(ShortcutOverride), so nothing fires twice.
"""

from __future__ import annotations

import logging
import platform
from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QKeyEvent, QKeySequence
from PySide6.QtWidgets import QMainWindow, QMenu

from options import OptionsDialog
from services.catalog import CatalogService
from services.navigation import NavModel
from services.playback import PlaybackSession, PlaybackState, create_instance
from services.thread_cleanup import has_pending_threads
from services.watch_state import WatchState
from services.zapping import ZapController
from ui.keymap import Action, resolve
from ui.logos import LogoCache
from ui.macos_window import attach_child_window
from ui.overlay_host import OverlayHost
from ui.overlays.channel_column import ChannelColumn
from ui.overlays.drawer import Drawer
from ui.overlays.toast import Toast
from ui.overlays.transport import Transport
from ui.overlays.zap_banner import ZapBanner
from ui.tv_controller import INFO_MS, Overlays, TVController
from ui.video_surface import VideoSurface

logger = logging.getLogger(__name__)

WINDOW_NAME = "tv_window"
NO_VIDEO_TEXT = "Video needs VLC: install it from videolan.org and restart QiTV"
CLOSING_TEXT = "Closing after background requests finish…"
CLOSE_RETRY_MS = 50
CLOSING_NOTICE_AFTER = 10  # close retries (half a second) before telling the user why
EPG_REFRESH_MS = 2 * 60 * 60 * 1000
MINI_WIDTH_FRACTION = 0.25
MINI_MARGIN = 10
_SHORTCUT_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.MetaModifier
)
# The Qt platform whose window ids libVLC can draw into (NSView, HWND, X11 window).
_NATIVE_PLATFORMS = {"Darwin": "cocoa", "Windows": "windows", "Linux": "xcb"}


def _native_video_handles() -> bool:
    """False on Qt platforms without real window ids (offscreen, Wayland): libVLC would crash."""
    return QGuiApplication.platformName() == _NATIVE_PLATFORMS.get(platform.system())


class _NoVideo(QObject):
    """Stands in for PlaybackSession when libVLC is missing: every play explains why."""

    stateChanged = Signal(str)
    positionChanged = Signal(int, int)
    errorOccurred = Signal(str)
    ended = Signal()
    seekableChanged = Signal(bool)

    url: Optional[str] = None
    kind = None

    def play(self, url: str, **_options: Any) -> None:
        self.errorOccurred.emit(NO_VIDEO_TEXT)

    def state(self) -> PlaybackState:
        return PlaybackState.IDLE

    def volume(self) -> int:
        return 0

    def toggle_mute(self) -> bool:
        return False

    def is_seekable(self) -> bool:
        return False

    def time_ms(self) -> int:
        return 0

    def length_ms(self) -> int:
        return 0

    def audio_tracks(self) -> list[tuple[int, str]]:
        return []

    def subtitle_tracks(self) -> list[tuple[int, str]]:
        return []

    def _ignore(self, *_args: Any) -> None:
        pass

    set_window = stop = toggle_pause = set_paused = seek = seek_relative = _ignore
    set_rate = set_volume = set_audio_track = set_subtitle_track = release = _ignore


class _Geometry:
    """What ConfigManager.save_window_settings reads: a geometry to store."""

    def __init__(self, rect: QRect) -> None:
        self._rect = rect

    def geometry(self) -> QRect:
        return self._rect


class TVWindow(QMainWindow):
    def __init__(
        self,
        app,
        config_manager,
        provider_manager,
        image_manager,
        epg_manager,
        *,
        vlc_instance=None,
    ):
        super().__init__()
        self.app = app
        # OptionsDialog reads these from its parent.
        self.config_manager = config_manager
        self.provider_manager = provider_manager
        self.image_manager = image_manager
        self.epg_manager = epg_manager
        self._started = False
        self._closing = False
        self._close_retries = 0
        self._video_released = False
        self._bound_handle: Optional[int] = None
        self._mini_restore: Optional[tuple[QRect, Qt.WindowType]] = None

        self.setWindowTitle("QiTV")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.surface = VideoSurface(self)
        self.setCentralWidget(self.surface)
        self.host = OverlayHost(self, self.surface)
        self.host.installEventFilter(self)
        self.surface.installEventFilter(self)
        self.logos = LogoCache(image_manager, config_manager, self)

        self.catalog = CatalogService(config_manager, provider_manager, epg_manager, parent=self)
        self.nav = NavModel(self.catalog, config_manager, self)
        self.zap = ZapController(parent=self)
        self._instance = vlc_instance if vlc_instance is not None else create_instance()
        self._owns_instance = vlc_instance is None and self._instance is not None
        if self._instance is None:
            logger.warning("libVLC is unavailable: channels cannot play")
            self.session: Any = _NoVideo(self)
        else:
            self.session = PlaybackSession(self._instance, self)
        self.watch = WatchState(config_manager)

        # The drawer and column ask the controller for "now" titles, and the controller
        # needs the overlays: a closure breaks the cycle.
        def now_provider(channel: dict):
            return controller.now_title(channel)

        host = self.host
        self.overlays = Overlays(
            banner=ZapBanner(host),
            column=ChannelColumn(self.logos, now_provider, parent=host),
            drawer=Drawer(self.nav, self.logos, now_provider, parent=host),
            transport=Transport(host),
            toast=Toast(host),
        )
        self._raised_overlays = (
            self.overlays.banner,
            self.overlays.column,
            self.overlays.drawer,
            self.overlays.transport,
        )
        for overlay in self._raised_overlays:
            host.add_overlay(overlay)
            overlay.installEventFilter(self)  # each raises itself on show: the toast follows
        host.add_overlay(self.overlays.toast)
        controller = TVController(
            config=config_manager,
            catalog=self.catalog,
            nav=self.nav,
            zap=self.zap,
            session=self.session,
            watch=self.watch,
            overlays=self.overlays,
            window_actions=self,
            logos=self.logos,
            parent=self,
        )
        self.controller = controller
        for source in (self.surface, self.host):
            source.clicked.connect(controller.video_clicked)
            source.doubleClicked.connect(controller.video_double_clicked)
            source.mouseMoved.connect(controller.mouse_moved)
            source.wheelSteps.connect(controller.wheel)
        self.logos.logoReady.connect(self._on_logo_ready)
        self.nav.notice.connect(self._on_notice)
        self.epg_refresh = QTimer(self, interval=EPG_REFRESH_MS)  # the guide keeps moving
        self.epg_refresh.timeout.connect(lambda: self.catalog.refresh_epg())

        self._build_menus()
        config_manager.apply_window_settings(WINDOW_NAME, self)

    # ---- menus --------------------------------------------------------------------------------

    def _build_menus(self) -> None:
        bar = self.menuBar()
        app_menu = bar.addMenu("QiTV")
        self._add_action(
            app_menu, "Settings…", "Ctrl+,", self.options_dialog, QAction.MenuRole.PreferencesRole
        )
        self._add_action(app_menu, "Quit", "Ctrl+Q", self.close, QAction.MenuRole.QuitRole)
        view = bar.addMenu("View")
        self._add_action(view, "Fullscreen", "F", self._handler(Action.FULLSCREEN))
        self._add_action(view, "Channel list", "Return", self._handler(Action.OPEN_DRAWER))
        self._add_action(view, "Mini player", "Alt+P", self._handler(Action.MINI_PLAYER))
        playback = bar.addMenu("Playback")
        self._add_action(playback, "Play/Pause", "Space", self._handler(Action.PLAY_PAUSE))
        self._add_action(playback, "Mute", "M", self._handler(Action.MUTE))
        self._add_action(playback, "Previous channel", "Backspace", self._handler(Action.PREVIOUS))

    def _add_action(
        self,
        menu: QMenu,
        text: str,
        shortcut: str,
        slot: Callable[[], Any],
        role: QAction.MenuRole = QAction.MenuRole.NoRole,
    ) -> QAction:
        action = QAction(text, self)
        sequence = QKeySequence(shortcut)
        action.setShortcut(sequence)
        action.setMenuRole(role)
        action.triggered.connect(lambda _checked=False: slot())
        menu.addAction(action)
        first = sequence[0]  # type: ignore[index]  # QKeyCombination; missing from the stubs
        if first.keyboardModifiers() & _SHORTCUT_MODIFIERS:
            action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            self.addAction(action)  # stays active while a non-native menu bar is hidden
        else:
            # A hint only: active solely inside the open menu, so the key reaches
            # keyPressEvent (the drawer types "f", "m" and " ", and Return activates).
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        return action

    def _handler(self, action: Action) -> Callable[[], None]:
        return lambda: self.controller.handle(action)

    # ---- keys ---------------------------------------------------------------------------------

    def _resolve(self, event: QKeyEvent):
        return resolve(
            event.key(),
            event.modifiers(),
            event.text(),
            drawer_open=self.overlays.drawer.isVisible(),
        )

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ShortcutOverride and isinstance(event, QKeyEvent):
            if self._resolve(event) is not None:
                event.accept()  # deliver it as a key press: keyPressEvent handles it once
                return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        resolved = self._resolve(event)
        if resolved is None or self._closing:
            super().keyPressEvent(event)
            return
        action, payload = resolved
        self.controller.handle(action, payload)
        event.accept()

    def _on_notice(self, text: str) -> None:
        self.overlays.toast.show_message(text, "info", INFO_MS)

    def _on_logo_ready(self, url: str) -> None:
        """Repaint the banner when its channel's logo arrives (the rows refresh themselves)."""
        banner = self.overlays.banner
        info = banner.info
        if not banner.isVisible() or info is None or info.logo is not None:
            return
        channel = next(
            (
                c
                for c in self.zap.context.channels  # a tuned channel is always in the context
                if c.get("logo") == url and str(c.get("name") or "") == info.name
            ),
            None,
        )
        if channel is not None:
            banner.show_info(self.controller.banner_info(channel))

    # ---- window actions (the controller's window_actions) -------------------------------------

    def toggle_fullscreen(self) -> None:
        if self._mini_restore is not None:
            self.toggle_mini_player()
        self.setWindowState(self.windowState() ^ Qt.WindowState.WindowFullScreen)

    def leave_fullscreen(self) -> None:
        if self.isFullScreen():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowFullScreen)

    def toggle_mini_player(self) -> None:
        """Frameless and on top, a quarter of the screen wide in the bottom-right corner."""
        if self._mini_restore is None:
            restore = self._normal_geometry()
            self.leave_fullscreen()
            self._mini_restore = (restore, self.windowFlags())
            self.setWindowFlags(
                self.windowFlags()
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
            )
            self.setGeometry(self._mini_rect())
        else:
            geometry, flags = self._mini_restore
            self._mini_restore = None
            self.setWindowFlags(flags)
            self.setGeometry(geometry)
        self._update_menu_bar()
        self.show()
        self._bind_video()

    def _mini_rect(self) -> QRect:
        screen = self.screen() or QGuiApplication.primaryScreen()
        available = screen.availableGeometry()
        width = round(screen.geometry().width() * MINI_WIDTH_FRACTION)
        height = round(width * 9 / 16)
        x = available.x() + available.width() - width - MINI_MARGIN
        y = available.y() + available.height() - height - MINI_MARGIN
        return QRect(x, y, width, height)

    def _normal_geometry(self) -> QRect:
        if self._mini_restore is not None:
            return self._mini_restore[0]
        if self.isFullScreen() or self.isMaximized():
            return self.normalGeometry()
        return self.geometry()

    def _update_menu_bar(self) -> None:
        bar = self.menuBar()
        if not bar.isNativeMenuBar():  # the macOS menu bar is outside the window
            bar.setVisible(not self.isFullScreen() and self._mini_restore is None)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.host and event.type() == QEvent.Type.Show:
            # macOS drops a child window from its parent when the child is ordered out, and
            # the host only attaches once: attach again after every show.
            QTimer.singleShot(0, self, self._attach_host)
        elif watched is self.surface and event.type() == QEvent.Type.Resize:
            self._size_hidden_host()
        elif event.type() == QEvent.Type.ZOrderChange and watched in self._raised_overlays:
            self.overlays.toast.raise_()  # messages stay readable above the drawer and bars
        return super().eventFilter(watched, event)

    def _size_hidden_host(self) -> None:
        """Keep a hidden host at the surface's size.

        Toasts and the banner lay themselves out from the host's size when they show; the
        host follows the surface only while visible, so its first overlay would be laid
        out for a stale size.
        """
        if not self.host.isVisible():
            self.host.sync_geometry()

    def _attach_host(self) -> None:
        if self.host.isVisible():
            attach_child_window(self, self.host)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self._update_menu_bar()
        super().changeEvent(event)

    # ---- OptionsDialog host -------------------------------------------------------------------

    def options_dialog(self) -> None:
        OptionsDialog(self).exec()

    def save_config(self) -> None:
        self.config_manager.save_config()

    def set_provider(self, force_update: bool = False) -> None:
        self.controller.select_provider(force_refresh=force_update)

    def refresh_channels(self) -> None:
        self.controller.refresh_overlays()

    def refresh_content_list_size(self) -> None:
        """Rows size themselves to the video area; nothing to do."""

    # ---- lifecycle ----------------------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._started:
            self._started = True
            # After the native window exists and is on screen.
            QTimer.singleShot(0, self, self._start)

    def _start(self) -> None:
        if self._closing:
            return
        self._bind_video()
        self._size_hidden_host()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.controller.start()
        self.epg_refresh.start()

    def _bind_video(self) -> None:
        """Hand libVLC the surface's native window (again, if a flag change recreated it)."""
        if not _native_video_handles():
            if self._bound_handle is None:
                self._bound_handle = 0
                logger.info(
                    "Qt platform %r has no native window for VLC; video is not embedded",
                    QGuiApplication.platformName(),
                )
            return
        handle = self.surface.native_handle()
        if handle != self._bound_handle:
            self._bound_handle = handle
            self.session.set_window(handle)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Close in steps, each after the event loop ran again.

        libVLC removes its video view on the main run loop after a stop (macOS), and closing
        the window before that crashes; running QThreads must finish without blocking the GUI.
        """
        if not self._closing:
            self._closing = True
            self._shut_down()  # stops playback
            self._close_later(event)
            return
        if has_pending_threads():
            self._close_retries += 1
            if self._close_retries == CLOSING_NOTICE_AFTER:
                self.overlays.toast.show_message(CLOSING_TEXT, "info", 60_000)
            self._close_later(event)
            return
        if not self._video_released:
            self._video_released = True
            self._release_video()
            self._close_later(event)
            return
        event.accept()
        self.app.quit()

    def _close_later(self, event: QCloseEvent) -> None:
        event.ignore()
        QTimer.singleShot(CLOSE_RETRY_MS, self, self.close)

    def _shut_down(self) -> None:
        self.epg_refresh.stop()
        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            ("stop the controller", self.controller.shutdown),
            ("stop loading logos", self.logos.shutdown),
            ("save the image cache index", self.image_manager.save_index),
            ("save the EPG index", self.epg_manager.save_index),
            ("save the window geometry", self._save_geometry),
        )
        for what, step in steps:
            try:
                step()
            except Exception:
                logger.exception("Could not %s", what)

    def _save_geometry(self) -> None:
        self.config_manager.save_window_settings(_Geometry(self._normal_geometry()), WINDOW_NAME)

    def _release_video(self) -> None:
        self.session.release()
        if self._owns_instance and self._instance is not None:
            instance, self._instance = self._instance, None
            instance.release()
