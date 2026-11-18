"""A song's album and cover, read from its folder on a worker thread (spec §10).

Blocking disk and libVLC work: call it only off the GUI thread (ui.song_jobs relays it). Each
call parses with a libVLC instance of its own (INSTANCE_ARGS: no picture, no sound, no Lua, no
media library), never the GUI's player: `parse_with_options(local)` reads a file's tags (QiTV
has no tag library) and, for one with an embedded picture, writes it to libVLC's art cache.

Only the song's own folder is read (never a "Disc 2" beside it), its audio files only
(`open_paths.is_music`), the hidden ones skipped (macOS's "._" companions). A folder of
thousands of files, or one on a slow share, is read for LIST_BUDGET_S at most and its first
LIST_CAP files at most, in natural order: what was read by then is the listing (`read_album`
says whether it was cut short: such an album can't say what follows its last). A file's own
parse gives up after PARSE_TIMEOUT_MS, or sooner when the budget runs out; a file not read is
listed as such ("parsed": False) and matches no album. Nothing here raises.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import logging
import os
import time
from typing import Any, Optional
import unicodedata

from services.open_paths import is_music, natural_key
from services.song_facts import (
    META_KEYS,
    Track,
    album_tracks,
    folder_cover,
    local_path,
    song_tags,
    stand_in_names,
)

logger = logging.getLogger(__name__)

INSTANCE_ARGS = ("--quiet", "--no-video", "--aout=dummy", "--no-lua", "--no-media-library")
PARSE_TIMEOUT_MS = 2000
LIST_BUDGET_S = 8.0
LIST_CAP = 400
_POLL_S = 0.01  # how often a parse under way is looked at
_GRACE_S = 0.05  # past its timeout, libVLC is given this long to say so


def _vlc() -> Any:
    try:
        import vlc  # the bundled libVLC, set up before (main.py)
    except Exception:  # no libVLC
        return None
    return vlc


def _open_instance() -> Any:
    """A libVLC instance of this call's own (None without libVLC)."""
    vlc = _vlc()
    if vlc is None:
        return None
    try:
        return vlc.Instance(list(INSTANCE_ARGS))
    except Exception as error:
        logger.info("libVLC could not start to read songs (%s)", type(error).__name__)
        return None


def _parse(instance: Any, path: str, timeout_ms: int, *, fetch: bool = False) -> Optional[dict]:
    """The tags libVLC reads from the file at `path` (song_facts.song_tags), or None when it
    could not read them within `timeout_ms`. `fetch` also looks for the file's art (libVLC's
    fetch_local: its art cache, then a few exact-case names in its folder)."""
    vlc = _vlc()
    flags = vlc.MediaParseFlag.local
    if fetch:
        flags = vlc.MediaParseFlag(flags.value | vlc.MediaParseFlag.fetch_local.value)
    media = instance.media_new_path(path)
    if media is None:
        return None
    try:
        if media.parse_with_options(flags, max(1, int(timeout_ms))) != 0:
            return None
        deadline = time.monotonic() + timeout_ms / 1000 + _GRACE_S
        status = _status(media)
        while status == 0 and time.monotonic() < deadline:
            time.sleep(_POLL_S)
            status = _status(media)
        if status != int(vlc.MediaParsedStatus.done.value):
            if status == 0:
                media.parse_stop()
            return None
        meta = {name: media.get_meta(getattr(vlc.Meta, name)) for name in META_KEYS}
        return song_tags(
            meta,
            stand_ins=stand_in_names(os.path.basename(path)),
            duration_ms=int(media.get_duration() or 0),
        )
    finally:
        media.release()


def _status(media: Any) -> int:
    status = media.get_parsed_status()
    return int(getattr(status, "value", status) or 0)


def music_files(folder: str) -> list[tuple[str, Optional[int]]]:
    """The audio files directly in `folder` as (name, size or None), in natural order; hidden
    ones left out. [] when the folder can't be read."""
    found: list[tuple[str, Optional[int]]] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if (
                        entry.name.startswith(".")
                        or not is_music(entry.name)
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
    except Exception as error:
        logger.debug("Could not list a song's folder: %s", type(error).__name__)
        return []
    found.sort(key=lambda item: natural_key(item[0]))
    return found


def read_tracks(
    files: Sequence[tuple[str, Optional[int]]],
    *,
    budget_s: float = LIST_BUDGET_S,
    cap: int = LIST_CAP,
    stop: Optional[Callable[[], bool]] = None,
) -> list[Track]:
    """Each file's Track (`files`: (path, size or None)), read in turn until `budget_s` has
    passed, `cap` were read or `stop()` says so: what was read by then. A file libVLC could
    not read (it failed, or its time ran out) is there with "parsed": False and no tags.
    [] without libVLC."""
    return _read(files, time.monotonic() + budget_s, cap, stop)


def _read(
    files: Sequence[tuple[str, Optional[int]]],
    deadline: float,
    cap: int,
    stop: Optional[Callable[[], bool]],
) -> list[Track]:
    return _read_some(files, deadline, cap, stop)[0]


def _read_some(
    files: Sequence[tuple[str, Optional[int]]],
    deadline: float,
    cap: int,
    stop: Optional[Callable[[], bool]],
) -> tuple[list[Track], bool]:
    """`_read`'s Tracks, and whether every file was read: the budget, the cap or `stop` cut
    none of them short (False without libVLC, or when reading failed)."""
    tracks: list[Track] = []
    whole = False
    instance = None
    try:
        instance = _open_instance()
        if instance is None:
            return [], False
        for path, size in files[: max(0, cap)]:
            left = deadline - time.monotonic()
            if left <= 0 or (stop is not None and stop()):
                break
            try:
                tags = _parse(instance, path, min(PARSE_TIMEOUT_MS, int(left * 1000) or 1))
            except Exception as error:  # a file libVLC fails on: its tags are not known
                logger.info("A song's tags could not be read (%s)", type(error).__name__)
                tags = None
            track = song_tags({}) if tags is None else dict(tags)
            track.update(path=path, name=os.path.basename(path), size=size, parsed=tags is not None)
            tracks.append(track)
        whole = len(tracks) == len(files)
    except Exception as error:  # never raises: what was read stays
        logger.warning("Songs could not be read (%s)", type(error).__name__)
    finally:
        if instance is not None and hasattr(instance, "release"):
            try:
                instance.release()
            except Exception:
                pass
    return tracks, whole


def list_album(folder: str, *, budget_s: float = LIST_BUDGET_S, cap: int = LIST_CAP) -> list[Track]:
    """The Tracks of the audio files in `folder` (`music_files`), within `budget_s` (from the
    call, its folder's listing included) and `cap` (module docstring). Never raises; blocking:
    call on a worker."""
    deadline = time.monotonic() + budget_s
    files = [(os.path.join(folder, name), size) for name, size in music_files(folder)]
    return _read(files, deadline, cap, None)


def album_of(
    path: str,
    *,
    budget_s: float = LIST_BUDGET_S,
    cap: int = LIST_CAP,
    stop: Optional[Callable[[], bool]] = None,
) -> list[Track]:
    """The album of the song at `path` in its folder (song_facts.album_tracks): its Tracks in
    order, the song's own among them, or [] (a lone song, or one libVLC could not read). The
    song is read first, so it is known whatever the folder's size; then its folder's audio
    files, as `list_album` reads them (`stop` ends that between two files). Never raises;
    blocking: call on a worker."""
    return read_album(path, budget_s=budget_s, cap=cap, stop=stop)[0]


def read_album(
    path: str,
    *,
    budget_s: float = LIST_BUDGET_S,
    cap: int = LIST_CAP,
    stop: Optional[Callable[[], bool]] = None,
) -> tuple[list[Track], bool]:
    """`album_of`'s album, and whether the folder was read whole: one cut short (the budget,
    the cap, `stop`) can't say what comes after its last track. Never raises; blocking."""
    deadline = time.monotonic() + budget_s
    try:
        folder = os.path.dirname(path)
        others = [
            (os.path.join(folder, n), size)
            for n, size in music_files(folder)
            if not _is_named(path, folder, n)
        ]
        try:
            own: Optional[int] = os.path.getsize(path)
        except OSError:
            own = None
        tracks, whole = _read_some([(path, own), *others], deadline, cap, stop)
        if not tracks:
            return [], whole
        return album_tracks(tracks[0], tracks), whole
    except Exception as error:
        logger.warning("A song's album could not be listed (%s)", type(error).__name__)
        return [], False


def _is_named(path: str, folder: str, name: str) -> bool:
    """`name` in `folder` is the file at `path` (spelled otherwise where the disk ignores case
    or the form of accents: macOS, Windows)."""
    mine = os.path.basename(path)
    if name == mine:
        return True
    if (
        unicodedata.normalize("NFC", name).casefold()
        != unicodedata.normalize("NFC", mine).casefold()
    ):
        return False
    return _same_place(os.path.join(folder, name), path)


def _art_file(art_url: str) -> Optional[str]:
    """The local file libVLC's artwork URL names (a file:// URL, or a path), if one is there:
    never attachment:// (a parse writes that one to the art cache) nor a network address."""
    path = local_path(art_url)
    return path if path is not None and os.path.isfile(path) else None


def _same_place(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError, ValueError:
        return False


def _parsed_art(path: str) -> Optional[str]:
    """The art file a parse of the song at `path` finds (libVLC's fetch_local)."""
    instance = _open_instance()
    if instance is None:
        return None
    try:
        tags = _parse(instance, path, PARSE_TIMEOUT_MS, fetch=True)
    finally:
        instance.release()
    return _art_file((tags or {}).get("art_url") or "")


def _folders_cover(folder: str) -> Optional[str]:
    try:
        name = folder_cover(os.listdir(folder))
    except OSError:
        return None
    return os.path.join(folder, name) if name is not None else None


def cover_candidates(path: str, art_url: str = "") -> list[str]:
    """The picture files that may be the cover of the song at `path`, the likeliest first:
    the song's own picture (libVLC's `art_url`, a file in its art cache; else a parse of the
    file finds it), then its folder's cover (song_facts.folder_cover: any case, QiTV's order),
    then a picture libVLC guessed in the folder by its own exact-case names. Only `art_url`
    for a song that is no local file (a radio's: its logo is the channel's). Never raises;
    blocking: call on a worker."""
    try:
        art = _art_file(art_url)
        if not os.path.isfile(path):
            return [art] if art else []
        if art is None:
            art = _parsed_art(path)
        folder = os.path.dirname(path)
        own = _folders_cover(folder)
        if art is None:
            return [own] if own else []
        if not _same_place(os.path.dirname(art), folder):
            return [art, own] if own else [art]  # the song's own picture first
        # libVLC's guess at a picture beside the song: the folder's cover in QiTV's order first
        if own is None or _same_place(art, own):
            return [own or art]
        return [own, art]
    except Exception as error:  # no cover: the screen draws its own
        logger.info("A song's cover could not be looked for (%s)", type(error).__name__)
        return []
