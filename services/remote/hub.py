"""The phone remote's network side: one asyncio loop in its own thread.

The loop runs the home door (services/remote/server.py) and, when the relay is on, the relay
client; every phone connection is a Session. Events go to `emit` on the loop's thread as plain
objects (services/remote/protocol.py); the GUI marshals them to its own thread. RemoteHub's
methods are safe to call from the GUI thread: they only schedule work on the loop and never wait
on the network. The home door also hands phones the MKV films they play as HLS
(services/remote/films.py), read from their providers on this loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
import logging
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Iterable, Optional

from services.redact import safe_exc
from services.remote import net
from services.remote.films import OPEN_S, PROVIDER, Film, FilmRefused, FilmShelf, HttpFetcher
from services.remote.page_id import page_id
from services.remote.page_strings import page_hello
from services.remote.pairing import RemoteStore
from services.remote.protocol import (
    CODE_ROTATE_S,
    HANDSHAKE_S,
    MAX_FRAME,
    MAX_PENDING,
    MAX_SESSIONS,
    PORTS,
    Code,
    Forgot,
    Paired,
    Status,
    StoreSaved,
    b64e,
)
from services.remote.session import MessageTooBig, Reply, Session

logger = logging.getLogger(__name__)

Emit = Callable[[object], None]
_STOP_S = 2.0
_GOODBYE_S = 0.3  # a goodbye to a phone that doesn't answer stops waiting after this
SHELF_READS = 4  # films read from their providers at once (each film reads once at a time)
# With the relay on, the pairing screen waits this long for it before showing the home door's
# address: pairing through the relay keeps the phone's keys at the address it keeps (its icon).
RELAY_WAIT_S = 8.0


def web_dir() -> Path:
    """The phone page's files: beside the sources, or in a PyInstaller bundle's data."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "remote_web"
    return Path(__file__).resolve().parent / "web"


def default_tv_name() -> str:
    name = socket.gethostname().split(".")[0].replace("-", " ").strip()
    return name or "QiTV"


@dataclass
class _Conn:
    session: Session
    send: Callable[[bytes | str], Awaitable[None]]
    close: Callable[[int, str], Awaitable[None]]


class HubCore:
    """Lives on the hub's loop; nothing here may be touched from another thread."""

    def __init__(
        self,
        emit: Emit,
        web_dir: Path,
        ports: Iterable[int],
        *,
        clock: Callable[[], float],
        tv_name: str,
        hello: Callable[[], dict],
        handshake_s: float,
        host: str,
    ) -> None:
        self.emit = emit
        self.host = host
        self.web_dir = web_dir
        self.ports = list(ports)
        self.clock = clock
        self.tv_name = tv_name
        self._page_hello = hello
        self._page_id: Optional[str] = (
            None  # the page's files, read on this thread when first asked
        )
        self.handshake_s = handshake_s
        self.store = RemoteStore()
        self.port = 0
        self.conns: dict[str, _Conn] = {}
        self._runner: Any = None
        self._rotation: Optional[asyncio.TimerHandle] = None
        self._relay_state = "off"
        self._started = False
        self._relay_wait_until = 0.0  # monotonic; the pairing screen waits for the relay till then
        self.relay: Any = None  # a RelayClient while the relay is on
        self._early: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
        self.films = FilmShelf()
        self._http: Any = None  # the films' aiohttp session, made when first needed
        self._openings: dict[str, asyncio.Task] = {}  # the films being opened, by phone

    # ---- lifecycle -------------------------------------------------------------------------

    async def start(self, config: dict) -> None:
        from services.remote.server import start_home_door

        self.store = RemoteStore.from_config(config)
        self.store.ensure_identity()
        self._runner, self.port = await start_home_door(self, self.web_dir, self.ports, self.host)
        logger.info("Phone remote listening on port %s", self.port)
        self._started = True
        self._status()
        await self._sync_relay()
        early, self._early = self._early, []
        for fn, args in early:
            fn(*args)

    def when_started(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run `fn` now, or once start() has loaded the store (calls made meanwhile wait)."""
        if self._started:
            fn(*args)
        else:
            self._early.append((fn, args))

    def apply(self, config: dict) -> None:
        fresh = RemoteStore.from_config(config)
        if fresh.relay_url != self.store.relay_url:
            self.store.room = self.store.room_secret = None  # that relay's room isn't this one's
        self.store.relay, self.store.relay_url = fresh.relay, fresh.relay_url
        self._status()
        asyncio.ensure_future(self._sync_relay())

    async def _sync_relay(self) -> None:
        """A relay client while the relay is on, for the relay address now set."""
        from services.remote.relay_client import RelayClient

        want = self.store.relay_url if self.store.relay else None
        if self.relay is not None and self.relay.base != (want or "").rstrip("/"):
            await self.relay.stop()
            self.relay = None
            self._relay_state = "off"
        if want and self.relay is None:
            self.relay = RelayClient(self, want)
            self.relay.start()
        self._status()
        if self.store.current_code() is not None:
            self.emit(Code(self.code_url()))

    async def stop(self) -> None:
        self.set_pairing(False)
        if self.relay is not None:
            await self.relay.stop()
            self.relay = None
        for conn in list(self.conns.values()):
            if conn.session.established:
                await _briefly(conn.send(conn.session.bye("stopped")))
            await _briefly(conn.close(1001, "stopped"))
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self.drop_films()
        if self._http is not None:
            await _quietly(self._http.close())
            self._http = None

    # ---- films -----------------------------------------------------------------------------

    def open_film(
        self, sid: str, device: str, url: str, headers: dict, verify: bool, message: dict
    ) -> None:
        """Open the MKV film at `url` for phone `sid` and send it `message` with the film's HLS
        address as its link, or, if a phone can't play it, a failed play saying why. The phone's
        film still opening, if any, is called off: the newest choice wins."""
        self.forget_film(device)
        task = asyncio.ensure_future(self._open_film(sid, device, url, headers, verify, message))
        self._openings[device] = task
        task.add_done_callback(partial(self._opened, device))

    def _opened(self, device: str, task: asyncio.Task) -> None:
        if self._openings.get(device) is task:
            del self._openings[device]

    async def _open_film(
        self, sid: str, device: str, url: str, headers: dict, verify: bool, message: dict
    ) -> None:
        home = self.home_url()
        film = Film(HttpFetcher(self._film_http(), url, headers, verify))
        try:
            if not home:
                raise FilmRefused(PROVIDER, "no home address")
            await asyncio.wait_for(film.open(), OPEN_S)
        except FilmRefused as refused:
            logger.info("Phone remote: a film can't play on the phone: %s", refused)
            self.send(sid, {"t": "play", "failed": True, "why": refused.reason})
            return
        except Exception as error:  # the provider's: unreachable, slow, refused
            logger.info("Phone remote: a film couldn't open: %s", safe_exc(error))
            self.send(sid, {"t": "play", "failed": True, "why": PROVIDER})
            return
        token = self.films.put(device, film)
        self.send(sid, {**message, "url": f"{home}film/{token}/index.m3u8"})

    def forget_film(self, device: str) -> None:
        """Phone `device` plays something else (or hands its film to the TV): its film, playing
        or still opening, goes, and stops reading its provider."""
        opening = self._openings.pop(device, None)
        if opening is not None:
            opening.cancel()
        self.films.drop(device)

    def drop_films(self) -> None:
        """The TV plays again: the films phones played or were opening go (one screen at a
        time)."""
        for opening in self._openings.values():
            opening.cancel()
        self._openings.clear()
        self.films.clear()

    def _film_http(self) -> Any:
        if self._http is None:
            import ssl

            from aiohttp import ClientSession, ClientTimeout, TCPConnector
            import certifi

            context = ssl.create_default_context(cafile=certifi.where())
            # No connection kept open to a provider between reads: it may count them.
            connector = TCPConnector(force_close=True, limit=SHELF_READS, ssl=context)
            timeout = ClientTimeout(sock_connect=10, sock_read=30)
            self._http = ClientSession(connector=connector, timeout=timeout)
        return self._http

    # ---- addresses and the pairing code ----------------------------------------------------

    def home_url(self) -> str:
        ip = net.home_ipv4()
        return f"http://{ip}:{self.port}/" if ip and self.port else ""

    def code_url(self) -> str:
        code = self.store.current_code()
        if code is None:
            return ""
        tv_pub = b64e(self.store.identity_pub())
        # The relay's address only while QiTV is in its room: a phone at home pairs either way.
        if self.store.relay and self.store.room and self._relay_state == "online":
            return f"{self.store.relay_url}/#1.{self.store.room}.{tv_pub}.{b64e(code)}"
        if self.store.relay and time.monotonic() < self._relay_wait_until:
            return ""  # "Starting…": the relay may answer in a moment
        home = self.home_url() or f"http://127.0.0.1:{self.port}/"
        return f"{home}#1.-.{tv_pub}.{b64e(code)}"

    def _status(self) -> None:
        self.emit(Status(self.home_url(), self._relay_state if self.store.relay else "off"))

    def set_relay_state(self, state: str) -> None:
        changed = state != self._relay_state
        self._relay_state = state
        self._status()
        if changed and self.store.current_code() is not None:
            self.emit(Code(self.code_url()))  # the QR follows: relay or home door

    def set_pairing(self, open_: bool) -> None:
        if self._rotation is not None:
            self._rotation.cancel()
            self._rotation = None
        if open_:
            self.store.open_pairing(self.clock())
            self._schedule_rotation()
            if self.store.relay and self._relay_state != "online":
                self._relay_wait_until = time.monotonic() + RELAY_WAIT_S
                asyncio.get_running_loop().call_later(RELAY_WAIT_S, self._relay_wait_over)
        else:
            self.store.close_pairing()
            self._relay_wait_until = 0.0
        self.emit(Code(self.code_url()))

    def _relay_wait_over(self) -> None:
        self._relay_wait_until = 0.0
        if self.store.current_code() is not None and self._relay_state != "online":
            self.emit(Code(self.code_url()))  # no relay: the home door's address after all

    def _schedule_rotation(self) -> None:
        loop = asyncio.get_running_loop()
        self._rotation = loop.call_later(CODE_ROTATE_S, self._rotate)

    def _rotate(self) -> None:
        self.store.rotate_code(self.clock())
        self.emit(Code(self.code_url()))
        self._schedule_rotation()

    # ---- phones ----------------------------------------------------------------------------

    def new_session(self, door: str, same_network: Optional[bool]) -> Session:
        return Session(
            self.store,
            sid=b64e(secrets.token_bytes(6)),
            door="relay" if door == "relay" else "home",
            home_url=self.home_url,
            same_network=same_network,
            clock=self.clock,
            tv_name=self.tv_name,
            hello=self.hello,
        )

    def hello(self) -> dict:
        """What the hello adds: the page's words, and which page this QiTV carries."""
        if self._page_id is None:
            try:
                self._page_id = page_id(self.web_dir)
            except OSError as error:
                logger.warning("Phone remote: couldn't read the page: %s", safe_exc(error))
                self._page_id = ""
        return {**self._page_hello(), "page": self._page_id}

    def capacity_left(self) -> bool:
        established = sum(1 for c in self.conns.values() if c.session.established)
        pending = len(self.conns) - established
        return established < MAX_SESSIONS and pending < MAX_PENDING

    def register(self, conn: _Conn) -> None:
        self.conns[conn.session.sid] = conn
        asyncio.get_running_loop().call_later(self.handshake_s, self._check_handshake, conn)

    def _check_handshake(self, conn: _Conn) -> None:
        if conn.session.sid in self.conns and not conn.session.established:
            asyncio.ensure_future(_quietly(conn.close(4408, "too slow")))

    def receive(self, conn: _Conn, frame: bytes | str) -> Reply:
        """A phone's frame into its session; a fault in there closes that phone, nothing else."""
        try:
            return conn.session.receive(frame)
        except Exception as error:
            logger.error("Phone remote: a frame broke its session: %s", error, exc_info=True)
            return Reply(close=(4400, "error"))

    async def deliver(self, conn: _Conn, reply: Reply) -> None:
        """Write a session's reply out and pass its events on."""
        for frame in reply.frames:
            await conn.send(frame)
        for event in reply.events:
            self.emit(event)
            if isinstance(event, Paired) and self.store.current_code() is not None:
                self.emit(Code(self.code_url()))  # the code was burnt: show the next one
            if isinstance(event, Forgot):
                self._end_device(event.device_id, but=conn)
        if reply.close is not None:
            await _quietly(conn.close(*reply.close))

    def unregister(self, conn: _Conn) -> None:
        if self.conns.pop(conn.session.sid, None) is not None and conn.session.device_id:
            if conn.session.established:
                self.emit(conn.session.closed_event())

    def remove_device(self, device_id: str) -> None:
        if not self.store.remove_device(device_id):
            return
        self.emit(StoreSaved(self.store.to_config()))
        self._end_device(device_id)

    def _end_device(self, device_id: str, but: Optional[_Conn] = None) -> None:
        """Close every connection of a device that is gone: established ones with a goodbye,
        those still shaking hands with 4401 (their handshake would fail anyway)."""
        for conn in list(self.conns.values()):
            if conn is but or conn.session.device_id != device_id:
                continue
            if conn.session.established:
                asyncio.ensure_future(self._bye(conn, "removed", 4403))
            else:
                asyncio.ensure_future(_quietly(conn.close(4401, "unknown")))

    async def _bye(self, conn: _Conn, reason: str, code: int) -> None:
        await _quietly(conn.send(conn.session.bye(reason)))
        await _quietly(conn.close(code, reason))

    def send(self, sid: str, message: dict) -> None:
        conn = self.conns.get(sid)
        if conn is not None and conn.session.established:
            asyncio.ensure_future(self._seal_and_send(conn, message))

    async def _seal_and_send(self, conn: _Conn, message: dict) -> None:
        # Sealed when written, so the phone gets messages in nonce order whatever else is sent.
        if not conn.session.established:
            return
        try:
            frame = conn.session.send(message)
        except MessageTooBig as error:  # never sent: a frame that big would cost every phone
            logger.warning("Phone remote: a message too big to send was dropped (%s)", error)
            return
        await _quietly(conn.send(frame))

    def broadcast(self, message: dict) -> None:
        for sid in list(self.conns):
            self.send(sid, message)

    # ---- the home door's WebSocket ---------------------------------------------------------

    async def handle_ws(self, request: Any) -> Any:
        from aiohttp import WSMsgType, web

        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME * 2, heartbeat=10.0)
        await ws.prepare(request)
        if not self.capacity_left():
            await ws.close(code=4429, message=b"busy")
            return ws

        async def send(frame: bytes | str) -> None:
            if isinstance(frame, str):
                await ws.send_str(frame)
            else:
                await ws.send_bytes(frame)

        async def close(code: int, reason: str) -> None:
            await ws.close(code=code, message=reason.encode("utf-8"))

        conn = _Conn(self.new_session("home", None), send, close)
        self.register(conn)
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    frame: bytes | str = message.data
                elif message.type == WSMsgType.BINARY:
                    frame = message.data
                else:
                    break
                await self.deliver(conn, self.receive(conn, frame))
                if ws.closed:
                    break
        finally:
            self.unregister(conn)
        return ws


async def _briefly(awaitable: Awaitable[Any]) -> None:
    """Like _quietly, but gives up after _GOODBYE_S (a phone that never answers a close)."""
    try:
        await asyncio.wait_for(_quietly(awaitable), _GOODBYE_S)
    except asyncio.TimeoutError:
        pass


async def _quietly(awaitable: Awaitable[Any]) -> None:
    try:
        await awaitable
    except (ConnectionError, RuntimeError, OSError) as error:
        logger.debug("Phone remote: a send or close failed: %s", error)


class RemoteHub:
    """The GUI's handle on the remote's thread. Thread-safe; nothing here blocks."""

    def __init__(
        self,
        emit: Emit,
        web_dir: Path,
        *,
        ports: Iterable[int] = PORTS,
        clock: Callable[[], float] = time.time,
        tv_name: Optional[str] = None,
        hello: Callable[[], dict] = page_hello,
        handshake_s: float = HANDSHAKE_S,
        host: str = "0.0.0.0",
    ) -> None:
        self._emit = emit
        self._core = HubCore(
            emit,
            web_dir,
            ports,
            clock=clock,
            tv_name=tv_name or default_tv_name(),
            hello=hello,
            handshake_s=handshake_s,
            host=host,
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self.ready = threading.Event()
        self.port = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: dict) -> None:
        if self.running:
            self.apply(config)
            return
        self.ready.clear()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, args=(self._loop, dict(config)), name="qitv-remote", daemon=True
        )
        self._thread.start()

    def _run(self, loop: asyncio.AbstractEventLoop, config: dict) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._core.start(config))
            self.port = self._core.port
        except OSError as error:
            logger.warning("Phone remote could not start: %s", error)
            self._emit(Status("", "error"))
            loop.close()
            return
        finally:
            self.ready.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None or not thread.is_alive():
            return
        future = asyncio.run_coroutine_threadsafe(self._core.stop(), loop)
        try:
            future.result(_STOP_S)
        except Exception as error:  # a stuck close must not keep QiTV from quitting
            logger.debug("Phone remote stop: %s", error)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(_STOP_S)
        self._thread = None

    def _call(self, fn: Callable[..., Any], *args: Any) -> None:
        if self._loop is not None and self.running:
            self._loop.call_soon_threadsafe(self._core.when_started, fn, *args)

    def apply(self, config: dict) -> None:
        self._call(self._core.apply, dict(config))

    def set_pairing(self, open_: bool) -> None:
        self._call(self._core.set_pairing, open_)

    def remove_device(self, device_id: str) -> None:
        self._call(self._core.remove_device, device_id)

    def send(self, sid: str, message: dict) -> None:
        self._call(self._core.send, sid, message)

    def broadcast(self, message: dict) -> None:
        self._call(self._core.broadcast, message)

    def open_film(
        self, sid: str, device: str, url: str, headers: dict, verify: bool, message: dict
    ) -> None:
        self._call(self._core.open_film, sid, device, url, dict(headers), verify, dict(message))

    def forget_film(self, device: str) -> None:
        self._call(self._core.forget_film, device)

    def drop_films(self) -> None:
        self._call(self._core.drop_films)
