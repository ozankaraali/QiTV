"""QiTV registered with the system for a kind of file (videos), per user: it shows in Open With
and can be made the default player. No admin is ever asked, and unregistering removes exactly
what QiTV wrote.

Everything here may touch the disk, the registry or start a program, so it runs on a worker
thread, except `status` and `temporary_location`, which only look at strings. Nothing here
saves the config: `register`, `unregister` and `refresh` assign `config.file_types` a new dict
(`{kind: the executable registered}`), and the caller saves it on the GUI thread.

- **Windows** (HKEY_CURRENT_USER only): the ProgID `QiTV.Video`, `OpenWithProgids` for every
  type, the exe's `Applications` entry, and Default Programs' `Capabilities` with its
  `RegisteredApplications` value; Windows' own Default Apps page then lets the user choose.
- **Linux**: `$XDG_DATA_HOME/applications/qitv-open.desktop`, the icon in the user's hicolor
  theme, and `xdg-mime default` for the default types (which writes the user's mimeapps.list).
- **macOS**: none yet; a later backend.

A source run never registers: there `sys.executable` is Python, and it may share the packaged
app's settings. Neither does a QiTV started from a place that goes away (a temporary folder,
inside a zip, macOS's App Translocation).
"""

from __future__ import annotations

import enum
import logging
import ntpath
import os
from pathlib import Path
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Optional, Protocol
from urllib.parse import quote

from services.atomic_write import atomic_write
from services.external_players import player_env
from services.open_paths import VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)

KINDS = ("videos",)  # music comes with its own row
DEFAULT_VIDEO = VIDEO_EXTENSIONS - {"ts"}  # .ts is Open With only: code files share it
MAC_DEFAULT_VIDEO = ("mkv", "mp4", "mov", "avi", "webm")

APP_NAME = "QiTV"  # Default Programs' name: RegisteredApplications' value and the deep link's
DESCRIPTION = "Plays video files, and TV channels from IPTV providers."

# Each kind's types (Open With) and the ones it offers to become the default.
_TYPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "videos": (VIDEO_EXTENSIONS, DEFAULT_VIDEO),
}

# Each type's MIME types, from shared-mime-info: today's name first, then the one older
# databases use (they read the newer one as an alias, and the older one as its own type).
MIME_TYPES: dict[str, tuple[str, ...]] = {
    "mp4": ("video/mp4",),
    "m4v": ("video/mp4", "video/x-m4v"),
    "mkv": ("video/x-matroska",),
    "avi": ("video/vnd.avi", "video/x-msvideo"),
    "mov": ("video/quicktime",),
    "webm": ("video/webm",),
    "ts": ("video/mp2t",),
    "m2ts": ("video/mp2t",),  # one MIME type with .ts: Open With only on Linux
    "mts": ("video/mp2t",),
    "mpg": ("video/mpeg",),
    "mpeg": ("video/mpeg",),
    "flv": ("video/x-flv",),
    "wmv": ("video/x-ms-wmv",),
    "ogv": ("video/ogg",),
}

SUBPROCESS_TIMEOUT_S = 10


class Status(enum.Enum):
    REGISTERED = "registered"
    NOT_REGISTERED = "not registered"
    TEMPORARY = "temporary"  # QiTV runs from a place that goes away: move it first
    UNAVAILABLE = "unavailable"  # a source run, or a system without a backend


class _Backend(Protocol):
    def register(
        self, kind: str, exe: str, *, defaults: bool = True, previous: Optional[str] = None
    ) -> bool: ...

    def unregister(self, kind: str, exe: str) -> bool: ...


def mime_types(kind: str) -> list[str]:
    """Every MIME type of `kind`'s types, once each, in a fixed order."""
    return _mimes(_kind_types(kind)[0])


def default_mime_types(kind: str) -> list[str]:
    """The MIME types `kind` becomes the default for: its default types' less those that a
    type left out shares (video/mp2t is .ts's, so .m2ts and .mts stay Open With only)."""
    every, chosen = _kind_types(kind)
    shared = set(_mimes(every - chosen))
    return [m for m in _mimes(chosen) if m not in shared]


def status(kind: str, config: Any, frozen: Optional[bool] = None) -> Status:
    """Whether QiTV is registered for `kind`, from the record (the truth: `refresh` repairs a
    moved executable). No disk, registry or program is touched."""
    _kind_types(kind)
    if not _frozen(frozen) or _backend() is None:
        return Status.UNAVAILABLE
    if temporary_location(sys.executable):
        return Status.TEMPORARY
    return Status.REGISTERED if kind in _recorded(config) else Status.NOT_REGISTERED


def register(
    kind: str, config: Any, exe: str = sys.executable, frozen: Optional[bool] = None
) -> bool:
    """Register `exe` for `kind`, and record it; False (nothing recorded) when it could not be,
    or must not be: a source run, a temporary place, a system without a backend."""
    _kind_types(kind)
    if not _frozen(frozen):
        logger.info("A run from source doesn't register itself for %s", kind)
        return False
    if temporary_location(exe):
        logger.info("QiTV runs from a temporary place; not registered for %s", kind)
        return False
    backend = _backend()
    if backend is None:
        return False
    if not _attempt("register", lambda: backend.register(kind, exe), kind):
        return False
    record = _recorded(config)
    record[kind] = exe
    config.file_types = record
    return True


def unregister(kind: str, config: Any) -> bool:
    """Remove what registering for `kind` wrote (for the executable recorded), and the record;
    False (the record kept, to try again) when that failed."""
    _kind_types(kind)
    backend = _backend()
    if backend is None:
        return False
    record = _recorded(config)
    exe = record.get(kind, sys.executable)
    if not _attempt("unregister", lambda: backend.unregister(kind, exe), kind):
        return False
    record.pop(kind, None)
    config.file_types = record
    return True


def refresh(config: Any, exe: str = sys.executable, frozen: Optional[bool] = None) -> None:
    """At start: each kind recorded for another executable (QiTV was moved or renamed) is
    registered for `exe` instead, without taking any default again (the user may have chosen
    another app since). Nothing from source or from a temporary place."""
    if not _frozen(frozen) or temporary_location(exe):
        return
    backend = _backend()
    if backend is None:
        return
    record = _recorded(config)
    moved = {kind: old for kind, old in record.items() if kind in KINDS and old != exe}
    for kind, old in moved.items():
        if _attempt(
            "refresh",
            lambda: backend.register(kind, exe, defaults=False, previous=old),
            kind,
        ):
            logger.info("Registered for %s again: QiTV moved", kind)
            record[kind] = exe
            config.file_types = dict(record)


def open_default_apps() -> None:
    """Windows' Default Apps page, at QiTV's own settings where Windows has them (Windows 11
    with the April 2023 updates), else the page itself. Does nothing elsewhere."""
    if sys.platform != "win32":
        return
    own = f"ms-settings:defaultapps?registeredAppUser={quote(APP_NAME)}"
    for page in (own, "ms-settings:defaultapps"):
        try:
            os.startfile(page)
            return
        except OSError as e:
            logger.info("Windows didn't open %s (%s)", page, type(e).__name__)


def temporary_location(exe: str) -> bool:
    """Whether `exe` sits where it won't stay: the system's temporary folder or below it
    (Windows' `AppData\\Local\\Temp` too), inside a `.zip` (Explorer's zip view, and the
    `Temp1_x.zip` folder it extracts to), or in macOS's App Translocation. Strings only: no
    disk is touched (the Settings row asks this)."""
    if not exe:
        return False
    windows = _windows_shaped(exe)
    slashed = exe.replace("\\", "/")
    folded = slashed.lower()
    if "/apptranslocation/" in folded:
        return True
    if windows and "/appdata/local/temp/" in folded:
        return True
    if any(part.lower().endswith(".zip") for part in slashed.split("/")[:-1]):
        return True
    module = ntpath if windows else posixpath
    path = module.normpath(exe)
    if windows:
        path = ntpath.normcase(path)
    for root in _temp_roots(windows):
        root = module.normpath(root)
        if windows:
            root = ntpath.normcase(root)
        if path.startswith(root.rstrip(module.sep) + module.sep):
            return True
    return False


# --- the service's own ----------------------------------------------------------------------


def _backend() -> Optional[_Backend]:
    if sys.platform == "win32":
        return _windows
    if sys.platform.startswith("linux"):
        return _linux
    return None


def _frozen(frozen: Optional[bool]) -> bool:
    return bool(getattr(sys, "frozen", False)) if frozen is None else frozen


def _kind_types(kind: str) -> tuple[frozenset[str], frozenset[str]]:
    try:
        return _TYPES[kind]
    except KeyError:
        raise ValueError(f"not a kind of file QiTV registers for: {kind!r}") from None


def _recorded(config: Any) -> dict[str, str]:
    """A copy of the record (the config's default is shared, so never changed in place)."""
    record = getattr(config, "file_types", None)
    if not isinstance(record, dict):
        return {}
    return {k: v for k, v in record.items() if isinstance(k, str) and isinstance(v, str)}


def _attempt(action: str, call: Callable[[], object], kind: str) -> bool:
    """`call()`'s answer; False, logged, when it raised (a worker must still answer)."""
    try:
        return bool(call())
    except Exception:
        logger.warning("Couldn't %s QiTV for %s", action, kind, exc_info=True)
        return False


def _mimes(extensions: frozenset[str]) -> list[str]:
    found: dict[str, None] = {}
    for ext in sorted(extensions):
        for mime in MIME_TYPES[ext]:
            found[mime] = None
    return list(found)


_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _windows_shaped(path: str) -> bool:
    return bool(_WINDOWS_PATH.match(path)) or "\\" in path


def _temp_roots(windows: bool) -> list[str]:
    """The temporary folders, from the environment (tempfile.gettempdir's first call writes a
    probe file; its answer is used once it has one)."""
    roots = [os.environ.get(name, "") for name in ("TMPDIR", "TEMP", "TMP")]
    if tempfile.tempdir:
        roots.append(tempfile.tempdir)
    if windows:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(ntpath.join(local, "Temp"))
        system_root = os.environ.get("SystemRoot") or os.environ.get("windir")
        if system_root:
            roots.append(ntpath.join(system_root, "Temp"))
    else:
        roots += ["/tmp", "/var/tmp"]
    return [r for r in roots if r]


def _asset(name: str) -> Path:
    """assets/<name> next to the sources, or in a PyInstaller bundle's data."""
    bundle = getattr(sys, "_MEIPASS", None)
    root = Path(bundle) if bundle else Path(__file__).resolve().parent.parent
    return root / "assets" / name


# --- Windows --------------------------------------------------------------------------------

# Microsoft's "File Types", "Programmatic Identifiers", "How to Include an Application in the
# Open With Dialog Box", "Application Registration", "Default Programs" and "Launch the
# Default Apps settings page".
_CLASSES = "Software\\Classes"
_CAPABILITIES = f"Software\\{APP_NAME}\\Capabilities"
_FILE_ASSOCIATIONS = f"{_CAPABILITIES}\\FileAssociations"
_REGISTERED_APPLICATIONS = "Software\\RegisteredApplications"
PROGIDS = {"videos": "QiTV.Video"}
SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_IDLIST = 0x0000  # what SHCNE_ASSOCCHANGED takes; both items are NULL


def _winreg() -> Any:
    import winreg  # Windows only: imported where it's used

    return winreg


def _assoc_changed() -> None:
    """Tell the shell the associations changed (else it may not see them before a restart)."""
    try:
        import ctypes

        notify = ctypes.windll.shell32.SHChangeNotify
        notify.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        notify.restype = None
        notify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    except (AttributeError, OSError) as e:
        logger.warning("Couldn't tell Windows the file types changed (%s)", type(e).__name__)


class _WindowsBackend:
    """HKEY_CURRENT_USER\\Software\\Classes and HKEY_CURRENT_USER\\Software only. Registering
    takes no default: Windows asks in its Default Apps page (`open_default_apps`)."""

    def register(
        self, kind: str, exe: str, *, defaults: bool = True, previous: Optional[str] = None
    ) -> bool:
        reg = _winreg()
        try:
            self._write(reg, kind, exe)
        except OSError as e:
            logger.warning("Couldn't register QiTV for %s (%s)", kind, type(e).__name__)
            if previous is None:  # a first registration: take back what was written
                try:
                    self._remove(reg, kind, exe)
                except OSError:
                    logger.warning("Couldn't take back a part of QiTV's registration")
            _assoc_changed()
            return False
        if previous and _exe_name(previous).lower() != _exe_name(exe).lower():
            try:
                self._remove_supported_types(reg, kind, previous)
            except OSError as e:
                logger.info("The renamed executable's old entry stays (%s)", type(e).__name__)
        _assoc_changed()
        return True

    def unregister(self, kind: str, exe: str) -> bool:
        reg = _winreg()
        try:
            self._remove(reg, kind, exe)
            ok = True
        except OSError as e:
            logger.warning("Couldn't unregister QiTV for %s (%s)", kind, type(e).__name__)
            ok = False
        _assoc_changed()
        return ok

    def _write(self, reg: Any, kind: str, exe: str) -> None:
        every, chosen = _kind_types(kind)
        progid = PROGIDS[kind]
        _set_values(reg, f"{_CLASSES}\\{progid}\\DefaultIcon", {"": f'"{exe}",0'})
        _set_values(reg, f"{_CLASSES}\\{progid}\\shell\\open\\command", {"": f'"{exe}" "%1"'})
        for ext in sorted(every):
            _set_values(reg, f"{_CLASSES}\\.{ext}\\OpenWithProgids", {progid: ""})
        _set_values(
            reg,
            f"{_CLASSES}\\Applications\\{_exe_name(exe)}\\SupportedTypes",
            {f".{ext}": "" for ext in sorted(every)},
        )
        _set_values(
            reg,
            _CAPABILITIES,
            {"ApplicationName": APP_NAME, "ApplicationDescription": DESCRIPTION},
        )
        _set_values(reg, _FILE_ASSOCIATIONS, {f".{ext}": progid for ext in sorted(chosen)})
        _set_values(reg, _REGISTERED_APPLICATIONS, {APP_NAME: _CAPABILITIES})

    def _remove(self, reg: Any, kind: str, exe: str) -> None:
        every, chosen = _kind_types(kind)
        progid = PROGIDS[kind]
        _delete_tree(reg, f"{_CLASSES}\\{progid}")
        for ext in sorted(every):
            path = f"{_CLASSES}\\.{ext}\\OpenWithProgids"
            _delete_values(reg, path, [progid])
            _prune(reg, path, _CLASSES)
        self._remove_supported_types(reg, kind, exe)
        _delete_values(reg, _FILE_ASSOCIATIONS, [f".{ext}" for ext in chosen])
        _prune(reg, _FILE_ASSOCIATIONS, _CAPABILITIES)
        if not _exists(reg, _FILE_ASSOCIATIONS):  # no kind left: QiTV leaves Default Programs
            _delete_values(reg, _CAPABILITIES, ["ApplicationName", "ApplicationDescription"])
            _prune(reg, _CAPABILITIES, "Software")
            _delete_values(reg, _REGISTERED_APPLICATIONS, [APP_NAME])
            _prune(reg, _REGISTERED_APPLICATIONS, "Software")

    def _remove_supported_types(self, reg: Any, kind: str, exe: str) -> None:
        """The exe's SupportedTypes; its Applications key stays when it holds more (Windows
        writes a command there for an Open with → Browse… of the same exe)."""
        path = f"{_CLASSES}\\Applications\\{_exe_name(exe)}\\SupportedTypes"
        _delete_values(reg, path, [f".{ext}" for ext in _kind_types(kind)[0]])
        _prune(reg, path, _CLASSES)


def _exe_name(exe: str) -> str:
    return ntpath.basename(exe)


def _set_values(reg: Any, path: str, values: dict[str, str]) -> None:
    with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_WRITE) as key:
        for name, value in values.items():
            reg.SetValueEx(key, name, 0, reg.REG_SZ, value)


def _delete_values(reg: Any, path: str, names: list[str]) -> None:
    try:
        key = reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_SET_VALUE)
    except FileNotFoundError:
        return
    with key:
        for name in names:
            try:
                reg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


def _exists(reg: Any, path: str) -> bool:
    try:
        reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_READ).Close()
        return True
    except FileNotFoundError:
        return False


def _prune(reg: Any, path: str, stop: str) -> None:
    """Delete the key at `path` while it holds no value and no subkey, then its parent the
    same way, up to `stop` (which stays)."""
    while path.lower() != stop.lower() and path.lower().startswith(stop.lower() + "\\"):
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_READ) as key:
                subkeys, values, _modified = reg.QueryInfoKey(key)
        except FileNotFoundError:
            path = path.rpartition("\\")[0]
            continue
        if subkeys or values:
            return
        reg.DeleteKey(reg.HKEY_CURRENT_USER, path)
        path = path.rpartition("\\")[0]


def _delete_tree(reg: Any, path: str) -> None:
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_READ) as key:
            children: list[str] = []
            while True:
                try:
                    children.append(reg.EnumKey(key, len(children)))
                except OSError:  # no more
                    break
    except FileNotFoundError:
        return
    for child in children:
        _delete_tree(reg, f"{path}\\{child}")
    reg.DeleteKey(reg.HKEY_CURRENT_USER, path)


_windows = _WindowsBackend()


# --- Linux ----------------------------------------------------------------------------------

# freedesktop.org's Desktop Entry Specification (recognized keys, value types, the Exec key),
# Association between MIME types and applications (mimeapps.list), the Icon Theme
# Specification (hicolor), and xdg-mime's manual.
DESKTOP_ID = "qitv-open.desktop"
ICON_NAME = "qitv"
_ICON_MARK = "X-QiTV-Installed-Icon=true"  # the icon is this file's: unregistering removes it
_MIMEAPPS_GROUPS = ("Default Applications", "Added Associations", "Removed Associations")
_QUOTED = re.compile(r'(["`$\\])')


def desktop_exec(exe: str) -> str:
    """The Exec value that starts `exe` with the files opened: the path quoted (", `, $ and \\
    escaped inside the quotes, % doubled), `%F`, then the escapes every string value takes
    (each backslash doubled; newline, tab and carriage return spelled out)."""
    argument = '"' + _QUOTED.sub(r"\\\1", exe).replace("%", "%%") + '"'
    command = f"{argument} %F"
    return (
        command.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    )


def _xdg_dir(name: str, fallback: str) -> Path:
    """$`name`, or ~/`fallback` when it is unset, empty or relative (the Base Directory
    Specification ignores a relative one)."""
    value = os.environ.get(name, "")
    return Path(value) if value and os.path.isabs(value) else Path.home() / fallback


class _LinuxBackend:
    """The user's own XDG folders only."""

    def register(
        self, kind: str, exe: str, *, defaults: bool = True, previous: Optional[str] = None
    ) -> bool:
        desktop = self._desktop_path()
        icon_ours = False
        try:
            desktop.parent.mkdir(parents=True, exist_ok=True)
            icon_ours = self._install_icon()
            self._write_desktop(desktop, exe, mime_types(kind), icon_ours)
        except OSError as e:
            logger.warning("Couldn't write QiTV's desktop file (%s)", type(e).__name__)
            self._remove_files(icon=icon_ours)
            return False
        self._run("update-desktop-database", str(desktop.parent))
        if defaults and not self._take_defaults(kind):
            self._remove_files()
            self._run("update-desktop-database", str(desktop.parent))
            return False
        return True

    def unregister(self, kind: str, exe: str) -> bool:
        ok = self._remove_files()
        applications = self._desktop_path().parent
        if applications.is_dir():
            self._run("update-desktop-database", str(applications))
        lists = (
            _xdg_dir("XDG_CONFIG_HOME", ".config") / "mimeapps.list",
            # where xdg-utils before 1.1 wrote (deprecated, still read)
            _xdg_dir("XDG_DATA_HOME", ".local/share") / "applications" / "mimeapps.list",
        )
        for mimeapps in lists:
            try:
                _drop_from_mimeapps(mimeapps, DESKTOP_ID)
            except OSError as e:
                logger.warning("Couldn't edit %s (%s)", mimeapps, type(e).__name__)
                ok = False
        return ok

    def _desktop_path(self) -> Path:
        return _xdg_dir("XDG_DATA_HOME", ".local/share") / "applications" / DESKTOP_ID

    def _icon_path(self) -> Path:
        data = _xdg_dir("XDG_DATA_HOME", ".local/share")
        return data / "icons" / "hicolor" / "256x256" / "apps" / f"{ICON_NAME}.png"

    def _icon_ours(self) -> bool:
        """Whether the icon is the one registering put there (QiTV's desktop file says so)."""
        try:
            text = self._desktop_path().read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            return False
        return _ICON_MARK in text.splitlines()

    def _install_icon(self) -> bool:
        """Copy QiTV's logo into the hicolor theme; False (nothing copied) when an icon of that
        name is there already and isn't ours (scripts/install-linux.sh puts one there)."""
        target = self._icon_path()
        if target.exists() and not self._icon_ours():
            return False
        source = _asset(f"{ICON_NAME}.png")
        if not source.is_file():
            logger.info("No %s to install as the icon", source)
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        with atomic_write(str(target)) as f:
            f.write(source.read_bytes())
        os.chmod(target, 0o644)
        return True

    def _write_desktop(self, path: Path, exe: str, mimes: list[str], icon_ours: bool) -> None:
        lines = [
            "[Desktop Entry]",
            "Type=Application",
            f"Name={APP_NAME}",
            "Comment=Video and IPTV player",
            f"Exec={desktop_exec(exe)}",
            f"Icon={ICON_NAME}",
            "Terminal=false",
            "Categories=AudioVideo;Video;Player;",
            "MimeType=" + "".join(f"{m};" for m in mimes),
            "StartupWMClass=qitv",
        ]
        if icon_ours:
            lines.append(_ICON_MARK)
        with atomic_write(str(path)) as f:
            f.write(("\n".join(lines) + "\n").encode("utf-8"))
        os.chmod(path, 0o644)

    def _remove_files(self, icon: Optional[bool] = None) -> bool:
        """Remove the desktop file, and the icon when it is ours (`icon`, else as the desktop
        file says); False when one stays."""
        ok = True
        paths = [self._desktop_path()]
        if self._icon_ours() if icon is None else icon:
            paths.append(self._icon_path())
        for path in reversed(paths):  # the icon first: the mark goes with the desktop file
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning("Couldn't remove %s (%s)", path, type(e).__name__)
                ok = False
        return ok

    def _take_defaults(self, kind: str) -> bool:
        """`xdg-mime default` for each default MIME type; False only when it ran and failed for
        every one (when it isn't installed, QiTV is only in Open With)."""
        answers = []
        for mime in default_mime_types(kind):
            answer = self._run("xdg-mime", "default", DESKTOP_ID, mime)
            if answer is None:
                return True  # not installed (logged)
            answers.append(answer)
        return 0 in answers

    def _run(self, tool: str, *args: str) -> Optional[int]:
        """`tool args` with the environment from before QiTV (a frozen build's library path
        would break what it starts); its exit code, or None when it isn't installed."""
        env = player_env()
        found = shutil.which(tool, path=env.get("PATH"))
        if found is None:
            logger.info("%s isn't installed; skipped", tool)
            return None
        try:
            done = subprocess.run(
                [found, *args],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=SUBPROCESS_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("%s %s took over %d s", tool, " ".join(args), SUBPROCESS_TIMEOUT_S)
            return -1
        except OSError as e:
            logger.warning("%s didn't start (%s)", tool, type(e).__name__)
            return -1
        if done.returncode:
            error = done.stderr.decode("utf-8", "replace").strip()[:300]
            logger.warning("%s %s answered %d: %s", tool, " ".join(args), done.returncode, error)
        return done.returncode


def _drop_from_mimeapps(path: Path, desktop_id: str) -> None:
    """Remove `desktop_id` from every list in mimeapps.list's three groups, and a key whose list
    is then empty; every other line stays as it was, and so does the file's mode (a link to a
    dotfiles folder stays a link: its target is written)."""
    path = Path(os.path.realpath(path))
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except UnicodeDecodeError:  # not ours to rewrite; without the desktop file it names nothing
        logger.warning("%s isn't UTF-8; QiTV's entries there stay (they name no app now)", path)
        return
    group = None
    kept: list[str] = []
    changed = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            group = stripped[1:-1]
        elif group in _MIMEAPPS_GROUPS and "=" in line and not stripped.startswith("#"):
            key, _, value = line.partition("=")
            body = value.rstrip("\r\n")
            ending = value.removeprefix(body)
            ids = [i.strip() for i in body.split(";") if i.strip()]
            if desktop_id in ids:
                changed = True
                others = [i for i in ids if i != desktop_id]
                if others:
                    kept.append(f"{key}={';'.join(others)};{ending}")
                continue
        kept.append(line)
    if not changed:
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    with atomic_write(str(path)) as f:
        f.write("".join(kept).encode("utf-8"))
    os.chmod(path, mode)


_linux = _LinuxBackend()
