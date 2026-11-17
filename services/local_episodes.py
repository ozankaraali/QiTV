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
    r"(?<![a-z0-9])s(\d{1,3})[ ._-]?e(\d{1,4})(?:-?e(\d{1,4}))?(?![0-9])", re.IGNORECASE
)
_NXMM = re.compile(r"(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?![a-z0-9])", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[\W_]+")
_TITLE_TRIM = " -_.(["


@dataclass(frozen=True)
class EpisodeName:
    show: str  # the comparison key
    title: str  # the show as the card names it
    season: int
    first: int
    last: int  # a double episode (S01E01E02, S01E01-E02) is first 1, last 2


def _show_key(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold().replace("ı", "i")
    return _NON_ALNUM.sub(" ", text).strip()


def parse_episode(file_name: str) -> Optional[EpisodeName]:
    """The episode a file name states, or None (resolutions, codecs and years never count)."""
    stem = os.path.splitext(file_name)[0]
    match = _SXXEYY.search(stem)
    if match:
        season, first = int(match.group(1)), int(match.group(2))
        last = int(match.group(3)) if match.group(3) else first
    else:
        match = _NXMM.search(stem)
        if not match:
            return None
        season, first = int(match.group(1)), int(match.group(2))
        last = first
    before = stem[: match.start()]
    title = before.replace(".", " ").replace("_", " ").strip(_TITLE_TRIM) or stem
    return EpisodeName(_show_key(before), title, season, first, max(first, last))


def list_folder(folder: str) -> list[tuple[str, Optional[int]]]:
    """The video files directly in `folder`, as (name, size); size None when unreadable.
    `[]` when the folder can't be read. Never raises; blocking I/O: call on a worker."""
    found: list[tuple[str, Optional[int]]] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if not is_video(entry.name) or not entry.is_file():
                        continue
                    try:
                        size: Optional[int] = entry.stat().st_size
                    except OSError:
                        size = None
                    found.append((entry.name, size))
                except OSError:
                    continue
    except Exception:
        logger.debug("Could not list %r", folder, exc_info=True)
        return []
    return found


def next_in_folder(
    current_path: str, listing: Sequence[tuple[str, Optional[int]]]
) -> Optional[dict]:
    """The same show's next episode in `listing` (the current file's folder), or None.

    (season, last + 1) when present; else (season + 1, 1), but only when no later episode of
    this season is listed: a gap offers nothing rather than skipping an episode.
    """
    folder, current_name = os.path.split(current_path)
    current = parse_episode(current_name)
    if current is None:
        return None
    same_show: list[tuple[str, Optional[int], EpisodeName]] = []
    for name, size in listing:
        if name == current_name:
            continue
        ep = parse_episode(name)
        if ep is not None and ep.show == current.show:
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
