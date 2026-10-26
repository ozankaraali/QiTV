"""Write the keys a release build carries into services/_build_keys.py (CI, before PyInstaller).

    QITV_OPENSUBTITLES_KEY=… QITV_TMDB_TOKEN=… uv run python scripts/write_build_keys.py

CI passes the repository secrets of those names. Without one (a fork, a build by hand) its key
stays empty and the app asks the user for theirs. No key is ever printed: the output only
says whether each was written. CI never commits; never commit the file with a key in it.
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
NAME = "OPENSUBTITLES_API_KEY"
# (the secret's environment variable, the module's name for it, what the output calls it)
KEYS = (
    (ENV_NAME, NAME, "OpenSubtitles key"),
    ("QITV_TMDB_TOKEN", "TMDB_API_TOKEN", "TMDB token"),
)


def render(source: str, key: str, name: str = NAME) -> str:
    """`source` (the module as the repository has it) with `key` in the line of `name`."""
    start = f"{name} ="
    lines = source.splitlines(keepends=True)
    found = [i for i, line in enumerate(lines) if line.startswith(start)]
    if len(found) != 1:
        raise ValueError(f"{TARGET.name} must have one line starting with {start!r}")
    lines[found[0]] = f"{start} {json.dumps(key)}\n"  # a Python string, as black writes it
    return "".join(lines)


def key_from(env: Mapping[str, str], env_name: str = ENV_NAME) -> str:
    """The key in the environment ("" when it is not set, or not a key)."""
    key = env.get(env_name, "").strip()
    if key and (not key.isprintable() or any(c.isspace() for c in key)):
        return ""
    return key


def main(env: Mapping[str, str] = os.environ, target: Path = TARGET) -> int:
    source = target.read_text(encoding="utf-8")
    said = []
    for env_name, name, label in KEYS:
        key = key_from(env, env_name)
        source = render(source, key, name)
        said.append(f"{label}: written\n" if key else f"{label}: none (the app asks)\n")
    target.write_text(source, encoding="utf-8")
    sys.stdout.write("".join(said))
    return 0


if __name__ == "__main__":
    sys.exit(main())
