"""Launch external VLC / MPV players. No Qt: failures are returned, not shown.

find_vlc() and find_mpv() name the player launch_vlc() and launch_mpv() would start, so a
caller can say a player is missing before it stops anything.
"""

import logging
import os
import platform
import shutil
import subprocess
import sys
from typing import Any, Mapping, Optional, Tuple

from services.i18n import N_, tr, translate
from services.vlc_bundle import ENV_LIB, ENV_MODULES, ENV_PLUGINS

logger = logging.getLogger(__name__)

# Single-instance python-mpv player, reused across launches (was PlaybackMixin state).
_external_mpv_player: Optional[Any] = None

PYTHON_MPV = "python-mpv"  # find_mpv(): mpv plays through the python-mpv module
# A missing player's message: its first line (the headline) is what a toast shows; the rest,
# how to install it, is only logged and stays English. vlc_missing() and mpv_missing() give the
# headline in the active language; translate() shows one taken from VLC_MISSING or MPV_MISSING.
VLC_MISSING_HEADLINE = N_("VLC Media Player is not installed.")  # i18n: a toast
VLC_INSTALL_HELP = (
    "Please install VLC:\n"
    "• macOS: Download from https://www.videolan.org/vlc/\n"
    "• Linux: Use your package manager (apt install vlc)\n"
    "• Windows: Download from https://www.videolan.org/vlc/"
)
VLC_MISSING = VLC_MISSING_HEADLINE + "\n\n" + VLC_INSTALL_HELP
MPV_MISSING_HEADLINE = N_("MPV Media Player is not installed.")  # i18n: a toast
MPV_INSTALL_HELP = (
    "Please install MPV:\n"
    "• macOS: brew install mpv\n"
    "• Linux: Use your package manager (apt install mpv)\n"
    "• Windows: Download from https://mpv.io/installation/"
)
MPV_MISSING = MPV_MISSING_HEADLINE + "\n\n" + MPV_INSTALL_HELP


def vlc_missing() -> str:
    """VLC_MISSING, its headline in the active language."""
    return translate(VLC_MISSING_HEADLINE) + "\n\n" + VLC_INSTALL_HELP


def mpv_missing() -> str:
    """MPV_MISSING, its headline in the active language."""
    return translate(MPV_MISSING_HEADLINE) + "\n\n" + MPV_INSTALL_HELP


def player_env(
    environ: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
    frozen: Optional[bool] = None,
) -> dict[str, str]:
    """The environment an external player starts with: QiTV's, less its own VLC's.

    VLC_PLUGIN_PATH (and python-vlc's two variables) point at the bundled libVLC, and an
    installed VLC would scan those plugins as well as its own. In a frozen Linux app,
    LD_LIBRARY_PATH points into the bundle: the value from before QiTV started comes back
    (PyInstaller's LD_LIBRARY_PATH_ORIG), or none when there was none.
    """
    environ = os.environ if environ is None else environ
    system = system or platform.system()
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    env = {k: v for k, v in environ.items() if k not in (ENV_LIB, ENV_MODULES, ENV_PLUGINS)}
    if system == "Linux" and frozen:
        original = env.get("LD_LIBRARY_PATH_ORIG")
        if original is not None:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def find_vlc() -> Optional[str]:
    """The VLC launch_vlc() starts: the app bundle on macOS, else the executable; or None."""
    if platform.system() == "Darwin":
        # macOS: 'open -a VLC' reuses the running instance
        for path in ("/Applications/VLC.app", os.path.expanduser("~/Applications/VLC.app")):
            if os.path.exists(path):
                return path
        return None

    vlc_cmd = shutil.which("vlc")
    if not vlc_cmd and platform.system() == "Windows":
        possible_paths = [
            os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                "VideoLAN",
                "VLC",
                "vlc.exe",
            ),
            os.path.join(
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                "VideoLAN",
                "VLC",
                "vlc.exe",
            ),
        ]
        vlc_cmd = next((path for path in possible_paths if os.path.exists(path)), None)
    return vlc_cmd


def launch_vlc(url: str) -> Tuple[bool, str]:
    """Launch VLC with platform-specific handling."""
    vlc_cmd = find_vlc()
    if not vlc_cmd:
        return False, vlc_missing()
    if platform.system() == "Darwin":
        # "--": a URL that starts with "-" must not be read as an option.
        argv = ["open", "-a", vlc_cmd, "--", url]
    else:
        # --started-from-file sends URL to existing VLC instance (replaces current)
        argv = [vlc_cmd, "--started-from-file", "--", url]
    try:
        subprocess.Popen(argv, env=player_env())
        return True, ""
    except Exception as e:
        # i18n: the first line shows in a toast; {error} is the system's reason
        return False, tr("Failed to launch VLC: {error}", error=str(e))


def _python_mpv_loads() -> bool:
    """Whether the python-mpv module (with libmpv) loads: launch_mpv() tries it first."""
    if _external_mpv_player is not None:
        return True
    try:
        import mpv  # noqa: F401
    except Exception as e:
        logger.debug("python-mpv unavailable (%s)", type(e).__name__)
        return False
    return True


def _mpv_executable() -> Optional[str]:
    """The mpv executable, with platform-specific paths."""
    mpv_cmd = None

    if platform.system() == "Darwin":
        # macOS: check app bundle and brew paths
        macos_paths = [
            "/Applications/mpv.app/Contents/MacOS/mpv",
            os.path.expanduser("~/Applications/mpv.app/Contents/MacOS/mpv"),
            "/opt/homebrew/bin/mpv",
            "/usr/local/bin/mpv",
        ]
        for path in macos_paths:
            if os.path.exists(path):
                mpv_cmd = path
                break
        if not mpv_cmd:
            mpv_cmd = shutil.which("mpv")

    elif platform.system() == "Windows":
        mpv_cmd = shutil.which("mpv")
        if not mpv_cmd:
            # Check Windows Registry App Paths (set by mpv-install.bat)
            # Try both HKEY_LOCAL_MACHINE and HKEY_CURRENT_USER
            try:
                import winreg

                for hkey in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                    try:
                        with winreg.OpenKey(
                            hkey,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\mpv.exe",
                        ) as key:
                            reg_path = winreg.QueryValue(key, None)
                            if reg_path and os.path.exists(reg_path):
                                mpv_cmd = reg_path
                                break
                    except Exception:
                        continue
            except Exception:
                pass
        if not mpv_cmd:
            user_profile = os.environ.get("USERPROFILE", "")
            possible_paths = [
                # Common installation paths
                os.path.join(
                    os.environ.get("ProgramFiles", r"C:\Program Files"),
                    "mpv",
                    "mpv.exe",
                ),
                os.path.join(
                    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                    "mpv",
                    "mpv.exe",
                ),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "mpv", "mpv.exe"),
                # Scoop package manager
                os.path.join(user_profile, "scoop", "apps", "mpv", "current", "mpv.exe"),
                # Chocolatey package manager
                os.path.join(
                    os.environ.get("ProgramData", r"C:\ProgramData"),
                    "chocolatey",
                    "bin",
                    "mpv.exe",
                ),
                # Portable/extracted mpv (common locations)
                r"C:\mpv\mpv.exe",
                os.path.join(user_profile, "mpv", "mpv.exe"),
                os.path.join(user_profile, "Downloads", "mpv", "mpv.exe"),
                os.path.join(user_profile, "Desktop", "mpv", "mpv.exe"),
                # Winget default location
                os.path.join(
                    os.environ.get("LOCALAPPDATA", ""),
                    "Microsoft",
                    "WinGet",
                    "Packages",
                    "mpv.net_Mpv.net",
                    "mpv",
                    "mpv.exe",
                ),
            ]
            for path in possible_paths:
                if path and os.path.exists(path):
                    mpv_cmd = path
                    break
    else:
        # Linux
        mpv_cmd = shutil.which("mpv")
    return mpv_cmd


def find_mpv() -> Optional[str]:
    """The mpv launch_mpv() starts: PYTHON_MPV, else the executable's path; or None."""
    return PYTHON_MPV if _python_mpv_loads() else _mpv_executable()


def launch_mpv(url: str) -> Tuple[bool, str]:
    """Launch MPV - try python-mpv first, fall back to subprocess."""
    global _external_mpv_player

    # Try python-mpv for single-instance behavior
    try:
        import mpv

        if _external_mpv_player is None:
            _external_mpv_player = mpv.MPV(
                input_default_bindings=True,
                input_vo_keyboard=True,
                osc=True,
            )

        _external_mpv_player.play(url)
        return True, ""
    except Exception as e:
        # python-mpv not available or failed, fall back to subprocess
        logger.debug("python-mpv unavailable (%s); launching mpv executable", e)

    mpv_cmd = _mpv_executable()
    if not mpv_cmd:
        return False, mpv_missing()

    try:
        subprocess.Popen([mpv_cmd, "--", url], env=player_env())
        return True, ""
    except Exception as e:
        # i18n: the first line shows in a toast; {error} is the system's reason
        return False, tr("Failed to launch MPV: {error}", error=str(e))
