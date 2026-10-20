"""The phone remote's network side: one asyncio loop in its own thread.

The loop runs the home door (services/remote/server.py) and, when the relay is on, the relay
client; every phone connection is a Session. Events go to `emit` on the loop's thread as plain
objects (services/remote/protocol.py); the GUI marshals them to its own thread. RemoteHub's
methods are safe to call from the GUI thread: they only schedule work on the loop and never wait
on the network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Iterable, Optional

from services.remote import net
from services.remote.pairing import RemoteStore
from services.remote.protocol import (
    CODE_ROTATE_S,
    HANDSHAKE_S,
    MAX_FRAME,
    MAX_PENDING,
    MAX_SESSIONS,
    PORTS,
    Code,
    Paired,
    Status,
    StoreSaved,
    b64e,
)
from services.remote.session import Reply, Session

logger = logging.getLogger(__name__)

Emit = Callable[[object], None]
_STOP_S = 2.0


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
        self.hello = hello
        self.handshake_s = handshake_s
        self.store = RemoteStore()
        self.port = 0
        self.conns: dict[str, _Conn] = {}
        self._runner: Any = None
        self._rotation: Optional[asyncio.TimerHandle] = None
        self._relay_state = "off"
        self._started = False
        self._early: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []

    # ---- lifecycle -------------------------------------------------------------------------

    async def start(self, config: dict) -> None:
        from services.remote.server import start_home_door

        self.store = RemoteStore.from_config(config)
        self.store.ensure_identity()
        self._runner, self.port = await start_home_door(self, self.web_dir, self.ports, self.host)
        logger.info("Phone remote listening on port %s", self.port)
        self._started = True
        self._status()
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
        self.store.relay, self.store.relay_url = fresh.relay, fresh.relay_url
        self._status()

    async def stop(self) -> None:
        self.set_pairing(False)
        for conn in list(self.conns.values()):
            if conn.session.established:
                await _quietly(conn.send(conn.session.bye("stopped")))
            await _quietly(conn.close(1001, "stopped"))
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ---- addresses and the pairing code ----------------------------------------------------

    def home_url(self) -> str:
        ip = net.home_ipv4()
        return f"http://{ip}:{self.port}/" if ip and self.port else ""

    def code_url(self) -> str:
        code = self.store.current_code()
        if code is None:
            return ""
        tv_pub = b64e(self.store.identity_pub())
        if self.store.relay and self.store.room:
            return f"{self.store.relay_url}/#1.{self.store.room}.{tv_pub}.{b64e(code)}"
        home = self.home_url() or f"http://127.0.0.1:{self.port}/"
        return f"{home}#1.-.{tv_pub}.{b64e(code)}"

    def _status(self) -> None:
        self.emit(Status(self.home_url(), self._relay_state if self.store.relay else "off"))

    def set_relay_state(self, state: str) -> None:
        self._relay_state = state
        self._status()

    def set_pairing(self, open_: bool) -> None:
        if self._rotation is not None:
            self._rotation.cancel()
            self._rotation = None
        if open_:
            self.store.open_pairing(self.clock())
            self._schedule_rotation()
        else:
            self.store.close_pairing()
        self.emit(Code(self.code_url()))

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

    async def deliver(self, conn: _Conn, reply: Reply) -> None:
        """Write a session's reply out and pass its events on."""
        for frame in reply.frames:
            await conn.send(frame)
        for event in reply.events:
            self.emit(event)
            if isinstance(event, Paired) and self.store.current_code() is not None:
                self.emit(Code(self.code_url()))  # the code was burnt: show the next one
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
        for conn in list(self.conns.values()):
            if conn.session.device_id == device_id and conn.session.established:
                asyncio.ensure_future(self._bye(conn, "removed", 4403))

    async def _bye(self, conn: _Conn, reason: str, code: int) -> None:
        await _quietly(conn.send(conn.session.bye(reason)))
        await _quietly(conn.close(code, reason))

    def send(self, sid: str, message: dict) -> None:
        conn = self.conns.get(sid)
        if conn is not None and conn.session.established:
            asyncio.ensure_future(_quietly(conn.send(conn.session.send(message))))

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
                await self.deliver(conn, conn.session.receive(frame))
                if ws.closed:
                    break
        finally:
            self.unregister(conn)
        return ws


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
        hello: Callable[[], dict] = dict,
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
