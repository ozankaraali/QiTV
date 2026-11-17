"""QiTV registered with the system for a kind of file (videos), per user: it shows in Open With
and can be made the default player. No admin is ever asked, and unregistering removes exactly
what QiTV wrote.

Everything here may touch the disk, the registry, a system framework or start a program, so it
runs on a worker thread, except `status` and `temporary_location`, which only look at strings.
Nothing here saves the config: `register`, `unregister` and `refresh` assign
`config.file_types` a new dict (`{kind: the executable registered}`, on macOS the `.app`), and
the caller saves it on the GUI thread.

- **Windows** (HKEY_CURRENT_USER only): the ProgID `QiTV.Video`, `OpenWithProgids` for every
  type, the exe's `Applications` entry, and Default Programs' `Capabilities` with its
  `RegisteredApplications` value; Windows' own Default Apps page then lets the user choose.
  While a registered QiTV runs, the ProgID's open verb also has a `ddeexec` (`add_ddeexec`):
  Explorer hands a double-clicked video to it over DDE (services.dde_windows), and starts
  QiTV with the file when none runs. Unregistering takes it with the ProgID.
- **Linux**: `$XDG_DATA_HOME/applications/qitv-open.desktop`, the icon in the user's hicolor
  theme, and `xdg-mime default` for the default types (which writes the user's mimeapps.list).
- **macOS**: the bundle's Info.plist declares the types (qitv-macos.spec), so Open With lists
  QiTV with nothing registered; registering makes it the default for a short list through
  Launch Services. macOS can't give a default back to the app before: unregistering forgets
  the record, and Finder's Get Info → Open with → Change All picks another app.

A source run never registers or unregisters: there `sys.executable` is Python, and it may share
the packaged app's settings. Nor does a QiTV started from a place that goes away register (a
temporary folder, inside a zip, macOS's App Translocation or a mounted disk image).
"""

from __future__ import annotations

from dataclasses import dataclass
import enum
import functools
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
import threading
from types import SimpleNamespace
from typing import Any, Callable, Optional, Protocol, TypeVar, cast
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

# Each type's Uniform Type Identifiers on macOS (qitv-macos.spec claims them all): the one
# Launch Services is told for a default first, then another that apps declare for it too.
# macOS 13, the oldest QiTV runs on, declares all but mkv and ogv itself (CoreTypes, and
# QuickTime Player for m2ts and mts). The bundle imports those two, and also the four that
# macOS declares only as imports (webm, flv, wmv, m2ts/mts), saying what macOS says.
MAC_UTIS: dict[str, tuple[str, ...]] = {
    "mp4": ("public.mpeg-4",),
    "m4v": ("com.apple.m4v-video",),
    "mkv": ("org.matroska.mkv",),
    "avi": ("public.avi",),
    "mov": ("com.apple.quicktime-movie",),
    "webm": ("org.webmproject.webm",),
    "ts": ("public.mpeg-2-transport-stream",),
    "m2ts": ("public.avchd-mpeg-2-transport-stream",),
    "mts": ("public.avchd-mpeg-2-transport-stream",),
    "mpg": ("public.mpeg",),
    "mpeg": ("public.mpeg",),
    "flv": ("com.adobe.flash.video",),
    "wmv": ("com.microsoft.windows-media-wmv",),
    "ogv": ("org.xiph.ogg-video", "org.xiph.ogv"),
}

SUBPROCESS_TIMEOUT_S = 10


class Status(enum.Enum):
    REGISTERED = "registered"
    NOT_REGISTERED = "not registered"
    TEMPORARY = "temporary"  # QiTV runs from a place that goes away: move it first
    UNAVAILABLE = "unavailable"  # a source run, or a system without a backend


@dataclass(frozen=True)
class RegisterResult:
    """What `register` did; truthy exactly when `ok`.

    - `ok`: QiTV is registered for the kind, and recorded.
    - `defaults`: the system took QiTV as the default for the kind's default types. False
      when QiTV is in Open With only and the user chooses it by hand: on Linux without
      xdg-mime, or when xdg-mime failed for every type. On macOS a registration is its
      defaults (the bundle's Open With needs none), so Launch Services refusing every type
      makes `ok` False too. On Windows it is True once Default Programs lists QiTV, and
      Windows' own Default Apps page then asks the user (`open_default_apps`).
    """

    ok: bool
    defaults: bool = False

    def __bool__(self) -> bool:
        return self.ok


class _Backend(Protocol):
    def register(
        self, kind: str, exe: str, *, defaults: bool = True, previous: Optional[str] = None
    ) -> RegisterResult: ...

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
) -> RegisterResult:
    """Register `exe` (on macOS the app holding it) for `kind`, and record it.

    The answer is a `RegisterResult`: truthy when QiTV was registered, and its `defaults` say
    whether the system also took QiTV as the default (else the Settings hint says to choose it
    by hand). It is falsy, with nothing recorded, when QiTV could not be registered or must not
    be: a source run, a temporary place, a system without a backend. Registering again tells
    the backend what was registered before, so a failure leaves that one working (only a first
    registration is taken back), and a renamed Windows exe's old entry goes once the new one
    is in."""
    _kind_types(kind)
    refused = RegisterResult(False)
    if not _frozen(frozen):
        logger.info("A run from source doesn't register itself for %s", kind)
        return refused
    exe = _own_path(exe)
    if temporary_location(exe):
        logger.info("QiTV runs from a temporary place; not registered for %s", kind)
        return refused
    backend = _backend()
    if backend is None:
        return refused
    record = _recorded(config)
    previous = record.get(kind)
    result = _as_result(
        _attempt("register", lambda: backend.register(kind, exe, previous=previous), kind)
    )
    if result:
        record[kind] = exe
        config.file_types = record
    return result


def unregister(kind: str, config: Any, frozen: Optional[bool] = None) -> bool:
    """Remove what registering for `kind` wrote (for the executable recorded), and the record;
    False (the record kept, to try again) when that failed, and from a source run (which may
    share the packaged app's settings: its registration stays)."""
    _kind_types(kind)
    if not _frozen(frozen):
        logger.info("A run from source doesn't unregister %s", kind)
        return False
    backend = _backend()
    if backend is None:
        return False
    record = _recorded(config)
    exe = record.get(kind) or _own_path(sys.executable)
    if not _attempt("unregister", lambda: backend.unregister(kind, exe), kind):
        return False
    record.pop(kind, None)
    config.file_types = record
    return True


def refresh(config: Any, exe: str = sys.executable, frozen: Optional[bool] = None) -> None:
    """At start: each kind recorded for another executable (QiTV was moved or renamed) is
    registered for `exe` instead, without taking any default again (the user may have chosen
    another app since). Nothing from source or from a temporary place."""
    if not _frozen(frozen):
        return
    exe = _own_path(exe)
    if temporary_location(exe):
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


def add_ddeexec(exe: str) -> bool:
    """Windows, while QiTV runs and its DDE server answers (services.dde_windows): Explorer
    hands a double-clicked video to it over DDE instead of starting QiTV again. Only where QiTV
    is registered; False when it isn't or the key couldn't be written. Off the GUI thread."""
    return _windows.add_ddeexec(exe)


def remove_ddeexec() -> bool:
    """Windows: Explorer starts QiTV with the file again (at quit, or a server that didn't
    start). False, logged, when a key stayed; it never raises."""
    return _windows.remove_ddeexec()


def temporary_location(exe: str) -> bool:
    """Whether `exe` sits where it won't stay: the system's temporary folder or below it
    (Windows' `AppData\\Local\\Temp` too), inside a `.zip` (Explorer's zip view, and the
    `Temp1_x.zip` folder it extracts to), in macOS's App Translocation, or in an app at the
    top of a mounted volume (the disk image QiTV comes in: `/Volumes/QiTV/QiTV.app`; an app in
    a folder of a disk stays). Strings only: no disk is touched (the Settings row asks this)."""
    if not exe:
        return False
    windows = _windows_shaped(exe)
    slashed = exe.replace("\\", "/")
    folded = slashed.lower()
    if "/apptranslocation/" in folded:
        return True
    if not windows and _DISK_IMAGE_APP.match(slashed):
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
    if sys.platform == "darwin":
        return _mac
    if sys.platform.startswith("linux"):
        return _linux
    return None


def _own_path(exe: str) -> str:
    """What registering records for `exe`: the app that holds a macOS executable
    (`<name>.app/Contents/MacOS/<exe>`), which Launch Services knows QiTV by and Finder moves
    as one; any other path as it is."""
    parts = exe.split("/")
    for i in range(len(parts) - 4, 0, -1):  # the innermost app
        inside = (parts[i + 1], parts[i + 2])
        if parts[i].lower().endswith(".app") and inside == ("Contents", "MacOS"):
            return "/".join(parts[: i + 1])
    return exe


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


def _attempt(action: str, call: Callable[[], object], kind: str) -> object:
    """`call()`'s answer; False, logged, when it raised (a worker must still answer)."""
    try:
        return call()
    except Exception:
        logger.warning("Couldn't %s QiTV for %s", action, kind, exc_info=True)
        return False


def _as_result(answer: object) -> RegisterResult:
    """A backend's answer as a RegisterResult (a plain bool: the defaults went as it did)."""
    if isinstance(answer, RegisterResult):
        return answer
    return RegisterResult(bool(answer), bool(answer))


def _mimes(extensions: frozenset[str]) -> list[str]:
    found: dict[str, None] = {}
    for ext in sorted(extensions):
        for mime in MIME_TYPES[ext]:
            found[mime] = None
    return list(found)


_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_DISK_IMAGE_APP = re.compile(r"^/Volumes/[^/]+/[^/]+\.app(?:/|$)", re.IGNORECASE)


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
# Open With Dialog Box", "Application Registration", "Default Programs", "Launch the Default
# Apps settings page" and "How to Associate Verbs with DDE Commands".
_CLASSES = "Software\\Classes"
_CAPABILITIES = f"Software\\{APP_NAME}\\Capabilities"
_FILE_ASSOCIATIONS = f"{_CAPABILITIES}\\FileAssociations"
_REGISTERED_APPLICATIONS = "Software\\RegisteredApplications"
PROGIDS = {"videos": "QiTV.Video"}
# The open verb's DDE while QiTV runs: its default value is the command; the application,
# topic and ifexec subkeys' default values are the service, the topic and what Explorer sends
# to a QiTV it started itself when the conversation failed (the file is on its command line).
_DDEEXEC = "shell\\open\\ddeexec"
IFEXEC = "[noop]"
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


_Method = TypeVar("_Method", bound=Callable[..., Any])
# Settings' press, the start's refresh and the DDE key's writes run on workers of their own:
# one at a time, so a key written meanwhile never stops an unregister from taking the ProgID.
_windows_lock = threading.RLock()


def _one_at_a_time(method: _Method) -> _Method:
    @functools.wraps(method)
    def locked(*args: Any, **kwargs: Any) -> Any:
        with _windows_lock:
            return method(*args, **kwargs)

    return cast(_Method, locked)


class _WindowsBackend:
    """HKEY_CURRENT_USER\\Software\\Classes and HKEY_CURRENT_USER\\Software only. Registering
    takes no default: Windows asks in its Default Apps page (`open_default_apps`)."""

    @_one_at_a_time
    def register(
        self, kind: str, exe: str, *, defaults: bool = True, previous: Optional[str] = None
    ) -> RegisterResult:
        """Write the registration for `exe`. When it fails, a first registration (`previous`
        None) is taken back, and so is a renamed exe's own entry: one that worked before stays
        as it was."""
        reg = _winreg()
        try:
            self._write(reg, kind, exe)
        except OSError as e:
            logger.warning("Couldn't register QiTV for %s (%s)", kind, type(e).__name__)
            if previous is None:
                if not self._remove(reg, kind, exe):
                    logger.warning("Couldn't take back all of QiTV's registration")
            elif _renamed(previous, exe):  # the old name's entry is the one that works
                try:
                    self._remove_supported_types(reg, kind, exe)
                except OSError as error:
                    logger.warning("The new name's entry stays (%s)", type(error).__name__)
            _assoc_changed()
            return RegisterResult(False)
        if previous and _renamed(previous, exe):
            try:
                self._remove_supported_types(reg, kind, previous)
            except OSError as e:
                logger.info("The renamed executable's old entry stays (%s)", type(e).__name__)
        _assoc_changed()
        return RegisterResult(True, True)

    @_one_at_a_time
    def unregister(self, kind: str, exe: str) -> bool:
        reg = _winreg()
        ok = self._remove(reg, kind, exe)
        _assoc_changed()
        return ok

    @_one_at_a_time
    def add_ddeexec(self, exe: str) -> bool:
        """The open verb's ddeexec, in each ProgID QiTV is registered with (its command is
        there: a ProgID is never made for it). A stale one, left by a crash, is written over;
        one that can't be written whole is taken back. True when every registered ProgID got
        it, False when there was none."""
        from services.dde_windows import COMMAND, SERVICE, TOPIC

        reg = _winreg()
        found, ok = False, True
        for progid in PROGIDS.values():
            verb = f"{_CLASSES}\\{progid}\\shell\\open"
            ddeexec = f"{_CLASSES}\\{progid}\\{_DDEEXEC}"
            try:
                command = _default(reg, f"{verb}\\command")
                if command is None:
                    continue
                found = True
                if command.lower() != f'"{exe}" "%1"'.lower():  # refresh moves it here
                    logger.info("The registration names another QiTV; this one answers")
                _set_values(reg, ddeexec, {"": COMMAND})
                subkeys = (("Application", SERVICE), ("Topic", TOPIC), ("ifexec", IFEXEC))
                for subkey, value in subkeys:
                    _set_values(reg, f"{ddeexec}\\{subkey}", {"": value})
            except OSError as e:
                logger.warning("Couldn't tell Explorer QiTV runs (%s)", type(e).__name__)
                ok = False
                try:
                    _delete_tree(reg, ddeexec)
                except OSError as error:
                    logger.warning("A part of the DDE key stays (%s)", type(error).__name__)
        if not found:
            logger.info("QiTV isn't registered for videos: Explorer starts it for each")
            return False
        _assoc_changed()
        return ok

    @_one_at_a_time
    def remove_ddeexec(self) -> bool:
        """Each ProgID's ddeexec, and the keys that leaves empty (a ProgID unregistered
        meanwhile leaves none); the shell is told only when one was there."""
        reg = _winreg()
        ok, removed = True, False
        for progid in PROGIDS.values():
            ddeexec = f"{_CLASSES}\\{progid}\\{_DDEEXEC}"
            try:
                if not _exists(reg, ddeexec):
                    continue
                removed = True
                _delete_tree(reg, ddeexec)
                _prune(reg, ddeexec.rpartition("\\")[0], _CLASSES)
            except OSError as e:
                logger.warning("Couldn't remove QiTV's DDE key (%s)", type(e).__name__)
                ok = False
        if removed:
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

    def _remove(self, reg: Any, kind: str, exe: str) -> bool:
        """Remove what `_write` wrote for `kind`, a key at a time: one that can't be changed is
        logged and the rest still goes. True when everything went."""
        every, chosen = _kind_types(kind)
        progid = PROGIDS[kind]
        ok = True

        def step(what: str, call: Callable[..., object], *args: Any) -> object:
            nonlocal ok
            try:
                return call(*args)
            except OSError as e:
                logger.warning("Couldn't remove QiTV's %s (%s)", what, type(e).__name__)
                ok = False
                return None

        step(progid, _delete_tree, reg, f"{_CLASSES}\\{progid}")
        for ext in sorted(every):
            path = f"{_CLASSES}\\.{ext}\\OpenWithProgids"
            step(f".{ext} Open With entry", _drop_values, reg, path, [progid], _CLASSES)
        step("supported types", self._remove_supported_types, reg, kind, exe)
        step(
            "file associations",
            _drop_values,
            reg,
            _FILE_ASSOCIATIONS,
            [f".{ext}" for ext in chosen],
            _CAPABILITIES,
        )
        # no kind left: QiTV leaves Default Programs
        if step("Default Programs entry", _exists, reg, _FILE_ASSOCIATIONS) is False:
            names = ["ApplicationName", "ApplicationDescription"]
            step("capabilities", _drop_values, reg, _CAPABILITIES, names, "Software")
            step(
                "Default Programs entry",
                _drop_values,
                reg,
                _REGISTERED_APPLICATIONS,
                [APP_NAME],
                "Software",
            )
        return ok

    def _remove_supported_types(self, reg: Any, kind: str, exe: str) -> None:
        """The exe's SupportedTypes; its Applications key stays when it holds more (Windows
        writes a command there for an Open with → Browse… of the same exe)."""
        path = f"{_CLASSES}\\Applications\\{_exe_name(exe)}\\SupportedTypes"
        _delete_values(reg, path, [f".{ext}" for ext in _kind_types(kind)[0]])
        _prune(reg, path, _CLASSES)


def _exe_name(exe: str) -> str:
    return ntpath.basename(exe)


def _renamed(previous: str, exe: str) -> bool:
    """Whether `exe` has another file name than `previous` (so its own Applications entry):
    a moved exe of the same name shares the entry."""
    return _exe_name(previous).lower() != _exe_name(exe).lower()


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


def _drop_values(reg: Any, path: str, names: list[str], stop: str) -> None:
    """Delete these values at `path`, then the keys left empty up to `stop`."""
    _delete_values(reg, path, names)
    _prune(reg, path, stop)


def _default(reg: Any, path: str) -> Optional[str]:
    """The default value of the key at `path`; None without the key or a string there."""
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_READ) as key:
            value, _kind = reg.QueryValueEx(key, "")
    except FileNotFoundError:
        return None
    return value if isinstance(value, str) else None


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
    ) -> RegisterResult:
        """Write the desktop file (and the icon), then take the defaults. A write that fails
        takes back only a first registration (`previous` None): one that worked stays. When
        no default could be taken, QiTV stays in Open With (`defaults` False)."""
        desktop = self._desktop_path()
        icon_ours = False
        try:
            desktop.parent.mkdir(parents=True, exist_ok=True)
            icon_ours = self._install_icon()
            self._write_desktop(desktop, exe, mime_types(kind), icon_ours)
        except OSError as e:
            logger.warning("Couldn't write QiTV's desktop file (%s)", type(e).__name__)
            if previous is None:
                self._remove_files(icon=icon_ours)
            return RegisterResult(False)
        self._run("update-desktop-database", str(desktop.parent))
        return RegisterResult(True, defaults and self._take_defaults(kind))

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
        """`xdg-mime default` for each default MIME type; whether it took at least one (False
        when it isn't installed, or failed for every one: QiTV is then in Open With only)."""
        answers = []
        for mime in default_mime_types(kind):
            answer = self._run("xdg-mime", "default", DESKTOP_ID, mime)
            if answer is None:
                return False  # not installed (logged)
            answers.append(answer)
        if 0 not in answers:
            logger.warning("xdg-mime took no default; QiTV is in Open With only")
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


# --- macOS ----------------------------------------------------------------------------------

# Apple's LaunchServices headers (LSInfo.h: LSSetDefaultRoleHandlerForContentType, deprecated
# since macOS 12 for an Objective-C call; LSConstants.h: kLSRolesAll), CoreFoundation's
# CFString.h (CFStringCreateWithCString, kCFStringEncodingUTF8), and "Defining file and data
# types for your app" (a type the system declares isn't declared again; see qitv-macos.spec).
MAC_BUNDLE_ID = "com.ozankaraali.QiTV"  # qitv-macos.spec's bundle_identifier
LS_ROLES_ALL = 0xFFFFFFFF  # every role, as Finder's Change All: one held before doesn't win
CF_STRING_ENCODING_UTF8 = 0x08000100
_CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_CORE_SERVICES = "/System/Library/Frameworks/CoreServices.framework/CoreServices"
_MAC_DEFAULTS = {"videos": MAC_DEFAULT_VIDEO}  # each kind's short list (one question each)


def _core_services() -> Any:
    """CoreFoundation's CFStringCreateWithCString and CFRelease, and Launch Services'
    LSSetDefaultRoleHandlerForContentType, with their C types (a CFStringRef is a pointer,
    which ctypes would otherwise cut to a C int; an OSStatus is 32 bits). macOS only."""
    import ctypes  # loaded where it's used

    foundation = ctypes.CDLL(_CORE_FOUNDATION)
    services = ctypes.CDLL(_CORE_SERVICES)
    create = foundation.CFStringCreateWithCString
    create.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    create.restype = ctypes.c_void_p
    release = foundation.CFRelease
    release.argtypes = [ctypes.c_void_p]
    release.restype = None
    set_default = services.LSSetDefaultRoleHandlerForContentType
    set_default.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    set_default.restype = ctypes.c_int32
    return SimpleNamespace(
        CFStringCreateWithCString=create,
        CFRelease=release,
        LSSetDefaultRoleHandlerForContentType=set_default,
    )


class _MacBackend:
    """The bundle declares the types, so Open With lists QiTV with nothing written.
    Registering makes QiTV the default app for the kind's short list (what Finder's Get
    Info → Open with → Change All does); macOS 26.4 and later ask the user once for each."""

    def register(
        self, kind: str, exe: str, *, defaults: bool = True, previous: Optional[str] = None
    ) -> RegisterResult:
        """Truthy when Launch Services took at least one type (each refusal is logged with its
        OSStatus). With `defaults` False (a moved app), there's nothing to do: Launch Services
        knows QiTV by its bundle id."""
        if not defaults:
            return RegisterResult(True)
        try:
            lib = _core_services()
        except (OSError, AttributeError) as e:
            logger.warning("Couldn't reach Launch Services (%s)", type(e).__name__)
            return RegisterResult(False)
        handler = _cf_string(lib, MAC_BUNDLE_ID)
        if not handler:
            return RegisterResult(False)
        short_list = _MAC_DEFAULTS[kind]
        taken = 0
        try:
            for ext in short_list:
                taken += _take_default(lib, MAC_UTIS[ext][0], handler)
        finally:
            lib.CFRelease(handler)
        logger.info("QiTV is the default for %d of %d %s types", taken, len(short_list), kind)
        return RegisterResult(taken > 0, taken > 0)

    def unregister(self, kind: str, exe: str) -> bool:
        """Nothing to remove: the defaults stay QiTV's until the user picks another app in
        Finder (Launch Services can't give one back), and Open With lists QiTV as before."""
        logger.info("macOS keeps QiTV's %s defaults until Finder's Change All picks another", kind)
        return True


def _cf_string(lib: Any, text: str) -> Any:
    """A new CFString of `text` (the caller releases it); None, logged, when macOS made none."""
    ref = lib.CFStringCreateWithCString(None, text.encode("utf-8"), CF_STRING_ENCODING_UTF8)
    if not ref:
        logger.warning("macOS made no string of %s", text)
    return ref


def _take_default(lib: Any, uti: str, handler: Any) -> bool:
    """Make the bundle `handler` names the default for `uti`, in every role; whether macOS
    did."""
    content_type = _cf_string(lib, uti)
    if not content_type:
        return False
    try:
        status = lib.LSSetDefaultRoleHandlerForContentType(content_type, LS_ROLES_ALL, handler)
    finally:
        lib.CFRelease(content_type)
    if status:
        logger.warning("macOS didn't make QiTV the default for %s (OSStatus %d)", uti, status)
        return False
    return True


_mac = _MacBackend()
