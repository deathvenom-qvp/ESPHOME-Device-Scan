"""End-to-end: the worked example from the specification, plus scan policy.

Section 7 of the spec:

    Base template  cloudbay-t.yaml, containing  name: cloudbay-t-${mac}
    Discovered     cloudbay-t-livingroom, AA:BB:CC:DD:EE:FF
    Generated      name: cloudbay-t-livingroom, MAC-suffix logic removed,
                   all other template content preserved.

The generated file is committed at ``examples/generated/`` and compared byte for
byte, so any change in generator behaviour shows up as a reviewable diff rather
than passing silently.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FakeHaApi, make_entry, make_registry_device

from app.config_store import EsphomeConfigStore
from app.discovery import DeviceDiscoveryService
from app.generator import YamlGenerator
from app.models import Outcome
from app.orchestrator import ScanOrchestrator
from app.templates import TemplateRepository
from app.yaml_compat import load

GOLDEN = (
    Path(__file__).resolve().parent.parent
    / "examples" / "generated" / "cloudbay-t-livingroom.yaml"
)


def build(settings, ha: FakeHaApi) -> ScanOrchestrator:
    return ScanOrchestrator(
        discovery=DeviceDiscoveryService(ha),
        store=EsphomeConfigStore(settings.esphome_config_dir),
        templates=TemplateRepository(settings.templates_dir),
        generator=YamlGenerator(
            settings.mac_policy, settings.name_add_mac_suffix_action
        ),
        settings=settings,
    )


def spec_example_ha() -> FakeHaApi:
    return FakeHaApi(
        config_entries=[make_entry(title="cloudbay-t-livingroom")],
        devices=[make_registry_device(
            name="CloudBay T Living Room", mac="AA:BB:CC:DD:EE:FF"
        )],
        entities=[{"entity_id": "sensor.uptime", "device_id": "dev1"}],
        states=[{"entity_id": "sensor.uptime", "state": "600"}],
    )


# -- the specified case -----------------------------------------------------


async def test_spec_example_end_to_end(settings, esphome_dir) -> None:
    orchestrator = build(settings, spec_example_ha())
    report = await orchestrator.scan()

    assert report.count(Outcome.GENERATED) == 1

    written = esphome_dir / "cloudbay-t-livingroom.yaml"
    assert written.exists()

    content = written.read_text()
    assert "name: cloudbay-t-livingroom" in content
    assert "name_add_mac_suffix: false" in content
    assert "cloudbay-t-${mac}" not in content

    # Everything else from the template is still there.
    assert "board: esp32dev" in content
    assert "type: esp-idf" in content
    assert "!secret api_encryption_key" in content
    assert "platform: uptime" in content


async def test_generated_file_matches_the_committed_example(settings) -> None:
    """Golden-file check, so behaviour changes surface as a reviewable diff."""
    orchestrator = build(settings, spec_example_ha())
    await orchestrator.scan()

    produced = (settings.esphome_config_dir / "cloudbay-t-livingroom.yaml").read_text()
    assert produced == GOLDEN.read_text(), (
        "Generated output drifted from examples/generated/. If the change is "
        "intended, refresh it with: python3 scripts/refresh_examples.py"
    )


async def test_generated_output_is_loadable_yaml(settings) -> None:
    orchestrator = build(settings, spec_example_ha())
    await orchestrator.scan()

    data = load((settings.esphome_config_dir / "cloudbay-t-livingroom.yaml").read_text())
    assert data["esphome"]["name"] == "cloudbay-t-livingroom"
    assert data["esphome"]["name_add_mac_suffix"] is False
    assert data["esp32"]["board"] == "esp32dev"
    assert data["substitutions"]["mac"] == "ddeeff"


# -- scan policy ------------------------------------------------------------


async def test_a_device_with_a_config_is_skipped(settings, esphome_dir) -> None:
    (esphome_dir / "existing.yaml").write_text(
        "esphome:\n  name: cloudbay-t-livingroom\n# hand written\n"
    )
    orchestrator = build(settings, spec_example_ha())
    report = await orchestrator.scan()

    assert report.count(Outcome.SKIPPED_HAS_CONFIG) == 1
    assert report.count(Outcome.GENERATED) == 0
    assert "# hand written" in (esphome_dir / "existing.yaml").read_text()


async def test_a_scan_never_overwrites(settings, esphome_dir) -> None:
    """The central safety guarantee: automation only ever creates."""
    orchestrator = build(settings, spec_example_ha())
    await orchestrator.scan()

    target = esphome_dir / "cloudbay-t-livingroom.yaml"
    target.write_text("# user edited this\nesphome:\n  name: cloudbay-t-livingroom\n")

    for _ in range(3):
        await orchestrator.scan()
    assert target.read_text().startswith("# user edited this")


async def test_scanning_is_idempotent(settings, esphome_dir) -> None:
    orchestrator = build(settings, spec_example_ha())
    await orchestrator.scan()

    target = esphome_dir / "cloudbay-t-livingroom.yaml"
    first = target.read_bytes()
    first_mtime = target.stat().st_mtime_ns

    second = await orchestrator.scan()
    third = await orchestrator.scan()

    assert target.read_bytes() == first
    assert target.stat().st_mtime_ns == first_mtime  # not even rewritten
    assert second.count(Outcome.GENERATED) == 0
    assert third.count(Outcome.GENERATED) == 0
    assert second.count(Outcome.SKIPPED_HAS_CONFIG) == 1


async def test_unmatched_devices_are_reported_not_generated(settings, esphome_dir) -> None:
    ha = FakeHaApi(
        config_entries=[make_entry(title="mystery-gadget")],
        devices=[make_registry_device(name="Mystery Gadget")],
    )
    report = await build(settings, ha).scan()

    assert report.count(Outcome.NO_TEMPLATE_MATCH) == 1
    assert list(esphome_dir.iterdir()) == []
    assert "No template matches" in report.devices[0].message


async def test_dry_run_writes_nothing(settings, esphome_dir) -> None:
    orchestrator = build(replace(settings, dry_run=True), spec_example_ha())
    report = await orchestrator.scan()

    assert report.count(Outcome.WOULD_GENERATE) == 1
    assert list(esphome_dir.iterdir()) == []


async def test_auto_generate_off_writes_nothing(settings, esphome_dir) -> None:
    orchestrator = build(replace(settings, auto_generate=False), spec_example_ha())
    report = await orchestrator.scan()

    assert report.count(Outcome.SKIPPED_AUTO_GENERATE_OFF) == 1
    assert list(esphome_dir.iterdir()) == []


# -- regeneration -----------------------------------------------------------


async def test_regenerate_overwrites_and_backs_up(settings, esphome_dir) -> None:
    orchestrator = build(settings, spec_example_ha())
    await orchestrator.scan()

    target = esphome_dir / "cloudbay-t-livingroom.yaml"
    target.write_text("# stale hand edit\n")

    report = await orchestrator.regenerate("cloudbay-t-livingroom")

    assert report.outcome is Outcome.REGENERATED
    assert "name: cloudbay-t-livingroom" in target.read_text()

    backups = list(esphome_dir.glob("cloudbay-t-livingroom.yaml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "# stale hand edit\n"


async def test_regenerate_of_an_unknown_device_raises(settings) -> None:
    orchestrator = build(settings, spec_example_ha())
    with pytest.raises(LookupError):
        await orchestrator.regenerate("not-a-device")


async def test_on_demand_generate_respects_the_overwrite_guard(settings, esphome_dir) -> None:
    orchestrator = build(settings, spec_example_ha())
    await orchestrator.scan()

    device = await orchestrator.find_device("cloudbay-t-livingroom")
    report = orchestrator.generate_for(device, overwrite=False)
    assert report.outcome is Outcome.SKIPPED_HAS_CONFIG


# -- resilience -------------------------------------------------------------


async def test_a_scan_survives_an_unreadable_template_dir(settings, esphome_dir) -> None:
    orchestrator = build(replace(settings, templates_dir=Path("/nonexistent")), spec_example_ha())
    report = await orchestrator.scan()

    assert report.count(Outcome.NO_TEMPLATE_MATCH) == 1
    assert list(esphome_dir.iterdir()) == []


async def test_a_scan_with_no_devices_reports_cleanly(settings) -> None:
    report = await build(settings, FakeHaApi()).scan()
    assert report.devices == ()
    assert "0 device(s)" in report.summary


async def test_two_devices_get_two_files(settings, esphome_dir) -> None:
    ha = FakeHaApi(
        config_entries=[
            make_entry("e1", "cloudbay-t-livingroom"),
            make_entry("e2", "switchboard-hallway"),
        ],
        devices=[
            make_registry_device("d1", "e1", mac="aa:bb:cc:dd:ee:ff"),
            make_registry_device("d2", "e2", name="Hallway", mac="11:22:33:44:55:66"),
        ],
    )
    report = await build(settings, ha).scan()

    assert report.count(Outcome.GENERATED) == 2
    assert (esphome_dir / "cloudbay-t-livingroom.yaml").exists()
    assert (esphome_dir / "switchboard-hallway.yaml").exists()

    hallway = (esphome_dir / "switchboard-hallway.yaml").read_text()
    assert "name: switchboard-hallway" in hallway
    assert "Template: switchboard.yaml" in hallway
