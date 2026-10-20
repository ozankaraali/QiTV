"""QiTV's line to the phone remote's relay: one outgoing WebSocket into its room.

The relay (relay/) forwards each phone's frames, tagged with a 2-byte connection id:
conn ‖ kind ‖ payload, kind 0 binary, 3 text, 1 opened ({"same": bool}), 2 closed. Every phone
gets its own Session, as on the home door; the relay never reads what passes. The room id is
minted by the relay once (POST /rooms) and kept with a secret only this QiTV knows. Runs on the
remote hub's loop; reconnects with backoff, and rests 10 minutes when the relay says it is over
its daily limit.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import secrets
import socket
from typing import TYPE_CHECKING, Any, Optional

from services.remote import net
from services.remote.protocol import MAX_FRAME, Code, StoreSaved, b64e

if TYPE_CHECKING:
    from services.remote.hub import HubCore, _Conn

logger = logging.getLogger(__name__)

MIN_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
RESTING_S = 600.0
HEARTBEAT_S = 30.0  # protocol pings: free on the relay, and they keep the NAT open
BINARY, OPENED, CLOSED, TEXT = 0, 1, 2, 3
_RESTING_CODES = (429, 1027)


def _frame(conn: int, kind: int, payload: bytes = b"") -> bytes:
    return conn.to_bytes(2, "big") + bytes([kind]) + payload


def _socket_url(base: str) -> str:
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    return base


class _Resting(Exception):
    pass


class RelayClient:
    def __init__(self, core: HubCore, base_url: str) -> None:
        self.core = core
        self.base = base_url.rstrip("/")
        self._task: Optional[asyncio.Task] = None
        self._ws: Any = None
        self._conns: dict[int, _Conn] = {}
        # The relay compares the TV's IPv4 with the phone's; IPv6-only networks fall back to any.
        self._family: int = socket.AF_INET

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._drop_all()

    # ---- the loop --------------------------------------------------------------------------

    async def _run(self) -> None:
        import aiohttp

        backoff = MIN_BACKOFF_S
        while True:
            self.core.set_relay_state("connecting")
            connector = aiohttp.TCPConnector(family=self._family)
            try:
                async with aiohttp.ClientSession(connector=connector) as http:
                    store = self.core.store
                    if not (store.room and store.room_secret):
                        await self._mint(http)
                    if await self._serve(http):
                        backoff = MIN_BACKOFF_S
            except _Resting:
                self.core.set_relay_state("resting")
                await asyncio.sleep(RESTING_S)
                continue
            except aiohttp.WSServerHandshakeError as error:
                if error.status in _RESTING_CODES:
                    self.core.set_relay_state("resting")
                    await asyncio.sleep(RESTING_S)
                    continue
                if error.status in (401, 403, 404):
                    logger.info("Phone remote: the relay doesn't know this room; a new one")
                    self._forget_room()
            except aiohttp.ClientConnectorError as error:
                logger.info("Phone remote: the relay is out of reach: %s", error)
                self._family = 0 if self._family == socket.AF_INET else socket.AF_INET
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError) as error:
                logger.info("Phone remote: the relay connection failed: %s", error)
            self._drop_all()
            self.core.set_relay_state("connecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _mint(self, http: Any) -> None:
        async with http.post(f"{self.base}/rooms") as response:
            if response.status in _RESTING_CODES:
                raise _Resting()
            response.raise_for_status()
            data = await response.json(content_type=None)
        room = data.get("room") if isinstance(data, dict) else None
        if not isinstance(room, str) or not 0 < len(room) <= 64:
            raise ValueError("the relay gave no room")
        store = self.core.store
        store.room, store.room_secret = room, b64e(secrets.token_bytes(32))
        self.core.emit(StoreSaved(store.to_config()))
        if store.current_code() is not None:
            self.core.emit(Code(self.core.code_url()))  # the QR now opens the relay door

    def _forget_room(self) -> None:
        store = self.core.store
        store.room = store.room_secret = None
        self.core.emit(StoreSaved(store.to_config()))

    async def _serve(self, http: Any) -> bool:
        """Serve one connection to the room; True once it was up (the backoff starts over)."""
        from aiohttp import WSMsgType

        store = self.core.store
        url = f"{_socket_url(self.base)}/room/{store.room}?role=tv"
        headers = {"Authorization": f"Bearer {store.room_secret}"}
        ws = await http.ws_connect(
            url, headers=headers, heartbeat=HEARTBEAT_S, max_msg_size=MAX_FRAME * 2
        )
        self._ws = ws
        self.core.set_relay_state("online")
        try:
            await ws.send_str(json.dumps({"v6": net.ipv6_prefixes()}))
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    await self._on_frame(message.data)
                elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
        finally:
            self._ws = None
            self._drop_all()
        if ws.close_code in _RESTING_CODES:
            raise _Resting()
        return True

    # ---- frames ----------------------------------------------------------------------------

    async def _on_frame(self, data: bytes) -> None:
        if len(data) < 3:
            return
        conn, kind, payload = int.from_bytes(data[:2], "big"), data[2], bytes(data[3:])
        if kind == OPENED:
            await self._opened(conn, payload)
        elif kind == CLOSED:
            dropped = self._conns.pop(conn, None)
            if dropped is not None:
                self.core.unregister(dropped)
        elif kind in (BINARY, TEXT):
            existing = self._conns.get(conn)
            if existing is not None:
                frame: bytes | str = payload.decode("utf-8", "replace") if kind == TEXT else payload
                await self.core.deliver(existing, existing.session.receive(frame))

    async def _opened(self, conn: int, payload: bytes) -> None:
        from services.remote.hub import _Conn

        previous = self._conns.pop(conn, None)
        if previous is not None:
            self.core.unregister(previous)
        try:
            same = bool(json.loads(payload).get("same"))
        except ValueError, AttributeError:
            same = False
        if not self.core.capacity_left():
            await self._send(_frame(conn, CLOSED, (4429).to_bytes(2, "big") + b"busy"))
            return

        async def send(frame: bytes | str) -> None:
            if isinstance(frame, str):
                await self._send(_frame(conn, TEXT, frame.encode("utf-8")))
            else:
                await self._send(_frame(conn, BINARY, frame))

        async def close(code: int, reason: str) -> None:
            await self._send(_frame(conn, CLOSED, code.to_bytes(2, "big") + reason.encode()))

        entry = _Conn(self.core.new_session("relay", same), send, close)
        self._conns[conn] = entry
        self.core.register(entry)

    async def _send(self, data: bytes) -> None:
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.send_bytes(data)

    def _drop_all(self) -> None:
        conns, self._conns = self._conns, {}
        for entry in conns.values():
            self.core.unregister(entry)
