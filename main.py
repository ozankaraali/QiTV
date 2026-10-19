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
from typing import Mapping, Optional, Sequence
import warnings

SMOKE_FLAG = "--smoke"
SMOKE_ENV = "QITV_SMOKE"


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


def handle_replace_flag():
    """Handle --replace flag for Windows auto-update.

    When launched with --replace <old_exe_path>, this function:
    1. Waits for the old process to exit
    2. Copies this executable to the old location
    3. Optionally cleans up the downloaded file
    """
    if "--replace" not in sys.argv:
        return

    try:
        replace_index = sys.argv.index("--replace")
        if replace_index + 1 >= len(sys.argv):
            return

        old_exe_path = sys.argv[replace_index + 1]
        current_exe = sys.executable

        # Wait for old process to exit
        time.sleep(1.5)

        # Copy current executable to old location
        shutil.copy2(current_exe, old_exe_path)
        logging.info(f"Updated {old_exe_path} successfully")

        # Note: We can't delete the currently running exe on Windows
        # The downloaded file in Downloads will remain - user can delete manually

    except Exception as e:
        logging.error(f"Failed to perform update replacement: {e}")


def written_dirs(config_dir: str) -> list[str]:
    """Where QiTV writes files through services.atomic_write: the config (and the channel
    stores beside it), the guide cache and the provider cache."""
    cache = os.path.join(config_dir, "cache")
    return [config_dir, os.path.join(cache, "epg"), os.path.join(cache, "provider")]


def _migrate_favorites(config_manager) -> None:
    """Old stars (bare names) become per-provider entries once; a failed save is only logged."""
    from services import favorites

    if not favorites.migrate(config_manager):
        return
    if not favorites.save(config_manager):
        logging.getLogger(__name__).warning("The migrated favorites were not saved; will retry")


def main() -> int:
    from PySide6 import QtGui
    from PySide6.QtCore import QLocale, QLoggingCategory, qInstallMessageHandler
    from PySide6.QtWidgets import QApplication
    import qdarktheme

    from services import i18n
    from services.atomic_write import sweep_in_background
    from services.config_manager import ConfigManager, get_app_version
    from services.epg_manager import EpgManager
    from services.gc_guard import GcGuard
    from services.image_manager import ImageManager
    from services.provider_manager import ProviderManager
    from services.sleep_manager import allow_sleep, prevent_sleep
    from ui.tv_window import TVWindow
    from ui.update_dialogs import check_for_updates

    # Basic logging configuration (tweak level as needed)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Handle Windows auto-update replacement if launched with --replace flag
    if platform.system() == "Windows":
        handle_replace_flag()

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
    app = QApplication(sys.argv)
    # Before any worker starts: garbage cycles are collected on this (the GUI) thread only.
    gc_guard = GcGuard(app)
    gc_guard.start()

    # Optional: capture Qt warnings to help debug thread/timer issues
    def _qt_msg_handler(mode, context, message):
        # Focus on timer/thread-related warnings only when enabled. Print directly to stderr to avoid recursion.
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
    sweep_in_background(written_dirs(config_manager.get_config_dir()))

    if platform.system() == "Windows":
        myappid = f"com.ozankaraali.qitv.{get_app_version()}"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)  # type: ignore

    app.setWindowIcon(QtGui.QIcon(icon_path))
    qdarktheme.setup_theme("dark", custom_colors={"primary": "#C96B43"})

    prevent_sleep()
    try:
        window = TVWindow(app, config_manager, provider_manager, image_manager, epg_manager)
        window.show()

        if config_manager.check_updates:
            check_for_updates(config_manager=config_manager)

        return app.exec()
    finally:
        allow_sleep()


if __name__ == "__main__":
    # First: a frozen build's helper process stops here instead of starting the app.
    multiprocessing.freeze_support()
    # Then, before anything imports vlc: a packaged app loads the libVLC it ships.
    from services.vlc_bundle import use_bundled_vlc

    use_bundled_vlc()
    smoke = smoke_arguments(sys.argv, os.environ)
    if smoke is not None:
        from scripts.smoke_tv import main as smoke_main

        sys.exit(smoke_main(smoke))
    sys.exit(main())
