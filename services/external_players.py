"""Launch external VLC / MPV players. No Qt: failures are returned, not shown."""

import logging
import os
import platform
import shutil
import subprocess
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# Single-instance python-mpv player, reused across launches (was PlaybackMixin state).
_external_mpv_player: Optional[Any] = None


def launch_vlc(url: str) -> Tuple[bool, str]:
    """Launch VLC with platform-specific handling."""
    vlc_cmd = None

    if platform.system() == "Darwin":
        # macOS: use 'open -a VLC' which reuses existing instance
        vlc_paths = [
            "/Applications/VLC.app",
            os.path.expanduser("~/Applications/VLC.app"),
        ]
        for path in vlc_paths:
            if os.path.exists(path):
                try:
                    # "--": a URL that starts with "-" must not be read as an option.
                    subprocess.Popen(["open", "-a", path, "--", url])
                    return True, ""
                except Exception as e:
                    return False, f"Failed to launch VLC: {str(e)}"

    elif platform.system() == "Windows":
        # Windows: find VLC executable
        vlc_cmd = shutil.which("vlc")
        if not vlc_cmd:
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
            for path in possible_paths:
                if os.path.exists(path):
                    vlc_cmd = path
                    break
    else:
        # Linux
        vlc_cmd = shutil.which("vlc")

    if not vlc_cmd:
        return False, (
            "VLC Media Player is not installed.\n\n"
            "Please install VLC:\n"
            "• macOS: Download from https://www.videolan.org/vlc/\n"
            "• Linux: Use your package manager (apt install vlc)\n"
            "• Windows: Download from https://www.videolan.org/vlc/"
        )

    try:
        # --started-from-file sends URL to existing VLC instance (replaces current)
        subprocess.Popen([vlc_cmd, "--started-from-file", "--", url])
        return True, ""
    except Exception as e:
        return False, f"Failed to launch VLC: {str(e)}"


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

    # Subprocess fallback with platform-specific paths
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

    if not mpv_cmd:
        return False, (
            "MPV Media Player is not installed.\n\n"
            "Please install MPV:\n"
            "• macOS: brew install mpv\n"
            "• Linux: Use your package manager (apt install mpv)\n"
            "• Windows: Download from https://mpv.io/installation/"
        )

    try:
        subprocess.Popen([mpv_cmd, "--", url])
        return True, ""
    except Exception as e:
        return False, f"Failed to launch MPV: {str(e)}"
