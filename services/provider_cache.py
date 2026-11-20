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

What opens one title or group at a time is kept beside it, in the pages file (write_pages): a
series' seasons, a season's episodes, a portal's group pages (a section whose "contents" is a
map), so that opening one writes that small file and not the lists again. Its lines are the
same, each with the section's "token": when the section was loaded (its "refreshed_at" in
"_cache"). A section loaded again leaves the pages of the one it replaced out.
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
PAGES_MAGIC = "provider-pages"
_PAGES_HEADER = orjson.dumps({"qitv": PAGES_MAGIC, "version": VERSION}) + b"\n"
_PAGES = ("seasons", "episodes", "contents")  # a section's maps opened a title or group at a time
_PAGE_VALUES = ("resolved_base",)  # kept with its pages: where its episodes play from
_END = orjson.dumps({"end": True}) + b"\n"
_METADATA = "_cache"


def write_cache(
    path: str, content: dict, replace: Optional[Callable[[str, str], object]] = None
) -> None:
    """Save `content` at `path` in pieces, but its pages (write_pages): a portal's group pages
    as an empty map. The file is replaced only once it is complete (`replace` as atomic_write
    takes it). TypeError for a value JSON cannot hold, as orjson.dumps raises it; OSError when
    it cannot be written."""
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


def has_pages(content: dict) -> bool:
    """Whether `content` holds pages (a series' seasons, a portal's group pages, …)."""
    return any(_section_pages(value) for key, value in content.items() if key != _METADATA)


def write_pages(
    path: str, content: dict, replace: Optional[Callable[[str, str], object]] = None
) -> None:
    """Save the pages of `content`'s sections at `path`, each with its section's token, as
    write_cache saves (TypeError, OSError)."""
    with atomic_write(path, replace) as f:
        f.write(_PAGES_HEADER)
        for key, value in content.items():
            pages = _section_pages(value) if key != _METADATA else []
            if not pages:
                continue
            prefix = b'{"key":' + orjson.dumps(key) + b',"token":'
            prefix += orjson.dumps(_token(content, key)) + b',"part":'
            for part in _PAGE_VALUES:
                if part in value:
                    f.write(prefix + orjson.dumps(part) + b',"value":')
                    f.write(orjson.dumps(value[part]) + b"}\n")
            for part, member in pages:
                entries = prefix + orjson.dumps(part) + b',"entries":'
                _write_pieces(f, entries, list(member.items()), _dumps_entries)
        f.write(_END)


def read_pages(path: str, content: dict) -> None:
    """Put the pages write_pages saved at `path` into `content`'s sections, each where its
    section is the one they were saved with (its token): those of a section loaded since are
    left out. Nothing goes in from a damaged file: ValueError then, and OSError when it
    cannot be read (FileNotFoundError: there are none)."""
    with open(path, "rb") as f:
        data = f.read()
    lines = _lines(data)
    if _document(next(lines, b"")) != {"qitv": PAGES_MAGIC, "version": VERSION}:
        raise ValueError("not a provider's pages")
    docs = []
    for line in lines:
        doc = _document(line)
        if doc.get("end") is True:
            break
        if not (
            isinstance(doc.get("key"), str)
            and isinstance(doc.get("part"), str)
            and ("value" in doc or isinstance(doc.get("entries"), dict))
        ):
            raise ValueError("a provider's page without its place")
        docs.append(doc)
        _yield()
    else:
        raise ValueError("the provider's pages are cut short")
    for doc in docs:
        key, part = doc["key"], doc["part"]
        section = content.get(key)
        if not isinstance(section, dict) or doc.get("token") != _token(content, key):
            continue
        if "value" in doc:
            section[part] = doc["value"]
            continue
        target = section.setdefault(part, {})
        if isinstance(target, dict):
            target.update(doc["entries"])


def _section_pages(value: Any) -> list[tuple[str, dict]]:
    """A section's pages that hold something: (its key, the map)."""
    if not isinstance(value, dict):
        return []
    return [(part, value[part]) for part in _PAGES if _is_page(part, value.get(part))]


def _is_page(part: str, member: Any) -> bool:
    return part in _PAGES and isinstance(member, dict) and bool(member)


def _token(content: dict, key: str) -> Any:
    """When `content`'s section `key` was loaded (its "refreshed_at"), else None."""
    metadata = content.get(_METADATA)
    refreshed = metadata.get("refreshed_at") if isinstance(metadata, dict) else None
    return refreshed.get(key) if isinstance(refreshed, dict) else None


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
    pages = {part for part, member in value.items() if _is_page(part, member)}
    head = {
        part: [] if isinstance(member, list) else {} if isinstance(member, dict) else member
        for part, member in value.items()
        if part not in pages or part == "contents"  # (a portal's: its pages come in later)
    }
    f.write(b'{"key":' + name + b',"value":' + orjson.dumps(head) + b"}\n")
    for part, member in value.items():  # (orjson took only string keys in `head`)
        if part in pages:
            continue
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
