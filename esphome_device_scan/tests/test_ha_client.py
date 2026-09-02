"""The Home Assistant WebSocket client.

Every call this add-on makes is a WebSocket command. That is not a style
choice: the registries have no REST equivalent, and neither does the
in-progress-flow list -- `GET /api/config/config_entries/flow` explicitly
raises 405 because that URL exists only to *start* a flow with POST.
"""

from __future__ import annotations

import pytest

from app.ha_client import (
    CMD_CONFIG_ENTRIES,
    CMD_DEVICE_REGISTRY,
    CMD_FLOW_PROGRESS,
    HaApiError,
    SupervisorHaClient,
)


class FakeSocket:
    """Replays the auth handshake, then answers one command."""

    def __init__(self, result, *, success: bool = True) -> None:
        self.result = result
        self.success = success
        self.sent: list[dict] = []
        self._replies = [{"type": "auth_required"}, {"type": "auth_ok"}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive_json(self):
        if self._replies:
            return self._replies.pop(0)
        return {
            "id": 1, "type": "result", "success": self.success,
            "result": self.result,
            "error": {"code": "nope", "message": "denied"},
        }


class FakeSession:
    def __init__(self, socket: FakeSocket) -> None:
        self.socket = socket
        self.ws_urls: list[str] = []
        self.get_urls: list[str] = []

    def ws_connect(self, url, **kwargs):
        self.ws_urls.append(url)
        return self.socket

    def get(self, url, **kwargs):
        # Any REST call is a bug: nothing this client needs is served over it.
        self.get_urls.append(url)
        raise AssertionError(f"unexpected REST request to {url}")


def client_for(socket: FakeSocket) -> tuple[SupervisorHaClient, FakeSession]:
    session = FakeSession(socket)
    return SupervisorHaClient(session, token="tok"), session


# -- discovery flows ---------------------------------------------------------


async def test_discovery_flows_use_the_websocket_command() -> None:
    """Regression: this used `GET /api/config/config_entries/flow`, which Home
    Assistant answers with 405 -- that URL only accepts POST, to start a flow."""
    socket = FakeSocket([
        {"handler": "esphome", "context": {"unique_id": "aabbccddeeff"}},
        {"handler": "hue", "context": {}},
    ])
    client, session = client_for(socket)

    flows = await client.list_discovery_flows()

    assert session.get_urls == [], "no REST call should be made"
    assert socket.sent[-1]["type"] == CMD_FLOW_PROGRESS
    assert [f["handler"] for f in flows] == ["esphome"]


async def test_the_flow_command_name_is_the_documented_one() -> None:
    assert CMD_FLOW_PROGRESS == "config_entries/flow/progress"


async def test_a_failed_flow_query_degrades_to_empty() -> None:
    """A scan that partially succeeds beats one that aborts."""
    client, _ = client_for(FakeSocket(None, success=False))
    assert await client.list_discovery_flows() == []


# -- the other commands ------------------------------------------------------


async def test_config_entries_are_filtered_by_domain() -> None:
    socket = FakeSocket([
        {"entry_id": "a", "domain": "esphome"},
        {"entry_id": "b", "domain": "hue"},
    ])
    client, _ = client_for(socket)

    entries = await client.list_config_entries("esphome")

    assert socket.sent[-1]["type"] == CMD_CONFIG_ENTRIES
    assert [e["entry_id"] for e in entries] == ["a"]


async def test_non_dict_rows_are_dropped() -> None:
    client, _ = client_for(FakeSocket(["nonsense", None, {"id": "d1"}]))
    assert await client.list_devices() == [{"id": "d1"}]


async def test_an_unexpected_payload_shape_degrades_to_empty() -> None:
    client, _ = client_for(FakeSocket({"not": "a list"}))
    assert await client.list_devices() == []


# -- transport ---------------------------------------------------------------


async def test_the_websocket_url_is_derived_from_the_base() -> None:
    socket = FakeSocket([])
    client, session = client_for(socket)
    await client.list_devices()

    assert session.ws_urls == ["ws://supervisor/core/websocket"]
    assert socket.sent[0] == {"type": "auth", "access_token": "tok"}
    assert socket.sent[1]["type"] == CMD_DEVICE_REGISTRY


async def test_https_becomes_wss() -> None:
    session = FakeSession(FakeSocket([]))
    client = SupervisorHaClient(session, base_url="https://ha.example/core", token="t")
    await client.list_devices()
    assert session.ws_urls == ["wss://ha.example/core/websocket"]


async def test_a_missing_token_is_explained_not_silently_empty() -> None:
    client = SupervisorHaClient(FakeSession(FakeSocket([])))
    with pytest.raises(HaApiError, match="homeassistant_api"):
        await client._ws_command(CMD_DEVICE_REGISTRY)


async def test_verify_reports_reachability() -> None:
    ok, _ = client_for(FakeSocket([]))
    assert await ok.verify() is True

    bad, _ = client_for(FakeSocket(None, success=False))
    assert await bad.verify() is False
