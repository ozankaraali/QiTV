"""How long a second start of QiTV takes to hand over to the one already running.

    uv run python scripts/smoke_open_file.py --app dist/qitv-linux --out smoke-frozen
    uv run python scripts/smoke_open_file.py --app "uv run python main.py" --out /tmp/open

Starts --app with its own config dir (<out>/config), waits for the control port, starts
--app again and times that second process until it exits 0 (it hands over with `show` and
leaves), then quits the first. Writes <out>/open-file.json: {"first_s", "handover_s"}.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qitv_control  # noqa: E402

START_TIMEOUT_S = 60.0
HANDOVER_TIMEOUT_S = 60.0


def _command(app: str) -> list[str]:
    if os.name == "nt":
        return [app]
    return shlex.split(app)


def _status_pid() -> int | None:
    """The running QiTV's pid when its control port answers, else None."""
    try:
        answer = qitv_control.ask("status", timeout=5.0)
    except SystemExit:
        return None
    pid = answer.get("pid")
    return int(pid) if pid else None


def _spawn(command: list[str], log) -> subprocess.Popen:
    # Its own session on POSIX, so the whole tree (a one-file build's unpacked child) can go.
    return subprocess.Popen(
        command,
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )


def _kill_tree(process: subprocess.Popen) -> None:
    """Stop `process` and everything it started (it may have unpacked a child)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    if process.poll() is None:
        process.kill()


def run(app: str, out: Path, file: str = "", second: str = "") -> dict:
    out.mkdir(parents=True, exist_ok=True)
    config = out / "config"
    config.mkdir(parents=True, exist_ok=True)
    # Nothing modal: no startup update check, no provider to fetch.
    (config / "config.json").write_text(json.dumps({"check_updates": False}), encoding="utf-8")
    os.environ.update(QITV_CONFIG_DIR=str(config), QITV_CONTROL="1", QITV_SMOKE_MUTE="1")
    command = _command(app)
    log = open(out / "open-file-first.out", "ab")
    started = time.monotonic()
    first = _spawn(command, log)
    others: list[subprocess.Popen] = []
    result: dict = {"ok": False}
    try:
        pid = None
        while time.monotonic() - started < START_TIMEOUT_S:
            if first.poll() is not None:
                raise SystemExit(f"The first QiTV exited with {first.returncode}.")
            pid = _status_pid()
            if pid:
                break
            time.sleep(0.25)
        if not pid:
            raise SystemExit(f"The first QiTV did not answer within {START_TIMEOUT_S:.0f} s.")
        result["first_s"] = round(time.monotonic() - started, 3)

        began = time.monotonic()
        again = _spawn(command + ([file] if file else []), log)
        others.append(again)
        try:
            code = again.wait(timeout=HANDOVER_TIMEOUT_S)
            result["second_exit"] = code
        except subprocess.TimeoutExpired:
            code = None
            result["second_exit"] = None
            result["handover_over_s"] = HANDOVER_TIMEOUT_S
        result["handover_s"] = round(time.monotonic() - began, 3)
        # The figure is on disk and printed before anything else can fail.
        (out / "open-file.json").write_text(json.dumps(result), encoding="utf-8")
        print(f"first_s={result['first_s']} handover_s={result['handover_s']}", flush=True)
        if code is None:
            raise SystemExit(f"The second start did not exit within {HANDOVER_TIMEOUT_S:.0f} s.")
        if code != 0:
            raise SystemExit(f"The second start exited with {code}.")
        if _status_pid() != pid:
            raise SystemExit("The first QiTV's pid changed or it stopped answering.")
        qitv_control.ask("quit")
        try:
            first.wait(timeout=30.0)  # not a pid poll: os.kill(pid, 0) would kill it on Windows
        except subprocess.TimeoutExpired:
            raise SystemExit("The first QiTV is still running 30 s after quit.")
        result["ok"] = True
        (out / "open-file.json").write_text(json.dumps(result), encoding="utf-8")
        return result
    finally:
        for process in [first, *others]:
            _kill_tree(process)
        log.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--app", required=True, help="the packaged binary, or a command")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--file", default="", help="(from Task 11 on)")
    parser.add_argument("--second", default="", help="(from Task 11 on)")
    options = parser.parse_args(argv)
    run(options.app, options.out.resolve(), options.file, options.second)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
