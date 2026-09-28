"""Temporary CI-only core capture; debug offline without changing MPV launch/timing.

Raw process memory stays private on the disposable runner. Only LLDB text is
published. https://lldb.llvm.org/man/lldb.html documents the --core interface.
"""

import argparse
import json
import logging
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time

logger = logging.getLogger(__name__)


def analyze_core(core: Path, evidence: Path) -> None:
    log = evidence / f"{core.name}.stack.log"
    # Loading a core never attaches to a live process or needs Developer Mode.
    with log.open("w", encoding="utf-8") as stream:
        subprocess.run(
            [
                "/usr/bin/lldb",
                "--no-lldbinit",
                "--batch",
                "--core",
                str(core),
                "-o",
                "thread backtrace all",
                "-o",
                "register read",
                "-o",
                "disassemble --frame",
                "-o",
                "image list -o -f",
            ],
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=90,
        )
    logger.info("Saved offline native stack: %s", log)


def prepare(core_directory: Path, evidence: Path) -> None:
    core_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    core_directory.chmod(0o700)
    subprocess.run(
        ["sudo", "sysctl", "-w", f"kern.corefile={core_directory}/core.%P"],
        check=True,
        timeout=10,
    )
    subprocess.run(["sudo", "sysctl", "-w", "kern.coredump=1"], check=True, timeout=10)
    _, hard = resource.getrlimit(resource.RLIMIT_CORE)
    resource.setrlimit(resource.RLIMIT_CORE, (resource.RLIM_INFINITY, hard))
    probe = core_directory / "qitv-core-probe"
    subprocess.run(
        ["clang", "-x", "c", "-g", "-o", str(probe), "-"],
        input=(
            "#include <signal.h>\n#include <stdio.h>\n"
            'int main(void) { fputs("Core probe: raising SIGSEGV\\n", stderr); '
            "fflush(stderr); raise(SIGSEGV); return 1; }\n"
        ),
        text=True,
        check=True,
        timeout=30,
    )
    core = None
    process = None
    complete = False
    started = time.monotonic()
    try:
        # Prove kernel capture and offline unwinding before expensive native builds.
        process = subprocess.Popen([str(probe)])
        core = core_directory / f"core.{process.pid}"
        try:
            # Core writing is diagnostic I/O, not a playback/shutdown deadline.
            code = process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise
        if code != -signal.SIGSEGV:
            raise RuntimeError("The owned core-capture probe did not receive SIGSEGV")
        if not core.is_file():
            raise RuntimeError("macOS did not write the owned probe's core file")
        analyze_core(core, evidence)
        complete = True
    finally:
        info = core.stat() if core is not None and core.is_file() else None
        metadata = {
            "pid": process.pid if process else None,
            "exit_code": process.poll() if process else None,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "core_bytes": info.st_size if info else None,
            "core_disk_bytes": info.st_blocks * 512 if info else None,
            "offline_analysis_completed": complete,
        }
        (evidence / "core-probe.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        if process and process.poll() is None:
            with (evidence / "core-probe-process.log").open("w") as stream:
                subprocess.run(
                    ["/bin/ps", "-p", str(process.pid), "-o", "pid=,stat=,wchan=,etime=,time="],
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=5,
                    check=False,
                )
        # On failure preserve the private probe/core for the always-run analysis.
        if complete and core is not None:
            core.unlink(missing_ok=True)
            probe.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "analyze"))
    args = parser.parse_args()
    if sys.platform != "darwin" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Native core collection is restricted to owned macOS CI runners")
    logging.basicConfig(level=logging.INFO)
    core_directory = Path(os.environ["RUNNER_TEMP"]) / "qitv-native-cores"
    evidence = Path(__file__).resolve().parents[1] / "build" / "mpv-smoke"
    evidence.mkdir(parents=True, exist_ok=True)
    if args.mode == "prepare":
        prepare(core_directory, evidence)
    else:
        cores = sorted(core_directory.glob("core.*"))
        if not cores:
            logger.info("No native core files were generated")
        for core in cores:
            try:
                analyze_core(core, evidence)
            finally:
                # Do not retain raw process memory after producing text diagnostics.
                core.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
