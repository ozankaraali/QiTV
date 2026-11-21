"""Which AI captions model suits this computer: the large one (Qwen3-ASR 1.7B) on a fast
machine with the memory for it, else the small one (0.6B). No Qt.

`cpu_score()` runs a short pure-Python benchmark (about 0.2 s), and `machine_score()` keeps the
best one measured on this computer (speed.json in the captions folder): call them off the GUI
thread.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import json
import logging
import os
from pathlib import Path
import platform
import sys
import threading
import time
from typing import Optional

from services.atomic_write import atomic_write

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
# 2.10: while AI captions are translated, the translator (MiLMMT-46 1B, on `translator_threads`)
# shares the CPU with the speech model: below this score Automatic takes the small one (an arm64
# Mac keeps its rule)
TRANSLATE_SMALL_BELOW = 1.5

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


TRANSLATOR_CORES = 6  # 2.13.1: physical cores from which the translator runs on 4 threads


def translator_threads(*, physical: Optional[int] = None, arm_mac: Optional[bool] = None) -> int:
    """2.13.1: the threads the caption translator (MiLMMT, in the captions process beside the
    speech model) runs on: 4 with at least TRANSLATOR_CORES physical cores and on any arm64
    Mac, else 2. Going from 2 to 4 threads took 33-39 % off a line on a 4-core ARM box and
    24-29 % on a 7950X; more add little, decoding is memory-bound (2026-11-21). The owner: the
    reference i7's 4 cores (8 threads) stay at 2, so that speech and translation don't fight
    over them. `physical` and `arm_mac` default to this machine's."""
    if is_arm_mac() if arm_mac is None else arm_mac:
        return 4
    cores = physical_cores() if physical is None else physical
    return 4 if cores >= TRANSLATOR_CORES else 2


@functools.cache
def physical_cores() -> int:
    """This machine's physical cores (once per run; `_physical_cores`)."""
    return _physical_cores()


def _physical_cores() -> int:
    """The physical cores (a core's hyper-threads counted once), with no dependency: macOS's
    sysctl, Linux's CPU topology (else /proc/cpuinfo), Windows' processor-core records; half
    the logical cores when the system doesn't say. Never more than the logical ones."""
    logical = os.cpu_count() or 2
    found: Optional[int] = None
    try:
        if sys.platform == "darwin":
            found = _sysctl_int("hw.physicalcpu")
        elif sys.platform.startswith("linux"):
            found = _linux_physical()
        elif sys.platform.startswith("win"):
            found = _windows_physical()
    except (OSError, ValueError, AttributeError) as error:
        logger.debug("physical cores not read: %s", error)
    if not found or found < 1:
        return max(1, logical // 2)
    return min(int(found), logical)


def _linux_physical(
    sysfs: Path = Path("/sys/devices/system/cpu"), cpuinfo: Path = Path("/proc/cpuinfo")
) -> Optional[int]:
    """The cores of a Linux machine: each online CPU's siblings (the CPUs of its core) counted
    once; without a topology, /proc/cpuinfo's (physical id, core id) pairs; None when neither
    says (an ARM cpuinfo names no cores)."""
    cores: set[str] = set()
    for topology in sysfs.glob("cpu[0-9]*/topology"):
        for name in ("core_cpus_list", "thread_siblings_list"):
            listed = topology / name
            if listed.is_file():
                cores.add(listed.read_text(encoding="ascii", errors="replace").strip())
                break
    if cores:
        return len(cores)
    if not cpuinfo.is_file():
        return None
    pairs: set[tuple[Optional[str], str]] = set()
    package: Optional[str] = None
    core: Optional[str] = None
    for line in cpuinfo.read_text(encoding="ascii", errors="replace").splitlines() + [""]:
        key, _, value = line.partition(":")
        key = key.strip()
        if key == "physical id":
            package = value.strip()
        elif key == "core id":
            core = value.strip()
        elif not line.strip():  # a processor's block ends
            if core is not None:
                pairs.add((package, core))
            package = core = None
    return len(pairs) or None


def _windows_physical() -> Optional[int]:
    """The processor-core records GetLogicalProcessorInformationEx gives (RelationProcessorCore,
    0): one a core."""
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "windll").kernel32
    length = wintypes.DWORD(0)
    kernel32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(length))
    if not length.value:
        return None
    buffer = ctypes.create_string_buffer(length.value)
    if not kernel32.GetLogicalProcessorInformationEx(0, buffer, ctypes.byref(length)):
        return None
    return _core_records(buffer.raw[: length.value]) or None


def _core_records(raw: bytes) -> int:
    """How many records `raw` holds: each starts with its relation and its size (two DWORDs)."""
    count = offset = 0
    while offset + 8 <= len(raw):
        size = int.from_bytes(raw[offset + 4 : offset + 8], "little")
        if size <= 0:
            break
        count += 1
        offset += size
    return count


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


SPEED_FILE = "speed.json"  # in the captions folder: the best score measured on this computer


def _this_computer() -> str:
    """What a remembered score belongs to: a profile copied to another computer measures anew."""
    return f"{platform.machine()}/{os.cpu_count() or 0}/{platform.processor()}"


_score_lock = threading.Lock()
_score: Optional[float] = None  # machine_score()'s, once measured


def machine_score() -> float:
    """The best cpu_score() measured on this computer (this run's, or one remembered from an
    earlier run): busy moments only ever lower a measurement (2.9: one taken as a film opened
    scored 0.81 on the reference, which had scored 1.0, and the small model was chosen). Call it
    off the GUI thread: it measures, and reads and writes a small file. Measured once: a second
    caller waits for the first's (2.9, the review: two at once each ran at half speed)."""
    global _score
    with _score_lock:
        if _score is None:
            _score = _measure_machine()
        return _score


def known_score() -> Optional[float]:
    """machine_score() once it has been measured, else None (never measures: the GUI's)."""
    return _score


def _forget_score() -> None:
    """Tests: measured again next time."""
    global _score
    with _score_lock:
        _score = None


setattr(machine_score, "cache_clear", _forget_score)  # as functools.cache's (the tests)


def _measure_machine() -> float:
    measured = cpu_score()
    from services.captions.models import captions_root

    path = captions_root() / SPEED_FILE
    try:
        kept = json.loads(path.read_text(encoding="utf-8"))
        if kept.get("computer") == _this_computer():
            best = max(measured, float(kept.get("score", 0.0)))
        else:
            best = measured
    except OSError, ValueError, TypeError, AttributeError:
        best = measured
    if best == measured:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with atomic_write(str(path)) as out:
                out.write(json.dumps({"computer": _this_computer(), "score": measured}).encode())
        except OSError as e:
            logger.debug("The CPU score was not kept: %s", e)
    if best != measured:
        logger.info("CPU score %.2f: the best measured on this computer", best)
    return best


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


def slow_to_translate(score: float, *, arm_mac: Optional[bool] = None) -> bool:
    """While AI captions are translated, the large tier and the translator can't both keep up
    on this machine: it scores below TRANSLATE_SMALL_BELOW and isn't an arm64 Mac."""
    if is_arm_mac() if arm_mac is None else arm_mac:
        return False
    return score < TRANSLATE_SMALL_BELOW


def choose_tier(
    override: str = AUTO,
    *,
    score: Optional[float] = None,
    ram: Optional[int] = None,
    arm_mac: Optional[bool] = None,
    translating: bool = False,
) -> str:
    """The tier for this machine, SMALL or LARGE. `override` (SMALL or LARGE, the viewer's
    choice in Settings) wins; otherwise LARGE when the machine is an arm64 Mac or scores at
    least LARGE_MIN_SCORE (TRANSLATE_SMALL_BELOW while AI captions are `translating`), and has
    at least LARGE_MIN_RAM. `score`, `ram` and `arm_mac` default to this machine's: the
    benchmark (about 0.2 s, off the GUI thread) runs only when the memory allows the large tier
    and the machine isn't an arm64 Mac."""
    if override in (SMALL, LARGE):
        return override
    if (total_ram_bytes() if ram is None else ram) < LARGE_MIN_RAM:
        return SMALL
    if is_arm_mac() if arm_mac is None else arm_mac:
        return LARGE
    score = machine_score() if score is None else score
    if translating and slow_to_translate(score, arm_mac=False):
        return SMALL
    return LARGE if score >= LARGE_MIN_SCORE else SMALL
