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
Four strategies, best first. Each is tried until one answers, so an unusual
install still works without the user configuring anything:

1. **Supervisor discovery.** The ESPHome add-on declares ``discovery: [esphome]``
   and publishes a record carrying its ``host`` and ``port``. This is the same
   source Home Assistant's own ESPHome integration uses to find the dashboard,
   so it is authoritative, it works whatever repository the add-on came from,
   and ``/discovery`` needs *no* Supervisor role at all.
2. **The discovery service map.** Even with no active record, ``/discovery``
   lists which add-on slugs provide each service, which gives us a slug to ask
   about.
3. **Known slugs via ``/addons/<slug>/info``**, which is permitted at
   ``hassio_role: default`` and returns the container hostname. (Listing *all*
   add-ons would need ``manager`` -- far more privilege than finding one
   dashboard warrants, so we ask about specific slugs instead.)
4. **Direct hostname probes**, since Supervisor puts every add-on on one Docker
   network at a hostname derived from its slug (``5c53de3b_esphome`` ->
   ``5c53de3b-esphome``).

``esphome_dashboard_url`` overrides all of it, which is also how you point at an
ESPHome dashboard running outside Home Assistant entirely. A successful answer
is cached; a failure is not, so a dashboard that starts later is picked up
without restarting this add-on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import aiohttp

_LOGGER = logging.getLogger(__name__)

#: Port the Device Builder dashboard listens on inside its container. This is
#: the container-internal port, so a host port mapping does not change it.
DASHBOARD_PORT = 6052

#: The service name the ESPHome add-on publishes under (``discovery: [esphome]``
#: in its manifest). Home Assistant's own ESPHome integration keys off this.
DISCOVERY_SERVICE = "esphome"

#: Fallback slugs, used only when Supervisor discovery says nothing. The
#: hostname is the slug with underscores as hyphens.
KNOWN_SLUGS = (
    "5c53de3b_esphome",       # official ESPHome add-on repository
    "5c53de3b_esphome-dev",   # its beta channel
    "5c53de3b_esphome-beta",
    "a0d7b954_esphome",       # older community repository
    "core_esphome",
    "local_esphome",
)

#: The Home Assistant Docker network's gateway -- i.e. the host itself, as seen
#: from inside any add-on container (Supervisor's 172.30.32.0/23, address .1).
#:
#: This matters more than it looks: the ESPHome add-on runs with
#: ``host_network: true``, so it is *not* on the bridge network and its
#: container hostname does not route. It binds 6052 in the host's network
#: namespace, and the gateway is how a sibling container reaches that.
HOST_GATEWAY = "172.30.32.1"

#: Bare hostnames worth trying when nothing else worked -- a local build, or a
#: dashboard running as a plain container beside Home Assistant.
FALLBACK_HOSTS = (
    "esphome",
    "homeassistant-esphome",
    "esphome-device-builder",
    "host.docker.internal",
)

#: The upload job's port value meaning "over the air". Device Builder's
#: models.OTA_PORT; the esphome CLI resolves the address from the config.
OTA_PORT = "OTA"

#: Endpoints tried when probing a candidate. ``/version`` is the cheapest and
#: oldest; ``/devices`` is the documented legacy endpoint. Trying several means
#: one being retired does not break detection.
PROBE_PATHS = ("/version", "/devices", "/")

CONNECT_TIMEOUT = aiohttp.ClientTimeout(total=10)

#: Per-candidate probe budget. Kept short because several may be tried.
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=4)

#: Ceiling on a whole discovery sweep, so a slow network cannot make the panel
#: hang for however long every candidate takes to time out.
DISCOVERY_BUDGET_SECONDS = 25

#: A build from cold can genuinely take several minutes on slow hardware.
FLASH_TIMEOUT_SECONDS = 30 * 60


class DashboardError(Exception):
    """The ESPHome Device Builder add-on could not be reached or used."""


@dataclass(frozen=True)
class DashboardLocation:
    """Where the Device Builder dashboard was found, and how."""

    base_url: str
    #: How it was found: option / discovery / discovery-services / addon-info /
    #: hostname-probe. Surfaced in the panel so a surprise is diagnosable.
    source: str
    slug: str | None = None

    def describe(self) -> str:
        via = {
            "option": "the esphome_dashboard_url option",
            "discovery": "Supervisor discovery",
            "discovery-services": "the Supervisor discovery service map",
            "addon-info": "a known add-on slug",
            "host-network": "the host (the add-on uses host networking)",
            "hostname-probe": "a hostname probe",
        }.get(self.source, self.source)
        slug = f" ({self.slug})" if self.slug else ""
        return f"{self.base_url} via {via}{slug}"


class EsphomeDashboardClient:
    """Drives builds and OTA uploads on the Device Builder add-on."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        configured_url: str | None = None,
        supervisor_token: str | None = None,
    ) -> None:
        self._session = session
        self._configured_url = self._normalise(configured_url)
        self._token = supervisor_token
        self._location: DashboardLocation | None = None
        self._attempts: list[str] = []
        self._extra_host_addresses: list[str] = []
        #: One discovery sweep at a time; several flashes starting together
        #: should not each probe every candidate.
        self._lock = asyncio.Lock()

    @property
    def location(self) -> DashboardLocation | None:
        """Where we last found the dashboard, if we have looked."""
        return self._location

    @property
    def attempts(self) -> list[str]:
        """What the last sweep tried, for the panel's diagnostics."""
        return list(self._attempts)

    def forget(self) -> None:
        """Drop the cached location, so the next call rediscovers."""
        self._location = None

    @staticmethod
    def _normalise(url: str | None) -> str:
        """Accept ``host``, ``host:port`` or a full URL from the option."""
        raw = (url or "").strip().rstrip("/")
        if not raw:
            return ""
        if "://" not in raw:
            raw = f"http://{raw}"
        # A bare host with no port means the dashboard's default.
        without_scheme = raw.split("://", 1)[1]
        if ":" not in without_scheme.split("/", 1)[0]:
            raw = f"{raw}:{DASHBOARD_PORT}"
        return raw

    # -- discovery -------------------------------------------------------

    async def locate(self, *, refresh: bool = False) -> DashboardLocation:
        """Find the Device Builder dashboard, caching a successful answer.

        Failures are deliberately not cached: an ESPHome add-on that starts
        after this one should be picked up on the next attempt rather than
        needing a restart.
        """
        async with self._lock:
            if self._location is not None and not refresh:
                return self._location

            self._attempts = []
            try:
                location = await asyncio.wait_for(
                    self._discover(), timeout=DISCOVERY_BUDGET_SECONDS
                )
            except TimeoutError:
                raise DashboardError(
                    "Timed out looking for the ESPHome Device Builder add-on. "
                    f"Tried: {', '.join(self._attempts) or 'nothing'}. Set "
                    "'esphome_dashboard_url' to point at it directly."
                ) from None

            if location is None:
                raise DashboardError(
                    "Could not find the ESPHome Device Builder add-on. Make sure "
                    "it is installed and running. Tried: "
                    f"{', '.join(self._attempts) or 'nothing'}. If it is running "
                    "somewhere unusual, set 'esphome_dashboard_url' (for example "
                    "http://5c53de3b-esphome:6052)."
                )

            self._location = location
            _LOGGER.info("Using ESPHome Device Builder at %s", location.describe())
            return location

    async def _discover(self) -> DashboardLocation | None:
        """Work through the strategies, best first."""
        if self._configured_url:
            # An explicit setting is honoured even if the probe fails, so the
            # error names the configured address rather than silently moving on.
            if await self._reachable(self._configured_url):
                return DashboardLocation(self._configured_url, "option")
            raise DashboardError(
                f"The configured ESPHome dashboard at {self._configured_url} did "
                "not respond. Check 'esphome_dashboard_url', or clear it to let "
                "the add-on discover the dashboard itself."
            )

        found = await self._from_supervisor_discovery()
        if found is not None:
            return found

        found = await self._from_addon_info()
        if found is not None:
            return found

        # The ESPHome add-on is host-network by default, so the host itself is
        # the single most likely place for it -- ahead of any container name.
        for host in await self._host_addresses():
            base = f"http://{host}:{DASHBOARD_PORT}"
            if await self._reachable(base):
                return DashboardLocation(base, "host-network")

        for host in [s.replace("_", "-") for s in KNOWN_SLUGS] + list(FALLBACK_HOSTS):
            base = f"http://{host}:{DASHBOARD_PORT}"
            if await self._reachable(base):
                return DashboardLocation(base, "hostname-probe")

        return None

    async def _from_addon_info(self) -> DashboardLocation | None:
        """Ask Supervisor about each candidate slug and probe what it reports.

        An add-on running with ``host_network: true`` -- which the ESPHome
        add-on does -- has no useful container address, so for those we go
        straight to the host.
        """
        for slug in await self._candidate_slugs():
            data = await self._supervisor_get(f"/addons/{slug}/info")
            if data is None:
                continue

            state = data.get("state")
            if state in (None, "unknown"):
                # Supervisor answers for store add-ons that are not installed;
                # they report state "unknown". Not an error, just not it.
                self._attempts.append(f"{slug} (not installed)")
                continue
            self._attempts.append(f"add-on {slug} ({state})")
            if state != "started":
                _LOGGER.warning(
                    "ESPHome add-on '%s' is installed but %s -- start it to flash.",
                    slug, state,
                )
                continue

            if data.get("host_network") and not self._extra_host_addresses:
                # Learn the host's real interface addresses before probing, so
                # a setup that does not route via the Docker gateway still works.
                await self._host_addresses()

            for host in self._addresses_for(data, slug):
                base = f"http://{host}:{DASHBOARD_PORT}"
                if await self._reachable(base):
                    return DashboardLocation(base, "addon-info", slug)
        return None

    def _addresses_for(self, data: dict, slug: str) -> list[str]:
        """Addresses worth trying for an add-on, most likely first."""
        if data.get("host_network"):
            # Its ports live in the host's namespace, so the container name and
            # ip_address are meaningless -- the host is what to talk to.
            return [HOST_GATEWAY, *self._extra_host_addresses]

        addresses = []
        hostname = data.get("hostname")
        if isinstance(hostname, str) and hostname:
            addresses.append(hostname)
        ip_address = data.get("ip_address")
        if isinstance(ip_address, str) and ip_address and not ip_address.startswith("0."):
            addresses.append(ip_address)
        addresses.append(slug.replace("_", "-"))
        return addresses

    async def _host_addresses(self) -> list[str]:
        """The host's own addresses, for reaching a host-network add-on.

        The Docker gateway always works from inside a container; the host's
        real interface addresses are asked for as well, since some setups route
        differently. ``/network/info`` is permitted at ``hassio_role: default``.
        """
        addresses = [HOST_GATEWAY]
        data = await self._supervisor_get("/network/info")
        for interface in (data or {}).get("interfaces") or []:
            if not isinstance(interface, dict) or not interface.get("enabled"):
                continue
            ipv4 = interface.get("ipv4") or {}
            for cidr in ipv4.get("address") or []:
                address = str(cidr).split("/")[0]
                if address and address not in addresses:
                    addresses.append(address)
        self._extra_host_addresses = addresses[1:]
        return addresses

    async def _supervisor_get(self, path: str) -> dict | None:
        """GET a Supervisor endpoint, returning its ``data`` payload."""
        if not self._token:
            return None
        try:
            async with self._session.get(
                f"http://supervisor{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=PROBE_TIMEOUT,
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Supervisor %s failed: %s", path, err)
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    async def _from_supervisor_discovery(self) -> DashboardLocation | None:
        """Read the ESPHome add-on's own published host and port.

        This is what Home Assistant's ESPHome integration uses, and needs no
        Supervisor role, so it is both the most authoritative answer and the
        cheapest one to ask for.
        """
        self._attempts.append("Supervisor discovery")
        data = await self._supervisor_get("/discovery")
        if data is None:
            return None

        for message in data.get("discovery") or []:
            if not isinstance(message, dict):
                continue
            if message.get("service") != DISCOVERY_SERVICE:
                continue
            config = message.get("config") or {}
            host = config.get("host")
            port = config.get("port") or DASHBOARD_PORT
            if not host:
                continue
            # Supervisor renamed this field from "addon" to "app"; accept both.
            slug = message.get("addon") or message.get("app")
            base = f"http://{host}:{port}"
            if await self._reachable(base):
                return DashboardLocation(base, "discovery", slug)
        return None

    async def _candidate_slugs(self) -> list[str]:
        """Slugs to ask about: whatever discovery names, then the known ones."""
        slugs: list[str] = []
        data = await self._supervisor_get("/discovery")
        if data:
            provided = (data.get("services") or {}).get(DISCOVERY_SERVICE) or []
            slugs.extend(s for s in provided if isinstance(s, str))
            if slugs:
                self._attempts.append(f"discovery service map ({', '.join(slugs)})")

        slugs.extend(slug for slug in KNOWN_SLUGS if slug not in slugs)
        return slugs

    async def _hostname_from_supervisor(self, slug: str) -> str | None:
        """Ask Supervisor for an add-on's container hostname.

        ``/addons/<slug>/info`` is reachable at ``hassio_role: default``; the
        add-on *list* is not, which is why this asks about specific slugs.
        """
        data = await self._supervisor_get(f"/addons/{slug}/info")
        if data is None:
            return None

        self._attempts.append(f"add-on {slug}")
        state = data.get("state")
        if state and state != "started":
            _LOGGER.warning(
                "ESPHome add-on '%s' is installed but %s; start it to flash.",
                slug, state,
            )
        hostname = data.get("hostname") or slug.replace("_", "-")
        return hostname if isinstance(hostname, str) and hostname else None

    async def _reachable(self, base_url: str) -> bool:
        """Whether something is answering HTTP at ``base_url``.

        Several paths are tried so retiring one endpoint does not break
        detection, and any non-5xx answer counts: a 401 still proves the
        dashboard is there, just behind authentication.
        """
        for path in PROBE_PATHS:
            try:
                async with self._session.get(
                    f"{base_url}{path}", timeout=PROBE_TIMEOUT,
                    allow_redirects=True,
                ) as response:
                    if response.status < 500:
                        _LOGGER.debug("Probe %s%s -> %s", base_url, path, response.status)
                        return True
            except (aiohttp.ClientError, TimeoutError):
                continue
        self._attempts.append(f"{base_url} (no response)")
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
            # The address we had stopped working -- the add-on may have
            # restarted on a new container IP. Drop it so the next attempt
            # rediscovers instead of failing the same way forever.
            self.forget()
            raise DashboardError(
                f"Lost contact with the ESPHome dashboard during {command} "
                f"of {configuration}: {err}"
            ) from err


    async def diagnostics(self) -> dict[str, object]:
        """Where the dashboard is, or why it could not be found.

        Never raises: this backs a status panel, and a panel that errors out
        tells the user less than one showing what was tried.
        """
        try:
            location = await self.locate(refresh=True)
        except DashboardError as err:
            return {
                "found": False,
                "error": str(err),
                "attempts": self.attempts,
                "configured": self._configured_url or None,
            }
        return {
            "found": True,
            "base_url": location.base_url,
            "source": location.source,
            "slug": location.slug,
            "description": location.describe(),
            "attempts": self.attempts,
            "configured": self._configured_url or None,
        }
