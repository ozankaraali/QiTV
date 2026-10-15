"""The EPG cache file, and the packed guide it loads into (no Qt).

A guide is a MultiKeyDict of programme lists (XMLTV sources) or a dict (STB portals). Saved as
one pickle, it was read by a single pickle.load call that built every programme's dicts while
holding the GIL: the GUI thread stood still for as long as that took (a third of a second for
90,000 programmes, at every start and provider switch), and the guide then held about 1 KiB
of Python objects per programme.

Here an XMLTV guide is saved as a stream of pickles: a header, its channels in pieces of about
PIECE_BYTES, each channel's programme list pickled on its own (a blob), then its key map and
an end mark. Reading it builds no programme at all: the blobs load as bytes into a
PackedGuide, which unpickles a channel's programmes the first time a lookup asks for them and
keeps them. Between pieces the reading thread runs Python code again, so the GUI thread gets
its turn. An STB guide (a dict) is saved in pieces of plain entries; any other guide whole.
"""

from __future__ import annotations

import os
import pickle
import tempfile
from typing import Any, Iterable, Iterator

from services.multikeydict import MultiKeyDict

PIECE_BYTES = 1 << 20  # programme blobs per piece of the file
PIECE_ENTRIES = 2000  # entries per piece for the other kinds (STB guides, key maps)
_MAGIC = "qitv-guide"
_VERSION = 1
_END = "end"
_KINDS = ("packed", "dict", "object")
_PROTOCOL = pickle.HIGHEST_PROTOCOL


class _Packed:
    """A channel's programme list, still pickled."""

    __slots__ = ("blob",)

    def __init__(self, blob: bytes) -> None:
        self.blob = blob


class PackedGuide(MultiKeyDict):
    """A MultiKeyDict whose values stay pickled until a lookup reads them (then kept).

    Lookups from several threads may unpickle the same channel at once: each gets an equal
    list, and one of them is kept.
    """

    def _value(self, keys: tuple) -> Any:
        value = self._data[keys]
        if isinstance(value, _Packed):
            value = pickle.loads(value.blob)
            self._data[keys] = value
        return value

    def __getitem__(self, key):
        keys = self._keys_map.get(key)
        if keys is None:
            raise KeyError(key)
        return self._value(keys)

    def get(self, key, default=None):
        keys = self._keys_map.get(key)
        if keys is None:
            return default
        return self._value(keys)

    def items(self):
        return [(keys, self._value(keys)) for keys in list(self._data)]

    def setdefault(self, keys, default=None):
        if not isinstance(keys, tuple):
            keys = (keys,)
        if keys in self._data:
            return self._value(keys)
        self[keys] = default
        return default

    def serialize(self):
        return [list(keys) + [value] for keys, value in self.items()]

    def __repr__(self):
        return f"{self.__class__.__name__}({dict(self.items())})"

    def packed(self) -> int:
        """How many channels are still pickled (for tests)."""
        return sum(isinstance(value, _Packed) for value in self._data.values())


def _blob(value: Any) -> bytes:
    if isinstance(value, _Packed):
        return value.blob
    return pickle.dumps(value, _PROTOCOL)


def _blob_pieces(entries: Iterable[tuple[Any, Any]]) -> Iterator[list[tuple[Any, bytes]]]:
    piece: list[tuple[Any, bytes]] = []
    size = 0
    for keys, value in entries:
        blob = _blob(value)
        piece.append((keys, blob))
        size += len(blob)
        if size >= PIECE_BYTES:
            yield piece
            piece, size = [], 0
    if piece:
        yield piece


def _entry_pieces(entries: Iterable[tuple[Any, Any]]) -> Iterator[list[tuple[Any, Any]]]:
    piece: list[tuple[Any, Any]] = []
    for entry in entries:
        piece.append(entry)
        if len(piece) >= PIECE_ENTRIES:
            yield piece
            piece = []
    if piece:
        yield piece


def write_guide(path: str, guide: Any) -> Any:
    """Save `guide` at `path`, replacing the file only once it is complete.

    Returns the guide as read_guide would load it: a PackedGuide for a MultiKeyDict (equal to
    `guide`, holding its programmes pickled), else `guide` itself.
    """
    if isinstance(guide, MultiKeyDict):
        kind = "packed"
    elif type(guide) is dict:
        kind = "dict"
    else:
        kind = "object"
    packed = PackedGuide() if kind == "packed" else None
    directory = os.path.dirname(path) or "."
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".guide-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump((_MAGIC, _VERSION, kind), f, _PROTOCOL)
            if kind == "object":
                pickle.dump(guide, f, _PROTOCOL)
            elif kind == "dict":
                for piece in _entry_pieces(guide.items()):
                    pickle.dump(piece, f, _PROTOCOL)
                pickle.dump(_END, f, _PROTOCOL)
            else:
                assert packed is not None
                for piece in _blob_pieces(guide._data.items()):
                    pickle.dump(piece, f, _PROTOCOL)
                    packed._data.update((keys, _Packed(blob)) for keys, blob in piece)
                pickle.dump(_END, f, _PROTOCOL)
                for piece in _entry_pieces(guide._keys_map.items()):
                    pickle.dump(piece, f, _PROTOCOL)
                pickle.dump(_END, f, _PROTOCOL)
                packed._keys_map = dict(guide._keys_map)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return packed if packed is not None else guide


def _read_entries(f) -> Iterator[tuple[Any, Any]]:
    while True:
        piece = pickle.load(f)
        if piece == _END:
            return
        yield from piece


def read_guide(path: str) -> Any:
    """The guide saved at `path` by write_guide (a PackedGuide for an XMLTV guide).

    ValueError for a file write_guide did not write; EOFError or an UnpicklingError for a
    damaged one; OSError when it cannot be read.
    """
    with open(path, "rb") as f:
        header = pickle.load(f)
        if (
            not isinstance(header, tuple)
            or len(header) != 3
            or header[:2] != (_MAGIC, _VERSION)
            or header[2] not in _KINDS
        ):
            raise ValueError("not a saved guide")
        kind = header[2]
        if kind == "object":
            return pickle.load(f)
        if kind == "dict":
            return dict(_read_entries(f))
        guide = PackedGuide()
        guide._data = {keys: _Packed(blob) for keys, blob in _read_entries(f)}
        guide._keys_map = dict(_read_entries(f))
        return guide
