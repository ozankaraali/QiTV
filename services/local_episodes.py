"""An episode named in a file name, and the same show's next one in its folder.

Pure logic: no Qt. `list_folder` does blocking disk I/O, so call it only on a worker.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import logging
import os
import re
from typing import Optional
import unicodedata

from services.open_paths import is_video, natural_key

logger = logging.getLogger(__name__)

_SXXEYY = re.compile(
    r"(?<![a-z0-9])s(\d{1,3})[ ._-]?e(\d{1,4})(?:-?e(\d{1,4})|-(\d{1,4})(?![a-z0-9]))?(?![0-9])",
    re.IGNORECASE,
)
_LEADING_GROUP = re.compile(r"^\s*(?:\[[^\]]*\]|\([^)]*\))")
_NXMM = re.compile(r"(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?![a-z0-9])", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[\W_]+")
_TITLE_TRIM = " -_.(["
# A season folder, read on a show key: "season 4", "s04", "sezon", ... (the number is optional).
_SEASON_FOLDER = re.compile(r"^(?:(?:season|sezon|staffel|saison)(?: \d{1,3})?|s\d{1,3})$")


@dataclass(frozen=True)
class EpisodeName:
    show: str  # the comparison key
    title: str  # the show as the card names it
    season: int
    first: int
    last: int  # a double episode (S01E01E02, S01E01-E02, S01E01-02) is first 1, last 2
    sxxeyy: bool = True  # written S01E02, not 1x02


def _show_key(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold().replace("ı", "i")
    return _NON_ALNUM.sub(" ", text).strip()


def _same_show(a: str, b: str) -> bool:
    """Equal keys, or one's words lead the other's with at least two words in common."""
    if not a or not b:
        return False
    if a == b:
        return True
    wa, wb = a.split(), b.split()
    n = min(len(wa), len(wb))
    return n >= 2 and wa[:n] == wb[:n]


def _folder_show(folder: str) -> str:
    """The show key a bare name takes from its folder (the folder above a season folder)."""
    folder = folder.rstrip("/\\")
    key = _show_key(os.path.basename(folder))
    if _SEASON_FOLDER.match(key):
        key = _show_key(os.path.basename(os.path.dirname(folder).rstrip("/\\")))
    return key


def parse_episode(file_name: str) -> Optional[EpisodeName]:
    """The episode a file name states, or None (resolutions, codecs and years never count)."""
    stem = os.path.splitext(file_name)[0]
    match = _SXXEYY.search(stem)
    sxxeyy = match is not None
    if match:
        season, first = int(match.group(1)), int(match.group(2))
        last = first
        if match.group(3):
            last = int(match.group(3))
        elif match.group(4) and int(match.group(4)) == first + 1:
            last = first + 1
    else:
        match = _NXMM.search(stem)
        if not match:
            return None
        season, first = int(match.group(1)), int(match.group(2))
        last = first
    before = stem[: match.start()]
    named = _LEADING_GROUP.sub("", before, count=1)
    title = named.replace(".", " ").replace("_", " ").strip(_TITLE_TRIM) or stem
    return EpisodeName(_show_key(before), title, season, first, max(first, last), sxxeyy)


def list_folder(folder: str) -> list[tuple[str, Optional[int]]]:
    """The video files directly in `folder`, as (name, size); size None when unreadable.
    `[]` when the folder can't be read. Never raises; blocking I/O: call on a worker."""
    found: list[tuple[str, Optional[int]]] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if (
                        entry.name.startswith(".")
                        or not is_video(entry.name)
                        or not entry.is_file()
                    ):
                        continue
                    try:
                        size: Optional[int] = entry.stat().st_size
                    except OSError:
                        size = None
                    found.append((entry.name, size))
                except OSError:
                    continue
    except Exception as exc:
        logger.debug("Could not list a folder: %s", type(exc).__name__)
        return []
    return found


def next_in_folder(
    current_path: str, listing: Sequence[tuple[str, Optional[int]]]
) -> Optional[dict]:
    """The same show's next episode in `listing` (the current file's folder), or None.

    The same show: equal keys, or one's leading words are the other's (two words at least).
    A bare name takes its folder's show (above a season folder), derived from `current_path`.

    (season, last + 1) when present; else (season + 1, 1), but only when no later episode of
    this season is listed: a gap offers nothing rather than skipping an episode.
    """
    folder, current_name = os.path.split(current_path)
    current = parse_episode(current_name)
    if current is None:
        return None
    # A bare name (nothing before the pattern) takes its show from its folder; a bare 1x02
    # has no such footing and never links.
    folder_show = _folder_show(folder)
    if not current.show and not current.sxxeyy:
        return None
    current_show = current.show or folder_show
    same_show: list[tuple[str, Optional[int], EpisodeName]] = []
    for name, size in listing:
        if name == current_name or name.startswith("."):
            continue
        ep = parse_episode(name)
        if ep is None or (not ep.show and not ep.sxxeyy):
            continue
        if not _same_show(current_show, ep.show or folder_show):
            continue
        same_show.append((name, size, ep))

    def pick(season: int, number: int) -> Optional[tuple[str, Optional[int], EpisodeName]]:
        hits = [h for h in same_show if h[2].season == season and h[2].first == number]
        return min(hits, key=lambda h: natural_key(h[0])) if hits else None

    chosen = pick(current.season, current.last + 1)
    if chosen is None and not any(
        h[2].season == current.season and h[2].first > current.last for h in same_show
    ):
        chosen = pick(current.season + 1, 1)
    if chosen is None:
        return None
    name, size, ep = chosen
    return {
        "name": ep.title,
        "season": ep.season,
        "number": ep.first,
        "local_path": os.path.join(folder, name),
        "size": size,
    }
