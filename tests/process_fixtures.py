"""Watching processes that a test's own children started (not a test module)."""

import os
import signal
import subprocess
import time


def exited(pid):
    """True once process `pid` has exited; a zombie waiting for its reaper counts."""
    if os.name == "nt":
        import _winapi

        try:
            handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
        except OSError:
            return True  # no such process anymore
        try:
            return _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_OBJECT_0
        finally:
            _winapi.CloseHandle(handle)
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10
    ).stdout.strip()
    return not state or state.startswith("Z")


def still_running(pids, timeout):
    """The processes among `pids` that have not exited within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    running = [pid for pid in pids if pid is not None]
    while running:
        running = [pid for pid in running if not exited(pid)]
        if not running or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return running


def terminate(pids):
    """Stop the processes a failed test left running."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass  # gone meanwhile
