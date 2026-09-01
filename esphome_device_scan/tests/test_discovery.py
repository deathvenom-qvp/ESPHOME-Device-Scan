"""Device discovery from Home Assistant's registries.

Runs entirely against :class:`FakeHaApi`, so these tests pin down exactly how
each Home Assistant payload shape is interpreted -- including the awkward ones,
like a device whose registry name is a friendly name rather than a node name.
"""

from __future__ import annotations

import pytest
from conftest import FakeHaApi, make_entry, make_registry_device

from app.discovery import DeviceDiscoveryService
from app.models import DeviceSource, DeviceStatus, normalise_mac, slugify_node_name


async def discover(**kwargs):
    return await DeviceDiscoveryService(FakeHaApi(**kwargs)).list_devices()


# -- MAC extraction ---------------------------------------------------------


async def test_mac_comes_from_the_connections_list() -> None:
    """ESPHome registers CONNECTION_NETWORK_MAC; this is why registry-only works."""
    devices = await discover(
        config_entries=[make_entry()],
        devices=[make_registry_device(mac="aa:bb:cc:dd:ee:ff")],
    )
    assert devices[0].mac == "aabbccddeeff"
    assert devices[0].mac_suffix == "ddeeff"
    assert devices[0].mac_pretty == "AA:BB:CC:DD:EE:FF"


async def test_non_mac_connections_are_ignored() -> None:
    device = make_registry_device(mac=None)
    device["connections"] = [["zigbee", "0x1234"], ["mac", "11:22:33:44:55:66"]]
    devices = await discover(config_entries=[make_entry()], devices=[device])
    assert devices[0].mac == "112233445566"


async def test_missing_mac_is_none_not_an_error() -> None:
    devices = await discover(
        config_entries=[make_entry()], devices=[make_registry_device(mac=None)]
    )
    assert devices[0].mac is None
    assert devices[0].mac_suffix is None


async def test_malformed_connections_do_not_crash() -> None:
    device = make_registry_device(mac=None)
    device["connections"] = ["nonsense", ["mac"], None, ["mac", "not-a-mac"]]
    devices = await discover(config_entries=[make_entry()], devices=[device])
    assert devices[0].mac is None


# -- node name resolution ---------------------------------------------------


async def test_entry_title_is_used_when_it_looks_like_a_node_name() -> None:
    devices = await discover(
        config_entries=[make_entry(title="cloudbay-t-livingroom")],
        devices=[make_registry_device(name="CloudBay T Living Room")],
    )
    assert devices[0].node_name == "cloudbay-t-livingroom"
    assert devices[0].name_source == "config-entry-title"
    # The registry name is the *friendly* name and must not become the filename.
    assert devices[0].friendly_name == "CloudBay T Living Room"


async def test_a_friendly_entry_title_is_slugified() -> None:
    devices = await discover(
        config_entries=[make_entry(title="CloudBay T Living Room")],
        devices=[make_registry_device()],
    )
    assert devices[0].node_name == "cloudbay-t-living-room"
    assert devices[0].name_source == "config-entry-title-slug"


async def test_registry_name_is_the_next_fallback() -> None:
    devices = await discover(
        config_entries=[make_entry(title="")],
        devices=[make_registry_device(name="Porch Light")],
    )
    assert devices[0].node_name == "porch-light"
    assert devices[0].name_source == "registry-name-slug"


async def test_mac_is_the_last_resort() -> None:
    devices = await discover(
        config_entries=[make_entry(title="")],
        devices=[make_registry_device(name=None, mac="aa:bb:cc:dd:ee:ff")],
    )
    assert devices[0].node_name == "esphome-ddeeff"
    assert devices[0].name_source == "mac-fallback"


async def test_user_renamed_devices_win_over_the_integration_name() -> None:
    device = make_registry_device(name="ESPHome Thing")
    device["name_by_user"] = "Garage Door"
    devices = await discover(config_entries=[make_entry(title="")], devices=[device])
    assert devices[0].node_name == "garage-door"


@pytest.mark.parametrize("raw,expected", [
    ("CloudBay T Living Room", "cloudbay-t-living-room"),
    ("under_scored_name", "under-scored-name"),
    ("Mixed  Spacing", "mixed-spacing"),
    ("Trailing-", "trailing"),
    ("weird!!chars@@", "weirdchars"),
    ("", None),
    ("!!!", None),
])
def test_slugify(raw: str, expected: str | None) -> None:
    assert slugify_node_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("aa:bb:cc:dd:ee:ff", "aabbccddeeff"),
    ("AA-BB-CC-DD-EE-FF", "aabbccddeeff"),
    ("aabb.ccdd.eeff", "aabbccddeeff"),
    ("aabbccddeeff", "aabbccddeeff"),
    ("too-short", None),
    ("", None),
    (None, None),
])
def test_normalise_mac(raw, expected) -> None:
    assert normalise_mac(raw) == expected


# -- status -----------------------------------------------------------------


async def test_loaded_entry_with_a_live_entity_is_online() -> None:
    devices = await discover(
        config_entries=[make_entry(state="loaded")],
        devices=[make_registry_device()],
        entities=[{"entity_id": "sensor.uptime", "device_id": "dev1"}],
        states=[{"entity_id": "sensor.uptime", "state": "1234"}],
    )
    assert devices[0].status is DeviceStatus.ONLINE


async def test_loaded_entry_with_only_unavailable_entities_is_offline() -> None:
    """A loaded config entry is not proof of life; the entities decide."""
    devices = await discover(
        config_entries=[make_entry(state="loaded")],
        devices=[make_registry_device()],
        entities=[{"entity_id": "sensor.uptime", "device_id": "dev1"}],
        states=[{"entity_id": "sensor.uptime", "state": "unavailable"}],
    )
    assert devices[0].status is DeviceStatus.OFFLINE


async def test_entry_in_setup_retry_is_offline() -> None:
    devices = await discover(
        config_entries=[make_entry(state="setup_retry")],
        devices=[make_registry_device()],
    )
    assert devices[0].status is DeviceStatus.OFFLINE


async def test_no_entities_means_unknown_not_offline() -> None:
    devices = await discover(
        config_entries=[make_entry(state="loaded")], devices=[make_registry_device()]
    )
    assert devices[0].status is DeviceStatus.UNKNOWN


# -- discovery flows --------------------------------------------------------


async def test_unadopted_devices_come_from_discovery_flows() -> None:
    devices = await discover(
        flows=[{
            "handler": "esphome",
            "context": {
                "unique_id": "112233445566",
                "title_placeholders": {"name": "cloudbay-t-porch"},
            },
        }]
    )
    assert len(devices) == 1
    assert devices[0].node_name == "cloudbay-t-porch"
    assert devices[0].mac == "112233445566"
    assert devices[0].status is DeviceStatus.DISCOVERED
    assert devices[0].source is DeviceSource.DISCOVERY_FLOW


async def test_a_flow_for_an_already_adopted_device_is_not_duplicated() -> None:
    devices = await discover(
        config_entries=[make_entry()],
        devices=[make_registry_device(mac="aa:bb:cc:dd:ee:ff")],
        flows=[{
            "handler": "esphome",
            "context": {
                "unique_id": "aabbccddeeff",
                "title_placeholders": {"name": "cloudbay-t-livingroom"},
            },
        }],
    )
    assert len(devices) == 1
    assert devices[0].source is DeviceSource.CONFIG_ENTRY


async def test_flows_from_other_integrations_are_ignored() -> None:
    assert await discover(flows=[{"handler": "hue", "context": {}}]) == []


# -- joining ----------------------------------------------------------------


async def test_devices_from_other_integrations_are_skipped() -> None:
    hue = make_registry_device(device_id="dev2", entry_id="hue1", name="Hue Bulb")
    devices = await discover(
        config_entries=[make_entry()],
        devices=[make_registry_device(), hue],
    )
    assert [d.node_name for d in devices] == ["cloudbay-t-livingroom"]


async def test_an_entry_with_no_registry_row_still_appears() -> None:
    """Happens while an integration is retrying setup; do not lose the device."""
    devices = await discover(config_entries=[make_entry(title="cloudbay-t-shed")])
    assert [d.node_name for d in devices] == ["cloudbay-t-shed"]


async def test_results_are_sorted_by_node_name() -> None:
    devices = await discover(
        config_entries=[
            make_entry("e1", "zeta-device"),
            make_entry("e2", "alpha-device"),
            make_entry("e3", "mid-device"),
        ],
    )
    assert [d.node_name for d in devices] == [
        "alpha-device", "mid-device", "zeta-device"
    ]


async def test_empty_home_assistant_yields_no_devices() -> None:
    assert await discover() == []


async def test_device_metadata_is_carried_through() -> None:
    devices = await discover(
        config_entries=[make_entry()],
        devices=[make_registry_device(model="esp32dev", sw_version="2024.6.0")],
    )
    assert devices[0].model == "esp32dev"
    assert devices[0].manufacturer == "espressif"
    assert devices[0].sw_version == "2024.6.0"
    assert devices[0].entry_id == "entry1"
    assert devices[0].device_id == "dev1"
