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
import time
from typing import TYPE_CHECKING, Any, Optional

from services.remote import net
from services.remote.protocol import MAX_FRAME, Code, StoreSaved, b64e

if TYPE_CHECKING:
    from services.remote.hub import HubCore, _Conn

logger = logging.getLogger(__name__)

MIN_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
STABLE_S = 60.0  # a connection up this long counts as working: the backoff starts over
RESTING_S = 600.0  # the relay is over its daily limit
REPLACED_S = 300.0  # another QiTV took this room (a copied config): don't fight over it
MAX_RETRY_AFTER_S = 300
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


def next_backoff(backoff: float, uptime_s: float) -> float:
    """The wait after the next one: doubling, from the start again after a stable connection."""
    base = MIN_BACKOFF_S if uptime_s >= STABLE_S else backoff
    return min(base * 2, MAX_BACKOFF_S)


def _retry_after(error: Any) -> Optional[int]:
    """A short Retry-After on a refusal (the relay's own per-address limit), in seconds."""
    headers = getattr(error, "headers", None) or {}
    try:
        value = int(headers.get("Retry-After", ""))
    except TypeError, ValueError:
        return None
    return value if 0 <= value <= MAX_RETRY_AFTER_S else None


class RelayClient:
    def __init__(self, core: HubCore, base_url: str) -> None:
        self.core = core
        self.base = base_url.rstrip("/")
        self._task: Optional[asyncio.Task] = None
        self._ws: Any = None
        self._conns: dict[int, _Conn] = {}
        # The relay compares the TV's IPv4 with the phone's; IPv6-only networks fall back to any.
        self._family: socket.AddressFamily = socket.AF_INET
        self._up_since = 0.0

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
            self._up_since = 0.0
            try:
                async with aiohttp.ClientSession(connector=connector) as http:
                    store = self.core.store
                    if not (store.room and store.room_secret):
                        await self._mint(http)
                    if await self._serve(http) == "replaced":
                        logger.warning("Phone remote: another QiTV uses this relay room")
                        self.core.set_relay_state("replaced")
                        await asyncio.sleep(REPLACED_S)
                        continue
            except _Resting:
                self.core.set_relay_state("resting")
                await asyncio.sleep(RESTING_S)
                continue
            except aiohttp.WSServerHandshakeError as error:
                pause = _retry_after(error) if error.status == 429 else None
                if pause is not None:  # the relay's per-address limit: a short pause
                    await asyncio.sleep(pause)
                    continue
                if error.status in _RESTING_CODES:
                    self.core.set_relay_state("resting")
                    await asyncio.sleep(RESTING_S)
                    continue
                if error.status in (401, 403, 404):
                    logger.info("Phone remote: the relay doesn't know this room; a new one")
                    self._forget_room()
            except aiohttp.ClientConnectorError as error:
                logger.info("Phone remote: the relay is out of reach: %s", error)
                fallback = socket.AF_UNSPEC if self._family == socket.AF_INET else socket.AF_INET
                self._family = fallback
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError) as error:
                logger.info("Phone remote: the relay connection failed: %s", error)
            except Exception as error:  # nothing may end the loop but stop()
                logger.error("Phone remote: the relay client failed: %s", error, exc_info=True)
            self._drop_all()
            self.core.set_relay_state("connecting")
            uptime = time.monotonic() - self._up_since if self._up_since else 0.0
            await asyncio.sleep(MIN_BACKOFF_S if uptime >= STABLE_S else backoff)
            backoff = next_backoff(backoff, uptime)

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

    async def _serve(self, http: Any) -> str:
        """Serve one connection to the room until it ends: "replaced" when another QiTV took the
        room, else "closed"."""
        from aiohttp import WSMsgType

        store = self.core.store
        url = f"{_socket_url(self.base)}/room/{store.room}?role=tv"
        headers = {"Authorization": f"Bearer {store.room_secret}"}
        ws = await http.ws_connect(
            url, headers=headers, heartbeat=HEARTBEAT_S, max_msg_size=MAX_FRAME * 2
        )
        self._ws = ws
        self._up_since = time.monotonic()
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
        return "replaced" if ws.close_code == 4000 else "closed"

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
                await self.core.deliver(existing, self.core.receive(existing, frame))

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
