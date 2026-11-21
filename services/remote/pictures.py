"""Pictures on a phone at home: the posters and logos its rows show, from QiTV's own image cache.

Only a session that came through the home door gets pictures. QiTV's GUI asks the shelf for a
token per picture (an opaque handle of 128 bits, as a film's), which it hands that phone inside
the sealed session; the phone's page asks the home door for GET /img/<token>. A token answers its
own session alone, asked from the address that session came from, and ends with it. A session
through the relay is never given one: its page is the relay's, which must never learn what anyone
watches. Nothing a phone receives names a picture's address (a provider's can carry the login).

The bytes are QiTV's image cache's (services/image_manager.py: the TV's own files), fetched into
it when missing, on a thread of their own: never the hub's loop, which carries the presses, nor
the GUI's. A cached file in one of the web's picture formats, of a reasonable size, goes as it
is; a larger one, or one in another format, goes as the copy QiTV's GUI scales from the TV's own
decoded picture (a PictureWanted event, then `put`); anything else is refused. What was served is
kept in memory a while. The shelf is thread-safe (the GUI issues, the hub's loop serves); the
server lives on the hub's loop. Nothing here logs a picture's address.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import concurrent.futures
from dataclasses import dataclass
from functools import partial
import logging
import os
import secrets
import threading
import time
from typing import Any, Callable, Optional

from services.remote.protocol import PictureWanted

logger = logging.getLogger(__name__)

POSTER, LOGO = "poster", "logo"  # a title's poster (2:3), a channel's logo (the TV's logo file)
KINDS = (POSTER, LOGO)
SESSION_PICTURES = 3000  # the tokens a session keeps: its latest
AS_IS_MAX = 400 * 1024  # a cached file this size or smaller goes as it is
MAX_SIDE = 2048  # and this many pixels a side at most (a phone decodes it whole)
SCALED_MAX = 2 * 1024 * 1024  # QiTV's own scaled copy, at most
KEPT_BYTES = 16 * 1024 * 1024  # what was served, kept in memory for the next ask
KEPT_COUNT = 400
FETCHES = 4  # pictures downloaded at once
FETCH_S = 25.0  # a picture's download (the image cache tries twice), at most, once begun
SCALE_S = 10.0  # QiTV's GUI scaling one, at most (a copy that comes later is kept)
# A picture that couldn't be had isn't tried again for this long: less than the page waits
# before it asks again (screens.js, 20 s), so that ask reaches the host (or the TV's cache).
FAILED_S = 15.0
FAILED_COUNT = 1000
_HEAD = 64
# What a picture's answer says: its own type, nothing to run, for this page alone; a phone may
# keep it while its token lives (a list drawn again asks again).
ANSWER_HEADERS = {
    "Cache-Control": "private, max-age=3600",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Cross-Origin-Resource-Policy": "same-origin",
}
_PAGES = (b"<!doctype html", b"<html", b"<head", b"<body", b"{", b"[")
_MISS = ("miss", b"", "")  # a cached file gone or cut off: downloaded again
_JPEG_SCAN = 64 * 1024  # how far into a JPEG its frame (and size) is looked for
# The JPEG markers that start a frame (SOF0–SOF15 but DHT, JPG and DAC) and those without length.
_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_BARE = frozenset((0x01, *range(0xD0, 0xD9)))

Found = tuple[bytes, str]  # a picture's bytes and their type


@dataclass(frozen=True)
class Picture:
    source: str  # its address (or a data: URI), as the TV's cache knows it
    kind: str  # POSTER or LOGO
    verify: bool = True  # the TLS check, as the TV's downloads do it (Settings)

    @property
    def key(self) -> tuple[str, str]:
        return self.source, self.kind


def sniff(data: bytes) -> Optional[str]:
    """The type of a picture in one of the formats a phone's browser shows as it is (PNG, JPEG,
    GIF, WebP) by its first bytes; None for anything else (SVG, which can run script, too)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def picture_size(data: bytes) -> Optional[tuple[int, int]]:
    """A picture's width and height from its header alone (nothing decoded): PNG, GIF, WebP,
    and a JPEG's frame within its first 64 KB; None when not found."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR" and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _webp_size(data)
        if data.startswith(b"\xff\xd8"):
            return _jpeg_size(data[:_JPEG_SCAN])
    except IndexError:
        return None
    return None


def _webp_size(data: bytes) -> Optional[tuple[int, int]]:
    kind, body = data[12:16], data[20:]
    if kind == b"VP8 " and body[3:6] == b"\x9d\x01\x2a" and len(body) >= 10:
        width = int.from_bytes(body[6:8], "little") & 0x3FFF
        return width, int.from_bytes(body[8:10], "little") & 0x3FFF
    if kind == b"VP8L" and body[:1] == b"\x2f" and len(body) >= 5:
        bits = int.from_bytes(body[1:5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X" and len(body) >= 10:
        width = int.from_bytes(body[4:7], "little") + 1
        return width, int.from_bytes(body[7:10], "little") + 1
    return None


def _jpeg_size(data: bytes) -> Optional[tuple[int, int]]:
    at = 2
    while at + 4 <= len(data):
        if data[at] != 0xFF:
            return None  # not a marker where one belongs
        marker = data[at + 1]
        if marker == 0xFF:  # a fill byte
            at += 1
            continue
        if marker in _BARE:
            at += 2
            continue
        if marker in (0xD9, 0xDA):  # the end, or the picture's data, before any frame
            return None
        length = int.from_bytes(data[at + 2 : at + 4], "big")
        if marker in _SOF and at + 9 <= len(data):
            height = int.from_bytes(data[at + 5 : at + 7], "big")
            return int.from_bytes(data[at + 7 : at + 9], "big"), height
        if length < 2:
            return None
        at += 2 + length
    return None


def _small(data: bytes) -> bool:
    """Whether a picture is known to be no more than MAX_SIDE pixels a side."""
    size = picture_size(data)
    return size is not None and 0 < size[0] <= MAX_SIDE and 0 < size[1] <= MAX_SIDE


def whole(data: bytes) -> bool:
    """Whether a picture of the web's formats ends where its format says it ends (a PNG's IEND,
    a JPEG's EOI, a GIF's trailer, a WebP's own length): not one cut off on the way."""
    kind = sniff(data[:_HEAD])
    if kind == "image/png":
        return data[-8:-4] == b"IEND"
    if kind == "image/jpeg":
        return b"\xff\xd9" in data[-64:]  # (some writers add a little after it)
    if kind == "image/gif":
        return data.rstrip(b"\x00").endswith(b";")
    if kind == "image/webp":
        return len(data) >= 8 + int.from_bytes(data[4:8], "little")
    return False


def _a_page(head: bytes) -> bool:
    """A refusal's or an error's page an image host answered instead of a picture."""
    return head.lstrip().lower().startswith(_PAGES)


class _Session:
    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.tokens: OrderedDict[tuple[str, str], str] = OrderedDict()


class PictureShelf:
    """The tokens of the pictures QiTV handed phones at home, by session. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._pictures: dict[str, tuple[str, Picture]] = {}  # token → (session, picture)

    def open(self, sid: str, peer: str) -> None:
        """The home door's session `sid` is open, from the address `peer`: it may have tokens."""
        with self._lock:
            self._drop(sid)
            self._sessions[sid] = _Session(peer)

    def close(self, sid: str) -> None:
        """Session `sid` ended (or never had pictures): its tokens answer nothing from now."""
        with self._lock:
            self._drop(sid)

    def _drop(self, sid: str) -> None:
        session = self._sessions.pop(sid, None)
        if session is not None:
            for token in session.tokens.values():
                self._pictures.pop(token, None)

    def issue(self, sid: str, source: str, kind: str, verify: bool = True) -> str:
        """The token of the picture at `source` for session `sid` (the same while it lasts);
        "" when that session may have none (not the home door's, or ended)."""
        if not isinstance(source, str) or not source or kind not in KINDS:
            return ""
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return ""
            key = (source, kind)
            token = session.tokens.get(key)
            if token is not None:
                session.tokens.move_to_end(key)
                return token
            token = secrets.token_urlsafe(16)
            session.tokens[key] = token
            self._pictures[token] = (sid, Picture(source, kind, bool(verify)))
            while len(session.tokens) > SESSION_PICTURES:
                self._pictures.pop(session.tokens.popitem(last=False)[1], None)
            return token

    def get(self, token: str, peer: str) -> Optional[Picture]:
        """The picture `token` stands for, asked from `peer`: None unless its session is open
        and was opened from that address."""
        with self._lock:
            found = self._pictures.get(token)
            if found is None:
                return None
            sid, picture = found
            session = self._sessions.get(sid)
            if session is None or session.peer != peer:
                return None
            return picture


class _Fetcher:
    """A thread of its own with an asyncio loop: the pictures' downloads into QiTV's image cache
    (`images`, an ImageManager) and the reads of its files."""

    def __init__(self, images: Any) -> None:
        self._images = images
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sessions: dict[bool, Any] = {}  # aiohttp sessions, by TLS check (this loop's)
        self._limit: Optional[asyncio.Semaphore] = None
        self._stopped = False

    def load(self, picture: Picture) -> concurrent.futures.Future:
        """("as_is", bytes, type), ("scale", b"", "") or None, on this fetcher's thread."""
        loop = self._started()
        if loop is None:  # the hub stops: nothing more is fetched
            done: concurrent.futures.Future = concurrent.futures.Future()
            done.set_result(None)
            return done
        return asyncio.run_coroutine_threadsafe(self._load(picture), loop)

    def _started(self) -> Optional[asyncio.AbstractEventLoop]:
        with self._lock:
            if self._loop is None and not self._stopped:
                loop = asyncio.new_event_loop()
                threading.Thread(
                    target=self._run, args=(loop,), name="qitv-pictures", daemon=True
                ).start()
                self._loop = loop
            return self._loop

    def _run(self, loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(self._drain())  # what began as it stopped, too
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    async def _load(self, picture: Picture) -> Optional[tuple[str, bytes, str]]:
        if self._limit is None:
            self._limit = asyncio.Semaphore(FETCHES)
        async with self._limit:  # FETCH_S counts from here: the wait for a turn isn't its
            for again in (False, True):
                try:
                    path = await asyncio.wait_for(self._cached(picture), FETCH_S)
                except TimeoutError:
                    logger.info("Phone remote: a picture's download took too long")
                    return None
                if not path:
                    return None
                read = self._read(path, last=again)
                if read != _MISS:
                    return read
        return None  # (never reached: the second read is never a miss)

    async def _cached(self, picture: Picture) -> Optional[str]:
        iconified = picture.kind == LOGO  # as the TV's LogoCache and PosterCache keep them
        source = picture.source
        if source.startswith("data:image"):
            path = await self._images.cache_image_from_base64(source, iconified)
        elif source.lower().startswith(("http://", "https://")):
            session = self._session(picture.verify)
            if session is None:
                return None
            path = await self._images.cache_image_from_url(session, source, iconified)
        else:
            return None  # a file, a share: nothing QiTV downloads
        return path if isinstance(path, str) and path else None

    def _session(self, verify: bool) -> Any:
        if self._stopped:
            return None  # none made that the stop's close would miss
        session = self._sessions.get(verify)
        if session is None or session.closed:
            from aiohttp import ClientSession, TCPConnector

            from services.provider_api import CLIENT_HEADERS

            # As the TV's ImageLoader asks (a public image host refuses a library's agent).
            connector = TCPConnector(ssl=verify, limit=FETCHES)
            session = ClientSession(connector=connector, headers=CLIENT_HEADERS)
            self._sessions[verify] = session
        return session

    def _read(self, path: str, last: bool) -> Optional[tuple[str, bytes, str]]:
        """The cached file at `path`: as it is, to be scaled, None, or a miss (_MISS: gone, or
        cut off) to download again unless this is the `last` try."""
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as file:
                head = file.read(_HEAD)
                kind = sniff(head)
                data = head + file.read(AS_IS_MAX) if kind and size <= AS_IS_MAX else b""
        except FileNotFoundError:  # evicted meanwhile: not in the cache after all
            return None if last else _MISS
        except OSError as error:
            logger.info(
                "Phone remote: a cached picture couldn't be read (%s)", type(error).__name__
            )
            return None
        if kind is not None and data and not whole(data) and not last:
            self._discard(path)  # cut off (an older QiTV's write): downloaded again
            return _MISS
        if kind is not None and data and whole(data) and _small(data):
            return "as_is", data, kind
        if not head:
            self._discard(path)
            return None if last else _MISS
        if _a_page(head):
            self._discard(path)  # asked for again next time, by the TV too
            return None
        # Too large, a format the page isn't sent as it is, or cut off by its host each time
        # (the TV reads it as far as it goes): scaled.
        return "scale", b"", ""

    def _discard(self, path: str) -> None:
        discard = getattr(self._images, "discard", None)
        if callable(discard):
            discard(path)

    async def _drain(self) -> None:
        """On this fetcher's loop: every download called off (begun or waiting its turn), then
        its sessions closed."""
        current = asyncio.current_task()
        for _ in range(3):  # those begun while the others ended, too
            others = [task for task in asyncio.all_tasks() if task is not current]
            if not others:
                break
            for task in others:
                task.cancel()
            await asyncio.gather(*others, return_exceptions=True)
        sessions, self._sessions = list(self._sessions.values()), {}
        for session in sessions:
            try:
                await session.close()
            except Exception as error:
                logger.debug("Phone remote: a pictures session's close: %s", error)

    def stop(self) -> None:
        """Its downloads are called off, its sessions close and its loop ends, on its own thread:
        not waited for (the hub's stop is short)."""
        with self._lock:
            loop, self._loop = self._loop, None
            self._stopped = True
        if loop is None:
            return

        async def close() -> None:
            await self._drain()
            asyncio.get_running_loop().stop()

        try:
            asyncio.run_coroutine_threadsafe(close(), loop)
        except RuntimeError:  # its loop closed meanwhile
            pass


class PictureServer:
    """On the hub's loop: a phone's ask for a picture, answered from what was kept, else from
    QiTV's image cache (on the fetcher's thread), else as QiTV's GUI scales it."""

    def __init__(self, shelf: PictureShelf, images: Any, emit: Callable[[object], None]) -> None:
        self.shelf = shelf
        self._fetcher = _Fetcher(images) if images is not None else None
        self._emit = emit
        self._kept: OrderedDict[tuple[str, str], Found] = OrderedDict()
        self._kept_bytes = 0
        self._loading: dict[tuple[str, str], asyncio.Future] = {}
        self._failed: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._scaling: dict[tuple[str, str], asyncio.Future] = {}

    async def answer(self, token: str, peer: str) -> Optional[Found]:
        """The picture `token` names, for a phone at `peer`; None when there is none for it (an
        unknown token, another session's or an ended one's: nothing is fetched for those)."""
        picture = self.shelf.get(token, peer)
        if picture is None:
            return None
        key = picture.key
        kept = self._kept.get(key)
        if kept is not None:
            self._kept.move_to_end(key)
            return kept
        failed = self._failed.get(key)
        if failed is not None and time.monotonic() - failed < FAILED_S:
            return None
        loading = self._loading.get(key)
        if loading is None:
            loading = asyncio.ensure_future(self._load(picture))
            self._loading[key] = loading
            loading.add_done_callback(partial(self._loaded, key))
        return await asyncio.shield(loading)  # a phone that stops asking leaves it to others

    def _loaded(self, key: tuple[str, str], loading: asyncio.Future) -> None:
        if self._loading.get(key) is loading:
            del self._loading[key]

    async def _load(self, picture: Picture) -> Optional[Found]:
        found: Optional[Found] = None
        read = None
        if self._fetcher is not None:
            try:  # bounded there, once its turn came (FETCH_S)
                read = await asyncio.wrap_future(self._fetcher.load(picture))
            except Exception as error:  # the image cache's own fault
                logger.info("Phone remote: a picture couldn't be had (%s)", type(error).__name__)
        if read is not None and read[0] == "as_is":
            found = (read[1], read[2])
        elif read is not None:
            found = await self._scaled(picture)
        if found is None:
            self._failed[picture.key] = time.monotonic()
            self._failed.move_to_end(picture.key)
            while len(self._failed) > FAILED_COUNT:
                self._failed.popitem(last=False)
            return None
        self._keep(picture.key, found)
        return found

    async def _scaled(self, picture: Picture) -> Optional[Found]:
        """QiTV's GUI's copy of `picture`, scaled from the TV's own decoded one; None when it
        doesn't come in SCALE_S (no GUI, a picture the TV can't read either)."""
        future = asyncio.get_running_loop().create_future()
        self._scaling[picture.key] = future
        self._emit(PictureWanted(picture.source, picture.kind))
        try:
            return await asyncio.wait_for(future, SCALE_S)
        except asyncio.TimeoutError:
            return None
        finally:
            if self._scaling.get(picture.key) is future:
                del self._scaling[picture.key]

    def put(self, source: str, kind: str, data: bytes) -> None:
        """QiTV's GUI's scaled copy of a picture asked for (PictureWanted): served if it is one
        of the web's formats, not too large. One that came after SCALE_S is the next answer."""
        key = (source, kind)
        copy = _scaled_copy(data)
        future = self._scaling.pop(key, None)
        if future is not None and not future.done():
            future.set_result(copy)
        elif copy is not None and key in self._failed:  # late (the TV's cache was busy)
            del self._failed[key]
            self._keep(key, copy)

    def _keep(self, key: tuple[str, str], found: Found) -> None:
        old = self._kept.pop(key, None)
        if old is not None:
            self._kept_bytes -= len(old[0])
        self._kept[key] = found
        self._kept_bytes += len(found[0])
        while len(self._kept) > 1 and (
            self._kept_bytes > KEPT_BYTES or len(self._kept) > KEPT_COUNT
        ):
            self._kept_bytes -= len(self._kept.popitem(last=False)[1][0])

    async def stop(self) -> None:
        """Every picture being had is called off, the fetcher's downloads with them."""
        loading = list(self._loading.values())
        for task in loading:
            task.cancel()
        for future in self._scaling.values():
            if not future.done():
                future.cancel()
        self._scaling.clear()
        if loading:
            await asyncio.wait(loading, timeout=0.5)
        if self._fetcher is not None:
            self._fetcher.stop()


def _scaled_copy(data: object) -> Optional[Found]:
    """QiTV's scaled copy, as it is served: one of the web's formats, small in bytes and in
    pixels; None else."""
    if not isinstance(data, bytes) or len(data) > SCALED_MAX:
        return None
    kind = sniff(data[:_HEAD])
    return (data, kind) if kind is not None and whole(data) and _small(data) else None


def add_routes(app: Any, server: PictureServer) -> None:
    """The pictures' route on the home door's aiohttp app (before its page's catch-all)."""
    from aiohttp import web

    async def picture(request: web.Request) -> web.StreamResponse:
        found = await server.answer(request.match_info["token"], request.remote or "")
        if found is None:  # never kept by the phone: its next ask reaches QiTV
            raise web.HTTPNotFound(text="404: Not Found", headers={"Cache-Control": "no-store"})
        data, kind = found
        return web.Response(body=data, content_type=kind, headers=ANSWER_HEADERS)

    app.router.add_get("/img/{token}", picture)
