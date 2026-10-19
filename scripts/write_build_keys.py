"""Write the keys a release build carries into services/_build_keys.py (CI, before PyInstaller).

    QITV_OPENSUBTITLES_KEY=… uv run python scripts/write_build_keys.py

CI passes the repository secret of that name. Without it (a fork, a build by hand) the key
stays empty and the app asks the user for theirs. The key is never printed: the output only
says whether one was written. CI never commits; never commit the file with a key in it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "services" / "_build_keys.py"
ENV_NAME = "QITV_OPENSUBTITLES_KEY"
LINE_START = "OPENSUBTITLES_API_KEY ="


def render(source: str, key: str) -> str:
    """`source` (the module as the repository has it) with `key` in its key's line."""
    lines = source.splitlines(keepends=True)
    found = [i for i, line in enumerate(lines) if line.startswith(LINE_START)]
    if len(found) != 1:
        raise ValueError(f"{TARGET.name} must have one line starting with {LINE_START!r}")
    lines[found[0]] = f"{LINE_START} {json.dumps(key)}\n"  # a Python string, as black writes it
    return "".join(lines)


def key_from(env: Mapping[str, str]) -> str:
    """The key in the environment ("" when it is not set, or not a key)."""
    key = env.get(ENV_NAME, "").strip()
    if key and (not key.isprintable() or any(c.isspace() for c in key)):
        return ""
    return key


def main(env: Mapping[str, str] = os.environ, target: Path = TARGET) -> int:
    key = key_from(env)
    target.write_text(render(target.read_text(encoding="utf-8"), key), encoding="utf-8")
    sys.stdout.write(
        "OpenSubtitles key: written\n" if key else "OpenSubtitles key: none (the app asks)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
