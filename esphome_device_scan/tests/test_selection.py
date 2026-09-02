"""Regenerating only the devices belonging to selected parent templates.

The panel lets each parent be ticked; these cover the path from that selection
through to the files it rewrites, and the validation that stops a bad selection
reaching the filesystem.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import FakeHaApi, make_entry, make_registry_device

from app.config_store import EsphomeConfigStore
from app.discovery import DeviceDiscoveryService
from app.generator import YamlGenerator
from app.logbuf import LogBuffer
from app.models import Outcome
from app.orchestrator import ScanOrchestrator
from app.scheduler import ScanScheduler
from app.templates import TemplateRepository
from app.web.server import create_app

CLOUDBAY = "esphome:\n  name: cloudbay-t-${mac}\n  name_add_mac_suffix: true\n"
SWITCHBOARD = "esphome:\n  name: switchboard-${mac}\n"


def three_device_ha() -> FakeHaApi:
    return FakeHaApi(
        config_entries=[
            make_entry("e1", "cloudbay-t-livingroom"),
            make_entry("e2", "cloudbay-t-porch"),
            make_entry("e3", "switchboard-hallway"),
        ],
        devices=[
            make_registry_device("d1", "e1", mac="aa:bb:cc:dd:ee:ff"),
            make_registry_device("d2", "e2", name="Porch", mac="11:22:33:44:55:66"),
            make_registry_device("d3", "e3", name="Hall", mac="99:88:77:66:55:44"),
        ],
    )


@pytest.fixture
def parented(esphome_dir):
    (esphome_dir / "cloudbay-t.yaml").write_text(CLOUDBAY)
    (esphome_dir / "switchboard.yaml").write_text(SWITCHBOARD)
    return esphome_dir


def build(settings) -> ScanOrchestrator:
    return ScanOrchestrator(
        discovery=DeviceDiscoveryService(three_device_ha()),
        store=EsphomeConfigStore(settings.esphome_config_dir),
        templates=TemplateRepository(settings.esphome_config_dir),
        generator=YamlGenerator(
            settings.mac_policy, settings.name_add_mac_suffix_action
        ),
        settings=settings,
    )


# -- selecting by template --------------------------------------------------


async def test_only_the_selected_template_is_regenerated(settings, parented) -> None:
    orchestrator = build(settings)
    await orchestrator.scan()
    for name in ("cloudbay-t-livingroom", "cloudbay-t-porch", "switchboard-hallway"):
        (parented / f"{name}.yaml").write_text("# stale\n")

    report = await orchestrator.regenerate_for_templates({"cloudbay-t.yaml"})

    assert report.count(Outcome.REGENERATED) == 2
    assert "# stale" not in (parented / "cloudbay-t-livingroom.yaml").read_text()
    # The unselected family is untouched.
    assert (parented / "switchboard-hallway.yaml").read_text() == "# stale\n"


async def test_devices_for_templates_maps_selection_to_devices(settings, parented) -> None:
    devices = await build(settings).devices_for_templates({"switchboard.yaml"})
    assert [d.node_name for d in devices] == ["switchboard-hallway"]


async def test_an_unknown_template_selects_nothing(settings, parented) -> None:
    assert await build(settings).devices_for_templates({"nope.yaml"}) == []


async def test_regenerating_a_selection_keeps_other_devices_listed(
    settings, parented
) -> None:
    """Replacing the cached report made unselected devices vanish from the
    panel's table until the next scan."""
    orchestrator = build(settings)
    await orchestrator.scan()
    assert len(orchestrator.last_report.devices) == 3

    await orchestrator.regenerate_for_templates({"cloudbay-t.yaml"})

    listed = {r.device.node_name for r in orchestrator.last_report.devices}
    assert listed == {
        "cloudbay-t-livingroom", "cloudbay-t-porch", "switchboard-hallway"
    }
    regenerated = {
        r.device.node_name
        for r in orchestrator.last_report.devices
        if r.outcome is Outcome.REGENERATED
    }
    assert regenerated == {"cloudbay-t-livingroom", "cloudbay-t-porch"}


# -- the endpoint -----------------------------------------------------------


@pytest.fixture
async def client(settings, parented):
    local = replace(settings, enforce_ingress_peer=False)
    generator = YamlGenerator(local.mac_policy, local.name_add_mac_suffix_action)
    orchestrator = ScanOrchestrator(
        discovery=DeviceDiscoveryService(three_device_ha()),
        store=EsphomeConfigStore(parented),
        templates=TemplateRepository(parented),
        generator=generator,
        settings=local,
    )
    scheduler = ScanScheduler(orchestrator, 900, scan_on_startup=False)
    logs = LogBuffer().install()
    app = create_app(local, orchestrator, scheduler, generator, logs)
    try:
        async with TestClient(TestServer(app)) as test_client:
            yield test_client
    finally:
        logging.getLogger().removeHandler(logs)


async def test_regenerate_selected_endpoint(client, parented) -> None:
    response = await client.post(
        "api/regenerate-selected", json={"templates": ["cloudbay-t.yaml"]}
    )
    data = await response.json()

    assert response.status == 200
    assert data["counts"]["regenerated"] == 2
    assert (parented / "cloudbay-t-livingroom.yaml").exists()
    assert not (parented / "switchboard-hallway.yaml").exists()


async def test_regenerate_selected_rejects_an_empty_selection(client) -> None:
    response = await client.post("api/regenerate-selected", json={"templates": []})
    assert response.status == 400


async def test_regenerate_selected_rejects_an_unknown_template(client) -> None:
    """Names come from the browser and are checked, not trusted."""
    response = await client.post(
        "api/regenerate-selected", json={"templates": ["../../etc/passwd"]}
    )
    assert response.status == 400
    assert "Unknown parent template" in (await response.json())["error"]


async def test_the_panel_offers_the_selection_controls(client) -> None:
    body = await (await client.get("/")).text()
    assert 'id="regen-selected-btn"' in body
    assert 'id="tpl-select-all"' in body
