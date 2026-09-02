"""aiohttp server for the ingress panel.

Two ingress-specific concerns are handled here:

* **Access control.** Supervisor proxies ingress traffic from a fixed address
  (172.30.32.2). Anything else reaching this port is bypassing Home Assistant's
  authentication, so it is refused.
* **Path prefix.** Ingress serves the panel under a generated prefix and passes
  it as ``X-Ingress-Path``. Rather than rewriting URLs server-side, the page is
  written with relative URLs and given a ``<base href>`` built from that header,
  so the browser resolves everything correctly on its own.
* **Asset freshness.** The page and its script must always be the same version.
  A browser holding a cached ``app.js`` from before an update, against freshly
  served HTML, breaks the panel outright -- the old script looks up elements the
  new page no longer has. Asset URLs therefore carry a hash of their contents,
  so an update changes the URL and the browser has to fetch it.
"""

from __future__ import annotations

import hashlib
import logging
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from ..settings import INGRESS_PEER, Settings
from . import api
from .keys import GENERATOR, LOGS, ORCHESTRATOR, SCHEDULER, SETTINGS

if TYPE_CHECKING:
    from ..generator import YamlGenerator
    from ..logbuf import LogBuffer
    from ..orchestrator import ScanOrchestrator
    from ..scheduler import ScanScheduler

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: Assets whose content is folded into the cache-busting token.
_VERSIONED_ASSETS = ("app.js", "styles.css")


@lru_cache(maxsize=1)
def asset_version() -> str:
    """Short hash of the panel's assets, used to bust browser caches.

    Content-derived rather than the add-on version, so an edit during
    development is picked up too, and two builds with identical assets keep the
    same URL. Computed once: the files cannot change under a running add-on.
    """
    digest = hashlib.sha256()
    for name in _VERSIONED_ASSETS:
        try:
            digest.update((STATIC_DIR / name).read_bytes())
        except OSError:  # pragma: no cover - a missing asset is caught on serve
            digest.update(name.encode())
    return digest.hexdigest()[:12]


@web.middleware
async def ingress_only_middleware(request: web.Request, handler):
    """Refuse requests that did not come through the Supervisor ingress proxy."""
    settings = request.app[SETTINGS]
    if not settings.enforce_ingress_peer:
        return await handler(request)

    peer = request.remote
    if peer != INGRESS_PEER:
        _LOGGER.warning("Rejected request from %s (ingress only)", peer)
        raise web.HTTPForbidden(text="This add-on is reachable through Home Assistant ingress only.")
    return await handler(request)


@web.middleware
async def error_middleware(request: web.Request, handler):
    """Return JSON for API errors so the frontend can display the reason."""
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if request.path.startswith("/api/"):
            return web.json_response({"error": exc.reason}, status=exc.status)
        raise
    except Exception as err:
        _LOGGER.exception("Unhandled error serving %s", request.path)
        if request.path.startswith("/api/"):
            return web.json_response({"error": str(err)}, status=500)
        raise


async def index_handler(request: web.Request) -> web.Response:
    """Serve the panel, injecting the ingress base path."""
    try:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    except OSError as err:
        _LOGGER.error("Cannot read panel HTML: %s", err)
        raise web.HTTPInternalServerError(text="Panel assets are missing.") from err

    # Ingress hands us e.g. /api/hassio_ingress/<token>; the trailing slash
    # matters so relative URLs resolve inside the prefix rather than beside it.
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    base_href = f"{ingress_path}/" if ingress_path else "./"

    # The value goes straight into an HTML attribute. Only the Supervisor proxy
    # can reach this handler, and it sets the header itself, but escaping a
    # request header before echoing it into markup costs nothing.
    html = html.replace("{{BASE_HREF}}", escape(base_href, quote=True))
    html = html.replace("{{ASSET_VERSION}}", asset_version())

    # The HTML itself must never be cached: it carries the asset URLs, so a
    # stale copy would keep pointing at stale assets and defeat the whole point.
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


def create_app(
    settings: Settings,
    orchestrator: ScanOrchestrator,
    scheduler: ScanScheduler,
    generator: YamlGenerator,
    logs: LogBuffer,
) -> web.Application:
    """Build the aiohttp application with its dependencies attached."""
    app = web.Application(middlewares=[ingress_only_middleware, error_middleware])

    # aiohttp's app mapping is the composition root for request handlers;
    # handlers pull collaborators from here rather than importing globals.
    app[SETTINGS] = settings
    app[ORCHESTRATOR] = orchestrator
    app[SCHEDULER] = scheduler
    app[GENERATOR] = generator
    app[LOGS] = logs

    app.add_routes(api.routes)
    app.router.add_get("/", index_handler)
    app.router.add_static("/static/", STATIC_DIR, name="static")
    return app


async def start_server(app: web.Application, settings: Settings) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.web_host, settings.web_port)
    await site.start()
    _LOGGER.info("Panel listening on %s:%s", settings.web_host, settings.web_port)
    return runner
