"""Locate QiTV's private standalone MPV; never search PATH for Internal mode."""

import json
import os
from pathlib import Path
import platform
import sys


def get_resource_root() -> Path:
    """Return the common root used by source and PyInstaller asset layouts."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def get_bundled_mpv_path() -> str:
    """Resolve the prepared executable or explain how to obtain the native bundle."""
    root = get_resource_root() / "native" / "mpv"
    guidance = (
        "The bundled MPV runtime is missing or invalid. "
        "For a source checkout, run `uv run scripts/prepare_mpv.py` with the "
        "native build prerequisites installed. For a packaged QiTV application, "
        "reinstall the complete release for your operating system and architecture."
    )
    try:
        metadata = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
        relative = Path(metadata["executable"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Executable escapes the runtime directory")
        executable = (root / relative).resolve()
        executable.relative_to(root.resolve())
        if not executable.is_file():
            raise ValueError("Executable does not exist")
        machine = platform.machine().lower()
        architecture = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(
            machine, machine
        )
        system = "windows" if sys.platform == "win32" else sys.platform
        if metadata.get("target") != f"{system}-{architecture}":
            raise ValueError("Runtime architecture does not match this process")
        if sys.platform != "win32" and not os.access(executable, os.X_OK):
            raise ValueError("Executable permission is missing")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FileNotFoundError(f"{guidance} ({exc})") from exc
    return str(executable)
