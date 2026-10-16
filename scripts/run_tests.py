"""Runs the unit tests one file per process, several files at once.

`uv run python scripts/run_tests.py` (CI's tests job does the same). Each file runs as
`unittest discover -s tests -p <file>` would run it alone, so a file's Qt application, temporary
folders and patched environment stay its own. The largest files start first. A failing file's
whole output is printed, and it runs once more alone at the end; the exit code is 1 if it
fails again.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


def run_file(path: Path) -> tuple[Path, int, str, float]:
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", path.name],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return path, proc.returncode, proc.stdout + proc.stderr, time.monotonic() - start


def main() -> int:
    files = sorted(TESTS.glob("test_*.py"), key=lambda p: p.stat().st_size, reverse=True)
    workers = int(os.environ.get("QITV_TEST_JOBS", "0")) or os.cpu_count() or 2
    start, failed, total = time.monotonic(), [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path, code, output, seconds in pool.map(run_file, files):
            ran = re.search(r"^Ran (\d+) tests?", output, re.MULTILINE)
            count = int(ran.group(1)) if ran else 0
            total += count
            print(
                f"{'ok  ' if code == 0 else 'FAIL'} {seconds:5.1f}s {count:4d} {path.name}",
                flush=True,
            )
            if code != 0:
                failed.append(path)
                print(output, flush=True)
    print(
        f"\n{total} tests in {len(files)} files, {time.monotonic() - start:.0f}s, {workers} at once"
    )
    # A file that failed among the others runs once more alone: a timing test can miss its
    # window on a loaded runner, a real failure fails again.
    still, flaky = [], []
    for path in failed:
        _path, code, output, seconds = run_file(path)
        (flaky if code == 0 else still).append(path.name)
        print(f"{'ok  ' if code == 0 else 'FAIL'} {seconds:5.1f}s alone {path.name}", flush=True)
        if code != 0:
            print(output, flush=True)
    if flaky:
        print("Passed alone (flaky under load): " + ", ".join(flaky))
    if still:
        print("Failed: " + ", ".join(still))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
