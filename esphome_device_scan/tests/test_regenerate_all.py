"""Bulk regeneration -- the most destructive thing the add-on can do.

Two properties matter most and are pinned here: the plan must tell the truth
before anything is written (the panel's warning is built from it), and every
replaced file must leave a backup behind.
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

PARENT = (
    "esphome:\n"
    "  name: cloudbay-t-${mac}\n"
    "  name_add_mac_suffix: true\n"
    "esp32:\n  board: esp32dev\n"
)


def build(settings) -> ScanOrchestrator:
    return ScanOrchestrator(
        discovery=DeviceDiscoveryService(three_devices()),
        store=EsphomeConfigStore(settings.esphome_config_dir),
        templates=TemplateRepository(settings.esphome_config_dir),
        generator=YamlGenerator(
            settings.mac_policy, settings.name_add_mac_suffix_action
        ),
        settings=settings,
    )


def three_devices() -> FakeHaApi:
    """Two devices the parent claims, plus one it does not."""
    return FakeHaApi(
        config_entries=[
            make_entry("e1", "cloudbay-t-livingroom"),
            make_entry("e2", "cloudbay-t-porch"),
            make_entry("e3", "mystery-gadget"),
        ],
        devices=[
            make_registry_device("d1", "e1", mac="aa:bb:cc:dd:ee:ff"),
            make_registry_device("d2", "e2", name="Porch", mac="11:22:33:44:55:66"),
            make_registry_device("d3", "e3", name="Mystery", mac="99:88:77:66:55:44"),
        ],
    )


@pytest.fixture
def parented(esphome_dir):
    (esphome_dir / "cloudbay-t.yaml").write_text(PARENT)
    return esphome_dir


# -- the plan ---------------------------------------------------------------


async def test_plan_writes_nothing(settings, parented) -> None:
    plan = await build(settings).plan_regenerate_all()

    assert plan.total == 2  # both cloudbay-t devices; mystery-gadget unmatched
    assert list(parented.glob("*.yaml")) == [parented / "cloudbay-t.yaml"]


async def test_plan_separates_generated_from_hand_edited(settings, parented) -> None:
    """The panel's warning is built from this split, so it has to be right."""
    orchestrator = build(settings)
    await orchestrator.scan()  # generates both

    # Simulate the user editing one of them.
    (parented / "cloudbay-t-porch.yaml").write_text(
        "esphome:\n  name: cloudbay-t-porch\n# my own changes\n"
    )

    plan = await orchestrator.plan_regenerate_all()

    assert plan.untouched == ("cloudbay-t-livingroom",)
    assert plan.edited == ("cloudbay-t-porch",)
    assert plan.missing == ()
    assert plan.unmatched == ("mystery-gadget",)
    assert plan.total == 2


async def test_plan_counts_missing_configs(settings, parented) -> None:
    plan = await build(settings).plan_regenerate_all()

    assert sorted(plan.missing) == ["cloudbay-t-livingroom", "cloudbay-t-porch"]
    assert plan.untouched == ()
    assert plan.edited == ()


async def test_plan_survives_a_discovery_failure(settings, parented) -> None:
    class Broken(FakeHaApi):
        async def list_devices(self):
            raise RuntimeError("home assistant is down")

    orchestrator = ScanOrchestrator(
        discovery=DeviceDiscoveryService(Broken()),
        store=EsphomeConfigStore(parented),
        templates=TemplateRepository(parented),
        generator=YamlGenerator(),
        settings=settings,
    )
    plan = await orchestrator.plan_regenerate_all()

    assert plan.error is not None
    assert plan.total == 0


# -- running it -------------------------------------------------------------


async def test_regenerate_all_rebuilds_every_matched_device(settings, parented) -> None:
    orchestrator = build(settings)
    await orchestrator.scan()

    for name in ("cloudbay-t-livingroom", "cloudbay-t-porch"):
        (parented / f"{name}.yaml").write_text("# stale\n")

    report = await orchestrator.regenerate_all()

    assert report.count(Outcome.REGENERATED) == 2
    for name in ("cloudbay-t-livingroom", "cloudbay-t-porch"):
        content = (parented / f"{name}.yaml").read_text()
        assert f"name: {name}" in content
        assert "board: esp32dev" in content


async def test_every_replaced_file_is_backed_up(settings, parented) -> None:
    orchestrator = build(settings)
    await orchestrator.scan()
    (parented / "cloudbay-t-porch.yaml").write_text("# irreplaceable\n")

    await orchestrator.regenerate_all()

    backups = list(parented.glob("cloudbay-t-porch.yaml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "# irreplaceable\n"


async def test_it_also_creates_configs_that_were_missing(settings, parented) -> None:
    """'Regenerate all' should leave every matched device in step, not just
    the ones that already had a file."""
    report = await build(settings).regenerate_all()

    assert report.count(Outcome.REGENERATED) == 2
    assert (parented / "cloudbay-t-livingroom.yaml").exists()
    assert (parented / "cloudbay-t-porch.yaml").exists()


async def test_skip_edited_leaves_hand_written_files_alone(settings, parented) -> None:
    orchestrator = build(settings)
    await orchestrator.scan()
    (parented / "cloudbay-t-porch.yaml").write_text(
        "esphome:\n  name: cloudbay-t-porch\n# mine\n"
    )

    report = await orchestrator.regenerate_all(skip_edited=True)

    assert "# mine" in (parented / "cloudbay-t-porch.yaml").read_text()
    assert not list(parented.glob("cloudbay-t-porch.yaml.bak-*"))
    assert report.count(Outcome.REGENERATED) == 1
    assert report.count(Outcome.SKIPPED_HAS_CONFIG) == 1


async def test_the_parent_is_never_regenerated(settings, parented) -> None:
    await build(settings).regenerate_all()
    assert (parented / "cloudbay-t.yaml").read_text() == PARENT
    assert not list(parented.glob("cloudbay-t.yaml.bak-*"))


async def test_unmatched_devices_are_reported_not_written(settings, parented) -> None:
    report = await build(settings).regenerate_all()

    assert report.count(Outcome.NO_TEMPLATE_MATCH) == 1
    assert not (parented / "mystery-gadget.yaml").exists()


async def test_regenerate_all_is_idempotent(settings, parented) -> None:
    """Running it twice must produce identical files the second time."""
    orchestrator = build(settings)
    await orchestrator.regenerate_all()
    first = {p.name: p.read_bytes() for p in parented.glob("*.yaml")}

    await orchestrator.regenerate_all()
    second = {p.name: p.read_bytes() for p in parented.glob("*.yaml")}

    assert first == second


async def test_the_summary_names_what_happened(settings, parented) -> None:
    """A fixed list of counts read "0 generated" after regenerating three."""
    report = await build(settings).regenerate_all()

    assert "2 regenerated" in report.summary
    assert "1 unmatched" in report.summary
    assert "0 " not in report.summary  # zero counts are omitted entirely


async def test_it_updates_the_cached_report(settings, parented) -> None:
    orchestrator = build(settings)
    await orchestrator.regenerate_all()

    assert orchestrator.last_report is not None
    assert orchestrator.last_report.count(Outcome.REGENERATED) == 2


async def test_dry_run_still_writes_because_this_is_explicit(settings, parented) -> None:
    """dry_run guards the *automatic* path. A button press is a decision."""
    orchestrator = build(replace(settings, dry_run=True))
    await orchestrator.regenerate_all()
    assert (parented / "cloudbay-t-livingroom.yaml").exists()


# -- the endpoints ----------------------------------------------------------


@pytest.fixture
async def client(settings, parented):
    local = replace(settings, enforce_ingress_peer=False)
    generator = YamlGenerator(local.mac_policy, local.name_add_mac_suffix_action)
    orchestrator = ScanOrchestrator(
        discovery=DeviceDiscoveryService(three_devices()),
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


async def test_plan_endpoint(client, parented) -> None:
    data = await (await client.get("api/regenerate-all/plan")).json()

    assert data["total"] == 2
    assert sorted(data["missing"]) == ["cloudbay-t-livingroom", "cloudbay-t-porch"]
    assert data["unmatched"] == ["mystery-gadget"]
    assert data["error"] is None
    assert list(parented.glob("*.yaml")) == [parented / "cloudbay-t.yaml"]


async def test_regenerate_all_endpoint(client, parented) -> None:
    response = await client.post("api/regenerate-all")
    data = await response.json()

    assert response.status == 200
    assert data["counts"]["regenerated"] == 2
    assert (parented / "cloudbay-t-livingroom.yaml").exists()


async def test_regenerate_all_endpoint_honours_skip_edited(client, parented) -> None:
    await client.post("api/regenerate-all")
    (parented / "cloudbay-t-porch.yaml").write_text(
        "esphome:\n  name: cloudbay-t-porch\n# mine\n"
    )

    data = await (await client.post("api/regenerate-all?skip_edited=1")).json()

    assert data["counts"]["regenerated"] == 1
    assert "# mine" in (parented / "cloudbay-t-porch.yaml").read_text()


async def test_the_button_exists_in_the_panel(client) -> None:
    body = await (await client.get("/")).text()
    assert 'id="regen-all-btn"' in body
    assert "Regenerate all" in body
    assert 'id="confirm"' in body  # its confirmation dialog
