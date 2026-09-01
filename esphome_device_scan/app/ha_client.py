"""Home Assistant access through the Supervisor proxy.

Add-ons reach Core at ``http://supervisor/core/api/...`` (REST) and
``ws://supervisor/core/websocket`` (WebSocket), authenticating with the
``SUPERVISOR_TOKEN`` environment variable as a bearer token.

The device registry is only available over WebSocket, and it is the one surface
that carries what this add-on needs most: the ESPHome integration registers each
device with ``connections={(CONNECTION_NETWORK_MAC, mac)}``, so every adopted
device arrives with its MAC already attached.

:class:`HaApi` is the injection seam -- the discovery service depends on the
protocol, never on this module, so tests run with a fake and touch no network.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import aiohttp

_LOGGER = logging.getLogger(__name__)

#: WebSocket commands. These are the commands the HA frontend itself uses;
#: they are stable but undocumented, so `scripts/probe_ha.py` exists to confirm
#: them against a live instance, and every call degrades to [] on failure.
CMD_DEVICE_REGISTRY = "config/device_registry/list"
CMD_ENTITY_REGISTRY = "config/entity_registry/list"
CMD_CONFIG_ENTRIES = "config_entries/get"
CMD_GET_STATES = "get_states"

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)

#: Ceiling on one full authenticate-send-receive exchange. `get_states` on a
#: large instance is the slow one, so this is generous rather than tight.
COMMAND_TIMEOUT_SECONDS = 60


class HaApiError(Exception):
    """Raised when Home Assistant cannot be reached or refuses us."""


class HaApi(Protocol):
    """Everything discovery needs from Home Assistant."""

    async def list_config_entries(self, domain: str = "esphome") -> list[dict[str, Any]]:
        ...

    async def list_devices(self) -> list[dict[str, Any]]:
        ...

    async def list_entities(self) -> list[dict[str, Any]]:
        ...

    async def list_states(self) -> list[dict[str, Any]]:
        ...

    async def list_discovery_flows(self) -> list[dict[str, Any]]:
        ...


class SupervisorHaClient:
    """HaApi backed by the Supervisor's Core proxy."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = "http://supervisor/core",
        token: str | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token
        # One command at a time: the request/response correlation below is
        # simple by design, and scans are not latency-critical.
        self._lock = asyncio.Lock()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    @property
    def _ws_url(self) -> str:
        url = f"{self._base_url}/websocket"
        if url.startswith("https://"):
            return "wss://" + url[len("https://") :]
        if url.startswith("http://"):
            return "ws://" + url[len("http://") :]
        return url

    # -- public API ------------------------------------------------------

    async def list_config_entries(self, domain: str = "esphome") -> list[dict[str, Any]]:
        entries = await self._ws_command_safe(CMD_CONFIG_ENTRIES, "config entries")
        return [e for e in entries if e.get("domain") == domain]

    async def list_devices(self) -> list[dict[str, Any]]:
        return await self._ws_command_safe(CMD_DEVICE_REGISTRY, "device registry")

    async def list_entities(self) -> list[dict[str, Any]]:
        return await self._ws_command_safe(CMD_ENTITY_REGISTRY, "entity registry")

    async def list_states(self) -> list[dict[str, Any]]:
        return await self._ws_command_safe(CMD_GET_STATES, "states")

    async def list_discovery_flows(self) -> list[dict[str, Any]]:
        """Discovery flows Home Assistant has started but nobody has finished.

        These are devices HA can see but has not adopted. ESPHome's zeroconf
        step sets the flow's unique_id to the MAC, so even an unadopted device
        arrives identifiable.
        """
        try:
            flows = await self._rest_get("/api/config/config_entries/flow")
        except HaApiError as err:
            _LOGGER.warning("Could not list discovery flows: %s", err)
            return []
        if not isinstance(flows, list):
            return []
        return [f for f in flows if isinstance(f, dict) and f.get("handler") == "esphome"]

    async def verify(self) -> bool:
        """Cheap connectivity check used at startup for a clear early error."""
        try:
            await self._ws_command(CMD_CONFIG_ENTRIES)
        except HaApiError as err:
            _LOGGER.error("Home Assistant API check failed: %s", err)
            return False
        return True

    # -- transport -------------------------------------------------------

    async def _ws_command_safe(self, command: str, label: str) -> list[dict[str, Any]]:
        """Run a command, degrading to an empty list with a clear log line.

        A scan that partially succeeds is far more useful than one that aborts,
        so a failure here is reported and stepped over rather than raised.
        """
        try:
            result = await self._ws_command(command)
        except HaApiError as err:
            _LOGGER.warning("Could not fetch %s: %s", label, err)
            return []
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        _LOGGER.warning("Unexpected %s payload: %r", label, type(result).__name__)
        return []

    async def _ws_command(self, command: str, **payload: Any) -> Any:
        """Open a WebSocket, authenticate, run one command, close."""
        if not self._token:
            raise HaApiError(
                "SUPERVISOR_TOKEN is not set. The add-on needs 'homeassistant_api: true'."
            )

        async with self._lock:
            try:
                async with self._session.ws_connect(
                    self._ws_url, timeout=DEFAULT_TIMEOUT, heartbeat=None
                ) as socket:
                    # The whole exchange is bounded. ws_connect's timeout covers
                    # only the handshake, so without this an unresponsive Home
                    # Assistant would block here forever -- and because the lock
                    # is held, every later scan would queue up behind it.
                    return await asyncio.wait_for(
                        self._exchange(socket, command, payload),
                        timeout=COMMAND_TIMEOUT_SECONDS,
                    )
            except HaApiError:
                raise
            except TimeoutError as err:
                raise HaApiError(
                    f"{command} timed out after {COMMAND_TIMEOUT_SECONDS}s"
                ) from err
            except (aiohttp.ClientError, ValueError, TypeError) as err:
                # TypeError is what aiohttp raises from receive_json() when the
                # peer closes mid-exchange, so it is a transport failure here.
                raise HaApiError(f"WebSocket {command}: {err}") from err

    async def _exchange(
        self,
        socket: aiohttp.ClientWebSocketResponse,
        command: str,
        payload: dict[str, Any],
    ) -> Any:
        """Authenticate, send one command, and return its result."""
        await self._authenticate(socket)
        await socket.send_json({"id": 1, "type": command, **payload})

        while True:
            message = await socket.receive_json()
            if message.get("type") != "result":
                continue  # events and pongs are not our business
            if not message.get("success", False):
                error = message.get("error") or {}
                raise HaApiError(
                    f"{command} failed: "
                    f"{error.get('code', 'unknown')} "
                    f"{error.get('message', '')}".strip()
                )
            return message.get("result")

    async def _authenticate(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        """Complete the auth_required -> auth -> auth_ok handshake."""
        greeting = await socket.receive_json()
        if greeting.get("type") != "auth_required":
            raise HaApiError(f"Unexpected greeting: {greeting.get('type')!r}")

        await socket.send_json({"type": "auth", "access_token": self._token})
        response = await socket.receive_json()
        if response.get("type") != "auth_ok":
            raise HaApiError(
                f"Authentication rejected: {response.get('message', response.get('type'))}"
            )

    async def _rest_get(self, path: str) -> Any:
        if not self._token:
            raise HaApiError("SUPERVISOR_TOKEN is not set.")
        url = f"{self._base_url}{path}"
        try:
            async with self._session.get(
                url, headers=self._headers, timeout=DEFAULT_TIMEOUT
            ) as response:
                if response.status == 401:
                    raise HaApiError(f"Unauthorized for {path}")
                if response.status >= 400:
                    raise HaApiError(f"HTTP {response.status} for {path}")
                return await response.json()
        except HaApiError:
            raise
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise HaApiError(f"GET {path}: {err}") from err
