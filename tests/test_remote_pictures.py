"""Pictures on the phone at home (services/remote/pictures.py): the home door serves a paired
phone the posters and logos its rows name by opaque tokens, from QiTV's own image cache; a token
is good for its session alone (and the address it came from); through the relay there are none.
"""

import asyncio
import logging
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock
import zlib

import aiohttp
from aiohttp import web
from remote_phone import PhoneClient
from test_remote_home_door import Recorder, code_from

from services.image_manager import ImageManager
from services.remote import pictures
from services.remote.hub import RemoteHub, _Conn
from services.remote.pictures import AS_IS_MAX, LOGO, POSTER, PictureShelf, sniff
from services.remote.protocol import Closed, Code, Opened, PictureWanted
from services.remote.session import Reply


def png(width=2, height=3, pad=0) -> bytes:
    """A real PNG (`pad` bytes of a text chunk make it as large as wanted)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\x80\x40\x20" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    extra = chunk(b"tEXt", b"c\x00" + b"x" * pad) if pad else b""
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + extra
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class ShelfTests(unittest.TestCase):
    def setUp(self):
        self.shelf = PictureShelf()
        self.shelf.open("s1", "192.168.1.20")

    def test_a_token_only_for_a_session_the_home_door_opened(self):
        self.assertEqual(self.shelf.issue("s9", "https://img.example/a.jpg", POSTER), "")
        token = self.shelf.issue("s1", "https://img.example/a.jpg", POSTER)
        self.assertRegex(token, r"^[A-Za-z0-9_-]{22,}$")  # 128 bits at least
        self.assertNotIn("img.example", token)

    def test_the_same_picture_keeps_its_token_in_a_session(self):
        first = self.shelf.issue("s1", "https://img.example/a.jpg", POSTER)
        self.assertEqual(self.shelf.issue("s1", "https://img.example/a.jpg", POSTER), first)
        self.assertNotEqual(self.shelf.issue("s1", "https://img.example/a.jpg", LOGO), first)
        self.shelf.open("s2", "192.168.1.21")
        self.assertNotEqual(self.shelf.issue("s2", "https://img.example/a.jpg", POSTER), first)

    def test_a_token_answers_its_sessions_address_alone(self):
        token = self.shelf.issue("s1", "https://img.example/a.jpg", POSTER, verify=False)
        found = self.shelf.get(token, "192.168.1.20")
        self.assertEqual(
            (found.source, found.kind, found.verify), ("https://img.example/a.jpg", POSTER, False)
        )
        self.assertIsNone(self.shelf.get(token, "192.168.1.99"))
        self.assertIsNone(self.shelf.get("nope", "192.168.1.20"))
        self.assertIsNone(self.shelf.get("", "192.168.1.20"))

    def test_tokens_end_with_their_session(self):
        token = self.shelf.issue("s1", "https://img.example/a.jpg", POSTER)
        self.shelf.close("s1")
        self.assertIsNone(self.shelf.get(token, "192.168.1.20"))
        self.assertEqual(self.shelf.issue("s1", "https://img.example/b.jpg", POSTER), "")

    def test_no_token_for_nothing(self):
        self.assertEqual(self.shelf.issue("s1", "", POSTER), "")
        self.assertEqual(self.shelf.issue("s1", "https://img.example/a.jpg", "film"), "")

    def test_a_session_keeps_its_latest_pictures(self):
        with mock.patch.object(pictures, "SESSION_PICTURES", 3):
            tokens = [
                self.shelf.issue("s1", f"https://img.example/{i}.jpg", POSTER) for i in range(4)
            ]
            self.assertIsNone(self.shelf.get(tokens[0], "192.168.1.20"))
            self.assertIsNotNone(self.shelf.get(tokens[3], "192.168.1.20"))


class SniffTests(unittest.TestCase):
    def test_only_the_webs_own_picture_formats(self):
        self.assertEqual(sniff(png()), "image/png")
        self.assertEqual(sniff(JPEG), "image/jpeg")
        self.assertEqual(sniff(b"GIF89a" + b"\x00" * 10), "image/gif")
        self.assertEqual(sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        for other in (
            b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            b"<!doctype html>",
            b"BM\x00\x00",
            b"",
        ):
            self.assertIsNone(sniff(other), other)


class FakeImages:
    """QiTV's image cache as the hub uses it: files by address, a download where there is none
    yet (`fetched`); every call and the thread it ran on recorded."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.files: dict[str, bytes] = {}  # what is cached
        self.fetched: dict[str, bytes] = {}  # what a download brings
        self.calls: list[tuple[str, bool, str]] = []
        self.discarded: list[str] = []
        self.gate: threading.Event | None = None

    async def cache_image_from_url(self, session, url, iconified):
        self.calls.append((url, iconified, threading.current_thread().name))
        if self.gate is not None:
            await asyncio.get_running_loop().run_in_executor(None, self.gate.wait, 5)
        data = self.files.get(url, self.fetched.get(url))
        if data is None:
            return None
        path = self.folder / f"{abs(hash((url, iconified)))}.{'png' if iconified else 'jpg'}"
        path.write_bytes(data)
        return str(path)

    async def cache_image_from_base64(self, image_str, iconified):
        return None

    def discard(self, path):
        self.discarded.append(path)


class HubCase(unittest.TestCase):
    images_class = FakeImages

    def make_images(self, folder: Path):
        return FakeImages(folder)

    def setUp(self):
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.web = Path(folder.name) / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<!doctype html><title>QiTV remote</title>")
        self.images = self.make_images(Path(folder.name))
        self.events = Recorder()
        self.hub = RemoteHub(
            self.events, self.web, ports=[0], host="127.0.0.1", tv_name="Den", images=self.images
        )
        self.hub.start({"version": 1, "enabled": True, "relay": False})
        self.assertTrue(self.hub.ready.wait(3))
        self.addCleanup(self.hub.stop)

    def url(self, path="/") -> str:
        return f"http://127.0.0.1:{self.hub.port}{path}"

    def run_async(self, coro):
        return asyncio.run(asyncio.wait_for(coro, 15))

    async def pair(self, session):
        """A phone paired at home: its socket, and its session's id (as QiTV's GUI hears it)."""
        mark = self.events.mark()
        self.hub.set_pairing(True)
        tv_pub, code = code_from(
            self.events.wait(Code, where=lambda e: bool(e.url), after=mark).url
        )
        phone = PhoneClient(tv_pub, mode="pair", code=code)
        ws = await session.ws_connect(self.url("/ws"))
        await ws.send_str(phone.opening())
        await ws.send_bytes(phone.first())
        phone.finish((await ws.receive()).data)
        phone.open((await ws.receive()).data)  # the hello
        sid = self.events.wait(Opened, after=mark).sid
        return ws, sid

    async def get(self, session, path):
        async with session.get(self.url(path)) as response:
            return response.status, response.headers, await response.read()


class HomeDoorPictureTests(HubCase):
    def test_a_phone_at_home_gets_the_picture_its_token_names(self):
        self.images.files["https://img.example/p/1.jpg?key=secret"] = png()

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(
                    sid, "https://img.example/p/1.jpg?key=secret", POSTER
                )
                answer = await self.get(s, f"/img/{token}")
                await ws.close()
                return answer

        status, headers, body = self.run_async(go())
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("private", headers["Cache-Control"])
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertEqual(body, png())
        self.assertEqual(
            self.images.calls[0][:2], ("https://img.example/p/1.jpg?key=secret", False)
        )

    def test_a_logo_is_the_tvs_logo_file(self):
        self.images.files["https://img.example/l.png"] = png()

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/l.png", LOGO)
                answer = await self.get(s, f"/img/{token}")
                await ws.close()
                return answer

        self.assertEqual(self.run_async(go())[0], 200)
        self.assertEqual(self.images.calls[0][:2], ("https://img.example/l.png", True))  # iconified

    def test_an_unknown_token_gets_nothing_and_fetches_nothing(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                ws, _sid = await self.pair(s)
                answers = [await self.get(s, path) for path in ("/img/nope", "/img/", "/img/a/b")]
                await ws.close()
                return answers

        for status, headers, _body in self.run_async(go()):
            self.assertEqual(status, 404)
        self.assertEqual(self.images.calls, [])

    def test_an_unpaired_client_gets_nothing(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                return await self.get(s, "/img/AAAAAAAAAAAAAAAAAAAAAA")

        self.assertEqual(self.run_async(go())[0], 404)
        self.assertEqual(
            self.hub.pictures.issue("s-unknown", "https://img.example/a.jpg", POSTER), ""
        )
        self.assertEqual(self.images.calls, [])

    def test_a_token_ends_with_its_session(self):
        self.images.files["https://img.example/a.jpg"] = JPEG

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/a.jpg", POSTER)
                first = (await self.get(s, f"/img/{token}"))[0]
                mark = self.events.mark()
                await ws.close()
                self.events.wait(Closed, after=mark)
                return first, (await self.get(s, f"/img/{token}"))[0]

        self.assertEqual(self.run_async(go()), (200, 404))

    def test_a_missing_picture_is_fetched_on_a_worker(self):
        self.images.fetched["https://img.example/new.jpg"] = JPEG

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/new.jpg", POSTER)
                answer = await self.get(s, f"/img/{token}")
                await ws.close()
                return answer

        status, headers, body = self.run_async(go())
        self.assertEqual((status, headers["Content-Type"], body), (200, "image/jpeg", JPEG))
        [(_url, _iconified, thread)] = self.images.calls
        self.assertNotIn(
            thread, ("MainThread", "qitv-remote")
        )  # neither the GUI's nor the presses'

    def test_many_asks_for_one_picture_fetch_it_once(self):
        self.images.fetched["https://img.example/once.jpg"] = JPEG
        self.images.gate = threading.Event()

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/once.jpg", POSTER)
                asks = [asyncio.ensure_future(self.get(s, f"/img/{token}")) for _ in range(3)]
                await asyncio.sleep(0.3)
                self.images.gate.set()
                answers = await asyncio.gather(*asks)
                again = await self.get(s, f"/img/{token}")  # kept: no download, no disk
                await ws.close()
                return answers + [again]

        self.assertEqual([a[0] for a in self.run_async(go())], [200, 200, 200, 200])
        self.assertEqual(len(self.images.calls), 1)

    def test_a_picture_that_failed_is_not_asked_for_again_at_once(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/gone.jpg", POSTER)
                answers = [(await self.get(s, f"/img/{token}"))[0] for _ in range(2)]
                await ws.close()
                return answers

        self.assertEqual(self.run_async(go()), [404, 404])
        self.assertEqual(len(self.images.calls), 1)

    def test_a_page_that_is_no_picture_is_refused_and_dropped_from_the_cache(self):
        self.images.files["https://img.example/err.jpg"] = (
            b"<!DOCTYPE html><title>Not found</title>"
        )

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/err.jpg", POSTER)
                answer = await self.get(s, f"/img/{token}")
                await ws.close()
                return answer

        self.assertEqual(self.run_async(go())[0], 404)
        self.assertEqual(len(self.images.discarded), 1)

    def test_an_svg_is_never_served_as_it_is(self):
        self.images.files["https://img.example/l.svg"] = (
            b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        )

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/l.svg", LOGO)
                answer = asyncio.ensure_future(self.get(s, f"/img/{token}"))
                wanted = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.events.wait(PictureWanted)
                )
                self.hub.put_picture(
                    wanted.source, wanted.kind, png(), "image/png"
                )  # as QiTV drew it
                result = await answer
                await ws.close()
                return result

        status, headers, body = self.run_async(go())
        self.assertEqual((status, headers["Content-Type"], body), (200, "image/png", png()))

    def test_a_large_picture_is_served_as_qitv_scaled_it(self):
        big = png(pad=AS_IS_MAX + 10)
        self.images.files["https://img.example/big.png"] = big

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/big.png", POSTER)
                answer = asyncio.ensure_future(self.get(s, f"/img/{token}"))
                wanted = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.events.wait(PictureWanted)
                )
                self.hub.put_picture(wanted.source, wanted.kind, JPEG, "image/jpeg")
                result = await answer
                await ws.close()
                return wanted, result

        wanted, (status, headers, body) = self.run_async(go())
        self.assertEqual((wanted.source, wanted.kind), ("https://img.example/big.png", POSTER))
        self.assertEqual((status, headers["Content-Type"], body), (200, "image/jpeg", JPEG))

    def test_a_large_picture_nobody_scales_is_refused_in_time(self):
        self.images.files["https://img.example/big.png"] = png(pad=AS_IS_MAX + 10)

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/big.png", POSTER)
                started = time.monotonic()
                answer = await self.get(s, f"/img/{token}")
                await ws.close()
                return answer[0], time.monotonic() - started

        with mock.patch.object(pictures, "SCALE_S", 0.3):
            status, took = self.run_async(go())
        self.assertEqual(status, 404)
        self.assertLess(took, 3)

    def test_a_scaled_copy_must_be_a_picture(self):
        self.images.files["https://img.example/big.png"] = png(pad=AS_IS_MAX + 10)

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/big.png", POSTER)
                answer = asyncio.ensure_future(self.get(s, f"/img/{token}"))
                wanted = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.events.wait(PictureWanted)
                )
                self.hub.put_picture(wanted.source, wanted.kind, b"<html>", "image/png")
                result = await answer
                await ws.close()
                return result[0]

        with mock.patch.object(pictures, "SCALE_S", 0.5):
            self.assertEqual(self.run_async(go()), 404)

    def test_the_relay_door_opens_no_pictures(self):
        """A session through the relay is never given a token, whatever QiTV's GUI asks."""
        core = self.hub._core

        async def relayed():
            session = core.new_session("relay", True)
            sent: list = []

            async def send(frame):
                sent.append(frame)

            async def close(code, reason):
                pass

            conn = _Conn(session, send, close)
            core.register(conn)
            await core.deliver(conn, Reply(events=[Opened(session.sid, "d1", "relay")]))
            return session.sid

        sid = asyncio.run_coroutine_threadsafe(relayed(), self.hub._loop).result(3)
        self.assertEqual(self.hub.pictures.issue(sid, "https://img.example/a.jpg", POSTER), "")

    def test_nothing_is_fetched_once_the_hub_stops(self):
        self.images.fetched["https://img.example/late.jpg"] = JPEG
        server = self.hub._core.pictures
        server.stop()

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/late.jpg", POSTER)
                answer = await self.get(s, f"/img/{token}")
                await ws.close()
                return answer[0]

        self.assertEqual(self.run_async(go()), 404)
        self.assertEqual(self.images.calls, [])

    def test_nothing_is_logged_with_a_link(self):
        self.images.fetched["https://img.example/p/9.jpg?user=me&pass=pw"] = b"<html>"
        records: list[logging.LogRecord] = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Keep(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous)
        self.addCleanup(root.removeHandler, handler)

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(
                    sid, "https://img.example/p/9.jpg?user=me&pass=pw", POSTER
                )
                await self.get(s, f"/img/{token}")
                await self.get(s, "/img/unknown-token")
                await ws.close()

        self.run_async(go())
        text = "\n".join(r.getMessage() for r in records)
        for secret in ("/p/9.jpg", "pass=pw", "img.example"):
            self.assertNotIn(secret, text)


class Provider:
    """An image host on localhost: /p/<name> answers the bytes `files` names (404 else)."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.asked: list[str] = []
        self.loop = asyncio.new_event_loop()
        started = threading.Event()

        async def serve(request):
            self.asked.append(request.path_qs)
            data = self.files.get(request.match_info["name"])
            if data is None:
                return web.Response(status=404, text="no")
            return web.Response(body=data, content_type="application/octet-stream")

        def run():
            asyncio.set_event_loop(self.loop)
            app = web.Application()
            app.router.add_get("/p/{name}", serve)
            self.runner = web.AppRunner(app)
            self.loop.run_until_complete(self.runner.setup())
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            self.loop.run_until_complete(site.start())
            self.port = site._server.sockets[0].getsockname()[1]
            started.set()
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        started.wait(5)

    def url(self, name, query=""):
        return f"http://127.0.0.1:{self.port}/p/{name}{query}"

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)


class Config:
    def __init__(self, folder: str):
        self.config_path = str(Path(folder) / "config.json")

    def get_config_dir(self):
        return str(Path(self.config_path).parent)


class TvImageCacheTests(HubCase):
    """With QiTV's own ImageManager: the phone's picture is the TV's cached file, fetched into
    that cache when missing."""

    def make_images(self, folder: Path):
        self.provider = Provider()
        self.addCleanup(self.provider.stop)
        return ImageManager(Config(str(folder)))

    def test_a_missing_poster_lands_in_the_tvs_cache_and_reaches_the_phone(self):
        self.provider.files["1.jpg"] = JPEG
        url = self.provider.url("1.jpg", "?token=secret")

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, url, POSTER)
                answers = [await self.get(s, f"/img/{token}") for _ in range(2)]
                await ws.close()
                return answers

        (status, _headers, body), again = self.run_async(go())
        self.assertEqual((status, body), (200, JPEG))
        self.assertEqual(again[0], 200)
        self.assertEqual(self.provider.asked, ["/p/1.jpg?token=secret"])  # once
        cached = list(Path(self.images.cache_dir).glob("*.jpg"))
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0].read_bytes(), JPEG)

    def test_a_poster_already_cached_is_not_downloaded(self):
        url = self.provider.url("2.jpg")

        async def warm():
            async with aiohttp.ClientSession() as s:
                self.provider.files["2.jpg"] = JPEG
                await self.images.cache_image_from_url(s, url, False)
                self.provider.asked.clear()

        self.run_async(warm())

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, url, POSTER)
                answer = await self.get(s, f"/img/{token}")
                await ws.close()
                return answer

        self.assertEqual(self.run_async(go())[0], 200)
        self.assertEqual(self.provider.asked, [])


if __name__ == "__main__":
    unittest.main()
