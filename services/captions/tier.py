"""Which AI captions model suits this computer: the large one (Qwen3-ASR 1.7B) on a fast
machine with the memory for it, else the small one (0.6B). No Qt.

`cpu_score()` runs a short pure-Python benchmark (about 0.2 s): call it off the GUI thread.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import logging
import os
import platform
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)

AUTO = "auto"
SMALL = "small"
LARGE = "large"

# The large tier wants a CPU score of 0.9 (the reference below, less the benchmark's own
# wander) or an arm64 Mac, and at least 12 GB of memory on any machine: on the reference the
# 1.7B model keeps up 1.6-1.7x on its 4 threads, and the picture held 10 s behind live gives
# its pieces the time. A "12 GB" computer reports a little less than 12 GiB (firmware and
# kernel keep some), hence 11.5 GiB.
LARGE_MIN_SCORE = 0.9
LARGE_MIN_RAM = int(11.5 * 1024**3)

# The reference, score 1.0: a 2017 MacBook Pro, an Intel i7-7820HQ (4 cores, 8 threads), on
# which the 0.6B model keeps up 2-4x beside 1080p decoding, and the 1.7B 1.6-1.7x. Calibrated on
# 2026-11-03 under QiTV's Python 3.14.8 (uv's CPython, macOS 13): the best of _ROUNDS rounds
# of _work(_ITERATIONS) took 66.0-66.5 ms in five runs (each run about 0.2 s in all).
REFERENCE_THREADS = 8
REFERENCE_ROUND_S = 0.066
_ITERATIONS = 350_000
_ROUNDS = 3


def _work(iterations: int) -> int:
    """A fixed mix of what Python spends its time on: integer arithmetic, a branch, dict
    stores and a list."""
    total = 0
    table: dict[int, int] = {}
    seen: list[int] = []
    for i in range(iterations):
        total = (total * 31 + i) & 0xFFFFFFFF
        if i & 7 == 0:
            table[i & 1023] = total
        if i & 255 == 0:
            seen.append(total)
    return total + len(table) + len(seen)


def single_thread_round_s() -> float:
    """The best of a few timed rounds of the benchmark, in seconds."""
    best = float("inf")
    for _ in range(_ROUNDS):
        start = time.perf_counter()
        _work(_ITERATIONS)
        best = min(best, time.perf_counter() - start)
    return best


def model_threads(cpus: Optional[int] = None) -> int:
    """The threads the speech model runs on: half the logical cores, 2 to 6."""
    count = (os.cpu_count() or 4) if cpus is None else cpus
    return max(2, min(6, count // 2))


@functools.cache
def cpu_score() -> float:
    """The model's threads × single-thread speed, relative to the reference machine's (1.0):
    what the model gets, not the whole machine (24 slow threads give it 6). Measured once per
    run (about 0.2 s)."""
    threads = os.cpu_count() or 1
    round_s = max(single_thread_round_s(), 1e-6)
    used = model_threads(threads) / model_threads(REFERENCE_THREADS)
    score = used * (REFERENCE_ROUND_S / round_s)
    logger.info("CPU score %.2f (%d threads, %.1f ms a round)", score, threads, round_s * 1000)
    return score


def _sysctl_int(name: str) -> Optional[int]:
    """A macOS sysctl number, or None when the name is unknown here."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
        value = ctypes.c_uint64(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        if libc.sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0):
            return None
        return int(value.value) if size.value == 8 else int(value.value & 0xFFFFFFFF)
    except OSError, AttributeError:
        return None


def is_arm_mac() -> bool:
    """An Apple silicon Mac, even when this Python runs translated (Rosetta)."""
    if sys.platform != "darwin":
        return False
    return platform.machine() == "arm64" or _sysctl_int("hw.optional.arm64") == 1


def _windows_ram() -> int:
    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    windll = getattr(ctypes, "windll")
    if not windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0
    return int(status.ullTotalPhys)


def _linux_ram(meminfo: str = "/proc/meminfo") -> int:
    with open(meminfo, encoding="ascii", errors="replace") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024  # given in kB
    return 0


def total_ram_bytes() -> int:
    """The computer's memory in bytes; 0 when it can't be read (then the small tier)."""
    try:
        if sys.platform == "darwin":
            return _sysctl_int("hw.memsize") or 0
        if sys.platform == "win32":
            return _windows_ram()
        return _linux_ram()
    except (OSError, ValueError, AttributeError) as e:
        logger.warning("Could not read the memory size: %s", e)
        return 0


def choose_tier(
    override: str = AUTO,
    *,
    score: Optional[float] = None,
    ram: Optional[int] = None,
    arm_mac: Optional[bool] = None,
) -> str:
    """The tier for this machine, SMALL or LARGE. `override` (SMALL or LARGE, the viewer's
    choice in Settings) wins; otherwise LARGE when the machine is an arm64 Mac or scores at
    least LARGE_MIN_SCORE, and has at least LARGE_MIN_RAM. `score`, `ram` and `arm_mac`
    default to this machine's: the benchmark (about 0.2 s, off the GUI thread) runs only when
    the memory allows the large tier and the machine isn't an arm64 Mac."""
    if override in (SMALL, LARGE):
        return override
    if (total_ram_bytes() if ram is None else ram) < LARGE_MIN_RAM:
        return SMALL
    if is_arm_mac() if arm_mac is None else arm_mac:
        return LARGE
    return LARGE if (cpu_score() if score is None else score) >= LARGE_MIN_SCORE else SMALL
