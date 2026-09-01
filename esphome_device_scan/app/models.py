"""Domain types shared across the add-on.

Everything here is a frozen dataclass: the scan pipeline is a chain of pure
transformations over these values, which is what makes generation deterministic
and unit-testable without any I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

# ESPHome node names: lowercase letters, digits and hyphens only.
# https://esphome.io/components/esphome/ -- `name` must match this charset.
ESPHOME_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class DeviceStatus(StrEnum):
    """How Home Assistant currently sees the device."""

    ONLINE = "online"
    OFFLINE = "offline"
    #: Home Assistant has discovered it but no config entry exists yet.
    DISCOVERED = "discovered"
    UNKNOWN = "unknown"


class DeviceSource(StrEnum):
    """Which Home Assistant surface the device was read from."""

    CONFIG_ENTRY = "config_entry"
    DISCOVERY_FLOW = "discovery_flow"


class MacPolicy(StrEnum):
    """What to substitute for ``${mac}`` outside the node name."""

    #: Last three bytes, matching ESPHome's own ``name_add_mac_suffix`` format.
    SUFFIX3 = "suffix3"
    #: Full MAC, lowercase, separators stripped.
    FULL = "full"
    #: Drop the placeholder (and a directly preceding separator) entirely.
    STRIP = "strip"


class MacSuffixAction(StrEnum):
    """What to do with an ``esphome.name_add_mac_suffix: true`` key."""

    #: Rewrite the value to ``false`` -- explicit, and reads well in a diff.
    SET_FALSE = "set_false"
    #: Delete the whole key/value line.
    REMOVE = "remove"


class Outcome(StrEnum):
    """Per-device result of a scan."""

    GENERATED = "generated"
    REGENERATED = "regenerated"
    WOULD_GENERATE = "would_generate"
    SKIPPED_HAS_CONFIG = "skipped_has_config"
    SKIPPED_AUTO_GENERATE_OFF = "skipped_auto_generate_off"
    NO_TEMPLATE_MATCH = "no_template_match"
    ERROR = "error"


def normalise_mac(raw: str | None) -> str | None:
    """Return a MAC as 12 lowercase hex chars, or None if unparseable.

    Home Assistant stores device-registry MACs colon-separated and lowercase
    (``format_mac()``), while ESPHome's zeroconf discovery flow uses the bare
    12-character form as its unique_id. Accept both, plus the hyphen and dot
    conventions, so callers never have to care which surface a MAC came from.
    """
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
    return cleaned if len(cleaned) == 12 else None


def slugify_node_name(raw: str | None) -> str | None:
    """Coerce a human name into something valid as an ESPHome node name.

    Mirrors what a user would type by hand: lowercase, spaces/underscores to
    hyphens, everything else dropped, runs collapsed. Returns None when nothing
    usable survives.
    """
    if not raw:
        return None
    slug = re.sub(r"[\s_]+", "-", raw.strip().lower())
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or None


@dataclass(frozen=True)
class Device:
    """An ESPHome device as Home Assistant knows it."""

    #: The ESPHome node name. Determines the generated file's basename.
    node_name: str
    friendly_name: str | None = None
    #: 12 lowercase hex chars, or None when Home Assistant has not exposed it.
    mac: str | None = None
    status: DeviceStatus = DeviceStatus.UNKNOWN
    source: DeviceSource = DeviceSource.CONFIG_ENTRY
    device_id: str | None = None
    entry_id: str | None = None
    host: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    sw_version: str | None = None
    area_id: str | None = None
    #: How node_name was derived, for display and troubleshooting.
    name_source: str = "unknown"

    @property
    def mac_suffix(self) -> str | None:
        """Last three bytes of the MAC (``aabbcc``), ESPHome's suffix format."""
        return self.mac[-6:] if self.mac else None

    @property
    def mac_pretty(self) -> str | None:
        """Colon-separated uppercase MAC, for display only."""
        if not self.mac:
            return None
        return ":".join(self.mac[i : i + 2] for i in range(0, 12, 2)).upper()

    @property
    def display_name(self) -> str:
        return self.friendly_name or self.node_name


@dataclass(frozen=True)
class TemplateSpec:
    """A base YAML template plus the matching directives found in its header."""

    name: str
    path: Path
    raw: str
    match_prefixes: tuple[str, ...] = ()
    match_regexes: tuple[str, ...] = ()
    match_models: tuple[str, ...] = ()
    priority: int = 0
    #: Per-template override of the global MAC policy, or None to inherit.
    mac_policy: MacPolicy | None = None
    #: Directives that were present but unparseable, surfaced in the UI.
    warnings: tuple[str, ...] = ()
    #: Family prefix taken from the parent's own ``esphome.name``:
    #: ``cloudbay-t-${mac}`` yields ``cloudbay-t``. More reliable than the
    #: filename, which may be anything.
    name_prefix: str | None = None
    #: Why this file was classified as a parent (see :mod:`app.parents`).
    detected_by: str = "unknown"

    @property
    def stem(self) -> str:
        """Filename without extension -- the last-resort implicit prefix."""
        return self.path.stem


@dataclass(frozen=True)
class TemplateMatch:
    """Why a given template was chosen for a device."""

    template: TemplateSpec
    #: One of: regex, prefix, filename-prefix, model.
    rule: str
    #: The literal pattern that matched, for display.
    pattern: str
    #: Higher wins. Encodes rule precedence plus the template's own priority.
    score: tuple[int, int, int]


@dataclass(frozen=True)
class TextEdit:
    """A replacement over a half-open byte range of the template source."""

    start: int
    end: int
    replacement: str
    reason: str


@dataclass(frozen=True)
class GeneratedYaml:
    """Output of the generator: the rendered file plus an audit trail."""

    content: str
    edits: tuple[TextEdit, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeviceReport:
    """What the scan decided for one device."""

    device: Device
    outcome: Outcome
    has_yaml: bool
    template_name: str | None = None
    match_rule: str | None = None
    path: str | None = None
    message: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanReport:
    """Aggregate result of one scan pass."""

    devices: tuple[DeviceReport, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)

    def count(self, outcome: Outcome) -> int:
        return sum(1 for d in self.devices if d.outcome is outcome)

    @property
    def summary(self) -> str:
        """One line describing the pass, naming only what actually happened.

        Built from the non-zero counts rather than a fixed list, so a bulk
        regenerate reads "3 regenerated" instead of "0 generated, 0 pending,
        0 already configured" -- which is what a fixed list produced.
        """
        parts = [
            (self.count(Outcome.GENERATED), "generated"),
            (self.count(Outcome.REGENERATED), "regenerated"),
            (self.count(Outcome.WOULD_GENERATE), "pending"),
            (self.count(Outcome.SKIPPED_HAS_CONFIG), "already configured"),
            (self.count(Outcome.SKIPPED_AUTO_GENERATE_OFF), "awaiting generation"),
            (self.count(Outcome.NO_TEMPLATE_MATCH), "unmatched"),
            (self.count(Outcome.ERROR), "error(s)"),
        ]
        described = [f"{count} {label}" for count, label in parts if count]
        detail = ", ".join(described) if described else "nothing to do"
        return f"{len(self.devices)} device(s): {detail}"
