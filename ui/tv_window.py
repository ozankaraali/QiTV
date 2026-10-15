"""The single TV window: the native video surface, the glued overlays and the menus (spec §5, §8).

Every key goes through ui.keymap to the TVController, so the open drawer receives typed text.
Single-key menu entries only show their key; modifier combinations are real shortcuts. Keys the
keymap resolves are claimed before any shortcut or native menu key equivalent
(ShortcutOverride), so nothing fires twice.
"""

from __future__ import annotations

import atexit
import logging
import os
import platform
import sys
from typing import Any, Callable, Optional

from PySide6.QtCore import QElapsedTimer, QEvent, QMargins, QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QGuiApplication,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QWindowStateChangeEvent,
)
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu

from options import OptionsDialog
from services import thread_cleanup
from services.catalog import CatalogService
from services.navigation import NavModel
from services.playback import NullSession, PlaybackSession, Session, create_instance
from services.thread_cleanup import has_pending_threads
from services.watch_state import WatchState
from services.zapping import ZapController
from ui.keymap import Action, resolve
from ui.logos import LogoCache
from ui.overlay_host import OverlayHost
from ui.overlays.channel_column import ChannelColumn
from ui.overlays.drawer import Drawer
from ui.overlays.toast import Toast
from ui.overlays.transport import Transport
from ui.overlays.zap_banner import ZapBanner
from ui.tv_controller import Overlays, TVController
from ui.video_surface import VideoSurface

logger = logging.getLogger(__name__)

WINDOW_NAME = "tv_window"
CLOSING_TEXT = "Closing after background requests finish…"
CLOSE_RETRY_MS = 50
CLOSE_THREAD_WAIT_MS = 30_000  # then quit anyway: a stuck worker must not keep QiTV open
CLOSING_NOTICE_AFTER = 10  # close retries (half a second) before the notice shows
CLOSING_NOTICE_MS = 60_000  # shown again only if it hides before the wait ends
EPG_REFRESH_MS = 2 * 60 * 60 * 1000
WATCH_SAVE_DELAY_MS = 2000  # a play is saved once the zapping settles (and on close)
MINI_WIDTH_FRACTION = 0.25
MINI_MARGIN = 10
FULLSCREEN_EXIT_MS = 1500  # the longest wait for the platform to finish leaving fullscreen
_SHORTCUT_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.MetaModifier
)
# The Qt platform whose window ids libVLC can draw into (NSView, HWND, X11 window).
_NATIVE_PLATFORMS = {"Darwin": "cocoa", "Windows": "windows", "Linux": "xcb"}
_MOUSE_EVENTS = (
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseMove,
    QEvent.Type.MouseButtonRelease,
)


def _native_video_handles() -> bool:
    """False on Qt platforms without real window ids (offscreen, Wayland): libVLC would crash."""
    return QGuiApplication.platformName() == _NATIVE_PLATFORMS.get(platform.system())


def _pending_thread_names() -> list[str]:
    """The workers of the QThreads still running, for the log (e.g. ["EpgLoadWorker"])."""
    names = []
    for cleanup in list(getattr(thread_cleanup, "_pending", ())):
        job = getattr(cleanup, "_worker", None) or getattr(cleanup, "_thread", None)
        names.append(type(job).__name__)
    return sorted(names)


def _exit_now() -> None:
    """Leave without tearing the interpreter down.

    Registered last with atexit, so it runs after Python's own threads (the provider cache
    writer) are joined: destroying a QThread that still runs makes Qt abort the process.
    """
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def fit_to_area(rect: QRect, margins: QMargins, area: QRect) -> QRect:
    """A window's client rect, moved and if need be shrunk so its frame lies inside `area`.

    The frame is `rect` grown by `margins` (the title bar and borders).
    """
    left, top, right, bottom = margins.left(), margins.top(), margins.right(), margins.bottom()
    width = min(rect.width() + left + right, area.width())
    height = min(rect.height() + top + bottom, area.height())
    x = min(max(rect.x() - left, area.x()), area.x() + area.width() - width)
    y = min(max(rect.y() - top, area.y()), area.y() + area.height() - height)
    return QRect(x + left, y + top, width - left - right, height - top - bottom)


def frame_margins(window: QMainWindow) -> QMargins:
    geometry, frame = window.geometry(), window.frameGeometry()
    return QMargins(
        geometry.left() - frame.left(),
        geometry.top() - frame.top(),
        frame.right() - geometry.right(),
        frame.bottom() - geometry.bottom(),
    )


class _FullscreenExitWatch(QObject):
    """Calls back when the platform reports that it has finished leaving fullscreen."""

    def __init__(self, window: QMainWindow, callback: Callable[[], None]) -> None:
        super().__init__(window)
        self._window = window
        self._callback = callback

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.WindowStateChange
            and event.spontaneous()
            and not self._window.isFullScreen()
        ):
            QTimer.singleShot(0, self._window, self._callback)
        return False


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
        self._closed = False
        self._threads_abandoned = False
        self._thread_wait = QElapsedTimer()
        # The close advances only on this timer: restarted, never stacked.
        self.close_retry = QTimer(self, singleShot=True, interval=CLOSE_RETRY_MS)
        self.close_retry.timeout.connect(self._close_step)
        self._stepping = False
        self._bound_handle: Optional[int] = None
        # (normal geometry, window flags, maximized) to restore when the mini player ends
        self._mini_restore: Optional[tuple[QRect, Qt.WindowType, bool]] = None
        self._mini_waiting = False  # for the fullscreen exit before entering the mini player
        # Leaving fullscreen: the normal geometry to return to, fitted to the screen.
        self._fullscreen_restore: Optional[QRect] = None
        self._exit_watch = _FullscreenExitWatch(self, self._fit_to_screen)
        self._fit_fallback = QTimer(self, singleShot=True, interval=FULLSCREEN_EXIT_MS)
        self._fit_fallback.timeout.connect(self._fit_to_screen)
        self._drag: Optional[tuple[QPoint, QPoint]] = None  # (press, window position)
        self._dragged = False  # the last press on the video moved the mini player
        self._pressed = False  # the left button is down on the video
        self._click_on_release = False  # a click that came while the button was still down

        self.setWindowTitle("QiTV")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.surface = VideoSurface(self)
        self.setCentralWidget(self.surface)
        self.surface.installEventFilter(self)
        self.host = OverlayHost(self, self.surface)
        self.logos = LogoCache(image_manager, config_manager, self)

        self.catalog = CatalogService(config_manager, provider_manager, epg_manager, parent=self)
        self.nav = NavModel(self.catalog, config_manager, self)
        self.zap = ZapController(parent=self)
        self._instance = vlc_instance if vlc_instance is not None else create_instance()
        self._owns_instance = vlc_instance is None and self._instance is not None
        self.session: Session
        if self._instance is None:
            logger.warning("libVLC is unavailable: channels cannot play")
            self.session = NullSession(self)
        else:
            self.session = PlaybackSession(self._instance, self)
        self.watch = WatchState(config_manager, save_delay_ms=WATCH_SAVE_DELAY_MS)

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
        for overlay in (
            self.overlays.banner,
            self.overlays.column,
            self.overlays.drawer,
            self.overlays.transport,
            self.overlays.toast,
        ):
            host.add_overlay(overlay)
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
            source.clicked.connect(self._on_video_clicked)
            source.doubleClicked.connect(controller.video_double_clicked)
            source.mouseMoved.connect(controller.mouse_moved)
            source.wheelSteps.connect(controller.wheel)
            source.backPressed.connect(controller.mouse_back)
            source.forwardPressed.connect(controller.mouse_forward)
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

    # ---- input --------------------------------------------------------------------------------

    def _resolve(self, event: QKeyEvent):
        return resolve(
            event.key(),
            event.modifiers(),
            event.text(),
            drawer_open=self.overlays.drawer.isVisible(),
        )

    def event(self, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.ShortcutOverride
            and isinstance(event, QKeyEvent)
            and not self._closing
            and self._resolve(event) is not None
        ):
            event.accept()  # deliver it as a key press: keyPressEvent handles it once
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        resolved = None if self._closing else self._resolve(event)
        if resolved is None:
            super().keyPressEvent(event)
            return
        action, payload = resolved
        self.controller.handle(action, payload)
        event.accept()

    def _on_video_clicked(self) -> None:
        if self._dragged:  # the press moved the mini player
            self._dragged = False
            return
        if self._pressed and self._mini_restore is not None:
            # Still held: a click only if it is released without dragging the mini player.
            self._click_on_release = True
            return
        self.controller.video_clicked()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.surface
            and event.type() in _MOUSE_EVENTS
            and isinstance(event, QMouseEvent)
        ):
            self._on_video_mouse(event)
        elif (
            self._mini_waiting
            and watched is self.windowHandle()
            and event.type() == QEvent.Type.WindowStateChange
            and event.spontaneous()
            and not self.isFullScreen()
        ):
            # The platform reports the end of the fullscreen exit (after its animation).
            QTimer.singleShot(0, self, self._enter_mini_after_fullscreen)
        return super().eventFilter(watched, event)

    def _on_video_mouse(self, event: QMouseEvent) -> None:
        """Track the left button on the video; in the mini player a drag moves the window."""
        kind, position = event.type(), event.globalPosition().toPoint()
        left = Qt.MouseButton.LeftButton
        if kind == QEvent.Type.MouseButtonPress and event.button() == left:
            self._pressed, self._dragged, self._click_on_release = True, False, False
            if self._mini_restore is not None:
                self._drag = (position, self.pos())
        elif kind == QEvent.Type.MouseMove and self._drag is not None:
            if not event.buttons() & left:
                self._drag = None
                return
            start, origin = self._drag
            delta = position - start
            if self._dragged or delta.manhattanLength() >= QApplication.startDragDistance():
                self._dragged = True
                self._click_on_release = False  # a drag, not a click
                self.move(origin + delta)
        elif kind == QEvent.Type.MouseButtonRelease and event.button() == left:
            self._pressed, self._drag = False, None
            if self._click_on_release:
                self._click_on_release = False
                self.controller.video_clicked()

    # ---- window actions (the controller's window_actions) -------------------------------------

    def toggle_fullscreen(self) -> None:
        if self._closing:
            return
        if self._mini_restore is not None:
            self._leave_mini()
        if self.isFullScreen():
            self._fullscreen_restore = self.normalGeometry()
        self.setWindowState(self.windowState() ^ Qt.WindowState.WindowFullScreen)

    def leave_fullscreen(self) -> None:
        if self.isFullScreen() and not self._closing:
            self._fullscreen_restore = self.normalGeometry()
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowFullScreen)

    def toggle_mini_player(self) -> None:
        """Frameless and on top, a quarter of the screen wide in the bottom-right corner."""
        if self._closing or self._mini_waiting:
            return
        if self._mini_restore is not None:
            self._leave_mini()
        elif self.isFullScreen():
            # Enter once the fullscreen exit has finished (an animation on macOS): the flags
            # and the corner geometry need a normal window and the normal screen area.
            self._mini_waiting = True
            self.leave_fullscreen()
            handle = self.windowHandle()
            if handle is not None:
                handle.installEventFilter(self)  # after the synchronous state change
            QTimer.singleShot(FULLSCREEN_EXIT_MS, self, self._enter_mini_after_fullscreen)
        else:
            self._enter_mini()

    def _enter_mini_after_fullscreen(self) -> None:
        if not self._mini_waiting:
            return
        self._mini_waiting = False
        handle = self.windowHandle()
        if handle is not None:
            handle.removeEventFilter(self)
        if not self._closing and not self.isFullScreen():
            self._enter_mini()

    def _enter_mini(self) -> None:
        maximized = self.isMaximized()
        restore = self.normalGeometry() if maximized else self.geometry()
        self._mini_restore = (restore, self.windowFlags(), maximized)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowState(Qt.WindowState.WindowNoState)
        self.setGeometry(self._mini_rect())
        self._show_again()
        if maximized:
            # macOS applies the end of the zoomed state on show, over the geometry set above.
            self.setGeometry(self._mini_rect())

    def _leave_mini(self) -> None:
        if self._mini_restore is None:
            return
        geometry, flags, maximized = self._mini_restore
        self._mini_restore = None
        self._drag = None
        self._dragged = self._click_on_release = False  # never swallow the next click
        self.setWindowFlags(flags)
        self.setGeometry(geometry)
        if maximized:
            self.setWindowState(Qt.WindowState.WindowMaximized)
        self._show_again()

    def _show_again(self) -> None:
        """setWindowFlags hides the window: show it again and keep the video bound."""
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

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self._update_menu_bar()
            if (
                isinstance(event, QWindowStateChangeEvent)
                and event.oldState() & Qt.WindowState.WindowFullScreen
                and not self.isFullScreen()
            ):
                self._after_fullscreen_exit(event.spontaneous())
        super().changeEvent(event)

    def _after_fullscreen_exit(self, finished: bool) -> None:
        """Fit the window once the platform has restored it (macOS animates the exit)."""
        if finished:  # the platform left fullscreen itself and reports it done
            QTimer.singleShot(0, self, self._fit_to_screen)
            return
        handle = self.windowHandle()
        if handle is not None:
            handle.installEventFilter(self._exit_watch)
        self._fit_fallback.start()

    def _fit_to_screen(self) -> None:
        """Return to the geometry from before fullscreen, inside the screen's work area.

        macOS restores the frame from before fullscreen even when it is wider than the
        screen (on macOS 26 even the size first asked for), leaving part of the window off
        screen: on a 1024×768 display it came back at x = -256.
        """
        self._fit_fallback.stop()
        handle = self.windowHandle()
        if handle is not None:
            handle.removeEventFilter(self._exit_watch)
        target, self._fullscreen_restore = self._fullscreen_restore, None
        unfit_states = (
            Qt.WindowState.WindowFullScreen
            | Qt.WindowState.WindowMaximized
            | Qt.WindowState.WindowMinimized
        )
        if (
            self._closing
            or self._mini_waiting
            or self._mini_restore is not None
            or self.windowState() & unfit_states
        ):
            return
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        geometry = self.geometry()
        fitted = fit_to_area(
            target if target is not None else geometry,
            frame_margins(self),
            screen.availableGeometry(),
        )
        if fitted != geometry:
            self.setGeometry(fitted)

    # ---- OptionsDialog host -------------------------------------------------------------------

    def options_dialog(self) -> None:
        if not self._closing:
            OptionsDialog(self).exec()

    def save_config(self) -> None:
        self.config_manager.save_config()

    def set_provider(self, force_update: bool = False) -> None:
        self.controller.select_provider(force_refresh=force_update)

    def refresh_channels(self) -> None:
        self.controller.refresh_overlays()

    def refresh_content_list_size(self) -> None:
        """Rows size themselves to the video area; nothing to do."""

    def reload_epg(self) -> None:
        """The EPG settings changed: reload the guide in the background (it repaints when ready)."""
        self.catalog.refresh_epg(force=True)

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
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.controller.start()
        self.epg_refresh.start()

    def _bind_video(self) -> None:
        """Hand libVLC the surface's native window; again only if its id changed.

        On macOS a window flag change (the mini player) keeps the same view.
        """
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
        """Close in steps, each after the event loop ran again (`close_retry`).

        1. Stop: the catalog starts no more jobs, playback stops, the geometry is saved.
        2. Wait for running QThreads (never block the GUI), with a notice after half a second,
           for at most CLOSE_THREAD_WAIT_MS.
        3. Save the image and EPG indexes and release libVLC. On macOS libVLC removes its
           video view on the run loop after a stop, and closing before that crashes.
        4. Close.

        Another close() meanwhile (Quit again) changes nothing; the retry timer finishes it.
        """
        if self._closed:
            event.accept()
            return
        if self._closing and not self._stepping:
            event.ignore()
            return
        if not self._closing:
            self._closing = True
            self._mini_waiting = False
            self._shut_down()
            self._thread_wait.start()
            self._close_later(event)
            return
        if has_pending_threads() and not self._threads_abandoned:
            if self._thread_wait.elapsed() < CLOSE_THREAD_WAIT_MS:
                self._close_retries += 1
                toast = self.overlays.toast
                if self._close_retries >= CLOSING_NOTICE_AFTER and not toast.isVisible():
                    toast.show_message(CLOSING_TEXT, "info", CLOSING_NOTICE_MS)
                self._close_later(event)
                return
            self._abandon_threads()
        if not self._video_released:
            self._video_released = True
            self._run_steps(
                ("save the image cache index", self.image_manager.save_index),
                ("save the EPG index", self.epg_manager.save_index),
                ("release the video", self._release_video),
            )
            self._close_later(event)
            return
        self._closed = True
        event.accept()
        self.app.quit()

    def _close_later(self, event: QCloseEvent) -> None:
        event.ignore()
        self.close_retry.start()

    def _close_step(self) -> None:
        self._stepping = True
        try:
            self.close()
        finally:
            self._stepping = False

    def _abandon_threads(self) -> None:
        self._threads_abandoned = True
        logger.warning(
            "Quitting while background work still runs after %d s: %s",
            CLOSE_THREAD_WAIT_MS // 1000,
            ", ".join(_pending_thread_names()) or "unknown",
        )
        atexit.register(_exit_now)

    def _shut_down(self) -> None:
        self.epg_refresh.stop()
        self._run_steps(
            ("stop the catalog", self.catalog.shutdown),
            ("stop the controller", self.controller.shutdown),
            ("stop loading logos", self.logos.shutdown),
            ("save the window geometry", self._save_geometry),
        )

    @staticmethod
    def _run_steps(*steps: tuple[str, Callable[[], None]]) -> None:
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
