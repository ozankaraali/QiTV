"""QiTV's side of the relay (services/remote/relay_client.py) against a fake relay that speaks the
real one's framing (relay/src/room.js): conn (2 bytes) ‖ kind (1) ‖ payload."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aiohttp import WSMsgType, web
from remote_phone import PhoneClient
from test_remote_home_door import Recorder

from services.remote import relay_client
from services.remote.hub import RemoteHub
from services.remote.protocol import Closed, Code, Paired, Press, Status, StoreSaved, b64d

ROOM = "R" * 32
BINARY, OPENED, CLOSED, TEXT = 0, 1, 2, 3


def frame(conn: int, kind: int, payload: bytes = b"") -> bytes:
    return conn.to_bytes(2, "big") + bytes([kind]) + payload


class FakeRelay:
    """POST /rooms and GET /room/<id>?role=tv, on the test's own loop."""

    def __init__(self, refuse: int = 0) -> None:
        self.refuse = refuse
        self.posts = 0
        self.auth: list[str] = []
        self.texts: list[dict] = []
        self.tv: web.WebSocketResponse | None = None
        self.from_tv: asyncio.Queue = asyncio.Queue()
        self.connected = asyncio.Event()

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/rooms", self.rooms)
        app.router.add_get("/room/{id}", self.room)
        self.runner = web.AppRunner(app, shutdown_timeout=0.5)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self.tv is not None:
            await self.tv.close()
        await self.runner.cleanup()

    async def rooms(self, request):
        self.posts += 1
        return web.json_response({"room": ROOM})

    async def room(self, request):
        if self.refuse:
            return web.Response(status=self.refuse)
        self.auth.append(request.headers.get("Authorization", ""))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.tv = ws
        self.connected.set()
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                self.texts.append(json.loads(message.data))
            elif message.type == WSMsgType.BINARY:
                data = message.data
                await self.from_tv.put((int.from_bytes(data[:2], "big"), data[2], data[3:]))
        self.connected.clear()
        return ws

    async def to_tv(self, conn: int, kind: int, payload: bytes = b"") -> None:
        await self.tv.send_bytes(frame(conn, kind, payload))

    async def phone_says(self, conn: int, data) -> None:
        if isinstance(data, str):
            await self.to_tv(conn, TEXT, data.encode())
        else:
            await self.to_tv(conn, BINARY, data)

    async def tv_says(self, conn: int, timeout: float = 3.0) -> tuple[int, bytes]:
        while True:
            got, kind, payload = await asyncio.wait_for(self.from_tv.get(), timeout)
            if got == conn:
                return kind, payload


class RelayCase(unittest.TestCase):
    def setUp(self):
        self.web = Path(tempfile.mkdtemp())
        (self.web / "index.html").write_text("x")
        self.events = Recorder()
        patch = mock.patch.object(relay_client, "MIN_BACKOFF_S", 0.1)
        patch.start()
        self.addCleanup(patch.stop)
        home = mock.patch("services.remote.net.home_ipv4", return_value="192.168.1.20")
        home.start()
        self.addCleanup(home.stop)

    def hub(self, base: str, config: dict | None = None) -> RemoteHub:
        hub = RemoteHub(self.events, self.web, ports=[0], host="127.0.0.1", tv_name="Living room")
        data = {"version": 1, "enabled": True, "relay": True, "relay_url": base}
        data.update(config or {})
        hub.start(data)
        self.assertTrue(hub.ready.wait(3))
        self.addCleanup(hub.stop)
        return hub

    def run_async(self, coro):
        return asyncio.run(asyncio.wait_for(coro, 20))

    async def wait_for(self, kind, where=lambda e: True, after=0, timeout=5.0):
        return await asyncio.to_thread(self.events.wait, kind, timeout, where, after)

    async def pair(self, relay: FakeRelay, hub: RemoteHub, conn: int = 1, same=False):
        await self.wait_for(Status, where=lambda e: e.relay == "online")
        mark = self.events.mark()
        hub.set_pairing(True)
        url = (await self.wait_for(Code, where=lambda e: ROOM in e.url, after=mark)).url
        room, tv_pub, code = url.split("#1.")[1].split(".")
        phone = PhoneClient(b64d(tv_pub), mode="pair", code=b64d(code))
        await relay.to_tv(conn, OPENED, json.dumps({"same": same}).encode())
        await relay.phone_says(conn, phone.opening())
        await relay.phone_says(conn, phone.first())
        kind, msg2 = await relay.tv_says(conn)
        self.assertEqual(kind, BINARY)
        answer = phone.finish(msg2)
        _, hello = await relay.tv_says(conn)
        self.assertEqual(phone.open(hello)["door"], "relay")
        return phone, answer, url


class RelayClientTests(RelayCase):
    def test_mints_room_once_and_saves(self):
        async def go():
            relay = FakeRelay()
            base = await relay.start()
            try:
                hub = self.hub(base)
                await asyncio.wait_for(relay.connected.wait(), 5)
                saved = await self.wait_for(StoreSaved, where=lambda e: e.data.get("room"))
                hub.stop()
                again = self.hub(base, saved.data)
                relay.connected.clear()
                await asyncio.wait_for(relay.connected.wait(), 5)
                again.stop()
                return relay.posts, relay.auth, saved.data, relay.texts
            finally:
                await relay.stop()

        posts, auth, saved, texts = self.run_async(go())
        self.assertEqual(posts, 1)
        self.assertEqual(saved["room"], ROOM)
        self.assertTrue(saved["room_secret"])
        self.assertEqual(auth, [f"Bearer {saved['room_secret']}"] * 2)
        self.assertIn("v6", texts[0])

    def test_qr_uses_relay_url_with_room(self):
        async def go():
            relay = FakeRelay()
            base = await relay.start()
            try:
                hub = self.hub(base)
                await self.wait_for(Status, where=lambda e: e.relay == "online")
                hub.set_pairing(True)
                url = (await self.wait_for(Code, where=lambda e: ROOM in e.url)).url
                return base, url
            finally:
                await relay.stop()

        base, url = self.run_async(go())
        self.assertTrue(url.startswith(f"{base}/#1.{ROOM}."), url)

    def test_phone_via_relay_pairs_and_presses(self):
        async def go():
            relay = FakeRelay()
            base = await relay.start()
            try:
                hub = self.hub(base)
                phone, answer, _ = await self.pair(relay, hub)
                await relay.phone_says(1, phone.seal({"t": "down", "b": "ok"}))
                await relay.phone_says(1, phone.seal({"t": "up", "b": "ok", "n": 1}))
                await self.wait_for(Press, where=lambda e: not e.down)
                return answer
            finally:
                await relay.stop()

        answer = self.run_async(go())
        self.assertEqual(self.events.wait(Paired).device_id, answer["device"])
        self.assertEqual(answer["room"], ROOM)

    def test_where_gives_ticket_when_same_network(self):
        async def go():
            relay = FakeRelay()
            base = await relay.start()
            try:
                hub = self.hub(base)
                phone, _, _ = await self.pair(relay, hub, same=True)
                await relay.phone_says(1, phone.seal({"t": "where"}))
                _, reply = await relay.tv_says(1)
                return phone.open(reply)
            finally:
                await relay.stop()

        where = self.run_async(go())
        self.assertTrue(where["same"])
        self.assertTrue(where["home"].startswith("http://192.168.1.20:"))
        self.assertEqual(len(where["ticket"].split(".")), 2)

    def test_closed_frame_emits_closed(self):
        async def go():
            relay = FakeRelay()
            base = await relay.start()
            try:
                hub = self.hub(base)
                _, answer, _ = await self.pair(relay, hub)
                await relay.to_tv(1, CLOSED)
                return answer, await self.wait_for(Closed)
            finally:
                await relay.stop()

        answer, closed = self.run_async(go())
        self.assertEqual(closed.device_id, answer["device"])

    def test_removal_closes_the_phone_with_4403(self):
        async def go():
            relay = FakeRelay()
            base = await relay.start()
            try:
                hub = self.hub(base)
                phone, answer, _ = await self.pair(relay, hub)
                hub.remove_device(answer["device"])
                kind, bye = await relay.tv_says(1)
                self.assertEqual(phone.open(bye), {"t": "bye", "reason": "removed"})
                kind, close = await relay.tv_says(1)
                return kind, close
            finally:
                await relay.stop()

        kind, close = self.run_async(go())
        self.assertEqual(kind, CLOSED)
        self.assertEqual(int.from_bytes(close[:2], "big"), 4403)
        self.assertEqual(close[2:], b"removed")

    def test_reconnects_after_drop(self):
        async def go():
            relay = FakeRelay()
            base = await relay.start()
            try:
                self.hub(base)
                await asyncio.wait_for(relay.connected.wait(), 5)
                await relay.tv.close()
                await asyncio.sleep(0.05)
                await asyncio.wait_for(relay.connected.wait(), 3)
                return len(relay.auth)
            finally:
                await relay.stop()

        self.assertEqual(self.run_async(go()), 2)

    def test_resting_on_429(self):
        async def go():
            relay = FakeRelay(refuse=429)
            base = await relay.start()
            try:
                self.hub(base)
                return await self.wait_for(Status, where=lambda e: e.relay == "resting")
            finally:
                await relay.stop()

        self.assertEqual(self.run_async(go()).relay, "resting")

    def test_unknown_room_is_minted_again(self):
        async def go():
            relay = FakeRelay(refuse=404)
            base = await relay.start()
            try:
                self.hub(base, {"room": "old", "room_secret": "s"})
                await self.wait_for(StoreSaved, where=lambda e: e.data.get("room") == ROOM)
                return relay.posts
            finally:
                await relay.stop()

        self.assertGreaterEqual(self.run_async(go()), 1)

    def test_relay_off_connects_nowhere(self):
        async def go():
            relay = FakeRelay()
            base = await relay.start()
            try:
                self.hub(base, {"relay": False})
                await asyncio.sleep(0.5)
                return relay.posts, relay.connected.is_set()
            finally:
                await relay.stop()

        self.assertEqual(self.run_async(go()), (0, False))


if __name__ == "__main__":
    unittest.main()
