"""Files opened in QiTV: at launch, from a second start, over DDE (Windows), and from many
starts at once; how long each takes to play.

    uv run python scripts/smoke_open_file.py --app dist/qitv-linux --out smoke-frozen \\
        --file tests/fixtures/smoke.mp4 --parallel 8
    uv run python scripts/smoke_open_file.py --app "uv run python main.py" --out /tmp/open \\
        --file tests/fixtures/smoke.mp4 --parallel 4

QiTV runs with a settings folder of its own (<out>/config), muted, with its control port on, and
its status says what it plays:

1. `<app> <file>` plays the file (`first_s`: from the start until the status names it).
2. `<app> <second>` hands its file to that QiTV and exits 0, the first's pid unchanged
   (`handover_s`: from that start until the status names the second file). Without --second, a
   copy of --file named "Dizi Bölüm 2 – test.mp4", a name that isn't plain ASCII.
3. Windows with --dde: QiTV is registered for videos in this user's HKCU (the checkout's
   backend) before the first start, so the running QiTV adds the ddeexec Explorer uses; a third
   file sent as Explorer sends it plays in `dde_s` (under DDE_LIMIT_S). It is unregistered at
   the end, whatever happened: for a CI runner's registry, not a user's.
4. Windows: a fourth file, its path longer than LONG_PATH_CHARS, plays from a hand-over
   (`long_ok`: the status names it and libVLC's log says it opened it).
5. --parallel N, once that QiTV quit: N starts at once, each with a file of its own (a
   multi-select in Explorer), on a fresh settings folder: one QiTV stays
   (`parallel_instances`), the others hand over and exit 0, and it plays one of the files.

Each figure goes to <out>/open-file.json as soon as it is known: `ok` says whether every check
passed, `failures` which didn't. Every wait is bounded, and every process started is stopped at
the end. The exit code is 1 when a check failed. The copies live in a temporary folder of their
own, removed at the end (<out> is uploaded by CI).
"""

from __future__ import annotations

import argparse
import json
import ntpath
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import IO, Any, Optional
import unicodedata
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qitv_control  # noqa: E402  (it puts the checkout on sys.path)

from services.open_paths import extended_path  # noqa: E402

START_TIMEOUT_S = 60.0  # the first start plays its file within this
HANDOVER_TIMEOUT_S = 60.0  # a later start's file is named within this
AFTER_EXIT_S = 10.0  # once a later start has exited, its file is named within this or never
EXIT_TIMEOUT_S = 30.0  # a start that handed over has exited within this, once its file plays
QUIT_TIMEOUT_S = 30.0
DDE_LIMIT_S = 1.0  # Explorer's double-click on a running QiTV
DDE_TIMEOUT_S = 10.0
DDEEXEC_WAIT_S = 15.0  # the running QiTV writes the ddeexec on a worker at its start
VLC_OPENED_WAIT_S = 15.0
PARALLEL_TIMEOUT_S = 180.0  # N one-file builds unpacking themselves at once
PARALLEL_MIN_S = 30.0  # the QiTVs are counted no sooner after the starts
PARALLEL_SETTLE_S = 5.0  # and no sooner after all but one have exited
POLL_S = 0.03  # dde_s is held to 1 s: the status is asked often
LONG_PATH_CHARS = 270
SECOND_NAME = "Dizi Bölüm 2 – test.mp4"
DDE_NAME = "DDE Bölüm 3 – test.mp4"
LONG_NAME = "qitv-long-path.mp4"
LONG_FOLDER = "a-folder-deep-enough-for-a-long-path"
RESULT = "open-file.json"
VLC_LOG = "open-file-libvlc.log"  # the first QiTV's libVLC, verbose (local files only)
VLC_OPENED = "successfully opened"  # libVLC's input, once its file is open
HANDED_OVER = "this start ends here"  # main.py's line for a start that handed over
WINDOWS = os.name == "nt"


def _command(app: str) -> list[str]:
    if WINDOWS:
        return [app]
    return shlex.split(app)


def _same_name(title: object, name: str) -> bool:
    """Whether the status's title is the file's name (macOS may spell ö in two ways)."""
    if not isinstance(title, str):
        return False
    return unicodedata.normalize("NFC", title) == unicodedata.normalize("NFC", name)


def _title(status: Optional[dict]) -> Optional[str]:
    playing = (status or {}).get("playing")
    if not isinstance(playing, dict):
        return None
    title = playing.get("title")
    return title if isinstance(title, str) else None


def _status() -> Optional[dict]:
    """The running QiTV's status, else None (no port yet, or no answer)."""
    try:
        answer = qitv_control.ask("status", timeout=5.0)
    except SystemExit, Exception:
        return None
    return answer if isinstance(answer, dict) and "error" not in answer else None


def _pid(status: Optional[dict]) -> Optional[int]:
    pid = (status or {}).get("pid")
    return pid if isinstance(pid, int) and pid else None


def _fresh_config(folder: Path, config: dict) -> None:
    """A settings folder with nothing of an earlier run (its control.json neither)."""
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _kill_tree(process: subprocess.Popen) -> None:
    """Stop `process` and everything it started (it may have unpacked a child)."""
    if WINDOWS:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        pass


def _long_copy(source: Path, folder: Path) -> str:
    """A copy of `source` under folders deep enough for its path to pass LONG_PATH_CHARS,
    made through the extended-length spelling; its path as Explorer passes it (plain)."""
    path = ntpath.abspath(str(folder))
    depth = 0
    while len(path) + 1 + len(LONG_NAME) <= LONG_PATH_CHARS:
        depth += 1
        path = ntpath.join(path, f"{depth:02d}-{LONG_FOLDER}")
    os.makedirs(extended_path(path), exist_ok=True)
    full = ntpath.join(path, LONG_NAME)
    shutil.copyfile(source, extended_path(full))
    return full


def _ddeexec() -> Optional[str]:
    """The ddeexec command of QiTV's ProgID in HKCU (Windows); None when there is none."""
    from services import file_types

    winreg = file_types._winreg()
    progid = file_types.PROGIDS["videos"]
    key_path = f"Software\\Classes\\{progid}\\shell\\open\\ddeexec"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _kind = winreg.QueryValueEx(key, "")
    except OSError:
        return None
    return str(value)


class OpenFileSmoke:
    def __init__(self, app: str, out: Path, files: Path) -> None:
        self.command = _command(app)
        self.out = out
        self.files = files
        self.result: dict[str, Any] = {"ok": False}
        self.failures: list[str] = []
        self.processes: list[subprocess.Popen] = []
        self._logs: list[IO[bytes]] = []
        self.first: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.exited_s: Optional[float] = None  # wait_named's

    # ---- the record -----------------------------------------------------------------------

    def save(self) -> None:
        self.result["failures"] = list(self.failures)
        text = json.dumps(self.result, ensure_ascii=False, indent=1)
        (self.out / RESULT).write_text(text, encoding="utf-8")

    def note(self, key: str, value: Any) -> None:
        """A figure, on disk and in the log at once (a later failure never loses it)."""
        self.result[key] = value
        self.save()
        print(f"{key}={value}", flush=True)

    def fail(self, message: str) -> None:
        self.failures.append(message)
        self.save()
        marker = "::error::" if os.environ.get("GITHUB_ACTIONS") else "FAILED: "
        print(f"{marker}{message}", flush=True)

    # ---- processes ------------------------------------------------------------------------

    def spawn(
        self, args: list[str], log_name: str, env: Optional[dict[str, str]] = None
    ) -> subprocess.Popen:
        log = open(self.out / log_name, "ab")
        self._logs.append(log)
        # Its own session on POSIX, so the whole tree (a one-file build's child) can go.
        process = subprocess.Popen(
            [*self.command, *args],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **env} if env else None,
            start_new_session=not WINDOWS,
        )
        self.processes.append(process)
        return process

    def stop_all(self) -> None:
        for process in self.processes:
            _kill_tree(process)
        self.processes = []

    def close(self) -> None:
        self.stop_all()
        for log in self._logs:
            log.close()
        self._logs = []

    def wait_named(
        self,
        name: str,
        timeout: float,
        process: Optional[subprocess.Popen] = None,
        began: Optional[float] = None,
        grace: float = AFTER_EXIT_S,
    ) -> Optional[float]:
        """Seconds from `began` until the status names `name`; None when it didn't within
        `timeout`, or within `grace` once `process` had exited. `exited_s`: when `process`
        was first seen gone, in seconds from `began` (None: not while waiting)."""
        began = time.monotonic() if began is None else began
        exited_at: Optional[float] = None
        self.exited_s = None
        while True:
            named = _same_name(_title(_status()), name)
            now = time.monotonic()
            if process is not None and exited_at is None and process.poll() is not None:
                exited_at = now
                self.exited_s = now - began
            if named:
                return now - began
            if exited_at is not None and now - exited_at >= grace:
                return None
            if now - began >= timeout:
                return None
            time.sleep(POLL_S)

    def quit(self, process: subprocess.Popen, which: str) -> bool:
        """Quit the QiTV answering the control port, `process`; False (a failure) when it
        didn't go within QUIT_TIMEOUT_S."""
        try:
            qitv_control.ask("quit", timeout=10.0)
        except SystemExit as error:
            self.fail(f"The {which} QiTV didn't take the quit ({error}).")
            return False
        try:
            # Not a pid poll: os.kill(pid, 0) would end it on Windows.
            code = process.wait(timeout=QUIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.fail(f"The {which} QiTV is still running {QUIT_TIMEOUT_S:.0f} s after quit.")
            return False
        self.note(f"{which}_quit_exit", code)
        return True

    # ---- the checks -----------------------------------------------------------------------

    def start_first(self, path: str) -> bool:
        name = Path(path).name
        began = time.monotonic()
        self.first = self.spawn([path], "open-file-first.out", {"QITV_VLC_LOG": self.vlc_log})
        took = self.wait_named(name, START_TIMEOUT_S, self.first, began, grace=0.0)
        if took is None:
            code = self.first.poll()
            if code is not None:
                self.fail(f"The first QiTV exited with {code} before playing {name!r}.")
            else:
                self.fail(f"The first QiTV didn't play {name!r} within {START_TIMEOUT_S:.0f} s.")
            return False
        self.note("first_s", round(took, 3))
        self.pid = _pid(_status())
        self.note("pid", self.pid)
        return True

    def hand_over(self, path: str, figure: str, label: str) -> bool:
        """A later start with `path`: its file plays in the first QiTV (`figure`, in seconds
        from that start), and it exits 0 (`label`_exit) with the first's pid unchanged."""
        name = Path(path).name
        began = time.monotonic()
        process = self.spawn([path], "open-file-starts.out")
        took = self.wait_named(name, HANDOVER_TIMEOUT_S, process, began)
        if took is None:
            self.fail(f"A later start's {name!r} didn't play in the first QiTV.")
        else:
            self.note(figure, round(took, 3))
        exited_s = self.exited_s
        try:
            code: Optional[int] = process.wait(timeout=EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            code = None
        if exited_s is None and code is not None:
            exited_s = time.monotonic() - began  # it went after its file was named
        self.note(f"{label}_exit", code)
        self.note(f"{label}_exit_s", None if exited_s is None else round(exited_s, 3))
        if code is None:
            self.fail(f"The start with {name!r} was still running {EXIT_TIMEOUT_S:.0f} s on.")
        elif code != 0:
            self.fail(f"The start with {name!r} exited with {code}.")
        self.check_pid(f"the start with {name!r}")
        return took is not None and code == 0

    def check_pid(self, after: str) -> None:
        pid = _pid(_status())
        if pid != self.pid:
            self.fail(f"After {after}, the QiTV answering is pid {pid}, not {self.pid}.")

    def register(self, exe: str) -> bool:
        """Windows: QiTV registered for videos in this user's HKCU, for the packaged `exe`.
        False (a failure) when it wasn't, or raised; `run` unregisters either way, so a part
        written before a failure goes too."""
        from services import file_types

        try:
            registered = bool(file_types._windows.register("videos", exe))
            why = ""
        except Exception as error:  # the run goes on without DDE
            registered, why = False, f" ({type(error).__name__}: {error})"
        self.note("dde_registered", registered)
        if not registered:
            self.fail(f"QiTV couldn't be registered for videos in HKCU{why}.")
        return registered

    def unregister(self, exe: str) -> None:
        from services import file_types

        try:
            removed = bool(file_types._windows.unregister("videos", exe))
        except Exception as error:  # the end of the run goes on
            removed = False
            print(f"Unregistering failed: {type(error).__name__}: {error}", flush=True)
        self.note("dde_unregistered", removed)

    def dde(self, path: str) -> None:
        """Windows: `path` sent to the running QiTV as Explorer sends it."""
        from services import dde_windows

        deadline = time.monotonic() + DDEEXEC_WAIT_S
        while _ddeexec() != dde_windows.COMMAND and time.monotonic() < deadline:
            time.sleep(0.1)
        written = _ddeexec() == dde_windows.COMMAND
        self.note("ddeexec_written", written)
        if not written:
            self.fail("The running QiTV didn't add the ddeexec that Explorer uses.")
        name = Path(path).name
        began = time.monotonic()
        acked = dde_windows.execute(dde_windows.COMMAND.replace("%1", path))
        self.note("dde_execute_s", round(time.monotonic() - began, 3))
        self.note("dde_ack", acked)
        if not acked:
            self.fail("QiTV didn't acknowledge the DDE command.")
            return
        took = self.wait_named(name, DDE_TIMEOUT_S, began=began)
        if took is None:
            self.fail(f"{name!r} sent over DDE didn't play within {DDE_TIMEOUT_S:.0f} s.")
            return
        self.note("dde_s", round(took, 3))
        if took >= DDE_LIMIT_S:
            self.fail(f"dde_s {took:.3f} is not under {DDE_LIMIT_S} s.")
        self.check_pid("the DDE command")

    @property
    def vlc_log(self) -> str:
        return str(self.out / VLC_LOG)

    def vlc_opened(self, name: str) -> bool:
        """Whether the first QiTV's libVLC log says it opened the file named `name` (its URI
        spells the name percent-encoded)."""
        try:
            text = Path(self.vlc_log).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        spellings = {name, quote(name), unicodedata.normalize("NFC", name)}
        return any(
            VLC_OPENED in line and any(spelling in line for spelling in spellings)
            for line in text.splitlines()
        )

    def wait_vlc_opened(self, name: str) -> bool:
        """`vlc_opened`, waited for: libVLC may hold its log's last lines a while."""
        deadline = time.monotonic() + VLC_OPENED_WAIT_S
        while not self.vlc_opened(name) and time.monotonic() < deadline:
            time.sleep(0.25)
        return self.vlc_opened(name)

    def one_qitv(self, source: Path, second: str, registered: bool) -> None:
        """The first QiTV: a start with `source`, a second start's file, then on Windows a
        file over DDE (when `registered`) and a long path; then it quits."""
        if not self.start_first(str(source)) or self.first is None:
            return
        self.hand_over(second, "handover_s", "second")
        names = [source.name, Path(second).name]
        if registered:
            dde_path = str(self.files / DDE_NAME)
            shutil.copyfile(source, dde_path)
            self.dde(dde_path)
            names.append(DDE_NAME)
        long_path, long_named = "", False
        if WINDOWS:
            long_path = _long_copy(source, self.files)
            self.note("long_path_chars", len(long_path))
            long_named = self.hand_over(long_path, "long_s", "long")
            if long_named and self.wait_vlc_opened(LONG_NAME):
                self.note("long_ok", True)
            names.append(LONG_NAME)
        quit_ok = self.quit(self.first, "first")
        if registered and quit_ok:
            removed = _ddeexec() is None
            self.note("ddeexec_removed_at_quit", removed)
            if not removed:
                self.fail("QiTV's ddeexec stayed after it quit.")
        # Read again once the first QiTV is gone: its libVLC's log is written out by then.
        self.note("played", {name: self.vlc_opened(name) for name in names})
        if long_path and "long_ok" not in self.result:
            long_ok = long_named and self.vlc_opened(LONG_NAME)
            self.note("long_ok", long_ok)
            if long_named and not long_ok:
                chars = len(long_path)
                if self.vlc_opened(source.name):
                    self.fail(f"libVLC's log doesn't say it opened the {chars}-char path.")
                else:  # a log that names no file at all: no proof either way
                    self.fail(f"libVLC's log can't tell whether it opened the {chars}-char path.")

    def parallel(self, count: int, source: Path, config: dict) -> None:
        """`count` starts at once with no QiTV running, each with a file of its own."""
        folder = self.out / "config-parallel"
        _fresh_config(folder, config)
        os.environ["QITV_CONFIG_DIR"] = str(folder)  # its control.json is there
        paths = []
        for i in range(1, count + 1):
            copy = self.files / f"parallel-{i:02d}.mp4"
            shutil.copyfile(source, copy)
            paths.append(str(copy))
        began = time.monotonic()
        starts = [
            self.spawn([path], f"open-file-parallel-{i:02d}.out") for i, path in enumerate(paths, 1)
        ]
        while time.monotonic() - began < PARALLEL_TIMEOUT_S:
            if sum(start.poll() is not None for start in starts) >= count - 1:
                break
            time.sleep(0.1)
        self.note("parallel_settled_s", round(time.monotonic() - began, 3))
        # Counted a little later: a survivor that dies, or one more that stays, shows then.
        until = max(time.monotonic() + PARALLEL_SETTLE_S, began + PARALLEL_MIN_S)
        while time.monotonic() < until:
            time.sleep(0.1)
        alive = [start for start in starts if start.poll() is None]
        exits = [start.returncode for start in starts if start.returncode is not None]
        self.note("parallel_instances", len(alive))
        self.note("parallel_exits", exits)
        self.note("parallel_handed_over", self._handed_over(count))
        if len(alive) != 1:
            self.fail(f"{count} starts at once left {len(alive)} QiTVs running, not one.")
        if any(code != 0 for code in exits):
            self.fail(f"Of {count} starts at once, some exited with {sorted(set(exits))}.")
        self._plays_one_of([Path(p).name for p in paths], count)
        if len(alive) == 1:
            self.quit(alive[0], "parallel")

    def _handed_over(self, count: int) -> int:
        """How many of the parallel starts said they handed over (their own output)."""
        handed = 0
        for i in range(1, count + 1):
            out = self.out / f"open-file-parallel-{i:02d}.out"
            if HANDED_OVER in out.read_text(encoding="utf-8", errors="replace"):
                handed += 1
        return handed

    def _plays_one_of(self, names: list[str], count: int) -> None:
        title = None
        deadline = time.monotonic() + AFTER_EXIT_S
        while time.monotonic() < deadline:
            title = _title(_status())
            if any(_same_name(title, name) for name in names):
                break
            time.sleep(0.1)
        self.note("parallel_playing", title)
        if not any(_same_name(title, name) for name in names):
            self.fail(f"After {count} starts at once, QiTV plays {title!r}, none of their files.")


def run(
    app: str,
    out: Path,
    file: str,
    second: str = "",
    parallel: int = 0,
    dde: bool = False,
) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    # libVLC and the starts append to their logs: an earlier run's lines would pass the checks.
    for stale in [out / RESULT, out / VLC_LOG, *out.glob("open-file-*.out")]:
        stale.unlink(missing_ok=True)
    source = Path(file).resolve()
    files = Path(os.path.realpath(tempfile.mkdtemp(prefix="qitv-open-")))
    smoke = OpenFileSmoke(app, out, files)
    # Nothing modal: no startup update check, no provider to fetch.
    config: dict[str, Any] = {"check_updates": False}
    if dde and not WINDOWS:
        print("--dde is for Windows: skipped", flush=True)
    dde = dde and WINDOWS
    exe = os.path.abspath(app) if dde else ""
    # Registered from the start: the running QiTV adds the ddeexec itself.
    first_config = {**config, "file_types": {"videos": exe}} if dde else config
    folder = out / "config"
    _fresh_config(folder, first_config)
    os.environ.update(QITV_CONFIG_DIR=str(folder), QITV_CONTROL="1", QITV_SMOKE_MUTE="1")
    smoke.save()
    # Once a registration was tried, whatever it answered, it is taken back at the end: one
    # that failed or raised may have written a part (removing keys never written is harmless).
    tried = registered = False
    try:
        if second:
            second_path = str(Path(second).resolve())
        else:
            second_path = str(files / SECOND_NAME)
            shutil.copyfile(source, second_path)
        if dde and not os.path.isfile(exe):
            smoke.fail(f"--dde needs the packaged QiTV's exe, not {app!r}.")
        elif dde:
            tried = True
            registered = smoke.register(exe)
        smoke.one_qitv(source, second_path, registered)
        smoke.stop_all()
        if parallel > 1:
            smoke.parallel(parallel, source, config)
        smoke.result["ok"] = not smoke.failures
        smoke.save()
        return smoke.result
    finally:
        smoke.close()  # every start's tree, before the registration goes
        if tried:
            smoke.unregister(exe)
        shutil.rmtree(extended_path(str(files)) if WINDOWS else files, ignore_errors=True)
        smoke.save()


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):  # a Windows console's code page lacks ğ, ş and ı
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--app", required=True, help="the packaged binary, or a command")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--file", required=True, help="a video the first start plays")
    parser.add_argument(
        "--second",
        default="",
        help=f"the video a second start hands over (default: a copy of --file, {SECOND_NAME!r})",
    )
    parser.add_argument(
        "--parallel", type=int, default=0, help="then this many starts at once (CI: 8)"
    )
    parser.add_argument(
        "--dde",
        action="store_true",
        help="Windows: register QiTV for videos in this user's HKCU for the run (CI's runner)",
    )
    options = parser.parse_args(argv)
    result = run(
        options.app,
        options.out.resolve(),
        options.file,
        options.second,
        options.parallel,
        options.dde,
    )
    figures = ("first_s", "handover_s", "dde_s", "long_ok", "parallel_instances", "ok")
    print(" ".join(f"{key}={result[key]}" for key in figures if key in result), flush=True)
    for failure in result.get("failures") or []:
        print(f"- {failure}", flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
