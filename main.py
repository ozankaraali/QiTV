"""QiTV's entry point.

Only the standard library is imported at module level: helper processes started with
multiprocessing's "spawn" import this module again (as __mp_main__), and frozen builds must
reach freeze_support() before anything heavy. Qt, the UI and the services load in main().
"""

import ctypes
import logging
import multiprocessing
import os
import platform
import shutil
import sys
import time
from typing import Callable, Mapping, Optional, Sequence
import warnings

SMOKE_FLAG = "--smoke"
SMOKE_ENV = "QITV_SMOKE"
# Windows groups a program's windows and its pinned taskbar icon by this id: one for every
# version, so that a pinned QiTV stays one icon after an update.
APP_USER_MODEL_ID = "com.ozankaraali.qitv"
# How long a Windows update's new QiTV waits for the old one to quit before it gives up
# replacing its file (Windows keeps a running program's file locked), and how often it tries.
REPLACE_WAIT_S = 30.0
REPLACE_RETRY_S = 0.5


def qt_platform_override(system: str, environ: Mapping[str, str]) -> Optional[str]:
    """The QT_QPA_PLATFORM to set before Qt starts, or None to leave Qt's choice.

    libVLC embeds its video and the overlay host stays glued only on X11. Qt 6 picks
    Wayland in Wayland sessions, so Linux prefers XWayland when an X display exists.
    """
    if system == "Linux" and not environ.get("QT_QPA_PLATFORM") and environ.get("DISPLAY"):
        return "xcb;wayland"
    return None


def smoke_arguments(argv: Sequence[str], environ: Mapping[str, str]) -> Optional[list[str]]:
    """The smoke test's arguments when this run is one, else None (start the app).

    `qitv --smoke <args>` runs scripts/smoke_tv.py inside the packaged app (CI's frozen smoke
    test), and only with QITV_SMOKE=1 set: without it, release users get the app as usual.
    """
    if environ.get(SMOKE_ENV) == "1" and list(argv[1:2]) == [SMOKE_FLAG]:
        return list(argv[2:])
    return None


def wants_splash(argv: Sequence[str], environ: Mapping[str, str]) -> bool:
    """Whether this start shows the splash (ui/splash_helper.py): not the smoke test, not a
    Windows update's copy (`--replace`: it starts while the old QiTV quits), and not a run with
    no screen to draw on (Qt's offscreen or minimal platform, the tests')."""
    if smoke_arguments(argv, environ) is not None or "--replace" in argv:
        return False
    chosen = environ.get("QT_QPA_PLATFORM", "").split(";", 1)[0].split(":", 1)[0].strip()
    return chosen not in ("offscreen", "minimal")


def replace_executable(
    old: str,
    new: str,
    *,
    wait_s: float = REPLACE_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Put the executable `new` in the place of `old` once the old QiTV has quit.

    `new` is copied beside `old` first, then moved over it: retried until the old QiTV let go
    of its file, for up to `wait_s` seconds, so that `old` is never left half-written. False
    when it never could (the copy beside it is removed again).
    """
    if os.path.normcase(os.path.abspath(old)) == os.path.normcase(os.path.abspath(new)):
        return True
    staged = old + ".update"
    deadline = clock() + wait_s
    try:
        shutil.copy2(new, staged)
        while True:
            try:
                os.replace(staged, old)
                return True
            except OSError:
                if clock() >= deadline:
                    raise
                sleep(REPLACE_RETRY_S)
    except OSError as e:
        logging.getLogger("qitv").error(
            "The update could not replace the old QiTV: %s (%s)",
            type(e).__name__,
            getattr(e, "winerror", None) or e.errno,
        )
        try:
            os.remove(staged)
        except OSError:
            pass
        return False


def handle_replace_flag(
    argv: Optional[Sequence[str]] = None,
    executable: Optional[str] = None,
    replace: Callable[[str, str], bool] = replace_executable,
) -> Optional[tuple[str, str]]:
    """Windows's self-update, in the new QiTV that the old one started with
    `--replace <old_exe_path>`: this executable takes the old one's place.

    (old path, this executable's path) when it could not: this version goes on from where it
    was downloaded, and says so (ui.update_dialogs.replace_failed); else None.
    """
    argv = sys.argv if argv is None else argv
    if "--replace" not in argv:
        return None
    index = list(argv).index("--replace")
    if index + 1 >= len(argv):
        return None
    old_exe_path = argv[index + 1]
    current_exe = sys.executable if executable is None else executable
    if replace(old_exe_path, current_exe):
        logging.getLogger("qitv").info("The update replaced the old QiTV.")
        return None
    return old_exe_path, current_exe


def replaced_qitv(argv: Sequence[str], not_replaced: Optional[tuple[str, str]]) -> Optional[str]:
    """The installed QiTV a Windows update's copy wrote itself over (`--replace <path>`, and
    the replace worked): this run is that QiTV from now on, though it runs from where it was
    downloaded, so it registers that one (services.file_types.use_installed). None for any
    other start (the old file kept: the copy is QiTV where it is)."""
    if not_replaced is not None or "--replace" not in argv:
        return None
    index = list(argv).index("--replace")
    return argv[index + 1] if index + 1 < len(argv) and argv[index + 1] else None


def old_qitv_still_runs(argv: Sequence[str], not_replaced: Optional[tuple[str, str]]) -> bool:
    """Whether this is a Windows update's copy whose old QiTV still runs: its file couldn't be
    replaced (`handle_replace_flag` waited REPLACE_WAIT_S for it to quit). Once it was, the old
    QiTV has quit, and this start claims its settings as any start does."""
    return "--replace" in argv and not_replaced is not None


def written_dirs(config_dir: str) -> list[str]:
    """Where QiTV writes files through services.atomic_write: the config (and the channel
    stores beside it), the cache (TMDB's, 2.8), the guide cache and the provider cache."""
    cache = os.path.join(config_dir, "cache")
    return [config_dir, cache, os.path.join(cache, "epg"), os.path.join(cache, "provider")]


def _migrate_favorites(config_manager) -> None:
    """Old stars (bare names) become per-provider entries once; a failed save is only logged."""
    from services import favorites

    if not favorites.migrate(config_manager):
        return
    if not favorites.save(config_manager):
        logging.getLogger(__name__).warning("The migrated favorites were not saved; will retry")


def settings_dir() -> str:
    """The settings folder this run uses (QITV_CONFIG_DIR, portable, or the system's), found
    as ConfigManager finds it, without reading or writing anything there."""
    from services.config_manager import ConfigManager

    return str(os.path.dirname(ConfigManager.__new__(ConfigManager)._get_config_path()))


def main() -> int:
    # Only what the claim on the settings folder needs loads first: a second start hands over
    # (its files too) before the UI's modules load, which takes seconds on Windows.
    from PySide6.QtCore import QLoggingCategory

    from services import log_file
    from services.config_manager import get_app_version
    from services.open_paths import holds_the_start, launch_paths
    from ui.quit_reason import QiTVApplication

    # Basic logging configuration (tweak level as needed); the console's lines are masked as
    # the file's are (addresses, Xtream paths, sign-in headers, MAC addresses).
    logging.basicConfig(level=logging.INFO, handlers=[log_file.console_handler()])
    # A packaged app has no terminal: its log is kept in a file too, where it can be read (2.7.2).
    log_file.install()
    log = logging.getLogger("qitv")
    log.info("QiTV %s starts.", get_app_version())

    # Windows's self-update: this new version takes the old one's place, once it quit.
    not_replaced, replaced = None, None
    if platform.system() == "Windows":
        not_replaced = handle_replace_flag()
        replaced = replaced_qitv(sys.argv, not_replaced)

    # Suppress noisy third-party warnings
    warnings.filterwarnings("ignore", category=SyntaxWarning, module="vlc")
    # Reduce Qt info logs that are not actionable
    QLoggingCategory.setFilterRules("qt.accessibility.*=false\nqt.qpa.fonts.*=false")
    qt_platform = qt_platform_override(platform.system(), os.environ)
    if qt_platform:
        os.environ["QT_QPA_PLATFORM"] = qt_platform
    # A worker thread busy in Python (a search over 200,000 titles) otherwise keeps the GUI
    # thread waiting up to 5 ms at every callback it makes: a 0.5 ms switch interval keeps
    # the window responsive while it works.
    sys.setswitchinterval(0.0005)
    # It notes a quit request the system sends (2.8), and keeps the files QiTV is asked to
    # open until the window takes them.
    app = QiTVApplication(sys.argv)
    # QiTV's mark, from now until the window opens into it, drawn by a process of its own: this
    # thread builds the window until it shows. It shows while another QiTV is asked, too.
    from ui.splash_helper import Splash

    splash = Splash()
    if wants_splash(sys.argv, os.environ):
        splash.start()
    if platform.system() == "Darwin":
        # A double-click in Finder gives no argument: the file comes as an open event, sent
        # at launch. Kept now, it is handed over (a second start) or kept for the window.
        app.processEvents()
    # The files this start was given (a double-click while QiTV runs hands them over).
    paths = launch_paths(sys.argv)
    # 2.8.12: one QiTV per settings folder (a second start would record and stream twice and
    # save over the first's settings): it brings the first to the front and quits.
    from services.single_instance import claim

    try:
        # The open events kept so far go too; they stay kept for the window if this is first.
        instance = claim(
            settings_dir(),
            replacing=old_qitv_still_runs(sys.argv, not_replaced),
            paths=paths + app.kept_opened(),
        )
        handed_over = instance is None
    except Exception as error:  # the check is never a reason for QiTV not to start
        log.warning("The check for another QiTV failed (%s); starting.", type(error).__name__)
        instance, handed_over = None, False
    if handed_over:
        splash.close()
        log.info("Another QiTV runs with these settings; this start ends here.")
        log_file.close()
        return 0
    if instance is not None:  # a start's files while the window is built are kept for it
        instance.opened.connect(app.add_opened)

    from PySide6 import QtGui
    from PySide6.QtCore import QLocale, QTimer, qInstallMessageHandler
    import qdarktheme

    from services import file_types, i18n, private_files
    from services.atomic_write import sweep_in_background
    from services.config_manager import ConfigManager
    from services.epg_manager import EpgManager
    from services.gc_guard import GcGuard
    from services.image_manager import ImageManager
    from services.provider_manager import ProviderManager
    from services.sleep_manager import allow_sleep, prevent_sleep
    from ui.file_types_settings import refresh_at_start
    from ui.theme import focus_qss
    from ui.tv_window import HANDOVER, LAUNCH, TVWindow
    from ui.update_dialogs import check_for_updates, replace_failed

    # Why QiTV ended, in its log: the window closed (tv_window says how), or its last one.
    app.lastWindowClosed.connect(lambda: log.info("The last window closed."))
    app.aboutToQuit.connect(lambda: log.info("QiTV quits."))
    # Before any worker starts: garbage cycles are collected on this (the GUI) thread only.
    gc_guard = GcGuard(app)
    gc_guard.start()

    # Optional: capture Qt warnings to help debug thread/timer issues
    def _qt_msg_handler(mode, context, message):
        # Focus on timer/thread-related warnings only when enabled. Print directly to stderr to
        # avoid recursion.
        if "QBasicTimer::start" in message or "QObject::startTimer" in message:
            try:
                sys.stderr.write(f"Qt: {message}\n")
                sys.stderr.flush()
            except Exception:
                pass

    if os.environ.get("QITV_DEBUG_QT", "0") == "1":
        qInstallMessageHandler(_qt_msg_handler)

    icon_path = "assets/qitv.png"

    # Resolve icon path for PyInstaller bundles
    if hasattr(sys, "_MEIPASS"):
        if platform.system() == "Windows":
            icon_path = os.path.join(sys._MEIPASS, "assets", "qitv.ico")
        else:
            icon_path = os.path.join(sys._MEIPASS, "assets", "qitv.png")

    config_manager = ConfigManager()
    # The sign-in's files (config, provider cache, the log) are this user's only, however an
    # older QiTV or the umask left them.
    private_files.tighten(config_manager.get_config_dir(), log_file.log_dir())
    # Saves from now on are written off the GUI thread (a 6 MB config.json took 20 to 60 ms
    # there, a locked one a quarter of a second more); quitting waits for them (below).
    config_manager.write_in_background()
    log_file.set_detailed(config_manager.detailed_log)  # 2.8.2: Settings' detailed log
    # The language, before any window: every text is looked up as it is shown.
    i18n.set_language(i18n.resolve(config_manager.language))
    i18n.install_qt_translations(app)
    # Qt's own widgets (spin boxes, date edits, file dialogs) write numbers and dates so too.
    QLocale.setDefault(i18n.app_locale())
    _migrate_favorites(config_manager)
    image_manager = ImageManager(config_manager, config_manager.max_cache_image_size * 1024 * 1024)
    provider_manager = ProviderManager(config_manager)
    epg_manager = EpgManager(config_manager, provider_manager)
    # Temporary files of writes killed midway, a day old, go (on a thread of their own).
    sweep_in_background(  # each downloaded subtitle's folder too (2.8)
        written_dirs(config_manager.get_config_dir()), nested=[config_manager.subtitles_dir()]
    )

    if platform.system() == "Windows":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    # An update's copy that replaced the installed QiTV registers (at start, from Settings, and
    # in Explorer's DDE command) the installed one, never itself in Downloads; any other start
    # registers where it runs, so a QiTV the user moved registers again where it is now.
    file_types.use_installed(replaced)

    app.setWindowIcon(QtGui.QIcon(icon_path))
    # The keyboard's focus shows on buttons, check boxes, tabs and sliders too (ui/theme.py).
    qdarktheme.setup_theme("dark", custom_colors={"primary": "#C96B43"}, additional_qss=focus_qss())

    prevent_sleep()
    control = None
    dde = None
    try:
        window = TVWindow(app, config_manager, provider_manager, image_manager, epg_manager)
        if instance is not None:
            instance.activated.connect(window.come_to_front)
            window.instance = instance  # it answers no start once the window's close begins
        # This start's files, and those that came before the window (macOS's open event,
        # another start's hand-over), taken once; from now on each plays as it comes.
        opened = paths + app.take_opened()
        app.filesOpened.connect(lambda files: window.open_paths(files, source=HANDOVER))
        if holds_the_start(opened):
            # A start with a film plays only the film: no last channel, drawer or welcome card
            # over it, from the window's first moment (open_paths holds too, once it shows).
            # A file QiTV won't play holds too: open_paths says so, then lets go.
            window.controller.hold_for_file(True)
        splash.before_show(window)  # transparent: it fades in as the splash opens into it
        window.show()
        splash.open_into(window)
        if opened:
            window.open_paths(opened, source=LAUNCH)
        # A QiTV moved or renamed since it registered for videos registers again where it is
        # now, on a thread of its own (never from source); the config is saved once it changed.
        refresh_at_start(config_manager, window)
        if sys.platform == "win32":
            # Registered for videos: a double-click reaches this QiTV over DDE, with no second
            # start (which unpacks the whole app first); Explorer is told so while it runs.
            # The run's one server: Settings turns it on and off (dde_follow_registration).
            try:
                from services import dde_windows

                dde = window.dde = dde_windows.serve(config_manager)
            except Exception:  # never a reason for QiTV not to start
                log.warning("No DDE: a double-click starts QiTV again.", exc_info=True)
                dde = window.dde = None
            if dde is not None:
                dde.server.opened.connect(lambda files: window.open_paths(files, source=HANDOVER))
                # A sign-out or shutdown ends QiTV after aboutToQuit, never reaching `finally`.
                app.aboutToQuit.connect(dde.close)
        if not_replaced is not None:  # where the new version is, and where the old one stayed
            old_path, new_path = not_replaced
            QTimer.singleShot(0, window, lambda: replace_failed(old_path, new_path, window))
        # 2.8.3: a developer's tools read and drive this QiTV (loopback only, token-guarded).
        from services import control as control_port

        if control_port.enabled():
            from ui.control_commands import WindowControl

            commands = WindowControl(window, python=control_port.python_allowed())
            directory = log_file.log_dir()
            control = control_port.ControlServer(
                commands.handle, directory, directory / log_file.LOG_NAME
            )
            control.start()

        if config_manager.check_updates:  # after the film a start opened, if it has one
            # Its question too waits for a film opened while the network answers (a Finder
            # double-click's open event comes after the window; a double-click meanwhile).
            window.when_idle(
                lambda: check_for_updates(config_manager=config_manager, when_idle=window.when_idle)
            )

        code = app.exec()
        log.info("QiTV's event loop ended (%d).", code)
        return code
    finally:
        if dde is not None:  # first: Explorer starts QiTV again (it never raises)
            dde.close()
        splash.close()  # one that never opened (the window didn't show)
        if control is not None:
            control.stop()
        allow_sleep()
        if not config_manager.flush_saves():  # the window's last save among them
            log.warning("QiTV quits before config.json was written: the last change is lost.")
        if instance is not None:
            # Last: a start waiting for the settings' lock (one during the close) takes it
            # only once the settings are written, the film's resume point among them.
            instance.close()
        log_file.close()


if __name__ == "__main__":
    # First: a frozen build's helper process stops here instead of starting the app.
    multiprocessing.freeze_support()
    # Then, before anything imports vlc: a packaged app loads the libVLC it ships.
    from services.vlc_bundle import use_bundled_vlc

    use_bundled_vlc()
    # And before anything connects: a computer without the authorities where the bundle's
    # OpenSSL looks (a Mac without Homebrew) trusts certifi's, which the bundle carries.
    from services.authorities import use_bundled_authorities

    use_bundled_authorities()
    smoke = smoke_arguments(sys.argv, os.environ)
    if smoke is not None:
        from scripts.smoke_tv import main as smoke_main

        sys.exit(smoke_main(smoke))
    sys.exit(main())
