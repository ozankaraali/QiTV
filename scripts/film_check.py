"""Play MKV films as a phone gets them (HLS from QiTV's home door) in headless WebKit.

    uv run --with playwright python scripts/film_check.py
    uv run --with playwright python scripts/film_check.py --url https://example.org/a.mkv
    uv run --with playwright==1.51.0 python scripts/film_check.py --webkit PATH/pw_run.sh

WebKit is Safari's engine (its media through the system's AVFoundation, as a phone's). The
fixtures (tests/fixtures/film) come from a stand-in provider on 127.0.0.1 with byte ranges; a
remote hub on 127.0.0.1 opens each (services/remote/films.py) and WebKit plays its playlist,
checks it moves, then seeks past the first piece and checks it moves again. --url plays a film
from elsewhere instead (muted, a few seconds of it). Nothing shows on screen. On macOS 13 the
newest Playwright has no WebKit: pin one whose WebKit is installed and pass its --webkit path.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Optional
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402

from services.remote.hub import RemoteHub  # noqa: E402

FILMS = ROOT / "tests" / "fixtures" / "film"
PLAY = """async ([url, seek]) => {
  const v = document.createElement('video');
  v.muted = true; v.playsInline = true; document.body.appendChild(v);
  const moved = (past) => new Promise((done) => {
    const end = Date.now() + 20000;
    const tick = () => {
      if (v.error) done('error ' + v.error.code);
      else if (v.currentTime > past) done('');
      else if (Date.now() > end) done('stuck at ' + v.currentTime.toFixed(2));
      else setTimeout(tick, 100);
    };
    tick();
  });
  v.src = url; v.play().catch(() => {});
  const first = await moved(1.5);
  if (first) return first;
  v.currentTime = seek;
  const second = await moved(seek + 1);
  return second ? 'after the seek: ' + second : 'plays, seeks; ' + v.duration.toFixed(1) + ' s';
}"""


class Provider:
    """The fixtures over HTTP with byte ranges, on its own loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True).start()
        ready.wait(5)

    def _run(self, ready: threading.Event) -> None:
        asyncio.set_event_loop(self.loop)

        async def serve(request: web.Request) -> web.StreamResponse:
            return web.FileResponse(FILMS / request.match_info["name"])

        app = web.Application()
        app.router.add_get("/movie/{name}", serve)
        runner = web.AppRunner(app)
        self.loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        self.loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]
        ready.set()
        self.loop.run_forever()


def hls_of(hub: RemoteHub, url: str) -> dict:
    """What the phone gets for the film at `url`: its play (with the HLS address) or why not."""
    sent: list[dict] = []
    core = hub._core
    with mock.patch.object(core, "send", lambda sid, message: sent.append(message)):
        hub.open_film("check", "check", url, {"User-Agent": "VLC/3.0.20"}, True, {"t": "play"})
        end = time.monotonic() + 40
        while not sent and time.monotonic() < end:
            time.sleep(0.05)
    return sent[-1] if sent else {"failed": True, "why": "timeout"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", help="an MKV film to play instead of the fixtures")
    parser.add_argument("--webkit", help="the WebKit executable (an older Playwright's)")
    parser.add_argument("--seek", type=float, default=6.2, help="where to seek to (s)")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    web_dir = Path(tempfile.mkdtemp())
    (web_dir / "index.html").write_text("<!doctype html><title>film check</title><body>")
    hub = RemoteHub(lambda event: None, web_dir, ports=[0], host="127.0.0.1")
    hub.start({"version": 1, "enabled": True, "relay": False})
    hub.ready.wait(5)
    home = f"http://127.0.0.1:{hub.port}/"
    films: list[tuple[str, str]] = []
    if args.url:
        films.append(("the film", args.url))
    else:
        provider = Provider()
        for path in sorted(FILMS.glob("*.mkv")):
            films.append((path.name, f"http://127.0.0.1:{provider.port}/movie/{path.name}"))
    failed = 0
    with (
        mock.patch.object(hub._core, "home_url", lambda: home),
        sync_playwright() as playwright,
    ):
        executable: Optional[str] = args.webkit
        browser = playwright.webkit.launch(headless=True, executable_path=executable)
        page = browser.new_page()
        page.goto(home)
        for name, url in films:
            message = hls_of(hub, url)
            if message.get("failed"):
                result = f"refused ({message.get('why')})"
            else:
                result = page.evaluate(PLAY, [message["url"], args.seek])
            ok = result.startswith("plays")
            failed += not ok
            print(f"{'PASS' if ok else 'FAIL'} {name}: {result}")
        browser.close()
    hub.stop()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
