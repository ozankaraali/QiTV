"""A back-off n-gram over our Turkish Moonshine's pieces, read with the standard library only
(mmap, bisect, struct; no numpy, no native code): the language model a beam search may read
with (services.captions.moon_beam; 2.10's Turkish is read without one).

The file (".qng", built from an ARPA file over piece ids written as decimal words: `write_arpa`):

    b"QNG1", the order N (uint32), then per order n = 1..N: its count, the offsets of its keys
    (uint64, sorted), of its probability codes (uint8) and of its back-off codes (uint8; 0 for
    order N, which has none) as four uint64, and two codebooks of 256 float32 (log10
    probability, log10 back-off). Little-endian throughout.

An n-gram's key packs its ids 14 bits each for n <= 4 (exact); a fifth id is mixed into the
4-gram's key (64 bits: collisions about one in a million at these sizes, refused when writing).
Code 0 is "none" (-99); codes 1..255 are 255 even steps over the finite values. Measured against
KenLM on the same ARPA file (2026-11-08): within 0.025 log10 per token.

Ids: <s> is 1, </s> 2 (the model's start and end tokens), the ARPA's <unk> UNK; any id the model
writes that the LM never saw scores as <unk>.
"""

from __future__ import annotations

from array import array
import bisect
import math
import mmap
import os
import struct
import sys
from typing import Iterable, Optional, Sequence, Union

MAGIC = b"QNG1"
BITS = 14
UNK = (1 << BITS) - 1  # the ARPA's <unk> (piece 3, the model's own unknown, is a word of its own)
BOS, EOS = 1, 2
_MASK64 = (1 << 64) - 1
_MIX = 0x9E3779B97F4A7C15
_SALT = 0x632BE59BD9B4E019
_ORDER_HEAD = struct.Struct("<QQQQ")
_BOOK = struct.Struct("<256f")
_NONE = -99.0  # code 0
_FINITE = -90.0  # a value at or below this (ARPA's -99 for <s>) is "none"


def key(ids: Sequence[int]) -> int:
    """An n-gram's key (ids 14 bits each up to four; a fifth one mixed in)."""
    packed = 0
    for i, token in enumerate(ids):
        if i < 4:
            packed = (packed << BITS) | token
        else:
            packed = ((packed * _MIX) ^ (token + _SALT)) & _MASK64
    return packed


class NgramLM:
    """A .qng file, mapped (the pages it reads stay in the page cache, not in this process's
    memory). `score` is safe from several threads (it only reads)."""

    def __init__(self, path: Union[str, os.PathLike[str]]) -> None:
        if sys.byteorder != "little":
            raise ValueError("a .qng file is read on little-endian computers only")
        self._file = open(path, "rb")
        try:
            self._map = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        except OSError, ValueError:
            self._file.close()
            raise
        view = memoryview(self._map)
        size = len(view)
        try:
            if size < 8 or bytes(view[:4]) != MAGIC:
                raise ValueError(f"{path}: not a .qng file")
            (self.order,) = struct.unpack_from("<I", view, 4)
            if not 1 <= self.order <= 5:
                raise ValueError(f"{path}: order {self.order}")
            self._keys: list = [None]
            self._probs: list = [None]
            self._backs: list = [None]
            self._prob_book: list = [None]
            self._back_book: list = [None]
            at = 8
            for n in range(1, self.order + 1):
                if at + _ORDER_HEAD.size + 2 * _BOOK.size > size:
                    raise ValueError(f"{path}: cut short")
                count, keys_at, probs_at, backs_at = _ORDER_HEAD.unpack_from(view, at)
                at += _ORDER_HEAD.size
                probs_book = _BOOK.unpack_from(view, at)
                backs_book = _BOOK.unpack_from(view, at + _BOOK.size)
                at += 2 * _BOOK.size
                ends = [keys_at + 8 * count, probs_at + count]
                if backs_at:
                    ends.append(backs_at + count)
                if max(ends) > size or (n < self.order and not backs_at):
                    raise ValueError(f"{path}: order {n} lies outside the file")
                self._keys.append(view[keys_at : keys_at + 8 * count].cast("Q"))
                self._probs.append(view[probs_at : probs_at + count])
                self._backs.append(view[backs_at : backs_at + count] if backs_at else None)
                self._prob_book.append(probs_book)
                self._back_book.append(backs_book)
        except BaseException:
            self.close()
            raise

    def _find(self, n: int, packed: int) -> int:
        keys = self._keys[n]
        i = bisect.bisect_left(keys, packed)
        return i if i < len(keys) and keys[i] == packed else -1

    def known(self, token: int) -> bool:
        """The LM has the id as a word of its own."""
        return self._find(1, token) >= 0

    def score(self, context: tuple[int, ...], token: int) -> tuple[float, tuple[int, ...]]:
        """log10 p(token | context) and the context after it (its last N - 1 ids). A sentence
        starts with the context (BOS,); `token` EOS scores its end."""
        if self._find(1, token) < 0:
            token = UNK
        width = self.order - 1
        context = context[-width:] if width else ()
        total = 0.0
        for n in range(len(context) + 1, 0, -1):
            gram = context[len(context) - (n - 1) :] + (token,)
            i = self._find(n, key(gram))
            if i >= 0:
                total += self._prob_book[n][self._probs[n][i]]
                break
            before = gram[:-1]  # backed off: the context's back-off weight, when it is there
            if before:
                j = self._find(n - 1, key(before))
                backs = self._backs[n - 1]
                if j >= 0 and backs is not None:
                    total += self._back_book[n - 1][backs[j]]
        after = (context + (token,))[-width:] if width else ()
        return total, after

    def close(self) -> None:
        """The file unmapped (scores after it fail)."""
        for name in ("_keys", "_probs", "_backs"):
            for view in getattr(self, name, ()):
                if view is not None:
                    view.release()
            setattr(self, name, [])
        mapped = getattr(self, "_map", None)
        if mapped is not None:
            try:
                mapped.close()
            except BufferError:
                pass  # a view still out: the map goes with it
        self._file.close()

    def __enter__(self) -> NgramLM:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ---- writing (offline: a model folder's LM is built once) -----------------------------------


def _codebook(values: Sequence[float]) -> tuple[list[float], bytes]:
    """255 even steps over the finite values (code 0: none), and each value's code."""
    finite = [v for v in values if math.isfinite(v) and v > _FINITE]
    low, high = (min(finite), max(finite)) if finite else (0.0, 0.0)
    step = (high - low) / 254 if high > low else 1.0
    book = [_NONE] + [low + step * i for i in range(255)]
    if high <= low:
        book[1:] = [low] * 255
    codes = bytearray(len(values))
    for i, value in enumerate(values):
        if math.isfinite(value) and value > _FINITE:
            codes[i] = min(254, max(0, round((value - low) / step))) + 1
    return book, bytes(codes)


def write(grams: dict[int, list[tuple[tuple[int, ...], float, float]]], out: str) -> None:
    """A .qng file from {n: [(ids, log10 probability, log10 back-off), ...]} (orders 1..N)."""
    order = max(grams)
    if sorted(grams) != list(range(1, order + 1)) or order > 5:
        raise ValueError(f"orders {sorted(grams)}")
    header = bytearray(MAGIC + struct.pack("<I", order))
    blobs: list[bytes] = []
    at = 8 + order * (_ORDER_HEAD.size + 2 * _BOOK.size)
    for n in range(1, order + 1):
        rows = sorted(((key(ids), p, b) for ids, p, b in grams[n]), key=lambda row: row[0])
        keys = array("Q", (row[0] for row in rows))
        if any(keys[i] == keys[i - 1] for i in range(1, len(keys))):
            raise ValueError(f"two {n}-grams share a key")
        probs_book, probs = _codebook([row[1] for row in rows])
        if n < order:
            backs_book, backs = _codebook([row[2] for row in rows])
        else:
            backs_book, backs = [0.0] * 256, b""
        keys_at, probs_at = at, at + 8 * len(keys)
        backs_at = probs_at + len(probs) if backs else 0
        at = probs_at + len(probs) + len(backs)
        header += _ORDER_HEAD.pack(len(keys), keys_at, probs_at, backs_at)
        header += _BOOK.pack(*probs_book) + _BOOK.pack(*backs_book)
        blobs += [keys.tobytes(), probs, backs]
    with open(out, "wb") as file:
        file.write(header)
        for blob in blobs:
            file.write(blob)


def read_arpa(lines: Iterable[str]) -> dict[int, list[tuple[tuple[int, ...], float, float]]]:
    """An ARPA file's n-grams over decimal piece ids (<s>, </s>, <unk> by name)."""
    special = {"<s>": BOS, "</s>": EOS, "<unk>": UNK}
    grams: dict[int, list[tuple[tuple[int, ...], float, float]]] = {}
    n: Optional[int] = None
    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("\\") and line.endswith("-grams:"):
            n = int(line[1 : line.index("-")])
            grams[n] = []
            continue
        if n is None or not line or line.startswith("\\"):
            continue
        parts = line.split("\t")
        ids = tuple(special[w] if w in special else int(w) for w in parts[1].split(" "))
        grams[n].append((ids, float(parts[0]), float(parts[2]) if len(parts) > 2 else 0.0))
    return grams


def write_arpa(arpa: Union[str, os.PathLike[str]], out: str) -> None:
    """An ARPA file over piece ids as a .qng file."""
    with open(arpa, encoding="utf-8") as lines:
        write(read_arpa(lines), out)
