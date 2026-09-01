"""Telling parent (base) configs apart from the per-device files beside them.

Parents are not shipped with the add-on; they are the base configs already in
the ESPHome directory. That means classification is load-bearing: get it wrong
in one direction and the add-on generates nothing, get it wrong in the other and
it overwrites the file every device is built from.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeHaApi, make_entry, make_registry_device

from app.config_store import EsphomeConfigStore, ParentTemplateError
from app.discovery import DeviceDiscoveryService
from app.generator import YamlGenerator
from app.models import Device, DeviceStatus, Outcome
from app.orchestrator import ScanOrchestrator
from app.parents import GENERATED_MARKER, classify, derive_name_prefix
from app.templates import TemplateRepository

PARENT_MAC_PLACEHOLDER = "esphome:\n  name: cloudbay-t-${mac}\n"
PARENT_SUFFIX_FLAG = "esphome:\n  name: switchboard\n  name_add_mac_suffix: true\n"
CHILD = "esphome:\n  name: cloudbay-t-livingroom\n"


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


# -- auto-detection ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected,reason",
    [
        (PARENT_MAC_PLACEHOLDER, True, "mac-placeholder"),
        ("esphome:\n  name: fam-$mac\n", True, "mac-placeholder"),
        ("esphome:\n  name: fam-${mac_suffix}\n", True, "mac-placeholder"),
        (PARENT_SUFFIX_FLAG, True, "mac-suffix-flag"),
        ("esphome:\n  name: x\n  name_add_mac_suffix: yes\n", True, "mac-suffix-flag"),
        (CHILD, False, "default"),
        ("esphome:\n  name: x\n  name_add_mac_suffix: false\n", False, "default"),
        ("sensor:\n  - platform: uptime\n", False, "no-esphome-block"),
        ("esphome:\n  name: [unclosed\n", False, "unparseable"),
        ("# only a comment\n", False, "empty"),
    ],
)
def test_classification(raw: str, expected: bool, reason: str) -> None:
    verdict = classify(raw, Path("t.yaml"))
    assert verdict.is_parent is expected
    assert verdict.reason == reason


def test_a_generated_file_is_never_a_parent() -> None:
    """Our own output carries MAC logic nowhere, but the header settles it."""
    raw = f"{GENERATED_MARKER}\n# Template: base.yaml\n\nesphome:\n  name: x-${{mac}}\n"
    verdict = classify(raw)
    assert verdict.is_parent is False
    assert verdict.reason == "generated-header"


# -- explicit directives ----------------------------------------------------


def test_x_template_true_forces_a_parent() -> None:
    """For a base config that happens to carry no MAC-suffix logic."""
    verdict = classify("# x-template: true\nesphome:\n  name: plain-base\n")
    assert verdict.is_parent is True
    assert verdict.reason == "directive"


def test_x_template_false_forces_a_child() -> None:
    """Escape hatch for a device config that legitimately keeps MAC logic."""
    verdict = classify("# x-template: false\n" + PARENT_MAC_PLACEHOLDER)
    assert verdict.is_parent is False


def test_a_match_directive_alone_marks_a_parent() -> None:
    assert classify("# x-match-prefix: fam\nesphome:\n  name: whatever\n").is_parent


def test_directives_below_the_header_do_not_count() -> None:
    raw = "esphome:\n  name: child\n# x-template: true\n"
    assert classify(raw).is_parent is False


# -- the implicit family prefix --------------------------------------------


@pytest.mark.parametrize(
    "declared,expected",
    [
        ("cloudbay-t-${mac}", "cloudbay-t"),
        ("cloudbay-t_${mac}", "cloudbay-t"),
        ("fam-$mac", "fam"),
        ("${mac}", None),
        ("no-placeholder", None),
    ],
)
def test_derive_name_prefix(declared: str, expected: str | None) -> None:
    assert derive_name_prefix(declared) == expected


def test_the_family_prefix_comes_from_the_name_not_the_filename(tmp_path) -> None:
    """A parent called `base.yaml` still claims its family correctly."""
    (tmp_path / "base.yaml").write_text(PARENT_MAC_PLACEHOLDER)
    template = TemplateRepository(tmp_path).load_all()[0]
    assert template.name_prefix == "cloudbay-t"


def test_a_parent_matches_by_its_declared_family(tmp_path) -> None:
    from app.templates import TemplateMatcher

    (tmp_path / "base.yaml").write_text(PARENT_MAC_PLACEHOLDER)
    matcher = TemplateMatcher(TemplateRepository(tmp_path).load_all())

    match = matcher.match(Device(node_name="cloudbay-t-livingroom"))
    assert match is not None
    assert match.rule == "name-prefix"
    assert match.pattern == "cloudbay-t"
    # The boundary rule still applies to the derived prefix.
    assert matcher.match(Device(node_name="cloudbay-tx-porch")) is None


def test_the_suffix_flag_family_is_the_bare_name(tmp_path) -> None:
    from app.templates import TemplateMatcher

    (tmp_path / "sb.yaml").write_text(PARENT_SUFFIX_FLAG)
    matcher = TemplateMatcher(TemplateRepository(tmp_path).load_all())
    assert matcher.match(Device(node_name="switchboard-hallway")) is not None


# -- the config index must exclude parents ---------------------------------


def test_a_parent_is_not_counted_as_a_device_config(esphome_dir) -> None:
    """`name: switchboard` + the suffix flag must not make a real `switchboard`
    device look already-configured."""
    (esphome_dir / "switchboard.yaml").write_text(PARENT_SUFFIX_FLAG)
    store = EsphomeConfigStore(esphome_dir)

    assert store.has_config("switchboard") is False
    assert store.index() == {}
    assert store.parent_paths() == {esphome_dir / "switchboard.yaml"}


def test_children_are_still_indexed_beside_their_parent(esphome_dir) -> None:
    (esphome_dir / "cloudbay-t.yaml").write_text(PARENT_MAC_PLACEHOLDER)
    (esphome_dir / "cloudbay-t-livingroom.yaml").write_text(CHILD)
    store = EsphomeConfigStore(esphome_dir)

    assert store.has_config("cloudbay-t-livingroom") is True
    assert set(store.index()) == {"cloudbay-t-livingroom"}


def test_a_stale_sidecar_cannot_resurrect_a_parent(esphome_dir) -> None:
    import json

    (esphome_dir / "switchboard.yaml").write_text(PARENT_SUFFIX_FLAG)
    storage = esphome_dir / ".esphome" / "storage"
    storage.mkdir(parents=True)
    (storage / "switchboard.yaml.json").write_text(json.dumps({"name": "switchboard"}))

    assert EsphomeConfigStore(esphome_dir).has_config("switchboard") is False


# -- parents are protected from writes -------------------------------------


def test_a_parent_is_never_written_over(esphome_dir) -> None:
    """A device named exactly `cloudbay-t` targets the parent's own filename."""
    parent = esphome_dir / "cloudbay-t.yaml"
    parent.write_text(PARENT_MAC_PLACEHOLDER)
    store = EsphomeConfigStore(esphome_dir)

    with pytest.raises(ParentTemplateError):
        store.write("cloudbay-t", "clobbered\n")

    assert parent.read_text() == PARENT_MAC_PLACEHOLDER


def test_regenerate_cannot_overwrite_a_parent_either(esphome_dir) -> None:
    """The guard must hold under overwrite -- that is the dangerous path."""
    parent = esphome_dir / "cloudbay-t.yaml"
    parent.write_text(PARENT_MAC_PLACEHOLDER)
    store = EsphomeConfigStore(esphome_dir)

    with pytest.raises(ParentTemplateError):
        store.write("cloudbay-t", "clobbered\n", overwrite=True)

    assert parent.read_text() == PARENT_MAC_PLACEHOLDER
    assert not list(esphome_dir.glob("*.bak-*"))


async def test_a_scan_reports_rather_than_clobbering_a_parent(settings, esphome_dir) -> None:
    (esphome_dir / "cloudbay-t.yaml").write_text(PARENT_MAC_PLACEHOLDER)
    ha = FakeHaApi(
        config_entries=[make_entry(title="cloudbay-t")],
        devices=[make_registry_device(name="CloudBay T", mac="aa:bb:cc:dd:ee:ff")],
    )
    report = await build(settings, ha).scan()

    assert report.count(Outcome.ERROR) == 1
    assert "parent template" in report.devices[0].message
    assert (esphome_dir / "cloudbay-t.yaml").read_text() == PARENT_MAC_PLACEHOLDER


# -- end to end -------------------------------------------------------------


async def test_parents_in_the_esphome_dir_drive_generation(settings, esphome_dir) -> None:
    """The whole point: the base config already in ESPHome is the template."""
    (esphome_dir / "cloudbay-t.yaml").write_text(
        "substitutions:\n  mac: \"000000\"\n"
        "esphome:\n  name: cloudbay-t-${mac}\n  name_add_mac_suffix: true\n"
        "esp32:\n  board: esp32dev\n"
    )
    ha = FakeHaApi(
        config_entries=[make_entry(title="cloudbay-t-livingroom")],
        devices=[make_registry_device(mac="aa:bb:cc:dd:ee:ff")],
    )
    report = await build(settings, ha).scan()

    assert report.count(Outcome.GENERATED) == 1
    written = (esphome_dir / "cloudbay-t-livingroom.yaml").read_text()
    assert "name: cloudbay-t-livingroom" in written
    assert "name_add_mac_suffix: false" in written
    assert "board: esp32dev" in written


async def test_generated_children_never_become_parents(settings, esphome_dir) -> None:
    """Otherwise the second scan would treat the first scan's output as a base."""
    (esphome_dir / "cloudbay-t.yaml").write_text(PARENT_MAC_PLACEHOLDER)
    ha = FakeHaApi(
        config_entries=[make_entry(title="cloudbay-t-livingroom")],
        devices=[make_registry_device(mac="aa:bb:cc:dd:ee:ff")],
    )
    orchestrator = build(settings, ha)
    await orchestrator.scan()
    await orchestrator.scan()

    parents = [t.name for t in TemplateRepository(esphome_dir).load_all()]
    assert parents == ["cloudbay-t.yaml"], "the generated child was treated as a base"
    # Exactly the parent plus one child; the second scan added nothing.
    assert sorted(p.name for p in esphome_dir.glob("*.yaml")) == [
        "cloudbay-t-livingroom.yaml",
        "cloudbay-t.yaml",
    ]


async def test_nothing_is_written_into_the_addon_config_dir(settings, esphome_dir) -> None:
    """Regression on the whole point of this change: no seeding, anywhere."""
    ha = FakeHaApi()
    await build(settings, ha).scan()
    assert list(esphome_dir.iterdir()) == []


def test_a_hand_written_config_beside_a_parent_stays_a_child(esphome_dir) -> None:
    (esphome_dir / "cloudbay-t.yaml").write_text(PARENT_MAC_PLACEHOLDER)
    (esphome_dir / "lounge.yaml").write_text(
        "esphome:\n  name: cloudbay-t-lounge\n# hand written\n"
    )
    store = EsphomeConfigStore(esphome_dir)

    assert store.find("cloudbay-t-lounge").path.name == "lounge.yaml"
    assert store.is_parent(esphome_dir / "lounge.yaml") is False


def test_secrets_and_dotfiles_are_never_parents(esphome_dir) -> None:
    (esphome_dir / "secrets.yaml").write_text("wifi_ssid: net\n")
    (esphome_dir / ".hidden.yaml").write_text(PARENT_MAC_PLACEHOLDER)
    assert TemplateRepository(esphome_dir).load_all() == []


def test_a_device_status_fixture_is_unaffected() -> None:
    assert DeviceStatus.ONLINE.value == "online"
