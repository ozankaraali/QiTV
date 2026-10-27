"""Drive the running QiTV through its control port (services/control.py, 2.8.3).

    uv run python scripts/qitv_control.py status
    uv run python scripts/qitv_control.py log [-n 200] [--grep TEXT]
    uv run python scripts/qitv_control.py actions
    uv run python scripts/qitv_control.py action go_live [PAYLOAD]
    uv run python scripts/qitv_control.py tune "Channel name" [--provider NAME]
    uv run python scripts/qitv_control.py python "window.controller.now_playing()"
    uv run python scripts/qitv_control.py python -f snippet.py
    uv run python scripts/qitv_control.py quit | start | restart

The port and its token are read from control.json beside QiTV's log (QITV_CONFIG_DIR's when
set). `start` and `restart` run QiTV from this checkout, its output in that folder's
qitv-dev.out. Answers print as JSON.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from services.control import FILE_NAME  # noqa: E402
from services.log_file import log_dir  # noqa: E402


def _port_file() -> Path:
    return log_dir() / FILE_NAME


def _connection() -> dict:
    try:
        return json.loads(_port_file().read_text(encoding="utf-8"))
    except OSError, ValueError:
        raise SystemExit(f"QiTV's control port is not open ({_port_file()} is missing).")


def ask(command: str, args: dict | None = None, timeout: float = 40.0) -> dict:
    connection = _connection()
    url = f"http://127.0.0.1:{connection['port']}/{command}"
    data = json.dumps({"args": args or {}}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Authorization", f"Bearer {connection['token']}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as error:
        return json.loads(error.read() or b"{}") | {"status": error.code}
    except (urllib.error.URLError, ConnectionError) as error:
        raise SystemExit(f"QiTV did not answer on its control port ({error}).")


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def quit_qitv(wait_s: float = 30.0) -> None:
    pid = int(_connection().get("pid") or 0)
    ask("quit")
    deadline = time.monotonic() + wait_s
    while pid and _running(pid) and time.monotonic() < deadline:
        time.sleep(0.25)
    if pid and _running(pid):
        raise SystemExit(f"QiTV (pid {pid}) is still running after {wait_s:.0f} s.")


def start_qitv(wait_s: float = 60.0) -> dict:
    before = _port_file().stat().st_mtime if _port_file().exists() else 0.0
    log_dir().mkdir(parents=True, exist_ok=True)  # a fresh profile has none yet
    out = open(log_dir() / "qitv-dev.out", "ab")
    subprocess.Popen(
        ["uv", "run", "python", "main.py"],
        cwd=REPO,
        stdout=out,
        stderr=out,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # it outlives this command
    )
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _port_file().exists() and _port_file().stat().st_mtime > before:
            return ask("status")
        time.sleep(0.5)
    raise SystemExit(f"QiTV did not open its control port within {wait_s:.0f} s.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    log = sub.add_parser("log")
    log.add_argument("-n", "--lines", type=int, default=200)
    log.add_argument("--grep", default="")
    sub.add_parser("actions")
    action = sub.add_parser("action")
    action.add_argument("name")
    action.add_argument("payload", nargs="?", default="")
    tune = sub.add_parser("tune")
    tune.add_argument("name")
    tune.add_argument("--provider", default="")
    python = sub.add_parser("python")
    python.add_argument("code", nargs="?", default="")
    python.add_argument("-f", "--file", default="")
    for name in ("quit", "start", "restart"):
        sub.add_parser(name)
    options = parser.parse_args(argv)

    if options.command == "log":
        answer = ask("log", {"lines": options.lines, "grep": options.grep})
        print("\n".join(answer.get("lines") or []))
        return 0
    if options.command in ("quit", "restart"):
        quit_qitv()
        if options.command == "quit":
            print(json.dumps({"quit": True}))
            return 0
    if options.command in ("start", "restart"):
        print(json.dumps(start_qitv(), indent=2, ensure_ascii=False))
        return 0
    args: dict = {}
    if options.command == "action":
        args = {"name": options.name, "payload": options.payload}
    elif options.command == "tune":
        args = {"name": options.name, "provider": options.provider}
    elif options.command == "python":
        code = Path(options.file).read_text(encoding="utf-8") if options.file else options.code
        args = {"code": code}
    answer = ask(options.command, args)
    print(json.dumps(answer, indent=2, ensure_ascii=False))
    return 1 if "error" in answer else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
