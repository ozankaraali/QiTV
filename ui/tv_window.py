"""The single TV window: the player and Library pages, the overlays and the menus (spec §5, §8).

Two pages share the window (a PageStack, ui/page_stack.py): the player (the native video
surfaces, with the overlay host glued over them) and the Library (Movies and Series, spec
§5.4); with the Library open, what plays goes on in a corner of it. The player has
two surfaces, each with its own player, live session and tuner (spec §7): the PiP
frame (ui/pip.py) fills the page with the large one and puts the other in a corner while
picture in picture is open; the SessionManager says which is large. The moves between
them are ui/tv_pages.py's, the menu bar ui/tv_menus.py's; this window lays them out, owns the
services, routes the keys, and handles fullscreen, the mini player and closing.

Every key goes through ui.keymap: on the Library page to the LibraryController, else to the
TVController, so the open drawer receives typed text. Single-key menu entries only show their
key; modifier combinations are real shortcuts. Keys the keymap resolves are claimed before any
shortcut or native menu key equivalent (ShortcutOverride), so nothing fires twice.

A subtitle file dropped on the window (the video, its overlays or the Library with its corner
player) loads with what plays (2.2; the controller says whether it would, while it is dragged).
A dropped playlist file (.m3u, .m3u8) or provider link becomes a provider (ui/provider_drop.py);
one drop may carry both kinds.
"""

from __future__ import annotations

import atexit
from datetime import datetime
import logging
import multiprocessing
import os
import platform
import sys
import time
from typing import Callable, Optional

from PySide6.QtCore import (
    QElapsedTimer,
    QEvent,
    QMargins,
    QObject,
    QPoint,
    QRect,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
    QDesktopServices,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPalette,
    QWindowStateChangeEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QWidget,
)

from services import favorites, free_channels, thread_cleanup
from services.account_check import AccountChecks
from services.browsed_catalog import BrowsedCatalog
from services.catalogs import Catalogs
from services.catchup_memory import FILE_NAME as CATCHUP_FILE, CatchupMemory
from services.connection_budget import ConnectionBudget
from services.find import Preferences, preferences_for
from services.i18n import N_, tr, translate
from services.jobs import JobRunner
from services.lazy_import import deferred_modules, load_module
from services.live.engine import LiveEngine
from services.live.live_session import LiveSession
from services.live.tuning import FILE_NAME as TUNING_FILE, LiveTuning
from services.navigation import NavModel
from services.picture import FILE_NAME as PICTURE_FILE, ChannelPictures
from services.playback import (
    NullSession,
    PlaybackSession,
    PlaybackState,
    Session,
    create_instance,
    default_args,
)
from services.provider_link import ProviderLink
from services.redact import safe_message, safe_traceback
from services.sessions import SessionManager, Slot
from services.sound_sync import FILE_NAME as SOUND_SYNC_FILE, SoundSync
from services.subtitle_picks import FILE_NAME as SUBTITLE_PICKS_FILE, SubtitlePicks
from services.thread_cleanup import has_pending_threads
from services.update_checker import call_before_update
from services.watch_state import WatchState
from services.zapping import ZapController
from ui import macos_window, provider_drop
from ui.bottom_band import BottomBand
from ui.desktop_pip import DesktopPip, corner_rect
from ui.export_flow import ExportFlow
from ui.find_session import FindSession
from ui.free_channels_menu import show_menu as show_free_channels_menu
from ui.grid_frame import GridFrame
from ui.keymap import Action, as_int, resolve
from ui.library.library_screen import LibraryScreen
from ui.live_tuner import LiveTuner, one_disk_ring, wait_for_each_other
from ui.logos import LogoCache, PosterCache
from ui.macos_window import reclaim_first_responder
from ui.overlay_host import OverlayHost
from ui.overlays.channel_column import ChannelColumn
from ui.overlays.corner_controls import CornerControls
from ui.overlays.drawer import Drawer
from ui.overlays.grid_tiles import GridTiles
from ui.overlays.guide import Guide
from ui.overlays.layout_row import LayoutRow
from ui.overlays.mixer import Mixer
from ui.overlays.next_card import NextCard
from ui.overlays.pip_label import PipLabel
from ui.overlays.sleep_notice import SleepNotice
from ui.overlays.stats import StatsOverlay
from ui.overlays.subtitle_search import SubtitleSearchPage
from ui.overlays.toast import Toast
from ui.overlays.track_panel import TrackPanel
from ui.overlays.transport import Transport
from ui.overlays.welcome import WelcomeCard
from ui.overlays.zap_banner import ZapBanner
from ui.page_stack import PageStack
from ui.pip import PipFrame
from ui.pointer import PointerHider
from ui.settings_dialog import OptionsDialog
from ui.subtitle_search import OnlineSetup
from ui.track_controls import SUBTITLE_FILTER
from ui.tv_controller import ERROR_MS, Overlays, TVController
from ui.tv_menus import TVMenus
from ui.tv_pages import TVPages
from ui.video_surface import VideoSurface

logger = logging.getLogger(__name__)

WINDOW_NAME = "tv_window"
TIMESHIFT_DIR = "timeshift"  # the live buffers' root, in the config directory
CLOSING_TEXT = N_("Closing after background requests finish…")  # i18n: a toast while QiTV quits
CLOSE_RETRY_MS = 50
CLOSE_THREAD_WAIT_MS = 30_000  # then quit anyway: a stuck worker must not keep QiTV open
CLOSING_NOTICE_AFTER = 10  # close retries (half a second) before the notice shows
CLOSING_NOTICE_MS = 60_000  # shown again only if it hides before the wait ends
EPG_REFRESH_MS = 2 * 60 * 60 * 1000
WATCH_SAVE_DELAY_MS = 2000  # a play is saved once the zapping settles (and on close)
MINI_WIDTH_FRACTION = 0.25
MINI_MARGIN = 10
FULLSCREEN_EXIT_MS = 1500  # the longest wait for the platform to finish leaving fullscreen
FULLSCREEN_SETTLE_MS = 5000  # the longest wait for macOS to report a fullscreen change
# config.json could not be written (locked by antivirus, read-only, a full disk).
SAVE_FAILED_TEXT = N_("Couldn't save the settings; changes will be lost when QiTV closes")
ABANDONED_EXIT_CODE = 3  # QiTV quit while background work still ran (see _exit_now)
# The Qt platform whose window ids libVLC can draw into (NSView, HWND, X11 window).
_NATIVE_PLATFORMS = {"Darwin": "cocoa", "Windows": "windows", "Linux": "xcb"}
_MOUSE_EVENTS = (
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseMove,
    QEvent.Type.MouseButtonRelease,
)
# A press or release on a video surface (the first responder: ui.macos_window).
_CLICK_EVENTS = (
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseButtonRelease,
)


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _macos_desktops() -> bool:
    """Only macOS has fullscreen desktops to leave (the desktop picture in picture)."""
    return QGuiApplication.platformName() == "cocoa"


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
    """Leave without tearing the interpreter down, with ABANDONED_EXIT_CODE.

    Registered with atexit only when the close gave up waiting for a QThread: destroying a
    QThread that still runs makes Qt abort the process. The interpreter joins Python's own
    non-daemon threads (the provider cache writer) before it runs any atexit handler, and
    atexit runs handlers last-in-first-out: this one, registered last, runs first, and the
    handlers registered before it never run. The exit code tells whoever started QiTV that
    background work was abandoned.
    """
    _end_child_processes()
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(ABANDONED_EXIT_CODE)


def _end_child_processes(timeout_s: float = 1.0) -> None:
    """Terminate multiprocessing's children (the time-shift helper), then kill stragglers.

    os._exit skips multiprocessing's own exit handler, which would end daemon children; the
    helper notices its owner's death too, but only after a while, with its connections open.
    """
    children = multiprocessing.active_children()
    for child in children:
        child.terminate()
    deadline = time.monotonic() + timeout_s
    for child in children:
        child.join(max(0.0, deadline - time.monotonic()))
        if child.is_alive():
            child.kill()


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


class _PlatformStateWatch(QObject):
    """Calls back on each window state change the platform reports (spontaneous)."""

    def __init__(self, window: QMainWindow, callback: Callable[[], None]) -> None:
        super().__init__(window)
        self._callback = callback

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowStateChange and event.spontaneous():
            self._callback()
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
        catalogs: Optional[Catalogs] = None,
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
        # Leaving fullscreen: the normal geometry to return to, fitted to the screen. The
        # second is remembered on entry, for an exit the window did not ask for (the green
        # button), after which the platform may restore another frame.
        self._fullscreen_restore: Optional[QRect] = None
        self._pre_fullscreen: Optional[QRect] = None
        self._exit_watch = _FullscreenExitWatch(self, self._fit_to_screen)
        # macOS animates a fullscreen change, and AppKit drops one asked for meanwhile: the
        # state asked for until the platform reports it, and a later request, applied then.
        self._fs_target: Optional[bool] = None
        self._fs_queued: Optional[bool] = None
        self._fs_watch = _PlatformStateWatch(self, self._fullscreen_settled)
        self._fs_settle = QTimer(self, singleShot=True, interval=FULLSCREEN_SETTLE_MS)
        self._fs_settle.timeout.connect(self._fullscreen_settled)
        self._fit_fallback = QTimer(self, singleShot=True, interval=FULLSCREEN_EXIT_MS)
        self._fit_fallback.timeout.connect(self._fit_to_screen)
        # Each other player's surface's native window, once bound (the PiP's, the tiles').
        self._bound: dict[str, int] = {}
        self._drag: Optional[tuple[QPoint, QPoint]] = None  # (press, window position)
        self._dragged = False  # the last press on the video moved the mini player
        self._pressed = False  # the left button is down on the video
        self._click_on_release = False  # a click that came while the button was still down

        self.setWindowTitle("QiTV")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)  # subtitle files, playlists or provider links dropped on it
        # The player page is the video container: the large surface fills it, and picture in
        # picture puts the other one in a corner (the PiP frame lays them out). The page paints
        # the black behind the video; the surfaces paint nothing (see ui/video_surface.py).
        self.player_page = QWidget()
        black = self.player_page.palette()
        black.setColor(QPalette.ColorRole.Window, QColor("#000000"))
        self.player_page.setPalette(black)
        self.player_page.setAutoFillBackground(True)
        self.surface = VideoSurface(self.player_page)
        self.pip_surface = VideoSurface(self.player_page)
        # Every player's surface by its slot's name; a multiview's tiles add theirs.
        self.surfaces: dict[str, VideoSurface] = {"main": self.surface, "pip": self.pip_surface}
        for surface in self.surfaces.values():
            surface.installEventFilter(self)  # before the PiP frame's: it sees them first
        # The first frame: then the libraries background jobs need load (services.lazy_import).
        self._first_frame = False
        self.player_page.installEventFilter(self)
        self.logos = LogoCache(image_manager, config_manager, self)
        self.posters = PosterCache(image_manager, config_manager, self)
        self.library_page = LibraryScreen(self.posters, self.logos)
        self.library_toast = Toast(self.library_page)  # the overlay host hides with the player
        # Both pages are always the stack's children: the player page is the whole picture, the
        # Library, or a small one in a corner over the Library while it plays on.
        self.pages = PageStack(self.player_page, self.library_page, config_manager, self)
        self.setCentralWidget(self.pages)
        # Glued to the page, not a surface: either surface may be the large one.
        self.host = OverlayHost(self, self.player_page)  # shown only while the page is
        self.host.setAcceptDrops(True)  # a window of its own: a drop on the overlays
        self.host.installEventFilter(self)

        # One catalog per provider behind the hub; each page browses its own provider's.
        # provider_manager stays the registry (the provider list Settings edits).
        self.catalogs = catalogs or Catalogs(
            config_manager, provider_manager, in_use=self._providers_in_use, parent=self
        )
        self.catalog = BrowsedCatalog(
            self.catalogs,
            config_manager,
            source_key="selected_provider_name",
            all_key="tv_all_providers",
            parent=self,
        )
        self.library_catalog = BrowsedCatalog(
            self.catalogs,
            config_manager,
            source_key="library_provider_name",
            all_key="library_all_providers",
            parent=self,
        )
        self.nav = NavModel(self.catalog, config_manager, self)
        self.zap = ZapController(parent=self)
        owns_instance = vlc_instance is None
        if owns_instance:  # the decoder Settings chose (software unless hardware)
            vlc_instance = create_instance(default_args(config_manager.hardware_decoding))
        self._instance = vlc_instance
        self._owns_instance = owns_instance and self._instance is not None
        self.session: Session
        # The time-shift pipeline (spec §6): only with libVLC, since nothing plays without it.
        # Picture in picture needs it too: a second player and live session (spec §7).
        self.engine: Optional[LiveEngine] = None
        self.live_session: Optional[LiveSession] = None
        self.pip_session: Optional[PlaybackSession] = None
        self.pip_live: Optional[LiveSession] = None
        self.live_tuning: Optional[LiveTuning] = None
        self.sessions: Optional[SessionManager] = None
        self.budget = ConnectionBudget(self)
        if self._instance is None:
            logger.warning("libVLC is unavailable: channels cannot play")
            self.session = NullSession(self)
        else:
            # With hardware decoding, a frozen picture sends its stream to software (2.1).
            hardware = config_manager.hardware_decoding
            self.session = PlaybackSession(self._instance, self, hardware=hardware)
            self.pip_session = PlaybackSession(self._instance, self, hardware=hardware)
            root = os.path.join(config_manager.get_config_dir(), TIMESHIFT_DIR)
            self.engine = LiveEngine(root, self)
            # What channels taught stay-live (Auto's target, a locked rate), kept for both.
            self.live_tuning = LiveTuning(
                os.path.join(config_manager.get_config_dir(), TUNING_FILE)
            )
            # Connected before the engine starts, so they hear a helper that never comes up.
            self.live_session = LiveSession(self.engine, self.session, tuning=self.live_tuning)
            self.pip_live = LiveSession(self.engine, self.pip_session, tuning=self.live_tuning)
            self.engine.start()
            self.sessions = SessionManager(
                self._slot("main", self.session, self.live_session),
                self._slot("pip", self.pip_session, self.pip_live),
                self.budget,
                parent=self,
            )
            self._join_tuners()
        self.watch = WatchState(config_manager, save_delay_ms=WATCH_SAVE_DELAY_MS)
        # Each channel's sound sync (J/K, the Sound panel), written off the GUI thread.
        self.sound_sync = SoundSync(os.path.join(config_manager.get_config_dir(), SOUND_SYNC_FILE))
        # Each channel's aspect ratio (the Picture panel, 2.3), alike.
        self.channel_pictures = ChannelPictures(
            os.path.join(config_manager.get_config_dir(), PICTURE_FILE)
        )
        # Whether each channel's provider archive answered (catch-up, 2.2).
        self.catchup_memory = CatchupMemory(
            os.path.join(config_manager.get_config_dir(), CATCHUP_FILE)
        )
        # The subtitles downloaded from OpenSubtitles for each title (2.2), and the folder.
        self.subtitle_picks = SubtitlePicks(
            os.path.join(config_manager.get_config_dir(), SUBTITLE_PICKS_FILE),
            config_manager.subtitles_dir(),
        )
        self.jobs = JobRunner(self)  # the Library's searches, the exports, OpenSubtitles

        # The drawer and column ask the controller for "now" titles, and the controller
        # needs the overlays: a closure breaks the cycle.
        def now_provider(channel: dict):
            return controller.now_title(channel)

        catalog = self.catalog

        def guide_programmes(channel: dict, start: datetime, limit: int):
            return catalog.programmes(channel, start, limit=limit)

        def archive_days(channel: dict) -> int:
            return controller.archive_days(channel)

        host = self.host
        self.grid_tiles = GridTiles(host)  # the multiview's labels, focus ring, "+" and cards
        self.pip_label = PipLabel(host)
        self.mixer = Mixer(host)
        self.sound_panel = TrackPanel(host)
        self.subtitle_panel = TrackPanel(host)
        self.picture_panel = TrackPanel(host)
        self.stats_overlay = StatsOverlay(host)
        self.sleep_notice = SleepNotice(host)
        self.layout_row = LayoutRow(host)  # the multiview's layouts, above its layout chip
        self.subtitle_search = SubtitleSearchPage(host)
        self.corner_controls = CornerControls(host)
        self.overlays = Overlays(
            banner=ZapBanner(host),
            column=ChannelColumn(self.logos, now_provider, parent=host),
            drawer=Drawer(self.nav, self.logos, now_provider, parent=host),
            transport=Transport(host),
            toast=Toast(host),
            guide=Guide(
                self.logos, guide_programmes, _local_now, parent=host, archive_days=archive_days
            ),
            next_card=NextCard(host),
            welcome=WelcomeCard(host),
            pip_label=self.pip_label,
            mixer=self.mixer,
            sound_panel=self.sound_panel,
            subtitle_panel=self.subtitle_panel,
            subtitle_search=self.subtitle_search,
            picture_panel=self.picture_panel,
            stats=self.stats_overlay,
            sleep_notice=self.sleep_notice,
            library_toast=self.library_toast,
            layout_row=self.layout_row,
        )
        # Bottom to top. The PiP label sits over the transport, whose bottom band covers a
        # bottom corner's PiP: its pill, ⇄, speaker and × stay within reach; elsewhere on the
        # PiP the transport keeps only its own buttons and bar, and other clicks reach the
        # small picture (OverlayHost.set_hole). The multiview's tiles are drawn under all of
        # them, and take their clicks over the transport but for its buttons and bar
        # (GridTiles.hits). The mixer shows over them (it sits above the transport's controls
        # and beside the PiP), and the Sound, Subtitles and Picture panels and the multiview's
        # layouts over it (above their buttons). The statistics sit over the transport, under
        # its title row (they take no click). The drawer and the guide cover them all; the
        # OpenSubtitles search covers the player with its backdrop, under the corner player's
        # controls, the sleep timer's last minute and the toast.
        for overlay in (
            self.grid_tiles,
            self.overlays.banner,
            self.overlays.column,
            self.overlays.transport,
            self.stats_overlay,
            self.grid_tiles.hits,
            self.pip_label,
            self.mixer,
            self.sound_panel,
            self.subtitle_panel,
            self.picture_panel,
            self.layout_row,
            self.overlays.drawer,
            self.overlays.guide,
            self.overlays.next_card,
            self.overlays.welcome,
            self.subtitle_search,
            self.corner_controls,
            self.sleep_notice,
            self.overlays.toast,
        ):
            host.add_overlay(overlay)
        # After the window's surface filters: the frame takes the small surface's left button
        # before the window (its mini-player drag) and the surface (the drawer) see it.
        self.pip_frame = PipFrame(self.player_page, self.surface, self.pip_surface, config_manager)
        # Desktop picture in picture (macOS): while the fullscreen window's desktop is not the
        # one shown, the large picture floats in a small window on every desktop.
        self.desktop_pip = DesktopPip(
            self,
            on_active_space=macos_window.on_active_space,
            may_float=self._may_float_picture,
            lend=self.pip_frame.lend_large,
            take_back=self.pip_frame.take_back,
            place=self._floating_rect,
            join_all_spaces=macos_window.join_all_spaces,
            remember=self._remember_floating_rect,
            parent=self,
        )
        # i18n: shown on the fullscreen player while its picture floats on another desktop
        self.floating_note = QLabel(tr("Playing in the small window"), self.player_page)
        self.floating_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.floating_note.setStyleSheet("color: rgba(255, 255, 255, 0.55); font-size: 18px;")
        self.floating_note.hide()
        self.desktop_pip.floatingChanged.connect(self._on_picture_floating)
        self.pip_frame.suspending.connect(self.desktop_pip.dock)  # the multiview lays them out
        # The host covers the page exactly: page coordinates are the host's.
        self.pip_frame.smallRectChanged.connect(self.pip_label.follow)
        self.pip_frame.smallRectChanged.connect(self.mixer.avoid)
        # The transport's shades leave the small picture alone (host and transport coordinates
        # differ only by the transport's offset in the host).
        self.pip_frame.smallRectChanged.connect(self.overlays.transport.follow_pip)
        # Clicks on the small picture reach it through the overlays (but their buttons).
        self.pip_frame.smallRectChanged.connect(self.host.set_hole)
        # While the transport, the mixer or the zap banner shows, a bottom corner's PiP rises
        # above the transport's controls or the banner's row; the mixer sits on the controls,
        # or 4u from the bottom.
        self.bottom_band = BottomBand(
            self.overlays.transport, self.mixer, self.overlays.banner, self.pip_frame, self
        )
        self.pip_label.pillPressed.connect(self.pip_frame.pill_press)
        self.pip_label.pillMoved.connect(self.pip_frame.pill_move)
        self.pip_label.pillReleased.connect(self.pip_frame.pill_release)
        # The multiview lays the surfaces out as tiles of the page (no surface moves pages);
        # after the PiP frame's filters, so it sees the tiles' presses first.
        self.grid_frame = GridFrame(self.player_page, self.surfaces, self.grid_tiles)
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
            live=self.live_session,
            budget=self.budget,
            sessions=self.sessions,
            pip_frame=self.pip_frame,
            sound_sync=self.sound_sync,
            channel_pictures=self.channel_pictures,
            catchup_memory=self.catchup_memory,
            pick_subtitle_file=self.pick_subtitle_file,
            # Why a channel or a provider failed, asked of its account (no stream opened).
            account_checks=AccountChecks(config_manager, parent=self),
            online_subtitles=OnlineSetup(self.jobs, self.subtitle_picks, self.open_url),
            grid_frame=self.grid_frame,
            grid_tiles=self.grid_tiles,
            host=self.host,
            make_slot=self._make_slot,
            parent=self,
        )
        self.controller = controller
        # Wired before the Library connects to the catalog: on providerReady the controller
        # settles the provider change before a Library play waiting for it runs.
        controller.wire()
        self.screens = TVPages(self, self)  # the Library's controller and the page moves
        self.library_controller = self.screens.library_controller
        self.exports = ExportFlow(
            catalog=self.catalog,
            config=config_manager,
            runner=self.jobs,
            notify=self.screens.notify,
            dialog_parent=self,
            parent=self,
        )
        self.menus = TVMenus(self, self)
        # The player controls' and the guide's ⌕: the Library's search, as Ctrl/⌘+F.
        self.overlays.transport.searchRequested.connect(self.screens.open_search)
        # ‹ Library on the player controls: back to the Library a title came from, as Esc.
        self.overlays.transport.libraryRequested.connect(self.screens.back_to_library)
        self.overlays.guide.searchRequested.connect(self.screens.open_search)
        # The corner player (the Library with what plays in a corner of it): ⤢ and a click on
        # it bring the player back, × stops it, the speaker mutes.
        self.pages.modeChanged.connect(self._on_page_mode)
        # The Library (its corner player) lays the player out: a floating picture comes back.
        self.pages.modeChanged.connect(lambda *_: self.desktop_pip.dock())
        self.pages.cornerClicked.connect(self.screens.back_to_player)
        self.corner_controls.backRequested.connect(self.screens.back_to_player)
        self.corner_controls.stopRequested.connect(self.screens.stop_corner)
        self.corner_controls.soundRequested.connect(self._on_corner_sound)
        # The Library's ⛶: the menu bar, with View → Fullscreen, hides in fullscreen.
        self.library_page.nav.fullscreenRequested.connect(self.toggle_fullscreen)
        self.overlays.welcome.addRequested.connect(self.add_provider)
        self.overlays.welcome.freeRequested.connect(self.show_free_channels)
        self.overlays.drawer.actionRequested.connect(self.menus.on_drawer_action)
        # The channel list's find: genres and channels at every provider, found in a worker;
        # its last row looks for films and series in the Library's search.
        self.finder = FindSession(
            self.provider_manager,
            self.jobs,
            loaded=self.catalog.loaded_contents,
            signature=self._find_signature,
            preferences=self._find_preferences,
            parent=self,
        )
        self.overlays.drawer.set_finder(self.finder)
        # Its index holds the catalogs' channels: a refresh or an edited list lets them go.
        self.catalogs.contentChanged.connect(self.finder.content_changed)
        self.catalogs.providersChanged.connect(self.finder.content_changed)
        self.overlays.drawer.libraryFindRequested.connect(self.screens.open_search)
        # A self-update (Windows, a macOS disk image) waits for background threads: the
        # engine's must stop first, and start again if the update cannot launch.
        call_before_update(self.stop_live, self.restart_live)
        sources = (*self.surfaces.values(), self.host)
        # The pointer over the video goes with the player controls (and comes back on a move).
        self.pointer = PointerHider(
            sources,
            self.overlays.transport,
            lambda: not self.screens.library_shown() and controller.pointer_may_hide(),
            parent=self,
        )
        for source in sources:
            self._wire_source(source)
        self.epg_refresh = QTimer(self, interval=EPG_REFRESH_MS)  # the guide keeps moving
        self.epg_refresh.timeout.connect(lambda: self.catalog.refresh_epg())

        self.menus.build()
        config_manager.apply_window_settings(WINDOW_NAME, self)

    # ---- the players --------------------------------------------------------------------------

    def _slot(self, name: str, playback: PlaybackSession, live: LiveSession) -> Slot:
        """A player's slot: its tuner's budget slots carry the slot's name."""
        tuner = LiveTuner(playback, self.config_manager, live, self.budget, self, purpose=name)
        return Slot(name, playback, live, tuner=tuner)

    def _join_tuners(self) -> None:
        """Every player's tuner: one disk ring among them (the user's disk cap is the total),
        and a direct open waits for the others' closing streams (one may hold the last slot)."""
        if self.sessions is None:
            return
        tuners = [slot.tuner for slot in self.sessions.slots()]
        live_tuners = [t for t in tuners if isinstance(t, LiveTuner)]
        one_disk_ring(*live_tuners)
        wait_for_each_other(*live_tuners)

    def _wire_source(self, source) -> None:
        """A surface (or the host over them): its clicks, moves, wheel and side buttons."""
        controller = self.controller
        source.clicked.connect(self._on_video_clicked)
        source.doubleClicked.connect(controller.video_double_clicked)
        source.mouseMoved.connect(self.pointer.moved)
        source.mouseMoved.connect(controller.mouse_moved)
        source.wheelSteps.connect(controller.wheel)
        source.backPressed.connect(controller.mouse_back)
        source.forwardPressed.connect(controller.mouse_forward)

    def _make_slot(self, name: str) -> Optional[Slot]:
        """A multiview tile's player: a surface on the player page (hidden until the grid lays
        it out), a player, a live session and a tuner, known to the session manager, the
        frames, the pointer and the controller. None without libVLC."""
        if (
            self._closing
            or self.sessions is None
            or self.engine is None
            or self._instance is None
            or name in self.surfaces
        ):
            return None
        surface = VideoSurface(self.player_page)
        surface.hide()
        surface.installEventFilter(self)  # before the frames': it sees them first
        self.surfaces[name] = surface
        hardware = self.config_manager.hardware_decoding
        playback = PlaybackSession(self._instance, self, hardware=hardware)
        live = LiveSession(self.engine, playback, tuning=self.live_tuning)
        slot = self._slot(name, playback, live)
        self.sessions.add_slot(slot)
        self._join_tuners()
        self.pip_frame.add_surface(name, surface)
        self.grid_frame.add_surface(name, surface)
        self.pointer.add_widget(surface)
        self._wire_source(surface)
        self.controller.slot_added(slot)
        self._bind_video()
        return slot

    def _report_video_size(self, surface: QObject) -> None:
        """A player's surface changed size: a picture filling the window follows its shape
        (the Picture panel's Fill, 2.3)."""
        for name, player in self._players():
            if self.surfaces.get(name) is surface:
                player.set_video_size(surface.width(), surface.height())

    def _players(self) -> list[tuple[str, Session]]:
        """Every player by its slot's name (the main one alone without libVLC)."""
        if self.sessions is None:
            return [("main", self.session)]
        return [(slot.name, slot.playback) for slot in self.sessions.slots()]

    # ---- state the menus and pages read -------------------------------------------------------

    def closing(self) -> bool:
        """The window is closing: menus and pages start nothing new."""
        return self._closing

    def mini_player_active(self) -> bool:
        return self._mini_restore is not None

    def leave_mini_player(self) -> None:
        """Back to the normal window (nothing when it is not the mini player)."""
        self._leave_mini()

    def _on_page_mode(self, mode: str) -> None:
        """The pages moved: in the corner only its controls stay on the host, and the picture-
        in-picture frame leaves the mouse to the corner player."""
        corner = mode == PageStack.CORNER
        self.pip_frame.set_interactive(not corner)
        self.host.show_only(self.corner_controls if corner else None)
        if corner:
            self.corner_controls.set_muted(self.controller.main_muted())
            self.corner_controls.set_level(self.controller.main_level())
            self.corner_controls.show()
        else:
            self.corner_controls.hide()
            if mode == PageStack.PLAYER and not self.pip_frame.small_rect().isEmpty():
                self.pip_label.show()  # the corner hid it; the PiP only shows it when it opens

    def open_url(self, url: str) -> None:
        """A web page in the browser (OpenSubtitles' sign-up and API key page)."""
        if not self._closing:
            QDesktopServices.openUrl(QUrl(url))

    def pick_subtitle_file(self, folder: str) -> str:
        """The Subtitles panel's "Load a file…": a subtitle file, from `folder` ("": none)."""
        if self._closing:
            return ""
        title = tr("Load subtitles")  # i18n: the title of the dialog that picks a subtitle file
        path, _filter = QFileDialog.getOpenFileName(self, title, folder, translate(SUBTITLE_FILTER))
        return path if not self._closing else ""

    def _on_corner_sound(self) -> None:
        self.controller.handle(Action.MUTE)
        self.corner_controls.set_muted(self.controller.main_muted())
        self.corner_controls.set_level(self.controller.main_level())

    # ---- input --------------------------------------------------------------------------------

    def _key_context(self) -> str:
        """The Library's page takes the keys; else the open guide or drawer, else the player."""
        return "library" if self.screens.library_shown() else self.controller.key_context()

    def _resolve(self, event: QKeyEvent):
        return resolve(event.key(), event.modifiers(), event.text(), context=self._key_context())

    def event(self, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.ShortcutOverride
            and isinstance(event, QKeyEvent)
            and not self._closing
            and self._resolve(event) is not None
        ):
            event.accept()  # deliver it as a key press: keyPressEvent handles it once
            return True
        if (
            event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Tab
            and not self._closing
            and self._resolve(event) is not None
        ):
            self.keyPressEvent(event)  # Tab moves the focus otherwise: the mixer's rows
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        resolved = None if self._closing else self._resolve(event)
        if resolved is None:
            super().keyPressEvent(event)
            return
        action, payload = resolved
        event.accept()
        with self.screens.from_keys():  # a page a key shows gets no second click
            self._act(action, payload, key=as_int(event.key()))

    def _act(self, action: Action, payload: str, key: int) -> None:
        if action is Action.ESCAPE and self.exports.cancel():
            return  # Esc while exporting cancels the export
        if self.screens.library_shown():
            handled = self.library_controller.handle(action, payload)
            if not handled and action is Action.FULLSCREEN:
                self.toggle_fullscreen()  # F11, ⌃⌘F: the Library's other keys are its own
            return
        if action is Action.SEARCH:
            self.screens.open_search()  # Ctrl/⌘+F: the quick search, from every screen
            return
        if action is Action.ESCAPE and self.screens.escape_to_library():
            return
        self.controller.handle(action, payload, key=key)

    def _on_video_clicked(self) -> None:
        if self._dragged:  # the press moved the mini player
            self._dragged = False
            return
        if self._pressed and self._mini_restore is not None:
            # Still held: a click only if it is released without dragging the mini player.
            self._click_on_release = True
            return
        self.controller.video_clicked()

    # ---- dropped files -------------------------------------------------------------------------

    @staticmethod
    def _dropped_paths(event: QDropEvent) -> list[str]:
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]

    @staticmethod
    def _dropped_texts(event: QDropEvent) -> list[str]:
        """Dropped web addresses and text: a provider's link, maybe (2.2)."""
        mime = event.mimeData()
        if mime is None:
            return []
        texts = [url.toString() for url in mime.urls() if not url.isLocalFile()]
        if mime.hasText():
            texts.append(mime.text())
        return texts

    def _dropped_providers(self, event: QDropEvent) -> tuple[list[str], list[ProviderLink]]:
        """The playlist files and provider links a drop carries (ui/provider_drop.py)."""
        files = provider_drop.playlist_files(self._dropped_paths(event))
        return files, provider_drop.links(self._dropped_texts(event))

    def _on_drag(self, event: QDropEvent) -> None:
        """Dragged over the window (or its overlays): taken only when it would load: subtitle
        files for what plays, or a playlist file or a provider's link (2.2)."""
        files, links = self._dropped_providers(event)
        subtitles = self.controller.accepts_subtitles(self._dropped_paths(event))
        if not self._closing and (subtitles or files or links):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _on_drop(self, event: QDropEvent) -> None:
        """Subtitle files go to what plays; playlist files and links become providers."""
        if self._closing:
            event.ignore()
            return
        subtitles = self.controller.drop_subtitles(self._dropped_paths(event))
        files, links = self._dropped_providers(event)
        added = bool(files or links)
        if added:
            # After the drop: adding switches the list, and a portal's form is a dialog.
            QTimer.singleShot(0, self, lambda: self.add_dropped_providers(files, links))
        if subtitles or added:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        self._on_drag(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self._on_drag(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._on_drop(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(watched, OverlayHost) and isinstance(event, QDropEvent):  # drags too
            if event.type() == QEvent.Type.Drop:
                self._on_drop(event)
            else:
                self._on_drag(event)
            return True
        if isinstance(event, QMouseEvent) and watched in self.surfaces.values():
            if event.type() in _CLICK_EVENTS:
                reclaim_first_responder(self)  # macOS: libVLC's view took the keys
            if event.type() in _MOUSE_EVENTS:
                self._on_video_mouse(event)
        elif event.type() == QEvent.Type.Resize and watched in self.surfaces.values():
            self._report_video_size(watched)
        elif (
            watched is self.player_page
            and event.type() == QEvent.Type.Paint
            and not self._first_frame
        ):
            self._first_frame = True
            self.player_page.removeEventFilter(self)
            self._load_deferred(deferred_modules())  # after this frame is shown
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

    def _load_deferred(self, names: list[str]) -> None:
        """Load the deferred libraries one per pass of the event loop (services.lazy_import)."""
        if names and not self._closing:
            QTimer.singleShot(0, self, lambda: self._load_first(names))

    def _load_first(self, names: list[str]) -> None:
        load_module(names[0])
        self._load_deferred(names[1:])

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
        self._request_fullscreen(not self.fullscreen_heading())

    @staticmethod
    def _animates_fullscreen() -> bool:
        """macOS animates fullscreen changes, and AppKit drops one made during another."""
        return QGuiApplication.platformName() == "cocoa"

    def leave_fullscreen(self) -> None:
        if not self._closing and self.fullscreen_heading():
            self._request_fullscreen(False)

    def fullscreen_heading(self) -> bool:
        """The fullscreen state the window is on its way to, reported or not."""
        for wanted in (self._fs_queued, self._fs_target):
            if wanted is not None:
                return wanted
        return self.isFullScreen()

    def _request_fullscreen(self, on: bool) -> None:
        """Enter or leave fullscreen; on macOS only once the previous change was reported.

        AppKit drops a change made while another still animates: a leave asked right after
        the entry (CI's macOS 15 VM) left the window in fullscreen for good.
        """
        if self._fs_target is not None:
            self._fs_queued = on  # applied when the platform reports the change under way
            return
        if on == self.isFullScreen():
            return
        if on:
            self._pre_fullscreen = None  # remembered afresh as fullscreen begins
        else:
            self._fullscreen_restore = self.normalGeometry()
        handle = self.windowHandle()
        if self._animates_fullscreen() and handle is not None:
            handle.installEventFilter(self._fs_watch)
            self._fs_target = on
            self._fs_settle.start()
        state = self.windowState()
        fullscreen = Qt.WindowState.WindowFullScreen
        self.setWindowState(state | fullscreen if on else state & ~fullscreen)

    def _fullscreen_settled(self) -> None:
        """The platform reported a state (or never did): apply a request made meanwhile."""
        if self._fs_target is None:
            return
        self._fs_settle.stop()
        handle = self.windowHandle()
        if handle is not None:
            handle.removeEventFilter(self._fs_watch)
        self._fs_target = None
        queued, self._fs_queued = self._fs_queued, None
        if queued is not None and not self._closing:
            QTimer.singleShot(0, self, lambda: self._request_fullscreen(queued))

    def toggle_mini_player(self) -> None:
        """Frameless and on top, a quarter of the screen wide in the bottom-right corner."""
        if self._closing or self._mini_waiting:
            return
        self.controller.leave_multiview()  # the mini player shows one picture
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

    def _may_float_picture(self) -> bool:
        """The large picture may float on another desktop now: fullscreen, the setting on, one
        picture (no multiview grid) on the player page, and something playing in it."""
        if self._closing or not self.isFullScreen():
            return False
        if not getattr(self.config_manager, "desktop_pip", True):
            return False
        if QApplication.activeModalWidget() is not None:
            return False  # a dialog would block the small window's clicks
        if self.controller.multiview_shown() or self.screens.library_shown():
            return False
        playing = (PlaybackState.OPENING, PlaybackState.BUFFERING, PlaybackState.PLAYING)
        state = self.controller.session.state()
        return state in playing or state is PlaybackState.PAUSED

    def _floating_rect(self) -> QRect:
        """The floating picture: where the user last moved or resized it while that is on a
        screen, else in the bottom-right corner of the screen under the pointer."""
        saved = getattr(self.config_manager, "desktop_pip_rect", None)
        if isinstance(saved, list) and len(saved) == 4 and all(isinstance(v, int) for v in saved):
            rect = QRect(*saved)
            if rect.width() > 0 and any(
                screen.availableGeometry().contains(rect.center())
                for screen in QGuiApplication.screens()
            ):
                return rect
        screen = QGuiApplication.screenAt(QCursor.pos()) or self.screen()
        screen = screen or QGuiApplication.primaryScreen()
        return corner_rect(screen.availableGeometry(), screen.geometry().width())

    def _remember_floating_rect(self, rect: QRect) -> None:
        self.config_manager.desktop_pip_rect = [rect.x(), rect.y(), rect.width(), rect.height()]
        try:
            self.config_manager.save_config()
        except Exception as e:  # a locked config: remembered for this run only
            logger.warning("Couldn't save the floating picture's place: %s", safe_message(e))

    def _on_picture_floating(self, floating: bool) -> None:
        note = self.floating_note
        if floating:
            note.setGeometry(self.player_page.rect())
            note.show()
            note.raise_()
        else:
            note.hide()

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
        """After fullscreen or the mini player changed: the menu bar and the transport's way back."""
        mini = self._mini_restore is not None
        bar = self.menuBar()
        if not bar.isNativeMenuBar():  # the macOS menu bar is outside the window
            bar.setVisible(not self.isFullScreen() and not mini)
        self.overlays.transport.set_window_mode(mini=mini, fullscreen=self.isFullScreen())

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self._update_menu_bar()
            # Fullscreen: the desktop shown is asked about (the picture may float); else the
            # picture is back, and nothing is asked.
            self.desktop_pip.watch(self.isFullScreen() and not self._closing and _macos_desktops())
            if isinstance(event, QWindowStateChangeEvent):
                was_fullscreen = event.oldState() & Qt.WindowState.WindowFullScreen
                if not was_fullscreen and self.isFullScreen() and self._pre_fullscreen is None:
                    # However it was entered: Qt keeps the frame from before (also when
                    # macOS enters fullscreen itself, checked on macOS 13). Kept until an
                    # exit uses it: a platform may echo the state change later.
                    normal = self.normalGeometry()
                    self._pre_fullscreen = normal if normal.isValid() else None
                elif was_fullscreen and not self.isFullScreen():
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
        target = self._fullscreen_restore or self._pre_fullscreen
        self._fullscreen_restore = self._pre_fullscreen = None
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
            self.menus.rebuild_providers()

    def add_provider(self, link: Optional[ProviderLink] = None) -> None:
        """Settings on the Providers tab, with a new provider to fill in (spec §8); from
        `link` when one was dropped (a portal's, whose MAC the user adds)."""
        if self._closing:
            return
        dialog = OptionsDialog(self)
        dialog.start_new_provider()
        if link is not None:
            dialog.fill_from_link(link)
        dialog.exec()
        self.menus.rebuild_providers()

    def add_dropped_providers(self, files: list[str], links: list[ProviderLink]) -> None:
        """Playlist files and provider links dropped on the window (2.2) become providers, as
        Free channels adds one: the list switches to the last one and a toast says so. One
        already in the list is only switched to. A portal's link without its MAC opens the
        add-provider form filled in instead."""
        if self._closing:
            return
        entries = [provider_drop.file_entry(path) for path in files]
        form: Optional[ProviderLink] = None
        for link in links:
            entry = provider_drop.link_entry(link)
            if entry is not None:
                entries.append(entry)
            elif form is None:
                form = link
        added: list[str] = []
        known: list[str] = []
        for entry in entries:
            name, existing = self._add_provider_entry(entry, provider_drop.same_provider)
            (known if existing else added).append(name)
        if added or known:
            self._show_provider((added or known)[-1])
            if added:
                names = ", ".join(added)
                # i18n: a toast; {names} is one or more provider names, joined with ", "
                self.screens.notify(tr("Added {names} to your providers", names=names))
            else:
                self.screens.notify(tr("{name} is already in your providers", name=known[-1]))
        if form is not None:
            self.add_provider(form)

    def _add_provider_entry(self, entry: dict, same) -> tuple[str, bool]:
        """Add provider `entry` (renamed "name (2)"… when its name is taken), unless the list
        has it already (`same(listed, entry)`): (its name in the list, whether it was there)."""
        providers = self.provider_manager.providers
        existing = next((p for p in providers if same(p, entry)), None)
        if existing is not None:
            return str(existing.get("name", "")), True
        entry["name"] = provider_drop.unique_name(entry["name"], (p.get("name") for p in providers))
        providers.append(entry)
        self.provider_manager.save_providers()
        self.catalogs.reload()  # the find, the Library and All providers hear of it
        return entry["name"], False

    def _show_provider(self, name: str) -> None:
        """The channel list shows provider `name` (loaded when it is not the one shown)."""
        self.config_manager.selected_provider_name = name
        self.save_config()
        self.menus.rebuild_providers()
        if name != self.catalog.current_provider_name():
            self.set_provider()

    def show_free_channels(self, global_pos: QPoint) -> None:
        """The Free channels… menu under `global_pos` (the welcome card's and the drawer's)."""
        if not self._closing:
            show_free_channels_menu(self, global_pos, self.add_free_channels)

    def add_free_channels(self, code: Optional[str]) -> None:
        """Add iptv-org's list (all countries, or `code`'s) and load it, as Settings → Save does
        for a new selected provider; a provider with that URL is only switched to."""
        if self._closing:
            return
        entry = free_channels.provider_entry(code)
        name, existing = self._add_provider_entry(entry, lambda p, e: p.get("url") == e["url"])
        self.config_manager.selected_provider_name = name
        self.save_config()
        self.menus.rebuild_providers()
        if not existing or name != self.catalog.current_provider_name():
            self.set_provider()

    def _providers_in_use(self) -> set[str]:
        """The providers a page shows (in All providers, every one): Settings may not drop
        their catalogs, and sets an edited one up again."""
        return self.catalog.shown_providers() | self.library_catalog.shown_providers()

    def providers_saved(self) -> None:
        """Settings changed the provider list: catalogs of removed or edited providers go, and
        what shows the guide reads it again (a provider's guide time offset may have moved)."""
        self.catalogs.reload()
        self.menus.rebuild_providers()
        self.controller.guides_changed()

    def save_config(self) -> None:
        """Settings → Save: the new settings apply even when config.json cannot be written."""
        try:
            self.config_manager.save_config()
        except Exception as e:
            logger.warning("Couldn't save the settings: %s", safe_message(e))
            self.screens.notify(translate(SAVE_FAILED_TEXT), "error", ERROR_MS)
        self.controller.settings_saved()

    def set_provider(self, force_update: bool = False) -> None:
        self.controller.select_provider(force_refresh=force_update)

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
        self._fit_restored()
        self._bind_video()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.controller.start()
        self.epg_refresh.start()
        if self.config_manager.save_failed():  # e.g. the defaults patched in at start
            self.screens.notify(translate(SAVE_FAILED_TEXT), "error", ERROR_MS)

    def _fit_restored(self) -> None:
        """Bring the saved geometry onto a screen: its monitor may be gone.

        Run once shown, when the frame margins are known; on the screen under the window's
        centre, else the one Qt put it on.
        """
        if self.windowState() & (Qt.WindowState.WindowFullScreen | Qt.WindowState.WindowMaximized):
            return
        geometry = self.geometry()
        screen = (
            QGuiApplication.screenAt(geometry.center())
            or self.screen()
            or QGuiApplication.primaryScreen()
        )
        if screen is None:
            return
        fitted = fit_to_area(geometry, frame_margins(self), screen.availableGeometry())
        if fitted != geometry:
            logger.info("Moved the restored window onto the screen: %s", fitted.getRect())
            self.setGeometry(fitted)

    def _bind_video(self) -> None:
        """Hand libVLC each surface's native window; again only if its id changed.

        Each player draws into its own surface for good: a PiP swap moves the surfaces, never
        the players. On macOS a window flag change (the mini player) keeps the same views.
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
        for name, player in self._players():
            if name == "main":
                continue
            handle = self.surfaces[name].native_handle()
            if handle != self._bound.get(name):
                self._bound[name] = handle
                player.set_window(handle)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Close in steps, each after the event loop ran again (`close_retry`).

        1. Stop: the live stream and the time-shift helper first (its supervisor thread
           would keep the wait below going), then the catalog starts no more jobs, playback
           stops, the geometry is saved.
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
            try:
                self.desktop_pip.close()  # the picture back in its window first
            except Exception as e:  # never in the way of closing
                logger.warning("Could not put the floating picture back: %s", safe_message(e))
            self._shut_down()
            self._thread_wait.start()
            self._close_later(event)
            return
        if has_pending_threads() and not self._threads_abandoned:
            if self._thread_wait.elapsed() < CLOSE_THREAD_WAIT_MS:
                self._close_retries += 1
                toast = self.overlays.toast
                if self._close_retries >= CLOSING_NOTICE_AFTER and not toast.isVisible():
                    toast.show_message(translate(CLOSING_TEXT), "info", CLOSING_NOTICE_MS)
                self._close_later(event)
                return
            self._abandon_threads()
        if not self._video_released:
            self._video_released = True
            self._run_steps(
                ("save the image cache index", self.image_manager.save_index),
                ("save the EPG indexes", self.catalogs.save_epg_indexes),
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

    def stop_live(self) -> None:
        """Close the live channel's stream, then the time-shift helper.

        Before anything waits for background threads: the engine's supervisor thread runs
        until its shutdown (window close, Windows self-update). The shutdown blocks for at
        most the engine's _SHUTDOWN_WAIT_MS (1.8 s; about 0.1 s normally). Safe to call again.
        """
        try:
            self.controller.stop_live()  # both live sessions stop, keeping the budget in step
        finally:
            if self.engine is not None:
                self.engine.shutdown()
                for live in self._lives():
                    live.engine_shut_down()  # it signals nothing from now on

    def restart_live(self) -> None:
        """A self-update stopped the time-shift helper and could not launch: start a new one.

        What played before plays again: a channel tunes afresh, a movie or episode reopens
        at its saved position.
        """
        if self._closing or self.engine is None or self.live_session is None:
            return
        old, self.engine = self.engine, LiveEngine(str(self.engine.root_dir), self)
        for live in self._lives():
            live.use_engine(self.engine)  # connected before it starts
        self.engine.start()
        old.deleteLater()
        call_before_update(self.stop_live, self.restart_live)  # each hook runs once
        logger.info("The update did not launch; time-shift is back on.")
        self.controller.resume_stopped()  # what played before plays again

    def _lives(self) -> list[LiveSession]:
        if self.sessions is None:
            return [live for live in (self.live_session,) if live is not None]
        return [slot.live for slot in self.sessions.slots()]

    def _find_signature(self) -> tuple:
        """Changes when what the channel list's find reads may have: the providers, or the
        content of a provider's catalog."""
        hub = self.catalogs
        versions = tuple((name, hub.catalog(name).content_version()) for name in hub.created())
        return tuple(hub.names()), versions

    def _find_preferences(self) -> Preferences:
        """What ranks first in the channel list's find: the provider it shows, the providers
        and channels watched lately, the stars."""
        catalog, config = self.catalog, self.config_manager
        favorite = favorites.matcher(config)
        names, browsed = catalog.provider_names(), catalog.current_provider_name()
        return preferences_for(names, browsed, config.watch_history, favorite)

    def _shut_down(self) -> None:
        self.epg_refresh.stop()
        self._run_steps(
            ("let the next click through", self.screens.shutdown),
            ("cancel the export", self.exports.shutdown),
            ("close the Library", self.library_controller.shutdown),  # its search, its pending
            ("stop the channel list's find", self.finder.shutdown),
            ("stop the live pipeline", self.stop_live),
            ("stop the catalogs", self.catalogs.shutdown),  # and cancel their running jobs
            ("cancel the Library's jobs", self.jobs.cancel_all),  # its searches
            ("stop the controller", self.controller.shutdown),
            ("stop following the surfaces", self.pip_frame.close),
            ("stop laying out the tiles", self.grid_frame.close),
            ("stop loading logos", self.logos.shutdown),
            ("stop loading posters", self.posters.shutdown),
            ("save the window geometry", self._save_geometry),
        )

    @staticmethod
    def _run_steps(*steps: tuple[str, Callable[[], None]]) -> None:
        for what, step in steps:
            try:
                step()
            except Exception as e:
                # The message can name a provider URL: a scrubbed one, and the frames only.
                logger.error("Could not %s: %s", what, safe_message(e))
                logger.debug("Could not %s:\n%s", what, safe_traceback(e))

    def _save_geometry(self) -> None:
        self.config_manager.save_window_settings(_Geometry(self._normal_geometry()), WINDOW_NAME)

    def _release_video(self) -> None:
        for _name, player in self._players():
            player.release()
        if self._owns_instance and self._instance is not None:
            instance, self._instance = self._instance, None
            instance.release()
