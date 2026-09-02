"""Selected-template regeneration, and build-and-flash via Device Builder.

The dashboard is stubbed at the client boundary, so these exercise the whole
coordinator -- session lifecycle, per-device state, cancellation, failure
reporting -- without needing an ESPHome add-on or flashing real hardware.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import FakeHaApi, make_entry, make_registry_device

from app.config_store import EsphomeConfigStore
from app.discovery import DeviceDiscoveryService
from app.esphome_dashboard import DashboardError
from app.flashing import FlashCoordinator, FlashState
from app.generator import YamlGenerator
from app.logbuf import LogBuffer
from app.models import Outcome
from app.orchestrator import ScanOrchestrator
from app.scheduler import ScanScheduler
from app.templates import TemplateRepository
from app.web.server import create_app

CLOUDBAY = "esphome:\n  name: cloudbay-t-${mac}\n  name_add_mac_suffix: true\n"
SWITCHBOARD = "esphome:\n  name: switchboard-${mac}\n"


class FakeDashboard:
    """Stands in for the ESPHome Device Builder add-on."""

    def __init__(self, exit_code: int = 0, fail_with: str | None = None,
                 delay: float = 0.0) -> None:
        self.exit_code = exit_code
        self.fail_with = fail_with
        self.delay = delay
        self.uploaded: list[str] = []

    async def locate(self):
        if self.fail_with:
            raise DashboardError(self.fail_with)
        return "http://fake:6052"

    async def diagnostics(self):
        return {"found": True, "base_url": "http://fake:6052",
                "description": "http://fake:6052 via a stub", "attempts": []}

    async def upload(self, configuration: str):
        self.uploaded.append(configuration)
        if self.fail_with:
            raise DashboardError(self.fail_with)
        yield ("line", f"INFO Compiling {configuration}")
        if self.delay:
            await asyncio.sleep(self.delay)
        yield ("line", "INFO Uploading... [100%]")
        yield ("exit", self.exit_code)


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


# -- the flash coordinator --------------------------------------------------


async def test_a_successful_run_marks_every_device_done() -> None:
    dashboard = FakeDashboard()
    coordinator = FlashCoordinator(dashboard)

    session = await coordinator.start(
        [("cloudbay-t-livingroom", "cloudbay-t-livingroom.yaml"),
         ("cloudbay-t-porch", "cloudbay-t-porch.yaml")]
    )
    await asyncio.wait_for(coordinator._task, timeout=5)

    assert [t.state for t in session.tasks] == [FlashState.DONE, FlashState.DONE]
    assert dashboard.uploaded == [
        "cloudbay-t-livingroom.yaml", "cloudbay-t-porch.yaml"
    ]
    assert not session.active


async def test_a_nonzero_exit_is_a_failure_with_the_code() -> None:
    coordinator = FlashCoordinator(FakeDashboard(exit_code=1))
    session = await coordinator.start([("thing", "thing.yaml")])
    await asyncio.wait_for(coordinator._task, timeout=5)

    assert session.tasks[0].state is FlashState.FAILED
    assert "code 1" in session.tasks[0].message


async def test_an_unreachable_dashboard_is_reported_per_device() -> None:
    coordinator = FlashCoordinator(FakeDashboard(fail_with="dashboard is down"))
    session = await coordinator.start([("thing", "thing.yaml")])
    await asyncio.wait_for(coordinator._task, timeout=5)

    assert session.tasks[0].state is FlashState.FAILED
    assert "dashboard is down" in session.tasks[0].message


async def test_output_lines_are_captured_for_the_dialog() -> None:
    coordinator = FlashCoordinator(FakeDashboard())
    session = await coordinator.start([("thing", "thing.yaml")])
    await asyncio.wait_for(coordinator._task, timeout=5)

    assert any("Compiling" in line for line in session.tasks[0].lines)
    assert session.tasks[0].detail  # last line, for the one-row summary


async def test_devices_are_flashed_one_at_a_time() -> None:
    """Concurrent builds would contend for the Device Builder's workspace."""
    order: list[str] = []

    class Recording(FakeDashboard):
        async def upload(self, configuration: str):
            order.append(f"start {configuration}")
            await asyncio.sleep(0.02)
            order.append(f"end {configuration}")
            yield ("exit", 0)

    coordinator = FlashCoordinator(Recording())
    await coordinator.start([("a", "a.yaml"), ("b", "b.yaml")])
    await asyncio.wait_for(coordinator._task, timeout=5)

    assert order == ["start a.yaml", "end a.yaml", "start b.yaml", "end b.yaml"]


async def test_cancelling_stops_before_the_next_device() -> None:
    """The in-flight device finishes: aborting an OTA write bricks boards."""
    coordinator = FlashCoordinator(FakeDashboard(delay=0.05))
    session = await coordinator.start(
        [("a", "a.yaml"), ("b", "b.yaml"), ("c", "c.yaml")]
    )
    await asyncio.sleep(0.02)
    assert coordinator.cancel() is True
    await asyncio.wait_for(coordinator._task, timeout=5)

    assert session.tasks[0].state is FlashState.DONE
    assert session.tasks[1].state is FlashState.CANCELLED
    assert session.tasks[2].state is FlashState.CANCELLED


async def test_cancel_with_nothing_running_is_false() -> None:
    assert FlashCoordinator(FakeDashboard()).cancel() is False


async def test_a_second_run_is_refused_while_one_is_active() -> None:
    coordinator = FlashCoordinator(FakeDashboard(delay=0.1))
    await coordinator.start([("a", "a.yaml")])
    with pytest.raises(RuntimeError, match="already in progress"):
        await coordinator.start([("b", "b.yaml")])
    await asyncio.wait_for(coordinator._task, timeout=5)


async def test_snapshot_shape_for_the_panel() -> None:
    coordinator = FlashCoordinator(FakeDashboard())
    assert coordinator.snapshot() is None

    await coordinator.start([("thing", "thing.yaml")])
    await asyncio.wait_for(coordinator._task, timeout=5)

    snapshot = coordinator.snapshot()
    assert snapshot["active"] is False
    assert snapshot["counts"]["done"] == 1
    assert snapshot["tasks"][0]["node_name"] == "thing"


# -- endpoints --------------------------------------------------------------


@pytest.fixture
async def client_and_dashboard(settings, parented):
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
    dashboard = FakeDashboard()
    flasher = FlashCoordinator(dashboard)
    logs = LogBuffer().install()
    app = create_app(local, orchestrator, scheduler, generator, logs, flasher)
    try:
        async with TestClient(TestServer(app)) as test_client:
            yield test_client, dashboard, flasher
    finally:
        logging.getLogger().removeHandler(logs)


async def test_regenerate_selected_endpoint(client_and_dashboard, parented) -> None:
    client, _, _ = client_and_dashboard
    response = await client.post(
        "api/regenerate-selected", json={"templates": ["cloudbay-t.yaml"]}
    )
    data = await response.json()

    assert response.status == 200
    assert data["counts"]["regenerated"] == 2
    assert (parented / "cloudbay-t-livingroom.yaml").exists()
    assert not (parented / "switchboard-hallway.yaml").exists()


async def test_regenerate_selected_rejects_an_empty_selection(client_and_dashboard) -> None:
    client, _, _ = client_and_dashboard
    response = await client.post("api/regenerate-selected", json={"templates": []})
    assert response.status == 400


async def test_regenerate_selected_rejects_an_unknown_template(client_and_dashboard) -> None:
    """Names come from the browser and are checked, not trusted."""
    client, _, _ = client_and_dashboard
    response = await client.post(
        "api/regenerate-selected", json={"templates": ["../../etc/passwd"]}
    )
    assert response.status == 400
    assert "Unknown parent template" in (await response.json())["error"]


async def test_flash_selected_regenerates_then_flashes(client_and_dashboard, parented) -> None:
    client, dashboard, flasher = client_and_dashboard

    response = await client.post(
        "api/flash-selected", json={"templates": ["cloudbay-t.yaml"]}
    )
    data = await response.json()
    assert response.status == 200
    assert data["started"] is True

    await asyncio.wait_for(flasher._task, timeout=5)

    # Configs were written before flashing, and each was flashed by filename.
    assert (parented / "cloudbay-t-livingroom.yaml").exists()
    assert sorted(dashboard.uploaded) == [
        "cloudbay-t-livingroom.yaml", "cloudbay-t-porch.yaml"
    ]


async def test_flash_status_endpoint(client_and_dashboard) -> None:
    client, _, flasher = client_and_dashboard
    await client.post("api/flash-selected", json={"templates": ["switchboard.yaml"]})
    await asyncio.wait_for(flasher._task, timeout=5)

    data = await (await client.get("api/flash/status")).json()
    assert data["active"] is False
    assert data["counts"]["done"] == 1


async def test_flash_status_before_any_run(client_and_dashboard) -> None:
    client, _, _ = client_and_dashboard
    data = await (await client.get("api/flash/status")).json()
    assert data["active"] is False
    assert data["tasks"] == []


async def test_a_concurrent_flash_is_refused(client_and_dashboard) -> None:
    client, dashboard, flasher = client_and_dashboard
    # Slow the stub down so the run is genuinely still in flight when the
    # second request arrives; with no delay it finishes first and the test
    # would pass for the wrong reason.
    dashboard.delay = 0.5
    await flasher.start([("busy", "busy.yaml")])
    assert flasher.busy

    response = await client.post(
        "api/flash-selected", json={"templates": ["cloudbay-t.yaml"]}
    )
    assert response.status == 409
    await asyncio.wait_for(flasher._task, timeout=5)


async def test_cancel_endpoint(client_and_dashboard) -> None:
    client, _, _ = client_and_dashboard
    assert (await (await client.post("api/flash/cancel")).json())["cancelling"] is False


async def test_the_panel_exposes_the_controls(client_and_dashboard) -> None:
    client, _, _ = client_and_dashboard
    body = await (await client.get("/")).text()
    assert 'id="regen-selected-btn"' in body
    assert 'id="flash-selected-btn"' in body
    assert 'id="tpl-select-all"' in body
    assert 'id="flash-body"' in body


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


async def test_the_builder_diagnostics_endpoint(client_and_dashboard) -> None:
    """Backs the panel's 'Check builder' button, so a detection failure is
    diagnosable without reading the add-on log."""
    client, _, _ = client_and_dashboard
    response = await client.get("api/esphome-dashboard")

    assert response.status == 200
    data = await response.json()
    assert data["found"] is True
    assert data["base_url"] == "http://fake:6052"


async def test_the_panel_offers_a_builder_check(client_and_dashboard) -> None:
    client, _, _ = client_and_dashboard
    body = await (await client.get("/")).text()
    assert 'id="builder-check"' in body
    assert 'id="builder-status"' in body


async def test_diagnostics_never_errors_without_a_dashboard() -> None:
    """The status panel must explain a failure, not become one."""
    report = await FlashCoordinator(None).diagnostics()
    assert report["found"] is False
    assert report["error"]


async def test_diagnostics_survives_a_dashboard_that_raises() -> None:
    class Exploding:
        async def diagnostics(self):
            raise RuntimeError("boom")

    report = await FlashCoordinator(Exploding()).diagnostics()
    assert report["found"] is False
    assert "boom" in report["error"]


async def test_an_unreachable_builder_fails_the_whole_run_at_once() -> None:
    """A 16-device run once spent minutes rediscovering nothing, once per
    device, burying the one useful error among sixteen copies."""
    dashboard = FakeDashboard(fail_with="Could not find the ESPHome add-on")
    coordinator = FlashCoordinator(dashboard)

    session = await coordinator.start([(f"dev{i}", f"dev{i}.yaml") for i in range(16)])
    await asyncio.wait_for(coordinator._task, timeout=5)

    assert all(t.state is FlashState.FAILED for t in session.tasks)
    assert session.error and "Could not find" in session.error
    # Crucially: it gave up before touching a single device.
    assert dashboard.uploaded == []
