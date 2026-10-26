"""The phone remote's home door: QiTV serving the remote page and its WebSocket on the LAN.

Runs on the remote hub's event loop (services/remote/hub.py). Only addresses of a home network
may connect; the page's files come from one folder, never from outside it. The films phones play
(services/remote/films.py) and the recordings (services/remote/recorded.py) are served here too.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable

from aiohttp import web

from services.remote.films import add_routes as add_film_routes
from services.remote.net import is_private
from services.remote.recorded import add_routes as add_recording_routes

if TYPE_CHECKING:
    from services.remote.hub import HubCore

logger = logging.getLogger(__name__)

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/manifest+json", ".webmanifest")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# A hop from the relay's page that QiTV refuses (the phone's address is no home network's: a VPN's,
# say): the phone goes back to the relay, to stay there (#b.), instead of being left on an error.
# The hop's address ends #h.<ticket id>.<secret>.<tv>.<room>.<the relay's origin, base64url>.
REFUSED_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>QiTV</title><p>403: Forbidden</p>
<script>
try {
  const part = location.hash.slice(1).split(".");
  const b64 = (part[5] || "").replace(/-/g, "+").replace(/_/g, "/");
  const relay = atob(b64 + "===".slice((b64.length + 3) % 4));
  const tv = part[3] || "";
  const secure = /^https:\\/\\/[a-z0-9.-]+(:\\d+)?$/i.test(relay);
  if (part[0] === "h" && /^[\\w-]{1,64}$/.test(tv) && secure) location.replace(relay + "/#b." + tv);
} catch (e) {}
</script>
"""


@web.middleware
async def private_only(request: web.Request, handler: Handler) -> web.StreamResponse:
    if not is_private(request.remote):
        logger.info("Phone remote: refused %s, not a home network's address", request.remote)
        if request.path == "/":
            return web.Response(status=403, text=REFUSED_PAGE, content_type="text/html")
        return web.Response(status=403, text="403: Forbidden")  # text: an empty reply's a file
    return await handler(request)


def _page_file(web_dir: Path, path: str) -> Path | None:
    root = web_dir.resolve()
    try:
        target = (root / path).resolve()
    except OSError, ValueError:
        return None
    if target == root or root not in target.parents or not target.is_file():
        return None
    return target


async def start_home_door(
    core: HubCore, web_dir: Path, ports: Iterable[int], host: str = "0.0.0.0"
) -> tuple[web.AppRunner, int]:
    """Serve on the first free port of `ports` (0 = any); returns the runner and the port."""

    async def page(request: web.Request) -> web.StreamResponse:
        path = request.match_info.get("path") or "index.html"
        found = _page_file(web_dir, path)
        if found is None:
            return web.Response(status=404, text="404: Not Found")
        headers = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
        return web.FileResponse(found, headers=headers)

    async def door(request: web.Request) -> web.StreamResponse:
        # The relay serves the same page with a static door.json saying "relay".
        return web.json_response({"door": "home"}, headers={"Cache-Control": "no-store"})

    app = web.Application(middlewares=[private_only])
    app.router.add_get("/ws", core.handle_ws)
    app.router.add_get("/door.json", door)
    add_film_routes(app, core.films)
    add_recording_routes(app, core.films)  # 2.8: recordings, on the films' shelf
    app.router.add_get("/", page)
    app.router.add_get("/{path:.+}", page)
    # A phone's open socket must not keep QiTV from quitting (aiohttp waits 60 s by default).
    runner = web.AppRunner(app, access_log=None, handle_signals=False, shutdown_timeout=1.0)
    await runner.setup()
    for port in ports:
        site = web.TCPSite(runner, host, port)
        try:
            await site.start()
        except OSError as error:
            logger.info("Phone remote: port %s is busy (%s)", port, error)
            continue
        server = getattr(site, "_server", None)
        sockets = getattr(server, "sockets", None) or []
        bound = sockets[0].getsockname()[1] if sockets else port
        return runner, int(bound)
    await runner.cleanup()
    raise OSError("no free port for the phone remote")
