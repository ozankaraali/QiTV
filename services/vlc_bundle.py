"""Make a packaged QiTV load the libVLC it ships, before anything imports `vlc`.

python-vlc finds libVLC on its own, and a packaged app must not let it: on macOS it prefers
/Applications/VLC.app and adds that install's plugins (VLC_PLUGIN_PATH), on Windows it asks
the registry for an installed VLC first, and on Linux the dynamic loader may take the
system's libvlccore. A different VLC would mix its plugins with the bundled core, or bring a
libVLC this python-vlc does not fit. So a frozen build loads the bundled libvlccore and
libvlc itself, by path, and then points python-vlc at them (PYTHON_VLC_LIB_PATH,
PYTHON_VLC_MODULE_PATH) and libVLC at the bundled plugins only (VLC_PLUGIN_PATH).

The layouts are the PyInstaller specs': libvlc and libvlccore at the bundle's top
(sys._MEIPASS), the plugins where that libvlccore looks for them: <top>/vlc/plugins on
macOS and Linux, <top>/plugins on Windows. Only the standard library is used: main.py calls
this before the app's imports.

On Linux the bundle's libva and libvdpau (hardware decoding) look for the system's drivers
in the folders of the distribution they were built on (Ubuntu's /usr/lib/x86_64-linux-gnu);
other distributions keep them elsewhere (Fedora and openSUSE /usr/lib64, Arch /usr/lib), so
the first folder that holds such drivers is named to them, unless the user named one.
"""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
import platform
import sys
from typing import Any, Callable, MutableMapping, Optional

_LOG = logging.getLogger(__name__)

ENV_LIB = "PYTHON_VLC_LIB_PATH"  # python-vlc loads this libvlc instead of searching
ENV_MODULES = "PYTHON_VLC_MODULE_PATH"  # python-vlc's plugin directory (VLC_PLUGIN_PATH)
ENV_PLUGINS = "VLC_PLUGIN_PATH"  # libVLC adds these to its own plugin directory
ADDED: set[str] = set()  # DRIVER_PATHS's variables use_bundled_vlc set (the user had none)

_NAMES = {  # system: (libvlccore, libvlc, plugin directory under the top)
    "Darwin": ("libvlccore.dylib", "libvlc.dylib", ("vlc", "plugins")),
    "Windows": ("libvlccore.dll", "libvlc.dll", ("plugins",)),
}
_LINUX = ("libvlccore.so*", "libvlc.so*", ("vlc", "plugins"))  # the spec keeps one version
# Linux: where distributions keep the hardware decoding drivers (Debian and Ubuntu, Fedora and
# openSUSE, Arch, NixOS), and what each variable names: a folder and its drivers' file names
# (VA-API's iHD_drv_video.so, VDPAU's libvdpau_nvidia.so.1).
_DRIVER_LIBS = ("usr/lib/x86_64-linux-gnu", "usr/lib64", "usr/lib", "run/opengl-driver/lib")
DRIVER_PATHS = {
    "LIBVA_DRIVERS_PATH": ("dri", "*_drv_video.so"),
    "VDPAU_DRIVER_PATH": ("vdpau", "libvdpau_*.so*"),
}


def bundled_vlc(top: Path, system: str) -> Optional[tuple[Path, Path, Path]]:
    """(libvlccore, libvlc, plugin directory) shipped under `top`; None unless all are there."""
    core_name, lib_name, plugins = _NAMES.get(system, _LINUX)
    if system in _NAMES:
        core, lib = top / core_name, top / lib_name
    else:
        cores, libs = sorted(top.glob(core_name)), sorted(top.glob(lib_name))
        if not cores or not libs:
            return None
        core, lib = cores[0], libs[0]
    directory = top.joinpath(*plugins)
    if not core.is_file() or not lib.is_file() or not directory.is_dir():
        return None
    return core, lib, directory


def driver_paths(root: Path = Path("/")) -> dict[str, str]:
    """Linux: each of DRIVER_PATHS's variables with the first folder under `root` that holds
    such a driver (none when no folder does)."""
    found = {}
    for variable, (folder, pattern) in DRIVER_PATHS.items():
        for lib in _DRIVER_LIBS:
            directory = root / lib / folder
            try:
                if any(directory.glob(pattern)):
                    found[variable] = str(directory)
                    break
            except OSError:
                continue
    return found


def use_bundled_vlc(
    *,
    top: Optional[str] = None,
    system: Optional[str] = None,
    environ: Optional[MutableMapping[str, str]] = None,
    load: Callable[[str], Any] = ctypes.CDLL,
    drivers: Callable[[], dict[str, str]] = driver_paths,
) -> bool:
    """In a packaged app, have python-vlc use the bundled libVLC; True when it will.

    `top` is the bundle's top directory: sys._MEIPASS in a frozen build (a source run has
    none and changes nothing). The libraries are loaded here first, the core before libvlc
    so that libvlc binds to it: python-vlc exits the process when PYTHON_VLC_LIB_PATH does
    not load, so a bundle that cannot is left to python-vlc's own search, with a warning.
    On Linux `drivers()` names the hardware decoding drivers' folders (`driver_paths`).
    """
    if top is None:
        top = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None
        if top is None:
            return False
    environ = os.environ if environ is None else environ
    system = system or platform.system()
    found = bundled_vlc(Path(top), system)
    if found is None:
        _LOG.warning("The app's own libVLC is missing; looking for an installed VLC")
        return False
    core, lib, plugins = found
    try:
        load(str(core))
        load(str(lib))
    except OSError as error:
        _LOG.warning("The app's own libVLC did not load (%s); looking for an installed VLC", error)
        return False
    environ[ENV_LIB] = str(lib)
    environ[ENV_MODULES] = environ[ENV_PLUGINS] = str(plugins)
    if system not in _NAMES:  # Linux: the bundled libva's and libvdpau's drivers
        for variable, directory in drivers().items():
            if not environ.get(variable):
                environ[variable] = directory
                ADDED.add(variable)
    return True
