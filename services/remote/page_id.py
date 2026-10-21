"""Which phone page a QiTV carries: a fingerprint of its files. QiTV's hello names it, and the
relay, which keeps every page it has served at /p/<id>/ (listed in /pages.json), hands a phone
that QiTV's own page; so a relay updated for a newer QiTV still serves an older one the page it
speaks with.

Standard library only: the relay's sync script runs this file with any Python 3 to name the page
it copies (python3 page_id.py <folder> prints the id).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys

ID_CHARS = 16
_RELAYS_OWN = ("pages.json", "p")  # what the relay adds beside a page: never part of it


def page_id(folder: str | Path) -> str:
    root = Path(folder)
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        parts = path.relative_to(root).parts
        if parts[0] in _RELAYS_OWN or any(part.startswith(".") for part in parts):
            continue
        # CRLF reads as LF: a Windows checkout (git's autocrlf) is the same page as the relay's.
        data = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(f"{'/'.join(parts)}\0{len(data)}\0".encode("utf-8"))
        digest.update(data)
    return digest.hexdigest()[:ID_CHARS]


if __name__ == "__main__":
    print(page_id(sys.argv[1]))
