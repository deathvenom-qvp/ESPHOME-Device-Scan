"""Shared fixtures.

The whole point of the dependency injection in the app modules is visible here:
a :class:`FakeHaApi` stands in for Home Assistant, so every test runs offline
and deterministically.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config_store import EsphomeConfigStore
from app.generator import YamlGenerator
from app.models import (
    Device,
    DeviceStatus,
    MacPolicy,
    MacSuffixAction,
    TemplateSpec,
)
from app.settings import Settings
from app.templates import TemplateRepository, parse_template

#: The add-on's real shipped templates. Tests read these rather than a copy,
#: so the golden files in examples/generated/ can never drift from the
#: templates the add-on actually installs.
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


class FakeHaApi:
    """In-memory stand-in for Home Assistant's registries."""

    def __init__(
        self,
        config_entries: list[dict[str, Any]] | None = None,
        devices: list[dict[str, Any]] | None = None,
        entities: list[dict[str, Any]] | None = None,
        states: list[dict[str, Any]] | None = None,
        flows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._entries = config_entries or []
        self._devices = devices or []
        self._entities = entities or []
        self._states = states or []
        self._flows = flows or []

    async def list_config_entries(self, domain: str = "esphome"):
        return [e for e in self._entries if e.get("domain") == domain]

    async def list_devices(self):
        return list(self._devices)

    async def list_entities(self):
        return list(self._entities)

    async def list_states(self):
        return list(self._states)

    async def list_discovery_flows(self):
        return list(self._flows)


def make_entry(
    entry_id: str = "entry1",
    title: str = "cloudbay-t-livingroom",
    state: str = "loaded",
) -> dict[str, Any]:
    return {
        "entry_id": entry_id,
        "domain": "esphome",
        "title": title,
        "state": state,
        "source": "zeroconf",
    }


def make_registry_device(
    device_id: str = "dev1",
    entry_id: str = "entry1",
    name: str = "CloudBay T Living Room",
    mac: str | None = "aa:bb:cc:dd:ee:ff",
    **extra: Any,
) -> dict[str, Any]:
    connections = [["mac", mac]] if mac else []
    return {
        "id": device_id,
        "name": name,
        "name_by_user": None,
        "connections": connections,
        "identifiers": [],
        "config_entries": [entry_id],
        "primary_config_entry": entry_id,
        "manufacturer": "espressif",
        "model": "esp32dev",
        "sw_version": "2024.6.0",
        "area_id": None,
        **extra,
    }


@pytest.fixture
def shipped_templates_dir() -> Path:
    return TEMPLATES


@pytest.fixture
def cloudbay_template() -> TemplateSpec:
    path = TEMPLATES / "cloudbay-t.yaml"
    return parse_template(path, path.read_text(encoding="utf-8"))


@pytest.fixture
def switchboard_template() -> TemplateSpec:
    path = TEMPLATES / "switchboard.yaml"
    return parse_template(path, path.read_text(encoding="utf-8"))


@pytest.fixture
def device() -> Device:
    return Device(
        node_name="cloudbay-t-livingroom",
        friendly_name="CloudBay T Living Room",
        mac="aabbccddeeff",
        status=DeviceStatus.ONLINE,
        model="esp32dev",
        manufacturer="espressif",
    )


@pytest.fixture
def generator() -> YamlGenerator:
    return YamlGenerator(MacPolicy.SUFFIX3, MacSuffixAction.SET_FALSE)


@pytest.fixture
def esphome_dir(tmp_path: Path) -> Path:
    path = tmp_path / "esphome"
    path.mkdir()
    return path


@pytest.fixture
def store(esphome_dir: Path) -> EsphomeConfigStore:
    return EsphomeConfigStore(esphome_dir)


@pytest.fixture
def templates_repo() -> TemplateRepository:
    return TemplateRepository(TEMPLATES)


@pytest.fixture
def settings(esphome_dir: Path) -> Settings:
    import logging

    return Settings(
        esphome_config_dir=esphome_dir,
        templates_dir=TEMPLATES,
        scan_interval_minutes=15,
        auto_generate=True,
        scan_on_startup=False,
        mac_policy=MacPolicy.SUFFIX3,
        name_add_mac_suffix_action=MacSuffixAction.SET_FALSE,
        dry_run=False,
        log_level=logging.DEBUG,
    )
