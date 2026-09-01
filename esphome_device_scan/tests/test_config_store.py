"""The YAML-presence check and the never-overwrite guarantee."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config_store import ConfigExistsError, EsphomeConfigStore


def write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


# -- indexing from YAML -----------------------------------------------------


def test_finds_a_device_by_its_declared_name(store, esphome_dir) -> None:
    write(esphome_dir, "lounge.yaml", "esphome:\n  name: cloudbay-t-livingroom\n")

    # The filename and the declared node name differ; only the latter counts.
    assert store.has_config("cloudbay-t-livingroom") is True
    assert store.find("cloudbay-t-livingroom").path.name == "lounge.yaml"
    assert store.find("cloudbay-t-livingroom").source == "yaml"


def test_resolves_substitutions_in_the_declared_name(store, esphome_dir) -> None:
    write(
        esphome_dir,
        "device.yaml",
        "substitutions:\n  devicename: porch-light\nesphome:\n  name: ${devicename}\n",
    )
    assert store.has_config("porch-light") is True


def test_resolves_a_composite_substituted_name(store, esphome_dir) -> None:
    write(
        esphome_dir,
        "device.yaml",
        'substitutions:\n  prefix: cloudbay-t\n  room: attic\n'
        "esphome:\n  name: ${prefix}-${room}\n",
    )
    assert store.has_config("cloudbay-t-attic") is True


def test_unresolvable_substitution_is_left_alone(store, esphome_dir) -> None:
    write(esphome_dir, "device.yaml", "esphome:\n  name: thing-${undefined}\n")
    assert store.has_config("thing-${undefined}") is True


def test_lookup_is_case_insensitive(store, esphome_dir) -> None:
    write(esphome_dir, "a.yaml", "esphome:\n  name: cloudbay-t-livingroom\n")
    assert store.has_config("CloudBay-T-LivingRoom") is True


def test_secrets_file_is_ignored(store, esphome_dir) -> None:
    write(esphome_dir, "secrets.yaml", "wifi_ssid: mynetwork\n")
    assert store.index() == {}


def test_hidden_files_are_ignored(store, esphome_dir) -> None:
    write(esphome_dir, ".hidden.yaml", "esphome:\n  name: ghost\n")
    assert store.has_config("ghost") is False


def test_unparseable_yaml_falls_back_to_the_filename(store, esphome_dir) -> None:
    """A broken file must still block generation, or we would clobber it."""
    write(esphome_dir, "broken.yaml", "esphome:\n  name: [unclosed\n")
    assert store.has_config("broken") is True


def test_a_package_fragment_falls_back_to_its_filename(store, esphome_dir) -> None:
    write(esphome_dir, "common.yaml", "sensor:\n  - platform: uptime\n")
    assert store.has_config("common") is True


def test_missing_directory_is_not_an_error(tmp_path) -> None:
    assert EsphomeConfigStore(tmp_path / "nope").index() == {}


# -- indexing from the .esphome storage sidecar -----------------------------


def test_reads_the_storage_json_index(store, esphome_dir) -> None:
    storage = esphome_dir / ".esphome" / "storage"
    storage.mkdir(parents=True)
    (storage / "lounge.yaml.json").write_text(
        json.dumps({"name": "cloudbay-t-livingroom", "esp_platform": "ESP32"})
    )

    found = store.find("cloudbay-t-livingroom")
    assert found is not None
    assert found.source == "storage"
    assert found.path.name == "lounge.yaml"


def test_yaml_wins_over_the_sidecar(store, esphome_dir) -> None:
    storage = esphome_dir / ".esphome" / "storage"
    storage.mkdir(parents=True)
    (storage / "old.yaml.json").write_text(json.dumps({"name": "shared-name"}))
    write(esphome_dir, "current.yaml", "esphome:\n  name: shared-name\n")

    found = store.find("shared-name")
    assert found.source == "yaml"
    assert found.path.name == "current.yaml"


def test_corrupt_storage_json_is_skipped(store, esphome_dir) -> None:
    storage = esphome_dir / ".esphome" / "storage"
    storage.mkdir(parents=True)
    (storage / "bad.yaml.json").write_text("{not json")
    (storage / "good.yaml.json").write_text(json.dumps({"name": "good-device"}))

    assert store.has_config("good-device") is True
    assert len(store.index()) == 1


# -- writing ----------------------------------------------------------------


def test_write_creates_the_file(store, esphome_dir) -> None:
    path = store.write("new-device", "esphome:\n  name: new-device\n")
    assert path == esphome_dir / "new-device.yaml"
    assert path.read_text() == "esphome:\n  name: new-device\n"


def test_write_creates_a_missing_directory(tmp_path) -> None:
    store = EsphomeConfigStore(tmp_path / "esphome")
    path = store.write("thing", "esphome:\n  name: thing\n")
    assert path.exists()


def test_write_refuses_to_overwrite(store, esphome_dir) -> None:
    write(esphome_dir, "taken.yaml", "esphome:\n  name: taken\n")
    with pytest.raises(ConfigExistsError):
        store.write("taken", "new content\n")
    assert "name: taken" in (esphome_dir / "taken.yaml").read_text()


def test_write_refuses_when_another_file_declares_the_name(store, esphome_dir) -> None:
    """The collision is on the node name, not the filename."""
    write(esphome_dir, "lounge.yaml", "esphome:\n  name: cloudbay-t-livingroom\n")
    with pytest.raises(ConfigExistsError):
        store.write("cloudbay-t-livingroom", "replacement\n")


def test_overwrite_backs_up_first(store, esphome_dir) -> None:
    write(esphome_dir, "device.yaml", "original content\n")
    store.write("device", "new content\n", overwrite=True)

    assert (esphome_dir / "device.yaml").read_text() == "new content\n"
    backups = list(esphome_dir.glob("device.yaml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "original content\n"


def test_overwrite_rewrites_the_differently_named_file_in_place(store, esphome_dir) -> None:
    """The user's filename is kept. Writing to <node>.yaml instead would leave
    two configs declaring the same esphome.name."""
    write(esphome_dir, "lounge.yaml", "esphome:\n  name: cloudbay-t-livingroom\n")

    path = store.write("cloudbay-t-livingroom", "new\n", overwrite=True)

    assert path.name == "lounge.yaml"
    assert path.read_text() == "new\n"
    assert not (esphome_dir / "cloudbay-t-livingroom.yaml").exists()
    assert list(esphome_dir.glob("lounge.yaml.bak-*"))


def test_no_temp_files_are_left_behind(store, esphome_dir) -> None:
    store.write("device", "content\n")
    assert [p.name for p in esphome_dir.iterdir()] == ["device.yaml"]


def test_the_index_refreshes_after_a_write(store, esphome_dir) -> None:
    assert store.has_config("fresh") is False
    store.write("fresh", "esphome:\n  name: fresh\n")
    assert store.has_config("fresh") is True


def test_read_returns_the_file_content(store, esphome_dir) -> None:
    store.write("device", "esphome:\n  name: device\n")
    assert store.read("device") == "esphome:\n  name: device\n"


def test_read_of_an_unknown_device_is_none(store) -> None:
    assert store.read("nope") is None


def test_target_path(store, esphome_dir) -> None:
    assert store.target_path("thing") == esphome_dir / "thing.yaml"
