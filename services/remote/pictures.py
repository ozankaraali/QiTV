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
SCALED_MAX = 2 * 1024 * 1024  # QiTV's own scaled copy, at most
KEPT_BYTES = 16 * 1024 * 1024  # what was served, kept in memory for the next ask
KEPT_COUNT = 400
FETCHES = 4  # pictures downloaded at once
FETCH_S = 25.0  # a picture's download (the image cache tries twice) and read, at most
SCALE_S = 10.0  # QiTV's GUI scaling one, at most
FAILED_S = 120.0  # a picture that couldn't be had isn't tried again for this long
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

    @staticmethod
    def _run(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            loop.close()

    async def _load(self, picture: Picture) -> Optional[tuple[str, bytes, str]]:
        if self._limit is None:
            self._limit = asyncio.Semaphore(FETCHES)
        async with self._limit:
            path = await self._cached(picture)
        return self._read(path) if path else None

    async def _cached(self, picture: Picture) -> Optional[str]:
        iconified = picture.kind == LOGO  # as the TV's LogoCache and PosterCache keep them
        source = picture.source
        if source.startswith("data:image"):
            path = await self._images.cache_image_from_base64(source, iconified)
        elif source.lower().startswith(("http://", "https://")):
            session = self._session(picture.verify)
            path = await self._images.cache_image_from_url(session, source, iconified)
        else:
            return None  # a file, a share: nothing QiTV downloads
        return path if isinstance(path, str) and path else None

    def _session(self, verify: bool) -> Any:
        session = self._sessions.get(verify)
        if session is None or session.closed:
            from aiohttp import ClientSession, TCPConnector

            from services.provider_api import CLIENT_HEADERS

            # As the TV's ImageLoader asks (a public image host refuses a library's agent).
            connector = TCPConnector(ssl=verify, limit=FETCHES)
            session = ClientSession(connector=connector, headers=CLIENT_HEADERS)
            self._sessions[verify] = session
        return session

    def _read(self, path: str) -> Optional[tuple[str, bytes, str]]:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as file:
                head = file.read(_HEAD)
                kind = sniff(head)
                if kind is not None and size <= AS_IS_MAX:
                    return "as_is", head + file.read(AS_IS_MAX), kind
        except OSError as error:
            logger.info(
                "Phone remote: a cached picture couldn't be read (%s)", type(error).__name__
            )
            return None
        if not head or _a_page(head):
            discard = getattr(self._images, "discard", None)
            if callable(discard):
                discard(path)  # asked for again next time, by the TV too
            return None
        return "scale", b"", ""  # too large, or a format the page isn't sent as it is

    def stop(self) -> None:
        """Its sessions close and its loop ends, without waiting (the hub's stop is short)."""
        with self._lock:
            loop, self._loop = self._loop, None
            self._stopped = True
        if loop is None:
            return
        sessions, self._sessions = list(self._sessions.values()), {}

        async def close() -> None:
            for session in sessions:
                try:
                    await session.close()
                except Exception as error:
                    logger.debug("Phone remote: a pictures session's close: %s", error)
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
            try:
                read = await asyncio.wait_for(
                    asyncio.wrap_future(self._fetcher.load(picture)), FETCH_S
                )
            except Exception as error:  # too slow, or the image cache's own fault
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
        of the web's formats, and not too large."""
        future = self._scaling.pop((source, kind), None)
        if future is None or future.done():
            return
        kind_of = sniff(data[:_HEAD]) if isinstance(data, bytes) else None
        future.set_result((data, kind_of) if kind_of and len(data) <= SCALED_MAX else None)

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

    def stop(self) -> None:
        if self._fetcher is not None:
            self._fetcher.stop()
        for future in self._scaling.values():
            if not future.done():
                future.cancel()
        self._scaling.clear()


def add_routes(app: Any, server: PictureServer) -> None:
    """The pictures' route on the home door's aiohttp app (before its page's catch-all)."""
    from aiohttp import web

    async def picture(request: web.Request) -> web.StreamResponse:
        found = await server.answer(request.match_info["token"], request.remote or "")
        if found is None:
            raise web.HTTPNotFound(text="404: Not Found")
        data, kind = found
        return web.Response(body=data, content_type=kind, headers=ANSWER_HEADERS)

    app.router.add_get("/img/{token}", picture)
