import asyncio
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import aiohttp
from aiohttp.test_utils import make_mocked_request
from remote_phone import PhoneClient

from services.remote.hub import RemoteHub
from services.remote.net import is_private
from services.remote.protocol import Closed, Code, Opened, Paired, Press, Status, b64d
from services.remote.server import private_only


class Recorder:
    def __init__(self) -> None:
        self.events: list[object] = []
        self._lock = threading.Lock()

    def __call__(self, event: object) -> None:
        with self._lock:
            self.events.append(event)

    def mark(self) -> int:
        with self._lock:
            return len(self.events)

    def wait(self, kind, timeout=3.0, where=lambda e: True, after=0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                found = [e for e in self.events[after:] if isinstance(e, kind) and where(e)]
            if found:
                return found[-1]
            time.sleep(0.01)
        raise AssertionError(f"no {kind.__name__} in {self.events}")


def code_from(url: str) -> tuple[bytes, bytes]:
    """(tv public key, code) from a pairing QR address."""
    _, tv_pub, code = url.split("#1.")[1].split(".")
    return b64d(tv_pub), b64d(code)


class HomeDoorCase(unittest.TestCase):
    def setUp(self):
        self.web = Path(tempfile.mkdtemp())
        (self.web / "index.html").write_text("<!doctype html><title>QiTV remote</title>")
        (self.web / "app.js").write_text("export {};")
        self.events = Recorder()
        self.hub = RemoteHub(
            self.events,
            self.web,
            ports=[0],
            host="127.0.0.1",
            tv_name="Living room",
            handshake_s=0.4,
        )
        self.hub.start({"version": 1, "enabled": True, "relay": False})
        self.assertTrue(self.hub.ready.wait(3))
        self.port = self.hub.port
        self.addCleanup(self.hub.stop)

    def url(self, path="/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def run_async(self, coro):
        return asyncio.run(asyncio.wait_for(coro, 10))

    async def pair(self, session: aiohttp.ClientSession):
        mark = self.events.mark()
        self.hub.set_pairing(True)
        code_url = self.events.wait(Code, where=lambda e: bool(e.url), after=mark).url
        tv_pub, code = code_from(code_url)
        phone = PhoneClient(tv_pub, mode="pair", code=code)
        ws = await session.ws_connect(self.url("/ws"))
        await ws.send_str(phone.opening())
        await ws.send_bytes(phone.first())
        answer = phone.finish((await ws.receive()).data)
        hello = phone.open((await ws.receive()).data)
        return phone, ws, answer, hello


class HomeDoorTests(HomeDoorCase):
    def test_page_is_served(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                async with s.get(self.url("/")) as r:
                    self.assertEqual(r.status, 200)
                    self.assertIn("text/html", r.headers["Content-Type"])
                async with s.get(self.url("/app.js")) as r:
                    self.assertEqual(r.status, 200)
                    self.assertIn("javascript", r.headers["Content-Type"])
                for path in ("/../pyproject.toml", "/%2e%2e/pyproject.toml", "/nope.js", "/sub/"):
                    async with s.get(self.url(path)) as r:
                        self.assertEqual(r.status, 404, path)

        self.run_async(go())

    def test_door_json_says_home(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                async with s.get(self.url("/door.json")) as r:
                    return r.status, await r.json()

        self.assertEqual(self.run_async(go()), (200, {"door": "home"}))

    def test_status_names_the_home_address(self):
        status = self.events.wait(Status)
        self.assertEqual(status.relay, "off")
        if status.home_url:
            self.assertTrue(status.home_url.endswith(f":{self.port}/"))

    def test_pair_and_press_end_to_end(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                phone, ws, answer, hello = await self.pair(s)
                self.assertEqual(hello["tv"], "Living room")
                await ws.send_bytes(phone.seal({"t": "down", "b": "down"}))
                await ws.send_bytes(phone.seal({"t": "up", "b": "down", "n": 2}))
                self.events.wait(Press, where=lambda e: not e.down)
                await ws.close()
                return answer

        answer = self.run_async(go())
        paired = self.events.wait(Paired)
        self.assertEqual(paired.device_id, answer["device"])
        presses = [e for e in self.events.events if isinstance(e, Press)]
        self.assertEqual(
            [(p.button, p.down, p.steps) for p in presses], [("down", True, 1), ("down", False, 2)]
        )

    def test_code_changes_after_a_pairing(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                await self.pair(s)

        self.run_async(go())
        codes = [e.url for e in self.events.events if isinstance(e, Code) and e.url]
        self.assertGreaterEqual(len(set(codes)), 2)

    def test_send_reaches_the_phone(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                phone, ws, _, _ = await self.pair(s)
                sid = self.events.wait(Opened).sid
                self.hub.send(sid, {"t": "state", "volume": 40})
                self.hub.broadcast({"t": "state", "volume": 41})
                first = phone.open((await ws.receive()).data)
                second = phone.open((await ws.receive()).data)
                return first, second

        first, second = self.run_async(go())
        self.assertEqual((first["volume"], second["volume"]), (40, 41))

    def test_ninth_session_refused_4429(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                sockets = [(await self.pair(s))[1] for _ in range(8)]
                extra = await s.ws_connect(self.url("/ws"))
                message = await extra.receive()
                for ws in sockets:
                    await ws.close()
                return message, extra.close_code

        message, code = self.run_async(go())
        self.assertEqual(message.type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(code, 4429)

    def test_handshake_timeout_closes(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                ws = await s.ws_connect(self.url("/ws"))
                await ws.send_str('{"v":1,"mode":"pair"}')
                message = await ws.receive()
                return message.type, ws.close_code

        kind, code = self.run_async(go())
        self.assertEqual(kind, aiohttp.WSMsgType.CLOSE)
        self.assertIn(code, (4401, 4408))  # pairing closed (4401) or too slow (4408)

    def test_handshake_timeout_with_pairing_open(self):
        self.hub.set_pairing(True)
        self.events.wait(Code, where=lambda e: bool(e.url))

        async def go():
            async with aiohttp.ClientSession() as s:
                ws = await s.ws_connect(self.url("/ws"))
                await ws.send_str('{"v":1,"mode":"pair"}')
                await ws.receive()
                return ws.close_code

        self.assertEqual(self.run_async(go()), 4408)

    def test_dropped_socket_emits_closed(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                _, ws, answer, _ = await self.pair(s)
                ws._response.connection.transport.abort()
                return answer

        answer = self.run_async(go())
        closed = self.events.wait(Closed, timeout=2.0)
        self.assertEqual(closed.device_id, answer["device"])

    def test_remove_device_closes_open_session(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                phone, ws, answer, _ = await self.pair(s)
                self.hub.remove_device(answer["device"])
                bye = phone.open((await ws.receive()).data)
                end = await ws.receive()
                return bye, end.type, ws.close_code

        bye, kind, code = self.run_async(go())
        self.assertEqual(bye, {"t": "bye", "reason": "removed"})
        self.assertEqual(kind, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(code, 4403)

    def test_stop_joins(self):
        started = time.monotonic()
        self.hub.stop()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse(self.hub.running)


class PortTests(unittest.TestCase):
    def test_busy_port_moves_to_next(self):
        web = Path(tempfile.mkdtemp())
        (web / "index.html").write_text("x")
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            taken = busy.getsockname()[1]
            events = Recorder()
            hub = RemoteHub(events, web, ports=[taken, 0], host="127.0.0.1")
            hub.start({"version": 1, "enabled": True, "relay": False})
            try:
                self.assertTrue(hub.ready.wait(3))
                self.assertNotEqual(hub.port, taken)
                self.assertGreater(hub.port, 0)
            finally:
                hub.stop()


class EarlyCallTests(unittest.TestCase):
    def test_pairing_asked_before_the_hub_is_up_is_kept(self):
        web = Path(tempfile.mkdtemp())
        (web / "index.html").write_text("x")
        events = Recorder()
        hub = RemoteHub(events, web, ports=[0], host="127.0.0.1")
        hub.start({"version": 1, "enabled": True, "relay": False})
        hub.set_pairing(True)  # at once, as Settings → Show the code does
        try:
            url = events.wait(Code, where=lambda e: bool(e.url)).url
            self.assertTrue(hub.ready.wait(3))
            self.assertIn(f":{hub.port}/#1.", url)  # the real port, not 0
        finally:
            hub.stop()


class PrivateTests(unittest.TestCase):
    def test_private_addresses(self):
        for addr in (
            "10.1.2.3",
            "172.16.0.9",
            "172.31.255.1",
            "192.168.50.123",
            "169.254.1.1",
            "127.0.0.1",
            "::1",
            "fe80::1",
            "fd12:3456::1",
            "::ffff:192.168.1.5",
        ):
            self.assertTrue(is_private(addr), addr)
        for addr in ("8.8.8.8", "172.32.0.1", "100.64.0.1", "2001:db8::1", "", "nonsense", None):
            self.assertFalse(is_private(addr), addr)

    def test_public_address_refused(self):
        async def go():
            transport = mock.Mock()
            transport.get_extra_info.side_effect = lambda name, default=None: (
                ("8.8.8.8", 4444) if name == "peername" else default
            )
            request = make_mocked_request("GET", "/", transport=transport)
            handler = mock.AsyncMock(return_value="page")
            response = await private_only(request, handler)
            handler.assert_not_called()
            return response.status

        self.assertEqual(asyncio.run(go()), 403)


if __name__ == "__main__":
    unittest.main()
