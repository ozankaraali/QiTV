"""A provider's cache file, read and written a piece at a time (no Qt).

Saved as one JSON document, a provider's cache was read by one orjson.loads call and written
by one orjson.dumps call. Each holds the GIL until it returns, so the GUI thread stood still
for as long as they ran on their worker: about 4 ms per MB, half a second for a large
account's cache at every start and provider setup, and 0.2 s at every save.

Here the file is JSON lines, as services.epg_store saves the guide in pieces: a header, then
each top-level value with its lists and maps cut into pieces of about PIECE_BYTES, one orjson
document per line, then an end mark. Between two lines the reading or writing thread lets go
of the GIL, and the GUI thread gets its turn. ProviderManager reads or writes one such file at
a time.

The lines:
    {"qitv": "provider-cache", "version": 2}    the header
    {"key": k, "value": v}                      content[k] is v
    {"key": k, "items": [...]}                  content[k], a list, goes on with these
    {"key": k, "part": p, "items": [...]}       content[k][p], a list, goes on with these
    {"key": k, "part": p, "entries": {...}}     content[k][p], a map, takes these
    {"end": true}                               the end mark
A section (a dict such as "itv", "vod" or "series") is written as its value with each list and
map in it empty, then their pieces. "_cache" (the identity and freshness) comes first, whole.
"""

from __future__ import annotations

import time
from typing import IO, Any, Callable, Iterator, Optional

import orjson

from services.atomic_write import atomic_write

# About this much JSON per line: an orjson call on it holds the GIL for a quarter of a
# millisecond, and the reader lets go of the GIL after each (`_yield`). The GUI thread waits
# for it again after each file it opens or reads (an import): beside a reader of 256 KiB
# lines that work took 14 times as long, 3.4 times beside this one (hel).
PIECE_BYTES = 1 << 16
_FIRST_SLICE = 64  # values in the first piece of a list or map; later pieces follow its size
MAGIC = "provider-cache"
VERSION = 2
_HEADER = orjson.dumps({"qitv": MAGIC, "version": VERSION}) + b"\n"
_END = orjson.dumps({"end": True}) + b"\n"
_METADATA = "_cache"


def write_cache(
    path: str, content: dict, replace: Optional[Callable[[str, str], object]] = None
) -> None:
    """Save `content` at `path` in pieces, replacing the file only once it is complete
    (`replace` as atomic_write takes it). TypeError for a value JSON cannot hold, as
    orjson.dumps raises it; OSError when it cannot be written."""
    with atomic_write(path, replace) as f:
        f.write(_HEADER)
        keys = sorted(content, key=lambda key: key != _METADATA)  # "_cache" first
        for key in keys:
            _write_value(f, key, content[key])
        f.write(_END)


def read_cache(path: str) -> dict:
    """The content write_cache saved at `path`.

    ValueError for a file it did not write or a damaged one (orjson's JSONDecodeError is
    one); OSError (FileNotFoundError among them) when it cannot be read.
    """
    with open(path, "rb") as f:
        data = f.read()  # (the GIL is let go while it reads)
    lines = _lines(data)
    if _document(next(lines, b"")) != {"qitv": MAGIC, "version": VERSION}:
        raise ValueError("not a provider cache")
    content: dict[str, Any] = {}
    for line in lines:
        doc = _document(line)
        if doc.get("end") is True:
            return content
        _apply(content, doc)
        _yield()
    raise ValueError("the provider cache is cut short")


def _lines(data: bytes) -> Iterator[memoryview]:
    """`data`'s lines, each ended by a newline, without copying them."""
    view = memoryview(data)
    start = 0
    while (end := data.find(b"\n", start)) >= 0:
        yield view[start:end]
        start = end + 1


def _yield() -> None:
    """Let go of the GIL for a moment: a thread waiting for it takes it now, not once the
    switch interval is up and this thread's next piece is parsed."""
    time.sleep(0)


def _document(line: bytes | memoryview) -> dict:
    doc = orjson.loads(line)
    if not isinstance(doc, dict):
        raise ValueError("not a provider cache line")
    return doc


def _apply(content: dict, doc: dict) -> None:
    """One line's value or piece into `content`."""
    key = doc.get("key")
    if not isinstance(key, str):
        raise ValueError("a provider cache line without its key")
    if "value" in doc:
        content[key] = doc["value"]
        return
    target = content.get(key)
    part = doc.get("part")
    if part is not None:
        target = target.get(part) if isinstance(target, dict) else None
    items, entries = doc.get("items"), doc.get("entries")
    if isinstance(items, list) and isinstance(target, list):
        target.extend(items)
    elif isinstance(entries, dict) and isinstance(target, dict):
        target.update(entries)
    else:
        raise ValueError("a provider cache piece without its place")


def _write_value(f: IO[bytes], key: str, value: Any) -> None:
    name = orjson.dumps(key)
    if isinstance(value, list):
        f.write(b'{"key":' + name + b',"value":[]}\n')
        _write_pieces(f, b'{"key":' + name + b',"items":', value, orjson.dumps)
        return
    if key == _METADATA or not isinstance(value, dict):
        f.write(b'{"key":' + name + b',"value":' + orjson.dumps(value) + b"}\n")
        return
    head = {
        part: [] if isinstance(member, list) else {} if isinstance(member, dict) else member
        for part, member in value.items()
    }
    f.write(b'{"key":' + name + b',"value":' + orjson.dumps(head) + b"}\n")
    for part, member in value.items():  # (orjson took only string keys in `head`)
        prefix = b'{"key":' + name + b',"part":' + orjson.dumps(part)
        if isinstance(member, list):
            _write_pieces(f, prefix + b',"items":', member, orjson.dumps)
        elif isinstance(member, dict):
            _write_pieces(f, prefix + b',"entries":', list(member.items()), _dumps_entries)


def _dumps_entries(entries: list) -> bytes:
    return orjson.dumps(dict(entries))


def _write_pieces(f: IO[bytes], prefix: bytes, values: list, dumps: Callable) -> None:
    """`values` as lines `prefix` + a piece of about PIECE_BYTES + "}", each piece one orjson
    call: the first of _FIRST_SLICE values, the next as many as the last one's size says."""
    for blob in _pieces(values, dumps):
        f.write(prefix)
        f.write(blob)
        f.write(b"}\n")
        _yield()


def _pieces(values: list, dumps: Callable) -> Iterator[bytes]:
    count, start = _FIRST_SLICE, 0
    while start < len(values):
        piece = values[start : start + count]
        blob = dumps(piece)
        yield blob
        start += len(piece)
        count = max(1, int(PIECE_BYTES * len(piece) / max(1, len(blob))))
