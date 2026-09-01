"""MAC-suffix removal -- the transformation the add-on exists to perform.

Two idioms are covered, because real templates use both:

* ``name: cloudbay-t-${mac}``      -- the substitution style in the spec
* ``name_add_mac_suffix: true``    -- ESPHome's own mechanism, which appends
  the last three bytes of the MAC as ``<name>-aabbcc``
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.generator import YamlGenerator
from app.models import (
    Device,
    DeviceStatus,
    MacPolicy,
    MacSuffixAction,
    TemplateSpec,
)


def template(raw: str) -> TemplateSpec:
    return TemplateSpec(name="t.yaml", path=Path("t.yaml"), raw=raw)


def device(mac: str | None = "aabbccddeeff") -> Device:
    return Device(
        node_name="cloudbay-t-livingroom",
        friendly_name="CloudBay T Living Room",
        mac=mac,
        status=DeviceStatus.ONLINE,
    )


# -- rule A: the node name --------------------------------------------------


def test_mac_placeholder_in_name_becomes_the_device_name(generator) -> None:
    out = generator.generate(template("esphome:\n  name: cloudbay-t-${mac}\n"), device())
    assert "name: cloudbay-t-livingroom" in out.content
    assert "${mac}" not in out.content


def test_substitution_reference_in_name_becomes_the_device_name(generator) -> None:
    raw = "substitutions:\n  devicename: cloudbay-t\nesphome:\n  name: ${devicename}\n"
    out = generator.generate(template(raw), device())
    assert "name: cloudbay-t-livingroom" in out.content


def test_plain_literal_name_is_still_replaced(generator) -> None:
    """The generated file names the device it is for, always."""
    out = generator.generate(template("esphome:\n  name: base-template\n"), device())
    assert "name: cloudbay-t-livingroom" in out.content
    assert "base-template" not in out.content


def test_name_is_quoted_only_when_it_has_to_be(generator) -> None:
    out = generator.generate(template('esphome:\n  name: "quoted-${mac}"\n'), device())
    assert 'name: "cloudbay-t-livingroom"' in out.content


# -- name_add_mac_suffix ----------------------------------------------------


def test_name_add_mac_suffix_is_disabled() -> None:
    gen = YamlGenerator(MacPolicy.SUFFIX3, MacSuffixAction.SET_FALSE)
    raw = "esphome:\n  name: switchboard\n  name_add_mac_suffix: true\n"
    out = gen.generate(template(raw), device())

    assert "name_add_mac_suffix: false" in out.content
    assert "name_add_mac_suffix: true" not in out.content


def test_name_add_mac_suffix_can_be_removed_entirely() -> None:
    gen = YamlGenerator(MacPolicy.SUFFIX3, MacSuffixAction.REMOVE)
    raw = "esphome:\n  name: switchboard\n  name_add_mac_suffix: true\n  comment: keep\n"
    out = gen.generate(template(raw), device())

    assert "name_add_mac_suffix" not in out.content
    assert "comment: keep" in out.content


def test_already_false_suffix_flag_is_left_alone(generator) -> None:
    raw = "esphome:\n  name: x\n  name_add_mac_suffix: false\n"
    out = generator.generate(template(raw), device())
    assert out.content.count("name_add_mac_suffix: false") == 1


def test_removal_keeps_surrounding_lines_intact() -> None:
    gen = YamlGenerator(MacPolicy.SUFFIX3, MacSuffixAction.REMOVE)
    raw = (
        "esphome:\n"
        "  name: switchboard\n"
        "  name_add_mac_suffix: true\n"
        "  friendly_name: Board\n"
        "\n"
        "logger:\n"
    )
    out = gen.generate(template(raw), device())

    assert "friendly_name: Board" in out.content
    assert "logger:" in out.content
    assert "name_add_mac_suffix" not in out.content


# -- rule B: ${mac} elsewhere ----------------------------------------------


def test_ap_ssid_gets_the_last_three_bytes(generator) -> None:
    """The spec's chosen policy: ${mac} -> aabbcc, matching ESPHome's format."""
    raw = 'esphome:\n  name: x\nwifi:\n  ap:\n    ssid: "${name}-${mac}"\n'
    out = generator.generate(template(raw), device())
    assert 'ssid: "cloudbay-t-livingroom-ddeeff"' in out.content


def test_full_mac_policy() -> None:
    gen = YamlGenerator(MacPolicy.FULL, MacSuffixAction.SET_FALSE)
    raw = 'esphome:\n  name: x\nwifi:\n  ap:\n    ssid: "ap-${mac}"\n'
    out = gen.generate(template(raw), device())
    assert 'ssid: "ap-aabbccddeeff"' in out.content


def test_strip_policy_also_eats_the_separator() -> None:
    """`"${name}-${mac}"` must strip to `"${name}"`, not `"${name}-"`."""
    gen = YamlGenerator(MacPolicy.STRIP, MacSuffixAction.SET_FALSE)
    raw = 'esphome:\n  name: x\nwifi:\n  ap:\n    ssid: "ap-${mac}"\n'
    out = gen.generate(template(raw), device())
    assert 'ssid: "ap"' in out.content


def test_per_template_policy_overrides_the_global_one() -> None:
    gen = YamlGenerator(MacPolicy.SUFFIX3, MacSuffixAction.SET_FALSE)
    spec = TemplateSpec(
        name="t.yaml",
        path=Path("t.yaml"),
        raw='esphome:\n  name: x\nwifi:\n  ap:\n    ssid: "ap-${mac}"\n',
        mac_policy=MacPolicy.FULL,
    )
    assert 'ssid: "ap-aabbccddeeff"' in gen.generate(spec, device()).content


def test_brace_less_placeholder_is_handled(generator) -> None:
    raw = 'esphome:\n  name: x\nwifi:\n  ap:\n    ssid: "ap-$mac"\n'
    out = generator.generate(template(raw), device())
    assert 'ssid: "ap-ddeeff"' in out.content


# -- substitutions block ----------------------------------------------------


def test_declared_substitution_is_patched_at_its_definition(generator) -> None:
    """One-line diff at the definition beats inlining at every use site."""
    raw = (
        'substitutions:\n  mac: "000000"\n'
        'esphome:\n  name: x\nwifi:\n  ap:\n    ssid: "ap-${mac}"\n'
    )
    out = generator.generate(template(raw), device())

    assert 'mac: "ddeeff"' in out.content
    # The reference resolves through the substitution, so it stays put.
    assert 'ssid: "ap-${mac}"' in out.content


def test_undeclared_placeholder_is_inlined(generator) -> None:
    raw = 'esphome:\n  name: x\nwifi:\n  ap:\n    ssid: "ap-${mac}"\n'
    out = generator.generate(template(raw), device())
    assert 'ssid: "ap-ddeeff"' in out.content


def test_name_substitution_is_patched(generator) -> None:
    raw = "substitutions:\n  devicename: cloudbay-t\nesphome:\n  name: x\n"
    out = generator.generate(template(raw), device())
    assert "devicename: cloudbay-t-livingroom" in out.content


# -- missing MAC ------------------------------------------------------------


def test_placeholders_are_stripped_when_no_mac_is_known(generator) -> None:
    """A literal ${mac} left in a config would fail to compile, so remove it."""
    raw = 'esphome:\n  name: cloudbay-t-${mac}\nwifi:\n  ap:\n    ssid: "ap-${mac}"\n'
    out = generator.generate(template(raw), device(mac=None))

    assert "${mac}" not in out.content
    assert 'ssid: "ap"' in out.content
    assert out.warnings
    assert "did not expose a MAC" in out.warnings[0]


def test_missing_mac_empties_a_declared_substitution(generator) -> None:
    raw = 'substitutions:\n  mac: "000000"\nesphome:\n  name: x\n'
    out = generator.generate(template(raw), device(mac=None))

    assert 'mac: ""' in out.content
    assert any("left empty" in w for w in out.warnings)


def test_a_value_is_never_emptied_by_substitution(generator) -> None:
    """`ssid: ${mac}` with no MAC would become `ssid:` -- null. Refuse."""
    gen = YamlGenerator(MacPolicy.STRIP, MacSuffixAction.SET_FALSE)
    raw = "esphome:\n  name: x\nwifi:\n  ap:\n    ssid: ${mac}\n"
    out = gen.generate(template(raw), device())

    assert "ssid: ${mac}" in out.content
    assert any("would empty the value" in w for w in out.warnings)


@pytest.mark.parametrize("mac,expected", [
    ("aabbccddeeff", "ddeeff"),
    ("AA:BB:CC:11:22:33", "112233"),
    ("aa-bb-cc-dd-ee-ff", "ddeeff"),
])
def test_mac_suffix_normalisation(mac: str, expected: str) -> None:
    from app.models import normalise_mac

    assert Device(node_name="x", mac=normalise_mac(mac)).mac_suffix == expected
