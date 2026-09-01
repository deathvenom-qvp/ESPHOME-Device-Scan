"""Regressions found in review. Each test names the failure it prevents."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import (
    FakeHaApi,
    generated_configs,
    make_entry,
    make_registry_device,
)

from app.config_store import EsphomeConfigStore
from app.discovery import DeviceDiscoveryService
from app.generator import YamlGenerator
from app.models import Device, DeviceStatus, Outcome, TemplateSpec
from app.orchestrator import ScanOrchestrator
from app.patching import quote_like
from app.templates import TemplateRepository


def template(raw: str) -> TemplateSpec:
    return TemplateSpec(name="t.yaml", path=Path("t.yaml"), raw=raw)


def build(settings, ha: FakeHaApi) -> ScanOrchestrator:
    return ScanOrchestrator(
        discovery=DeviceDiscoveryService(ha),
        store=EsphomeConfigStore(settings.esphome_config_dir),
        templates=TemplateRepository(settings.esphome_config_dir),
        generator=YamlGenerator(
            settings.mac_policy, settings.name_add_mac_suffix_action
        ),
        settings=settings,
    )


# -- discovery --------------------------------------------------------------


async def test_unknown_entity_state_does_not_mean_offline() -> None:
    """`unknown` means connected-but-no-value; only `unavailable` means dead.

    A device whose entities have not reported yet was being shown as offline.
    """
    ha = FakeHaApi(
        config_entries=[make_entry(state="loaded")],
        devices=[make_registry_device()],
        entities=[{"entity_id": "sensor.temp", "device_id": "dev1"}],
        states=[{"entity_id": "sensor.temp", "state": "unknown"}],
    )
    devices = await DeviceDiscoveryService(ha).list_devices()
    assert devices[0].status is DeviceStatus.ONLINE


async def test_unavailable_entities_still_mean_offline() -> None:
    ha = FakeHaApi(
        config_entries=[make_entry(state="loaded")],
        devices=[make_registry_device()],
        entities=[{"entity_id": "sensor.temp", "device_id": "dev1"}],
        states=[{"entity_id": "sensor.temp", "state": "unavailable"}],
    )
    devices = await DeviceDiscoveryService(ha).list_devices()
    assert devices[0].status is DeviceStatus.OFFLINE


async def test_colliding_node_names_are_disambiguated() -> None:
    """Two devices slugifying to the same name must not share a filename.

    Previously the second device kept the duplicate name, so it was reported as
    "already configured" against a file describing the *first* device.
    """
    ha = FakeHaApi(
        config_entries=[
            make_entry("e1", "Porch Light"),
            make_entry("e2", "Porch Light"),
        ],
        devices=[
            make_registry_device("d1", "e1", "Porch Light", "aa:bb:cc:dd:ee:ff"),
            make_registry_device("d2", "e2", "Porch Light", "11:22:33:44:55:66"),
        ],
    )
    devices = await DeviceDiscoveryService(ha).list_devices()
    names = [d.node_name for d in devices]

    assert len(names) == len(set(names)), f"duplicate node names: {names}"
    assert "porch-light" in names
    assert "porch-light-445566" in names


async def test_collision_without_a_mac_still_resolves() -> None:
    ha = FakeHaApi(
        config_entries=[make_entry("e1", "Thing"), make_entry("e2", "Thing")],
        devices=[
            make_registry_device("d1", "e1", "Thing", None),
            make_registry_device("d2", "e2", "Thing", None),
        ],
    )
    names = [d.node_name for d in await DeviceDiscoveryService(ha).list_devices()]
    assert len(names) == len(set(names))


async def test_two_colliding_devices_generate_two_files(
    settings, esphome_dir, parents_installed
) -> None:
    ha = FakeHaApi(
        config_entries=[
            make_entry("e1", "CloudBay T Porch"),
            make_entry("e2", "CloudBay T Porch"),
        ],
        devices=[
            make_registry_device("d1", "e1", "CloudBay T Porch", "aa:bb:cc:dd:ee:ff"),
            make_registry_device("d2", "e2", "CloudBay T Porch", "11:22:33:44:55:66"),
        ],
    )
    report = await build(settings, ha).scan()

    assert report.count(Outcome.GENERATED) == 2
    assert len(generated_configs(esphome_dir)) == 2


# -- config store -----------------------------------------------------------


def test_two_backups_in_the_same_second_do_not_clobber(store, esphome_dir) -> None:
    """The timestamp is second-resolution; a fast double Regenerate must not
    overwrite the only surviving copy of the user's original file."""
    (esphome_dir / "device.yaml").write_text("original\n")

    store.write("device", "first\n", overwrite=True)
    store.write("device", "second\n", overwrite=True)

    backups = sorted(p.read_text() for p in esphome_dir.glob("device.yaml.bak-*"))
    assert len(backups) == 2
    assert "original\n" in backups
    assert "first\n" in backups


def test_regenerate_rewrites_the_file_that_holds_the_name(store, esphome_dir) -> None:
    """Writing to <node>.yaml instead would leave two configs claiming one name."""
    (esphome_dir / "lounge.yaml").write_text(
        "esphome:\n  name: cloudbay-t-livingroom\n# hand written\n"
    )

    path = store.write("cloudbay-t-livingroom", "regenerated\n", overwrite=True)

    assert path.name == "lounge.yaml"
    assert path.read_text() == "regenerated\n"
    assert not (esphome_dir / "cloudbay-t-livingroom.yaml").exists()

    # Exactly one config declares the name, plus its backup.
    assert [p.name for p in esphome_dir.glob("*.yaml")] == ["lounge.yaml"]


def test_index_stays_correct_after_an_incremental_write(store, esphome_dir) -> None:
    """The write path updates the cache in place; it must not drift from disk."""
    store.index()  # prime the cache
    store.write("alpha", "esphome:\n  name: alpha\n")
    store.write("beta", "esphome:\n  name: beta\n")

    assert store.has_config("alpha") and store.has_config("beta")
    # A fresh store reading the same directory must agree.
    fresh = EsphomeConfigStore(esphome_dir)
    assert set(fresh.index()) == set(store.index()) == {"alpha", "beta"}


def test_regenerate_index_points_at_the_rewritten_file(store, esphome_dir) -> None:
    (esphome_dir / "lounge.yaml").write_text("esphome:\n  name: cloudbay-t-livingroom\n")
    store.index()
    store.write("cloudbay-t-livingroom", "esphome:\n  name: cloudbay-t-livingroom\n",
                overwrite=True)

    assert store.find("cloudbay-t-livingroom").path.name == "lounge.yaml"
    fresh = EsphomeConfigStore(esphome_dir)
    assert set(fresh.index()) == {"cloudbay-t-livingroom"}


# -- quoting ----------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "123456",      # int
        "0755",        # int, leading zero
        "1.5",         # float
        "1e5",         # float, exponent
        "0x1f",        # hex int
        "0o17",        # octal int
        "1_000",       # int with digit separator
        "2024-01-01",  # date
        "true", "false", "null", "yes", "no", "on", "off",
    ],
)
def test_values_that_would_not_load_back_as_strings_are_quoted(value: str) -> None:
    """Unquoted, each of these loads as something other than the string written.

    `name: 123456` would reach ESPHome as an integer and be rejected.
    """
    quoted = quote_like(value, None)
    assert quoted.startswith('"'), f"{value!r} was left unquoted as {quoted!r}"


@pytest.mark.parametrize(
    "value", ["123456", "0x1f", "2024-01-01", "1_000", "true", "a-1", "plain"]
)
def test_emitted_scalars_always_load_back_unchanged(value: str) -> None:
    """The property that matters, checked directly rather than via quoting rules."""
    from app.yaml_compat import load

    assert load(f"k: {quote_like(value, None)}\n")["k"] == value


@pytest.mark.parametrize("value", ["cloudbay-t-livingroom", "abc123", "a-1"])
def test_ordinary_names_stay_unquoted(value: str) -> None:
    assert quote_like(value, None) == value


def test_control_characters_are_escaped() -> None:
    """Friendly names come from a Home Assistant text field; a newline in one
    would otherwise splice a raw break into the middle of a scalar."""
    out = quote_like("Living\nRoom\tA", '"')
    assert "\n" not in out
    assert out == '"Living\\nRoom\\tA"'


def test_a_newline_falls_back_out_of_single_quotes() -> None:
    out = quote_like("a\nb", "'")
    assert not out.startswith("'")
    assert "\\n" in out


def test_generated_yaml_survives_a_newline_in_a_friendly_name(generator) -> None:
    from app.yaml_compat import load

    device = Device(
        node_name="thing", friendly_name="Living\nRoom", mac="aabbccddeeff"
    )
    out = generator.generate(
        template('esphome:\n  name: x\n  friendly_name: "${f}"\n'), device
    )
    assert load(out.content)["esphome"]["friendly_name"] == "Living\nRoom"


# -- generator --------------------------------------------------------------


def test_every_declared_name_substitution_is_patched(generator) -> None:
    """Patching only the first left `${name}` resolving to the stale value."""
    raw = (
        "substitutions:\n"
        "  devicename: base\n"
        "  name: base\n"
        "esphome:\n  name: x\n"
        'wifi:\n  ap:\n    ssid: "${name}"\n'
    )
    device = Device(node_name="cloudbay-t-livingroom", mac="aabbccddeeff")
    out = generator.generate(template(raw), device)

    body = out.content.partition("\n\n")[2]  # drop the provenance header
    assert "devicename: cloudbay-t-livingroom" in body
    assert "base" not in body, "a declared substitution kept its stale value"


def test_every_declared_mac_substitution_is_patched(generator) -> None:
    raw = 'substitutions:\n  mac: "000000"\n  mac_suffix: "000000"\nesphome:\n  name: x\n'
    out = generator.generate(
        template(raw), Device(node_name="thing", mac="aabbccddeeff")
    )
    assert out.content.count('"ddeeff"') == 2
    assert '"000000"' not in out.content


# -- orchestrator -----------------------------------------------------------


async def test_generate_updates_the_cached_report(
    settings, esphome_dir, parents_installed
) -> None:
    """The panel polls /api/state after Generate; a stale report showed the
    device as still unconfigured until the next full scan."""
    ha = FakeHaApi(
        config_entries=[make_entry(title="cloudbay-t-livingroom")],
        devices=[make_registry_device(mac="aa:bb:cc:dd:ee:ff")],
    )
    orchestrator = build(replace_auto_generate(settings, False), ha)

    await orchestrator.scan()
    before = orchestrator.last_report.devices[0]
    assert before.outcome is Outcome.SKIPPED_AUTO_GENERATE_OFF

    device = await orchestrator.find_device("cloudbay-t-livingroom")
    orchestrator.generate_for(device)

    after = orchestrator.last_report.devices[0]
    assert after.outcome is Outcome.GENERATED
    assert after.has_yaml is True


def replace_auto_generate(settings, value: bool):
    from dataclasses import replace

    return replace(settings, auto_generate=value)


# -- ha client --------------------------------------------------------------


async def test_a_hung_home_assistant_does_not_block_forever(monkeypatch) -> None:
    """Without a bound on the exchange, one unresponsive call would hold the
    client's lock and stall every later scan."""
    from app import ha_client
    from app.ha_client import HaApiError, SupervisorHaClient

    monkeypatch.setattr(ha_client, "COMMAND_TIMEOUT_SECONDS", 0.2)

    class HangingSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def receive_json(self):
            await asyncio.sleep(3600)

        async def send_json(self, _payload):
            pass

    class HangingSession:
        def ws_connect(self, *args, **kwargs):
            return HangingSocket()

    client = SupervisorHaClient(HangingSession(), token="tok")  # type: ignore[arg-type]

    with pytest.raises(HaApiError, match="timed out"):
        await client._ws_command("config/device_registry/list")


async def test_a_closed_socket_is_reported_not_raised_raw() -> None:
    """aiohttp raises TypeError from receive_json() when the peer closes."""
    from app.ha_client import HaApiError, SupervisorHaClient

    class ClosingSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def receive_json(self):
            raise TypeError("Received message CLOSED is not str")

        async def send_json(self, _payload):
            pass

    class ClosingSession:
        def ws_connect(self, *args, **kwargs):
            return ClosingSocket()

    client = SupervisorHaClient(ClosingSession(), token="tok")  # type: ignore[arg-type]

    with pytest.raises(HaApiError):
        await client._ws_command("get_states")

    # And the safe wrapper degrades to an empty list rather than propagating.
    assert await client.list_devices() == []
