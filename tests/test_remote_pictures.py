"""Pictures on the phone at home (services/remote/pictures.py): the home door serves a paired
phone the posters and logos its rows name by opaque tokens, from QiTV's own image cache; a token
is good for its session alone (and the address it came from); through the relay there are none.
"""

import asyncio
import gc
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
from services.remote.pictures import (
    AS_IS_MAX,
    LOGO,
    MAX_SIDE,
    POSTER,
    PictureShelf,
    picture_size,
    sniff,
)
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


def jpeg(width=4, height=6, sof=True) -> bytes:
    """A JPEG's opening as far as its size (APP0, then SOF0 unless `sof` is False)."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    frame = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    sof0 = b"\xff\xc0" + struct.pack(">H", len(frame) + 2) + frame if sof else b""
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xda" + b"\x00" * 32 + b"\xff\xd9"


def webp(kind: bytes, width: int, height: int) -> bytes:
    """A WebP's opening as far as its size: a lossy (VP8 ), lossless (VP8L) or extended one."""
    if kind == b"VP8 ":
        body = b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", width, height)
    elif kind == b"VP8L":
        bits = (width - 1) | ((height - 1) << 14)
        body = b"\x2f" + struct.pack("<I", bits)
    else:
        body = (
            b"\x00\x00\x00\x00"
            + (width - 1).to_bytes(3, "little")
            + (height - 1).to_bytes(3, "little")
        )
    chunk = kind + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


JPEG = jpeg()


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
    def test_a_pictures_size_from_its_header_alone(self):
        self.assertEqual(picture_size(png(2, 3)), (2, 3))
        self.assertEqual(picture_size(jpeg(640, 960)), (640, 960))
        self.assertEqual(
            picture_size(b"GIF89a" + struct.pack("<HH", 300, 200) + b"\x00" * 8), (300, 200)
        )
        for kind in (b"VP8 ", b"VP8L", b"VP8X"):
            self.assertEqual(picture_size(webp(kind, 1000, 1500)), (1000, 1500), kind)
        self.assertIsNone(picture_size(jpeg(sof=False)))  # no frame found: not known
        self.assertIsNone(picture_size(png()[:20]))
        self.assertIsNone(picture_size(b"<svg/>"))

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
        self.delay = 0.0  # a download's time
        self.hold = False  # downloads wait until called off
        self.cancelled: list[str] = []
        self.sessions: list = []
        self.fixed: dict[str, bytes] = {}  # what a download brings once the file was dropped
        self.vanish: set[str] = set()  # whose file goes (evicted) before it is read, once
        self._urls: dict[str, str] = {}  # file → address

    async def cache_image_from_url(self, session, url, iconified):
        self.calls.append((url, iconified, threading.current_thread().name))
        self.sessions.append(session)
        if self.gate is not None:
            await asyncio.get_running_loop().run_in_executor(None, self.gate.wait, 5)
        try:
            if self.hold:
                await asyncio.sleep(30)
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled.append(url)
            raise
        data = self.files.get(url, self.fetched.get(url))
        if data is None:
            return None
        path = self.folder / f"{abs(hash((url, iconified)))}.{'png' if iconified else 'jpg'}"
        path.write_bytes(data)
        self._urls[str(path)] = url
        if url in self.vanish:
            self.vanish.discard(url)
            path.unlink()
        return str(path)

    async def cache_image_from_base64(self, image_str, iconified):
        return None

    def discard(self, path):
        self.discarded.append(path)
        url = self._urls.get(path, "")
        if url in self.fixed:
            self.files[url] = self.fixed.pop(url)


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

    def test_a_scaled_copy_that_comes_late_is_kept_for_the_next_ask(self):
        """The TV's cache was busy past SCALE_S: its copy, when it comes, is the next answer."""
        self.images.files["https://img.example/big.png"] = png(pad=AS_IS_MAX + 10)

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/big.png", POSTER)
                first = await self.get(s, f"/img/{token}")
                wanted = self.events.wait(PictureWanted)
                self.hub.put_picture(wanted.source, wanted.kind, JPEG, "image/jpeg")  # late
                await asyncio.sleep(0.2)
                again = await self.get(s, f"/img/{token}")
                await ws.close()
                return first, again

        with mock.patch.object(pictures, "SCALE_S", 0.3):
            (status, headers, _body), again = self.run_async(go())
        self.assertEqual(status, 404)
        self.assertEqual(headers["Cache-Control"], "no-store")  # a retry reaches QiTV
        self.assertEqual((again[0], again[2]), (200, JPEG))

    def test_a_picture_that_failed_is_tried_again_after_a_while(self):
        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/late.jpg", POSTER)
                first = (await self.get(s, f"/img/{token}"))[0]
                self.images.fetched["https://img.example/late.jpg"] = JPEG  # the host is back
                await asyncio.sleep(0.3)
                again = (await self.get(s, f"/img/{token}"))[0]
                await ws.close()
                return first, again

        with mock.patch.object(pictures, "FAILED_S", 0.2):
            self.assertEqual(self.run_async(go()), (404, 200))
        self.assertEqual(len(self.images.calls), 2)
        self.assertLess(pictures.FAILED_S, 20)  # before the page asks again (screens.js)

    def test_waiting_for_a_download_slot_is_not_counted_in_its_time(self):
        for i in range(2):
            self.images.fetched[f"https://img.example/q{i}.jpg"] = JPEG
        self.images.delay = 0.35

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                tokens = [
                    self.hub.pictures.issue(sid, f"https://img.example/q{i}.jpg", POSTER)
                    for i in range(2)
                ]
                answers = await asyncio.gather(*(self.get(s, f"/img/{t}") for t in tokens))
                await ws.close()
                return [a[0] for a in answers]

        with mock.patch.object(pictures, "FETCHES", 1), mock.patch.object(pictures, "FETCH_S", 0.5):
            self.assertEqual(self.run_async(go()), [200, 200])

    def test_a_picture_of_many_pixels_goes_scaled_however_few_its_bytes(self):
        wide = png(width=MAX_SIDE + 1, height=2)
        self.assertLess(len(wide), AS_IS_MAX)
        self.images.files["https://img.example/wide.png"] = wide

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/wide.png", POSTER)
                answer = asyncio.ensure_future(self.get(s, f"/img/{token}"))
                wanted = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.events.wait(PictureWanted)
                )
                self.hub.put_picture(wanted.source, wanted.kind, png(), "image/png")
                result = await answer
                await ws.close()
                return result

        status, _headers, body = self.run_async(go())
        self.assertEqual((status, body), (200, png()))

    def test_a_jpeg_whose_size_isnt_found_goes_scaled(self):
        self.images.files["https://img.example/odd.jpg"] = jpeg(sof=False)

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/odd.jpg", POSTER)
                answer = await self.get(s, f"/img/{token}")
                await ws.close()
                return answer[0]

        with mock.patch.object(pictures, "SCALE_S", 0.3):
            self.assertEqual(self.run_async(go()), 404)
        self.assertTrue(self.events.wait(PictureWanted))

    def test_a_scaled_copy_of_many_pixels_is_refused(self):
        self.images.files["https://img.example/big.png"] = png(pad=AS_IS_MAX + 10)

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/big.png", POSTER)
                answer = asyncio.ensure_future(self.get(s, f"/img/{token}"))
                wanted = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.events.wait(PictureWanted)
                )
                self.hub.put_picture(wanted.source, wanted.kind, png(MAX_SIDE + 1, 1), "image/png")
                result = await answer
                await ws.close()
                return result[0]

        with mock.patch.object(pictures, "SCALE_S", 0.5):
            self.assertEqual(self.run_async(go()), 404)

    def test_stopping_calls_off_the_downloads_and_closes_their_session(self):
        self.images.fetched["https://img.example/slow.jpg"] = JPEG
        self.images.hold = True
        records: list[logging.LogRecord] = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Keep(logging.DEBUG)
        logging.getLogger("asyncio").addHandler(handler)
        self.addCleanup(logging.getLogger("asyncio").removeHandler, handler)

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, "https://img.example/slow.jpg", POSTER)
                asking = asyncio.ensure_future(self.get(s, f"/img/{token}"))
                end = time.monotonic() + 3
                while not self.images.calls and time.monotonic() < end:
                    await asyncio.sleep(0.02)
                await asyncio.get_running_loop().run_in_executor(None, self.hub.stop)
                await asyncio.gather(asking, return_exceptions=True)

        self.run_async(go())
        end = time.monotonic() + 3
        while not self.images.cancelled and time.monotonic() < end:
            time.sleep(0.02)
        self.assertEqual(self.images.cancelled, ["https://img.example/slow.jpg"])
        self.assertTrue(self.images.sessions and all(s.closed for s in self.images.sessions))
        gc.collect()
        text = "\n".join(r.getMessage() for r in records)
        self.assertNotIn("Task was destroyed", text)
        self.assertNotIn("Unclosed", text)

    def get_one(self, url, kind=POSTER, scale_with=None):
        """A phone's ask for the picture at `url` (as QiTV would scale it: `scale_with`)."""

        async def go():
            async with aiohttp.ClientSession() as s:
                ws, sid = await self.pair(s)
                token = self.hub.pictures.issue(sid, url, kind)
                answer = asyncio.ensure_future(self.get(s, f"/img/{token}"))
                if scale_with is not None:
                    wanted = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: self.events.wait(PictureWanted)
                    )
                    self.hub.put_picture(wanted.source, wanted.kind, scale_with, "image/png")
                result = await answer
                await ws.close()
                return result

        return self.run_async(go())

    def test_a_picture_cut_off_in_the_cache_is_a_miss_fetched_again(self):
        url = "https://img.example/torn.png"
        self.images.files[url] = png()[:-12]  # no end: half written by an older QiTV
        self.images.fixed[url] = png()
        status, _headers, body = self.get_one(url)
        self.assertEqual((status, body), (200, png()))
        self.assertEqual(len(self.images.calls), 2)
        self.assertEqual(len(self.images.discarded), 1)

    def test_an_empty_file_in_the_cache_is_a_miss_fetched_again(self):
        url = "https://img.example/empty.jpg"
        self.images.files[url] = b""
        self.images.fixed[url] = JPEG
        status, _headers, body = self.get_one(url)
        self.assertEqual((status, body), (200, JPEG))

    def test_a_picture_gone_from_the_cache_before_its_read_is_fetched_again(self):
        url = "https://img.example/evicted.jpg"
        self.images.files[url] = JPEG
        self.images.vanish.add(url)
        status, _headers, body = self.get_one(url)
        self.assertEqual((status, body), (200, JPEG))
        self.assertEqual(len(self.images.calls), 2)

    def test_a_picture_its_host_cuts_off_goes_as_qitv_scales_it(self):
        url = "https://img.example/short.png"
        self.images.files[url] = png()[:-12]  # every download as short: read as far as it goes
        status, _headers, body = self.get_one(url, scale_with=png())
        self.assertEqual((status, body), (200, png()))

    def test_a_cut_off_scaled_copy_is_refused(self):
        url = "https://img.example/big.png"
        self.images.files[url] = png(pad=AS_IS_MAX + 10)
        with mock.patch.object(pictures, "SCALE_S", 0.5):
            self.assertEqual(self.get_one(url, scale_with=png()[:-12])[0], 404)

    def test_nothing_is_fetched_once_the_hub_stops(self):
        self.images.fetched["https://img.example/late.jpg"] = JPEG
        server = self.hub._core.pictures
        asyncio.run_coroutine_threadsafe(server.stop(), self.hub._loop).result(3)

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
