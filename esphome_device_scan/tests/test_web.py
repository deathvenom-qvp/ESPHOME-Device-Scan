"""The ingress panel: routing, ingress access control, and the JSON API.

Runs a real aiohttp server over a real orchestrator wired to a fake Home
Assistant, so these exercise the same code path the panel hits in production.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import (
    FakeHaApi,
    generated_configs,
    make_entry,
    make_registry_device,
)

from app.config_store import EsphomeConfigStore
from app.discovery import DeviceDiscoveryService
from app.generator import YamlGenerator
from app.logbuf import LogBuffer
from app.orchestrator import ScanOrchestrator
from app.scheduler import ScanScheduler
from app.settings import INGRESS_PEER
from app.templates import TemplateRepository
from app.web.server import create_app


def build_stack(settings, ha: FakeHaApi):
    generator = YamlGenerator(settings.mac_policy, settings.name_add_mac_suffix_action)
    orchestrator = ScanOrchestrator(
        discovery=DeviceDiscoveryService(ha),
        store=EsphomeConfigStore(settings.esphome_config_dir),
        templates=TemplateRepository(settings.esphome_config_dir),
        generator=generator,
        settings=settings,
    )
    scheduler = ScanScheduler(orchestrator, 900, scan_on_startup=False)
    return orchestrator, scheduler, generator


@pytest.fixture
async def client(settings, parents_installed):
    """A test client with the ingress peer check relaxed."""
    local = replace(settings, enforce_ingress_peer=False)
    ha = FakeHaApi(
        config_entries=[make_entry(title="cloudbay-t-livingroom")],
        devices=[make_registry_device(mac="aa:bb:cc:dd:ee:ff")],
    )
    orchestrator, scheduler, generator = build_stack(local, ha)
    logs = LogBuffer().install()
    app = create_app(local, orchestrator, scheduler, generator, logs)

    try:
        async with TestClient(TestServer(app)) as test_client:
            yield test_client
    finally:
        logging.getLogger().removeHandler(logs)


# -- panel -----------------------------------------------------------------


async def test_index_is_served(client) -> None:
    response = await client.get("/")
    assert response.status == 200
    body = await response.text()
    assert "ESPHome Device Scan" in body
    assert "{{BASE_HREF}}" not in body  # placeholder must be substituted


async def test_ingress_path_becomes_the_base_href(client) -> None:
    """Relative URLs must resolve inside the ingress prefix."""
    response = await client.get(
        "/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"}
    )
    assert '<base href="/api/hassio_ingress/abc123/">' in await response.text()


async def test_base_href_falls_back_without_the_header(client) -> None:
    assert '<base href="./">' in await (await client.get("/")).text()


async def test_static_assets_are_served(client) -> None:
    assert (await client.get("/static/app.js")).status == 200
    assert (await client.get("/static/styles.css")).status == 200


# -- API -------------------------------------------------------------------


async def test_state_endpoint_shape(client) -> None:
    data = await (await client.get("/api/state")).json()

    assert "scan" in data
    assert "templates" in data
    assert "settings" in data
    assert {t["name"] for t in data["templates"]} == {
        "cloudbay-t.yaml", "switchboard.yaml"
    }


async def test_scan_endpoint_generates(client, esphome_dir) -> None:
    data = await (await client.post("/api/scan")).json()

    assert len(data["devices"]) == 1
    assert data["devices"][0]["node_name"] == "cloudbay-t-livingroom"
    assert data["devices"][0]["outcome"] == "generated"
    assert data["devices"][0]["mac"] == "AA:BB:CC:DD:EE:FF"
    assert (esphome_dir / "cloudbay-t-livingroom.yaml").exists()


async def test_yaml_endpoint_returns_the_file(client) -> None:
    await client.post("/api/scan")
    data = await (await client.get("/api/yaml/cloudbay-t-livingroom")).json()

    assert "name: cloudbay-t-livingroom" in data["content"]
    assert data["path"].endswith("cloudbay-t-livingroom.yaml")


async def test_yaml_endpoint_404s_for_an_unknown_device(client) -> None:
    response = await client.get("/api/yaml/nope")
    assert response.status == 404
    assert "No config file found" in (await response.json())["error"]


async def test_preview_renders_without_writing(client, esphome_dir) -> None:
    data = await (await client.get("/api/preview/cloudbay-t-livingroom")).json()

    assert "name: cloudbay-t-livingroom" in data["content"]
    assert data["template"] == "cloudbay-t.yaml"
    assert generated_configs(esphome_dir) == []  # nothing written


async def test_preview_404s_for_an_unknown_device(client) -> None:
    assert (await client.get("/api/preview/nope")).status == 404


async def test_generate_endpoint(client, esphome_dir) -> None:
    response = await client.post("/api/generate/cloudbay-t-livingroom")
    assert response.status == 200
    assert (await response.json())["outcome"] == "generated"
    assert (esphome_dir / "cloudbay-t-livingroom.yaml").exists()


async def test_generate_refuses_to_overwrite(client, esphome_dir) -> None:
    await client.post("/api/scan")
    (esphome_dir / "cloudbay-t-livingroom.yaml").write_text(
        "# hand edited\nesphome:\n  name: cloudbay-t-livingroom\n"
    )

    data = await (await client.post("/api/generate/cloudbay-t-livingroom")).json()
    assert data["outcome"] == "skipped_has_config"
    assert "# hand edited" in (esphome_dir / "cloudbay-t-livingroom.yaml").read_text()


async def test_regenerate_overwrites_and_backs_up(client, esphome_dir) -> None:
    await client.post("/api/scan")
    target = esphome_dir / "cloudbay-t-livingroom.yaml"
    target.write_text("# stale\n")

    data = await (await client.post("/api/regenerate/cloudbay-t-livingroom")).json()

    assert data["outcome"] == "regenerated"
    assert "name: cloudbay-t-livingroom" in target.read_text()
    assert len(list(esphome_dir.glob("cloudbay-t-livingroom.yaml.bak-*"))) == 1


async def test_generate_404s_for_an_unknown_device(client) -> None:
    response = await client.post("/api/generate/nope")
    assert response.status == 404
    assert "error" in await response.json()


async def test_logs_endpoint(client) -> None:
    await client.post("/api/scan")
    data = await (await client.get("/api/logs")).json()

    assert isinstance(data["entries"], list)
    assert any("Scan complete" in e["message"] for e in data["entries"])


async def test_logs_since_filters(client) -> None:
    await client.post("/api/scan")
    first = (await (await client.get("/api/logs")).json())["entries"]
    assert first

    # `since` must exclude everything already seen. It is not required to come
    # back empty: unrelated lines (aiohttp's own access log, for one) can be
    # recorded between the two requests.
    newest = max(e["id"] for e in first)
    later = await (await client.get(f"/api/logs?since={newest}")).json()
    assert all(e["id"] > newest for e in later["entries"])


async def test_health(client) -> None:
    assert (await (await client.get("/api/health")).json())["status"] == "ok"


async def test_unknown_api_route_returns_json(client) -> None:
    response = await client.get("/api/does-not-exist")
    assert response.status == 404
    assert "error" in await response.json()


# -- ingress access control -------------------------------------------------


async def test_non_ingress_requests_are_refused(settings) -> None:
    """Anything not proxied by Supervisor is bypassing HA's authentication."""
    strict = replace(settings, enforce_ingress_peer=True)
    orchestrator, scheduler, generator = build_stack(strict, FakeHaApi())
    app = create_app(strict, orchestrator, scheduler, generator, LogBuffer())

    async with TestClient(TestServer(app)) as test_client:
        # The test client connects from 127.0.0.1, not the ingress peer.
        assert (await test_client.get("/api/health")).status == 403


def test_the_ingress_peer_is_the_documented_one() -> None:
    assert INGRESS_PEER == "172.30.32.2"
