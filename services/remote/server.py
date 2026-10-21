"""The phone remote's home door: QiTV serving the remote page and its WebSocket on the LAN.

Runs on the remote hub's event loop (services/remote/hub.py). Only addresses of a home network
may connect; the page's files come from one folder, never from outside it.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable

from aiohttp import web

from services.remote.net import is_private

if TYPE_CHECKING:
    from services.remote.hub import HubCore

logger = logging.getLogger(__name__)

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/manifest+json", ".webmanifest")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@web.middleware
async def private_only(request: web.Request, handler: Handler) -> web.StreamResponse:
    if not is_private(request.remote):
        return web.Response(status=403)
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
            return web.Response(status=404)
        headers = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
        return web.FileResponse(found, headers=headers)

    async def door(request: web.Request) -> web.StreamResponse:
        # The relay serves the same page with a static door.json saying "relay".
        return web.json_response({"door": "home"}, headers={"Cache-Control": "no-store"})

    app = web.Application(middlewares=[private_only])
    app.router.add_get("/ws", core.handle_ws)
    app.router.add_get("/door.json", door)
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
