"""What a song is: its tags, its album among its folder's files, the folder's cover file and the
cover's colours (spec §10, the audio screen).

Pure logic: no disk, no Qt, no libVLC. The tags come from libVLC's parser (QiTV has no tag
library): `song_tags` turns what libVLC read (python-vlc's Meta names, `META_KEYS`) into plain
values, as `PlaybackSession.tags()` reads the playing media and `services.album_listing` the
files beside it.

A `Track` is a plain dict: path, name (the file's), size (bytes, None when unknown), title,
artist, album_artist, album, disc, track, year and duration_ms (ms), art_url (libVLC's artwork
URL) and now_playing (a stream's song). What the tags don't say is "" (text) or None (numbers).
A file listed but not read (libVLC failed on it, or the listing's time ran out) has
"parsed": False: its tags are not known, which is not the same as having none.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import math
import os
import re
from typing import Any, Optional
import unicodedata
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from services.open_paths import natural_key

# python-vlc's Meta names read from a media, and the keys they become.
META_KEYS = {
    "Title": "title",
    "Artist": "artist",
    "AlbumArtist": "album_artist",
    "Album": "album",
    "DiscNumber": "disc",
    "TrackNumber": "track",
    "Date": "year",
    "ArtworkURL": "art_url",
    "NowPlaying": "now_playing",
}
Track = dict  # the keys above (module docstring)

# The glow's colours when a cover has none of its own (ui/theme.py's accent and brand).
BRAND_COLOURS = [(0xE0, 0x8A, 0x5F), (0xC9, 0x6B, 0x43)]

_NUMBER = re.compile(r"\s*(\d{1,4})(?!\d)")
_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_NUMBERED = re.compile(r"^\d{1,3}[ ._-]")  # "01 Song", "3.Song", "004_Song", "5-Song"
_SPACES = re.compile(r"\s+")
_COVER_STEMS = ("cover", "folder", "front")
_COVER_EXTENSIONS = (".jpg", ".jpeg", ".png")
_SAMPLE_SIDE = 32  # the colours are read from at most this many pixels a side
_DARK, _LIGHT, _GREY = 40, 215, 30  # a pixel's brightest channel, darkest one, spread
_ENOUGH_COLOUR = 0.03  # of the pixels: fewer coloured ones and the cover counts as grey
_FEWEST_COLOURED = 6
_SAME_COLOUR = 64  # colours nearer than this (RGB distance) are one colour
_BUCKET_SHIFT = 5  # the colour buckets: 8 levels a channel


def track_number(value: Any) -> Optional[int]:
    """A track or disc number as tags write it: "3/12" and "03" are 3, "1/2" is 1; None when
    there is none ("", "A2")."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    match = _NUMBER.match(value)
    return int(match.group(1)) if match else None


def album_key(text: Optional[str]) -> str:
    """An album or artist tag as albums are matched: case folded, accents and marks dropped
    (NFKD), runs of spaces as one, the ends trimmed. A Turkish dotless ı is an i (as
    `local_episodes` reads a show's name)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold().replace("ı", "i")
    return _SPACES.sub(" ", text).strip()


def local_path(location: Optional[str]) -> Optional[str]:
    """The local path a file:// URL names, or `location` itself when it is a path; None for
    any other address (libVLC's attachment://, a network one)."""
    if not location:
        return None
    parts = urlsplit(location)
    scheme = parts.scheme.lower()
    if scheme == "file":
        path = parts.path
        if parts.netloc and parts.netloc.lower() != "localhost":
            path = f"//{parts.netloc}{parts.path}"  # a share's (Windows)
        try:
            return url2pathname(path)
        except OSError, ValueError:  # a share's elsewhere: urllib refuses it (URLError)
            return None
    if len(scheme) > 1:
        return None
    return location  # a path (a Windows drive letter reads as a one-letter scheme)


def stand_in_names(*urls: Optional[str]) -> set[str]:
    """The titles libVLC gives a media whose tags name none: its address, or its file's name."""
    names: set[str] = set()
    for url in urls:
        if not url:
            continue
        names.update((url, unquote(url)))
        path = local_path(url)
        if path is None:
            path = unquote(urlsplit(url).path)
        name = re.split(r"[\\/]", path.rstrip("/\\"))[-1]
        if name:
            names.add(name)
    return {unicodedata.normalize("NFC", name) for name in names}


def song_tags(
    meta: Mapping[str, Optional[str]],
    *,
    stand_ins: Iterable[str] = (),
    duration_ms: Optional[int] = None,
) -> dict[str, Any]:
    """What libVLC read of a media (`meta`: python-vlc's Meta names to its text, or None) as
    plain tags, every key there: text stripped ("" when missing), the track and disc as
    numbers, the year from the date. A title that is one of libVLC's `stand_ins` (its file's
    name, its address: `stand_in_names`) is no title. `duration_ms` <= 0 is not known."""
    tags: dict[str, Any] = {}
    for name, key in META_KEYS.items():
        value = meta.get(name)
        tags[key] = value.strip() if isinstance(value, str) else ""
    stand_in = {unicodedata.normalize("NFC", name) for name in stand_ins}
    if unicodedata.normalize("NFC", tags["title"]) in stand_in:
        tags["title"] = ""
    tags["track"] = track_number(tags["track"])
    tags["disc"] = track_number(tags["disc"])
    year = _YEAR.search(tags["year"])
    tags["year"] = year.group(1) if year else ""
    tags["duration_ms"] = int(duration_ms) if duration_ms and duration_ms > 0 else None
    return tags


def _same_artist(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """The same album artist, else the same artist, when both songs name one; else no telling."""
    for key in ("album_artist", "artist"):
        mine, theirs = album_key(a.get(key)), album_key(b.get(key))
        if mine and theirs:
            return mine == theirs
    return True


def _order(track: Mapping[str, Any]) -> tuple:
    disc, number = track_number(track.get("disc")), track_number(track.get("track"))
    return (
        1 if disc is None else disc,  # no disc tag: the first disc
        number is None,  # no track number: after those with one
        number or 0,
        natural_key(str(track.get("name") or "")),
    )


def _unread(track: Mapping[str, Any]) -> bool:
    """Its tags are not known (album_listing could not read them), so it matches no album."""
    return track.get("parsed") is False


def _numbered(track: Mapping[str, Any]) -> bool:
    return bool(_NUMBERED.match(str(track.get("name") or "")))


def album_tracks(current: Track, folder_tracks: Sequence[Track]) -> list[Track]:
    """The songs of `current`'s album among its folder's (`folder_tracks`), `current` among them,
    ordered by disc, track and name; [] when it has no album there (a lone song).

    The album is the songs whose album tag is `current`'s (`album_key`), by the same album
    artist or artist when both name one. A song with no album tag whose name starts with a
    number ("01 …") has the folder's numbered songs that carry no album tag either. Another
    album is never listed: neither one named alike by another artist nor numbered files with
    an album tag of their own, nor files whose tags were not read ("parsed": False): with
    `current`'s own not read, it has no list."""
    if _unread(current):
        return []
    name = current.get("name")
    pool = [t for t in folder_tracks if not _unread(t)]
    if not any(t.get("name") == name for t in pool):
        pool.append(current)  # the listing missed it (its cap or time ran out first)
    album = album_key(current.get("album"))
    if album:
        found = [t for t in pool if album_key(t.get("album")) == album and _same_artist(current, t)]
    elif _numbered(current):
        found = [t for t in pool if not album_key(t.get("album")) and _numbered(t)]
    else:
        return []
    if len(found) < 2:
        return []
    return sorted(found, key=_order)


def folder_cover(names: Iterable[str]) -> Optional[str]:
    """The folder's cover among its file `names`: cover, folder or front, .jpg, .jpeg or .png,
    in any case, preferred in that order; None when it has none."""
    best: Optional[tuple[int, int, str]] = None
    for name in names:
        stem, extension = os.path.splitext(name.casefold())
        if stem not in _COVER_STEMS or extension not in _COVER_EXTENSIONS:
            continue
        rank = (_COVER_STEMS.index(stem), _COVER_EXTENSIONS.index(extension), name)
        if best is None or rank < best:
            best = rank
    return best[2] if best is not None else None


def _sample(rgb: bytes, width: int, height: int) -> list[tuple[int, int, int]]:
    """The pixels of an RGB888 picture, every few taken so at most _SAMPLE_SIDE a side."""
    step_x = max(1, -(-width // _SAMPLE_SIDE))
    step_y = max(1, -(-height // _SAMPLE_SIDE))
    pixels = []
    for y in range(0, height, step_y):
        row = y * width * 3
        for x in range(0, width, step_x):
            at = row + x * 3
            pixels.append((rgb[at], rgb[at + 1], rgb[at + 2]))
    return pixels


def _coloured(pixel: tuple[int, int, int]) -> bool:
    high, low = max(pixel), min(pixel)
    return high >= _DARK and low <= _LIGHT and high - low >= _GREY


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.dist(a, b)


def dominant_colours(rgb: bytes, width: int, height: int, k: int = 3) -> list[tuple[int, int, int]]:
    """Up to `k` colours of a cover (packed RGB888, `width` × `height`), the most of it first,
    read from a small copy (at most 32 × 32 pixels). Near-black, near-white and grey pixels are
    left out; a cover with too little colour left (all grey, or no picture) has QiTV's own
    (BRAND_COLOURS)."""
    if width <= 0 or height <= 0 or k <= 0 or len(rgb) < width * height * 3:
        return list(BRAND_COLOURS)
    pixels = _sample(rgb, width, height)
    coloured = [p for p in pixels if _coloured(p)]
    if len(coloured) < max(_FEWEST_COLOURED, _ENOUGH_COLOUR * len(pixels)):
        return list(BRAND_COLOURS)
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for r, g, b in coloured:
        key = (r >> _BUCKET_SHIFT, g >> _BUCKET_SHIFT, b >> _BUCKET_SHIFT)
        total = buckets.setdefault(key, [0, 0, 0, 0])
        total[0] += 1
        total[1] += r
        total[2] += g
        total[3] += b
    # The fullest buckets first; one near a colour already found joins it.
    found: list[list[float]] = []  # [count, r, g, b] (the colour is the pixels' mean)
    for count, r, g, b in sorted(buckets.values(), key=lambda t: -t[0]):
        mean = (r / count, g / count, b / count)
        for colour in found:
            if _distance(mean, (colour[1], colour[2], colour[3])) < _SAME_COLOUR:
                whole = colour[0] + count
                for i in (1, 2, 3):
                    colour[i] = (colour[i] * colour[0] + mean[i - 1] * count) / whole
                colour[0] = whole
                break
        else:
            found.append([count, *mean])
    found.sort(key=lambda c: -c[0])
    return [(round(c[1]), round(c[2]), round(c[3])) for c in found[:k]]
