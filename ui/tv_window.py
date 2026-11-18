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
from datetime import datetime, timedelta
import logging
import multiprocessing
import os
import platform
import stat
import sys
import threading
import time
from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import (
    QElapsedTimer,
    QEvent,
    QMargins,
    QObject,
    QPoint,
    QPointF,
    QRect,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
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

from services import favorites, free_channels, secure_dns, system_open, thread_cleanup
from services.account_check import AccountChecks
from services.browsed_catalog import BrowsedCatalog
from services.casting import CastDevices, LibVlcDiscovery
from services.catalogs import Catalogs
from services.catchup_memory import FILE_NAME as CATCHUP_FILE, CatchupMemory
from services.connection_budget import ConnectionBudget
from services.epg_view import Programme
from services.find import Preferences, preferences_for
from services.folder_shelf import FILE_NAME as SHELF_FILE, FolderShelf, folder_kind, shelf_key
from services.hidden_groups import HiddenGroups
from services.i18n import N_, number, tr, tr_n, translate
from services.jobs import JobRunner
from services.lazy_import import deferred_modules, load_module
from services.live.engine import LiveEngine
from services.live.kept import KeptBuffers
from services.live.live_session import LiveSession
from services.live.tuning import FILE_NAME as TUNING_FILE, LiveTuning
from services.local_playlist import (
    EMPTY,
    HLS,
    LOCAL,
    OTHER,
    PROVIDER,
    UNREADABLE,
    PlaylistRead,
    read_files,
    read_folder,
    read_playlist,
    subtitles_for,
)
from services.media_keys import MediaKeys
from services.media_reporter import MediaReporter
from services.navigation import NavModel
from services.open_paths import (
    PLAYLIST_EXTENSIONS,
    Sorted,
    extended_path,
    is_music,
    is_video,
    natural_key,
    sort_paths,
)
from services.parental import Parental, install as install_parental
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
from services.provider_rename import HashedKeys, Renames, carry_over
from services.provider_types import is_media_server
from services.recording_schedule import (
    FILE_NAME as SCHEDULE_FILE,
    Schedule,
    flush_writes as recording_schedule_flush,
    slot_text,
)
from services.recordings_index import FILE_NAME as RECORDINGS_INDEX_FILE, RecordingsIndex
from services.recordings_list import timer_text
from services.redact import safe_message, safe_traceback
from services.sessions import SessionManager, Slot
from services.sound_sync import FILE_NAME as SOUND_SYNC_FILE, SoundSync
from services.subtitle_picks import FILE_NAME as SUBTITLE_PICKS_FILE, SubtitlePicks
from services.thread_cleanup import has_pending_threads
from services.track_memory import FILE_NAME as TRACKS_FILE, TrackMemory
from services.update_checker import call_before_update, set_quit_note
from services.watch_state import WatchState
from services.zapping import ZapController
from ui import item_provider, macos_window, provider_drop, quit_reason
from ui.bottom_band import BottomBand
from ui.captions_controller import CaptionsController
from ui.desktop_pip import COVERED, DESKTOP, DesktopPip, corner_rect
from ui.export_flow import ExportFlow
from ui.find_session import FindSession
from ui.free_channels_menu import show_menu as show_free_channels_menu
from ui.grid_frame import GridFrame
from ui.keymap import Action, as_int, resolve
from ui.library.folders_view import where_it_starts
from ui.library.library_screen import LibraryScreen
from ui.link_lanes import Target
from ui.live_tuner import LiveTuner, one_disk_ring, wait_for_each_other
from ui.logos import LogoCache, PosterCache
from ui.macos_window import reclaim_first_responder
from ui.overlay_host import OverlayHost
from ui.overlays.audio_screen import AudioScreen
from ui.overlays.back_notice import BackNotice
from ui.overlays.captions import CaptionsOverlay
from ui.overlays.cast_notice import CastNotice
from ui.overlays.channel_column import ChannelColumn
from ui.overlays.corner_controls import CornerControls
from ui.overlays.drawer import Drawer
from ui.overlays.grid_tiles import GridTiles
from ui.overlays.guide import Guide
from ui.overlays.layout_row import LayoutRow
from ui.overlays.mixer import Mixer
from ui.overlays.more_menu import MoreMenu
from ui.overlays.next_card import NextCard
from ui.overlays.opening import OpeningOverlay
from ui.overlays.painting import fmt_clock
from ui.overlays.pip_label import PipLabel
from ui.overlays.question_card import QuestionCard
from ui.overlays.rec_sign import RecSign
from ui.overlays.remote_pair import PairOverlay
from ui.overlays.retry_notice import RetryNotice
from ui.overlays.sleep_notice import SleepNotice
from ui.overlays.stats import StatsOverlay
from ui.overlays.stopped_notice import StoppedNotice
from ui.overlays.subtitle_search import SubtitleSearchPage
from ui.overlays.toast import Toast
from ui.overlays.track_panel import TrackPanel
from ui.overlays.transport import Transport
from ui.overlays.welcome import WelcomeCard
from ui.overlays.zap_banner import ZapBanner
from ui.page_stack import PageStack
from ui.pip import PipFrame
from ui.pointer import PointerHider
from ui.record_timer_dialog import RecordTimerDialog, TimerChoice
from ui.recording_scheduler import RecordingScheduler
from ui.remote_controller import RemoteController
from ui.settings_dialog import OptionsDialog
from ui.song_jobs import PanelCover
from ui.subtitle_search import OnlineSetup
from ui.track_controls import SUBTITLE_FILTER
from ui.tv_controller import ERROR_MS, INFO_MS, Overlays, TVController
from ui.tv_menus import TVMenus
from ui.tv_pages import TVPages
from ui.video_surface import VideoSurface
from ui.visualizer import Visualizer

logger = logging.getLogger(__name__)

WINDOW_NAME = "tv_window"
TIMESHIFT_DIR = "timeshift"  # the live buffers' root, in the config directory
CLOSING_TEXT = N_("Closing after background requests finish…")  # i18n: a toast while QiTV quits
CLOSE_RETRY_MS = 50
CLOSE_THREAD_WAIT_MS = 30_000  # then quit anyway: a stuck worker must not keep QiTV open
CLOSING_NOTICE_AFTER = 10  # close retries (half a second) before the notice shows
CLOSING_NOTICE_MS = 60_000  # shown again only if it hides before the wait ends
REMOTE_CLOSE_WAIT_S = 4.0  # quitting waits this long at most for the phone remote to close
EPG_REFRESH_MS = 2 * 60 * 60 * 1000
WATCH_SAVE_DELAY_MS = 2000  # a play is saved once the zapping settles (and on close)
MINI_WIDTH_FRACTION = 0.25
MINI_MARGIN = 10
FULLSCREEN_EXIT_MS = 1500  # the longest wait for the platform to finish leaving fullscreen
FULLSCREEN_SETTLE_MS = 5000  # the longest wait for macOS to report a fullscreen change
FORWARD_WAIT_MS = 1000  # the longest wait for QiTV to come forward before a remote's fullscreen
FORWARD_POLL_MS = 50
# config.json could not be written (locked by antivirus, read-only, a full disk).
SAVE_FAILED_TEXT = N_("Couldn't save the settings; changes will be lost when QiTV closes")
# i18n: a toast after Settings → Save: the scheduled recordings of a provider removed there were
# cancelled ({titles}: their programmes, comma-separated)
DROPPED_RECORDINGS_TEXT = N_("Recordings of a removed provider were cancelled: {titles}")
ABANDONED_EXIT_CODE = 3  # QiTV quit while background work still ran (see _exit_now)
# Where open_paths' files come from: QiTV started with them, another start handed them on, a
# drop, File → Open File….
LAUNCH, HANDOVER, DROP, DIALOG = "launch", "handover", "drop", "dialog"
SHELF = "shelf"  # Plan 4: the Folders tab's (a tile, Add folder): as a drop; Esc goes back there
# A multi-select opened from the file manager can come as one start per file, in any order
# (Windows): a start's file this soon after the one chosen replaces it only if it sorts first.
OPEN_BURST_S = 3.0
OPENED_POLL_MS = 100  # subtitle files dropped with a video wait for it to open,
OPENED_WAIT_S = 30.0  # this long at most
# The film opened: its player takes subtitle files (it says OPENING before it has the input).
_OPENED_STATES = frozenset({PlaybackState.BUFFERING, PlaybackState.PLAYING, PlaybackState.PAUSED})
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


def app_active() -> bool:
    """Whether QiTV is the app in front."""
    return QGuiApplication.applicationState() == Qt.ApplicationState.ApplicationActive


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _macos_desktops() -> bool:
    """Only macOS has fullscreen desktops to leave (the desktop picture in picture)."""
    return QGuiApplication.platformName() == "cocoa"


def _native_video_handles() -> bool:
    """False on Qt platforms without real window ids (offscreen, Wayland): libVLC would crash."""
    return QGuiApplication.platformName() == _NATIVE_PLATFORMS.get(platform.system())


def _readable_size(path: str) -> Optional[int]:
    """The size of the file at `path`; None when it is gone, no file (a folder, a broken link)
    or can't be read. On Windows a path past 260 characters may only be found in its
    extended spelling (a QiTV that isn't long-path aware): that is tried too. It touches the
    disk: never on the GUI thread."""
    size = _file_size(path)
    if size is None and sys.platform == "win32":
        extended = extended_path(path)
        if extended != path:
            size = _file_size(extended)
    return size


def _file_size(path: str) -> Optional[int]:
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return None
        with open(path, "rb"):
            pass
    except OSError, ValueError:
        return None
    return info.st_size


def _same_file(a: str, b: str) -> bool:
    """Whether two spellings of a path name one file, without the disk: "." and ".." anywhere,
    and the case and separators on Windows."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


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


class _FileCheck(QObject):
    """A file opened in QiTV, looked at on a thread of its own: `checked` (the open's number,
    its path, its size or None when it can't play) comes on the GUI thread."""

    checked = Signal(int, str, object)

    def start(self, generation: int, path: str) -> None:
        threading.Thread(
            target=self._check, args=(generation, path), name="qitv-open-file", daemon=True
        ).start()

    def _check(self, generation: int, path: str) -> None:
        try:
            size = _readable_size(path)
        except Exception as error:  # never unanswered: the open would hold for good
            name = type(error).__name__
            logger.warning("A file opened in QiTV could not be looked at (%s)", name)
            size = None
        try:
            self.checked.emit(generation, path, size)
        except RuntimeError:
            pass  # QiTV closed meanwhile


def _is_playlist(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in PLAYLIST_EXTENSIONS


def _is_folder_read(path: str) -> bool:
    """Whether a read that plays (_play_read) is a folder's (read_folder): a playlist file's
    path is a playlist, and several media files read together come under their first one's
    path (a media file: sort_paths put it among the media); a folder's never is either (it was
    among the others)."""
    return not _is_playlist(path) and not (is_video(path) or is_music(path))


def _shown_name(path: str) -> str:
    """A file's or a folder's name as a message says it (macOS ends a folder with "/")."""
    trimmed = path.rstrip("/\\") or path
    return os.path.basename(trimmed) or trimmed


class _PlaylistRead(QObject):
    """What an open names that only the disk tells, read on a thread of its own: `read` (the
    read's number, [(each path, its PlaylistRead)]) comes on the GUI thread. A playlist file is
    read as one (read_playlist), any other path as a folder (read_folder: a file QiTV won't
    play is OTHER), and media `files` opened together as one list (read_files, given under
    the first one's path): the kind of each path (open_paths.sort_paths) says which read it
    has. Without `tags` (nothing of it will play: a film came with it) a folder's songs'
    tags are not read."""

    read = Signal(int, object)

    def start(
        self, serial: int, paths: list[str], files: Sequence[str] = (), *, tags: bool = True
    ) -> None:
        threading.Thread(
            target=self._read,
            args=(serial, list(paths), list(files), tags),
            name="qitv-open-playlist",
            daemon=True,
        ).start()

    def _read(self, serial: int, paths: list[str], files: list[str], tags: bool) -> None:
        reads: list[tuple[str, PlaylistRead]] = []
        for path in paths:
            playlist = _is_playlist(path)
            try:
                if playlist:
                    read = read_playlist(path)
                else:
                    read = read_folder(path) if tags else read_folder(path, cap=0)
            except Exception as error:  # neither raises; never unanswered all the same
                name = type(error).__name__
                logger.warning("A playlist or folder opened in QiTV could not be read (%s)", name)
                # As before playlists were read: a provider, whose loader says what is wrong.
                stem = os.path.splitext(os.path.basename(path))[0]
                read = PlaylistRead(PROVIDER, stem) if playlist else PlaylistRead(UNREADABLE, "")
            reads.append((path, read))
        if files:
            try:
                read = read_files(files)
            except Exception as error:  # it never raises; never unanswered all the same
                name = type(error).__name__
                logger.warning("Files opened together could not be read (%s)", name)
                read = PlaylistRead(UNREADABLE, "")
            reads.append((files[0], read))
        try:
            self.read.emit(serial, reads)
        except RuntimeError:
            pass  # QiTV closed meanwhile


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
        # 2.9.1: before the first provider request and the time-shift helper's start.
        secure_dns.enable(getattr(config_manager, "secure_dns", False) is True)
        self.provider_manager = provider_manager
        self.image_manager = image_manager
        self.epg_manager = epg_manager
        self._started = False
        self._closing = False
        # Windows: the run's DDE owner (services.dde_windows.Serving), set by main(); None
        # elsewhere and from source
        self.dde: Any = None
        # This QiTV's claim on its settings folder (services.single_instance.InstanceServer),
        # set by main(): it stops answering other starts once the close begins.
        self.instance: Any = None
        self._close_retries = 0
        self._video_released = False
        self._closed = False
        self._quit_asked: Optional[str] = None  # how QiTV's Quit action was reached (2.8)
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
        # Files opened in QiTV (open_paths): each looked at off the GUI thread, the newest plays.
        self._file_check = _FileCheck(self)
        self._file_check.checked.connect(self._on_file_checked)
        self._clock: Callable[[], float] = time.monotonic  # OPEN_BURST_S's (tests set it)
        self._open_generation = 0
        # The newest open's (number, media file, the subtitle files dropped with it).
        self._open_due: Optional[tuple[int, str, list[str]]] = None
        # Playlist files opened in QiTV, read off the GUI thread first (_on_playlists_read):
        # the reads' count, the read that may still play (None: a newer choice came), and the
        # provider links dropped with each read's files (added with its providers, at once).
        self._playlist_read = _PlaylistRead(self)
        self._playlist_read.read.connect(self._on_playlists_read)
        self._playlist_reads = 0
        self._playlist_due: Optional[int] = None
        self._read_links: dict[int, list[ProviderLink]] = {}
        # The reads of paths of no known kind alone (folders, or files QiTV won't play: only
        # the disk tells), which become the choice only once one turns out to play: a file
        # QiTV won't play leaves alone what is read before it (_on_paths_read).
        self._tentative: set[int] = set()
        # The subtitle files dropped with several media files, for their list (by its read's
        # number), then by the entry each is for, for each time it opens (list_entry_opens),
        # until another list or file plays.
        self._read_subtitles: dict[int, list[str]] = {}
        self._entry_subtitles: dict[str, list[str]] = {}
        self._shelf_reads: set[int] = set()  # Plan 4: the reads of a Folders tile (P4-26)
        # Plan 4: the shelf's key of the folder whose list plays (None: no folder's): its
        # entries opening are kept as its last entry (list_entry_opens).
        self._shelf_folder: Optional[str] = None
        self._burst: Optional[tuple[float, str]] = None  # (when, file): a start's choice
        # A file is being played and QiTV's mark is to draw over it (opening_over says no).
        self._opening_due = False
        # The subtitle files dropped with a video, for its target once it opened, until when.
        self._subtitles_due: Optional[tuple[Target, list[str], float]] = None
        self._subtitles_wait = QTimer(self, interval=OPENED_POLL_MS)
        self._subtitles_wait.timeout.connect(self._load_due_subtitles)

        self.setWindowTitle("QiTV")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Media, subtitle and playlist files or provider links dropped on it.
        self.setAcceptDrops(True)
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
        self.library_question = QuestionCard(self.library_page)  # 2.6: stop a recording?
        self.library_rec_sign = RecSign(self.library_page, bottom=True)  # 2.6: recordings on
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
        self.catalogs.screen_4k = self._screen_is_4k  # 2.8: a media server's 4K versions
        # 2.8: parental controls: the browsed catalogs and the searches leave out what they hide
        self.parental = Parental(config_manager)
        self.catalogs.parental = self.parental
        # 2.9.1: groups hidden to tidy the lists, without a PIN (Settings → Providers).
        self.hidden_groups = HiddenGroups(config_manager)
        self.catalogs.hidden_groups = self.hidden_groups
        self.parental.group_title = self.catalogs.group_title  # the adult rule reads names
        self._relock = QTimer(self, singleShot=True)
        self._relock.timeout.connect(self.parental_changed)
        install_parental(self.parental)
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
            mix_key="library_mix",  # 2.8: the providers ticked in its menu
            parent=self,
        )
        self.nav = NavModel(self.catalog, config_manager, self)
        self.zap = ZapController(parent=self)
        owns_instance = vlc_instance is None
        if owns_instance:  # the decoder Settings chose (software unless hardware)
            from ui.overlays.captions import subtitle_font

            vlc_instance = create_instance(  # subtitles drawn as the AI captions are (2.9)
                default_args(config_manager.hardware_decoding, subtitle_font=subtitle_font())
            )
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
        self.kept: Optional[KeptBuffers] = None
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
            # 2.6: the buffers of the channels left, for every player (and those kept on disk
            # before a restart, taken back by the helper).
            self.kept = KeptBuffers(self.engine)
            self._take_kept_back()
            self.sessions = SessionManager(
                self._slot("main", self.session, self.live_session),
                self._slot("pip", self.pip_session, self.pip_live),
                self.budget,
                parent=self,
            )
            self.sessions.set_slot_factory(self._make_slot)  # 2.6: recordings' stand-ins
            self._join_tuners()
        self.watch = WatchState(config_manager, save_delay_ms=WATCH_SAVE_DELAY_MS)
        # Each channel's sound sync (J/K, the Sound panel), written off the GUI thread.
        self.sound_sync = SoundSync(os.path.join(config_manager.get_config_dir(), SOUND_SYNC_FILE))
        # The tracks chosen for each film, series and channel (2.9.1), alike.
        self.track_memory = TrackMemory(os.path.join(config_manager.get_config_dir(), TRACKS_FILE))
        # Plan 4: the folders opened as folders, for the Library's Folders tab, alike.
        self.folder_shelf = FolderShelf(os.path.join(config_manager.get_config_dir(), SHELF_FILE))
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

        def playing() -> Optional[dict]:
            return controller.playing_channel()  # its rows' ▶

        catalog = self.catalog

        def guide_programmes(channel: dict, start: datetime, limit: int):
            return catalog.programmes(channel, start, limit=limit)

        def archive_days(channel: dict) -> int:
            return controller.archive_days(channel)

        def archive_reach(channel: dict) -> timedelta:
            return controller.archive_reach(channel)  # 2.7: a server window's too

        def deepest_window(channels: list[dict], beyond: timedelta) -> timedelta:
            return controller.deepest_window(channels, beyond)

        host = self.host
        self.grid_tiles = GridTiles(host)  # the multiview's labels, focus ring, "+" and cards
        self.pip_label = PipLabel(host)
        self.mixer = Mixer(host)
        self.sound_panel = TrackPanel(host)
        self.subtitle_panel = TrackPanel(host)
        self.picture_panel = TrackPanel(host)
        self.quality_panel = TrackPanel(host)  # 2.8: the ⚙ quality menu
        self.stats_overlay = StatsOverlay(host)
        self.sleep_notice = SleepNotice(host)
        self.back_notice = BackNotice(host)  # 2.6: Resume, where a channel was left
        self.question_card = QuestionCard(host)  # 2.6: stop a recording for a stream?
        self.quality_card = QuestionCard(host)  # 2.8: convert on the server?
        self.captions_card = QuestionCard(host)  # 2.9: download AI captions' speech model?
        self.rec_sign = RecSign(host)  # 2.6: recordings in the background
        self.stopped_notice = StoppedNotice(host)  # 2.6: after Stop, Play again
        self.cast_notice = CastNotice(host)  # 2.8: Playing on <device>, Stop casting
        self.retry_notice = RetryNotice(host)  # a channel's stream down: Trying again, Try now
        self.pair_overlay = PairOverlay(host)  # the phone remote's pairing QR
        self.layout_row = LayoutRow(host)  # the multiview's layouts, above its layout chip
        self.subtitle_search = SubtitleSearchPage(host)
        self.corner_controls = CornerControls(host)
        # What a song (or a radio channel) shows where the black picture was: its cover, album
        # and words, lit by the cover's colours (spec §10).
        self.audio_screen = AudioScreen(host)
        self.captions_overlay = CaptionsOverlay(host)  # 2.9: AI captions over the picture
        self.opening_overlay = OpeningOverlay(host)  # QiTV's mark while a file opens
        # 2.8: the recordings scheduled from the guide (a small file beside the config).
        self.recording_schedule = Schedule(
            os.path.join(config_manager.get_config_dir(), SCHEDULE_FILE)
        )
        # 2.9.7: what each recording holds (its programme, channel and logo), by its file
        self.recordings_index = RecordingsIndex(
            os.path.join(config_manager.get_config_dir(), RECORDINGS_INDEX_FILE)
        )
        self.scheduler: Optional[RecordingScheduler] = None
        self._timer_dialog: Optional[RecordTimerDialog] = None  # "Record at a time", last open
        self.overlays = Overlays(
            banner=ZapBanner(host),
            column=ChannelColumn(self.logos, now_provider, parent=host),
            drawer=Drawer(self.nav, self.logos, now_provider, parent=host),
            transport=Transport(host),
            toast=Toast(host),
            guide=Guide(
                self.logos,
                guide_programmes,
                _local_now,
                parent=host,
                archive_days=archive_days,
                archive_reach=archive_reach,
                deepest_window=deepest_window,
                # 2.8: QiTV records programmes at their time (with the time-shift helper)
                scheduled=self._programme_scheduled if self._instance is not None else None,
                repeating=self._programme_repeats,  # 2.9.7: its Cancel skips the airing
            ),
            next_card=NextCard(host),
            welcome=WelcomeCard(host),
            pip_label=self.pip_label,
            mixer=self.mixer,
            sound_panel=self.sound_panel,
            subtitle_panel=self.subtitle_panel,
            subtitle_search=self.subtitle_search,
            picture_panel=self.picture_panel,
            quality_panel=self.quality_panel,
            quality_question=self.quality_card,
            stats=self.stats_overlay,
            sleep_notice=self.sleep_notice,
            back_notice=self.back_notice,
            question=self.question_card,
            library_question=self.library_question,
            rec_sign=self.rec_sign,
            library_rec_sign=self.library_rec_sign,
            stopped_notice=self.stopped_notice,
            cast_notice=self.cast_notice,
            retry_notice=self.retry_notice,
            pairing=self.pair_overlay,
            library_toast=self.library_toast,
            layout_row=self.layout_row,
            captions=self.captions_overlay,
            audio_screen=self.audio_screen,
        )
        # The audio dock's "⋯": the player's controls the dock has no button for (Plan 4).
        self.more_menu = MoreMenu(self.overlays.transport, host)
        self.overlays.more_menu = self.more_menu
        # 2.8.12: the REC sign showed through the channel list, the guide and the grid's tiles.
        self.rec_sign.cover_with(self.overlays.drawer, self.overlays.guide, self.grid_tiles)
        self.rec_sign.follow_hud(self.overlays.transport)  # 2.9.7: up with the controls
        for listing in (self.overlays.column, self.overlays.drawer, self.overlays.guide):
            listing.set_playing(playing)
        # Bottom to top. The PiP label sits over the transport, whose bottom band covers a
        # bottom corner's PiP: its pill, ⇄, speaker and × stay within reach; elsewhere on the
        # PiP the transport keeps only its own buttons and bar, and other clicks reach the
        # small picture (OverlayHost.set_hole). The multiview's tiles are drawn under all of
        # them, and take their clicks over the transport but for its buttons and bar
        # (GridTiles.hits); a song's screen above the tiles, over the black (it leaves the
        # bottom to the player controls, and all but its own controls' clicks to the host); AI
        # captions just above it, under everything else (they sit above the player controls
        # while those show, and take no click), and QiTV's mark
        # while a file opens over them (it takes no click either). The mixer shows over
        # them (it sits above the transport's controls and beside the PiP), and the Sound,
        # Subtitles and Picture panels, the multiview's layouts and the audio dock's ⋯ menu
        # over it (above their buttons). The statistics sit over the transport, under its title
        # row (they take no click). The drawer and the guide cover them all; the OpenSubtitles
        # search covers the player with its backdrop, under the corner player's controls, the
        # sleep timer's last minute, Resume and the question (2.6) and the toast.
        for overlay in (
            self.grid_tiles,
            self.audio_screen,
            self.captions_overlay,
            self.opening_overlay,
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
            self.quality_panel,
            self.layout_row,
            self.more_menu,
            self.overlays.drawer,
            self.overlays.guide,
            self.overlays.next_card,
            self.overlays.welcome,
            self.subtitle_search,
            self.corner_controls,
            self.sleep_notice,
            self.back_notice,
            self.stopped_notice,
            self.cast_notice,
            self.retry_notice,
            self.rec_sign,
            self.question_card,
            self.quality_card,
            self.captions_card,
            self.pair_overlay,
            self.overlays.toast,
        ):
            host.add_overlay(overlay)
        self.captions_overlay.follow(self.overlays.transport)
        # A radio channel keeps live TV's controls: the screen's top bar sits below their title
        # row while they show.
        self.audio_screen.follow(self.overlays.transport)
        self.audio_screen.leave_room_for(self.captions_overlay)  # Words on: title kept clear
        self.overlays.transport.moreRequested.connect(self.more_menu.open_at)
        # After the window's surface filters: the frame takes the small surface's left button
        # before the window (its mini-player drag) and the surface (the drawer) see it.
        # Each small picture takes the shape its player's picture shows in (no bars).
        self.pip_frame = PipFrame(
            self.player_page,
            self.surface,
            self.pip_surface,
            config_manager,
            shape_of=self._picture_shape,
        )
        # Desktop picture in picture (macOS): while the fullscreen window's desktop is not the
        # one shown, the large picture floats in a small window on every desktop; with its own
        # setting, also while the window (not fullscreen) is covered whole or minimized.
        self.desktop_pip = DesktopPip(
            self,
            on_active_space=macos_window.on_active_space,
            may_float=self._may_float_picture,
            lend=self.pip_frame.lend_large,
            take_back=self.pip_frame.take_back,
            place=self._floating_rect,
            join_all_spaces=macos_window.join_all_spaces,
            remember=self._remember_floating_rect,
            seen=macos_window.is_visible,
            may_float_covered=self._may_float_covered,
            activate=macos_window.activate_app,
            shape=self.pip_frame.lent_shape,
            parent=self,
        )
        # Covered: QiTV becoming the active app (a click on its Dock icon, ⌘Tab) or leaving it.
        self.app.applicationStateChanged.connect(self._on_app_state)
        # i18n: shown on the fullscreen player while its picture floats on another desktop
        self.floating_note = QLabel(tr("Playing in the small window"), self.player_page)
        self.floating_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.floating_note.setStyleSheet("color: rgba(255, 255, 255, 0.55); font-size: 18px;")
        self.floating_note.hide()
        self._floating_captions: Optional[OverlayHost] = None  # AI captions over it (2.9)
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
        # 2.8: the devices libVLC finds to cast to (looked for once the cast menu opens).
        self.cast_devices: Optional[CastDevices] = None
        if self._instance is not None:
            self.cast_devices = CastDevices(LibVlcDiscovery(self._instance), self)
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
            track_memory=self.track_memory,
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
            cast_devices=self.cast_devices,
            media_reporter=MediaReporter(  # 2.8: Jellyfin, Emby
                self._media_servers, verify=lambda: bool(config_manager.ssl_verify)
            ),
            parent=self,
        )
        self.controller = controller
        # Wired before the Library connects to the catalog: on providerReady the controller
        # settles the provider change before a Library play waiting for it runs.
        controller.wire()
        # 2.8.13: the system's media keys (the player listed in the system's media panel gets
        # them: macOS only so): each acts as the keyboard's, and the system hears what the
        # large picture plays (after the controller hears the state: it knows what plays by
        # then). It listens once the window is on screen (_start: Windows lists the window).
        self.media_keys = MediaKeys(self)
        self.media_keys.pressed.connect(self.remote_key)
        # Plan 4: a song on the audio screen shows there with its artist, album and cover
        # (a small file of its own, written on a thread); its tags and cover come after its
        # state, so what the screen shows tells the panel again (once the controller is done:
        # queued), as the cover's file does once written.
        self.panel_cover = PanelCover(self)
        self._panel_state = ""  # the large picture's state the panel heard last
        self.panel_cover.written.connect(self._refresh_now_playing)
        self.audio_screen.shownChanged.connect(
            self._refresh_now_playing, Qt.ConnectionType.QueuedConnection
        )
        # 2.9: AI captions (the Subtitles panel's row): a speech model in its own process
        # reads the live channel ahead of the picture; the captions show over it.
        self.captions = CaptionsController(
            self, card=self.captions_card, overlay=self.captions_overlay
        )
        # Words on the audio screen: its Stage shows them large (the line before, the line
        # said, its translation), and while it does the captions over the picture give way.
        self.captions.add_overlay(self.audio_screen)
        self.audio_screen.stageShown.connect(lambda _shown: self.captions.words_moved())
        # The Stage's bars, read from the song's own sound: its helper process, or the
        # captions' film run when they read the same file (one decode).
        self.visualizer = Visualizer(
            self.audio_screen, lambda: self.controller.session, self.captions, parent=self
        )
        self.media_keys.raiseRequested.connect(self.come_to_front)
        self.session.stateChanged.connect(self._show_now_playing)
        self.screens = TVPages(self, self)  # the Library's controller and the page moves
        if self.engine is not None:  # 2.8: scheduled recordings, by the time-shift helper
            self.attach_scheduler(
                RecordingScheduler(
                    schedule=self.recording_schedule,
                    engine=self.engine,
                    catalogs=self.catalogs,
                    budget=self.budget,
                    config=config_manager,
                    toast=lambda text, kind="info", ms=INFO_MS: self.screens.notify(text, kind, ms),
                    parent=self,
                    index=self.recordings_index,
                )
            )
        controller.parental = self.parental  # what it protects never enters the history
        controller.note_recording = self.note_recording  # 2.9.7: the recordings index
        controller.note_recording_end = self.note_recording_end
        controller.hidden_groups = self.hidden_groups
        self.overlays.guide.recordRequested.connect(self.schedule_programme)
        self.overlays.guide.recordCancelled.connect(self.cancel_programme)
        self.overlays.guide.timerRequested.connect(self.record_at)
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
        self.remote = RemoteController(self)  # the phone remote; off until Settings turns it on
        self.pair_overlay.opened.connect(lambda: self.remote.set_pairing(True))
        self.pair_overlay.closed.connect(lambda: self.remote.set_pairing(False))
        self.remote.codeChanged.connect(self.pair_overlay.set_url)
        # The player controls' and the guide's ⌕: the Library's search, as Ctrl/⌘+F.
        self.overlays.transport.searchRequested.connect(self.screens.open_search)
        # ‹ Library on the player controls: back to the Library a title came from, as Esc; the
        # audio dock's ⋯ Library and the audio screen's ‹: the Library for any song (P4-8 a).
        self.overlays.transport.libraryRequested.connect(self.screens.back_to_library)
        self.audio_screen.backRequested.connect(self.screens.library_from_sound)
        self.captions.changed.connect(controller.words_changed)  # the screen's Words pill
        self.overlays.transport.liveTvRequested.connect(self.screens.back_to_tv)  # 2.9.3
        self.overlays.guide.searchRequested.connect(self.screens.open_search)
        # The corner player (the Library with what plays in a corner of it): ⤢ and a click on
        # it bring the player back, × stops it, the speaker mutes.
        self.pages.modeChanged.connect(self._on_page_mode)
        self.pages.modeChanged.connect(self.controller.page_changed)  # 2.6: a question goes along
        # The Library (its corner player) lays the player out: a floating picture comes back.
        self.pages.modeChanged.connect(lambda *_: self.desktop_pip.dock())
        self.pages.cornerClicked.connect(self.screens.back_to_player)
        # A song's screen stays in the corner (small): a press on it is the corner player's
        # (dragged or clicked), as on its picture, never the song's play or pause.
        self.audio_screen.installEventFilter(self.pages)
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
        set_quit_note(self.controller.quit_note)  # 2.6: an update asks before it stops recordings
        sources = (*self.surfaces.values(), self.host)
        # The pointer over the video goes with the player controls (and comes back on a move).
        self.pointer = PointerHider(
            sources,
            self.overlays.transport,
            lambda: not self.screens.library_shown() and controller.pointer_may_hide(),
            parent=self,
            controls_hold=self._controls_hold_pointer,
        )
        # The player controls come on a real move, not on a jittering mouse (2.8.12: it kept
        # them, and the pointer with them, up).
        self.pointer.realMoved.connect(controller.mouse_moved)
        for source in sources:
            self._wire_source(source)
        self.epg_refresh = QTimer(self, interval=EPG_REFRESH_MS)  # the guide keeps moving
        self.epg_refresh.timeout.connect(lambda: self.catalog.refresh_epg())

        self.menus.build()
        config_manager.apply_window_settings(WINDOW_NAME, self)
        self._apply_keep_on_top()  # before the first show: only the flag
        self._arm_desktop_pip()

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
        for tuner in live_tuners:
            tuner.set_kept(self.kept)  # every player keeps the buffer of the channel it leaves

    def _wire_source(self, source) -> None:
        """A surface (or the host over them): its clicks, moves, wheel and side buttons."""
        controller = self.controller
        source.clicked.connect(self._on_video_clicked)
        source.doubleClicked.connect(controller.video_double_clicked)
        source.contextRequested.connect(controller.video_context)  # 2.9.3: a right-click
        source.mouseMoved.connect(self.pointer.moved)  # its realMoved: controller.mouse_moved
        source.wheelSteps.connect(controller.wheel)
        source.backPressed.connect(controller.mouse_back)
        source.forwardPressed.connect(controller.mouse_forward)

    # ---- scheduled recordings (2.8) -----------------------------------------------------------

    def _programme_scheduled(self, channel: dict, programme: Programme) -> bool:
        return self.recording_schedule.is_scheduled(channel, programme)

    def writing_paths(self) -> frozenset[str]:
        """The files recordings write now (the REC button's and the schedule's), as the
        engine running now says; safe from other threads (a reference read)."""
        engine = self.engine
        return engine.writing if engine is not None else frozenset()

    def _account_of(self, channel: dict) -> Optional[dict]:
        """The channel's provider's account, kept out of the recording schedule (2.8)."""
        try:
            return self.catalogs.provider(str(channel.get("_provider") or ""))
        except KeyError:
            return None

    def schedule_programme(self, channel: dict, programme: Programme, series: bool) -> str:
        """The guide's Record (or Record series, R): QiTV records it at its time. What the
        toast said ("" when the programme had ended)."""
        schedule = self.recording_schedule
        name = str(channel.get("name") or "")
        if series:
            item = schedule.add_series(channel, programme.title, account=self._account_of(channel))
            # 2.9.7: this airing records again if it was skipped
            schedule.unskip(str(item.get("id") or ""), programme.start.timestamp())
            # i18n: a toast: every airing of the programme on the channel will be recorded
            text = tr(
                "Every “{title}” on {channel} will be recorded.",
                title=programme.title,
                channel=name,
            )
        else:
            if schedule.add_once(channel, programme, account=self._account_of(channel)) is None:
                # i18n: a toast: a programme asked to be recorded ended already
                self.screens.notify(tr("“{title}” has ended.", title=programme.title))
                return ""
            when = fmt_clock(programme.start.astimezone())
            # i18n: a toast: the programme will be recorded at its time ({time}: its start)
            text = tr("“{title}” will be recorded at {time}.", title=programme.title, time=when)
        self.screens.notify(text)
        self.controller.update_guides()
        self._schedule_changed()
        if self.scheduler is not None:
            self.scheduler.tick()  # one on now starts at once
        return text

    def record_at(self, channel: dict, programme: Optional[Programme] = None) -> None:
        """2.8: "Record at a time" for `channel` (its right-click, the guide's footer), on the
        programme's time when one is given; what it is given is scheduled."""
        dialog = RecordTimerDialog(
            str(channel.get("name") or ""),
            start=programme.start if programme is not None else None,
            stop=programme.stop if programme is not None else None,
            parent=self,
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        def chosen() -> None:
            choice = dialog.choice()
            if choice is not None:
                self.schedule_timer(channel, choice)

        dialog.accepted.connect(chosen)
        self._timer_dialog = dialog
        dialog.open()

    def schedule_timer(self, channel: dict, choice: TimerChoice) -> str:
        """Once: a programme of its own at that time; else a timer on its days. What the toast
        said ("" when its time was over)."""
        if not choice.days:
            title = choice.title or slot_text(choice.begin, choice.minutes)
            programme = Programme(title, choice.start(), choice.stop())
            return self.schedule_programme(channel, programme, False)
        item = self.recording_schedule.add_timer(
            channel,
            begin=choice.begin,
            minutes=choice.minutes,
            days=choice.days,
            title=choice.title,
            account=self._account_of(channel),
        )
        text = timer_text(item)
        self.screens.notify(text)
        self._schedule_changed()
        if self.scheduler is not None:
            self.scheduler.tick()  # one on now starts at once
        return text

    def _programme_repeats(self, channel: dict, programme: Programme) -> bool:
        """2.9.7: `programme` records as an airing of a series (its Cancel skips it)."""
        item = self.recording_schedule.scheduled_item(channel, programme)
        return item is not None and item.get("kind") != "once"

    def cancel_programme(self, channel: dict, programme: Programme) -> None:
        """The guide's Cancel: a one-time recording is cancelled; an airing of a series is
        skipped, the series going on (2.9.7)."""
        item = self.recording_schedule.scheduled_item(channel, programme)
        if item is None:
            return
        if item.get("kind") == "once":
            self.cancel_scheduled(str(item.get("id") or ""))
            return
        item_id, start = str(item.get("id") or ""), programme.start.timestamp()
        if self.scheduler is not None:  # it ends a wait, or stops the airing that records
            done = self.scheduler.skip_airing(item_id, start)
        else:
            self.recording_schedule.skip(item_id, start)
            done = "skipped"
        time = programme.start.astimezone().strftime("%H:%M")
        if done == "stopped":
            # i18n: a toast: one airing of a series stopped recording ({title}: its own)
            text = tr("“{title}” stopped recording; the series goes on.", title=programme.title)
        else:
            # i18n: a toast: one airing of a series won't be recorded ({title}, {time}: its own)
            text = tr(
                "“{title}” at {time} won't be recorded; the series goes on.",
                title=programme.title,
                time=time,
            )
        self.screens.notify(text)
        self._schedule_changed()

    def cancel_scheduled(self, item_id: str) -> None:
        """A scheduled recording is cancelled (the guide's, the Library's right-click)."""
        if self.scheduler is not None:
            self.scheduler.cancel(item_id)
        else:
            self.recording_schedule.cancel(item_id)
        # i18n: a toast: a scheduled recording was cancelled
        self.screens.notify(tr("Recording cancelled."))
        self._schedule_changed()

    def schedule_changed(self) -> None:
        """2.9.7: the schedule was changed without the scheduler (the Recordings page's Edit
        when QiTV can't record): as its own change."""
        self._schedule_changed()

    def _schedule_changed(self) -> None:
        """The guide's dots, the Library's Recordings shelf and the guides in use follow."""
        if self._closing:
            return
        self.overlays.guide.update()
        self.screens.recordings.set_scheduled(self.recording_schedule.items())
        self.screens.recordings.refresh()  # a recording that ended is a file now
        self.controller.update_guides()

    def parental_changed(self) -> None:
        """Parental controls changed (a PIN, locked or not, hidden or shown again; 2.8): the
        channel list, the guide and the Library show what may show now."""
        if self._closing:
            return
        self.controller.parental_changed()
        self.screens.library_model.changed.emit()
        # Unlocked for a while: locked again when it runs out (a hidden channel stops then).
        remaining = self.parental.unlocked_for()
        if remaining > 0:
            self._relock.start(int(remaining * 1000) + 250)
        else:
            self._relock.stop()
        # 2.9.7: the Recordings tab hides a hidden channel's recordings while locked
        self.screens.library_controller.recordings_changed()

    def note_recording(self, path: str, channel: dict, title: str, start: float) -> None:
        """2.9.7: a recording started with R: what its file holds, in the recordings index."""
        self.recordings_index.note(
            path,
            title=title,
            channel=str(channel.get("name") or ""),
            provider=item_provider.provider_name(self.catalog, channel),
            channel_id=str(channel.get("id") or ""),
            logo=str(channel.get("logo") or ""),
            start=start,
            stop=None,
        )

    def note_recording_end(self, path: str) -> None:
        """2.9.7: a player's recording ended now: its file holds until here."""
        self.recordings_index.ended(path, time.time())

    def attach_scheduler(self, scheduler: Any) -> None:
        """The scheduled recordings (2.8): the Library and the guides follow them; what records
        shows on the REC sign, which stops it."""
        self.scheduler = scheduler
        controller = self.controller
        scheduler.changed.connect(self._schedule_changed)
        scheduler.changed.connect(controller.scheduled_changed)
        self.screens.recordings_desk.follow_scheduler(scheduler)  # 2.9.7: the Recordings page
        controller.extra_guides = scheduler.providers
        controller.recording_user_stop = scheduler.stopped_by_user  # 2.9.7: hosted runs
        controller.scheduled_host = scheduler.host_of
        if controller.sessions is not None:  # 2.9.7: a background player the next one wants
            controller.sessions.wanted = scheduler.wants
        controller.scheduled_now = scheduler.recording_now
        controller.stop_scheduled = scheduler.stop_now
        controller.lend_scheduled = scheduler.lend  # 2.9.7: watched with no second stream
        controller.lendable_scheduled = scheduler.lendable
        scheduler.players = controller.players_showing  # 2.9.7: records what a player shows
        controller.record_at = self.record_at  # a channel's right-click: Record at a time

    def _stop_scheduler(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown()
            recording_schedule_flush(2.0)  # what it noted reaches the disk before QiTV goes

    @staticmethod
    def _screen_is_4k() -> bool:
        """Whether a screen shows 4K (its pixels, at least 3,800 across): a Jellyfin or Emby
        title's 4K version plays then, by default (2.8)."""
        for screen in QGuiApplication.screens():
            size = screen.size()
            if max(size.width(), size.height()) * screen.devicePixelRatio() >= 3800:
                return True
        return False

    def _media_servers(self) -> list[dict]:
        """The Jellyfin and Emby providers' entries (2.8: the reporter tells them)."""
        servers = []
        for name in self.catalogs.names():
            try:
                entry = self.catalogs.provider(name)
            except KeyError:
                continue
            if is_media_server(entry.get("type")):
                servers.append(entry)
        return servers

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

    def _picture_shape(self, name: str) -> Optional[float]:
        """The shape (width / height) slot `name`'s picture shows in (None: not known yet, or
        it fits any): the picture in picture's boxes take it."""
        for slot_name, player in self._players():
            if slot_name == name:
                return player.picture_ratio()
        return None

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
        """The pages moved: in the corner only its controls stay on the host (and a song's
        screen, small: its cover and title), and the picture-in-picture frame leaves the mouse
        to the corner player."""
        corner = mode == PageStack.CORNER
        self.pip_frame.set_interactive(not corner)
        self.audio_screen.set_compact(corner)
        self.host.show_only(
            self.corner_controls if corner else None, also=(self.audio_screen,) if corner else ()
        )
        if corner:
            self.corner_controls.set_muted(self.controller.main_muted())
            self.corner_controls.set_level(self.controller.main_level())
            self.corner_controls.show()
        else:
            self.corner_controls.hide()
            if mode == PageStack.PLAYER and not self.pip_frame.small_rect().isEmpty():
                self.pip_label.show()  # the corner hid it; the PiP only shows it when it opens
            transport = self.overlays.transport
            if mode == PageStack.PLAYER and transport.audio():
                transport.poke()  # the song's dock, which the corner hid (it never fades)

    def _controls_hold_pointer(self, at: QPoint) -> Optional[bool]:
        """The pointer and the player controls (PointerHider): None while they fade by
        themselves; the audio dock never does, and holds the pointer only while it rests on
        the dock's band (in fullscreen Stage it goes after the wait elsewhere)."""
        transport = self.overlays.transport
        if not transport.audio():
            return None
        return transport.hovers_controls(QPointF(transport.mapFromGlobal(at)))

    def open_url(self, url: str) -> None:
        """A web page in the browser (OpenSubtitles' sign-up and API key page)."""
        if not self._closing:
            system_open.open_url(url)

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

    def remote_key(self, key: int, text: str = "") -> None:
        """A phone remote's press of `key` (no modifier): the same path as the keyboard's."""
        if self._closing:
            return
        resolved = resolve(key, Qt.KeyboardModifier.NoModifier, text, context=self._key_context())
        if resolved is None:
            return
        action, payload = resolved
        with self.screens.from_keys():
            self._act(action, payload, key=key)

    def _show_now_playing(self, state: str) -> None:
        """The system's media panel: what the large picture plays, in `state`; a song on the
        audio screen with its artist, album and cover, a radio channel with its logo (as the
        screen shows them: TVController.panel_facts)."""
        if self._closing:
            return
        self._panel_state = state
        target = self.controller.now_playing()
        facts = self.controller.panel_facts() if target is not None else None
        if target is None:
            self.media_keys.show(state, "", False)
        elif facts is None:  # a film, a channel with its picture: a video, its title alone
            self.media_keys.show(state, target.title, target.live)
        else:  # sound: a song (tagged or not, with a cover or not), a radio channel
            title, artist, album = facts
            # The screen's cover as a file (and its bytes, for macOS): the last one until a
            # new picture's is written, none for QiTV's drawn stand-in.
            art_path, art_bytes = self.panel_cover.file_for(self.audio_screen.cover())
            self.media_keys.show(
                state,
                title or target.title,
                target.live,
                artist=artist,
                album=album,
                art_path=art_path,
                art_bytes=art_bytes,
                music=True,
            )

    def _refresh_now_playing(self) -> None:
        """The audio screen's song or cover changed, or the cover's file came: the panel
        again, in the state it heard last."""
        self._show_now_playing(self._panel_state)

    def remote_action(self, action: Action, payload: str = "") -> None:
        """A phone remote's button that means one action wherever the TV is (Search, Fullscreen)."""
        if self._closing:
            return
        if action is Action.FULLSCREEN and not app_active():
            # macOS ignores a fullscreen change asked of a window in the background: QiTV comes
            # forward first, and changes once in front (or after a moment, whatever happened).
            self.raise_()
            self.activateWindow()
            self._when_in_front(lambda: self._remote_act(action, payload), FORWARD_WAIT_MS)
            return
        self._remote_act(action, payload)

    def _remote_act(self, action: Action, payload: str) -> None:
        if self._closing:
            return
        with self.screens.from_keys():
            self._act(action, payload, key=0)

    def _when_in_front(self, then: Callable[[], None], left_ms: int) -> None:
        if app_active() or left_ms <= 0:
            then()
            return
        after = left_ms - FORWARD_POLL_MS
        QTimer.singleShot(FORWARD_POLL_MS, self, lambda: self._when_in_front(then, after))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        resolved = None if self._closing else self._resolve(event)
        if resolved is None:
            super().keyPressEvent(event)
            return
        action, payload = resolved
        event.accept()
        self.media_keys.keyboard(as_int(event.key()))  # its copy from the system is this press
        with self.screens.from_keys():  # a page a key shows gets no second click
            self._act(action, payload, key=as_int(event.key()))

    def _act(self, action: Action, payload: str, key: int) -> None:
        if action is Action.ESCAPE and self.exports.cancel():
            return  # Esc while exporting cancels the export
        if self.controller.answer_key(action):
            return  # 2.6: QiTV asks (stop a recording?): its answers' keys, on every page
        if self.screens.library_shown():
            handled = self.library_controller.handle(action, payload)
            if not handled and action is Action.FULLSCREEN:
                self.toggle_fullscreen()  # F11, ⌃⌘F: the Library's other keys are its own
            return
        if self.more_menu.handle(action):
            return  # Esc closes the audio dock's ⋯ menu, and only it (other keys close it, on)
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

    # ---- opened files -------------------------------------------------------------------------

    def open_paths(self, paths: Sequence[str], *, source: str) -> None:
        """Files opened in QiTV, from a `source` (LAUNCH, HANDOVER, DROP, DIALOG or SHELF).

        - Playlist files are read first, off the GUI thread (_on_playlists_read): one with a
          network link becomes a provider, as a dropped one does; one of local files plays as
          a list; an HLS one plays as one film. A media file opened with them plays instead.
        - So is a path of no kind QiTV knows (only the disk tells a folder from a file): a
          folder plays as a list of its own files (services.local_playlist.read_folder), from
          the one the viewer stopped part-way into (_where_it_starts); a file QiTV won't play
          says so, the first one named. A media file opened with them plays instead.
        - The first media file by name plays once a thread found it there (it is checked off
          the GUI thread); one that is gone or can't be read says so, and nothing else changes.
          The subtitle files with it load once it opened; without one, they go to what plays.
        - Several media files dropped together play as a list, in a folder's order, read on
          that thread too (read_files); a launch's or a hand-over's play one, as below.
        - The file that plays already only comes forward.
        - A start's file within OPEN_BURST_S of the one a start chose (a multi-select, one
          start per file) replaces it only when it sorts first by name; else it is left out.
        - Open File plays what it picks, whatever its kind (an HLS playlist too).
        - A Folders tile's folder, and one Add folder picks (SHELF), is read and played as a
          dropped one; Esc (and the player's ‹ Library) then goes back to that tab, as a
          Recordings tile's does (P4-26, P4-29).
        - As a media file is played, QiTV's mark draws in the middle of the page until its
          picture shows (ui/overlays/opening.py), unless a picture shows already.

        A launch or a hand-over brings the window forward. The paths are passed on as given.
        While QiTV closes nothing opens (the log names each film left out).

        From the moment a file is to be looked at, nothing opens over it (the controller's
        hold_for_file): no last channel, no drawer, no welcome card, and what waits for QiTV to
        be idle (when_idle) waits. A file that's gone lets go, unless a file plays on, and so
        does a start with nothing to play (main() holds for a folder, and a file QiTV won't
        play, too: until it is read).
        """
        if self._closing:
            for path in sort_paths(paths).media:
                name = os.path.basename(path)
                logger.info("QiTV is closing: a file it was handed isn't opened (%s)", name)
            return
        started = source in (LAUNCH, HANDOVER)
        if started:
            self.come_to_front()
        kinds = Sorted(tuple(paths[:1]), (), ()) if source == DIALOG else sort_paths(paths)
        if kinds.playlists or kinds.others:
            playable = not kinds.media
            if started and playable:
                # A start waits for its playlist or folder (main() held it before the window
                # showed; a hand-over may come before the start ran): nothing of it is spent
                # until the read says what it is (_on_playlists_read). Once it ran, nothing.
                self.controller.hold_for_read()
            self._read_playlists(
                [*kinds.playlists, *kinds.others],
                playable=playable,
                tentative=playable and not kinds.playlists,
                library=source == SHELF,
            )
        subtitles = list(kinds.subtitles)
        if not kinds.media:
            if subtitles:
                self.controller.drop_subtitles(subtitles)
            if (
                not kinds.others
                and not self._reads_pending()
                and self._open_due is None
                and self._playing_file() is None
            ):
                # Nothing to play: a start's hold ends (one whose playlist or folder is still
                # read, once that read is over: _on_playlists_read).
                self.controller.hold_for_file(False)
            return
        path = kinds.media[0]
        if started:
            now, burst = self._clock(), self._burst
            if (
                burst is not None
                and now - burst[0] < OPEN_BURST_S
                and natural_key(path) >= natural_key(burst[1])
            ):
                logger.info("A file opened with an earlier one by name is left out")
                return
            self._burst = (now, path)
        else:
            self._burst = None  # a drop or Open File is a choice of its own
        self._open_generation += 1
        self._open_due = None  # a file still looked at is not played
        if self._reads_pending():
            self._playlist_due = None  # nor a playlist still read,
            self._tentative.clear()  # nor a folder
            self.controller.read_over(as_file=True)  # a start that waited for it is the film's
        if source == DROP and len(kinds.media) > 1:
            # Several dropped together: a list, whose order and sizes are read off the GUI
            # thread (a songs' tags too). Held, as a playlist is, once it plays.
            self._read_playlists([], playable=True, files=kinds.media, subtitles=subtitles)
            return
        if self._plays_now(path):
            if not started:
                self.come_to_front()
            if subtitles:
                self.controller.drop_subtitles(subtitles)
            return
        self._open_due = (self._open_generation, path, subtitles)
        self.controller.hold_for_file(True)  # while it is looked at, it counts as playing
        self._file_check.start(self._open_generation, path)

    def _read_playlists(
        self,
        paths: list[str],
        *,
        playable: bool,
        links: Sequence[ProviderLink] = (),
        files: Sequence[str] = (),
        subtitles: Sequence[str] = (),
        tentative: bool = False,
        library: bool = False,
    ) -> None:
        """Playlist files and paths of no known kind (folders, maybe) opened in QiTV (`paths`),
        and media `files` dropped together, read on a thread (_on_playlists_read). With
        `playable` (no media file came with them), the first that names local files or is one
        HLS stream plays once read, unless a newer choice came meanwhile. A running QiTV holds
        nothing while they are read: what was chosen before, a film still looked at too, is
        left alone until one turns out to play; a start waits (hold_for_read). Provider
        `links` dropped with them are added with their providers, in one go; the `subtitles`
        dropped with `files` load into the entries they are for (list_entry_opens).

        A `tentative` read (paths of no known kind alone) is no choice until one of them turns
        out to play: a file QiTV won't play only says so, and leaves alone what is read before
        it. Without `playable`, a folder's songs' tags are not read (nothing of it plays).
        With `library` (a Folders tile's, P4-26), what plays of it goes back to the Library."""
        self._playlist_reads += 1
        serial = self._playlist_reads
        if library:
            self._shelf_reads.add(serial)
        if tentative:
            self._tentative.add(serial)
        elif playable:
            self._tentative.clear()  # a folder still read gives way to this newer choice
            self._playlist_due = serial
        if links:
            self._read_links[serial] = list(links)
        if subtitles:
            self._read_subtitles[serial] = list(subtitles)
        self._playlist_read.start(serial, paths, files, tags=playable)

    def _reads_pending(self) -> bool:
        """A playlist, a folder or files dropped together are still read, and may play."""
        return self._playlist_due is not None or bool(self._tentative)

    def _on_playlists_read(self, serial: int, reads: list[tuple[str, PlaylistRead]]) -> None:
        """Playlist files and folders read (_read_playlists): each playlist with a network link
        becomes a provider, as a dropped one does, with the links dropped beside them (adding
        switches the list to the last), and a file QiTV won't play says so. Then, unless a
        newer choice came:
        - an HLS one plays as one film, as Open File plays it;
        - one of local files plays as a list (TVController.play_list), held and drawn over as
          a file opened in QiTV is; the entries that aren't there are counted in a toast;
          a folder's, and several files dropped together, play from the entry the viewer
          stopped part-way into (_where_it_starts), the whole list kept;
        - one with none of its files there says so, and so does a folder with none of its
          own, and one that can't be read ("Couldn't open"); nothing plays.
        Of several, the first that plays does. A start that waited for them (hold_for_read) is
        held as a film's by any of these; by providers alone, or a file QiTV won't play, it
        starts as any start does (held as a film's by main() for such a file all the same).
        A tentative read (_on_paths_read) is the choice only once it plays."""
        if self._closing:
            return
        links = self._read_links.pop(serial, [])
        library = serial in self._shelf_reads  # a Folders tile's: Esc goes back to its tab
        self._shelf_reads.discard(serial)
        subtitles = self._read_subtitles.pop(serial, [])
        providers = [path for path, read in reads if read.kind == PROVIDER]
        unplayable = [(path, read) for path, read in reads if read.kind == OTHER]
        if unplayable:
            self._say_unplayable(*unplayable[0])  # whatever plays, as a film's open says it
        playable = [(path, read) for path, read in reads if read.kind in (LOCAL, HLS)]
        failed = [(path, read) for path, read in reads if read.kind in (EMPTY, UNREADABLE)]
        if serial in self._tentative:
            self._tentative.discard(serial)
            self._on_paths_read(serial, playable, failed, subtitles, library=library)
            self._add_read_providers(providers, links)  # the links dropped with its folders
            return
        if self._playlist_due != serial:
            if any(read.kind not in (PROVIDER, OTHER) for _path, read in reads):
                logger.info("A list opened in QiTV gave way to a newer choice")
            self._add_read_providers(providers, links)
            return
        self._playlist_due = None
        as_file = bool(playable or failed)
        if as_file or not self._tentative:
            # The start that waited: a film's from here, or a plain start before the
            # providers (unless a folder opened after it is still read: that one says).
            self.controller.read_over(as_file=as_file)
        self._add_read_providers(providers, links)
        if not playable:
            if failed:
                self._say_unplayable(*failed[0])
            self._nothing_plays()
            return
        self._play_read(playable, subtitles, library=library)

    def _on_paths_read(
        self,
        serial: int,
        playable: list[tuple[str, PlaylistRead]],
        failed: list[tuple[str, PlaylistRead]],
        subtitles: list[str],
        *,
        library: bool = False,
    ) -> None:
        """A tentative read (paths of no known kind alone) is back, no newer choice made since:
        a folder of it that plays is the choice now (what is read before it gives way, a
        folder read before it too); otherwise it says why, and leaves alone what is still
        read."""
        if playable:
            self._tentative = {older for older in self._tentative if older > serial}
            self._playlist_due = None  # a playlist read before it gives way
            self.controller.read_over(as_file=True)  # a start that waited: the list's
            self._play_read(playable, subtitles, library=library)
            return
        if failed:
            self._say_unplayable(*failed[0])
        if not self._reads_pending():
            self.controller.read_over(as_file=bool(failed))
            self._nothing_plays()

    def _nothing_plays(self) -> None:
        """What was read plays nothing: a start held for it goes on, as an open with nothing to
        play lets go, unless a file or another read is still on its way, or a file plays."""
        if not self._reads_pending() and self._open_due is None and self._playing_file() is None:
            self.controller.hold_for_file(False)

    def _play_read(
        self,
        playable: list[tuple[str, PlaylistRead]],
        subtitles: list[str],
        *,
        library: bool = False,
    ) -> None:
        """The first read that plays (`playable`) plays: an HLS playlist as one film, a list of
        files as a list, the `subtitles` dropped with it for the entries they are for. With
        `library` (a Folders tile's), Esc goes back to the Library's Folders tab."""
        if len(playable) > 1:
            logger.info("Playlists opened together: the first plays")
        path, read = playable[0]
        if read.kind == HLS:
            self.open_paths([path], source=DIALOG)  # one stream: a film, as Open File plays it
            return
        self._open_due = None  # a file still looked at is not played: the list came after it
        playlist = _is_playlist(path)
        start = None if playlist else self._where_it_starts(read)
        first = start or 0
        self._entry_subtitles = subtitles_for(read.entries, subtitles, first) if subtitles else {}
        # Plan 4: a folder that plays as one goes on the Folders tab (at its front, once); a
        # playlist's or several files' list never puts its folder there.
        folder = _is_folder_read(path)
        self._shelf_folder = shelf_key(path) if folder else None
        if folder:
            entry = os.path.basename(read.entries[first])
            self.folder_shelf.add(path, read.name, folder_kind(read.entries), entry)
        self._play_opened(
            read.entries[first],
            lambda: self.screens.play_list(
                read.name, read.entries, read.sizes, start, from_library=library
            ),
        )
        if read.missing and not playlist:
            logger.info("Files opened together play without %d of them", read.missing)
        elif read.missing:
            logger.info("A playlist plays without %d of its entries", read.missing)
            text = tr_n(
                # i18n: a toast as a playlist plays: {n} of its files aren't there (skipped)
                "{n} file in the playlist is missing",
                "{n} files in the playlist are missing",
                read.missing,
                n=number(read.missing),
            )
            self.screens.notify(text, "info", ERROR_MS)

    def _add_read_providers(self, files: list[str], links: list[ProviderLink]) -> None:
        """The read's provider playlists and the links dropped with them become providers, in
        one go, afterwards as a drop's are: after the window's start (it is posted before), as
        adding switches the list, and a portal's form is a dialog."""
        if files or links:
            QTimer.singleShot(0, self, lambda: self.add_dropped_providers(files, links))

    def _say_unplayable(self, path: str, read: PlaylistRead) -> None:
        """A playlist or folder that plays nothing says why: it couldn't be read (gone,
        refused), as a file that couldn't be opened says it, or none of its files are there
        (none of a folder's own). A file of no kind QiTV knows says QiTV can't play it."""
        name = _shown_name(path)
        if read.kind == OTHER:
            kind = os.path.splitext(name)[1] or "no extension"
            logger.info("A file QiTV can't play was opened in it (%s)", kind)
            # i18n: an error toast: a file opened in QiTV isn't a kind it plays ({name}: its name)
            self.screens.notify(tr("QiTV can't play “{name}”", name=name), "error", ERROR_MS)
        elif read.kind == UNREADABLE:
            logger.info("A playlist or folder opened in QiTV is gone or can't be read")
            # i18n: an error toast: a file opened in QiTV is gone or unreadable ({name}: its name)
            self.screens.notify(tr("Couldn't open “{name}”", name=name), "error", ERROR_MS)
        elif _is_playlist(path):
            logger.info("None of a playlist's files are there")
            # i18n: an error toast: a playlist opened in QiTV names no file that is there
            text = tr("None of the files in this playlist are here")
            self.screens.notify(text, "error", ERROR_MS)
        else:
            logger.info("A folder opened in QiTV has no media file of its own")
            # i18n: an error toast: a folder opened in QiTV has no video or music ({name}: its name)
            text = tr("No videos or music in “{name}”", name=name)
            self.screens.notify(text, "error", ERROR_MS)

    def _where_it_starts(self, read: PlaylistRead) -> Optional[int]:
        """Where a folder's list (or several files dropped together) starts: at the first entry
        the viewer stopped part-way into (one with a resume point: neither barely begun nor
        seen to its end), which resumes there as any file does; else (Plan 4, Task 12) at the
        one after the last entry seen to its end; None when there is neither (it starts as any
        list: its first entry, or a song of its own picking when shuffled). The Folders tab's
        "next" is this entry (folders_view.where_it_starts, one rule)."""
        return where_it_starts(self.watch, read.entries, read.sizes)

    def list_entry_opens(self, target: Target, path: str) -> None:
        """A list's entry at `path` opens as `target` (TVController._play_entry): the subtitle
        files dropped for it load once it opened (subtitles_for), as a video's dropped with
        it do; each time it opens (Play again, back to it), until another list or file plays.
        A folder's entry is kept as its last on the Folders tab (Plan 4)."""
        folder = os.path.dirname(path)
        if self._shelf_folder is not None and shelf_key(folder) == self._shelf_folder:
            self.folder_shelf.played(folder, os.path.basename(path))  # Plan 4: its last entry
        subtitles = self._entry_subtitles.get(path)
        if subtitles and target.local:
            self._subtitles_due = (target, subtitles, self._clock() + OPENED_WAIT_S)
            self._subtitles_wait.start()

    def _plays_now(self, path: str) -> bool:
        """Whether the file at `path` is what the large picture plays (opening, playing or
        paused; one that ended or failed opens again)."""
        target = self._playing_file()
        if target is None:
            return False
        playing = QUrl(str(target.item.get("cmd") or "")).toLocalFile()
        return bool(playing) and _same_file(playing, path)

    def _playing_file(self) -> Optional[Target]:
        """The file the large picture plays (opening, playing or paused), if it plays one."""
        target = self.controller.now_playing()
        local = target is not None and target.local and self.controller.playing()
        return target if local else None

    def _on_file_checked(self, generation: int, path: str, size: Optional[int]) -> None:
        due = self._open_due
        if self._closing or due is None or due[0] != generation:
            return  # QiTV closes, or another file was opened since
        self._open_due = None
        if size is None:
            logger.info("A file opened in QiTV is gone or can't be read")
            name = os.path.basename(path)
            # i18n: an error toast: a file opened in QiTV is gone or unreadable ({name}: its name)
            self.screens.notify(tr("Couldn't open “{name}”", name=name), "error", ERROR_MS)
            if self._playing_file() is None:  # a film that plays on keeps its hold
                self.controller.hold_for_file(False)
            return
        self._entry_subtitles = {}  # a list's, for its entries: never this file's
        self._shelf_folder = None  # nor its folder's, on the Folders tab
        target = self._play_opened(path, lambda: self.screens.play_path(path, size))
        if due[2] and target is not None and target.local:
            self._subtitles_due = (target, due[2], self._clock() + OPENED_WAIT_S)
            self._subtitles_wait.start()

    def _play_opened(self, path: str, play: Callable[[], None]) -> Optional[Target]:
        """A file opened in QiTV and found there plays (`play`: the player page's play_path, or
        play_list with `path` its first entry): held, with QiTV's mark over it as it opens.
        What plays then (the controller's target)."""
        # Held again: what let go of the hold while it was looked at (a film before it that
        # ended, the link of a channel chosen before it) leaves it this file's. A choice made
        # after it dropped the file instead (file_gives_way).
        self.controller.hold_for_file(True)
        before = self.controller.now_playing()
        # QiTV's mark draws while it opens, unless a picture shows (a film's, a channel's) or a
        # song's screen does for the song after it (it stays; a radio channel's goes, so the
        # mark draws): an error as it is played (opening_over) leaves it out.
        self._opening_due = not self.controller.session.shows_picture() and not (
            is_music(path) and self.controller.song_shown()
        )
        play()
        target = self.controller.now_playing()
        if self._opening_due and target is not None and target is not before and target.local:
            session = self.controller.session  # the large picture's, which plays it
            self.opening_overlay.open_for(
                session,
                str(target.item.get("cmd") or ""),
                sound_only=is_music(path) or session.casting(),
            )
        self._opening_due = False
        return target

    def opening_over(self) -> None:
        """What the large picture opens is no longer the file QiTV's mark draws for: it
        failed or ended, another title was chosen, or Stop was pressed (TVController). The
        mark opens out of the page (and one about to show for it does not)."""
        self._opening_due = False
        self.opening_overlay.close_opening()

    def file_gives_way(self) -> None:
        """Something other than a file was chosen for the large picture since a file was
        opened (a channel, a provider's film, a swap, a phone's Watch or an outside player:
        TVController): the file still being looked at (a slow share) isn't played when its
        check is back; the later choice wins. Its hold goes with it unless a film still plays
        (that one's hold goes once it gives way, ends or stops). A playlist or folder still
        read plays nothing either (its links still make a provider), and a start that waited
        for it starts."""
        if self._reads_pending():
            self._playlist_due = None
            self._tentative.clear()  # nor does a folder
            self.controller.read_over(as_file=False)
        if self._open_due is None:
            return
        self._open_due = None
        logger.info("A file opened in QiTV gave way to a newer choice before it played")
        if self._playing_file() is None:
            self.controller.hold_for_file(False)

    def when_idle(self, callback: Callable[[], None]) -> None:
        """`callback` now, or once the file opened in QiTV is over (it ended, was stopped,
        failed or gave way to another title): the startup update question waits for it."""
        self.controller.when_idle(callback)

    def _load_due_subtitles(self) -> None:
        """The subtitle files dropped with a video load once it opened, never into another
        title; they are left after OPENED_WAIT_S, or when it fails."""
        due, controller = self._subtitles_due, self.controller
        if due is None or self._closing:
            self._stop_subtitles_wait()
            return
        target, subtitles, until = due
        if controller.now_playing() is not target:
            self._stop_subtitles_wait()  # another title was chosen
        elif controller.session.state() in _OPENED_STATES:
            self._stop_subtitles_wait()
            controller.drop_subtitles(subtitles)
        elif not controller.playing() or self._clock() > until:
            self._stop_subtitles_wait()  # it failed, or never opened

    def _stop_subtitles_wait(self) -> None:
        self._subtitles_due = None
        self._subtitles_wait.stop()

    def subtitles_due(self) -> bool:
        """Subtitle files opened with the video playing large are still to load into it (once
        it plays): AI captions don't come on by themselves over them (TVController)."""
        due = self._subtitles_due
        return due is not None and due[0] is self.controller.now_playing()

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
        """Dragged over the window (or its overlays): taken only when it may load: a media
        file, subtitle files for what plays, a playlist file or a provider's link (2.2), or a
        file of no kind QiTV knows: a folder, maybe, which only the disk tells (Task 11; it is
        looked at once dropped, and one that is no folder says QiTV can't play it)."""
        paths = self._dropped_paths(event)
        files, links = self._dropped_providers(event)
        kinds = sort_paths(paths)  # by its name: the disk is looked at once dropped
        subtitles = self.controller.accepts_subtitles(paths)
        if not self._closing and (kinds.media or kinds.others or subtitles or files or links):
            event.acceptProposedAction()
        else:
            event.ignore()

    def _on_drop(self, event: QDropEvent) -> None:
        """A media file plays, and the subtitle files dropped with it load for it (several
        play as a list); subtitle files alone go to what plays (open_paths). Playlist files and
        folders are read first, as opened ones are (_read_playlists): their links make a
        provider, local files play as a list. Provider links become providers, with the
        playlists' when some came along (one toast, the link's shown last)."""
        if self._closing:
            event.ignore()
            return
        kinds = sort_paths(self._dropped_paths(event))
        subtitles = bool(kinds.subtitles) and (
            bool(kinds.media) or self.controller.accepts_subtitles(kinds.subtitles)
        )
        if kinds.media or subtitles:
            self.open_paths([*kinds.media, *kinds.subtitles], source=DROP)
        files, links = self._dropped_providers(event)
        read = [*files, *kinds.others]  # the others: folders, maybe (only the disk tells)
        if read:
            playable = not kinds.media
            tentative = playable and not files
            self._read_playlists(read, playable=playable, links=links, tentative=tentative)
        elif links:
            # After the drop: adding switches the list, and a portal's form is a dialog.
            QTimer.singleShot(0, self, lambda: self.add_dropped_providers([], links))
        added = bool(read or links)
        if kinds.media or subtitles or added:
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
            QTimer.singleShot(0, self, self.remote.apply_config)  # a phone remote, if it's on
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

    def set_keep_on_top(self, on: bool) -> None:
        """View → Keep on Top (2.8.5): the window stays above other apps' windows. Saved; the
        mini player is on top anyway and leaves it as chosen; in fullscreen (on its way to
        it, or back) or minimized, the change waits for the window to be a normal one again."""
        self.config_manager.keep_on_top = on
        try:
            self.config_manager.save_config()
        except Exception as e:  # a locked config: kept for this run only
            logger.warning("Couldn't save Keep on Top: %s", safe_message(e))
        self._apply_keep_on_top()

    def _apply_keep_on_top(self) -> None:
        on = bool(getattr(self.config_manager, "keep_on_top", False))
        hint = Qt.WindowType.WindowStaysOnTopHint
        if self._mini_restore is not None:  # the flags the mini player ends with
            geometry, flags, maximized = self._mini_restore
            self._mini_restore = (geometry, flags | hint if on else flags & ~hint, maximized)
            return
        if self._closing or self._fs_target is not None or self.isFullScreen():
            return  # applied once fullscreen ends (`_fit_to_screen`)
        if self.isMinimized():
            return  # showing it again would bring the window back: applied when it is back
        if bool(self.windowFlags() & hint) == on:
            return
        if not self.isVisible():
            self.setWindowFlag(hint, on)  # before the first show: nothing to show again
            return
        maximized = self.isMaximized()
        geometry = self.normalGeometry() if maximized else self.geometry()
        self.setWindowFlag(hint, on)
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
        return self._one_picture_plays()

    def _may_float_covered(self) -> bool:
        """The large picture may float while the window is out of sight: a window shown (not
        fullscreen, nor on its way there or back, nor the mini player, on top anyway), its
        setting on, Keep on Top off, and one picture playing on the player page."""
        if self._closing or not self.isVisible() or self.isFullScreen():
            return False
        if self._fs_target is not None or self._mini_restore is not None or self._mini_waiting:
            return False
        if not getattr(self.config_manager, "covered_pip", False):
            return False
        if getattr(self.config_manager, "keep_on_top", False):
            return False  # it stays above other apps' windows: never floats
        return self._one_picture_plays()

    def _one_picture_plays(self) -> bool:
        """One picture (no multiview grid, no dialog) on the player page, playing or paused."""
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
            self._caption_the_floating_picture()
            host = self._floating_captions
            if host is not None:
                # Once the floating window is placed: its Show came before (2.9, the review:
                # the captions sat where it floated last time for a moment).
                QTimer.singleShot(0, host, host.sync_geometry)
        else:
            note.hide()

    def _caption_the_floating_picture(self) -> None:
        """2.9: AI captions over the floating picture too (the owner: "no subs in pip"); the
        window's own show on its desktop, away. As over the window, a see-through window of
        their own above it: libVLC's view covers any widget inside it. It lets the mouse
        through (the floating picture is dragged and double-clicked there)."""
        player = self.desktop_pip.floating_window()
        if player is None or self._floating_captions is not None:
            return
        host = OverlayHost(player, player)
        host.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
        overlay = CaptionsOverlay(host)
        host.add_overlay(overlay)
        macos_window.join_all_spaces(host)  # beside the floating picture on every desktop
        self._floating_captions = host
        self.captions.add_overlay(overlay)

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
        """After fullscreen or the mini player changed: the menu bar and the transport's way
        back."""
        mini = self._mini_restore is not None
        bar = self.menuBar()
        if not bar.isNativeMenuBar():  # the macOS menu bar is outside the window
            bar.setVisible(not self.isFullScreen() and not mini)
        self.overlays.transport.set_window_mode(mini=mini, fullscreen=self.isFullScreen())

    def _desktop_pip_reason(self) -> Optional[str]:
        """What the desktop picture in picture asks about (macOS): fullscreen, the desktop
        shown; a window with its setting on, whether it can be seen; else nothing."""
        if self._closing or not _macos_desktops():
            return None
        if self.isFullScreen():
            return DESKTOP
        if getattr(self.config_manager, "covered_pip", False):
            return COVERED
        return None

    def _arm_desktop_pip(self) -> None:
        """Ask what is to be asked now (a state change, Settings); another reason, or none,
        puts a floating picture back first."""
        reason = self._desktop_pip_reason()
        self.desktop_pip.arm(reason)
        if reason == COVERED:
            self.desktop_pip.nudge()  # minimized, or back from the Dock: asked at once

    def _on_app_state(self, _state: Qt.ApplicationState) -> None:
        if not self._closing and self.desktop_pip.reason() == COVERED:
            self.desktop_pip.nudge()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.ActivationChange:
            self.desktop_pip.nudge()  # a desktop change takes or gives the focus
        if event.type() == QEvent.Type.WindowStateChange:
            self._update_menu_bar()
            # Fullscreen: the desktop shown is asked about (the picture may float); a window,
            # whether it can be seen (with its setting); else the picture is back, and nothing
            # is asked.
            self._arm_desktop_pip()
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
                elif event.oldState() & Qt.WindowState.WindowMinimized and not self.isMinimized():
                    QTimer.singleShot(0, self, self._apply_keep_on_top)  # chosen while minimized
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
        # Keep on Top chosen in fullscreen: once this fit is done, however fullscreen ended.
        QTimer.singleShot(0, self, self._apply_keep_on_top)
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

    def options_dialog(self, tab: str = "") -> None:
        """Settings; `tab` "remote" opens it on Phone remote."""
        if not self._closing:
            dialog = OptionsDialog(self)
            if tab == "remote":
                dialog.options_tab.setCurrentWidget(dialog.remote_tab)
            dialog.exec()
            self.menus.rebuild_providers()
            self.screens.tmdb.settings_changed()  # 2.8: TMDB turned off or on
            self._arm_desktop_pip()  # the covered window's setting turned on or off

    def open_phone_remote(self) -> None:
        """The menu's Phone Remote…: turns the remote on if it is off, then the pairing QR."""
        if self._closing:
            return
        if self.remote.hub is None:
            self.remote.turn_on()
            if self.remote.hub is None:  # settings of a newer QiTV: its Settings tab says so
                self.options_dialog(tab="remote")
                return
        if self.screens.library_shown():
            self.screens.back_to_tv()  # the QR shows over the TV
        self.pair_overlay.open()

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
        self.screens.tmdb.settings_changed()  # 2.8: TMDB turned off or on
        self._arm_desktop_pip()  # the covered window's setting turned on or off

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
        name, existing = self._add_provider_entry(entry, free_channels.same_list)
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

    def providers_renamed(self, renames: Renames, removed: Sequence[str] = ()) -> None:
        """Settings saved the provider list with providers renamed or removed (called before
        `providers_saved`, while the catalogs under the old names still hold their lists): a
        removed provider's scheduled recordings go, said in a toast (they could never record
        again); what is kept under a renamed one's old name moves to its new name."""
        if removed:
            self._drop_recordings_of(removed)
        if not renames:
            return
        contents = {
            old: self.catalogs.catalog(old).provider_manager.current_provider_content
            for old in renames.names
            if old in self.catalogs.created()
        }
        schedule: Any = self.scheduler if self.scheduler is not None else self.recording_schedule
        hooks: list[Callable[[Renames], Any]] = [
            self.parental.rename_providers,
            self.hidden_groups.rename_providers,  # 2.9.1
            schedule.rename_providers,  # the scheduler's: its recordings under way too
            self.budget.rename_providers,
            self.remote.rename_providers,
        ]
        carry_over(self.config_manager, renames, hooks)
        self._rekey_hashed(renames, contents)
        self._schedule_changed()

    def _drop_recordings_of(self, removed: Sequence[str]) -> None:
        dropped = self.recording_schedule.drop_providers(removed)
        if not dropped:
            return
        if self.scheduler is not None:
            for item in dropped:  # one recording now stops (what it has is kept)
                self.scheduler.cancel(str(item.get("id") or ""))
        titles = ", ".join(
            str(item.get("title") or "")
            or (slot_text(item["begin"], item["minutes"]) if "begin" in item else "")
            for item in dropped
        )
        logger.info("%d scheduled recording(s) of a removed provider went.", len(dropped))
        self.screens.notify(translate(DROPPED_RECORDINGS_TEXT, titles=titles), "error", ERROR_MS)
        self._schedule_changed()

    def _hashed_stores(self) -> list[Any]:
        """The stores that key channels (and films) by a hash of their provider's name."""
        stores = (
            self.live_tuning,
            self.sound_sync,
            self.track_memory,
            self.channel_pictures,
            self.catchup_memory,
            self.subtitle_picks,
        )
        return [store for store in stores if store is not None]

    def _rekey_hashed(self, renames: Renames, contents: dict) -> None:
        """The renamed providers' channels and films hashed anew off the GUI thread, for the
        stores that key them so (a provider whose lists are not loaded keeps the old keys)."""
        wanted: set[str] = set()
        for store in self._hashed_stores():
            wanted |= store.keys()
        if self.kept is not None:
            wanted |= {ring.channel for ring in self.kept.rings()}
        if not contents or not wanted:
            return
        worker = HashedKeys(renames, contents, wanted)
        self.jobs.run_worker(worker, self._on_hashed_keys, lambda *_args: None)

    def _on_hashed_keys(self, _context: Any, keys: dict[str, str]) -> None:
        if self._closing or not keys:
            return
        for store in self._hashed_stores():
            store.rekey(keys)
        if self.kept is not None:
            self.kept.rekey(keys)

    def save_config(self) -> None:
        """Settings → Save: the new settings apply even when config.json cannot be written."""
        try:
            self.config_manager.save_config(wait=True)  # its failure is said
        except Exception as e:
            logger.warning("Couldn't save the settings: %s", safe_message(e))
            self.screens.notify(translate(SAVE_FAILED_TEXT), "error", ERROR_MS)
        self.controller.settings_saved()
        if self.kept is not None:
            # The disk buffer off: what was kept on disk goes, and what is left later too.
            self.kept.set_disk(self.config_manager.timeshift_enabled)
            if self.engine is not None and not self.config_manager.timeshift_enabled:
                self.engine.drop_kept()
        if self.engine is not None and self.config_manager.timeshift_enabled:
            # 2.8: the disk amount applies to the disk buffers at once (a kept one gives way
            # first), not only from the next disk open.
            mib = int(self.config_manager.timeshift_max_mib)
            self.engine.set_disk_amount(mib * 1024 * 1024)
        if self.engine is not None:  # 2.8.2: the running helper's detail follows at once
            self.engine.set_detailed_log(self.config_manager.detailed_log)
        secure = getattr(self.config_manager, "secure_dns", False) is True  # 2.9.1
        secure_dns.enable(secure)  # at once, here ...
        if self.engine is not None:
            self.engine.set_secure_dns(secure)  # ... and in the time-shift helper
        self.remote.apply_config()

    def set_provider(self, force_update: bool = False) -> None:
        self.controller.select_provider(force_refresh=force_update)

    def reload_epg(self) -> None:
        """The EPG settings changed: reload the guide in the background (it repaints when ready)."""
        self.catalog.refresh_epg(force=True)

    # ---- lifecycle ----------------------------------------------------------------------------

    def dde_follow_registration(self, registered: bool) -> None:
        """Settings registered QiTV for a kind of file, or stopped using it (`registered`: for
        any kind now): on Windows, double-clicks reach this QiTV over DDE from now on, or no
        more (services.dde_windows). Nothing elsewhere."""
        if self.dde is not None and not self._closing:
            self.dde.follow(registered)

    def come_to_front(self) -> None:
        """QiTV was started again with these settings (2.8.12, services.single_instance):
        this window shows in front instead, restored when it was minimized (fullscreen or
        maximized as it was)."""
        if self._closing:
            return
        state = self.windowState()
        if state & Qt.WindowState.WindowMinimized:
            self.setWindowState(state & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        if sys.platform == "darwin":
            macos_window.activate_app()  # another app is in front: QiTV comes forward
        self.activateWindow()

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
        self.media_keys.start(int(self.winId()))
        self.controller.start()
        self.epg_refresh.start()
        if self.scheduler is not None:
            self.scheduler.start()  # what was missed meanwhile is said; then the schedule
            self._schedule_changed()
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

    def _come_forward(self) -> None:
        """The window in front, not minimized, its picture back from the desktop's."""
        try:
            self.desktop_pip.dock()
        except Exception as e:  # never in the way of the question
            logger.warning("Could not put the floating picture back: %s", safe_message(e))
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
            self.show()
        self.raise_()
        self.activateWindow()

    def quit_chosen(self) -> None:
        """QiTV's Quit action (menu, ⌘Q, Ctrl+Q): noted for the log, then the window closes."""
        if self._quit_asked is None:
            active = self.app.applicationState() == Qt.ApplicationState.ApplicationActive
            self._quit_asked = quit_reason.asked_by_menu(active)
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Close in steps, each after the event loop ran again (`close_retry`).

        1. Stop: the live stream and the time-shift helper first (its supervisor thread
           would keep the wait below going), then the catalog starts no more jobs, playback
           stops, the geometry is saved.
        2. Wait for running QThreads (never block the GUI), with a notice after half a second,
           for at most CLOSE_THREAD_WAIT_MS.
        3. Save the image and EPG indexes, let the phone remote finish closing (stopped in
           step 1, REMOTE_CLOSE_WAIT_S at most) and release libVLC. On macOS libVLC removes its
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
        if not self._closing and self.controller.asks_before_quit(self.close):
            event.ignore()  # 2.6: channels record: Quit stops them and closes again
            self._come_forward()  # the question shows where it can be answered
            return
        if not self._closing:
            # Why QiTV ends (its log is kept in a file since 2.7.2; 2.8 tells a request from
            # outside, which macOS sends the way QiTV's own Quit closes, apart from the Quit).
            asked = getattr(self.app, "quit_asked", None) or self._quit_asked
            how = quit_reason.describe(event.spontaneous(), asked)
            logger.info("The window closes (%s).", how)
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
                # Closing since step 1 on a thread of its own (its phones hear goodbye).
                ("let the phone remote close", self._wait_remote_closed),
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
                if self.scheduler is not None:
                    self.scheduler.engine_gone()  # its streams go back; the rest tried again

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
        if self.kept is not None:
            self.kept.use_engine(self.engine)
            self._take_kept_back()
        if self.scheduler is not None:
            self.scheduler.use_engine(self.engine)
        self.engine.start()
        old.deleteLater()
        call_before_update(self.stop_live, self.restart_live)  # each hook runs once
        logger.info("The update did not launch; time-shift is back on.")
        self.controller.resume_stopped()  # what played before plays again

    def _take_kept_back(self) -> None:
        """The disk buffers kept before (2.6): taken back with the disk buffer on, else gone."""
        config = self.config_manager
        if self.engine is None:
            return
        if config.timeshift_enabled:
            self.engine.adopt_kept(int(config.timeshift_max_mib) * 1024 * 1024)
        else:
            self.engine.drop_kept()

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
        self._stop_subtitles_wait()
        self._run_steps(
            # First: a start of QiTV during the close (QiTV closed, then the next episode
            # double-clicked) is no longer handed to this window, which is going; it waits for
            # the settings' lock, kept until main() lets go last, and starts with its file.
            ("stop answering other starts", self._answer_no_starts),
            ("take Explorer's DDE back", self._close_dde),  # Explorer starts QiTV again
            ("let the next click through", self.screens.shutdown),
            ("stop the phone remote", self.remote.shutdown),
            ("leave Now Playing", self.media_keys.stop),  # the media keys go back to others
            ("remove the media panel's cover", self.panel_cover.close),  # its temporary file
            ("stop AI captions", self.captions.shutdown),  # its process and any download
            ("stop the Stage's bars", self.visualizer.shutdown),  # their helper process
            ("cancel the export", self.exports.shutdown),
            ("close the Library", self.library_controller.shutdown),  # its search, its pending
            ("stop the channel list's find", self.finder.shutdown),
            # 2.8: before the helper goes, so they keep what they recorded
            ("stop the scheduled recordings", self._stop_scheduler),
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

    def _answer_no_starts(self) -> None:
        if self.instance is not None:
            self.instance.stop_listening()  # its lock stays held (services.single_instance)

    def _close_dde(self) -> None:
        if self.dde is not None:
            self.dde.close()  # once: aboutToQuit and main()'s `finally` find it closed

    @staticmethod
    def _run_steps(*steps: tuple[str, Callable[[], None]]) -> None:
        for what, step in steps:
            try:
                step()
            except Exception as e:
                # The message can name a provider URL: a scrubbed one, and the frames only.
                logger.error("Could not %s: %s", what, safe_message(e))
                logger.debug("Could not %s:\n%s", what, safe_traceback(e))

    def _wait_remote_closed(self) -> None:
        self.remote.wait_stopped(REMOTE_CLOSE_WAIT_S)

    def _save_geometry(self) -> None:
        self.config_manager.save_window_settings(_Geometry(self._normal_geometry()), WINDOW_NAME)

    def _release_video(self) -> None:
        for _name, player in self._players():
            player.release()
        if self._owns_instance and self._instance is not None:
            instance, self._instance = self._instance, None
            instance.release()
