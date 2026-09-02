"""Client for the ESPHome Device Builder add-on.

Building and OTA-flashing firmware is the Device Builder add-on's job, not
ours -- it owns the toolchain, the build cache and the "Firmware tasks" panel.
This module drives it over the API it maintains for exactly this purpose.

Wire protocol (the "legacy" endpoints, kept for Home Assistant compatibility):

* ``GET /compile`` and ``GET /upload`` are **WebSocket** endpoints.
* client -> server, once: ``{"type": "spawn", "configuration": "kitchen.yaml",
  "port": "OTA"}``
* server -> client: ``{"event": "line", "data": "..."}`` per output line, then
  ``{"event": "exit", "code": <int>}``.

``port: "OTA"`` is Device Builder's own constant for an over-the-air upload;
the esphome CLI resolves the device's address itself from there.

Finding the add-on
------------------
Supervisor puts every add-on on one Docker network, reachable at a hostname
derived from its slug (``5c53de3b_esphome`` -> ``5c53de3b-esphome``). Listing
installed add-ons would need ``hassio_role: manager``, which is far more
privilege than this warrants, so instead we ask about a handful of known slugs
through ``/addons/<slug>/info`` -- allowed at the default role -- and fall back
to probing the hostnames directly. Either way the answer is cached, and the
``esphome_dashboard_url`` option overrides the whole thing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import aiohttp

_LOGGER = logging.getLogger(__name__)

#: Port the Device Builder dashboard listens on inside its container.
DASHBOARD_PORT = 6052

#: Slugs of the ESPHome add-on across the repositories people install it from,
#: most likely first. The hostname is the slug with underscores as hyphens.
KNOWN_SLUGS = (
    "5c53de3b_esphome",       # official ESPHome add-on repository
    "5c53de3b_esphome-dev",   # its beta channel
    "a0d7b954_esphome",       # older community repository
    "core_esphome",
    "local_esphome",
)

#: The upload job's port value meaning "over the air". Device Builder's
#: models.OTA_PORT; the esphome CLI resolves the address from the config.
OTA_PORT = "OTA"

CONNECT_TIMEOUT = aiohttp.ClientTimeout(total=10)

#: A build from cold can genuinely take several minutes on slow hardware.
FLASH_TIMEOUT_SECONDS = 30 * 60


class DashboardError(Exception):
    """The ESPHome Device Builder add-on could not be reached or used."""


@dataclass(frozen=True)
class DashboardLocation:
    """Where the Device Builder dashboard was found, and how."""

    base_url: str
    #: "option", "supervisor" or "probe" -- shown in the panel for diagnosis.
    source: str
    slug: str | None = None


class EsphomeDashboardClient:
    """Drives builds and OTA uploads on the Device Builder add-on."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        configured_url: str | None = None,
        supervisor_token: str | None = None,
    ) -> None:
        self._session = session
        self._configured_url = (configured_url or "").strip().rstrip("/")
        self._token = supervisor_token
        self._location: DashboardLocation | None = None

    @property
    def location(self) -> DashboardLocation | None:
        """Where we last found the dashboard, if we have looked."""
        return self._location

    def forget(self) -> None:
        """Drop the cached location, so the next call rediscovers."""
        self._location = None

    # -- discovery -------------------------------------------------------

    async def locate(self) -> DashboardLocation:
        """Find the Device Builder dashboard, caching the result."""
        if self._location is not None:
            return self._location

        if self._configured_url:
            location = DashboardLocation(self._configured_url, "option")
            if not await self._reachable(location.base_url):
                raise DashboardError(
                    f"The configured ESPHome dashboard at {location.base_url} did "
                    f"not respond. Check 'esphome_dashboard_url' in the add-on options."
                )
            self._location = location
            return location

        for slug in KNOWN_SLUGS:
            hostname = await self._hostname_from_supervisor(slug)
            candidates = [hostname] if hostname else [slug.replace("_", "-")]
            for host in candidates:
                base = f"http://{host}:{DASHBOARD_PORT}"
                if await self._reachable(base):
                    self._location = DashboardLocation(
                        base, "supervisor" if hostname else "probe", slug
                    )
                    _LOGGER.info("Found ESPHome Device Builder at %s", base)
                    return self._location

        raise DashboardError(
            "Could not find the ESPHome Device Builder add-on. Make sure it is "
            "installed and running, or set 'esphome_dashboard_url' in this "
            "add-on's options (for example http://5c53de3b-esphome:6052)."
        )

    async def _hostname_from_supervisor(self, slug: str) -> str | None:
        """Ask Supervisor for an add-on's container hostname.

        ``/addons/<slug>/info`` is reachable at ``hassio_role: default``; the
        add-on *list* is not, which is why this asks about specific slugs.
        """
        if not self._token:
            return None
        try:
            async with self._session.get(
                f"http://supervisor/addons/{slug}/info",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=CONNECT_TIMEOUT,
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None

        data = payload.get("data") or {}
        hostname = data.get("hostname")
        if data.get("state") and data.get("state") != "started":
            _LOGGER.warning("ESPHome add-on '%s' is %s", slug, data.get("state"))
        return hostname if isinstance(hostname, str) and hostname else None

    async def _reachable(self, base_url: str) -> bool:
        """Cheap liveness probe that does not depend on a specific route."""
        try:
            async with self._session.get(
                f"{base_url}/devices", timeout=CONNECT_TIMEOUT
            ) as response:
                # Any answer at all proves something is listening and speaking
                # HTTP; 401 would still mean the dashboard is there.
                return response.status < 500
        except (aiohttp.ClientError, TimeoutError):
            return False

    # -- commands --------------------------------------------------------

    async def upload(self, configuration: str) -> AsyncIterator[tuple[str, object]]:
        """Compile and OTA-flash one config, yielding progress as it goes.

        Yields ``("line", text)`` for each line of build output and finally
        ``("exit", code)``. Raises DashboardError if the dashboard cannot be
        reached at all.
        """
        async for event in self._spawn("upload", configuration, port=OTA_PORT):
            yield event

    async def compile(self, configuration: str) -> AsyncIterator[tuple[str, object]]:
        """Build firmware without flashing it."""
        async for event in self._spawn("compile", configuration):
            yield event

    async def _spawn(
        self, command: str, configuration: str, port: str | None = None
    ) -> AsyncIterator[tuple[str, object]]:
        location = await self.locate()
        url = location.base_url.replace("http://", "ws://", 1).replace(
            "https://", "wss://", 1
        )
        payload: dict[str, str] = {"type": "spawn", "configuration": configuration}
        if port is not None:
            payload["port"] = port

        try:
            async with self._session.ws_connect(
                f"{url}/{command}", timeout=CONNECT_TIMEOUT, heartbeat=30
            ) as socket:
                await socket.send_json(payload)

                deadline = asyncio.get_running_loop().time() + FLASH_TIMEOUT_SECONDS
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise DashboardError(
                            f"{command} of {configuration} exceeded "
                            f"{FLASH_TIMEOUT_SECONDS // 60} minutes"
                        )
                    message = await socket.receive(timeout=remaining)
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        # The dashboard closed the socket without an exit
                        # frame; treat that as a failure rather than success.
                        yield ("exit", 1)
                        return

                    event = message.json()
                    kind = event.get("event")
                    if kind == "line":
                        yield ("line", str(event.get("data", "")).rstrip("\n"))
                    elif kind == "exit":
                        yield ("exit", int(event.get("code", 1)))
                        return
        except DashboardError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise DashboardError(
                f"Lost contact with the ESPHome dashboard during {command} "
                f"of {configuration}: {err}"
            ) from err
